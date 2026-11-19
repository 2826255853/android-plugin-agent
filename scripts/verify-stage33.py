"""Stage 3.3 specification revision and emulator rollback verification."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from plugin_delivery import DeliveryError, export_delivery
from plugin_generation import GenerationPipeline
from project_history import project_history
from spec_workflow import SpecError, load_json, revise, review_spec, save_confirmed
from task_runtime import TaskStore


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "desktop-spec/examples/quick-notes-v1.json"


def confirm(draft: dict, specs: Path) -> dict:
    review = review_spec(draft, specs)
    assert not review["questions"] and review["reviewSha256"]
    return load_json(save_confirmed(draft, specs, review_sha256=review["reviewSha256"]))


def build(store: TaskStore, spec: dict) -> str:
    task_id = store.create(spec, max_cost_micros=100000, max_repair_rounds=0)["taskId"]
    GenerationPipeline(store).run(task_id)
    return task_id


class RevisionTests(unittest.TestCase):
    def setUp(self) -> None:
        work = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="stage33-")))
        self.specs = work / "specs"
        self.tasks = TaskStore(work / "tasks")
        self.project = "handoff-" + uuid4().hex[:12]
        self.original = save_confirmed(dict(load_json(SAMPLE), projectId=self.project), self.specs)

    def revision(self, version: int = 1) -> dict:
        return revise(self.specs, self.project, version)

    def test_clarification_and_confirmation_match_the_reviewed_draft(self) -> None:
        draft = self.revision()
        draft["storesData"] = None
        review = review_spec(draft, self.specs)
        self.assertIn("storesData", [item["field"] for item in review["questions"]])
        self.assertIsNone(review["reviewSha256"])
        draft["storesData"] = True
        self.assertIsNone(review_spec(draft, self.specs)["reviewSha256"])  # No changes yet.
        draft["title"] = "Fictional Notes B"
        review = review_spec(draft, self.specs)
        self.assertEqual(review["changes"], [{"field": "title", "before": "Quick Notes", "after": draft["title"]}])
        with self.assertRaisesRegex(SpecError, "reviewSha256"):
            save_confirmed(draft, self.specs)
        draft["title"] = "Fictional Notes C"
        with self.assertRaisesRegex(SpecError, "reviewSha256"):
            save_confirmed(draft, self.specs, review_sha256=review["reviewSha256"])
        self.assertEqual(confirm(draft, self.specs)["title"], "Fictional Notes C")

    def test_old_base_shows_changes_against_the_latest_and_stale_review_fails(self) -> None:
        second = self.revision()
        second["title"] = "Fictional Notes B"
        competing = dict(second, title="Competing title")
        token = review_spec(competing, self.specs)["reviewSha256"]
        confirm(second, self.specs)
        with self.assertRaisesRegex(SpecError, "next version"):
            save_confirmed(competing, self.specs, review_sha256=token)
        restore = self.revision(1)
        review = review_spec(restore, self.specs)
        self.assertEqual(review["baseChanges"], [])
        self.assertEqual(review["changes"], [{"field": "title", "before": second["title"], "after": "Quick Notes"}])
        self.assertEqual(confirm(restore, self.specs)["revision"]["baseVersion"], 1)

    def test_revision_rejects_changed_base(self) -> None:
        draft = self.revision()
        self.original.write_text(json.dumps(dict(load_json(self.original), title="Changed base")), encoding="utf-8")
        with self.assertRaisesRegex(SpecError, "base has changed"):
            review_spec(draft, self.specs)
        with self.assertRaises(SpecError):
            revise(self.specs, "../quick-notes", 1)

    def test_two_builds_and_failed_revision_preserve_usable_history(self) -> None:
        original_bytes = self.original.read_bytes()
        task_ids = [build(self.tasks, load_json(self.original))]
        for version in (2, 3):
            draft = self.revision(version - 1)
            draft["title"] = f"Fictional Notes {version}"
            task_ids.append(build(self.tasks, confirm(draft, self.specs)))
        draft = self.revision(3)
        draft["goal"] = "Fictional request outside the available template."
        failed = build(self.tasks, confirm(draft, self.specs))
        failed_task = self.tasks.inspect(failed)
        self.assertEqual(failed_task["status"], "stopped")
        self.assertEqual(failed_task["builds"][-1]["code"], "GENERATOR_UNSUPPORTED_SPEC")
        with self.assertRaises(DeliveryError):
            export_delivery(self.tasks, failed, self.specs.parent / "not-created")
        history = project_history(self.project, self.specs, self.tasks.root)
        self.assertEqual(history["latestUsableBuild"]["taskId"], task_ids[-1])
        self.assertEqual([v["tasks"][0]["status"] for v in history["versions"]], ["built"] * 3 + ["stopped"])
        self.assertEqual([v["revision"]["baseVersion"] for v in history["versions"][1:]], [1, 2, 3])
        self.assertEqual(self.original.read_bytes(), original_bytes)


def verify_android(serial: str) -> None:
    with tempfile.TemporaryDirectory(prefix="stage33-device-") as temporary:
        work = Path(temporary)
        specs = work / "specs"
        store = TaskStore(work / "tasks")
        project = "handoff-" + uuid4().hex[:12]
        first = dict(load_json(SAMPLE), projectId=project, title="Fictional Notes 1")
        current = load_json(save_confirmed(first, specs))
        deliveries = []
        for version in (1, 2, 3, 4):
            if version != 1:
                draft = revise(specs, project, version - 1)
                draft["title"] = f"Fictional Notes {version}"
                if version == 4:  # Valid package whose model tightens an existing field.
                    draft["collections"][0]["fields"][0]["maxLength"] = 12
                current = confirm(draft, specs)
            task_id = build(store, current)
            directory = work / f"delivery-{version}"
            export_delivery(store, task_id, directory)
            deliveries += [task_id, str(directory)]
        subprocess.run(["node", str(ROOT / "scripts/verify-stage31.mjs"), str(store.root),
                        *deliveries[:2], serial, *deliveries[2:]], cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--android", action="store_true", help="also test through the emulator system picker")
    parser.add_argument("--serial", help="explicit emulator serial required with --android")
    args = parser.parse_args()
    if args.android and (not args.serial or not args.serial.startswith("emulator-")):
        parser.error("--android requires an explicit emulator --serial")
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RevisionTests))
    if not result.wasSuccessful():
        return 1
    if args.android:
        verify_android(args.serial)
    print("ALL STAGE 3.3 CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
