"""Verify phone review, confirmation, immutable storage, and receipt recovery."""

import argparse
import hashlib
import json
import os
import re
import runpy
import sqlite3
import subprocess
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from dialogue_coordinator import DialogueCoordinator, encode, envelope, timestamp
from dialogue_draft import MOCK_NOTES_REQUEST, MOCK_UNSUPPORTED_REQUEST
from dialogue_protocol import parse
from message_protocol import ProtocolError
from receiver_inbox import Inbox
from spec_workflow import load_json, save_confirmed, spec_json


ROOT = Path(__file__).resolve().parents[1]


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def submit_event(key, review, kind="phone.confirm", sequence=1):
    return envelope(key, kind, eventId=str(uuid4()), sequence=sequence, createdAt=timestamp(),
                    messageId=key[2], inReplyTo=review["eventId"],
                    specVersion=review["specVersion"], specSha256=review["specSha256"])


def verify_saved(review, receipt, event, path):
    draft = json.loads(review["specJson"])
    assert digest(review["specJson"]) == review["specSha256"]
    saved = load_json(path)
    assert saved == dict(draft, status="confirmed")
    assert receipt["state"] == "confirmed"
    assert receipt["inReplyTo"] == event["eventId"]
    assert receipt["specVersion"] == event["specVersion"] == saved["version"]
    assert receipt["specSha256"] == event["specSha256"] == review["specSha256"]
    assert receipt["savedSpecSha256"] == digest(spec_json(saved))


