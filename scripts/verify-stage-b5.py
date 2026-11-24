"""Verify phone clarification, revision, idempotency, and cancellation handling."""

import argparse
import hashlib
import json
import os
import sqlite3
import threading
import subprocess
import runpy
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from tempfile import TemporaryDirectory
from uuid import uuid4

from dialogue_coordinator import DialogueCoordinator, envelope, timestamp
from dialogue_protocol import parse
from message_protocol import ProtocolError
from model_boundary import ModelBoundary, ProviderReply, Usage
from receiver_inbox import Inbox
from spec_workflow import SPEC_DIRECTORY, SPEC_METADATA, load_json, spec_json


PROJECT = "fixture-notes"
ROOT = Path(__file__).resolve().parents[1]
SAMPLE = load_json(SPEC_DIRECTORY / "examples" / "quick-notes-v1.json")
COMPLETE = {key: value for key, value in SAMPLE.items() if key not in SPEC_METADATA}
INCOMPLETE = {
    **COMPLETE, "goal": "", "storesData": None, "pages": [], "collections": [],
    "workflows": [], "acceptanceCriteria": [],
}


class FixtureProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def invoke(self, request, stop):
        self.requests.append(request)
        return ProviderReply(self.responses.pop(0), Usage(40, 30))


class StageB5Tests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory(prefix="stage-b5-")
        self.addCleanup(scratch.cleanup)
        self.path = Path(scratch.name) / "inbox.sqlite3"
        self.store = Path(scratch.name) / "projects"

    def make_coordinator(self, responses):
        database = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(database.close)
        inbox = Inbox(database)
        provider = FixtureProvider([spec_json(item) for item in responses])
        coordinator = DialogueCoordinator(inbox, provider, spec_store=self.store)
        key = ("fixture-phone", PROJECT, str(uuid4()))
        body = "Save short notes locally with a list and editor."
        message = {
            "deviceId": key[0], "projectId": key[1], "messageId": key[2], "createdAt": timestamp(),
            "body": body, "bodySha256": hashlib.sha256(body.encode()).hexdigest(), "sequence": 1,
            "messageType": "user.message",
        }
        row, _ = inbox.save(message, timestamp(), datetime.now(timezone.utc) - timedelta(days=1))
        coordinator.ingest(row)
        self.addCleanup(coordinator.stop)
        return database, coordinator, provider, key

    def run_call(self, coordinator):
        job = coordinator._claim()
        self.assertIsNotNone(job)
        coordinator._call(*job)

    def events(self, coordinator, key):
        return parse(json.dumps(coordinator.pull(envelope(key, "dialogue.pull", after=0, limit=20))))["events"]

    def phone_event(self, key, head, kind, sequence, text=None):
        event = envelope(key, kind, eventId=str(uuid4()), sequence=sequence, createdAt=timestamp(),
                         messageId=key[2], inReplyTo=head["eventId"])
        if kind != "phone.cancel":
            event.update(specVersion=head["specVersion"], specSha256=head["specSha256"], text=text)
        return event

    def test_two_clarifications_are_bound_and_idempotent(self):
        database, coordinator, provider, key = self.make_coordinator(
            [INCOMPLETE, {**INCOMPLETE, "goal": "Keep notes after restart"}, COMPLETE, COMPLETE]
        )
        self.run_call(coordinator)
        first = self.events(coordinator, key)[-1]
        first_answer = self.phone_event(key, first, "phone.answer", 1, "Keep notes after restart")
        self.assertFalse(coordinator.submit(first_answer)["duplicate"])
        self.assertTrue(coordinator.submit(first_answer)["duplicate"])
        self.run_call(coordinator)
        second_events = self.events(coordinator, key)
        second = second_events[-1]
        self.assertEqual(second["type"], "desktop.question")
        self.assertNotEqual(second["specSha256"], first["specSha256"])
        self.assertEqual(database.execute("SELECT COUNT(*) FROM dialogue_model_calls").fetchone()[0], 2)
        second_answer = self.phone_event(key, second, "phone.answer", 2, "Use a title and note body")
        coordinator.submit(second_answer)
        self.run_call(coordinator)
        final = self.events(coordinator, key)[-1]
        self.assertEqual(final["type"], "desktop.review")
        self.assertEqual(len(provider.requests), 3)
        revised_input = json.loads(provider.requests[1].input_text)
        self.assertEqual(revised_input["userInput"]["inReplyTo"], first["eventId"])
        self.assertEqual(revised_input["userInput"]["specSha256"], first["specSha256"])
        self.assertEqual(revised_input["previousDraft"]["version"], first["specVersion"])
        self.assertEqual(json.loads(provider.requests[2].input_text)["userInput"]["inReplyTo"], second["eventId"])
        self.assertEqual(coordinator._session(key)["state"], "result_ready")
        revision = self.phone_event(key, final, "phone.revise", 3, "Add a categories page")
        coordinator.submit(revision)
        self.run_call(coordinator)
        revised = self.events(coordinator, key)[-1]
        self.assertEqual(revised["type"], "desktop.review")
        self.assertEqual(len(provider.requests), 4)
        revision_input = json.loads(provider.requests[3].input_text)
        self.assertEqual(revision_input["userInput"]["type"], "phone.revise")
        self.assertEqual(revision_input["userInput"]["inReplyTo"], final["eventId"])
        self.assertEqual(revision_input["userInput"]["specSha256"], final["specSha256"])

    def test_stale_answer_and_cancel_do_not_start_a_call(self):
        _, coordinator, provider, key = self.make_coordinator([INCOMPLETE, COMPLETE])
        self.run_call(coordinator)
        question = self.events(coordinator, key)[-1]
        stale = self.phone_event(key, question, "phone.answer", 1, "Old context")
        stale["inReplyTo"] = str(uuid4())
        with self.assertRaises(ProtocolError) as failure:
            coordinator.submit(stale)
        self.assertEqual(failure.exception.code, "STALE_EVENT")
        cancel = self.phone_event(key, question, "phone.cancel", 1)
        coordinator.submit(cancel)
        self.assertIsNone(coordinator._claim())
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(coordinator._session(key)["state"], "cancelled")

    def test_expired_question_is_rejected(self):
        _, coordinator, _, key = self.make_coordinator([INCOMPLETE])
        self.run_call(coordinator)
        question = self.events(coordinator, key)[-1]
        expired = self.phone_event(key, question, "phone.answer", 1, "Too late")
        expired["createdAt"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat().replace("+00:00", "Z")
        with self.assertRaises(ProtocolError) as failure:
            coordinator.submit(expired)
        self.assertEqual(failure.exception.code, "EXPIRED_EVENT")

    def test_cancel_stops_an_active_model_call(self):
        database, coordinator, _, key = self.make_coordinator([COMPLETE])
        started = threading.Event()
        stopped = threading.Event()

        class BlockingProvider:
            def invoke(self, request, stop):
                started.set()
                stop.wait(5)
                stopped.set()
                return ProviderReply(spec_json(COMPLETE), Usage(40, 30))

        coordinator.model = ModelBoundary(coordinator.config, BlockingProvider())
        job = coordinator._claim()
        worker = threading.Thread(target=coordinator._call, args=job)
        worker.start()
        self.assertTrue(started.wait(2))
        head = self.events(coordinator, key)[-1]
        cancel = self.phone_event(key, head, "phone.cancel", 1)
        coordinator.submit(cancel)
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertTrue(stopped.is_set())
        self.assertEqual(coordinator._session(key)["state"], "cancelled")
        self.assertEqual(coordinator._session(key)["desktop_sequence"], 3)
        self.assertEqual(database.execute("SELECT COUNT(*) FROM dialogue_model_calls").fetchone()[0], 1)
        self.assertIsNone(coordinator._claim())


def android_acceptance(serial):
    b3 = runpy.run_path(str(ROOT / "scripts/verify-stage-b3.py"))
    a5 = b3["A5"]
    sdk = Path(os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or
               Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk")
    gradle = ROOT / "android-app"
    phone = a5["Android"](sdk / "platform-tools/adb.exe", serial)
    app = a5["APP"]
    directory = TemporaryDirectory(prefix="stage-b5-android-")
    home = Path(directory.name)
    assert phone.shell("getprop", "sys.boot_completed").strip() == "1", "Start and unlock the emulator first"
    assert not phone.shell("pm", "list", "packages", app).strip(), "Acceptance app is already installed"
    subprocess.run([str(gradle / "gradlew.bat"), ":app:assembleDebug", "-PstageA5Acceptance=true"],
                   cwd=gradle, check=True)
    phone.adb("install", str(gradle / "app/build/outputs/apk/debug/app-debug.apk"))
    reverse = None
    try:
        phone.launch()
        phone.tap("随手记")
        phone.tap("发送")
        phone.expect("电脑配对")
        phone.tap("返回会话")
        with b3["Console"](home, "127.0.0.1") as receiver:
            port = receiver.address[1]
            reverse = f"tcp:{port}"
            phone.adb("reverse", "--no-rebind", reverse, reverse)
            a5["pair"](phone, receiver, [], address=f"https://127.0.0.1:{port}")
            phone.open_project("新建项目")
            phone.fill("项目名称", "B5 acceptance project")
            phone.tap("创建")
            phone.fill("输入插件需求", "Fictional B5 request for weekly notes.")
            phone.tap("发送")
            phone.expect("电脑已接收")
            message = phone.outbox()["Fictional B5 request for weekly notes."]
            b3["wait_call"](home, message["id"])
            phone.tap("刷新规格会话")
            try:
                phone.expect("规格会话等待补充")
            except AssertionError:
                print("B.5 first refresh screen:", [node.get("text") for node in phone.nodes() if node.get("text")])
                print("B.5 desktop state:", b3["database"](
                    home, "SELECT state, desktop_sequence FROM dialogue_sessions WHERE session_id = ?",
                    (message["id"],),
                ))
                raise
            first_question = b3["database"](
                home,
                "SELECT payload FROM dialogue_events WHERE direction = 'desktop' AND session_id = ? "
                "AND json_extract(payload, '$.type') = 'desktop.question' ORDER BY sequence LIMIT 1",
                (message["id"],),
            )[0][0]
            first = json.loads(first_question)
            phone.fill("补充信息", "Keep weekly notes after restart")
            phone.tap("提交回答")
            b3["wait_call"](home, message["id"])
            phone.expect("规格会话等待补充")
            second_question = b3["database"](
                home,
                "SELECT payload FROM dialogue_events WHERE direction = 'desktop' AND session_id = ? "
                "AND json_extract(payload, '$.type') = 'desktop.question' ORDER BY sequence DESC LIMIT 1",
                (message["id"],),
            )[0][0]
            second = json.loads(second_question)
            phone.fill("补充信息", "Store notes locally")
            phone.tap("提交回答")
            b3["wait_call"](home, message["id"])
            phone.expect("规格等待审阅", "Quick Notes")
            phone_events = [json.loads(row[0]) for row in b3["database"](
                home,
                "SELECT payload FROM dialogue_events WHERE direction = 'phone' AND session_id = ? ORDER BY sequence",
                (message["id"],),
            )]
            assert [item["type"] for item in phone_events] == ["phone.answer", "phone.answer"]
            assert phone_events[0]["inReplyTo"] == first["eventId"]
            assert phone_events[0]["specSha256"] == first["specSha256"]
            assert phone_events[1]["inReplyTo"] == second["eventId"]
            assert phone_events[1]["specSha256"] == second["specSha256"]
            assert b3["database"](
                home,
                "SELECT COUNT(*) FROM dialogue_model_calls WHERE session_id = ?", (message["id"],),
            ) == [(3,)]
            phone.fill("补充需求或要求修改", "Add a categories page")
            phone.tap("请求修订")
            b3["wait_call"](home, message["id"])
            phone.expect("规格等待审阅", "Quick Notes")
            assert b3["database"](
                home,
                "SELECT COUNT(*) FROM dialogue_model_calls WHERE session_id = ?", (message["id"],),
            ) == [(4,)]
        print("PASS Android two-round clarification, bound answers and review revision")
    finally:
        if reverse:
            phone.adb("reverse", "--remove", reverse)
        phone.adb("uninstall", app)
        directory.cleanup()


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
