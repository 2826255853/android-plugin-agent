"""Exercise the dialogue fixtures and optionally validate the JSON Schema."""

import argparse
import json
from datetime import datetime
from pathlib import Path

from dialogue_protocol import check_submission, check_transition, parse
from message_protocol import ProtocolError


ROOT = Path(__file__).resolve().parents[1] / "dialogue-protocol"


def outcome(action):
    try:
        return action()
    except ProtocolError as error:
        return error.code


def verify(check_schema):
    fixtures = json.loads((ROOT / "fixtures/stage-b1.json").read_text(encoding="utf-8"))
    samples = {name: parse(json.dumps(value)) for name, value in fixtures["samples"].items()}
    for name, fixture in fixtures["invalid"].items():
        raw = fixture.get("raw", json.dumps(fixture.get("value")))
        assert outcome(lambda: parse(raw)) == fixture.get("code", "INVALID_REQUEST"), name
    for name, fixture in fixtures["submission"].items():
        actual = outcome(lambda: check_submission(
            {**samples[fixture["action"]], **fixture.get("patch", {})},
            samples[fixture["head"]], samples.get(fixture.get("prior")),
            fixture.get("nextSequence", 1), datetime.fromisoformat(fixture.get("now", fixtures["now"])),
        ))
        assert actual == fixture["expected"], (name, actual)
    for previous, following, accepted in fixtures["transitions"]:
        assert outcome(lambda: check_transition(previous, following)) == (None if accepted else "INVALID_STATE")
    if check_schema:
        from jsonschema import Draft202012Validator

        schema = json.loads((ROOT / "dialogue.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
        for sample in samples.values():
            validator.validate(sample)
        for name, fixture in fixtures["invalid"].items():
            if "value" in fixture:
                assert validator.is_valid(fixture["value"]) == fixture.get("schema", False), name
    print("B.1 PASSED:", *(f"{len(fixtures[group])} {group}" for group in ("samples", "invalid", "submission", "transitions")))


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--schema", action="store_true", help="also verify with the optional jsonschema package")
    verify(cli.parse_args().schema)
