"""Verify B.3 with a pinned Windows receiver and optionally the Android simulator."""

import argparse
import hashlib
import http.client
import json
import os
import re
import runpy
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from desktop_receiver import envelope as message_envelope
from dialogue_coordinator import DialogueCoordinator, encode, envelope, timestamp
from dialogue_draft import MOCK_UNSUPPORTED_REQUEST
from dialogue_protocol import parse
from model_boundary import MockProvider
from receiver_inbox import Inbox


ROOT = Path(__file__).resolve().parents[1]
A2 = runpy.run_path(str(ROOT / "scripts/verify-stage-a2.py"))
A5 = runpy.run_path(str(ROOT / "scripts/verify-stage-a5.py"))
Console = A2["Console"]
BODY = "Fictional B3 request for weekly notes."
PROJECT = "fixture-notes"
OTHER_PROJECT = "fixture-calendar"


def pair(receiver, device):
    code = receiver.command("pair")["pairingCode"]
    return A2["pairing_decision"](receiver, A2["pairing_request"](code, device_id=device), "approve")


def send(credential, project, sequence=1, *, message_type="user.message"):
    return message_envelope(
        "message.send", deviceId=credential["deviceId"], credentialId=credential["credentialId"],
        messageId=str(uuid4()), projectId=project, sequence=sequence, messageType=message_type,
        body=BODY, bodySha256=hashlib.sha256(BODY.encode()).hexdigest(),
    )


def scope_of(message):
    return message["deviceId"], message["projectId"], message["messageId"]


def post(receiver, action, payload, credential=None, *, raw=None, size=None, expected=200):
    with closing(http.client.HTTPConnection(*receiver.address, timeout=10)) as connection:
        connection.sock = receiver.tls()
        headers = {"Content-Type": "application/json", "Connection": "close"}
        if credential:
            headers["Authorization"] = f"Bearer {credential['credential']}"
            headers["X-Credential-Id"] = credential["credentialId"]
        if size is not None:
            headers["Content-Length"] = str(size)
        body = encode(payload).encode() if raw is None else raw
        connection.request("POST", f"/v1/dialogue/{action}", body=body, headers=headers)
        with connection.getresponse() as reply:
            value = parse(reply.read())
            assert reply.status == expected, (reply.status, value)
            assert reply.getheader("Cache-Control") == "no-store"
            return value


def pull(receiver, key, credential, after=0, limit=20, *, expected=200):
    return post(receiver, "pull", envelope(key, "dialogue.pull", after=after, limit=limit),
                credential, expected=expected)


def database(home, query, params=()):
    with closing(sqlite3.connect(home / "AndroidPluginAgentReceiver/inbox.sqlite3")) as db:
        return db.execute(query, params).fetchall()


def completed(home, message_id):
    query = "SELECT status, charged_nano_usd, exact_charge FROM dialogue_model_calls WHERE session_id = ?"
    calls = database(home, query, (message_id,))
    return bool(calls) and all(call == ("completed", 10_800, 1) for call in calls)


def wait_call(home, message_id):
    A5["wait_for"](lambda: completed(home, message_id), "persisted mock call")


def cancelled(key, head, sequence=1):
    return envelope(key, "phone.cancel", sequence=sequence, eventId=str(uuid4()), createdAt=timestamp(),
                    messageId=key[2], inReplyTo=head["eventId"])


def byte_limited_pages():
    with closing(sqlite3.connect(":memory:")) as db:
        inbox = Inbox(db)
        coordinator = DialogueCoordinator(inbox)
        message = send({"deviceId": "pagination-phone", "credentialId": str(uuid4())}, PROJECT)
        received, _ = inbox.save(message, timestamp(), datetime.now(timezone.utc) - timedelta(days=1))
        coordinator.ingest(received)
        key, _ = coordinator._claim()
        check_page_sizes(coordinator, key)
    print("PASS wire pagination respects the byte limit")


