"""Verify Stage C.2 confirmed-spec task binding and recovery."""

from __future__ import annotations

import json
import hashlib
import runpy
import sqlite3
import sys
import threading
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from dialogue_coordinator import DialogueCoordinator
from receiver_inbox import Inbox
from task_runtime import TaskError, TaskStore
from task_runtime import encoded

B6 = runpy.run_path(str(ROOT / "scripts/verify-stage-b6.py"))


class StageC2Tests(unittest.TestCase):
    def setUp(self):
        self.b6 = B6["StageB6Tests"]()
        self.b6.setUp()
        self.addCleanup(self.b6.doCleanups)
        self.tasks = TaskStore(Path(self.b6.path).parent / "tasks")

    def coordinator(self, database, task_store=None):
        return DialogueCoordinator(Inbox(database), spec_store=self.b6.store, task_store=task_store)

    def test_confirmation_creates_persisted_task_snapshot_and_duplicate_reuses_it(self):
        event = B6["submit_event"](self.b6.key, self.b6.review)
        with closing(sqlite3.connect(self.b6.path)) as database:
            coordinator = self.coordinator(database, self.tasks)
            coordinator.submit(event)
            receipt = self.b6.head(coordinator)
            task_row = self.tasks.database
            with sqlite3.connect(task_row) as tasks_db:
                binding, = tasks_db.execute("SELECT confirmation_event_id, project_id, spec_version, spec_sha256, task_id FROM confirmation_tasks").fetchall()
                task = tasks_db.execute("SELECT spec_json FROM tasks WHERE id = ?", (binding[4],)).fetchone()
            saved = json.loads(task[0])
            task_view = self.tasks.inspect(binding[4])
            self.assertEqual(event["eventId"], binding[0])
            self.assertEqual(self.b6.key[1], binding[1])
            self.assertEqual(self.b6.review["specVersion"], binding[2])
            self.assertEqual(receipt["savedSpecSha256"], binding[3])
            self.assertEqual(event["eventId"], task_view["confirmationEventId"])
            self.assertEqual("confirmed", saved["status"])
            self.assertEqual(saved, json.loads((self.b6.version).read_text(encoding="utf-8")))
            coordinator.submit(event)
            with sqlite3.connect(task_row) as tasks_db:
                self.assertEqual(1, tasks_db.execute("SELECT count(*) FROM confirmation_tasks").fetchone()[0])

    def test_concurrent_binding_for_same_event_returns_one_task(self):
        event = "fixture-confirmation-event"
        spec = json.loads(self.b6.review["specJson"])
        spec["status"] = "confirmed"
        expected = hashlib.sha256(encoded(spec)).hexdigest()
        results, failures = [], []

        def bind():
            try:
                results.append(self.tasks.bind_confirmed_spec(spec, confirmation_event_id=event,
                                                              expected_sha256=expected, max_cost_micros=1000)["taskId"])
            except Exception as exc:
                failures.append(exc)

        workers = [threading.Thread(target=bind) for _ in range(6)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertFalse(failures)
        self.assertEqual(1, len(set(results)))
        self.assertEqual(6, len(results))
        self.assertEqual(1, len(set(results)))

    def test_saved_spec_recovers_binding_after_interruption(self):
        event = B6["submit_event"](self.b6.key, self.b6.review)
        with closing(sqlite3.connect(self.b6.path)) as database:
            coordinator = self.coordinator(database, self.tasks)
            with patch.object(self.tasks, "bind_confirmed_spec", side_effect=OSError("injected interruption")):
                with self.assertRaises(OSError):
                    coordinator.submit(event)
            self.assertTrue(self.b6.version.exists())
        with closing(sqlite3.connect(self.b6.path)) as database:
            coordinator = self.coordinator(database, self.tasks)
            with sqlite3.connect(self.tasks.database) as tasks_db:
                self.assertEqual(1, tasks_db.execute("SELECT count(*) FROM confirmation_tasks").fetchone()[0])
            self.assertEqual("confirmed", self.b6.head(coordinator)["state"])

    def test_draft_refusal_and_digest_conflict_do_not_bind(self):
        spec = json.loads(self.b6.review["specJson"])
        draft_digest = hashlib.sha256(encoded(spec)).hexdigest()
        with self.assertRaises(TaskError) as draft:
            self.tasks.bind_confirmed_spec(spec, confirmation_event_id="draft-event",
                                           expected_sha256=draft_digest, max_cost_micros=1000)
        self.assertEqual("SPEC_NOT_CONFIRMED", draft.exception.code)
        with self.assertRaises(TaskError) as mismatch:
            self.tasks.bind_confirmed_spec(spec, confirmation_event_id="draft-event",
                                           expected_sha256="0" * 64, max_cost_micros=1000)
        self.assertEqual("SPEC_SHA256_MISMATCH", mismatch.exception.code)
        spec["status"] = "confirmed"
        expected = hashlib.sha256(encoded(spec)).hexdigest()
        self.tasks.bind_confirmed_spec(spec, confirmation_event_id="same-event",
                                       expected_sha256=expected, max_cost_micros=1000)
        with self.assertRaises(TaskError) as conflict:
            self.tasks.bind_confirmed_spec(spec, confirmation_event_id="another-event",
                                           expected_sha256=expected, max_cost_micros=1000)
        self.assertEqual("SPEC_VERSION_CONFLICT", conflict.exception.code)
        with sqlite3.connect(self.tasks.database) as tasks_db:
            self.assertEqual(1, tasks_db.execute("SELECT count(*) FROM confirmation_tasks").fetchone()[0])


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(StageC2Tests))
    raise SystemExit(0 if result.wasSuccessful() else 1)
