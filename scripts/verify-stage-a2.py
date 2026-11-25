"""Exercise the Windows A.2/A.3 HTTPS receiver, device registry, and durable inbox."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import queue
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from desktop_receiver import IO_TIMEOUT, PAIRING_ATTEMPTS, envelope, now, timestamp, token_digest
from message_protocol import MAX_BODY_CHARS, MAX_REQUEST_BYTES, parse_envelope
from receiver_storage import dpapi


SCRIPT = Path(__file__).with_name("desktop_receiver.py")
DEVICE = "fixture-phone"
PROJECT = "fixture-project"
BODY = "虚构需求：记下今天的三件事。"
MESSAGE = "e48f518a-4cec-445e-b38b-004c7724e3ac"
UNKNOWN_CREDENTIAL = "53af7234-82cc-48e1-8219-e87c5e42516d"
NO_TOKEN = "x" * 43
CLIENT_PIN = "a" * 64

# Run the real server and terminate it at a specific transaction boundary.
INTERRUPT = """
import os
import sys

directory, phase, *args = sys.argv[1:]
sys.path.insert(0, directory)
sys.argv[1:] = args
from desktop_receiver import main
from receiver_inbox import Inbox

save = Inbox.save

def exit_before_commit(statement):
    if statement == "COMMIT":
        os._exit(71)

def interrupt_save(self, *args):
    if phase == "before-commit":
        self.database.set_trace_callback(exit_before_commit)
    save(self, *args)
    os._exit(72)

if phase == "model":
    from model_boundary import MockProvider

    def interrupt_model(self, request, stop):
        os._exit(73)

    MockProvider.invoke = interrupt_model
else:
    Inbox.save = interrupt_save
sys.exit(main())
"""

REPLY_FAULTS = """
import sys
import time
from pathlib import Path

directory, fault_path, *args = sys.argv[1:]
sys.path.insert(0, directory)
sys.argv[1:] = args
from desktop_receiver import ReceiverHandler, close_connection, main

fault = Path(fault_path)
respond = ReceiverHandler.respond

def respond_with_fault(self, status, payload):
    if payload.get("operation") == "message.receipt" and fault.is_file():
        mode = fault.read_text(encoding="ascii")
        fault.unlink()
        if mode == "timeout":
            self.deadline.cancel()
            time.sleep(20)
        close_connection(self.connection)
        self.close_connection = True
        return
    respond(self, status, payload)

ReceiverHandler.respond = respond_with_fault
sys.exit(main())
"""

ISOLATED_SPEC_STORE = """
import sys
from functools import partial
from pathlib import Path

directory, spec_store, *args = sys.argv[1:]
sys.path.insert(0, directory)
sys.argv[1:] = args
import desktop_receiver

