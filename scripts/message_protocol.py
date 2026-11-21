"""Strict parser and contract checks for the phone-to-desktop protocol v1."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any


SCHEMA_VERSION = 1
MAX_REQUEST_BYTES = 128 * 1024
MAX_BODY_CHARS = 12_000
RETRY_WINDOW_SECONDS = 24 * 60 * 60
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$")
DEVICE_ID = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")
PAIRING_CODE = re.compile(r"^[0-9]{6}$")
CREDENTIAL = re.compile(r"^[A-Za-z0-9_-]{43,512}$")


class ProtocolError(ValueError):
    def __init__(self, code: str, reason: str = "invalid request") -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _duplicate_key_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("INVALID_REQUEST", "duplicate-json-key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ProtocolError("INVALID_REQUEST", f"non-standard-number:{value}")


def parse_json(raw: str | bytes) -> dict[str, Any]:
    if isinstance(raw, bytes):
        if len(raw) > MAX_REQUEST_BYTES:
            raise ProtocolError("PAYLOAD_TOO_LARGE", "request-bytes")
        if raw.startswith(b"\xef\xbb\xbf"):
            raise ProtocolError("INVALID_REQUEST", "bom")
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("INVALID_REQUEST", "utf8") from exc
    else:
        try:
            encoded_length = len(raw.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ProtocolError("INVALID_REQUEST", "utf8") from exc
        if encoded_length > MAX_REQUEST_BYTES:
            raise ProtocolError("PAYLOAD_TOO_LARGE", "request-bytes")
        if raw.startswith("\ufeff"):
            raise ProtocolError("INVALID_REQUEST", "bom")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_duplicate_key_object,
            parse_constant=_reject_constant,
        )
    except ProtocolError:
        raise
    except (ValueError, RecursionError) as exc:
        raise ProtocolError("INVALID_REQUEST", "json") from exc
    if not isinstance(value, dict):
        raise ProtocolError("INVALID_REQUEST", "root-type")
    return value


def _keys(value: dict[str, Any], required: set[str], optional: set[str] = set()) -> None:
    actual = set(value)
    if actual - required - optional:
        raise ProtocolError("INVALID_REQUEST", "unknown-field")
    if required - actual:
        raise ProtocolError("INVALID_REQUEST", "missing-field")


def _string(value: Any, field: str, *, maximum: int | None = None, non_empty: bool = True) -> str:
    if not isinstance(value, str) or (non_empty and not value):
        raise ProtocolError("INVALID_REQUEST", f"{field}-type")
    if maximum is not None and len(value) > maximum:
        raise ProtocolError("INVALID_REQUEST", f"{field}-length")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProtocolError("INVALID_REQUEST", f"{field}-utf8") from exc
    return value


def _integer(value: Any, field: str, minimum: int = 0, maximum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ProtocolError("INVALID_REQUEST", f"{field}-type")
    if maximum is not None and value > maximum:
        raise ProtocolError("INVALID_REQUEST", f"{field}-range")
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ProtocolError("INVALID_REQUEST", f"{field}-type")
    return value


def _uuid(value: Any, field: str) -> str:
    text = _string(value, field)
    if not UUID.fullmatch(text):
        raise ProtocolError("INVALID_REQUEST", f"{field}-format")
    return text


def _timestamp(value: Any, field: str) -> datetime:
    text = _string(value, field)
    if not TIMESTAMP.fullmatch(text):
        raise ProtocolError("INVALID_REQUEST", f"{field}-format")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProtocolError("INVALID_REQUEST", f"{field}-date") from exc


def _common(value: dict[str, Any], *, response: bool) -> tuple[str, datetime, datetime | None]:
    required = {"schemaVersion", "requestId", "createdAt", "operation"}
    if response:
        required.add("inReplyTo")
    if required - set(value):
        raise ProtocolError("INVALID_REQUEST", "missing-field")
    if value["schemaVersion"] != SCHEMA_VERSION:
        raise ProtocolError("UNSUPPORTED_SCHEMA_VERSION", "schema-version")
    request_id = _uuid(value["requestId"], "requestId")
    created_at = _timestamp(value["createdAt"], "createdAt")
    if response:
        _uuid(value["inReplyTo"], "inReplyTo")
    operation = _string(value["operation"], "operation")
    return request_id, created_at, None


def _device(value: Any) -> str:
    text = _string(value, "deviceId")
    if not DEVICE_ID.fullmatch(text):
        raise ProtocolError("INVALID_REQUEST", "device-id-format")
    return text


def _credential_id(value: Any) -> str:
    return _uuid(value, "credentialId")


def _sha(value: Any, field: str) -> str:
    text = _string(value, field)
    if not SHA256.fullmatch(text):
        raise ProtocolError("INVALID_REQUEST", f"{field}-format")
    return text


def _validate_pair_request(value: dict[str, Any]) -> None:
    _keys(value, {"schemaVersion", "requestId", "createdAt", "operation", "deviceId", "deviceName", "clientKeyFingerprint", "pairingCode", "expiresAt"})
    _, created_at, _ = _common(value, response=False)
    if value["operation"] != "pair.request":
        raise ProtocolError("INVALID_REQUEST", "operation")
    _device(value["deviceId"])
    name = _string(value["deviceName"], "deviceName", maximum=64)
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in name):
        raise ProtocolError("INVALID_REQUEST", "device-name-control")
    _sha(value["clientKeyFingerprint"], "clientKeyFingerprint")
    if not PAIRING_CODE.fullmatch(_string(value["pairingCode"], "pairingCode")):
        raise ProtocolError("INVALID_REQUEST", "pairing-code-format")
    expires_at = _timestamp(value["expiresAt"], "expiresAt")
    if expires_at <= created_at:
        raise ProtocolError("INVALID_REQUEST", "pairing-expiry")
    if (expires_at - created_at).total_seconds() > 600:
        raise ProtocolError("INVALID_REQUEST", "pairing-expiry-window")


def _validate_pair_confirm(value: dict[str, Any]) -> None:
    _keys(value, {"schemaVersion", "requestId", "inReplyTo", "createdAt", "operation", "deviceId", "approved"}, {"serverCertificateFingerprint", "credentialId", "credential", "credentialExpiresAt", "errorCode"})
    _, created_at, _ = _common(value, response=True)
    if value["operation"] != "pair.confirm":
        raise ProtocolError("INVALID_REQUEST", "operation")
    _device(value["deviceId"])
    approved = _boolean(value["approved"], "approved")
    if approved:
        _keys(value, {"schemaVersion", "requestId", "inReplyTo", "createdAt", "operation", "deviceId", "approved", "serverCertificateFingerprint", "credentialId", "credential", "credentialExpiresAt"})
        _sha(value["serverCertificateFingerprint"], "serverCertificateFingerprint")
        _credential_id(value["credentialId"])
        credential = _string(value["credential"], "credential")
        if not CREDENTIAL.fullmatch(credential):
            raise ProtocolError("INVALID_REQUEST", "credential-format")
        expires_at = _timestamp(value["credentialExpiresAt"], "credentialExpiresAt")
        if expires_at <= created_at:
            raise ProtocolError("INVALID_REQUEST", "credential-expiry")
    else:
        _keys(value, {"schemaVersion", "requestId", "inReplyTo", "createdAt", "operation", "deviceId", "approved", "errorCode"})
        if value["errorCode"] not in ("PAIRING_EXPIRED", "PAIRING_REJECTED", "SERVER_IDENTITY_MISMATCH", "PAIRING_RATE_LIMITED"):
            raise ProtocolError("INVALID_REQUEST", "pair-error-code")


def _validate_pair_revoke(value: dict[str, Any]) -> None:
    _keys(value, {"schemaVersion", "requestId", "createdAt", "operation", "deviceId", "credentialId", "reason"})
    _common(value, response=False)
    if value["operation"] != "pair.revoke":
        raise ProtocolError("INVALID_REQUEST", "operation")
    _device(value["deviceId"])
    _credential_id(value["credentialId"])
    if value["reason"] not in ("user_requested", "credential_compromised", "device_removed"):
        raise ProtocolError("INVALID_REQUEST", "revoke-reason")


def _validate_pair_revoked(value: dict[str, Any]) -> None:
    _keys(value, {"schemaVersion", "requestId", "inReplyTo", "createdAt", "operation", "deviceId", "credentialId", "revokedAt"})
    _common(value, response=True)
    if value["operation"] != "pair.revoked":
        raise ProtocolError("INVALID_REQUEST", "operation")
    _device(value["deviceId"])
    _credential_id(value["credentialId"])
    _timestamp(value["revokedAt"], "revokedAt")


def _validate_confirmation(value: Any) -> None:
    if not isinstance(value, dict):
        raise ProtocolError("INVALID_REQUEST", "confirmation-type")
    _keys(value, {"specVersion", "decision", "confirmedAt"})
    _integer(value["specVersion"], "specVersion", minimum=1)
    if value["decision"] not in ("confirmed", "rejected"):
        raise ProtocolError("INVALID_REQUEST", "confirmation-decision")
    _timestamp(value["confirmedAt"], "confirmedAt")


def _validate_message_send(value: dict[str, Any]) -> None:
    optional = {"specVersion", "confirmation"}
    _keys(value, {"schemaVersion", "requestId", "createdAt", "operation", "deviceId", "credentialId", "messageId", "projectId", "sequence", "messageType", "body", "bodySha256"}, optional)
    _common(value, response=False)
    if value["operation"] != "message.send":
        raise ProtocolError("INVALID_REQUEST", "operation")
    _device(value["deviceId"])
    _credential_id(value["credentialId"])
    _uuid(value["messageId"], "messageId")
    project_id = _string(value["projectId"], "projectId")
    if not PROJECT_ID.fullmatch(project_id):
        raise ProtocolError("INVALID_REQUEST", "project-id-format")
    _integer(value["sequence"], "sequence", minimum=1, maximum=2_147_483_647)
    if value["messageType"] not in ("user.message", "user.answer", "user.confirmation"):
        raise ProtocolError("INVALID_REQUEST", "message-type")
    body = _string(value["body"], "body", maximum=MAX_BODY_CHARS)
    digest = _sha(value["bodySha256"], "bodySha256")
    if hashlib.sha256(body.encode("utf-8")).hexdigest() != digest:
        raise ProtocolError("INVALID_REQUEST", "body-hash")
    if "specVersion" in value:
        _integer(value["specVersion"], "specVersion", minimum=1)
    if "confirmation" in value:
        _validate_confirmation(value["confirmation"])


def _validate_message_receipt(value: dict[str, Any]) -> None:
    _keys(value, {"schemaVersion", "requestId", "inReplyTo", "createdAt", "operation", "deviceId", "messageId", "projectId", "sequence", "status", "receivedAt", "retryable"})
    _common(value, response=True)
    if value["operation"] != "message.receipt":
        raise ProtocolError("INVALID_REQUEST", "operation")
    _device(value["deviceId"])
    _uuid(value["messageId"], "messageId")
    project_id = _string(value["projectId"], "projectId")
    if not PROJECT_ID.fullmatch(project_id):
        raise ProtocolError("INVALID_REQUEST", "project-id-format")
    _integer(value["sequence"], "sequence", minimum=1, maximum=2_147_483_647)
    if value["status"] not in ("received", "duplicate") or value["retryable"] is not False:
        raise ProtocolError("INVALID_REQUEST", "receipt-status")
    _timestamp(value["receivedAt"], "receivedAt")


def _validate_status_query(value: dict[str, Any]) -> None:
    _keys(value, {"schemaVersion", "requestId", "createdAt", "operation", "deviceId", "credentialId", "messageId"})
    _common(value, response=False)
    if value["operation"] != "status.query":
        raise ProtocolError("INVALID_REQUEST", "operation")
    _device(value["deviceId"])
    _credential_id(value["credentialId"])
    _uuid(value["messageId"], "messageId")


def _validate_status_response(value: dict[str, Any]) -> None:
    _keys(value, {"schemaVersion", "requestId", "inReplyTo", "createdAt", "operation", "deviceId", "messageId", "projectId", "sequence", "status", "updatedAt", "retryable"})
    _common(value, response=True)
    if value["operation"] != "status.response":
        raise ProtocolError("INVALID_REQUEST", "operation")
    _device(value["deviceId"])
    _uuid(value["messageId"], "messageId")
    project_id = _string(value["projectId"], "projectId")
    if not PROJECT_ID.fullmatch(project_id):
        raise ProtocolError("INVALID_REQUEST", "project-id-format")
    _integer(value["sequence"], "sequence", minimum=1, maximum=2_147_483_647)
    if value["status"] not in ("queued", "received", "failed_retryable", "rejected", "revoked"):
        raise ProtocolError("INVALID_REQUEST", "message-status")
    _timestamp(value["updatedAt"], "updatedAt")
    _boolean(value["retryable"], "retryable")


ERROR_CODES = {
    "INVALID_REQUEST", "UNSUPPORTED_SCHEMA_VERSION", "AUTH_REQUIRED", "PAIRING_REQUIRED", "PAIRING_EXPIRED",
    "PAIRING_REJECTED", "SERVER_IDENTITY_MISMATCH", "PAIRING_RATE_LIMITED", "DEVICE_REVOKED",
    "MESSAGE_ID_CONFLICT", "MESSAGE_SEQUENCE_CONFLICT", "MESSAGE_NOT_FOUND", "PAYLOAD_TOO_LARGE",
    "RATE_LIMITED", "RETRY_WINDOW_EXPIRED", "INTERNAL_ERROR",
}


def _validate_error(value: dict[str, Any]) -> None:
    _keys(value, {"schemaVersion", "requestId", "inReplyTo", "createdAt", "operation", "error"})
    _common(value, response=True)
    if value["operation"] != "error" or not isinstance(value["error"], dict):
        raise ProtocolError("INVALID_REQUEST", "error-object")
    error = value["error"]
    _keys(error, {"code", "retryable"}, {"retryAfterSeconds"})
    if _string(error["code"], "error-code") not in ERROR_CODES:
        raise ProtocolError("INVALID_REQUEST", "error-code")
    retryable = _boolean(error["retryable"], "retryable")
    if "retryAfterSeconds" in error:
        if not retryable:
            raise ProtocolError("INVALID_REQUEST", "retry-after-without-retry")
        _integer(error["retryAfterSeconds"], "retryAfterSeconds", minimum=1, maximum=86400)


def validate_envelope(value: dict[str, Any]) -> dict[str, Any]:
    if "schemaVersion" not in value:
        raise ProtocolError("INVALID_REQUEST", "missing-schema-version")
    if _integer(value["schemaVersion"], "schemaVersion", minimum=-2_147_483_648) != SCHEMA_VERSION:
        raise ProtocolError("UNSUPPORTED_SCHEMA_VERSION", "schema-version")
    operation = _string(value.get("operation"), "operation")
    validators = {
        "pair.request": _validate_pair_request,
        "pair.confirm": _validate_pair_confirm,
        "pair.revoke": _validate_pair_revoke,
        "pair.revoked": _validate_pair_revoked,
        "message.send": _validate_message_send,
        "message.receipt": _validate_message_receipt,
        "status.query": _validate_status_query,
        "status.response": _validate_status_response,
        "error": _validate_error,
    }
    validator = validators.get(operation)
    if validator is None:
        raise ProtocolError("INVALID_REQUEST", "operation")
    validator(value)
    return value


def parse_envelope(raw: str | bytes) -> dict[str, Any]:
    return validate_envelope(parse_json(raw))
