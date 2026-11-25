"""Run Stage B desktop regressions, Android acceptance, or one bounded real call."""

import argparse
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from dialogue_draft import DRAFT_PROMPT_VERSION, MOCK_UNSUPPORTED_REQUEST
from model_boundary import MAX_OUTPUT_TOKENS, RATES, RELAY_MODEL, Usage
from model_credentials import read_key
from relay_model import RELAY_TARGET


ROOT = Path(__file__).resolve().parents[1]
B3 = runpy.run_path(str(ROOT / "scripts/verify-stage-b3.py"))
B6 = runpy.run_path(str(ROOT / "scripts/verify-stage-b6.py"))
A5 = B3["A5"]
Console = B3["Console"]
RATE = RATES["kakou"]
REAL_BUDGET = RATE.ceiling(MAX_OUTPUT_TOKENS)
REAL_BODY = (
    "Fictional acceptance fixture: create a read-only greeting plugin with one page displaying "
    "Hello. Workflow: open the plugin, view the greeting on its only page. "
    "No saved data or permissions needed. Acceptance: opening the plugin shows Hello."
)


def desktop_regression(check_schema):
    for number in range(1, 8):
        command = [sys.executable, str(ROOT / f"scripts/verify-stage-b{number}.py")]
        if number == 1 and check_schema:
            command.append("--schema")
        subprocess.run(command, cwd=ROOT, check=True)
    print("PASS B.1-B.7 desktop regression")


def events(home, message, direction):
    rows = B3["database"](
        home,
        "SELECT payload FROM dialogue_events WHERE project_id = ? AND session_id = ? "
        "AND direction = ? ORDER BY sequence",
        (message["project_id"], message["id"], direction),
    )
    return [json.loads(row[0]) for row in rows]


def latest(home, message, *event_types, after=0):
    def find():
        history = events(home, message, "desktop")
        if history and history[-1]["sequence"] > after:
            event = history[-1]
            assert event["type"] != "desktop.error", event.get("code")
            if event["type"] in event_types:
                return event
        return None

    return A5["wait_for"](find, " / ".join(event_types), timeout=45)


def downstream_files(directory):
    return {path.relative_to(directory): path.stat().st_mtime_ns for path in directory.rglob("*")
            if path.name in {"tasks.sqlite3", "tasks.sqlite3-wal", "delivery.json"} or path.suffix == ".zip"}


def no_downstream_state(home):
    tables = B3["database"](home, "SELECT name FROM sqlite_master WHERE type = 'table'")
    assert not any("task" in name or "delivery" in name for name, in tables)
    assert not downstream_files(home)


def send_from_phone(phone, title, body):
    phone.open_project("新建项目")
    phone.fill("项目名称", title)
    phone.tap("创建")
    phone.fill("输入插件需求", body)
    phone.tap("发送")
    phone.expect("电脑已接收")
    message = phone.outbox()[body]
    phone.tap("消息详情")
    phone.expect(f"项目 ID {message['project_id']}", f"消息 ID {message['id']}",
                 f"回执 ID {message['receipt_id']}")
    phone.tap("收起详情")
    return message


def confirm_on_phone(phone, home, store, message, review):
    project = message["project_id"]
    assert review["projectId"] == json.loads(review["specJson"])["projectId"] == project
    assert review["sessionId"] == message["id"]
    assert not (store / project).exists()
    B6["verify_review"](phone, review, "确认此规格", check_fields=True)
    receipt = latest(home, message, "desktop.status", after=review["sequence"])
    event = events(home, message, "phone")[-1]
    assert event["type"] == "phone.confirm"
    assert event["inReplyTo"] == review["eventId"]
    assert event["projectId"] == receipt["projectId"] == project
    assert event["sessionId"] == receipt["sessionId"] == message["id"]
    path = store / project / f"v{review['specVersion']}.json"
    B6["verify_saved"](review, receipt, event, path)
    phone.expect("规格已确认", f"规格版本 {receipt['specVersion']}",
                 f"保存摘要 SHA-256 {receipt['savedSpecSha256']}")
    return path, receipt