desktop_receiver.Receiver = partial(desktop_receiver.Receiver, spec_store=Path(spec_store))
sys.exit(desktop_receiver.main())
"""


class Console:
    def __init__(self, home: Path, address: str, port: int = 0, *,
                 interrupt: str | None = None, reply_fault: Path | None = None, spec_store: Path | None = None,
                 model_budget: int | None = None):
        command = [sys.executable, SCRIPT]
        if interrupt:
            command = [sys.executable, "-c", INTERRUPT, SCRIPT.parent, interrupt]
        elif reply_fault is not None:
            command = [sys.executable, "-c", REPLY_FAULTS, SCRIPT.parent, reply_fault]
        elif spec_store is not None:
            command = [sys.executable, "-c", ISOLATED_SPEC_STORE, SCRIPT.parent, spec_store]
        command += ["start", "--bind", address, "--port", str(port)]
        if model_budget is not None:
            command += ["--model-budget-nano-usd", str(model_budget)]
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", env={**os.environ, "LOCALAPPDATA": str(home)},
        )
        self.output: queue.Queue = queue.Queue()
        self.public = []
        self.errors = []
        self.reader = threading.Thread(target=self.read_stdout, daemon=True)
        self.error_reader = threading.Thread(target=self.read_stderr, daemon=True)
        self.reader.start()
        self.error_reader.start()

    def read_stdout(self) -> None:
        for line in self.process.stdout:
            self.output.put(line)
        self.output.put(None)

    def read_stderr(self) -> None:
        self.errors.extend(self.process.stderr)

    def read(self, *, private: bool = False):
        line = self.output.get(timeout=10)
        assert line is not None, "receiver stopped before responding"
        if not private:
            self.public.append(line)
        return json.loads(line)

    def command(self, command: str):
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()
        return self.read(private=command in {"pair", "inbox"})

    def __enter__(self):
        try:
            status = self.read()
            assert status["state"] == "listening"
            self.pin = status["serverCertificateFingerprint"]
            address = urlsplit(status["address"])
            self.address = address.hostname, address.port
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=10)
        self.reader.join(timeout=10)
        self.error_reader.join(timeout=10)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()

    def stop(self, *sensitive: str) -> None:
        assert self.command("stop") == {"state": "stopped"}
        assert self.process.wait(timeout=10) == 0
        self.error_reader.join(timeout=10)
        logs = "".join(self.errors + self.public)
        for secret in sensitive:
            escaped = json.dumps(secret, ensure_ascii=True)[1:-1]
            assert secret not in logs and escaped not in logs, "secret or message content leaked into logs"
        try:
            socket.create_connection(self.address, timeout=0.5).close()
        except (ConnectionRefusedError, TimeoutError):
            return
        raise AssertionError("TCP listener remained reachable after stop")

    def tls(self, *, pin: str | None = None, version=ssl.TLSVersion.TLSv1_3) -> ssl.SSLSocket:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = context.maximum_version = version
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with socket.create_connection(self.address, timeout=10) as tcp:
            tls = context.wrap_socket(tcp)
        if hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest() != (pin or self.pin):
            tls.close()
            raise ssl.SSLCertVerificationError("pin mismatch")
        return tls

    def post(self, path: str, request: dict | bytes, token: str | None = None,
             *, size: int | None = None) -> tuple[int, dict]:
        body = json.dumps(request, ensure_ascii=True).encode("ascii") if isinstance(request, dict) else request
        with closing(http.client.HTTPConnection(*self.address)) as connection:
            connection.sock = self.tls()
            headers = {"Content-Type": "application/json; charset=utf-8", "Connection": "close"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            if size is not None:
                headers["Content-Length"] = str(size)
            connection.request("POST", path, body=body, headers=headers)
            with connection.getresponse() as reply:
                payload = parse_envelope(reply.read())
                assert reply.getheader("Cache-Control") == "no-store"
                if isinstance(request, dict):
                    assert payload["inReplyTo"] == request["requestId"] or size is not None
                return reply.status, payload


def pairing_request(code: str, *, expiry=timedelta(minutes=5), device_id=DEVICE) -> dict:
    expires = now() + expiry
    return envelope("pair.request", createdAt=timestamp(expires - timedelta(minutes=5)),
                    deviceId=device_id, deviceName="虚构测试设备", clientKeyFingerprint=CLIENT_PIN,
                    pairingCode=code, expiresAt=timestamp(expires))


def pairing_decision(console: Console, request: dict, action: str | None) -> dict:
    devices_before = console.command("devices")
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(console.post, "/v1/pair", request)
        for _ in range(40):
            entries = console.command("pending")
            if entries:
                break
            assert not pending.done(), "pairing completed without a desktop decision or expiry"
            time.sleep(0.05)
        assert len(entries) == 1
        assert entries[0]["requestId"] == request["requestId"]
        assert entries[0]["deviceId"] == request["deviceId"]
        assert entries[0]["deviceName"] == request["deviceName"]
        assert entries[0]["clientKeyFingerprint"] == CLIENT_PIN
        assert entries[0]["expiresAt"] == request["expiresAt"]
        assert not pending.done()
        assert console.command("devices") == devices_before
        if action:
            assert console.command(f"{action} {request['requestId']}")["decision"] == action
        status, reply = pending.result(timeout=8)
    assert status == 200 and reply["operation"] == "pair.confirm"
    return reply


def expect_error(reply: tuple[int, dict], status: int, code: str) -> None:
    actual, payload = reply
    assert actual == status and payload["operation"] == "error" and payload["error"]["code"] == code, (
        f"expected HTTP {status} / {code}, got HTTP {actual} / {payload.get('error', {}).get('code')}"
    )


def expect_receipt(reply: tuple[int, dict], request: dict) -> dict:
    status, receipt = reply
    assert status == 200 and receipt["operation"] == "message.receipt"
    assert receipt["status"] == "received" and receipt["retryable"] is False
    assert all(receipt[key] == request[key] for key in ("deviceId", "messageId", "projectId", "sequence"))
    return receipt


def expect_status(reply: tuple[int, dict], receipt: dict) -> None:
    status, found = reply
    assert status == 200 and found["operation"] == "status.response" and found["retryable"] is False
    assert all(found[key] == receipt[key] for key in ("deviceId", "messageId", "projectId", "sequence", "status"))
    assert found["updatedAt"] == receipt["receivedAt"]


def with_body(request: dict, body: str) -> dict:
    return {**request, "body": body, "bodySha256": hashlib.sha256(body.encode("utf-8")).hexdigest()}


def interrupted_send(home: Path, address: str, message: dict, token: str, phase: str, exit_code: int) -> None:
    with Console(home, address, interrupt=phase) as receiver:
        try:
            receiver.post("/v1/messages", message, token)
        except (http.client.RemoteDisconnected, ConnectionResetError, ssl.SSLEOFError):
            pass
        else:
            raise AssertionError("interrupted receiver sent a response")
        assert receiver.process.wait(timeout=10) == exit_code


def check_recovery(home: Path, address: str, message: dict, token: str, previous: dict) -> dict:
    query = envelope("status.query", **{key: message[key] for key in ("deviceId", "credentialId", "messageId")})
    interrupted_send(home, address, message, token, "before-commit", 71)
    with Console(home, address) as receiver:
        assert receiver.command("inbox") == [previous]
        expect_error(receiver.post("/v1/status", query, token), 404, "MESSAGE_NOT_FOUND")
        receiver.stop(token, BODY)

    interrupted_send(home, address, message, token, "after-commit", 72)
    with Console(home, address) as receiver:
        status, found = receiver.post("/v1/status", query, token)
        assert status == 200 and found["status"] == "received"
        receipt = expect_receipt(receiver.post("/v1/messages", message, token), message)
        expect_status((status, found), receipt)
        assert receiver.post("/v1/messages", message, token) == (200, receipt)
        inbox = receiver.command("inbox")
        assert len(inbox) == 2 and inbox[0] == previous and inbox[1]["messageId"] == message["messageId"]
        receiver.stop(token, BODY)
    print("PASS interruption before commit rolls back; interruption after commit recovers the original receipt")
    return inbox[1]


def check_connection(console: Console, address: str) -> None:
    assert console.address[0] == address
    assert console.command("status")["address"] == f"https://{address}:{console.address[1]}"
    with console.tls() as connection:
        assert connection.version() == "TLSv1.3"
    for options, error in [({"version": ssl.TLSVersion.TLSv1_2}, ssl.SSLError),
                           ({"pin": "0" * 64}, ssl.SSLCertVerificationError)]:
        try:
            console.tls(**options).close()
        except error:
            continue
        raise AssertionError("unacceptable TLS session succeeded")
    with socket.create_connection(console.address, timeout=IO_TIMEOUT + 2) as tcp:
        tcp.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        try:
            cleartext = tcp.recv(128)
        except ConnectionResetError:
            cleartext = b""
        assert b"HTTP" not in cleartext, "cleartext HTTP response"


def check_deadlines(console: Console) -> None:
    with socket.create_connection(console.address, timeout=IO_TIMEOUT + 3) as idle, console.tls() as partial:
        partial.settimeout(IO_TIMEOUT + 3)
        partial.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: localhost\r\n"
                        b"Content-Type: application/json\r\nContent-Length: 50\r\n\r\n{")
        for connection in (idle, partial):
            try:
                assert connection.recv(1) == b"", "idle connection was not closed"
            except ConnectionResetError:
                pass
        assert console.command("status")["state"] == "listening"


def run(home: Path, address: str) -> None:
    with Console(home, address) as receiver:
        check_connection(receiver, address)
        check_deadlines(receiver)
        print("PASS explicit binding, pinned TLS 1.3, plaintext refusal and I/O deadlines")
        query = envelope("status.query", deviceId=DEVICE, credentialId=UNKNOWN_CREDENTIAL, messageId=MESSAGE)
        expect_error(receiver.post("/v1/status", query), 401, "AUTH_REQUIRED")
        expect_error(receiver.post("/v1/status", query, NO_TOKEN), 401, "AUTH_REQUIRED")
        expect_error(receiver.post("/v1/pair", pairing_request("111111")), 403, "PAIRING_REQUIRED")
        code = receiver.command("pair")["pairingCode"]
        expired = pairing_request(code, expiry=timedelta(seconds=-1))
        expect_error(receiver.post("/v1/pair", expired), 410, "PAIRING_EXPIRED")
        expired_pending = pairing_decision(receiver, pairing_request(code, expiry=timedelta(seconds=3)), None)
        assert expired_pending["approved"] is False and expired_pending["errorCode"] == "PAIRING_EXPIRED"
        rejected_code = receiver.command("pair")["pairingCode"]
        rejected = pairing_decision(receiver, pairing_request(rejected_code), "reject")
        assert rejected["approved"] is False and rejected["errorCode"] == "PAIRING_REJECTED"
        legal_code = receiver.command("pair")["pairingCode"]
        request = pairing_request(legal_code)
        approved = pairing_decision(receiver, request, "approve")
        assert approved["approved"] is True and approved["serverCertificateFingerprint"] == receiver.pin
        expect_error(receiver.post("/v1/pair", request), 403, "PAIRING_REQUIRED")
        query["credentialId"] = approved["credentialId"]
        token = approved["credential"]
        expect_error(receiver.post("/v1/status", query, token), 404, "MESSAGE_NOT_FOUND")
        assert receiver.command("devices")[0]["status"] == "paired"
        for _ in range(PAIRING_ATTEMPTS - 6):
            expect_error(receiver.post("/v1/pair", pairing_request(legal_code)), 403, "PAIRING_REQUIRED")
        # Rate limiting runs before reading a request ID.
        limited = receiver.post("/v1/pair", json.dumps(request).encode())
        expect_error(limited, 429, "PAIRING_RATE_LIMITED")
        assert limited[1]["error"]["retryable"] and limited[1]["error"]["retryAfterSeconds"] > 0
        print("PASS unpaired, expired, rejected, approved, one-use and rate-limited pairing")

        message = envelope("message.send", deviceId=DEVICE, credentialId=query["credentialId"], messageId=MESSAGE,
                           projectId=PROJECT, sequence=1, messageType="user.message", body=BODY,
                           bodySha256=hashlib.sha256(BODY.encode("utf-8")).hexdigest(), specVersion=1,
                           confirmation={"specVersion": 1, "decision": "confirmed", "confirmedAt": timestamp(now())})
        expect_error(receiver.post("/v1/messages", message), 401, "AUTH_REQUIRED")
        expect_error(receiver.post("/v1/messages", message, NO_TOKEN), 401, "AUTH_REQUIRED")
        expect_error(receiver.post("/v1/messages", {**message, "deviceId": "other-phone"}, token), 401, "AUTH_REQUIRED")
        expect_error(receiver.post("/v1/messages", {**message, "schemaVersion": True}, token), 400, "INVALID_REQUEST")
        expect_error(receiver.post("/v1/messages", {**message, "operation": []}, token), 400, "INVALID_REQUEST")
        expect_error(receiver.post("/v1/messages", {**message, "messageType": {}}, token), 400, "INVALID_REQUEST")
        expect_error(receiver.post("/v1/messages", {**message, "body": "\ud800"}, token), 400, "INVALID_REQUEST")
        expect_error(receiver.post("/v1/messages", {**message, "bodySha256": "0" * 64}, token), 400, "INVALID_REQUEST")
        expect_error(receiver.post("/v1/messages", b'{"operation": 1, "operation": 2}', token), 400, "INVALID_REQUEST")
        expect_error(receiver.post("/v1/messages", b"[" * 10000, token), 400, "INVALID_REQUEST")
        expect_error(receiver.post("/v1/messages", message, token, size=MAX_REQUEST_BYTES + 1), 413, "PAYLOAD_TOO_LARGE")
        expect_error(receiver.post("/v1/messages", with_body(message, "a" * (MAX_BODY_CHARS + 1)), token),
                     400, "INVALID_REQUEST")
        expect_error(receiver.post("/v1/messages", {**message, "createdAt": timestamp(now() - timedelta(days=2))}, token),
                     400, "RETRY_WINDOW_EXPIRED")
        expect_error(receiver.post("/v1/tasks", b"{}", token), 404, "INVALID_REQUEST")
        expect_error(receiver.post("/v1/pair", b"{}", token), 429, "PAIRING_RATE_LIMITED")
        expect_error(receiver.post("/v1/status", {**query, "operation": "pair.revoke"}, token), 400, "INVALID_REQUEST")

        assert receiver.command("inbox") == []
        with ThreadPoolExecutor(max_workers=4) as executor:
            pending = [executor.submit(receiver.post, "/v1/messages", message, token) for _ in range(4)]
            receipt = expect_receipt(pending[0].result(), message)
            assert all(reply.result() == (200, receipt) for reply in pending[1:])
        retried = {**message, "requestId": str(uuid4()),
                   "confirmation": dict(reversed(list(message["confirmation"].items())))}
        assert receiver.post("/v1/messages", retried, token) == (200, {**receipt, "inReplyTo": retried["requestId"]})
        conflicts = [
            with_body(message, "另一条虚构需求"), {**message, "projectId": "other-project"},
            {**message, "sequence": 2}, {**message, "messageType": "user.answer"},
            {**message, "specVersion": 2},
            {**message, "confirmation": {**message["confirmation"], "decision": "rejected"}},
        ]
        for conflict in conflicts:
            expect_error(receiver.post("/v1/messages", conflict, token), 409, "MESSAGE_ID_CONFLICT")
        expect_error(receiver.post("/v1/messages", {**message, "messageId": str(uuid4())}, token),
                     409, "MESSAGE_SEQUENCE_CONFLICT")
        expect_status(receiver.post("/v1/status", query, token), receipt)
        inbox_row = {"messageId": MESSAGE, "projectId": PROJECT, "receivedAt": receipt["receivedAt"],
                     "status": "received", "body": BODY}
        assert receiver.command("inbox") == [inbox_row]
        state_dir = home / "AndroidPluginAgentReceiver"
        protected = (state_dir / "devices.dpapi").read_bytes()
        decrypted = dpapi(protected, encrypt=False)
        assert DEVICE.encode() not in protected and token.encode() not in protected + decrypted
        assert token_digest(token).encode() in decrypted
        assert not list(state_dir.glob("tls-*"))
        print("PASS durable receipts, concurrent deduplication, message/sequence conflicts, invalid/oversize requests")
        receiver.stop("pairingCode", *(json.dumps(code) for code in (code, rejected_code, legal_code)),
                      token, NO_TOKEN, BODY, "另一条虚构需求")
        events = [json.loads(line) for line in receiver.errors]
        assert all(set(event) <= {"at", "messageId", "status", "error"} for event in events)
        assert sum(event.get("status") == "received" and event.get("messageId") == MESSAGE for event in events) == 1
        assert sum(event.get("status") == "duplicate" and event.get("messageId") == MESSAGE for event in events) == 4
        assert any(event.get("error") == "MESSAGE_ID_CONFLICT" and event.get("messageId") == MESSAGE for event in events)
        pin, port = receiver.pin, receiver.address[1]

    recovered = check_recovery(home, address, {**message, "messageId": str(uuid4()), "sequence": 2}, token, inbox_row)
    with Console(home, address, port) as receiver:
        assert receiver.pin == pin and receiver.command("devices")[0]["status"] == "paired"
        assert receiver.command("inbox") == [inbox_row, recovered]
        expect_status(receiver.post("/v1/status", query, token), receipt)
        assert receiver.post("/v1/messages", message, token) == (200, receipt)
        other_code = receiver.command("pair")["pairingCode"]
        other = pairing_decision(receiver, pairing_request(other_code, device_id="other-phone"), "approve")
        other_query = {**query, "deviceId": other["deviceId"], "credentialId": other["credentialId"]}
        other_token = other["credential"]
        expect_error(receiver.post("/v1/status", other_query, other_token), 404, "MESSAGE_NOT_FOUND")
        other_message = {**message, "deviceId": other["deviceId"], "credentialId": other["credentialId"]}
        other_receipt = expect_receipt(receiver.post("/v1/messages", other_message, other_token), other_message)
        assert other_receipt["requestId"] != receipt["requestId"]
        assert receiver.command(f"revoke {DEVICE}")["status"] == "revoked"
        for path, request in (("/v1/messages", message), ("/v1/status", query)):
            status, revoked = receiver.post(path, request, token)
            assert status == 403 and revoked["operation"] == "pair.revoked"
        expect_error(receiver.post("/v1/status", query, NO_TOKEN), 401, "AUTH_REQUIRED")
        expect_status(receiver.post("/v1/status", other_query, other_token), other_receipt)
        receiver.stop(token, other_token, json.dumps(other_code), NO_TOKEN, BODY)

    with Console(home, address, port) as receiver:
        assert receiver.command("devices")[0]["status"] == "revoked"
        assert receiver.post("/v1/status", query, token)[1]["operation"] == "pair.revoked"
        expect_status(receiver.post("/v1/status", other_query, other_token), other_receipt)
        renewed_code = receiver.command("pair")["pairingCode"]
        renewed = pairing_decision(receiver, pairing_request(renewed_code), "approve")
        assert renewed["credentialId"] != query["credentialId"] and renewed["credential"] != token
        assert receiver.post("/v1/messages", message, token)[1]["operation"] == "pair.revoked"
        renewed_message = {**message, "credentialId": renewed["credentialId"]}
        assert receiver.post("/v1/messages", renewed_message, renewed["credential"]) == (200, receipt)
        assert len(receiver.command("inbox")) == 3
        receiver.stop(token, other_token, renewed["credential"], json.dumps(renewed_code), BODY)

    with Console(home, address, port) as receiver:
        assert len(receiver.command("inbox")) == 3
        assert receiver.command("inbox clear") == {"error": "UNKNOWN_COMMAND"}
        assert receiver.command("inbox clear CONFIRM") == {"cleared": 3}
        assert receiver.command("inbox") == []
        expect_error(receiver.post("/v1/status", other_query, other_token), 404, "MESSAGE_NOT_FOUND")
        receiver.stop(token, other_token, BODY)
    database = state_dir / "inbox.sqlite3"
    assert BODY.encode("utf-8") not in database.read_bytes()
    with Console(home, address, port) as receiver:
        assert receiver.command("inbox") == []
        assert receiver.post("/v1/status", query, token)[1]["operation"] == "pair.revoked"
        receiver.stop(token, other_token, BODY)
    print("PASS stop closes port, restart retains pairing/revocation, logs exclude secrets and body")
    print("PASS preserved receipts across restart, device isolation and persistent inbox cleanup")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1", help="explicit local address for the test server and client")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="receiver-a2-") as directory:
        home = Path(directory)
        for arguments in ([], ["--bind", "0.0.0.0"], ["--bind", "8.8.8.8"]):
            rejected = subprocess.run(
                [sys.executable, SCRIPT, "start", *arguments],
                capture_output=True, env={**os.environ, "LOCALAPPDATA": str(home)}, timeout=5,
            )
            assert rejected.returncode == 2 and not list(home.iterdir())
        run(home, args.bind)
    print("A.2/A.3 DESKTOP RECEIVER AND INBOX PASSED")


if __name__ == "__main__":
    main()
