"""Exercise the real Windows A.2 console, pinned TLS transport, and device registry."""

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

from desktop_receiver import IO_TIMEOUT, PAIRING_ATTEMPTS, envelope, now, timestamp, token_digest
from message_protocol import MAX_REQUEST_BYTES, parse_envelope
from receiver_storage import dpapi


SCRIPT = Path(__file__).with_name("desktop_receiver.py")
DEVICE = "fixture-phone"
PROJECT = "fixture-project"
BODY = "虚构需求：记下今天的三件事。"
MESSAGE = "e48f518a-4cec-445e-b38b-004c7724e3ac"
UNKNOWN_CREDENTIAL = "53af7234-82cc-48e1-8219-e87c5e42516d"
NO_TOKEN = "x" * 43
CLIENT_PIN = "a" * 64


class Console:
    def __init__(self, home: Path, address: str, port: int = 0):
        self.process = subprocess.Popen(
            [sys.executable, SCRIPT, "start", "--bind", address, "--port", str(port)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", env={**os.environ, "LOCALAPPDATA": str(home)},
        )
        self.output: queue.Queue = queue.Queue()
        self.public = []
        self.reader = threading.Thread(target=self.read_stdout, daemon=True)
        self.reader.start()

    def read_stdout(self) -> None:
        for line in self.process.stdout:
            self.output.put(line)
        self.output.put(None)

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
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()

    def stop(self, *sensitive: str) -> None:
        assert self.command("stop") == {"state": "stopped"}
        assert self.process.wait(timeout=10) == 0
        logs = self.process.stderr.read() + "".join(self.public)
        assert all(secret not in logs for secret in sensitive), "secret or message content leaked into logs"
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
        assert all(device["deviceId"] != request["deviceId"] for device in console.command("devices"))
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
                           bodySha256=hashlib.sha256(BODY.encode("utf-8")).hexdigest())
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
        expect_error(receiver.post("/v1/messages", {**message, "createdAt": timestamp(now() - timedelta(days=2))}, token),
                     400, "RETRY_WINDOW_EXPIRED")
        expect_error(receiver.post("/v1/tasks", b"{}", token), 404, "INVALID_REQUEST")
        expect_error(receiver.post("/v1/pair", b"{}", token), 429, "PAIRING_RATE_LIMITED")
        expect_error(receiver.post("/v1/status", {**query, "operation": "pair.revoke"}, token), 400, "INVALID_REQUEST")

        status, queued = receiver.post("/v1/messages", message, token)
        assert status == 202 and queued["operation"] == "status.response" and queued["status"] == "queued"
        assert receiver.post("/v1/messages", message, token)[1]["updatedAt"] == queued["updatedAt"]
        status, found = receiver.post("/v1/status", query, token)
        assert status == 200 and found["status"] == "queued" and found["updatedAt"] == queued["updatedAt"]
        assert receiver.command("inbox") == [
            {"messageId": MESSAGE, "projectId": PROJECT, "receivedAt": queued["updatedAt"], "status": "queued", "body": BODY}
        ]
        state_dir = home / "AndroidPluginAgentReceiver"
        protected = (state_dir / "devices.dpapi").read_bytes()
        decrypted = dpapi(protected, encrypt=False)
        assert DEVICE.encode() not in protected and token.encode() not in protected + decrypted
        assert token_digest(token).encode() in decrypted
        assert not list(state_dir.glob("tls-*"))
        print("PASS authenticated queued status, temporary inbox, framing/schema validation and DPAPI storage")
        receiver.stop("pairingCode", *(json.dumps(code) for code in (code, rejected_code, legal_code)),
                      token, NO_TOKEN, BODY)
        pin, port = receiver.pin, receiver.address[1]

    with Console(home, address, port) as receiver:
        assert receiver.pin == pin and receiver.command("devices")[0]["status"] == "paired"
        assert receiver.command("inbox") == []
        expect_error(receiver.post("/v1/status", query, token), 404, "MESSAGE_NOT_FOUND")
        assert receiver.post("/v1/messages", message, token)[0] == 202
        other_code = receiver.command("pair")["pairingCode"]
        other = pairing_decision(receiver, pairing_request(other_code, device_id="other-phone"), "approve")
        other_query = {**query, "deviceId": other["deviceId"], "credentialId": other["credentialId"]}
        other_token = other["credential"]
        assert receiver.command(f"revoke {DEVICE}")["status"] == "revoked"
        for path, request in (("/v1/messages", message), ("/v1/status", query)):
            status, revoked = receiver.post(path, request, token)
            assert status == 403 and revoked["operation"] == "pair.revoked"
        expect_error(receiver.post("/v1/status", query, NO_TOKEN), 401, "AUTH_REQUIRED")
        expect_error(receiver.post("/v1/status", other_query, other_token), 404, "MESSAGE_NOT_FOUND")
        receiver.stop(token, other_token, json.dumps(other_code), NO_TOKEN, BODY)

    with Console(home, address, port) as receiver:
        assert receiver.command("devices")[0]["status"] == "revoked"
        assert receiver.post("/v1/status", query, token)[1]["operation"] == "pair.revoked"
        expect_error(receiver.post("/v1/status", other_query, other_token), 404, "MESSAGE_NOT_FOUND")
        receiver.stop(token, other_token, BODY)
    print("PASS stop closes port, restart retains pairing/revocation, logs exclude secrets and body")


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
    print("A.2 DESKTOP RECEIVER PASSED")


if __name__ == "__main__":
    main()
