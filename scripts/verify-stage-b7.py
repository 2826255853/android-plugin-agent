"""Inject model faults into the durable receiver and verify Android recovery."""

import argparse
import hashlib
import http.client
import io
import json
import os
import runpy
import sqlite3
import subprocess
import threading
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from dialogue_coordinator import DialogueCoordinator, envelope, timestamp
from dialogue_draft import MOCK_NOTES_REQUEST, mock_draft
from model_boundary import MAX_CLARIFICATIONS, MAX_OUTPUT_BYTES, MAX_OUTPUT_TOKENS, MOCK_MODEL
from model_boundary import RATES, Config, MockProvider, ProviderReply, Usage
from receiver_inbox import Inbox


ROOT = Path(__file__).resolve().parents[1]
B3 = runpy.run_path(str(ROOT / "scripts/verify-stage-b3.py"))
A2, A5 = B3["A2"], B3["A5"]
RESERVE = RATES["mock"].ceiling(MAX_OUTPUT_TOKENS)
MOCK_CHARGE = RATES["mock"].cost(Usage(24, 12))
BODY = "B7 fictional private requirement."
RAW_OUTPUT = "B7 fictional raw response."
FAKE_KEY = "sk-fictional-B7-000000000000"
BUDGET_MESSAGE = "项目剩余模型预算不足。请在电脑核对用量和预算后，再发送新消息。"


class Provider:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def invoke(self, request, stop):
        self.calls += 1
        return self.response(request, stop)