def android_acceptance(serial):
    sdk = Path(os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or
               Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk")
    gradle = ROOT / "android-app"
    phone = A5["Android"](sdk / "platform-tools/adb.exe", serial)
    app = A5["APP"]
    assert phone.shell("getprop", "sys.boot_completed").strip() == "1", "Start and unlock the emulator first"
    assert not phone.shell("pm", "list", "packages", app).strip(), "Acceptance app is already installed"
    subprocess.run([str(gradle / "gradlew.bat"), ":app:assembleDebug", "-PstageA5Acceptance=true"],
                   cwd=gradle, check=True)
    phone.adb("install", str(gradle / "app/build/outputs/apk/debug/app-debug.apk"))
    reverse = None
    try:
        with TemporaryDirectory(prefix="stage-b8-android-") as scratch:
            home = Path(scratch)
            store = home / "projects"
            default_store = ROOT / "desktop-tasks"
            downstream_before = downstream_files(default_store)
            notes_title = "B8 weekly notes"
            contacts_title = "B8 contact alternative"
            phone.launch()
            with Console(home, "127.0.0.1", spec_store=store) as receiver:
                port = receiver.address[1]
                reverse = f"tcp:{port}"
                phone.adb("reverse", "--no-rebind", reverse, reverse)
                A5["pair"](phone, receiver, [], address=f"https://127.0.0.1:{port}")
                notes = send_from_phone(phone, notes_title, "Fictional B8 request for weekly notes.")
                sequence = 0
                for answer in ("Keep weekly notes after restart", "Store notes locally"):
                    question = latest(home, notes, "desktop.question", after=sequence)
                    phone.tap("刷新规格会话")
                    phone.expect("规格会话等待补充", question["question"])
                    phone.fill("补充信息", answer)
                    phone.tap("提交回答")
                    sequence = question["sequence"]
                    submitted = A5["wait_for"](
                        lambda: next((event for event in events(home, notes, "phone")
                                      if event["inReplyTo"] == question["eventId"]), None),
                        "bound clarification answer",
                    )
                    assert submitted["type"] == "phone.answer" and submitted["text"] == answer
                    assert submitted["specVersion"] == question["specVersion"]
                    assert submitted["specSha256"] == question["specSha256"]
                notes_review = latest(home, notes, "desktop.review", after=sequence)
                phone.tap("刷新规格会话")
                notes_path, notes_receipt = confirm_on_phone(phone, home, store, notes, notes_review)
                print("PASS Android new project, two clarifications, review and confirmation")
                phone.tap("返回会话")
                contacts = send_from_phone(phone, contacts_title, MOCK_UNSUPPORTED_REQUEST)
                contacts_review = latest(home, contacts, "desktop.review")
                draft = json.loads(contacts_review["specJson"])
                assert [item["request"] for item in draft["unsupportedItems"]] == ["device.contacts"]
                phone.tap("刷新规格会话")
                phone.expect("规格等待审阅", "审阅完整规格")
                notes_bytes = notes_path.read_bytes()
                receiver.stop()

            phone.launch()
            phone.open_project(contacts_title)
            phone.expect("规格等待审阅", "审阅完整规格")
            with Console(home, "127.0.0.1", port, spec_store=store) as receiver:
                assert latest(home, contacts, "desktop.review") == contacts_review
                contacts_path, contacts_receipt = confirm_on_phone(phone, home, store, contacts, contacts_review)
                contacts_bytes = contacts_path.read_bytes()
                receiver.stop()

            with Console(home, "127.0.0.1", port, spec_store=store) as receiver:
                for title, message, path, payload, receipt, call_count in (
                    (notes_title, notes, notes_path, notes_bytes, notes_receipt, 3),
                    (contacts_title, contacts, contacts_path, contacts_bytes, contacts_receipt, 1),
                ):
                    phone.launch()
                    phone.open_project(title)
                    phone.expect("规格已确认", f"保存摘要 SHA-256 {receipt['savedSpecSha256']}")
                    phone.tap("刷新规格会话")
                    phone.expect("规格已确认", f"规格版本 {receipt['specVersion']}",
                                 f"保存摘要 SHA-256 {receipt['savedSpecSha256']}")
                    assert events(home, message, "desktop")[-1] == receipt
                    assert path.read_bytes() == payload
                    calls = receiver.command(f"model ledger {message['project_id']}")["calls"]
                    assert len(calls) == call_count
                    assert all(call["provider"] == "mock" and call["status"] == "completed" for call in calls)
                assert set(store.glob("*/*")) == {notes_path, contacts_path}
                no_downstream_state(home)
                assert downstream_files(default_store) == downstream_before
                receiver.stop()
        print("PASS Android capability denial and alternative, confirmed digests, restart, and Stage B boundary")
    finally:
        if reverse:
            phone.adb("reverse", "--remove", reverse)
        phone.adb("uninstall", app)


def real_acceptance():
    provider_key = read_key(RELAY_TARGET)
    with TemporaryDirectory(prefix="stage-b8-real-") as scratch:
        home = Path(scratch)
        store = home / "projects"
        usage_file = home / "relay-usage.jsonl"
        with Console(home, "127.0.0.1", spec_store=store, model_budget=REAL_BUDGET,
                     relay_usage=usage_file) as receiver:
            phone = B3["pair"](receiver, "fixture-b8-phone")
            project = "fixture-b8-greeting"
            message = B3["send"](phone, project)
            message.update(body=REAL_BODY, bodySha256=B6["digest"](REAL_BODY))
            received = receiver.post("/v1/messages", message, phone["credential"])
            B3["A2"]["expect_receipt"](received, message)
            assert receiver.post("/v1/messages", message, phone["credential"]) == received
            try:
                draft = latest(home, {"id": message["messageId"], "project_id": project},
                               "desktop.review", "desktop.question")
                assert draft["projectId"] == project
                assert draft["specSha256"] == B6["digest"](draft["specJson"])
                ledger = receiver.command(f"model ledger {project}")
                call, = ledger["calls"]
                assert call["provider"] == "kakou" and call["model"] == RELAY_MODEL
                assert call["prompt_version"] == DRAFT_PROMPT_VERSION and call["status"] == "completed"
                assert call["device_id"] == phone["deviceId"] and call["session_id"] == message["messageId"]
                (reported,) = map(json.loads, usage_file.read_text(encoding="ascii").splitlines())
                assert reported == [call[key] for key in ("input_tokens", "output_tokens", "cached_input_tokens")]
                usage = Usage(*reported)
                assert usage.input_tokens > 0 and usage.output_tokens > 0
                charge = call["charged_nano_usd"]
                assert 0 < charge <= RATE.cost(usage) <= call["reserved_nano_usd"] == REAL_BUDGET
                if not call["exact_charge"]:
                    assert charge == RATE.cost(usage)
                assert ledger["budget_nano_usd"] == REAL_BUDGET
                assert ledger["charged_nano_usd"] == charge and ledger["reserved_nano_usd"] == 0
                assert not store.exists()
                no_downstream_state(home)
            finally:
                receiver.stop(provider_key, phone["credential"], REAL_BODY)
        with Console(home, "127.0.0.1", spec_store=store, model_budget=REAL_BUDGET,
                     relay_usage=usage_file) as receiver:
            assert receiver.post("/v1/messages", message, phone["credential"]) == received
            assert receiver.command(f"model ledger {project}") == ledger
            assert len(usage_file.read_text(encoding="ascii").splitlines()) == 1
            receiver.stop(provider_key, phone["credential"], REAL_BODY)
        print(f"PASS real Kakou {draft['type']} usage equals persistent ledger:", json.dumps(call, sort_keys=True))


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    mode = cli.add_mutually_exclusive_group()
    mode.add_argument("--android", action="store_true", help="run both Android scenarios with the mock provider")
    mode.add_argument("--real-kakou", action="store_true", help="make one real request with the configured credential")
    cli.add_argument("--serial", default=os.environ.get("ANDROID_SERIAL", "emulator-5554"))
    cli.add_argument("--schema", action="store_true", help="include the optional B.1 jsonschema check")
    args = cli.parse_args()
    if args.android:
        if not args.serial.startswith("emulator-"):
            cli.error("--android requires an emulator serial")
        android_acceptance(args.serial)
    elif args.real_kakou:
        real_acceptance()
    else:
        desktop_regression(args.schema)
