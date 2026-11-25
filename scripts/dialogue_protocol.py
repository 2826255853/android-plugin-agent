"""Wire validation and submission preconditions for dialogue v1."""

from datetime import timedelta
from hashlib import sha256
from math import isfinite

from message_protocol import (
    PROJECT_ID, ProtocolError, _device, _integer, _keys, _sha, _string, _timestamp, _uuid, parse_json,
)


SCOPE = {"deviceId", "projectId", "sessionId"}
SPEC = {"specVersion", "specSha256"}
LINK = {"inReplyTo"}
EXPIRY = {"expiresAt"}
EVENT = {"eventId", "sequence", "createdAt", "messageId"}
MAX_SEQUENCE = 2_147_483_647
MAX_SPEC_BYTES = 48 * 1024
WINDOW = timedelta(hours=24)
FIELDS = {
    "desktop.status": {"state"},
    "desktop.question": SPEC | EXPIRY | {"question"},
    "desktop.draft": SPEC | {"specJson"},
    "desktop.review": SPEC | EXPIRY | {"specJson", "summary"},
    "desktop.error": {"code"},
    "phone.answer": LINK | SPEC | {"text"},
    "phone.cancel": LINK,
    "phone.revise": LINK | SPEC | {"text"},
    "phone.confirm": LINK | SPEC,
    "phone.reject": LINK | SPEC,
    "dialogue.pull": {"after", "limit"},
    "dialogue.page": {"after", "next", "hasMore", "events"},
    "dialogue.ack": {"through"},
    "dialogue.accepted": {"eventId", "sequence", "duplicate"},
    "dialogue.error": {"code"},
}
STATES = {
    "connected": {"queued", "cancelled", "failed"},
    "queued": {"running", "cancelled", "failed"},
    "running": {"waiting_for_user", "result_ready", "cancelled", "failed"},
    "waiting_for_user": {"queued", "cancelled", "failed"},
    "result_ready": {"result_ready", "queued", "confirmed", "cancelled", "failed"},
    "failed": {"queued", "cancelled"},
    "confirmed": set(),
    "cancelled": set(),
}
ERRORS = {
    "INVALID_REQUEST", "UNSUPPORTED_SCHEMA_VERSION", "PAYLOAD_TOO_LARGE", "AUTH_REQUIRED",
    "DEVICE_REVOKED", "SESSION_NOT_FOUND", "CURSOR_EXPIRED", "INVALID_CURSOR",
    "EVENT_ID_CONFLICT", "EVENT_SEQUENCE_CONFLICT", "STALE_EVENT", "EXPIRED_EVENT",
    "SPEC_MISMATCH", "INVALID_STATE", "RATE_LIMITED", "INTERNAL_ERROR",
    "MODEL_TIMEOUT", "MODEL_CANCELLED", "MODEL_UNAVAILABLE", "MODEL_AUTH_FAILED",
    "MODEL_RATE_LIMITED", "MODEL_INVALID_OUTPUT", "MODEL_OUTPUT_TOO_LARGE",
    "MODEL_BUDGET_EXHAUSTED", "MODEL_ROUND_LIMIT", "MODEL_CONFIG_INVALID",
    "MODEL_BUSY", "MODEL_USAGE_UNCERTAIN", "MODEL_INPUT_TOO_LARGE", "MODEL_REQUEST_CONFLICT",
}


def require(condition, code="INVALID_REQUEST"):
    if not condition:
        raise ProtocolError(code)


def sequence(event, field, minimum=1, maximum=MAX_SEQUENCE):
    return _integer(event[field], field, minimum, maximum)


def state_of(event):
    return {
        "desktop.question": "waiting_for_user", "desktop.review": "result_ready",
        "desktop.error": "failed", "desktop.draft": "running",
    }.get(event["type"], event.get("state"))


def check_transition(previous, following):
    require(previous in STATES and following in STATES[previous], "INVALID_STATE")


