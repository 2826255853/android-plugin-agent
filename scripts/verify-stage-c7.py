"""End-to-end desktop artifact-registration regression for Stage C.7."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from execution_isolation import SandboxConfig
from model_boundary import Config, MOCK_MODEL, MockProvider
from plugin_delivery import DeliveryError, artifact_record, export_artifact
from plugin_generation import GenerationPipeline
from plugin_repair import RepairCoordinator
from task_runtime import TaskStore


QUICK = json.loads((ROOT / "desktop-spec/examples/quick-notes-v1.json").read_text(encoding="utf-8"))
CONTACTS = json.loads((ROOT / "desktop-spec/examples/manual-contacts-v1.json").read_text(encoding="utf-8"))
PASS_DOM = '<output id="c5-result">PASS</output>'
BUILD_FAILURE = {"code": "ISOLATION_WORKLOAD_FAILED", "exitCode": 1, "output": "",
                 "guestProcesses": 0, "hostProcesses": 0, "hostProcessesStarted": 2}


class StageC7Tests(unittest.TestCase):
    config: SandboxConfig | None = None
    confirmed_snapshot: dict | None = None

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="stage-c7-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = TaskStore(self.root / "tasks")

    def create_task(self, source):
        spec = copy.deepcopy(source)
        spec["status"] = "confirmed"
        return self.store.create(spec, max_cost_micros=100_000)["taskId"]

    @staticmethod
    def pass_isolation(_config, profile, files, **_kwargs):
        return {"code": "OK", "exitCode": 0, "output": PASS_DOM if profile == "preview" else "",
                "guestProcesses": 0, "hostProcesses": 0, "hostProcessesStarted": 2,
                "inputNames": sorted(files)}

    def pipeline(self):
        config = self.config or SandboxConfig(Path("qemu.exe"), Path("image"), "0" * 64, "0" * 64)
        return GenerationPipeline(self.store, config, self.root / "runs")

    def test_templates_build_and_reexport_byte_identical_registered_artifacts(self):
        for spec in (QUICK, CONTACTS):
            with self.subTest(template=spec["collections"][0]["id"]):
                task_id = self.create_task(spec)
                with patch("plugin_generation.run_isolated", side_effect=self.pass_isolation):
                    report = self.pipeline().run(task_id)
                self.assertTrue(report["ok"], report)
                record = artifact_record(self.store, report["artifact"]["sha256"])
                self.assertEqual(task_id, record["taskId"])
                self.assertEqual(report["specSha256"], record["specSha256"])
                self.assertEqual(report["templateVersion"], record["templateVersion"])
                self.assertEqual("built", record["status"])
                self.assertFalse(record["deliverable"])
                self.assertIsNone(record["model"])
                self.assertEqual([], record["modelAccounting"]["calls"])
                destination = self.root / (task_id + "-by-hash.zip")
                export_artifact(self.store, report["artifact"]["sha256"], destination)
                self.assertEqual(self.store.export_build(task_id).read_bytes(), destination.read_bytes())

    def test_repaired_build_registers_model_revision_checks_and_settlement(self):
        task_id = self.create_task(QUICK)
        isolation_calls = 0

        def first_build_fails_then_passes(config, profile, files, **kwargs):
            nonlocal isolation_calls
            isolation_calls += 1
            if isolation_calls == 1:
                return BUILD_FAILURE
            return self.pass_isolation(config, profile, files, **kwargs)

        coordinator = RepairCoordinator(
            self.store, Config("mock", MOCK_MODEL, 100_000_000), MockProvider(), self.pipeline())
        with patch("plugin_generation.run_isolated", side_effect=first_build_fails_then_passes):
            report = coordinator.run(task_id)
        self.assertTrue(report["ok"], report)
        record = artifact_record(self.store, report["artifact"]["sha256"])
        self.assertEqual({"provider": "mock", "modelVersion": MOCK_MODEL,
                          "promptVersion": "plugin-development-v1"}, record["model"])
        self.assertGreater(len(record["sourceRevision"]), 0)
        self.assertEqual(0, record["modelAccounting"]["reservedMicros"])
        self.assertEqual("settled", record["modelAccounting"]["calls"][0]["status"])
        self.assertGreater(record["modelAccounting"]["spentMicros"], 0)
        self.assertTrue(record["checks"])
        self.assertEqual({"build", "test", "preview"}, set(record["isolatedChecks"]))

    def test_android_confirmed_snapshot_binds_task_and_builds(self):
        if self.confirmed_snapshot is None:
            self.skipTest("pass --confirmed-snapshot from verify-stage-b6.py --confirmed-output")
        snapshot = self.confirmed_snapshot
        spec = snapshot["spec"]
        binding = self.store.bind_confirmed_spec(
            spec, confirmation_event_id=snapshot["confirmationEventId"],
            expected_sha256=snapshot["specSha256"], max_cost_micros=100_000)
        self.assertEqual(snapshot["confirmationEventId"], binding["confirmationEventId"])
        task_id = binding["taskId"]
        if self.config is None:
            with patch("plugin_generation.run_isolated", side_effect=self.pass_isolation):
                report = self.pipeline().run(task_id)
        else:
            report = self.pipeline().run(task_id)
        self.assertTrue(report["ok"], report)
        record = artifact_record(self.store, report["artifact"]["sha256"])
        self.assertEqual(spec["projectId"], record["projectId"])
        self.assertEqual(snapshot["confirmationEventId"], record["confirmationEventId"])
        self.assertEqual(snapshot["specSha256"], record["specSha256"])
        self.assertEqual(task_id, record["taskId"])

    def test_failed_required_check_is_not_registered(self):
        task_id = self.create_task(QUICK)
        with patch("plugin_generation.run_isolated", side_effect=lambda _c, _p, _f, **_k: BUILD_FAILURE):
            report = self.pipeline().run(task_id)
        self.assertFalse(report["ok"])
        with self.assertRaises(DeliveryError) as error:
            artifact_record(self.store, "0" * 64)
        self.assertEqual("ARTIFACT_NOT_FOUND", error.exception.code)

    def test_hash_validation_and_existing_export_are_rejected(self):
        task_id = self.create_task(QUICK)
        with patch("plugin_generation.run_isolated", side_effect=self.pass_isolation):
            report = self.pipeline().run(task_id)
        with self.assertRaises(DeliveryError) as invalid:
            artifact_record(self.store, "bad-hash")
        self.assertEqual("INVALID_PACKAGE_HASH", invalid.exception.code)
        destination = self.root / "existing.zip"
        destination.write_bytes(b"preserve")
        with self.assertRaises(DeliveryError) as existing:
            export_artifact(self.store, report["artifact"]["sha256"], destination)
        self.assertEqual("DELIVERY_DESTINATION_EXISTS", existing.exception.code)
        self.assertEqual(b"preserve", destination.read_bytes())

    def test_quick_notes_and_contacts_in_c4_vm(self):
        if self.config is None:
            self.skipTest("pass --qemu and --image to run C.4 isolated VM integration")
        for spec in (QUICK, CONTACTS):
            with self.subTest(template=spec["collections"][0]["id"]):
                task_id = self.create_task(spec)
                report = self.pipeline().run(task_id)
                self.assertTrue(report["ok"], report)
                self.assertTrue(report["packaging"]["deterministic"])
                self.assertIn(PASS_DOM, report["isolatedChecks"]["preview"]["output"])


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", type=Path)
    parser.add_argument("--image", type=Path)
    parser.add_argument("--confirmed-snapshot", type=Path,
                        help="confirmed spec bundle exported by verify-stage-b6.py")
    args, tests = parser.parse_known_args()
    if bool(args.qemu) != bool(args.image):
        parser.error("--qemu and --image must be provided together")
    if args.qemu:
        StageC7Tests.config = SandboxConfig.load(args.qemu.resolve(), args.image.resolve())
    if args.confirmed_snapshot:
        StageC7Tests.confirmed_snapshot = json.loads(args.confirmed_snapshot.read_text(encoding="utf-8"))
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(StageC7Tests))
    raise SystemExit(0 if result.wasSuccessful() else 1)