def check_page_sizes(coordinator, key):
    # Inject wire-only fixtures into an isolated running session to reach the transport byte limit.
    spec_json = encode({"schemaVersion": 1, "status": "draft", "projectId": key[1], "version": 1,
                        "title": "Fictional B3 draft", "goal": "记" * 15_000})
    fields = {"specVersion": 1, "specSha256": hashlib.sha256(spec_json.encode()).hexdigest()}
    with coordinator.transaction():
        for _ in range(3):
            coordinator._publish(key, "desktop.draft", specJson=spec_json, **fields)
    page = coordinator.pull(envelope(key, "dialogue.pull", after=1, limit=20))
    assert page["hasMore"] and page["next"] == 4
    final = coordinator.pull(envelope(key, "dialogue.pull", after=4, limit=20))
    assert final["next"] == 5 and not final["hasMore"]


def desktop(home):
    with Console(home, "127.0.0.1") as receiver:
        phone = pair(receiver, "fixture-phone")
        other = pair(receiver, "fixture-other-phone")
        first = send(phone, PROJECT)
        same_project = send(phone, PROJECT, 2)
        isolated = send(phone, OTHER_PROJECT)
        same_id_other_device = {**send(other, PROJECT), "messageId": first["messageId"]}
        legacy = send(phone, PROJECT, 3, message_type="user.confirmation")
        legacy.update(confirmation={"specVersion": 1, "decision": "confirmed", "confirmedAt": timestamp()})
        key = scope_of(first)
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda _: receiver.post("/v1/messages", first, phone["credential"]), range(4)))
        receipt = A2["expect_receipt"](replies[0], first)
        assert all(reply == (200, receipt) for reply in replies)
        for msg, auth in ((same_project, phone), (isolated, phone), (same_id_other_device, other), (legacy, phone)):
            A2["expect_receipt"](receiver.post("/v1/messages", msg, auth["credential"]), msg)
        wait_call(home, first["messageId"])
        wait_call(home, isolated["messageId"])
        assert database(home, "SELECT COUNT(*) FROM dialogue_model_calls")[0][0] == 3
        pending = pull(receiver, scope_of(same_project), phone)
        assert [event["state"] for event in pending["events"]] == ["queued"]
        assert pull(receiver, scope_of(legacy), phone, expected=404)["code"] == "SESSION_NOT_FOUND"
        assert pull(receiver, key, None, expected=401)["code"] == "AUTH_REQUIRED"
        assert pull(receiver, key, other, expected=401)["code"] == "AUTH_REQUIRED"
        assert pull(receiver, (other["deviceId"], OTHER_PROJECT, isolated["messageId"]), other,
                    expected=404)["code"] == "SESSION_NOT_FOUND"
        assert pull(receiver, (key[0], OTHER_PROJECT, key[2]), phone, expected=404)["code"] == "SESSION_NOT_FOUND"
        malformed = post(receiver, "pull", None, phone, raw=b'{"type":1,"type":2}', expected=400)
        assert malformed["code"] == "INVALID_REQUEST"
        oversized = post(receiver, "pull", None, phone, raw=b"", size=128 * 1024 + 1, expected=413)
        assert oversized["code"] == "PAYLOAD_TOO_LARGE"
        first_page = pull(receiver, key, phone, limit=1)
        assert first_page == pull(receiver, key, phone, limit=1) and first_page["hasMore"]
        assert first_page["events"][0]["state"] == "queued" and first_page["next"] == 1
        ack = envelope(key, "dialogue.ack", through=1)
        assert post(receiver, "ack", ack, phone) == ack
        assert pull(receiver, key, phone, after=5, expected=409)["code"] == "INVALID_CURSOR"
        assert post(receiver, "ack", {**ack, "through": 5}, phone, expected=409)["code"] == "INVALID_CURSOR"
        question = pull(receiver, key, phone, after=3)["events"][0]
        spec = {name: question[name] for name in ("specVersion", "specSha256")}
        receiver.stop(phone["credential"], other["credential"], BODY)
    print("PASS inbox idempotency, one active round per scope, legacy receipt, and device/project isolation")

    with Console(home, "127.0.0.1") as receiver:
        assert receiver.post("/v1/messages", first, phone["credential"]) == (200, receipt)
        assert pull(receiver, key, phone, limit=1) == first_page
        second_page = pull(receiver, key, phone, after=1, limit=2)
        assert second_page["hasMore"] and second_page["next"] == 3
        final_page = pull(receiver, key, phone, after=3)
        assert final_page["events"][-1] == question and not final_page["hasMore"]
        end = final_page["next"]
        assert post(receiver, "ack", {**ack, "through": end}, phone)["through"] == end
        assert post(receiver, "ack", ack, phone)["through"] == end
        cancel = cancelled(key, question)
        invalid = post(receiver, "submit", {**cancel, "sequence": 2}, phone, expected=409)
        assert invalid["code"] == "EVENT_SEQUENCE_CONFLICT"
        stale = post(receiver, "submit", {**cancel, "inReplyTo": str(uuid4())}, phone, expected=409)
        assert stale["code"] == "STALE_EVENT"
        answer = {**cancel, "type": "phone.answer", "text": "Synthetic answer", **spec}
        assert post(receiver, "submit", answer, phone, expected=409)["code"] == "INVALID_STATE"
        assert not post(receiver, "submit", cancel, phone)["duplicate"]
        assert post(receiver, "submit", cancel, phone)["duplicate"]
        conflict = post(receiver, "submit", {**cancel, "sequence": 2}, phone, expected=409)
        assert conflict["code"] == "EVENT_ID_CONFLICT"
        wait_call(home, same_project["messageId"])
        receiver.stop(phone["credential"], BODY)
    with Console(home, "127.0.0.1") as receiver:
        assert post(receiver, "submit", cancel, phone)["duplicate"]
        assert post(receiver, "ack", ack, phone)["through"] == end
        page = pull(receiver, key, phone, after=end)
        assert [event["state"] for event in page["events"]] == ["cancelled"]
        assert database(home, "SELECT COUNT(*) FROM dialogue_model_calls")[0][0] == 4
        receiver.command(f"revoke {phone['deviceId']}")
        assert pull(receiver, key, phone, expected=403)["code"] == "DEVICE_REVOKED"
        receiver.stop(phone["credential"], BODY)
    print("PASS ordered pages, monotonic acknowledgements, duplicate/conflicting submissions and restart")