def validate(event):
    require(isinstance(event, dict))
    version = _integer(event.get("dialogueVersion"), "dialogueVersion", -2_147_483_648, MAX_SEQUENCE)
    require(version == 1, "UNSUPPORTED_SCHEMA_VERSION")
    kind = _string(event.get("type"), "type")
    require(kind in FIELDS)
    is_event = not kind.startswith("dialogue.")
    required = SCOPE | FIELDS[kind] | {"dialogueVersion", "type"} | (EVENT if is_event else set())
    if kind == "desktop.status":
        state = _string(event.get("state"), "state")
        require(state in {"connected", "queued", "running", "cancelled", "confirmed"})
        if state == "confirmed":
            required |= SPEC | LINK | {"savedSpecSha256"}
    _keys(event, required)
    _device(event["deviceId"])
    require(PROJECT_ID.fullmatch(_string(event["projectId"], "projectId")) is not None)
    for key in {"sessionId", "eventId", "messageId", "inReplyTo"} & required:
        _uuid(event[key], key)
    for key in {"sequence", "specVersion", "through"} & required:
        sequence(event, key)
    for key in {"specSha256", "savedSpecSha256"} & required:
        _sha(event[key], key)
    for key in {"text", "question", "summary"} & required:
        _string(event[key], key, maximum=12_000 if key == "text" else 2_000)
    if is_event:
        created = _timestamp(event["createdAt"], "createdAt")
        if "expiresAt" in required:
            expiry = _timestamp(event["expiresAt"], "expiresAt")
            require(created < expiry <= created + WINDOW)
    if "specJson" in required:
        encoded = _string(event["specJson"], "specJson").encode("utf-8")
        require(len(encoded) <= MAX_SPEC_BYTES, "PAYLOAD_TOO_LARGE")
        require(sha256(encoded).hexdigest() == event["specSha256"])
        spec = strict_object(encoded)
        require(spec.get("status") == "draft" and spec.get("projectId") == event["projectId"])
        require(type(spec.get("schemaVersion")) is int and spec["schemaVersion"] == 1)
        require(type(spec.get("version")) is int and spec["version"] == event["specVersion"])
    if "code" in required:
        require(_string(event["code"], "code") in ERRORS)
    if "duplicate" in required:
        require(type(event["duplicate"]) is bool)
    if "after" in required:
        sequence(event, "after", 0)
    if kind == "dialogue.pull":
        sequence(event, "limit", 1, 20)
    if kind == "dialogue.page":
        validate_page(event)
    return event


def validate_page(page):
    events = page["events"]
    require(isinstance(events, list) and len(events) <= 20 and type(page["hasMore"]) is bool)
    require(events or not page["hasMore"])
    ids = set()
    for expected, item in enumerate(events, page["after"] + 1):
        require(isinstance(item, dict) and isinstance(item.get("type"), str) and item["type"].startswith("desktop."))
        validate(item)
        require(all(item[key] == page[key] for key in SCOPE) and item["sequence"] == expected)
        require(item["eventId"] not in ids)
        ids.add(item["eventId"])
    require(sequence(page, "next", 0) == page["after"] + len(events))


def parse(raw):
    return validate(strict_object(raw))


def strict_object(raw):
    value = parse_json(raw)
    pending = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        require(depth <= 64)
        if isinstance(item, dict):
            for key, child in item.items():
                _string(key, "json-key", non_empty=False)
                pending.append((child, depth + 1))
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            _string(item, "json-string", non_empty=False)
        elif isinstance(item, float):
            require(isfinite(item))
        elif type(item) is int:
            try:
                require(isfinite(float(item)))
            except OverflowError:
                raise ProtocolError("INVALID_REQUEST") from None
    return value


def check_submission(event, head, prior, next_sequence, now):
    """Call inside the session transaction, with prior looked up by scoped event ID."""
    require(event["type"].startswith("phone."))
    require(all(event[key] == head[key] for key in SCOPE), "SESSION_NOT_FOUND")
    if prior is not None:
        require(event == prior, "EVENT_ID_CONFLICT")
        return "duplicate"
    require(event["sequence"] == next_sequence, "EVENT_SEQUENCE_CONFLICT")
    created = _timestamp(event["createdAt"], "createdAt")
    require(now - WINDOW < created <= now + timedelta(minutes=5), "EXPIRED_EVENT")
    require(event["inReplyTo"] == head["eventId"] and event["messageId"] == head["messageId"], "STALE_EVENT")
    kind = event["type"]
    state = state_of(head)
    if kind == "phone.cancel":
        check_transition(state, "cancelled")
        return "accepted"
    required_state = "waiting_for_user" if kind == "phone.answer" else "result_ready"
    require(state == required_state, "INVALID_STATE")
    require(now < _timestamp(head["expiresAt"], "expiresAt"), "EXPIRED_EVENT")
    require(all(event[key] == head[key] for key in SPEC), "SPEC_MISMATCH")
    return "accepted"
