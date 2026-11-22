"""Run the Stage A.5 Android UI and Windows receiver acceptance on a booted emulator."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import runpy
import sqlite3
import subprocess
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path
from xml.etree import ElementTree

from receiver_storage import dpapi


ROOT = Path(__file__).resolve().parents[1]
Console = runpy.run_path(str(Path(__file__).with_name("verify-stage-a2.py")))["Console"]
APP = "com.example.pluginhost.a5acceptance"
BODY = {name: f"A5 fictional plugin request {name}." for name in (
    "normal", "offline", "timeout", "interrupted", "restart", "moved", "revoked", "blocked",
)}
RETRY = "重试待发送消息"
REVOKED = "配对已撤销，请重新配对。"


def wait_for(condition, description, timeout=25):
    deadline = time.monotonic() + timeout
    while True:
        if result := condition():
            return result
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out waiting for {description}")
        time.sleep(0.25)


class Android:
    def __init__(self, adb: Path, serial: str):
        self.command = [str(adb), "-s", serial]

    def adb(self, *args: str) -> bytes:
        try:
            result = subprocess.run([*self.command, *args], capture_output=True, timeout=45)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"ADB {args[0]} timed out") from None
        if result.returncode != 0:
            # A pairing code can be present in an input command.
            raise RuntimeError(f"ADB {args[0]} failed ({result.returncode})")
        return result.stdout

    def shell(self, *args: str) -> str:
        return self.adb("shell", *args).decode("utf-8")

    def nodes(self):
        raw = self.adb("exec-out", "uiautomator", "dump", "/dev/stdout")
        xml, end, _ = raw.partition(b"</hierarchy>")
        return list(ElementTree.fromstring(xml + end).iter("node")) if end else []

    def expect(self, *labels: str):
        def match():
            nodes = self.nodes()
            texts = {node.get("text") for node in nodes}
            return nodes if all(label in texts for label in labels) else None

        return wait_for(match, " / ".join(labels))

    def tap(self, label: str):
        node = next(node for node in self.expect(label) if node.get("text") == label)
        x1, y1, x2, y2 = map(int, re.findall(r"\d+", node.get("bounds")))
        self.shell("input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))

    def fill(self, field: str, text: str, *, replace=False):
        self.tap(field)
        if replace:
            self.shell("input", "keycombination", "113", "29")  # Ctrl+A
        self.shell("input", "text", text.replace(" ", "%s"))
        if "mInputShown=true" in self.shell("dumpsys", "input_method"):
            self.shell("input", "keyevent", "KEYCODE_BACK")

    def launch(self, *, reopen=False, connected=None):
        self.shell("am", "force-stop", APP)
        self.shell("am", "start", "-W", "-n", f"{APP}/com.example.pluginhost.MainActivity")
        self.tap("开发会话")
        self.expect("项目会话")
        if connected:
            self.expect(f"上次连接成功 · {connected}")
        if reopen:
            self.tap("A5 acceptance project")

    def send(self, name: str):
        self.fill("输入插件需求", BODY[name])
        self.tap("发送")

    def outbox(self):
        data = self.adb("exec-out", "run-as", APP, "cat", "no_backup/desktop-conversations.db")
        with closing(sqlite3.connect(":memory:")) as db:
            db.deserialize(data)
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM messages ORDER BY sequence").fetchall()
        return {row["body"]: dict(row) for row in rows}

    @contextmanager
    def offline(self):
        wifi = self.shell("settings", "get", "global", "wifi_on").strip()
        data = self.shell("settings", "get", "global", "mobile_data").strip()
        assert wifi in {"0", "1"} and data in {"0", "1"}, "Unknown emulator network settings"

        def has_network():
            return bool(re.search(r"(?m)^Active default network: \d+", self.shell("dumpsys", "connectivity")))

        assert has_network(), "Emulator requires an active network before the offline test"
        try:
            self.shell("svc", "wifi", "disable")
            self.shell("svc", "data", "disable")
            wait_for(lambda: not has_network(), "offline emulator")
            yield
        finally:
            self.shell("svc", "wifi", "enable" if wifi == "1" else "disable")
            self.shell("svc", "data", "enable" if data == "1" else "disable")
        wait_for(has_network, "restored emulator network")


def pair(phone, receiver, codes, *, address=None, device_id=None):
    code = receiver.command("pair")["pairingCode"]
    codes.append(code)
    phone.tap("连接电脑")
    nodes = phone.expect("电脑配对")
    shown_id = next(node.get("text").split(" ")[-1] for node in nodes
                    if node.get("text", "").startswith("本机设备 ID "))
    if device_id is not None:
        assert shown_id == device_id
    if address is not None:
        phone.fill("电脑 HTTPS 地址", address)
        phone.fill("证书 SHA-256 指纹", receiver.pin)
    phone.fill("六位配对码", code)
    assert all(code != node.get("text") for node in phone.nodes()), "Pairing code in UI snapshot"
    phone.tap("申请配对")
    entries = wait_for(lambda: receiver.command("pending"), "desktop pairing request")
    assert len(entries) == 1 and entries[0]["deviceId"] == shown_id
    assert receiver.command(f"approve {entries[0]['requestId']}")["decision"] == "approve"
    phone.tap("返回会话")
    return shown_id


def check_pending(phone, name, state, reason=None):
    delivery = "QUEUED" if state == "本机排队" and reason is None else "RETRYABLE"

    def pending():
        row = phone.outbox().get(BODY[name])
        return row if row and row["delivery"] == delivery else None

    row = wait_for(pending, f"persisted pending message for {name}")
    labels = [BODY[name], state]
    if reason:
        labels.append(reason)
    phone.expect(*labels)
    assert row["receipt_id"] is None
    return row


def check_received(phone, receiver, state, name, original=None):
    phone.expect(BODY[name], "电脑已接收")
    local = wait_for(
        lambda: (row if (row := phone.outbox().get(BODY[name])) and row["delivery"] == "RECEIVED" else None),
        f"persisted Android receipt for {name}",
    )
    inbox = receiver.command("inbox")
    assert [row for row in inbox if row["body"] == BODY[name]] == [{
        "messageId": local["id"], "projectId": local["project_id"], "body": BODY[name],
        "receivedAt": local["received_at"], "status": "received",
    }]
    with closing(sqlite3.connect(state / "inbox.sqlite3")) as db:
        assert db.execute("SELECT receipt_id, sequence, body_sha256 FROM messages WHERE message_id = ?",
                          (local["id"],)).fetchall() == [
            (local["receipt_id"], local["sequence"], hashlib.sha256(BODY[name].encode()).hexdigest()),
        ]
    if original:
        assert all(local[key] == original[key] for key in ("id", "project_id", "sequence", "created_at", "body"))
    return local


def credential_records(state: Path):
    return json.loads(dpapi((state / "devices.dpapi").read_bytes(), encrypt=False))


def run(phone: Android, home: Path):
    state = home / "AndroidPluginAgentReceiver"
    fault = home / "reply-fault"
    codes = []
    audit = []
    phone.launch()
    with Console(home, "127.0.0.1", reply_fault=fault) as receiver:
        pin, port = receiver.pin, receiver.address[1]
        original_address = f"https://10.0.2.2:{port}"
        device = pair(phone, receiver, codes, address=original_address)
        phone.expect(f"上次连接成功 · {original_address}")
        phone.tap("新建项目")
        phone.fill("项目名称", "A5 acceptance project")
        phone.tap("创建")
        phone.send("normal")
        normal = check_received(phone, receiver, state, "normal")
        print("PASS Android pairing, project creation, message and durable receipt")

        with phone.offline():
            phone.send("offline")
            offline = check_pending(phone, "offline", "本机排队", "手机当前离线，联网后可重试。")
            assert len(receiver.command("inbox")) == 1
        phone.tap(RETRY)
        check_received(phone, receiver, state, "offline", offline)
        print("PASS offline queue and same-ID retry")

        for mode, name, reason in (
            ("timeout", "timeout", "连接或等待电脑响应超时。"),
            ("drop", "interrupted", "与电脑的连接中断，可重试原消息。"),
        ):
            fault.write_text(mode, encoding="ascii")
            phone.send(name)
            pending = check_pending(phone, name, "发送失败，可重试", reason)
            persisted = [row for row in receiver.command("inbox") if row["messageId"] == pending["id"]]
            assert len(persisted) == 1
            phone.tap(RETRY)
            recovered = check_received(phone, receiver, state, name, pending)
            assert recovered["received_at"] == persisted[0]["receivedAt"]
        receiver.stop(*(json.dumps(code) for code in codes), *BODY.values())
        audit.extend(receiver.errors)
    print("PASS response timeout and interrupted connection after commit, original receipts on retry")

    phone.send("restart")
    stopped = check_pending(phone, "restart", "发送失败，可重试")
    assert stopped["detail"] in {"DESKTOP_UNREACHABLE", "DESKTOP_INTERRUPTED", "DESKTOP_TLS", "DESKTOP_TIMEOUT"}, stopped["detail"]
    with Console(home, "127.0.0.1", port) as receiver:
        assert receiver.pin == pin and len(receiver.command("inbox")) == 4
        phone.launch(reopen=True, connected=original_address)
        assert check_pending(phone, "restart", "发送失败，可重试")["id"] == stopped["id"]
        phone.tap(RETRY)
        check_received(phone, receiver, state, "restart", stopped)
        receiver.stop(*BODY.values())
        audit.extend(receiver.errors)
    print("PASS service stop and restart, app process restart, and same-ID retry")

    phone.send("moved")
    moved = check_pending(phone, "moved", "发送失败，可重试")
    different_home = home / "different-receiver"
    different_home.mkdir()
    with Console(different_home, "127.0.0.1") as different:
        phone.tap("连接电脑")
        phone.fill(original_address, f"https://10.0.2.2:{different.address[1]}", replace=True)
        phone.tap("验证并更新电脑地址")
        phone.expect("电脑证书指纹不匹配，请在电脑上重新核对。")
        assert different.command("inbox") == []
        different.stop()
    with Console(home, "127.0.0.1") as receiver:
        assert receiver.pin == pin and receiver.address[1] != port
        # ADB reverse exposes a different IPv4 and port without a LAN or firewall dependency.
        moved_port = f"tcp:{receiver.address[1]}"
        phone.adb("reverse", "--no-rebind", moved_port, moved_port)
        updated = f"https://127.0.0.1:{receiver.address[1]}"
        try:
            phone.fill(f"https://10.0.2.2:{different.address[1]}", updated, replace=True)
            phone.tap("验证并更新电脑地址")
            phone.expect(f"上次连接成功 · {updated}")
            phone.tap("返回会话")
            check_received(phone, receiver, state, "moved", moved)
            phone.launch(reopen=True, connected=updated)
            before = credential_records(state)
            assert len(before) == 1
            print("PASS rejected wrong server identity and persisted verified new IPv4/port")

            assert receiver.command(f"revoke {device}")["status"] == "revoked"
            phone.send("revoked")
            revoked = check_pending(phone, "revoked", "发送失败，可重试", REVOKED)
            phone.send("blocked")
            blocked = check_pending(phone, "blocked", "本机排队")
            phone.launch(reopen=True)
            texts = {node.get("text") for node in phone.expect(REVOKED, BODY["blocked"])}
            assert RETRY not in texts and len(receiver.command("inbox")) == 6
            pair(phone, receiver, codes, device_id=device)
            check_received(phone, receiver, state, "blocked", blocked)
            check_received(phone, receiver, state, "revoked", revoked)
            rows = phone.outbox()
            assert len(rows) == len(BODY) and all(row["delivery"] == "RECEIVED" for row in rows.values())
            assert rows[BODY["normal"]] == normal and len(receiver.command("inbox")) == len(rows)
            records = credential_records(state)
            renewed = records.keys() - before.keys()
            assert len(renewed) == 1 and all("revokedAt" in records[key] for key in before)
            assert records[renewed.pop()]["credentialDigest"] not in {record["credentialDigest"] for record in before.values()}
            receiver.stop(*(json.dumps(code) for code in codes), *BODY.values())
            audit.extend(receiver.errors)
        finally:
            phone.adb("reverse", "--remove", moved_port)

    events = [json.loads(line) for line in audit]
    assert all(set(event) <= {"at", "messageId", "status", "error"} for event in events)
    for name, body in BODY.items():
        attempts = [event.get("status") or event.get("error") for event in events if event.get("messageId") == rows[body]["id"]]
        expected = {
            "timeout": ["received", "duplicate"], "interrupted": ["received", "duplicate"],
            "revoked": ["DEVICE_REVOKED", "received"],
        }.get(name, ["received"])
        assert attempts == expected, f"Unexpected receiver attempts for {name}"

    uid = re.search(r"\b(?:appId|userId)=(\d+)", phone.shell("dumpsys", "package", APP))[1]
    logs = phone.shell("logcat", "-d", f"--uid={uid}")
    assert not any(re.search(rf"(?<!\d){code}(?!\d)", logs) for code in codes), "Pairing code in Android log"
    digests = {record["credentialDigest"] for record in records.values()}
    assert not any(hashlib.sha256(token.encode()).hexdigest() in digests for token in
                   re.findall(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])", logs)), "Credential in Android log"
    assert all(body not in logs for body in BODY.values()), "Message body in Android log"
    print(f"PASS revocation, restart while revoked, fresh credential, no confirmed replay, "
          f"{len(rows)} distinct receipts, and Android/desktop log checks")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", default=os.environ.get("ANDROID_SERIAL", "emulator-5554"))
    args = parser.parse_args()
    if not re.fullmatch(r"emulator-\d+", args.serial):
        parser.error("Specify an emulator serial")
    sdk = Path(os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or
               Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk")
    phone = Android(sdk / "platform-tools" / "adb.exe", args.serial)
    assert phone.shell("getprop", "sys.boot_completed").strip() == "1", "Start and unlock the emulator first"
    assert APP not in phone.shell("pm", "list", "packages", APP), "Acceptance app is already installed"
    android = ROOT / "android-app"
    subprocess.run([str(android / "gradlew.bat"), ":app:assembleDebug", "-PstageA5Acceptance=true"],
                   cwd=android, check=True)
    apk = android / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
    phone.adb("install", str(apk))
    try:
        with tempfile.TemporaryDirectory(prefix="stage-a5-") as directory:
            run(phone, Path(directory))
    finally:
        phone.adb("uninstall", APP)
    print("A.5 ANDROID TO WINDOWS ACCEPTANCE PASSED")


if __name__ == "__main__":
    main()
