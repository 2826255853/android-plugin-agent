"""Repeatable local handoff checks; optional real emulator import acceptance."""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from plugin_delivery import AdbTransport, DeliveryError, export_delivery, push_delivery, verify_delivery
from plugin_generation import GenerationPipeline
from spec_workflow import save_confirmed
from task_runtime import TaskError, TaskStore, encoded


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = json.loads((ROOT / "desktop-spec/examples/quick-notes-v1.json").read_text(encoding="utf-8"))


def build(base: Path, *, unique: bool = False) -> tuple[TaskStore, str]:
    spec = copy.deepcopy(SAMPLE)
    if unique:
        spec["projectId"] = "handoff-" + uuid.uuid4().hex[:12]
    confirmed = save_confirmed(spec, base / "specs")
    store = TaskStore(base / "tasks")
    task_id = store.create(json.loads(confirmed.read_text(encoding="utf-8")), max_cost_micros=100_000)["taskId"]
    assert GenerationPipeline(store).run(task_id)["ok"]
    return store, task_id


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="stage31-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.store, self.task_id = build(self.base)
        self.bundle = self.base / "handoff"

    def export(self):
        return export_delivery(self.store, self.task_id, self.bundle)

    def test_export_reopen_and_copied_bundle_keep_identity_and_bytes(self):
        import shutil
        receipt = self.export()
        copied = self.base / "copied"
        shutil.copytree(self.bundle, copied)
        self.assertEqual(receipt, verify_delivery(TaskStore(self.store.root), self.task_id, copied))
        self.assertEqual(receipt, self.export())
        self.assertFalse(self.store.inspect(self.task_id)["deliverable"])
        self.assertEqual("built", self.store.inspect(self.task_id)["status"])

    def test_editable_artifact_is_not_used_as_snapshot(self):
        (self.store.root / "artifacts" / (self.task_id + ".zip")).write_bytes(b"fictional tamper")
        receipt = self.export()
        self.assertGreater(receipt["sizeBytes"], 100)

    def test_pending_cancelled_failed_tasks_have_no_handoff(self):
        spec = json.loads((self.base / "specs/quick-notes/v1.json").read_text(encoding="utf-8"))
        for status in ("ready", "cancelled", "failed", "stopped"):
            task_id = self.store.create(spec, max_cost_micros=100_000)["taskId"]
            with self.store._transaction() as db:
                db.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
            with self.subTest(status=status), self.assertRaisesRegex(DeliveryError, "BUILD_NOT_AVAILABLE"):
                export_delivery(self.store, task_id, self.bundle)
        self.assertFalse(self.bundle.exists())

    def test_receipt_identity_version_hash_and_claim_tampering_rejected(self):
        receipt = self.export()
        for key, value in (("pluginId", "org.fictional.other"), ("pluginVersion", "9.0.0"),
                           ("packageSha256", "0" * 64), ("deliverable", True), ("packageFile", "../escape.zip")):
            altered = {**receipt, key: value}
            (self.bundle / "delivery.json").write_bytes(encoded(altered) + b"\n")
            with self.subTest(key=key), self.assertRaisesRegex(DeliveryError, "RECEIPT_MISMATCH"):
                verify_delivery(self.store, self.task_id, self.bundle)

    def test_package_tampering_or_truncation_rejected(self):
        receipt = self.export()
        path = self.bundle / receipt["packageFile"]
        for raw in (b"", path.read_bytes()[:-1], b"not a ZIP"):
            path.write_bytes(raw)
            with self.assertRaisesRegex(DeliveryError, "PACKAGE_HASH_MISMATCH"):
                verify_delivery(self.store, self.task_id, self.bundle)

    def test_unrelated_destination_is_never_overwritten(self):
        self.bundle.mkdir()
        keep = self.bundle / "keep.txt"
        keep.write_text("fictional unrelated file")
        with self.assertRaisesRegex(DeliveryError, "DELIVERY_FILES_MISMATCH"):
            self.export()
        self.assertEqual("fictional unrelated file", keep.read_text())

    def test_linked_file_is_rejected(self):
        receipt = self.export()
        path = self.bundle / receipt["packageFile"]
        os.link(path, self.base / "linked.zip")
        with self.assertRaisesRegex(TaskError, "WORKSPACE_ESCAPE"):
            verify_delivery(self.store, self.task_id, self.bundle)

    def test_store_snapshot_and_spec_binding_corruption_rejected(self):
        with self.store._transaction() as db:
            db.execute("UPDATE tasks SET spec_sha256 = ? WHERE id = ?", ("0" * 64, self.task_id))
        with self.assertRaisesRegex(DeliveryError, "BUILD_BINDING_MISMATCH"):
            self.export()

    def test_corrupt_stored_package_rejected_before_export(self):
        with self.store._transaction() as db:
            db.execute("UPDATE builds SET package = ? WHERE task_id = ?", (b"fictional corrupt snapshot", self.task_id))
        with self.assertRaisesRegex(DeliveryError, "PACKAGE_HASH_MISMATCH"):
            self.export()
        self.assertFalse(self.bundle.exists())

    def test_other_valid_package_cannot_replace_bound_package(self):
        receipt = self.export()
        (self.bundle / receipt["packageFile"]).write_bytes(
            (ROOT / "plugin-protocol/fixtures/valid-known-capability-v1.zip").read_bytes())
        with self.assertRaisesRegex(DeliveryError, "PACKAGE_HASH_MISMATCH"):
            verify_delivery(self.store, self.task_id, self.bundle)

    def test_missing_static_checks_rejected(self):
        with self.store._transaction() as db:
            row = db.execute("SELECT seq, report_json FROM builds WHERE task_id = ?", (self.task_id,)).fetchone()
            report = json.loads(row["report_json"])
            report["checks"] = []
            db.execute("UPDATE builds SET report_json = ? WHERE seq = ?", (encoded(report).decode(), row["seq"]))
        with self.assertRaisesRegex(DeliveryError, "BUILD_CHECKS_MISMATCH"):
            self.export()

    def test_device_selection_offline_unauthorized_and_physical_guard(self):
        for listing in ("List of devices attached\n", "chosen\toffline", "chosen\tunauthorized", "other\tdevice"):
            with patch.object(AdbTransport, "run", return_value=listing):
                with self.assertRaisesRegex(DeliveryError, "DEVICE_NOT_READY"):
                    AdbTransport("adb", "chosen")
        with patch.object(AdbTransport, "run", side_effect=["chosen\tdevice", "0"]):
            with self.assertRaisesRegex(DeliveryError, "PHYSICAL_DEVICE_REQUIRES_OPT_IN"):
                AdbTransport("adb", "chosen")
        with patch.object(AdbTransport, "run", side_effect=["chosen\tdevice", "1"]):
            self.assertEqual("chosen", AdbTransport("adb", "chosen").serial)
        with self.assertRaisesRegex(DeliveryError, "INVALID_DEVICE_SERIAL"):
            AdbTransport("adb", "device;reboot")

    def test_transport_hash_mismatch_never_reports_import_or_success(self):
        from unittest.mock import Mock
        self.export()
        transport = Mock(serial="fictional-emulator")
        transport.remote_hash.return_value = "0" * 64
        with self.assertRaisesRegex(DeliveryError, "DEVICE_HASH_MISMATCH"):
            push_delivery(self.store, self.task_id, self.bundle, transport)
        self.assertFalse(any(call.args[:3] == ("shell", "mv", "-n") for call in transport.run.call_args_list))
        self.assertEqual(("shell", "rm", "-f"), transport.run.call_args.args[:3])

    def test_existing_device_file_conflict_fails_after_no_clobber_move(self):
        from unittest.mock import Mock
        receipt = self.export()
        transport = Mock(serial="fictional-emulator")
        transport.remote_hash.side_effect = [receipt["packageSha256"], "0" * 64]
        with self.assertRaisesRegex(DeliveryError, "DEVICE_HASH_MISMATCH"):
            push_delivery(self.store, self.task_id, self.bundle, transport)
        self.assertTrue(any(call.args[:3] == ("shell", "mv", "-n") for call in transport.run.call_args_list))

    def test_verified_transfer_does_not_claim_installation(self):
        from unittest.mock import Mock
        receipt = self.export()
        transport = Mock(serial="fictional-emulator")
        transport.remote_hash.return_value = receipt["packageSha256"]
        result = push_delivery(self.store, self.task_id, self.bundle, transport)
        self.assertEqual("transferred", result["status"])
        self.assertFalse(result["imported"])
        self.assertFalse(result["deliverable"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--android", action="store_true", help="Also import a generated package through the emulator system picker")
    parser.add_argument("--serial", help="Required with --android; only emulator serials accepted")
    args = parser.parse_args()
    if args.android and (not args.serial or not args.serial.startswith("emulator-")):
        parser.error("--android requires an explicit emulator --serial")
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(HandoffTests))
    if not result.wasSuccessful():
        return 1
    if args.android:
        with tempfile.TemporaryDirectory(prefix="stage31-device-") as temporary:
            base = Path(temporary)
            store, task_id = build(base, unique=True)
            directory = base / "handoff"
            export_delivery(store, task_id, directory)
            subprocess.run(["node", str(ROOT / "scripts/verify-stage31.mjs"), str(store.root), task_id,
                            str(directory), args.serial], cwd=ROOT, check=True)
    print("ALL STAGE 3.1 CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