class StageB7Tests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="stage-b7-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.store = self.home / "projects"

    def open(self, db, provider=None, budget=25_000_000):
        return DialogueCoordinator(Inbox(db), provider, config=Config("mock", MOCK_MODEL, budget),
                                   spec_store=self.store)

    def ingest(self, coordinator, sequence=1, *, project="fixture-project", device="fixture-phone"):
        message = B3["send"]({"deviceId": device, "credentialId": str(uuid4())}, project, sequence)
        message.update(body=BODY, bodySha256=hashlib.sha256(BODY.encode()).hexdigest())
        row, _ = Inbox(coordinator.database, coordinator.lock).save(
            message, timestamp(), datetime.now(timezone.utc) - timedelta(days=1)
        )
        coordinator.ingest(row)
        return B3["scope_of"](message)

    def events(self, coordinator, key):
        events = []
        after = 0
        while True:
            page = coordinator.pull(envelope(key, "dialogue.pull", after=after, limit=20))
            events.extend(page["events"])
            if not page["hasMore"]:
                return events
            after = page["next"]

    def assert_failed(self, coordinator, key, code):
        events = self.events(coordinator, key)
        self.assertEqual(events[-1]["code"], code)
        self.assertFalse(any(event.get("state") == "confirmed" for event in events))
        self.assertFalse(self.store.exists())

    def test_budget_uses_settled_cost_across_devices_and_restarts(self):
        provider = Provider(MockProvider().invoke)
        path = self.home / "inbox.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            coordinator = self.open(db, provider, budget=RESERVE)
            first = self.ingest(coordinator)
            job = coordinator._claim()
            self.assertEqual(coordinator.model_ledger(first[1])["reserved_nano_usd"], RESERVE)
            coordinator._call(*job)
            coordinator._call(*job)
            ledger = coordinator.model_ledger(first[1])
            self.assertEqual((ledger["charged_nano_usd"], ledger["reserved_nano_usd"]), (MOCK_CHARGE, 0))
            self.assertEqual(provider.calls, 1)
        with closing(sqlite3.connect(path)) as db:
            coordinator = self.open(db, provider, budget=RESERVE)
            blocked = self.ingest(coordinator, device="other-phone")
            self.assertEqual(coordinator._claim(), (blocked, None))
            self.assert_failed(coordinator, blocked, "MODEL_BUDGET_EXHAUSTED")
            self.assertEqual(len(coordinator.model_ledger(first[1])["calls"]), 1)
            self.ingest(coordinator, project="other-project")
            coordinator._call(*coordinator._claim())
            self.assertEqual(provider.calls, 2)

    def test_provider_failures_classify_usage_and_discard_content(self):
        usage = Usage(24, 12)

        def disconnect(*_):
            raise ConnectionResetError(FAKE_KEY + BODY + RAW_OUTPUT)

        cases = (
            ("MODEL_AUTH_FAILED", ProviderReply(None, Usage(0, 0), "MODEL_AUTH_FAILED"), "failed", 0),
            ("MODEL_UNAVAILABLE", disconnect, "uncertain", RESERVE),
            ("MODEL_TIMEOUT", ProviderReply(None, None, "MODEL_TIMEOUT"), "uncertain", RESERVE),
            ("MODEL_INVALID_OUTPUT", ProviderReply(RAW_OUTPUT + FAKE_KEY, usage), "failed", MOCK_CHARGE),
            ("MODEL_OUTPUT_TOO_LARGE", ProviderReply(RAW_OUTPUT * MAX_OUTPUT_BYTES, usage), "failed", MOCK_CHARGE),
            ("MODEL_USAGE_UNCERTAIN", ProviderReply("{}", Usage(-1, 12)), "uncertain", RESERVE),
        )
        for code, response, status, charge in cases:
            with self.subTest(code=code), closing(sqlite3.connect(":memory:")) as db:
                provider = Provider(response if callable(response) else lambda *_: response)
                coordinator = self.open(db, provider)
                key = self.ingest(coordinator)
                job = coordinator._claim()
                logs = io.StringIO()
                with redirect_stdout(logs), redirect_stderr(logs):
                    coordinator._call(*job)
                    coordinator._call(*job)
                self.assert_failed(coordinator, key, code)
                ledger = coordinator.model_ledger(key[1])
                call, = ledger["calls"]
                self.assertEqual((call["status"], call["charged_nano_usd"]), (status, charge))
                self.assertEqual(provider.calls, 1)
                self.assertIsNone(coordinator._claim())
                exported = json.dumps([ledger, self.events(coordinator, key)])
                for private in (BODY, RAW_OUTPUT, FAKE_KEY):
                    self.assertNotIn(private, exported)
                    self.assertNotIn(private, logs.getvalue())
                self.assertIsNone(db.execute("SELECT candidate_json FROM dialogue_model_calls").fetchone()[0])

    def test_deadline_persists_uncertainty_and_ignores_late_candidate(self):
        release, stopped = threading.Event(), threading.Event()

        def late(request, stop):
            if stop.wait(3):
                stopped.set()
            release.wait(3)
            return MockProvider().invoke(request, stop)

        provider = Provider(late)
        with closing(sqlite3.connect(":memory:")) as db:
            coordinator = self.open(db, provider)
            key = self.ingest(coordinator)
            _, request = coordinator._claim()
            try:
                coordinator._call(key, replace(request, timeout_seconds=0.1))
                self.assertTrue(stopped.wait(2))
                self.assert_failed(coordinator, key, "MODEL_TIMEOUT")
            finally:
                release.set()
            call, = coordinator.model_ledger(key[1])["calls"]
            self.assertEqual((call["status"], call["charged_nano_usd"]), ("uncertain", RESERVE))
            self.assertGreaterEqual(call["elapsed_ms"], 100)
            self.assertIsNone(call["content_sha256"])
            self.assertEqual(provider.calls, 1)
            self.assertIsNone(coordinator._claim())

    def test_cancel_is_idempotent_and_keeps_uncertain_usage(self):
        started, release = threading.Event(), threading.Event()

        def active(request, stop):
            started.set()
            release.wait(5)
            return MockProvider().invoke(request, stop)

        provider = Provider(active)
        with closing(sqlite3.connect(":memory:", check_same_thread=False)) as db:
            coordinator = self.open(db, provider)
            key = self.ingest(coordinator)
            coordinator.start()
            try:
                self.assertTrue(started.wait(3))
                cancel = B3["cancelled"](key, self.events(coordinator, key)[-1])
                self.assertFalse(coordinator.submit(cancel)["duplicate"])
                self.assertTrue(coordinator.submit(cancel)["duplicate"])
                A5["wait_for"](lambda: coordinator.model_ledger(key[1])["charged_nano_usd"] == RESERVE, "cancel settlement")
            finally:
                release.set()
                coordinator.stop()
            self.assertEqual(self.events(coordinator, key)[-1]["state"], "cancelled")
            self.assertEqual(coordinator.model_ledger(key[1])["calls"][0]["status"], "uncertain")
            self.assertEqual(provider.calls, 1)
            self.assertFalse(self.store.exists())

    def test_round_limit_is_persistent_and_needs_no_extra_call(self):
        empty = json.dumps(mock_draft(BODY))
        provider = Provider(lambda *_: ProviderReply(empty, Usage(24, 12)))
        path = self.home / "inbox.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            coordinator = self.open(db, provider)
            key = self.ingest(coordinator)
            for sequence in range(1, MAX_CLARIFICATIONS + 2):
                coordinator._call(*coordinator._claim())
                head = self.events(coordinator, key)[-1]
                answer = envelope(key, "phone.answer", eventId=str(uuid4()), sequence=sequence,
                                  createdAt=timestamp(), messageId=key[2], inReplyTo=head["eventId"],
                                  text="A fictional answer.", specVersion=head["specVersion"], specSha256=head["specSha256"])
                self.assertFalse(coordinator.submit(answer)["duplicate"])
                self.assertTrue(coordinator.submit(answer)["duplicate"])
        with closing(sqlite3.connect(path)) as db:
            coordinator = self.open(db, provider)
            self.assertEqual(coordinator._claim(), (key, None))
            self.assert_failed(coordinator, key, "MODEL_ROUND_LIMIT")
            self.assertEqual(provider.calls, 1 + MAX_CLARIFICATIONS)

    def test_receiver_process_interrupts_and_reconciles_without_replay(self):
        project = "b7-crash-project"
        with B3["Console"](self.home, "127.0.0.1", spec_store=self.store) as receiver:
            paired = B3["pair"](receiver, "b7-fixture-phone")
            receiver.stop(paired["credential"])
        message = B3["send"](paired, project)
        key = B3["scope_of"](message)
        with B3["Console"](self.home, "127.0.0.1", interrupt="model") as receiver:
            try:
                receiver.post("/v1/messages", message, paired["credential"])
            except (OSError, http.client.HTTPException):
                pass
            self.assertEqual(receiver.process.wait(timeout=10), 73)
        with B3["Console"](self.home, "127.0.0.1", spec_store=self.store, model_budget=RESERVE) as receiver:
            status, receipt = receiver.post("/v1/messages", message, paired["credential"])
            self.assertEqual((status, receipt["status"]), (200, "received"))
            self.assertEqual(receiver.post("/v1/messages", message, paired["credential"]), (200, receipt))
            fault = B3["pull"](receiver, key, paired)["events"][-1]
            self.assertEqual(fault["code"], "MODEL_USAGE_UNCERTAIN")
            ack = envelope(key, "dialogue.ack", through=fault["sequence"])
            self.assertEqual(B3["post"](receiver, "ack", ack, paired), B3["post"](receiver, "ack", ack, paired))
            ledger = receiver.command(f"model ledger {project}")
            row, = ledger["calls"]
            self.assertEqual((row["session_id"], row["status"], row["charged_nano_usd"]), (key[2], "uncertain", RESERVE))
            blocked = B3["send"](paired, project, sequence=2)
            receiver.post("/v1/messages", blocked, paired["credential"])
            code = A5["wait_for"](
                lambda: B3["pull"](receiver, B3["scope_of"](blocked), paired)["events"][-1].get("code"),
                "budget denial",
            )
            self.assertEqual(code, "MODEL_BUDGET_EXHAUSTED")
            reconcile = f"model reconcile {row['request_id']} 0 0 0 0"
            self.assertEqual(receiver.command(reconcile), receiver.command(reconcile))
            self.assertEqual(receiver.command(f"model reconcile {row['request_id']} 1 0 0 0"),
                             {"error": "MODEL_REQUEST_CONFLICT"})
            receiver.stop(paired["credential"], B3["BODY"])
        with B3["Console"](self.home, "127.0.0.1", spec_store=self.store, model_budget=RESERVE) as receiver:
            ledger = receiver.command(f"model ledger {project}")
            self.assertEqual(ledger["charged_nano_usd"], 0)
            self.assertEqual(len(ledger["calls"]), 1)
            self.assertEqual(B3["pull"](receiver, key, paired)["events"][-1], fault)
            retry = B3["send"](paired, project, sequence=3)
            receiver.post("/v1/messages", retry, paired["credential"])
            B3["wait_call"](self.home, retry["messageId"])
            self.assertEqual(len(receiver.command(f"model ledger {project}")["calls"]), 2)
            receiver.stop(paired["credential"], B3["BODY"])
        self.assertFalse(self.store.exists())
        self.assertFalse(B3["database"](self.home, "SELECT name FROM sqlite_master WHERE name = 'tasks'"))
        self.assertFalse(list(self.home.rglob("*.zip")))


