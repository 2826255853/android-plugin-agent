"""Repeatable Stage C.3 checks using fixed model responses and temporary task stores."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import task_runtime
from generation_response import GenerationPatchCoordinator, MAX_RESPONSE_BYTES, parse_response
from model_boundary import MAX_PROJECT_BUDGET, MOCK_MODEL, Config, ModelBoundary, ProviderReply, Request, Usage
from plugin_generation import generate_files
from task_runtime import TaskStore
from uuid import uuid4


SAMPLE = json.loads((ROOT / "desktop-spec/examples/quick-notes-v1.json").read_text(encoding="utf-8"))


class StageC3Tests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="stage-c3-")
        self.addCleanup(self.directory.cleanup)
        self.store = TaskStore(Path(self.directory.name) / "tasks")
        self.spec = copy.deepcopy(SAMPLE)
        self.spec["status"] = "confirmed"
        self.task_id = self.store.create(self.spec, max_cost_micros=1000)["taskId"]
        self.store.transition(self.task_id, "generating")
        self.files = generate_files(self.spec)
        for name, content in self.files.items():
            self.store.tool(self.task_id, "apply_patch", {
                "path": name, "expectedSha256": None, "content": content.decode("utf-8")})
        self.coordinator = GenerationPatchCoordinator(self.store)

    def response(self, *, paths=("app.js",), mutate=None):
        patches = []
        for path in paths:
            current = self.store.tool(self.task_id, "read_file", {"path": path})["result"]
            content = current["content"] + "\n/* fixed C.3 response */\n"
            patches.append({
                "plan": "Preserve the confirmed note workflow.",
                "specRef": {"type": "acceptanceCriteria", "id": "save_note"},
                "path": path,
                "expectedSha256": current["sha256"],
                "replacement": content,
                "expectedChecks": ["page_structure", "resource_integrity"],
            })
        candidate = {"version": "plugin-development-v1", "patches": patches}
        if mutate:
            mutate(candidate)
        return json.dumps(candidate, ensure_ascii=False)

    def test_fixed_response_validates_and_records_before_after_hashes(self):
        result = self.coordinator.apply(self.task_id, self.response())
        self.assertTrue(result["ok"], result)
        patch = result["patches"][0]
        self.assertEqual(hashlib.sha256(self.files["app.js"]).hexdigest(), patch["beforeSha256"])
        current = self.store.tool(self.task_id, "read_file", {"path": "app.js"})["result"]["content"]
        self.assertEqual(hashlib.sha256(current.encode()).hexdigest(), patch["afterSha256"])
        history = self.store.inspect(self.task_id)["patches"]
        self.assertEqual(5, len(history))
        self.assertIn("PATCH_REVISION_COMMITTED", [event["code"] for event in self.store.inspect(self.task_id)["events"]])

    def test_invalid_json_duplicate_keys_and_output_limit_have_stable_codes(self):
        for raw, code in ((b'{"version":"x","version":"y"}', "MODEL_INVALID_OUTPUT"),
                          (b"{" + b" " * MAX_RESPONSE_BYTES + b"}", "MODEL_OUTPUT_TOO_LARGE"),
                          ("not-json", "MODEL_INVALID_OUTPUT")):
            with self.subTest(code=code):
                result = self.coordinator.apply(self.task_id, raw)
                self.assertEqual(code, result["code"])
        with self.assertRaises(ValueError):
            parse_response(b"[]")

    def test_fixed_provider_response_uses_stage_b_model_boundary(self):
        response = self.response()

        class FixedProvider:
            def __init__(self, content):
                self.content = content

            def invoke(self, request, stop):
                return ProviderReply(self.content, Usage(10, 64))

        def call(content):
            boundary = ModelBoundary(Config("mock", MOCK_MODEL, MAX_PROJECT_BUDGET), FixedProvider(content))
            request = Request(uuid4(), "quick-notes", uuid4(), "fixed C.3 patch fixture")
            return boundary.call(request)

        valid = call(response)
        self.assertEqual("plugin-development-v1", valid.candidate["version"])
        invalid = call("not-json")
        self.assertEqual("MODEL_INVALID_OUTPUT", invalid.receipt.error)
        oversized = call("x" * (MAX_RESPONSE_BYTES + 1))
        self.assertEqual("MODEL_OUTPUT_TOO_LARGE", oversized.receipt.error)

    def test_schema_reference_path_check_and_capability_expansion_are_rejected(self):
        cases = [
            (lambda value: value["patches"][0].update(expectedChecks=["shell"]), "PATCH_CHECK_NOT_ALLOWED"),
            (lambda value: value["patches"][0]["specRef"].update(id="missing"), "PATCH_SPEC_REFERENCE_INVALID"),
            (lambda value: value["patches"][0].update(path="manifest.json"), "PATCH_PATH_NOT_ALLOWED"),
            (lambda value: value["patches"][0].update(replacement='call("device.contacts", {})'),
             "PATCH_CAPABILITY_NOT_ALLOWED"),
        ]
        for mutate, code in cases:
            with self.subTest(code=code):
                result = self.coordinator.apply(self.task_id, self.response(mutate=mutate))
                self.assertEqual(code, result["code"])
        self.assertEqual(4, len(self.store.inspect(self.task_id)["patches"]))

    def test_stale_base_digest_is_rejected_without_writing(self):
        raw = json.loads(self.response())
        raw["patches"][0]["expectedSha256"] = "0" * 64
        result = self.coordinator.apply(self.task_id, json.dumps(raw))
        self.assertEqual("PATCH_CONFLICT", result["code"])
        self.assertEqual(4, len(self.store.inspect(self.task_id)["patches"]))

    def test_multi_file_commit_failure_restores_successful_snapshot(self):
        original = task_runtime.os.replace
        calls = 0

        def fail_second(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected replacement failure")
            return original(source, destination)

        before = {name: self.store.tool(self.task_id, "read_file", {"path": name})["result"]["sha256"]
                  for name in ("app.js", "app.css")}
        with patch("task_runtime.os.replace", side_effect=fail_second):
            result = self.coordinator.apply(self.task_id, self.response(paths=("app.js", "app.css")))
        self.assertFalse(result["ok"])
        self.assertEqual("PATCH_APPLY_FAILED", result["code"])
        after = {name: self.store.tool(self.task_id, "read_file", {"path": name})["result"]["sha256"]
                 for name in before}
        self.assertEqual(before, after)
        self.assertEqual(4, len(self.store.inspect(self.task_id)["patches"]))
        self.assertNotIn("PATCH_REVISION_COMMITTED", [event["code"] for event in self.store.inspect(self.task_id)["events"]])


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(StageC3Tests))
    raise SystemExit(0 if result.wasSuccessful() else 1)