class StageB6Tests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory(prefix="stage-b6-")
        self.addCleanup(scratch.cleanup)
        self.path = Path(scratch.name) / "inbox.sqlite3"
        self.store = Path(scratch.name) / "projects"
        self.key = ("fixture-phone", "fixture-notes", str(uuid4()))
        self.version = self.store / self.key[1] / "v1.json"
        with closing(sqlite3.connect(self.path)) as db:
            inbox = Inbox(db)
            coordinator = DialogueCoordinator(inbox, spec_store=self.store)
            device, project, session = self.key
            message = dict(deviceId=device, projectId=project, messageId=session, createdAt=timestamp(),
                           body=MOCK_UNSUPPORTED_REQUEST, bodySha256=digest(MOCK_UNSUPPORTED_REQUEST),
                           sequence=1, messageType="user.message")
            row, _ = inbox.save(message, timestamp(), datetime.now(timezone.utc) - timedelta(days=1))
            coordinator.ingest(row)
            coordinator._call(*coordinator._claim())
            self.review = self.head(coordinator)

    def open(self, db):
        return DialogueCoordinator(Inbox(db), spec_store=self.store)

    def head(self, coordinator):
        page = coordinator.pull(envelope(self.key, "dialogue.pull", after=0, limit=20))
        return parse(encode(page))["events"][-1]

    def assert_error(self, code, coordinator, event):
        with self.assertRaises(ProtocolError) as failure:
            coordinator.submit(event)
        self.assertEqual(failure.exception.code, code)

    def test_confirmation_and_duplicate_survive_restart(self):
        event = submit_event(self.key, self.review)
        with closing(sqlite3.connect(self.path)) as db:
            coordinator = self.open(db)
            self.assertEqual(self.head(coordinator), self.review)
            self.assertFalse(self.version.exists())
            self.assertFalse(coordinator.submit(event)["duplicate"])
            receipt = self.head(coordinator)
            verify_saved(self.review, receipt, event, self.version)
        with closing(sqlite3.connect(self.path)) as db:
            coordinator = self.open(db)
            self.assertTrue(coordinator.submit(event)["duplicate"])
            self.assertEqual(self.head(coordinator), receipt)
            self.assert_error("EVENT_ID_CONFLICT", coordinator, dict(event, specVersion=2))
            self.assertEqual(list(self.version.parent.iterdir()), [self.version])
            self.assertEqual(db.execute("SELECT count(*) FROM dialogue_model_calls").fetchone()[0], 1)
            self.assertIsNone(coordinator._claim())
            self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name = 'tasks'").fetchone())

    def test_recovers_on_both_sides_of_file_publication(self):
        event = submit_event(self.key, self.review)
        with closing(sqlite3.connect(self.path)) as db:
            coordinator = self.open(db)
            with patch("dialogue_coordinator.save_confirmed", side_effect=OSError("before file publication")):
                with self.assertRaises(OSError):
                    coordinator.submit(event)
            self.assertFalse(self.version.exists())
            self.assertEqual(coordinator._session(self.key)["state"], "result_ready")

        def interrupt_after_link(*args, **kwargs):
            save_confirmed(*args, **kwargs)
            raise OSError("after file publication")

        with closing(sqlite3.connect(self.path)) as db:
            with patch("dialogue_coordinator.save_confirmed", side_effect=interrupt_after_link):
                coordinator = self.open(db)
            self.assertTrue(self.version.is_file())
            original_bytes = self.version.read_bytes()
            self.assertEqual(coordinator._session(self.key)["state"], "result_ready")
            self.assertEqual(db.execute("SELECT count(*) FROM dialogue_events WHERE direction = 'phone'").fetchone()[0], 0)
        with closing(sqlite3.connect(self.path)) as db:
            coordinator = self.open(db)
            self.assertTrue(coordinator.submit(event)["duplicate"])
            verify_saved(self.review, self.head(coordinator), event, self.version)
            self.assertEqual(self.version.read_bytes(), original_bytes)
            self.assertEqual(list(self.version.parent.iterdir()), [self.version])

    def test_rejection_and_duplicate_leave_no_version(self):
        event = submit_event(self.key, self.review, "phone.reject")
        with closing(sqlite3.connect(self.path)) as db:
            coordinator = self.open(db)
            self.assertFalse(coordinator.submit(event)["duplicate"])
            receipt = self.head(coordinator)
            self.assertEqual(receipt["state"], "cancelled")
        with closing(sqlite3.connect(self.path)) as db:
            coordinator = self.open(db)
            self.assertTrue(coordinator.submit(event)["duplicate"])
            self.assertEqual(self.head(coordinator), receipt)
            self.assert_error("STALE_EVENT", coordinator, submit_event(self.key, self.review, sequence=2))
            self.assertFalse(self.store.exists())
            self.assertIsNone(coordinator._claim())

    def test_expired_review_is_refreshed_without_a_model_call(self):
        with closing(sqlite3.connect(self.path)) as db:
            coordinator = self.open(db)
            earlier = datetime.now(timezone.utc) - timedelta(hours=25)
            expired = dict(self.review, createdAt=earlier.isoformat().replace("+00:00", "Z"),
                           expiresAt=(earlier + timedelta(hours=24)).isoformat().replace("+00:00", "Z"))
            with db:
                db.execute("UPDATE dialogue_events SET payload = ? WHERE event_id = ?",
                           (encode(expired), expired["eventId"]))
            event = submit_event(self.key, expired)
            self.assert_error("EXPIRED_EVENT", coordinator, event)
            refreshed = self.head(coordinator)
            self.assertNotEqual(refreshed["eventId"], expired["eventId"])
            self.assertEqual(refreshed["specSha256"], expired["specSha256"])
            self.assert_error("STALE_EVENT", coordinator, event)
            self.assertFalse(self.version.exists())
            self.assertFalse(coordinator.submit(submit_event(self.key, refreshed))["duplicate"])
            self.assertEqual(db.execute("SELECT count(*) FROM dialogue_model_calls").fetchone()[0], 1)

    def test_conflicting_version_requires_fresh_consent_even_with_identical_content(self):
        old = save_confirmed(json.loads(self.review["specJson"]), self.store)
        original_bytes = old.read_bytes()
        event = submit_event(self.key, self.review)
        with closing(sqlite3.connect(self.path)) as db:
            coordinator = self.open(db)
            self.assert_error("SPEC_MISMATCH", coordinator, dict(event, specSha256="0" * 64))
            self.assert_error("SPEC_MISMATCH", coordinator, event)
            current = self.head(coordinator)
            self.assertEqual(current["specVersion"], 2)
            self.assertNotEqual(current["specSha256"], event["specSha256"])
            self.assertEqual(list(old.parent.iterdir()), [old])
            self.assert_error("STALE_EVENT", coordinator, event)
            fresh = submit_event(self.key, current)
            coordinator.submit(fresh)
            verify_saved(current, self.head(coordinator), fresh, old.with_name("v2.json"))
            self.assertEqual(old.read_bytes(), original_bytes)

    def test_confirmation_rechecks_persisted_draft(self):
        with closing(sqlite3.connect(self.path)) as db:
            coordinator = self.open(db)
            draft = dict(json.loads(self.review["specJson"]), unsupportedItems=[])
            payload = spec_json(draft)
            corrupted = dict(self.review, specJson=payload, specSha256=digest(payload))
            with db:
                db.execute("UPDATE dialogue_events SET payload = ? WHERE event_id = ?",
                           (encode(corrupted), corrupted["eventId"]))
            self.assert_error("SPEC_MISMATCH", coordinator, submit_event(self.key, corrupted))
            self.assertEqual(self.head(coordinator)["code"], "SPEC_MISMATCH")
            self.assertFalse(self.version.exists())


