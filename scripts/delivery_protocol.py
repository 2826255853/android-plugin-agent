"""Stage D.1 candidate delivery v1 validation. Callers supply authenticated context."""

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import Any

from message_protocol import (DEVICE_ID, PROJECT_ID, SHA256, UUID, ProtocolError,
                              _integer, _keys, _string, _timestamp, parse_json)
from plugin_package import MAX_ZIP_BYTES, VERSION


MAX_SEGMENT_BYTES = 256 * 1024
MAX_PAGE = 20
MAX_BUILD_ID = 2**53 - 1
MAX_OFFER_AGE = timedelta(hours=24)
COMMON = {"deliveryVersion", "type"}
TARGET = {"sourceFingerprint", "deviceId", "credentialId", "projectId"}
BUILD = {"taskId", "buildId", "packageSha256"}
CHECKS = {"static", "packaging", "isolatedBuild", "isolatedTest", "interactivePreview"}
ERRORS = {
    "INVALID_REQUEST", "UNSUPPORTED_SCHEMA_VERSION", "PAYLOAD_TOO_LARGE", "AUTH_REQUIRED",
    "DEVICE_REVOKED", "DELIVERY_NOT_FOUND", "DELIVERY_EXPIRED", "DELIVERY_REVOKED",
    "DELIVERY_BINDING_MISMATCH", "DELIVERY_RANGE_INVALID", "REQUEST_ID_CONFLICT",
    "BUILD_NOT_AVAILABLE", "RATE_LIMITED", "INTERNAL_ERROR",
}
FIELDS = {
    "delivery.receipt": TARGET | BUILD | {
        "deliveryId", "specVersion", "specSha256", "pluginId", "pluginName", "pluginVersion",
        "hostApi", "checks", "manifestSha256", "packageFile", "sizeBytes", "offeredAt", "expiresAt",
    },
    "delivery.publish": TARGET | BUILD | {"requestId"},
    "delivery.published": {"requestId", "receipt"},
    "delivery.query": {"requestId", "projectId", "after", "limit"},
    "delivery.page": {"requestId", "projectId", "after", "hasMore", "receipts"},
    "delivery.lookup": {"requestId", "projectId", "deliveryId"},
    "delivery.found": {"requestId", "receipt"},
    "delivery.download": {"requestId", "projectId", "deliveryId", "packageSha256", "offset", "length"},
    "delivery.revoke": {"requestId", "deliveryId"},
    "delivery.revoked": {"requestId", "deliveryId"},
    "delivery.error": {"requestId", "code"},
}
PATTERNS = {
    "requestId": UUID, "deliveryId": UUID, "credentialId": UUID, "deviceId": DEVICE_ID,
    "projectId": PROJECT_ID, "taskId": re.compile(r"task-[0-9a-f]{32}"),
    **dict.fromkeys(("sourceFingerprint", "specSha256", "manifestSha256", "packageSha256"), SHA256),
}


def require(condition: Any, code: str = "INVALID_REQUEST") -> None:
    if not condition:
        raise ProtocolError(code, "candidate-delivery")


def _object(value: Any) -> dict:
    require(isinstance(value, dict))
    return value


def _version(value: Any, field: str) -> tuple[tuple[int, str], ...]:
    text = _string(value, field, maximum=32)
    require(VERSION.fullmatch(text))
    return tuple((len(part), part) for part in text.split("."))


def _validate(value: dict) -> None:
    require(_integer(value.get("deliveryVersion"), "deliveryVersion", 0, 2**31 - 1) == 1,
            "UNSUPPORTED_SCHEMA_VERSION")
    kind = _string(value.get("type"), "type")
    require(kind in FIELDS)
    _keys(value, COMMON | FIELDS[kind])
    for key in PATTERNS.keys() & value.keys():
        require(PATTERNS[key].fullmatch(_string(value[key], key)))
    if "buildId" in value:
        _integer(value["buildId"], "buildId", 1, MAX_BUILD_ID)
    if "after" in value:
        require(value["after"] == "" or UUID.fullmatch(_string(value["after"], "after")))
    if kind == "delivery.receipt":
        _integer(value["specVersion"], "specVersion", 1, 2**31 - 1)
        _integer(value["sizeBytes"], "sizeBytes", 1, MAX_ZIP_BYTES)
        plugin_id = _string(value["pluginId"], "pluginId", maximum=128)
        require(re.fullmatch(r"[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+", plugin_id))
        name = _string(value["pluginName"], "pluginName")
        require(len(name.encode("utf-16-le")) <= 128 and all(ord(c) >= 32 and ord(c) != 127 for c in name))
        _version(value["pluginVersion"], "pluginVersion")
        require(value["packageFile"] == f"{plugin_id}-{value['pluginVersion']}.zip")
        api = _object(value["hostApi"])
        _keys(api, {"min", "max"})
        low, high = _version(api["min"], "min"), _version(api["max"], "max")
        require(low[0] == high[0] and low <= high)
        checks = _object(value["checks"])
        _keys(checks, CHECKS)
        require(all(result is True for result in checks.values()))
        age = _timestamp(value["expiresAt"], "expiresAt") - _timestamp(value["offeredAt"], "offeredAt")
        require(timedelta(0) < age <= MAX_OFFER_AGE)
    elif kind == "delivery.query":
        _integer(value["limit"], "limit", 1, MAX_PAGE)
    elif kind == "delivery.download":
        _integer(value["offset"], "offset", 0, MAX_ZIP_BYTES - 1)
        _integer(value["length"], "length", 1, MAX_SEGMENT_BYTES)
        require(value["offset"] + value["length"] <= MAX_ZIP_BYTES, "DELIVERY_RANGE_INVALID")
    elif kind in {"delivery.published", "delivery.found"}:
        _receipt(value["receipt"])
    elif kind == "delivery.page":
        receipts = value["receipts"]
        require(isinstance(receipts, list) and len(receipts) <= MAX_PAGE)
        require(type(value["hasMore"]) is bool and (receipts or not value["hasMore"]))
        previous = value["after"]
        for receipt in receipts:
            _receipt(receipt)
            require(receipt["projectId"] == value["projectId"] and receipt["deliveryId"] > previous)
            previous = receipt["deliveryId"]
    elif kind == "delivery.error":
        require(_string(value["code"], "code") in ERRORS)


