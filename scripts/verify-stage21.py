"""Repeatable Stage 2.1 checks using only the Python standard library."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from spec_workflow import (  # noqa: E402
    SpecError,
    clarification_questions,
    save_confirmed,
    unsupported_requests,
    validate_spec,
)


SAMPLE = json.loads((ROOT / "desktop-spec" / "examples" / "quick-notes-v1.json").read_text(encoding="utf-8"))


class Stage21Tests(unittest.TestCase):
    def test_complete_example_is_confirmable(self) -> None:
        validate_spec(SAMPLE, require_confirmable=True)
        self.assertEqual([], clarification_questions(SAMPLE))
        self.assertEqual([], unsupported_requests(SAMPLE))

    def test_missing_decisions_produce_targeted_questions(self) -> None:
        draft = copy.deepcopy(SAMPLE)
        draft["pages"] = []
        draft["workflows"] = []
        draft["acceptanceCriteria"] = []
        draft["storesData"] = None
        fields = {question["field"] for question in clarification_questions(draft)}
        self.assertEqual({"pages", "workflows", "storesData", "acceptanceCriteria"}, fields)

    def test_unsupported_capability_requires_reason_and_alternative(self) -> None:
        draft = copy.deepcopy(SAMPLE)
        draft["permissions"].append({"capability": "device.contacts", "justification": "查找联系人"})
        with self.assertRaisesRegex(SpecError, "reason and alternative"):
            validate_spec(draft, require_confirmable=True)
        draft["unsupportedItems"].append({
            "request": "device.contacts",
            "reason": "当前插件宿主未开放通讯录访问。",
            "alternative": "允许用户手动输入联系人名称。",
        })
        validate_spec(draft, require_confirmable=True)
        self.assertEqual("允许用户手动输入联系人名称。", unsupported_requests(draft)[0]["alternative"])

    def test_confirmed_versions_are_ordered_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory)
            first = save_confirmed(copy.deepcopy(SAMPLE), store)
            self.assertEqual("confirmed", json.loads(first.read_text(encoding="utf-8"))["status"])
            next_spec = copy.deepcopy(SAMPLE)
            next_spec["version"] = 2
            second = save_confirmed(next_spec, store)
            self.assertEqual("v2.json", second.name)
            with self.assertRaisesRegex(SpecError, "next version must be 3"):
                save_confirmed(next_spec, store)
            self.assertTrue(first.exists())

    def test_duplicate_field_ids_are_rejected(self) -> None:
        draft = copy.deepcopy(SAMPLE)
        draft["collections"][0]["fields"][1]["id"] = "title"
        with self.assertRaisesRegex(SpecError, "duplicate field id"):
            validate_spec(draft)


if __name__ == "__main__":
    unittest.main(verbosity=2)
