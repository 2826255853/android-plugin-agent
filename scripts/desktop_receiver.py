"""Start the local HTTPS receiver and operate it from its desktop console."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import secrets
import select
import socket
import socketserver
import sqlite3
import ssl
import sys
import threading
from collections import deque
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from time import monotonic
from uuid import uuid4

from message_protocol import (
    CREDENTIAL, DEVICE_ID, MAX_REQUEST_BYTES, RETRY_WINDOW_SECONDS, SCHEMA_VERSION,
    UUID, ProtocolError, parse_json, validate_envelope,
)
from dialogue_coordinator import DialogueCoordinator
from dialogue_protocol import strict_object, validate as validate_dialogue
from receiver_inbox import Inbox
from receiver_storage import ReceiverStorage, StorageError
from spec_workflow import SPEC_DIRECTORY


PAIRING_DURATION = timedelta(minutes=10)
DEVICE_LIFETIME = timedelta(days=90)
PAIRING_RATE_WINDOW = 60
PAIRING_ATTEMPTS = 10
MAX_PENDING = 8
MAX_CONNECTIONS = 16
IO_TIMEOUT = 5
REQUEST_TIMEOUT = 10
ROUTES = {"/v1/pair": "pair.request", "/v1/messages": "message.send", "/v1/status": "status.query"}
DIALOGUE_ROUTES = {
    "/v1/dialogue/pull": "dialogue.pull",
    "/v1/dialogue/ack": "dialogue.ack",
    "/v1/dialogue/submit": "phone.",
}
HTTP_ERRORS = {
    "AUTH_REQUIRED": 401, "DEVICE_REVOKED": 403, "PAIRING_REQUIRED": 403,
    "PAIRING_EXPIRED": 410, "MESSAGE_NOT_FOUND": 404, "MESSAGE_ID_CONFLICT": 409,
    "MESSAGE_SEQUENCE_CONFLICT": 409, "PAYLOAD_TOO_LARGE": 413,
    "PAIRING_RATE_LIMITED": 429, "RATE_LIMITED": 429, "INTERNAL_ERROR": 503,
    "UNSUPPORTED_SCHEMA_VERSION": 400, "SESSION_NOT_FOUND": 404, "CURSOR_EXPIRED": 409,
    "INVALID_CURSOR": 409, "EVENT_ID_CONFLICT": 409, "EVENT_SEQUENCE_CONFLICT": 409,
    "STALE_EVENT": 409, "EXPIRED_EVENT": 409, "SPEC_MISMATCH": 409, "INVALID_STATE": 409,
}


def now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def envelope(operation: str, **fields) -> dict:
    return {"schemaVersion": SCHEMA_VERSION, "requestId": str(uuid4()),
            "createdAt": timestamp(now()), "operation": operation, **fields}


def response(operation: str, request_id: str | None, **fields) -> dict:
    return validate_envelope(envelope(operation, inReplyTo=request_id or str(uuid4()), **fields))


def audit(message_id: str | None, **event: str) -> None:
    if message_id is not None:
        event["messageId"] = message_id
    print(json.dumps({"at": timestamp(now()), **event}), file=sys.stderr, flush=True)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def lan_address(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise argparse.ArgumentTypeError("select a specific local IPv4 address") from exc
    networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")
    if address == ipaddress.IPv4Address("127.0.0.1") or any(
        address in ipaddress.IPv4Network(network) for network in networks
    ):
        return str(address)
    raise argparse.ArgumentTypeError("select a private/link-local LAN address or 127.0.0.1 for local testing")


@dataclass
class PendingPair:
    request_id: str
    device_id: str
    name: str
    client_fingerprint: str
    expires_at: datetime
    decided: threading.Event = field(default_factory=threading.Event)
    approved: bool = False

    def details(self) -> dict:
        return {"requestId": self.request_id, "deviceId": self.device_id, "deviceName": self.name,
                "clientKeyFingerprint": self.client_fingerprint, "expiresAt": timestamp(self.expires_at)}


class Receiver:
    def __init__(self, storage: ReceiverStorage, fingerprint: str, inbox: Inbox,
                 *, spec_store: Path = SPEC_DIRECTORY / "projects"):
        self.storage = storage
        self.fingerprint = fingerprint
        self.message_store = inbox
        self.dialogue = DialogueCoordinator(inbox, spec_store=spec_store)
        self.devices = storage.load_devices()
        self.lock = threading.Lock()
        self.code: tuple[str, datetime] | None = None
        self.attempts: deque[float] = deque()
        self.pending: dict[str, PendingPair] = {}
        self.stopped = False

    def open_pairing(self) -> dict:
        with self.lock:
            code = f"{secrets.randbelow(1_000_000):06d}"
            expires_at = now() + PAIRING_DURATION
            self.code = code, expires_at
            return {"pairingCode": code, "expiresAt": timestamp(expires_at),
                    "serverCertificateFingerprint": self.fingerprint}

    def count_pairing_attempt(self) -> None:
        with self.lock:
            cutoff = monotonic() - PAIRING_RATE_WINDOW
            while self.attempts and self.attempts[0] <= cutoff:
                self.attempts.popleft()
            if len(self.attempts) >= PAIRING_ATTEMPTS:
                raise ProtocolError("PAIRING_RATE_LIMITED")
            self.attempts.append(monotonic())

    def begin_pairing(self, request: dict) -> PendingPair:
        with self.lock:
            current = now()
            expires_at = date(request["expiresAt"])
            if expires_at <= current or (self.code and self.code[1] <= current):
                raise ProtocolError("PAIRING_EXPIRED")
            if self.code is None or not secrets.compare_digest(self.code[0], request["pairingCode"]):
                raise ProtocolError("PAIRING_REQUIRED")
            request_id = request["requestId"]
            if request_id in self.pending:
                raise ProtocolError("INVALID_REQUEST")
            if len(self.pending) >= MAX_PENDING:
                raise ProtocolError("PAIRING_RATE_LIMITED")
            pending = PendingPair(request_id, request["deviceId"], request["deviceName"],
                                  request["clientKeyFingerprint"], min(expires_at, self.code[1]))
            self.code = None
            self.pending[request_id] = pending
            return pending

    def decide(self, request_id: str, approved: bool) -> None:
        with self.lock:
            pending = self.pending.get(request_id)
            if pending is None or pending.decided.is_set() or pending.expires_at <= now():
                raise ProtocolError("PAIRING_EXPIRED")
            pending.approved = approved
            pending.decided.set()

    def finish_pairing(self, pending: PendingPair) -> dict:
        with self.lock:
            if self.stopped:
                raise ProtocolError("INTERNAL_ERROR")
            fields = {"deviceId": pending.device_id}
            if pending.expires_at <= now():
                return response("pair.confirm", pending.request_id, approved=False,
                                errorCode="PAIRING_EXPIRED", **fields)
            if not pending.approved:
                return response("pair.confirm", pending.request_id, approved=False,
                                errorCode="PAIRING_REJECTED", **fields)
            credential_id = str(uuid4())
            credential = secrets.token_urlsafe(32)
            expiry = timestamp(now() + DEVICE_LIFETIME)
            updated = self.revoked_devices(pending.device_id)
            updated[credential_id] = {
                **fields, "deviceName": pending.name, "clientKeyFingerprint": pending.client_fingerprint,
                "credentialDigest": token_digest(credential), "credentialExpiresAt": expiry,
            }
            self.storage.save_devices(updated)
            self.devices = updated
            return response("pair.confirm", pending.request_id, approved=True, credential=credential,
                            credentialId=credential_id, credentialExpiresAt=expiry,
                            serverCertificateFingerprint=self.fingerprint, **fields)

    def wait_for_pairing(self, request: dict, connection: ssl.SSLSocket) -> dict:
        pending = self.begin_pairing(request)
        try:
            while pending.expires_at > now():
                if pending.decided.wait(min(0.5, max(0, (pending.expires_at - now()).total_seconds()))):
                    break
                # One HTTP request per TLS connection. Incoming data or EOF abandons this wait.
                if connection.pending() or select.select([connection], [], [], 0)[0]:
                    raise ConnectionAbortedError()
            return self.finish_pairing(pending)
        finally:
            with self.lock:
                del self.pending[pending.request_id]

    def revoked_devices(self, device_id: str) -> dict:
        revoked_at = timestamp(now())
        return {
            credential_id: {**record, "revokedAt": revoked_at}
            if record["deviceId"] == device_id and "revokedAt" not in record else record
            for credential_id, record in self.devices.items()
        }

    def revoke(self, device_id: str, reason: str) -> None:
        with self.lock:
            if not DEVICE_ID.fullmatch(device_id):
                raise ProtocolError("INVALID_REQUEST")
            active = next((key for key, record in self.devices.items()
                          if record["deviceId"] == device_id and "revokedAt" not in record), None)
            if active is None:
                raise ProtocolError("PAIRING_REQUIRED")
            request = validate_envelope(envelope("pair.revoke", deviceId=device_id, credentialId=active, reason=reason))
            updated = self.revoked_devices(request["deviceId"])
            self.storage.save_devices(updated)
            self.devices = updated

    def message(self, request: dict, token: str) -> tuple[int, dict]:
        with self.lock:
            device_id, credential_id = request["deviceId"], request["credentialId"]
            message_id = request["messageId"]
            record = self.devices.get(credential_id)
            if (record is None or record["deviceId"] != device_id
                    or not secrets.compare_digest(record["credentialDigest"], token_digest(token))):
                raise ProtocolError("AUTH_REQUIRED")
            if "revokedAt" in record:
                audit(message_id, error="DEVICE_REVOKED")
                return 403, response("pair.revoked", request["requestId"], deviceId=device_id,
                                     credentialId=credential_id, revokedAt=record["revokedAt"])
            if date(record["credentialExpiresAt"]) <= now():
                raise ProtocolError("PAIRING_EXPIRED")
            if self.stopped:
                raise ProtocolError("INTERNAL_ERROR")
            if request["operation"] == "message.send":
                received_at = now()
                retry_cutoff = received_at - timedelta(seconds=RETRY_WINDOW_SECONDS)
                if date(request["createdAt"]) < retry_cutoff:
                    raise ProtocolError("RETRY_WINDOW_EXPIRED")
                message, inserted = self.message_store.save(request, timestamp(received_at), retry_cutoff)
                self.dialogue.ingest(message)
                audit(message_id, status=message["status"] if inserted else "duplicate")
                return 200, response(
                    "message.receipt", request["requestId"], requestId=message["receipt_id"],
                    createdAt=message["received_at"], receivedAt=message["received_at"],
                    deviceId=device_id, messageId=message_id, projectId=message["project_id"],
                    sequence=message["sequence"], status=message["status"], retryable=False,
                )
            message = self.message_store.get(device_id, message_id)
            if message is None:
                raise ProtocolError("MESSAGE_NOT_FOUND")
            return 200, response(
                "status.response", request["requestId"], deviceId=device_id, messageId=message_id,
                projectId=message["project_id"], sequence=message["sequence"],
                status=message["status"], updatedAt=message["received_at"], retryable=False,
            )

    def dialogue_request(self, request: dict, token: str, credential_id: str) -> tuple[int, dict]:
        with self.lock:
            device_id = request["deviceId"]
            record = self.devices.get(credential_id)
            if (record is None or record["deviceId"] != device_id
                    or not secrets.compare_digest(record["credentialDigest"], token_digest(token))):
                raise ProtocolError("AUTH_REQUIRED")
            if "revokedAt" in record:
                raise ProtocolError("DEVICE_REVOKED")
            if date(record["credentialExpiresAt"]) <= now():
                raise ProtocolError("AUTH_REQUIRED")
            if self.stopped:
                raise ProtocolError("INTERNAL_ERROR")

            kind = request["type"]
            if kind == "dialogue.pull":
                return 200, self.dialogue.pull(request)
            if kind == "dialogue.ack":
                return 200, self.dialogue.acknowledge(request)
            return 200, self.dialogue.submit(request)

    @staticmethod
    def dialogue_error(request: dict | None, code: str) -> dict:
        request = request or {}
        device_id = request.get("deviceId")
        project_id = request.get("projectId")
        session_id = request.get("sessionId")
        if not isinstance(device_id, str) or not DEVICE_ID.fullmatch(device_id):
            device_id = "unknown-device"
        if not isinstance(project_id, str) or not DEVICE_ID.fullmatch(project_id):
            project_id = "unknown-project"
        if not isinstance(session_id, str) or not UUID.fullmatch(session_id):
            session_id = str(uuid4())
        return {
            "dialogueVersion": 1, "deviceId": device_id, "projectId": project_id,
            "sessionId": session_id, "type": "dialogue.error", "code": code,
        }

    def list_pending(self) -> list[dict]:
        with self.lock:
            return [item.details() for item in self.pending.values()
                    if not item.decided.is_set() and item.expires_at > now()]

    def list_devices(self) -> list[dict]:
        with self.lock:
            latest = {item["deviceId"]: item for item in self.devices.values()}
            return [
                {**{key: record[key] for key in ("deviceId", "deviceName", "credentialExpiresAt")},
                 "status": "revoked" if "revokedAt" in record else
                 "expired" if date(record["credentialExpiresAt"]) <= now() else "paired"}
                for record in latest.values()
            ]

    def inbox(self) -> list[dict]:
        with self.lock:
            return self.message_store.list()

    def clear_inbox(self) -> int:
        with self.lock:
            return self.message_store.clear()

    def stop(self) -> None:
        with self.lock:
            self.stopped = True
            self.code = None
            for pending in self.pending.values():
                pending.decided.set()

    def expire_pairing_code(self) -> None:
        with self.lock:
            if self.code and self.code[1] <= now():
                self.code = None


class ReceiverServer(socketserver.ThreadingMixIn, HTTPServer):
    allow_reuse_address = False
    request_queue_size = MAX_CONNECTIONS

    def __init__(self, address: tuple[str, int], context: ssl.SSLContext, receiver: Receiver):
        self.context = context
        self.receiver = receiver
        self.connections: set[ssl.SSLSocket] = set()
        self.connections_lock = threading.Lock()
        super().__init__(address, ReceiverHandler)

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        socketserver.TCPServer.server_bind(self)

    def service_actions(self) -> None:
        self.receiver.expire_pairing_code()

    def process_request(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        with self.connections_lock:
            if len(self.connections) >= MAX_CONNECTIONS:
                request.close()
                return
            request.settimeout(IO_TIMEOUT)
            connection = self.context.wrap_socket(request, server_side=True, do_handshake_on_connect=False)
            self.connections.add(connection)
        try:
            super().process_request(connection, client_address)
        except Exception:
            self.shutdown_request(connection)
            raise

    def finish_request(self, request: ssl.SSLSocket, client_address: tuple[str, int]) -> None:
        try:
            request.do_handshake()
            super().finish_request(request, client_address)
        except OSError:
            pass

    def shutdown_request(self, request: ssl.SSLSocket) -> None:
        super().shutdown_request(request)
        with self.connections_lock:
            self.connections.discard(request)

    def handle_error(self, request, client_address) -> None:
        print("receiver: INTERNAL_ERROR", file=sys.stderr)

    def server_close(self) -> None:
        self.receiver.stop()
        with self.connections_lock:
            for connection in self.connections:
                close_connection(connection)
        super().server_close()
        self.receiver.dialogue.stop()


def close_connection(connection: socket.socket) -> None:
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class ReceiverHandler(BaseHTTPRequestHandler):
    server: ReceiverServer
    protocol_version = "HTTP/1.1"

    def handle(self) -> None:
        self.deadline = threading.Timer(REQUEST_TIMEOUT, close_connection, (self.request,))
        self.deadline.start()
        try:
            self.close_connection = True
            self.handle_one_request()
        finally:
            self.deadline.cancel()

    def handle_expect_100(self) -> bool:
        self.send_error(417)
        return False

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self.respond(code, response("error", None, error={"code": "INVALID_REQUEST", "retryable": False}))

    def log_message(self, format: str, *args) -> None:
        # BaseHTTPRequestHandler would log the untrusted request line and remote address.
        pass

    def read_request_body(self) -> bytes:
        lengths = self.headers.get_all("Content-Length", [])
        if (self.headers.get_all("Transfer-Encoding") or len(lengths) != 1
                or not re.fullmatch(r"[0-9]{1,10}", lengths[0])):
            raise ProtocolError("INVALID_REQUEST")
        length = int(lengths[0])
        if length > MAX_REQUEST_BYTES:
            raise ProtocolError("PAYLOAD_TOO_LARGE")
        content_types = self.headers.get_all("Content-Type", [])
        if (len(content_types) != 1 or not re.fullmatch(
            r'application/json(?:;\s*charset=(?:utf-8|"utf-8"))?', content_types[0], re.IGNORECASE
        ) or self.headers.get_all("Content-Encoding")):
            raise ProtocolError("INVALID_REQUEST")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ProtocolError("INVALID_REQUEST")
        self.deadline.cancel()
        return body

    def do_POST(self) -> None:
        request_id = None
        message_id = None
        operation = ROUTES.get(self.path)
        dialogue_operation = DIALOGUE_ROUTES.get(self.path)
        receiver = self.server.receiver
        if operation is None and dialogue_operation is None:
            self.send_error(404)
            return
        if dialogue_operation is not None:
            request = None
            try:
                request = strict_object(self.read_request_body())
                validate_dialogue(request)
                kind = request["type"]
                if kind != dialogue_operation and not (dialogue_operation == "phone." and kind.startswith("phone.")):
                    raise ProtocolError("INVALID_REQUEST")
                authorization = self.headers.get_all("Authorization", [])
                credential_ids = self.headers.get_all("X-Credential-Id", [])
                if (len(authorization) != 1 or not authorization[0].lower().startswith("bearer ")
                        or len(credential_ids) != 1 or not UUID.fullmatch(credential_ids[0])):
                    raise ProtocolError("AUTH_REQUIRED")
                token = authorization[0][7:]
                if not CREDENTIAL.fullmatch(token):
                    raise ProtocolError("AUTH_REQUIRED")
                status, payload = receiver.dialogue_request(request, token, credential_ids[0])
            except ProtocolError as error:
                self.dialogue_failure(request, error.code)
                return
            except (TimeoutError, ConnectionError, ssl.SSLError):
                return
            except (OSError, sqlite3.Error):
                self.dialogue_failure(request, "INTERNAL_ERROR")
                return
            self.respond(status, payload)
            return
        try:
            if operation == "pair.request":
                receiver.count_pairing_attempt()
            request = parse_json(self.read_request_body())
            candidate_id = request.get("requestId")
            if isinstance(candidate_id, str) and UUID.fullmatch(candidate_id):
                request_id = candidate_id
            candidate_message_id = request.get("messageId")
            if operation != "pair.request" and isinstance(candidate_message_id, str) and UUID.fullmatch(candidate_message_id):
                message_id = candidate_message_id
            validate_envelope(request)
            if request["operation"] != operation:
                raise ProtocolError("INVALID_REQUEST")
            authorization = self.headers.get_all("Authorization", [])
            if operation == "pair.request":
                if authorization:
                    raise ProtocolError("INVALID_REQUEST")
                payload = receiver.wait_for_pairing(request, self.request)
                status = 200
            else:
                if len(authorization) != 1 or not authorization[0].lower().startswith("bearer "):
                    raise ProtocolError("AUTH_REQUIRED")
                token = authorization[0][7:]
                if not CREDENTIAL.fullmatch(token):
                    raise ProtocolError("AUTH_REQUIRED")
                status, payload = receiver.message(request, token)
        except ProtocolError as error:
            self.failure(request_id, error.code, message_id)
            return
        except (TimeoutError, ConnectionError, ssl.SSLError):
            return
        except (OSError, sqlite3.Error):
            self.failure(request_id, "INTERNAL_ERROR", message_id)
            return
        self.respond(status, payload)

    def failure(self, request_id: str | None, code: str, message_id: str | None) -> None:
        audit(message_id, error=code)
        retryable = code in {"PAIRING_RATE_LIMITED", "INTERNAL_ERROR"}
        error = {"code": code, "retryable": retryable}
        if code == "PAIRING_RATE_LIMITED":
            error["retryAfterSeconds"] = PAIRING_RATE_WINDOW
        self.respond(HTTP_ERRORS.get(code, 400), response("error", request_id, error=error))

    def dialogue_failure(self, request: dict | None, code: str) -> None:
        status = HTTP_ERRORS.get(code, 400)
        self.respond(status, self.server.receiver.dialogue_error(request, code))

    def respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.close_connection = True
        self.send_response_only(status)
        self.send_header("Connection", "close")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def display(value: dict | list) -> None:
    line = json.dumps(value, ensure_ascii=False)
    # Preserve readable Unicode while escaping terminal controls and invisible formatting characters.
    print("".join(char if char.isprintable() else json.dumps(char)[1:-1] for char in line), flush=True)


def console(server: ReceiverServer) -> None:
    receiver = server.receiver
    host, port = server.server_address
    status = {"state": "listening", "address": f"https://{host}:{port}",
              "serverCertificateFingerprint": receiver.fingerprint}
    display(status)
    for line in sys.stdin:
        words = line.split()
        try:
            match words:
                case ["status"]:
                    display(status)
                case ["pair"]:
                    display(receiver.open_pairing())
                case ["pending"]:
                    display(receiver.list_pending())
                case ["approve" | "reject" as action, request_id]:
                    receiver.decide(request_id, approved=action == "approve")
                    display({"decision": action, "requestId": request_id})
                case ["devices"]:
                    display(receiver.list_devices())
                case ["revoke", device_id, *reason] if len(reason) <= 1:
                    receiver.revoke(device_id, reason[0] if reason else "user_requested")
                    display({"deviceId": device_id, "status": "revoked"})
                case ["inbox"]:
                    display(receiver.inbox())
                case ["inbox", "clear", "CONFIRM"]:
                    display({"cleared": receiver.clear_inbox()})
                case ["stop"]:
                    return
                case ["help"]:
                    display({"commands": ["status", "pair", "pending", "approve REQUEST_ID",
                                          "reject REQUEST_ID", "devices", "revoke DEVICE_ID [REASON]",
                                          "inbox", "inbox clear CONFIRM", "stop"]})
                case []:
                    continue
                case _:
                    display({"error": "UNKNOWN_COMMAND"})
        except ProtocolError as error:
            display({"error": error.code})
        except (OSError, sqlite3.Error):
            display({"error": "INTERNAL_ERROR"})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start"])
    parser.add_argument("--bind", required=True, type=lan_address, help="explicit local IPv4 LAN address")
    parser.add_argument("--port", type=int, default=8443, metavar="PORT")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        storage = ReceiverStorage()
        with storage.session():
            context, fingerprint = storage.tls_identity()
            database_path = storage.root / "inbox.sqlite3"
            with closing(sqlite3.connect(
                database_path, isolation_level="IMMEDIATE", check_same_thread=False,
                autocommit=sqlite3.LEGACY_TRANSACTION_CONTROL,
            )) as database, ReceiverServer(
                (args.bind, args.port), context, Receiver(storage, fingerprint, Inbox(database))
            ) as server:
                server.receiver.dialogue.start()
                worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1})
                worker.start()
                try:
                    console(server)
                except KeyboardInterrupt:
                    pass
                finally:
                    server.shutdown()
                    worker.join()
        display({"state": "stopped"})
        return 0
    except StorageError as error:
        print(f"receiver: {error}", file=sys.stderr)
    except (OSError, ValueError, sqlite3.Error):
        print("receiver: STARTUP_FAILED", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
