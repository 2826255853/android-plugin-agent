"""Repeatable stage 2.3 checks for bounded generation and static packaging.

All specifications and costs are fictional. Tests use temporary stores and do
not call models, execute generated JavaScript, start browsers or use devices.
"""

from __future__ import annotations

import copy
import hashlib
import html
import io
import json
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from spec_workflow import save_confirmed
from task_runtime import TaskStore
from plugin_package import PackageError, build_package, validate_package, validate_source
from plugin_generation import GenerationError, GenerationPipeline, generate_files, run_static_tests


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "plugin-protocol" / "fixtures"
SAMPLE = json.loads((ROOT / "desktop-spec/examples/quick-notes-v1.json").read_text(encoding="utf-8"))


class PackageContractTests(unittest.TestCase):
    def test_manifest_text_uses_android_utf16_lengths_and_rejects_surrogates(self):
        files = self.source()
        manifest = json.loads(files["manifest.json"])
        manifest["name"] = "😀" * 32
        files["manifest.json"] = json.dumps(manifest).encode("utf-8")
        validate_source(files)
        for name in ("😀" * 33, "\ud800"):
            manifest["name"] = name
            files["manifest.json"] = json.dumps(manifest).encode("utf-8")
            with self.subTest(name=ascii(name)), self.assertRaises(PackageError):
                validate_source(files)
        manifest["name"] = "Fictional notes"
        field = manifest["dataModel"]["collections"][0]["fields"][0]
        field["maxLength"] = 64
        for value, accepted in (("😀" * 32, True), ("😀" * 33, False), ("\udfff", False)):
            field["default"] = value
            files["manifest.json"] = json.dumps(manifest).encode("utf-8")
            if accepted:
                validate_source(files)
            else:
                with self.assertRaises(PackageError):
                    validate_source(files)

    def source(self):
        with zipfile.ZipFile(FIXTURES / "valid-known-capability-v1.zip") as archive:
            return {info.filename: archive.read(info) for info in archive.infolist()}

    def replace_resource(self, files, path, content):
        files[path] = content
        manifest = json.loads(files["manifest.json"])
        for resource in manifest["files"]:
            if resource["path"] == path:
                resource["sha256"] = hashlib.sha256(content).hexdigest()
        files["manifest.json"] = json.dumps(manifest).encode("utf-8")

    def test_existing_valid_packages_pass(self):
        for name in ("valid-minimal-v1.zip", "valid-known-capability-v1.zip"):
            with self.subTest(package=name):
                manifest = validate_package((FIXTURES / name).read_bytes())
                self.assertEqual(1, manifest["manifestVersion"])

    def test_existing_negative_packages_keep_protocol_error_codes(self):
        expected = json.loads((FIXTURES / "expected-errors.json").read_text(encoding="utf-8"))
        for name, code in expected.items():
            with self.subTest(package=name):
                if code == "ACCEPTED":
                    manifest = validate_package((FIXTURES / name).read_bytes())
                    self.assertEqual(1, manifest["manifestVersion"])
                    continue
                with self.assertRaises(PackageError) as raised:
                    validate_package((FIXTURES / name).read_bytes())
                self.assertEqual(code, raised.exception.code, name)

    def test_rebuilding_valid_source_is_deterministic(self):
        files = self.source()
        manifest = validate_source(files)
        first = build_package(files)
        self.assertEqual(first, build_package(dict(reversed(list(files.items())))))
        self.assertEqual(manifest, validate_package(first))

    def test_changed_resource_hash_is_rejected_before_packaging(self):
        files = self.source()
        files["app.js"] += b"\n// fictional untrusted edit\n"
        with self.assertRaises(PackageError) as raised:
            build_package(files)
        self.assertEqual("RESOURCE_HASH_MISMATCH", raised.exception.code)

    def test_package_identity_is_checked_against_coordinator_binding(self):
        payload = (FIXTURES / "valid-known-capability-v1.zip").read_bytes()
        with self.assertRaises(PackageError) as raised:
            validate_package(payload, expected_plugin_id="org.fictional.another-plugin")
        self.assertEqual("PLUGIN_ID_MISMATCH", raised.exception.code)

    def test_local_resource_links_must_exist_and_external_css_is_rejected(self):
        for content in (b"body { background: url(https://fictional.invalid/image.png); }",
                        b"@import '\\68 ttps://fictional.invalid/style.css';",
                        b"body { background: url(missing.png); }"):
            with self.subTest(content=content):
                files = self.source()
                self.replace_resource(files, "app.css", content)
                with self.assertRaises(PackageError) as raised:
                    validate_source(files)
                self.assertEqual("RESOURCE_REFERENCE_INVALID", raised.exception.code)

    def test_manifest_depth_and_nonfinite_values_are_rejected(self):
        for content in (b'{"value":NaN}', b'{"value":' + b'[' * 40 + b'0' + b']' * 40 + b'}'):
            with self.subTest(content=content):
                files = self.source()
                files["manifest.json"] = content
                with self.assertRaises(PackageError) as raised:
                    validate_source(files)
                self.assertEqual("MANIFEST_INVALID", raised.exception.code)

    def test_source_and_zip_resource_limits_are_enforced(self):
        files = self.source()
        self.replace_resource(files, "app.js", b"x" * (2 * 1024 * 1024 + 1))
        with self.assertRaises(PackageError) as raised:
            validate_source(files)
        self.assertEqual("PACKAGE_LIMIT_EXCEEDED", raised.exception.code)
        files = self.source()
        self.replace_resource(files, "app.js", b"x" * (100 * 1024))
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        with self.assertRaises(PackageError) as raised:
            validate_package(stream.getvalue())
        self.assertEqual("PACKAGE_LIMIT_EXCEEDED", raised.exception.code)

    def test_crc_corruption_is_detected_without_extracting_files(self):
        payload = bytearray(build_package(self.source()))
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            resource = archive.getinfo("app.js")
            offset = resource.header_offset + 30 + len(resource.filename.encode()) + len(resource.extra)
        payload[offset] ^= 1
        with self.assertRaises(PackageError) as raised:
            validate_package(bytes(payload))
        self.assertEqual("PACKAGE_CORRUPT", raised.exception.code)


class GenerationPipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="stage23-")
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.now = 1000.0
        self.store = TaskStore(self.base / "tasks", clock=lambda: self.now)
        saved = save_confirmed(copy.deepcopy(SAMPLE), self.base / "specs")
        self.spec = json.loads(saved.read_text(encoding="utf-8"))
        self.pipeline = GenerationPipeline(self.store)

    def create(self, spec=None, **limits):
        return self.store.create(spec or self.spec, max_cost_micros=1000, **limits)["taskId"]

    def artifact(self, task_id):
        return self.store.root / "artifacts" / (task_id + ".zip")

    def assert_no_artifact(self, task_id):
        state = self.store.inspect(task_id)
        self.assertFalse(state["deliverable"])
        self.assertFalse(self.artifact(task_id).exists())
        self.assertFalse(any(build["ok"] for build in state["builds"]))

    def test_same_spec_in_distinct_tasks_produces_identical_verified_packages(self):
        first, second = self.create(), self.create()
        one, two = self.pipeline.run(first), self.pipeline.run(second)
        self.assertTrue(one["ok"], one)
        self.assertTrue(two["ok"], two)
        self.assertEqual("built", one["status"])
        self.assertEqual(one["artifact"]["sha256"], two["artifact"]["sha256"])
        self.assertEqual(one["sourceSha256"], two["sourceSha256"])
        self.assertEqual(self.artifact(first).read_bytes(), self.artifact(second).read_bytes())
        manifest = validate_package(self.artifact(first).read_bytes())
        self.assertEqual(("org.generated.quick-notes", "1.0.0"), (manifest["id"], manifest["version"]))
        self.assertEqual(self.spec["collections"], manifest["dataModel"]["collections"])
        self.assertEqual({"data.read", "data.write"}, set(manifest["capabilities"]))
        state = self.store.inspect(first)
        self.assertEqual(state["specSha256"], one["specSha256"])
        self.assertEqual(4, len(state["patches"]))
        self.assertEqual([one], state["builds"])
        self.assertEqual((0, 0, []), (state["spentMicros"], state["reservedMicros"], state["modelCalls"]))
        self.assertFalse(one["deliverable"])
        self.assertFalse(one["executedGeneratedCode"])
        self.assertTrue(one["checks"])

    def test_name_markers_and_html_are_escaped_without_second_template_expansion(self):
        spec = copy.deepcopy(self.spec)
        spec["title"] = "__TITLE_LIMIT__ </title><script>1</script>"
        files = generate_files(spec)
        page = files["index.html"].decode("utf-8")
        self.assertIn(html.escape(spec["title"], quote=True), page)
        self.assertIn("__TITLE_LIMIT__ &lt;/title&gt;", page)
        self.assertNotIn("<script>1</script>", page)
        self.assertEqual(spec["title"], validate_source(files)["name"])
        run_static_tests(files, spec)

    def test_title_length_matches_android_utf16_limit(self):
        spec = copy.deepcopy(self.spec)
        spec["title"] = "😀" * 32
        self.assertEqual(spec["title"], validate_source(generate_files(spec))["name"])
        spec["title"] += "😀"
        with self.assertRaises(GenerationError) as raised:
            generate_files(spec)
        self.assertEqual("GENERATOR_TITLE_INVALID", raised.exception.code)

    def test_supported_project_version_and_field_limit_changes_reach_generated_contract(self):
        spec = copy.deepcopy(self.spec)
        spec.update(projectId="fictional-notes", version=2, title="虚构笔记样例")
        spec["collections"][0]["fields"][0]["maxLength"] = 80
        spec["collections"][0]["fields"][1]["maxLength"] = 2000
        task_id = self.create(spec)
        report = self.pipeline.run(task_id)
        self.assertTrue(report["ok"], report)
        manifest = validate_package(self.artifact(task_id).read_bytes())
        self.assertEqual(("org.generated.fictional-notes", "2.0.0"), (manifest["id"], manifest["version"]))
        self.assertEqual(spec["collections"], manifest["dataModel"]["collections"])
        self.assertEqual("1.1.0", manifest["dataModel"]["version"])
        with zipfile.ZipFile(self.artifact(task_id)) as archive:
            page = archive.read("index.html").decode("utf-8")
        self.assertIn('maxlength="80"', page)
        self.assertIn('maxlength="2000"', page)

    def test_draft_and_unrepresented_workflows_are_rejected_explicitly(self):
        with self.assertRaises(GenerationError) as raised:
            generate_files(SAMPLE)
        self.assertEqual("SPEC_NOT_CONFIRMED", raised.exception.code)
        spec = copy.deepcopy(self.spec)
        spec["workflows"][0]["steps"].append("发送到一个虚构远程服务")
        task_id = self.create(spec)
        result = self.pipeline.run(task_id)
        self.assertEqual((False, "generating", "GENERATOR_UNSUPPORTED_SPEC"),
                         (result["ok"], result["stage"], result["code"]))
        self.assert_no_artifact(task_id)

    def test_unknown_capability_with_recorded_alternative_does_not_silently_generate(self):
        spec = copy.deepcopy(self.spec)
        spec["permissions"].append({"capability": "device.contacts", "justification": "虚构联系人需求"})
        spec["unsupportedItems"].append({"request": "device.contacts", "reason": "宿主未开放", "alternative": "手动输入"})
        task_id = self.create(spec)
        result = self.pipeline.run(task_id)
        self.assertEqual("GENERATOR_UNSUPPORTED_SPEC", result["code"])
        self.assert_no_artifact(task_id)

    def test_manifest_failure_is_located_and_persisted(self):
        task_id = self.create()
        files = generate_files(self.spec)
        files["manifest.json"] = b"{}"
        with patch("plugin_generation.generate_files", return_value=files):
            result = self.pipeline.run(task_id)
        self.assertEqual((False, "manifest", "MANIFEST_INVALID"),
                         (result["ok"], result["stage"], result["code"]))
        self.assertEqual("manifest", self.store.inspect(task_id)["builds"][0]["stage"])
        self.assert_no_artifact(task_id)

    def test_build_and_static_test_failures_keep_distinct_stage_diagnostics(self):
        cases = (("build_package", PackageError("FICTIONAL_BUILD_ERROR", check="fixture"), "building"),
                 ("run_static_tests", GenerationError("STATIC_TEST_FAILED", "index.html", "page_structure"), "testing"))
        for function, error, stage in cases:
            with self.subTest(stage=stage):
                task_id = self.create()
                with patch("plugin_generation." + function, side_effect=error):
                    result = self.pipeline.run(task_id)
                self.assertEqual((False, stage, error.code), (result["ok"], result["stage"], result["code"]))
                state = self.store.inspect(task_id)
                self.assertEqual(stage, state["builds"][0]["stage"])
                self.assertTrue(any(event["code"] == "ROUND_FAILED" for event in state["events"]))
                self.assert_no_artifact(task_id)

    def test_hash_corruption_stops_task_immediately(self):
        task_id = self.create()
        files = generate_files(self.spec)
        files["app.js"] += b"\n// fictional alteration\n"
        with patch("plugin_generation.generate_files", return_value=files):
            result = self.pipeline.run(task_id)
        state = self.store.inspect(task_id)
        self.assertEqual(("manifest", "RESOURCE_HASH_MISMATCH", "stopped"),
                         (result["stage"], result["code"], state["status"]))
        self.assertEqual("RESOURCE_HASH_MISMATCH", state["stopCode"])
        self.assert_no_artifact(task_id)

    def test_preexisting_undeclared_workspace_file_is_not_ignored(self):
        task_id = self.create()
        self.store.transition(task_id, "generating")
        result = self.store.tool(task_id, "apply_patch", {
            "path": "extra.js", "expectedSha256": None,
            "content": "throw new Error('fictional payload must never execute');"})
        self.assertTrue(result["ok"])
        report = self.pipeline.run(task_id)
        self.assertEqual(("manifest", "RESOURCE_UNDECLARED"), (report["stage"], report["code"]))
        self.assert_no_artifact(task_id)

    def test_repeat_failure_requires_repair_then_stops_at_identical_fingerprint(self):
        task_id = self.create()
        with patch("plugin_generation.build_package", side_effect=PackageError("FICTIONAL_BUILD_ERROR", check="fixture")):
            first = self.pipeline.run(task_id)
            blocked = self.pipeline.run(task_id)
            self.assertEqual("REPAIR_REQUIRED", blocked["code"])
            self.assertEqual(1, len(self.store.inspect(task_id)["builds"]))
            self.assertTrue(self.store.begin_repair(task_id)["ok"])
            second = self.pipeline.run(task_id)
        state = self.store.inspect(task_id)
        self.assertFalse(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual(("stopped", "REPEATED_ERROR", 1),
                         (state["status"], state["stopCode"], state["repairRounds"]))
        self.assertEqual(2, len(state["builds"]))
        self.assert_no_artifact(task_id)

    def test_transient_build_failure_can_be_retried_without_losing_history(self):
        task_id = self.create()
        with patch("plugin_generation.build_package", side_effect=PackageError("FICTIONAL_BUILD_ERROR", check="fixture")):
            self.assertFalse(self.pipeline.run(task_id)["ok"])
        self.assertTrue(self.store.begin_repair(task_id)["ok"])
        self.assertTrue(self.pipeline.run(task_id)["ok"])
        state = self.store.inspect(task_id)
        self.assertEqual(("built", 1, 2), (state["status"], state["repairRounds"], len(state["builds"])))
        self.assertEqual([False, True], [item["ok"] for item in state["builds"]])
        self.assertEqual(8, len(state["patches"]))
        validate_package(self.artifact(task_id).read_bytes())

    def test_same_error_in_distinct_build_stages_has_distinct_fingerprint(self):
        task_id = self.create()
        error = GenerationError("FICTIONAL_STAGE_ERROR", "index.html", "fixture")
        with patch("plugin_generation.build_package", side_effect=error):
            first = self.pipeline.run(task_id)
        self.assertEqual("building", first["stage"])
        self.store.begin_repair(task_id)
        with patch("plugin_generation.run_static_tests", side_effect=error):
            second = self.pipeline.run(task_id)
        state = self.store.inspect(task_id)
        self.assertEqual("testing", second["stage"])
        self.assertEqual("validating", state["status"])
        failures = [event["details"] for event in state["events"] if event["code"] == "ROUND_FAILED"]
        self.assertEqual(["building", "testing"], [failure["failureStage"] for failure in failures])
        self.assertNotEqual(failures[0]["fingerprint"], failures[1]["fingerprint"])
        self.assert_no_artifact(task_id)

    def test_cancel_during_generation_rejects_later_file_writes_and_build_success(self):
        task_id = self.create()
        original = self.store.tool

        def cancel_before_patch(bound_task, name, params, **kwargs):
            if name == "apply_patch":
                self.store.cancel(bound_task)
            return original(bound_task, name, params, **kwargs)

        with patch.object(self.store, "tool", side_effect=cancel_before_patch):
            result = self.pipeline.run(task_id)
        state = self.store.inspect(task_id)
        self.assertFalse(result["ok"])
        self.assertEqual(("cancelled", "USER_CANCELLED", []),
                         (state["status"], state["stopCode"], state["patches"]))
        self.assertEqual("generating", state["builds"][0]["stage"])
        self.assert_no_artifact(task_id)

    def test_deadline_before_finalization_prevents_successful_artifact(self):
        task_id = self.create(max_duration_seconds=1)
        original = build_package

        def expire_after_build(files):
            payload = original(files)
            self.now += 2
            return payload

        with patch("plugin_generation.build_package", side_effect=expire_after_build):
            result = self.pipeline.run(task_id)
        self.assertEqual((False, "TIME_LIMIT", "stopped"), (result["ok"], result["code"], result["status"]))
        self.assertNotIn("artifact", result)
        self.assertEqual("TIME_LIMIT", self.store.inspect(task_id)["stopCode"])
        self.assertEqual("testing", self.store.inspect(task_id)["builds"][0]["stage"])
        self.assert_no_artifact(task_id)

    def test_tool_quota_is_preserved_by_generation_coordinator(self):
        task_id = self.create(max_tool_calls=1)
        result = self.pipeline.run(task_id)
        self.assertEqual((False, "TOOL_LIMIT", "stopped"), (result["ok"], result["code"], result["status"]))
        self.assert_no_artifact(task_id)

    def test_concurrent_coordinator_cannot_overwrite_active_round(self):
        task_id = self.create()
        entered, release = threading.Event(), threading.Event()
        original = generate_files

        def hold_generation(spec):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test coordinator did not release generation")
            return original(spec)

        with patch("plugin_generation.generate_files", side_effect=hold_generation):
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(self.pipeline.run, task_id)
                try:
                    self.assertTrue(entered.wait(5))
                    blocked = self.pipeline.run(task_id)
                    self.assertEqual("BUILD_IN_PROGRESS", blocked["code"])
                finally:
                    release.set()
                result = future.result(timeout=5)
        self.assertTrue(result["ok"], result)
        self.assertEqual(1, len(self.store.inspect(task_id)["builds"]))

    def test_built_snapshot_survives_reopen_deadline_and_repeated_export(self):
        task_id = self.create()
        self.assertTrue(self.pipeline.run(task_id)["ok"])
        before = self.store.inspect(task_id)
        payload = self.artifact(task_id).read_bytes()
        self.now += 10000
        reopened = TaskStore(self.store.root, clock=lambda: self.now)
        after = reopened.inspect(task_id)
        self.assertEqual(("built", before["elapsedSeconds"]), (after["status"], after["elapsedSeconds"]))
        self.assertEqual(payload, reopened.export_build(task_id).read_bytes())
        self.assertEqual("built", reopened.cancel(task_id)["status"])
        self.assertEqual("TASK_TERMINAL", GenerationPipeline(reopened).run(task_id)["code"])
        self.assertEqual(1, len(reopened.inspect(task_id)["builds"]))

    def test_failed_export_keeps_verified_snapshot_available_for_retry(self):
        task_id = self.create()
        with patch.object(self.store, "export_build", side_effect=PermissionError("fictional private export location")):
            result = self.pipeline.run(task_id)
        self.assertTrue(result["ok"], result)
        self.assertEqual(("built", False, "EXPORT_FAILED"),
                         (result["status"], result["exported"], result["exportCode"]))
        state = self.store.inspect(task_id)
        self.assertEqual("built", state["status"])
        self.assertEqual(1, len(state["builds"]))
        self.assertTrue(state["builds"][0]["ok"])
        self.assertNotIn("fictional private export location", json.dumps(result))
        self.assertNotIn("fictional private export location", json.dumps(state))
        self.assertFalse(self.artifact(task_id).exists())
        payload = self.store.export_build(task_id).read_bytes()
        validate_package(payload)
        self.assertEqual(result["artifact"]["sha256"], hashlib.sha256(payload).hexdigest())

    def test_claim_from_ready_atomically_starts_and_is_recoverable_after_interruption(self):
        task_id = self.create()
        claimed = self.store.claim_build(task_id)
        self.assertTrue(claimed["ok"])
        self.assertEqual("generating", claimed["status"])
        self.assertTrue((self.store.workspaces / task_id).is_dir())
        reopened = TaskStore(self.store.root, clock=lambda: self.now)
        self.assertEqual("BUILD_IN_PROGRESS", reopened.claim_build(task_id)["code"])
        reopened.sweep(recover=True)
        state = reopened.inspect(task_id)
        self.assertEqual(("stopped", "PROCESS_INTERRUPTED"), (state["status"], state["stopCode"]))
        self.assertEqual("TASK_TERMINAL", GenerationPipeline(reopened).run(task_id)["code"])
        self.assert_no_artifact(task_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