def android_acceptance(serial):
    sdk = Path(os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or
               Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk")
    gradle = ROOT / "android-app"
    app = A5["APP"]
    phone = A5["Android"](sdk / "platform-tools/adb.exe", serial)
    assert phone.shell("getprop", "sys.boot_completed").strip() == "1", "Start and unlock the emulator"
    assert not phone.shell("pm", "list", "packages", app).strip(), "Acceptance app is already installed"
    subprocess.run([str(gradle / "gradlew.bat"), ":app:assembleDebug", "-PstageA5Acceptance=true"], cwd=gradle, check=True)
    phone.adb("install", str(gradle / "app/build/outputs/apk/debug/app-debug.apk"))
    title = "B7 budget acceptance"
    pairing_codes = []
    try:
        with TemporaryDirectory(prefix="stage-b7-android-") as temporary:
            home = Path(temporary)
            store = home / "projects"
            phone.launch()
            with B3["Console"](home, "127.0.0.1", spec_store=store, model_budget=RESERVE) as receiver:
                port = receiver.address[1]
                A5["pair"](phone, receiver, pairing_codes, address=f"https://10.0.2.2:{port}")
                phone.open_project("新建项目")
                phone.fill("项目名称", title)
                phone.tap("创建")
                phone.fill("输入插件需求", MOCK_NOTES_REQUEST)
                phone.tap("发送")
                phone.expect("电脑已接收")
                first = phone.outbox()[MOCK_NOTES_REQUEST]
                B3["wait_call"](home, first["id"])
                phone.tap("刷新规格会话")
                phone.expect("规格等待审阅")
                phone.tap("取消本轮")
                phone.expect("规格会话已取消")
                receiver.stop(MOCK_NOTES_REQUEST, *pairing_codes)

            with B3["Console"](home, "127.0.0.1", port, spec_store=store, model_budget=RESERVE) as receiver:
                with phone.offline():
                    phone.fill("输入插件需求", BODY)
                    phone.tap("发送")
                    phone.expect("本机排队", "手机当前离线，联网后可重试。")
                    pending = phone.outbox()[BODY]
                phone.tap(A5["RETRY"])
                A5["wait_for"](
                    lambda: B3["database"](home, "SELECT state FROM dialogue_sessions WHERE session_id = ?", (pending["id"],))
                    == [("failed",)], "budget denial for the queued message",
                )
                phone.tap("刷新规格会话")
                phone.expect("规格会话处理失败", BUDGET_MESSAGE)
                before = phone.outbox()[BODY]
                assert before["id"] == pending["id"]
                phone.launch()
                phone.open_project(title)
                phone.expect(BUDGET_MESSAGE)
                phone.tap("刷新规格会话")
                assert phone.outbox()[BODY] == before
                ledger = receiver.command(f"model ledger {first['project_id']}")
                assert ledger["charged_nano_usd"] == MOCK_CHARGE and len(ledger["calls"]) == 1
                receiver.stop(MOCK_NOTES_REQUEST, BODY, *pairing_codes)
                assert not store.exists() and not list(home.rglob("*.zip"))
    finally:
        phone.adb("uninstall", app)
    print("PASS Android offline queue, same-ID retry, durable budget denial and cached failure after restart")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--android", action="store_true")
    parser.add_argument("--serial", default=os.environ.get("ANDROID_SERIAL", "emulator-5554"))
    args, remaining = parser.parse_known_args()
    if args.android:
        if not args.serial.startswith("emulator-"):
            parser.error("--android requires an emulator serial")
        android_acceptance(args.serial)
    else:
        unittest.main(argv=[__file__, *remaining], verbosity=2)
