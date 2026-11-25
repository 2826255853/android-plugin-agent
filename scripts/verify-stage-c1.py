"""Verify Stage C.1 template bounds and optionally exercise the Android host."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from plugin_generation import GenerationError, generate_files, run_static_tests
from plugin_package import PackageError, build_package, validate_package, validate_source
from spec_workflow import save_confirmed
from task_runtime import TaskStore

QUICK_NOTES = json.loads((ROOT / "desktop-spec/examples/quick-notes-v1.json").read_text(encoding="utf-8"))
CONTACTS = json.loads((ROOT / "desktop-spec/examples/manual-contacts-v1.json").read_text(encoding="utf-8"))


class StageC1Tests(unittest.TestCase):
    def setUp(self):
        self.quick = copy.deepcopy(QUICK_NOTES)
        self.quick["status"] = "confirmed"
        self.contacts = copy.deepcopy(CONTACTS)
        self.contacts["status"] = "confirmed"

    def test_legacy_quick_notes_still_builds(self):
        files = generate_files(self.quick)
        manifest = validate_source(files)
        self.assertEqual("org.generated.quick-notes", manifest["id"])
        self.assertIn("#notes", files["app.js"].decode())

    def test_manual_contacts_profile_generates_declared_workflow_and_limits(self):
        files = generate_files(self.contacts)
        manifest = validate_source(files)
        self.assertEqual("org.generated.manual-contacts", manifest["id"])
        self.assertEqual({"data.read", "data.write"}, set(manifest["capabilities"]))
        self.assertEqual(self.contacts["collections"], manifest["dataModel"]["collections"])
        page = files["index.html"].decode()
        self.assertIn('id="new-contact"', page)
        self.assertIn('id="email" maxlength="254"', page)
        self.assertEqual([
            "confirmed_spec_binding", "manifest_contract", "resource_integrity", "local_resource_references",
            "page_structure", "field_limits", "package_roundtrip",
        ], run_static_tests(files, self.contacts))
        package = build_package(files)
        self.assertEqual(manifest, validate_package(package))
        self.assertEqual(package, build_package(files))

    def test_workflow_and_permission_expansion_are_rejected(self):
        workflow = copy.deepcopy(self.contacts)
        workflow["workflows"][0]["steps"].append("读取系统通讯录")
        permission = copy.deepcopy(self.contacts)
        permission["permissions"].append({"capability": "device.contacts", "justification": "虚构权限"})
        for spec in (workflow, permission):
            with self.subTest(spec=spec["permissions"]):
                with self.assertRaises(GenerationError) as raised:
                    generate_files(spec)
                self.assertEqual("GENERATOR_UNSUPPORTED_SPEC", raised.exception.code)

    def test_protocol_accepts_profile_package_and_rejects_unknown_contacts_capability(self):
        files = generate_files(self.contacts)
        fixture_dir = ROOT / "plugin-protocol/fixtures"
        valid_package = (fixture_dir / "valid-manual-contacts-v1.zip").read_bytes()
        self.assertEqual(build_package(files), valid_package)
        self.assertEqual("org.generated.manual-contacts", validate_package(valid_package)["id"])
        invalid_package = (fixture_dir / "invalid-manual-contacts-unknown-capability-v1.zip").read_bytes()
        with self.assertRaises(PackageError) as raised:
            validate_package(invalid_package)
        self.assertEqual("CAPABILITY_UNKNOWN", raised.exception.code)


def verify_android(serial: str) -> None:
    with tempfile.TemporaryDirectory(prefix="stage-c1-") as temporary:
        work = Path(temporary)
        specs = work / "specs"
        confirmed = json.loads(save_confirmed(copy.deepcopy(CONTACTS), specs).read_text(encoding="utf-8"))
        store = TaskStore(work / "tasks")
        task_id = store.create(confirmed, max_cost_micros=1000)["taskId"]
        from plugin_generation import GenerationPipeline

        report = GenerationPipeline(store).run(task_id)
        if not report["ok"]:
            raise RuntimeError(f"C.1 Android fixture generation failed: {report}")
        package_path = work / "manual-contacts.zip"
        package_path.write_bytes(store.export_build(task_id).read_bytes())
        subprocess.run([
            "node", str(ROOT / "scripts/verify-stage-c1.mjs"), str(package_path), serial,
        ], cwd=ROOT, check=True)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--android", action="store_true")
    parser.add_argument("--serial")
    args = parser.parse_args()
    if args.android and (not args.serial or not args.serial.startswith("emulator-")):
        parser.error("--android requires an explicit Android emulator --serial")
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(StageC1Tests))
    if not result.wasSuccessful():
        return 1
    if args.android:
        verify_android(args.serial)
    print("ALL STAGE C.1 CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
