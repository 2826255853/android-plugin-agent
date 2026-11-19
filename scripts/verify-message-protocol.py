"""Run the stage A.1 message, pairing, and error contract matrix."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

from message_protocol import MAX_BODY_CHARS, MAX_REQUEST_BYTES, ProtocolError, parse_envelope


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "message-protocol" / "fixtures"
VALID = [
    "valid-pair-request.json",
    "valid-pair-confirm.json",
    "valid-pair-rejected.json",
    "valid-pair-revoke.json",
    "valid-pair-revoked.json",
    "valid-message-send.json",
    "valid-message-receipt.json",
    "valid-status-query.json",
    "valid-status-response.json",
    "valid-error.json",
]


def expect_error(name: str, raw: str | bytes, code: str) -> None:
    try:
        parse_envelope(raw)
    except ProtocolError as error:
        if error.code != code:
            raise AssertionError(f"{name}: expected {code}, got {error.code} ({error.reason})") from error
        return
    raise AssertionError(f"{name}: request was accepted")


def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=FIXTURES)
    parser.add_argument("--report", type=Path, help="write a JSON evidence report")
    args = parser.parse_args()
    fixtures = args.fixtures
    for name in VALID:
        parse_envelope((fixtures / name).read_bytes())

    expected = json.loads((fixtures / "expected-errors.json").read_text(encoding="utf-8"))
    for name, code in expected.items():
        expect_error(name, (fixtures / name).read_bytes(), code)

    message = json.loads((fixtures / "valid-message-send.json").read_text(encoding="utf-8"))
    too_long = copy.deepcopy(message)
    too_long["body"] = "x" * (MAX_BODY_CHARS + 1)
    too_long["bodySha256"] = "0" * 64
    expect_error("body-length-boundary", json.dumps(too_long), "INVALID_REQUEST")

    oversized = b"{" + b"x" * MAX_REQUEST_BYTES + b"}"
    expect_error("request-byte-boundary", oversized, "PAYLOAD_TOO_LARGE")

    unsupported = copy.deepcopy(message)
    unsupported["schemaVersion"] = 2
    expect_error("unsupported-schema-version", json.dumps(unsupported), "UNSUPPORTED_SCHEMA_VERSION")

    invalid_retry = json.loads((fixtures / "valid-error.json").read_text(encoding="utf-8"))
    invalid_retry["error"]["retryable"] = False
    expect_error("retry-after-without-retry", json.dumps(invalid_retry), "INVALID_REQUEST")

    report = {
        "status": "passed",
        "schemaVersion": 1,
        "fixedValidFixtures": len(VALID),
        "fixedInvalidFixtures": len(expected),
        "boundaryFixtures": 4,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"A.1 MESSAGE PROTOCOL PASSED: {len(VALID)} valid, {len(expected)} invalid and {report['boundaryFixtures']} boundary fixtures")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except (AssertionError, OSError, ValueError) as error:
        print(f"A.1 MESSAGE PROTOCOL FAILED: {error}", file=sys.stderr)
        raise SystemExit(1)