def verify_review(phone, review, decision, check_fields=False):
    phone.tap("审阅完整规格")
    nodes = phone.expect("审阅规格", "返回会话", f"规格版本 {review['specVersion']}")
    draft = json.loads(review["specJson"])
    expected = [draft["title"], draft["goal"], f"审阅摘要 SHA-256 {review['specSha256']}"]
    if check_fields:
        expected += ["页面与用途", "数据集合与字段", "保存数据", "操作流程", "权限及理由", "验收条件"]
        for page in draft["pages"]:
            expected.append(f"{page['name']} · {page['id']} {page['purpose']}")
        for collection in draft["collections"]:
            expected += [collection["id"]]
            for field in collection["fields"]:
                required = "必填" if field["required"] else "选填"
                expected.append(f"{field['id']} · {field['type']} · {required} · maxLength {field['maxLength']}")
        for flow in draft["workflows"]:
            expected.append(f"{flow['id']} · {flow['trigger']}")
            expected.extend(f"{index}. {step}" for index, step in enumerate(flow["steps"], 1))
        expected.extend(f"{item['capability']} · {item['justification']}" for item in draft["permissions"])
        for item in draft["unsupportedItems"]:
            expected.extend([f"不支持的请求 {item['request']}", item["reason"], f"替代方案 {item['alternative']}"])
        expected.extend(f"{item['id']} · {item['scenario']} {item['expected']}" for item in draft["acceptanceCriteria"])
    scrolling = next(node for node in nodes if node.get("scrollable") == "true")
    x1, top, x2, bottom = map(int, re.findall(r"\d+", scrolling.get("bounds")))
    x = str((x1 + x2) // 2)
    observed = set()
    for _ in range(10):
        observed.update(" ".join(node.get("text", "").split()) for node in nodes)
        if decision in observed:
            break
        phone.shell("input", "swipe", x, str(bottom - 60), x, str(top + 60), "400")
        nodes = phone.nodes()
    assert set(expected) <= observed, f"Missing review fields: {set(expected) - observed}"
    phone.tap(decision)

def android_acceptance(serial):
    b3 = runpy.run_path(str(ROOT / "scripts/verify-stage-b3.py"))
    a5 = b3["A5"]
    sdk = Path(os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or
               Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk")
    gradle = ROOT / "android-app"
    phone = a5["Android"](sdk / "platform-tools/adb.exe", serial)
    app = a5["APP"]
    assert phone.shell("getprop", "sys.boot_completed").strip() == "1", "Start and unlock the emulator first"
    assert not phone.shell("pm", "list", "packages", app).strip(), "Acceptance app is already installed"
    subprocess.run([str(gradle / "gradlew.bat"), ":app:assembleDebug", "-PstageA5Acceptance=true"], cwd=gradle, check=True)
    phone.adb("install", str(gradle / "app/build/outputs/apk/debug/app-debug.apk"))

    def latest(home, session, direction="desktop"):
        return json.loads(b3["database"](
            home, "SELECT payload FROM dialogue_events WHERE session_id = ? AND direction = ? ORDER BY sequence DESC LIMIT 1",
            (session, direction),
        )[0][0])

    def start_review(home, title, body):
        phone.open_project("新建项目")
        phone.fill("项目名称", title)
        phone.tap("创建")
        phone.fill("输入插件需求", body)
        phone.tap("发送")
        phone.expect("电脑已接收")
        message = phone.outbox()[body]
        b3["wait_call"](home, message["id"])
        phone.tap("刷新规格会话")
        phone.expect("规格等待审阅", "审阅完整规格")
        return message

    reverse = None
    try:
        with TemporaryDirectory(prefix="stage-b6-android-") as scratch:
            home = Path(scratch)
            store = home / "projects"
            phone.launch()
            with b3["Console"](home, "127.0.0.1", spec_store=store) as receiver:
                port = receiver.address[1]
                reverse = f"tcp:{port}"
                phone.adb("reverse", "--no-rebind", reverse, reverse)
                a5["pair"](phone, receiver, [], address=f"https://127.0.0.1:{port}")
                accepted = start_review(home, "B6 acceptance project", MOCK_UNSUPPORTED_REQUEST)
                review = latest(home, accepted["id"])
                assert review["type"] == "desktop.review"
                assert not store.exists()

            phone.launch()
            phone.open_project("B6 acceptance project")
            phone.expect("规格等待审阅", "审阅完整规格")
            with b3["Console"](home, "127.0.0.1", port, spec_store=store) as receiver:
                verify_review(phone, review, "确认此规格", check_fields=True)
                phone.expect("规格已确认", "规格版本 1")
                receipt = latest(home, accepted["id"])
                event = latest(home, accepted["id"], "phone")
                path = store / accepted["project_id"] / "v1.json"
                verify_saved(review, receipt, event, path)
                assert event["type"] == "phone.confirm"
                phone.expect(f"保存摘要 SHA-256 {receipt['savedSpecSha256']}")
                print("PASS Android review fields, confirmation binding, and immutable version")

            with b3["Console"](home, "127.0.0.1", port, spec_store=store) as receiver:
                phone.launch()
                phone.open_project("B6 acceptance project")
                phone.expect("规格已确认", f"保存摘要 SHA-256 {receipt['savedSpecSha256']}")
                phone.tap("刷新规格会话")
                assert latest(home, accepted["id"]) == receipt
                assert list(path.parent.iterdir()) == [path]
                phone.tap("返回会话")
                rejected = start_review(home, "B6 rejection project", MOCK_NOTES_REQUEST)
                initial = latest(home, rejected["id"])
                phone.fill("补充需求或要求修改", "Improve the note editor")
                phone.tap("请求修订")
                a5["wait_for"](lambda: latest(home, rejected["id"])["sequence"] > initial["sequence"]
                               and latest(home, rejected["id"])["type"] == "desktop.review", "revised review")
                phone.tap("刷新规格会话")
                verify_review(phone, latest(home, rejected["id"]), "拒绝此规格")
                phone.expect("规格已拒绝")
                phone.launch()
                phone.open_project("B6 rejection project")
                phone.expect("规格已拒绝")
                assert latest(home, rejected["id"], "phone")["type"] == "phone.reject"
                assert not (store / rejected["project_id"]).exists()
                assert not b3["database"](home, "SELECT name FROM sqlite_master WHERE name = 'tasks'")
                assert not list(home.rglob("*.zip"))
        print("PASS Android full review, saved fields and digests, revision, rejection, and restarts")
    finally:
        if reverse:
            phone.adb("reverse", "--remove", reverse)
        phone.adb("uninstall", app)


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
