"""Verify isolated generation, deterministic packaging, and template interactions."""

from __future__ import annotations

import copy
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from execution_isolation import SandboxConfig
from generation_response import GenerationPatchCoordinator
from plugin_generation import GenerationPipeline, generate_files
from task_runtime import TaskStore


QUICK = json.loads((ROOT / "desktop-spec/examples/quick-notes-v1.json").read_text(encoding="utf-8"))
CONTACTS = json.loads((ROOT / "desktop-spec/examples/manual-contacts-v1.json").read_text(encoding="utf-8"))
PASS_DOM = '<output id="c5-result">PASS</output>'


class StageC5Tests(unittest.TestCase):
    config: SandboxConfig | None = None

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="stage-c5-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = TaskStore(self.root / "tasks")

    def create_task(self, source=QUICK):
        spec = copy.deepcopy(source)
        spec["status"] = "confirmed"
        return self.store.create(spec, max_cost_micros=1000)["taskId"]

    def ready_workspace_with_revision(self, spec=QUICK):
        task_id = self.create_task(spec)
        self.store.transition(task_id, "generating")
        files = generate_files({**spec, "status": "confirmed"})
        for name, content in files.items():
            self.store.tool(task_id, "apply_patch", {
                "path": name, "expectedSha256": None, "content": content.decode("utf-8")})
        current = self.store.tool(task_id, "read_file", {"path": "app.js"})["result"]
        response = {"version": "plugin-development-v1", "patches": [{
            "plan": "Keep the confirmed note workflow while adding a harmless revision marker.",
            "specRef": {"type": "acceptanceCriteria", "id": "save_note"},
            "path": "app.js", "expectedSha256": current["sha256"],
            "replacement": current["content"] + "\n/* retained C.3 revision */\n",
            "expectedChecks": ["resource_integrity"]}]}
        applied = GenerationPatchCoordinator(self.store).apply(task_id, json.dumps(response))
        self.assertTrue(applied["ok"], applied)
        return task_id

    @staticmethod
    def success_result(profile, files):
        return {"code": "OK", "exitCode": 0, "output": PASS_DOM if profile == "preview" else "",
                "guestProcesses": 0, "hostProcesses": 0, "hostProcessesStarted": 2,
                "inputNames": sorted(files)}

    def fake_isolation(self, outcomes=None):
        outcomes = outcomes or {}

        def run(_config, profile, files, **_kwargs):
            return outcomes.get(profile, self.success_result(profile, files))

        return run

    def run_pipeline(self, task_id, outcomes=None):
        with patch("plugin_generation.run_isolated", side_effect=self.fake_isolation(outcomes)):
            return GenerationPipeline(self.store, self.config or SandboxConfig(
                Path("qemu.exe"), Path("image"), "0" * 64, "0" * 64), self.root / "runs").run(task_id)

    def test_c3_revision_is_the_source_for_deterministic_package_and_each_isolated_stage(self):
        task_id = self.ready_workspace_with_revision()
        report = self.run_pipeline(task_id)
        self.assertTrue(report["ok"], report)
        self.assertEqual(["build", "test", "preview"], list(report["isolatedChecks"]))
        self.assertTrue(report["executedGeneratedCode"])
        self.assertTrue(report["packaging"]["deterministic"])
        self.assertEqual(report["artifact"]["sha256"], report["packaging"]["packageSha256"])
        package = self.store.export_build(task_id).read_bytes()
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            self.assertIn(b"retained C.3 revision", archive.read("app.js"))
        self.assertEqual(report, self.store.inspect(task_id)["builds"][0])

    def test_isolation_build_failure_does_not_commit_success_package(self):
        task_id = self.create_task()
        failure = {"code": "ISOLATION_WORKLOAD_FAILED", "exitCode": 1, "output": "", "guestProcesses": 0,
                   "hostProcesses": 0, "hostProcessesStarted": 2}
        report = self.run_pipeline(task_id, {"build": failure})
        self.assertFalse(report["ok"])
        self.assertEqual("isolated_build", report["check"])
        self.assertFalse(report["executedGeneratedCode"])
        self.assertEqual("NOT_RUN", report["isolatedChecks"]["preview"]["code"])
        self.assertIsNone(self.store.inspect(task_id)["builds"][0].get("artifact"))
        self.assertFalse(list((self.root / "tasks" / "artifacts").glob("*.zip")))

    def test_preview_interaction_failure_preserves_previous_success_snapshot(self):
        first = self.create_task()
        successful = self.run_pipeline(first)
        payload = self.store.export_build(first).read_bytes()
        revised = copy.deepcopy(QUICK)
        revised["status"] = "confirmed"
        revised["version"] = 2
        second = self.create_task(revised)
        failure = {"code": "OK", "exitCode": 0, "output": '<output id="c5-result">FAIL</output>',
                   "guestProcesses": 0, "hostProcesses": 0, "hostProcessesStarted": 2}
        failed = self.run_pipeline(second, {"preview": failure})
        self.assertTrue(successful["ok"])
        self.assertFalse(failed["ok"])
        self.assertEqual("testing", failed["stage"])
        self.assertEqual("interactive_preview", failed["check"])
        self.assertEqual(payload, self.store.export_build(first).read_bytes())

    def test_quick_notes_and_manual_contacts_in_c4(self):
        if self.config is None:
            self.skipTest("pass --qemu and --image to run C.4 isolated VM integration")
        for spec in (QUICK, CONTACTS):
            with self.subTest(template=spec["collections"][0]["id"]):
                task_id = self.create_task(spec)
                report = GenerationPipeline(self.store, self.config, self.root / "real-runs").run(task_id)
                self.assertTrue(report["ok"], report)
                self.assertTrue(report["packaging"]["deterministic"])
                self.assertIn(PASS_DOM, report["isolatedChecks"]["preview"]["output"])


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", type=Path)
    parser.add_argument("--image", type=Path)
    args, tests = parser.parse_known_args()
    if bool(args.qemu) != bool(args.image):
        parser.error("--qemu and --image must be provided together")
    if args.qemu:
        StageC5Tests.config = SandboxConfig.load(args.qemu.resolve(), args.image.resolve())
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(StageC5Tests))
    raise SystemExit(0 if result.wasSuccessful() else 1)
