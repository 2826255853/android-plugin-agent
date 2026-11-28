"""Run the desktop half of the shared candidate delivery v1 fixtures."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import unittest

from delivery_protocol import (Scope, StateEvent, check_active_receipt, check_page,
                               check_segment, check_single_receipt, next_state, parse)
from message_protocol import ProtocolError


FIXTURE = Path(__file__).resolve().parents[1] / "delivery-protocol" / "fixtures" / "stage-d1.json"


def wire(samples: dict, case: dict) -> str:
    value = samples[case["sample"]] | case.get("set", {})
    if "remove" in case:
        del value[case["remove"]]
    raw = case.get("raw", json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    if "replace" in case:
        old, new = case["replace"]
        if old not in raw:
            raise AssertionError("fixture replacement did not match")
        raw = raw.replace(old, new, 1)
    return raw


def event(value: dict) -> StateEvent:
    return StateEvent(value["eventId"], value["actor"], value["action"], value.get("reason"))


def verify_schema() -> None:
    from jsonschema import Draft202012Validator

    schema = json.loads((FIXTURE.parents[1] / "delivery.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    fixture = DeliveryFixtures.fixture
    for name, sample in fixture["samples"].items():
        assert validator.is_valid(sample), name
    invalid_shapes = {"missing-spec-sha", "negative-size", "zero-size", "above-max-size",
                      "boolean-size", "string-size", "bad-package-hash", "short-manifest-hash",
                      "unsafe-package-name", "unknown-field", "failed-check", "oversized-segment",
                      "empty-page-with-more", "unknown-error-code"}
    for case in fixture["parseCases"]:
        if case["name"] in invalid_shapes:
            assert not validator.is_valid(json.loads(wire(fixture["samples"], case))), case["name"]
    print(f"Candidate delivery schema passed: {len(fixture['samples'])} samples, {len(invalid_shapes)} invalid shapes")


class DeliveryFixtures(unittest.TestCase):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def check_outcome(self, case: dict, action) -> None:
        with self.subTest(case=case["name"]):
            try:
                actual = action() or "OK"
            except ProtocolError as failure:
                actual = failure.code
            self.assertEqual(case["expected"], actual)

    def test_parsing(self) -> None:
        samples = self.fixture["samples"]
        for case in self.fixture["parseCases"]:
            def validate():
                parse(wire(samples, case))
            self.check_outcome(case, validate)

    def test_context(self) -> None:
        samples = self.fixture["samples"]
        receipt, query = (parse(json.dumps(samples[name])) for name in ("receipt", "query"))
        for case in self.fixture["contexts"]:
            def validate():
                value = parse(wire(samples, case))
                scope = self.fixture["scope"] | case.get("scope", {})
                target = Scope(scope["sourceFingerprint"], scope["deviceId"], scope["credentialId"], scope["projectId"])
                now = datetime.fromisoformat(case.get("now", self.fixture["now"]).replace("Z", "+00:00"))
                if case["sample"] == "page":
                    check_page(value, query, target, now)
                elif case["sample"] in {"published", "found"}:
                    request = "publish" if case["sample"] == "published" else "lookup"
                    check_single_receipt(value, parse(json.dumps(samples[request])), target, now)
                elif case["sample"] == "segment":
                    check_active_receipt(receipt, target, now)
                    check_segment(value, receipt)
                else:
                    check_active_receipt(value, target, now)
            self.check_outcome(case, validate)

    def test_states(self) -> None:
        for case in self.fixture["states"]:
            prior = event(case["prior"]) if "prior" in case else None
            self.check_outcome(case, lambda: next_state(case["current"], event(case["event"]), prior))


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--schema", action="store_true", help="also verify with the optional jsonschema package")
    args = cli.parse_args()
    if args.schema:
        verify_schema()
    unittest.main(argv=[__file__], verbosity=2)