def _receipt(value: Any) -> None:
    require(_object(value).get("type") == "delivery.receipt")
    _validate(value)


def parse(raw: str | bytes) -> dict:
    value = parse_json(raw)
    _validate(value)
    return value


@dataclass(frozen=True)
class Scope:
    source_fingerprint: str
    device_id: str
    credential_id: str
    project_id: str


def check_active_receipt(receipt: dict, scope: Scope, now: datetime) -> None:
    """Check a parsed receipt against the pinned connection and local project."""
    require(receipt["type"] == "delivery.receipt")
    require((receipt["sourceFingerprint"], receipt["deviceId"], receipt["credentialId"], receipt["projectId"])
            == (scope.source_fingerprint, scope.device_id, scope.credential_id, scope.project_id),
            "DELIVERY_BINDING_MISMATCH")
    require(now < _timestamp(receipt["expiresAt"], "expiresAt"), "DELIVERY_EXPIRED")


def check_page(page: dict, query: dict, scope: Scope, now: datetime) -> None:
    require(page["type"] == "delivery.page" and query["type"] == "delivery.query")
    require(page["projectId"] == scope.project_id
            and all(page[key] == query[key] for key in ("requestId", "projectId", "after")),
            "DELIVERY_BINDING_MISMATCH")
    count = len(page["receipts"])
    require(count <= query["limit"] and (not page["hasMore"] or count == query["limit"]))
    for receipt in page["receipts"]:
        check_active_receipt(receipt, scope, now)


def check_single_receipt(response: dict, request: dict, scope: Scope, now: datetime) -> None:
    kind = request["type"]
    require((kind, response["type"]) in {
        ("delivery.publish", "delivery.published"), ("delivery.lookup", "delivery.found"),
    })
    receipt = response["receipt"]
    keys = TARGET | BUILD if kind == "delivery.publish" else {"deliveryId", "projectId"}
    require(response["requestId"] == request["requestId"] and all(receipt[key] == request[key] for key in keys),
            "DELIVERY_BINDING_MISMATCH")
    check_active_receipt(receipt, scope, now)


def check_segment(request: dict, receipt: dict) -> None:
    """Check parsed inputs after pairing, availability and receipt checks."""
    require(request["type"] == "delivery.download" and receipt["type"] == "delivery.receipt")
    require(all(request[key] == receipt[key] for key in ("deliveryId", "projectId", "packageSha256")),
            "DELIVERY_BINDING_MISMATCH")
    require(request["offset"] + request["length"] <= receipt["sizeBytes"], "DELIVERY_RANGE_INVALID")


@dataclass(frozen=True)
class StateEvent:
    event_id: str
    actor: str
    action: str
    reason: str | None = None


PENDING = {"offered", "interrupted", "transferred", "verified"}
TRANSITIONS = {
    "publish": ("desktop", {"built"}, "offered"),
    "observe": ("android", {"unseen"}, "offered"),
    "disconnect": ("android", {"offered"}, "interrupted"),
    "resume": ("android", {"interrupted"}, "offered"),
    "receive_all": ("android", {"offered"}, "transferred"),
    "verify": ("android", {"transferred"}, "verified"),
    "commit_import": ("android", {"verified"}, "imported"),
    "reject": ("android", PENDING, "rejected"),
    "cancel": ("android", PENDING, "cancelled"),
    "expire": ("either", PENDING, "expired"),
    "revoke": ("either", PENDING, "revoked"),
}
REJECTIONS = {
    "package_hash_mismatch": {"transferred"},
    "plugin_identity_mismatch": {"transferred"},
    "incompatible_host_api": {"transferred"},
    "invalid_package": {"transferred"},
    "user_declined": {"verified"},
    "import_failed": {"verified"},
}


def next_state(current: str, event: StateEvent, prior: StateEvent | None = None) -> str:
    """Validate local events after the caller has established the action's evidence."""
    require(UUID.fullmatch(event.event_id) and event.action in TRANSITIONS, "INVALID_STATE")
    actor, allowed, following = TRANSITIONS[event.action]
    require(event.actor in {"desktop", "android"} and (actor == "either" or event.actor == actor),
            "INVALID_STATE")
    require(event.reason in REJECTIONS if event.action == "reject" else event.reason is None,
            "INVALID_STATE")
    if prior is not None:
        require(event == prior, "EVENT_ID_CONFLICT")
        return current
    require(current in allowed, "INVALID_STATE")
    if actor == "either" and event.actor == "desktop":
        require(current == "offered", "INVALID_STATE")
    if event.action == "reject":
        require(current in REJECTIONS[event.reason], "INVALID_STATE")
    return following