def interrupted_claim(path):
    with closing(sqlite3.connect(path)) as db:
        inbox = Inbox(db)
        worker = DialogueCoordinator(inbox)
        phone = {"deviceId": "interrupted-phone", "credentialId": str(uuid4())}
        message = send(phone, PROJECT)
        received, _ = inbox.save(message, timestamp(), datetime.now(timezone.utc) - timedelta(days=1))
        worker.ingest(received)
        key, call = worker._claim()
        assert str(call.session_id) == message["messageId"]
    with closing(sqlite3.connect(path, check_same_thread=False)) as db:
        class NoReplay(MockProvider):
            def invoke(self, *_):
                raise AssertionError("Unexpected model invocation")

        worker = DialogueCoordinator(Inbox(db), NoReplay())
        assert worker._claim() is None
        events = worker.pull(envelope(key, "dialogue.pull", after=0, limit=20))["events"]
        assert [item.get("state") or item.get("code") for item in events] == [
            "queued", "running", "MODEL_USAGE_UNCERTAIN",
        ]
        assert db.execute("""
            SELECT status, reserved_nano_usd = charged_nano_usd FROM dialogue_model_calls
        """).fetchone()[:] == ("uncertain", 1)
    print("PASS interrupted model claim recovered without replay")


def android(home, serial):
    assert re.fullmatch(r"emulator-\d+", serial), "Specify an emulator serial"
    sdk = Path(os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or
               Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk")
    app = A5["APP"]
    phone = A5["Android"](sdk / "platform-tools/adb.exe", serial)
    assert phone.shell("getprop", "sys.boot_completed").strip() == "1"
    assert not phone.shell("pm", "list", "packages", app).strip(), "Acceptance app is already installed"
    gradle = ROOT / "android-app"
    subprocess.run([str(gradle / "gradlew.bat"), ":app:assembleDebug", "-PstageA5Acceptance=true"],
                   cwd=gradle, check=True)
    phone.adb("install", str(gradle / "app/build/outputs/apk/debug/app-debug.apk"))
    reverse_port = None
    try:
        phone.launch()
        phone.tap("随手记")
        draft = "我想做一个随手记插件，可以新增、编辑和删除笔记，把内容保存在本机。"
        phone.expect(draft)
        phone.tap("发送")
        phone.expect("电脑配对")
        phone.tap("返回会话")
        phone.expect(draft)
        with Console(home, "127.0.0.1") as receiver:
            port = receiver.address[1]
            endpoint = f"tcp:{port}"
            phone.adb("reverse", "--no-rebind", endpoint, endpoint)
            reverse_port = endpoint
            address = f"https://127.0.0.1:{port}"
            A5["pair"](phone, receiver, [], address=address)
            phone.open_project("新建项目")
            phone.fill("项目名称", "B3 acceptance project")
            phone.tap("创建")
            phone.fill("输入插件需求", BODY)
            phone.tap("发送")
            phone.expect("电脑已接收", "刷新规格会话")
            message = phone.outbox()[BODY]
            phone.tap("消息详情")
            phone.expect(f"回执 ID {message['receipt_id']}")
            phone.tap("收起详情")
            session_id = message["id"]
            wait_call(home, session_id)
            phone.tap("刷新规格会话")
            question_text, sequence = database(
                home, "SELECT payload, sequence FROM dialogue_events WHERE direction = 'desktop' ORDER BY sequence DESC LIMIT 1"
            )[0]
            question = json.loads(question_text)
            phone.expect("电脑已接收", "规格会话等待补充", question["question"])
            A5["wait_for"](lambda: database(home, "SELECT acked_sequence FROM dialogue_sessions") == [(sequence,)],
                           "first Android acknowledgement")
            receiver.stop(BODY)
        print("PASS Android receipt and deterministic clarification")
        with Console(home, "127.0.0.1", port=port) as receiver:
            phone.launch(connected=address)
            try:
                phone.open_project("B3 acceptance project")
                phone.tap("刷新规格会话")
                phone.expect("电脑已接收", "规格会话等待补充", question["question"])
            except AssertionError:
                labels = [node.get("text") for node in phone.nodes() if node.get("text")]
                print("B.3 fixture screen:", labels[:16])
                print("B.3 stored ack:", database(home, "SELECT acked_sequence FROM dialogue_sessions"))
                raise
            A5["wait_for"](
                lambda: database(home, "SELECT acked_sequence FROM dialogue_sessions") == [(question["sequence"],)],
                "Android acknowledgement after paginated catch-up",
            )
            assert database(home, "SELECT COUNT(*) FROM dialogue_model_calls") == [(1,)]
            assert phone.outbox()[BODY] == message
            phone.tap("返回会话")
            phone.fill("输入插件需求", MOCK_UNSUPPORTED_REQUEST)
            phone.tap("发送")
            phone.expect("电脑已接收", "刷新规格会话")
            review_message = phone.outbox()[MOCK_UNSUPPORTED_REQUEST]
            assert review_message["project_id"] != message["project_id"] and review_message["sequence"] == 1
            wait_call(home, review_message["id"])
            phone.tap("刷新规格会话")
            phone.expect("规格等待审阅", "Quick Notes", "不支持的请求 device.contacts",
                         "当前宿主未开放通讯录访问。", "替代方案 允许用户手动输入联系人名称。")
            receiver.stop(BODY, MOCK_UNSUPPORTED_REQUEST)
        print("PASS Android restart, acknowledgement and unsupported-capability review")
        phone.adb("reverse", "--remove", reverse_port)
        reverse_port = None
        phone.launch()
        failure_labels = {"无法连接电脑，请检查网络和接收服务。", "连接或等待电脑响应超时。",
                          "与电脑的连接中断，可重试原消息。"}
        A5["wait_for"](lambda: any(node.get("text") in failure_labels for node in phone.nodes()),
                       "completed offline connection check")
        phone.open_project("B3 acceptance project")
        phone.expect("电脑已接收", question["question"])
    finally:
        if reverse_port:
            phone.adb("reverse", "--remove", reverse_port)
        phone.adb("uninstall", app)
    print("PASS cached Android conversation after offline app reopen")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--android", action="store_true")
    parser.add_argument("--serial", default=os.environ.get("ANDROID_SERIAL", "emulator-5554"))
    args = parser.parse_args()
    with TemporaryDirectory(prefix="stage-b3-") as directory:
        home = Path(directory)
        byte_limited_pages()
        interrupted_claim(home / "interrupted.sqlite3")
        desktop(home)
    if args.android:
        with TemporaryDirectory(prefix="stage-b3-android-") as directory:
            android(Path(directory), args.serial)
    print("STAGE B.3 PASSED")


if __name__ == "__main__":
    main()
