"""Stage 2.2 acceptance: persistent isolation, bounded tools and stop evidence.

All data is fictional. No network, real model, generated code or Android device
is used. Temporary stores are removed by TemporaryDirectory after each check.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from spec_workflow import save_confirmed
from task_runtime import FILE_BYTES, TaskError, TaskStore


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = json.loads((ROOT / "desktop-spec/examples/quick-notes-v1.json").read_text(encoding="utf-8"))


class Stage22Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="stage22-")
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.now = 1000.0
        self.store = TaskStore(self.base / "tasks", clock=lambda: self.now)
        self.confirmed_path = save_confirmed(copy.deepcopy(SAMPLE), self.base / "specs")
        self.spec = json.loads(self.confirmed_path.read_text(encoding="utf-8"))

    def create(self, **limits):
        return self.store.create(self.spec, max_cost_micros=limits.pop("max_cost_micros", 1000), **limits)["taskId"]

    def running(self, **limits):
        task_id = self.create(**limits)
        self.assertTrue(self.store.transition(task_id, "generating")["ok"])
        return task_id

    def patch(self, task_id, path="index.html", content="fictional sample", expected=None):
        return self.store.tool(task_id, "apply_patch", {"path": path, "expectedSha256": expected, "content": content})

    def assert_stopped(self, task_id, code):
        state = self.store.inspect(task_id)
        self.assertEqual("stopped", state["status"])
        self.assertEqual(code, state["stopCode"])
        self.assertFalse(state["deliverable"])
        self.assertTrue(any(event["code"] == code for event in state["events"]))
        self.assertEqual("TASK_TERMINAL", self.patch(task_id, "after.txt")["code"])
        self.assertFalse((self.store.workspaces / task_id / "after.txt").exists())

    def test_confirmed_spec_binding_and_independent_ids(self):
        one, two = self.create(), self.create()
        self.assertNotEqual(one, two)
        read = self.store.inspect(one)
        self.assertEqual(("quick-notes", 1, "ready"), (read["projectId"], read["specVersion"], read["status"]))
        with self.assertRaisesRegex(TaskError, "SPEC_NOT_CONFIRMED"):
            self.store.create(SAMPLE, max_cost_micros=1000)
        changed = copy.deepcopy(self.spec)
        changed["goal"] = "另一个虚构目标"
        with self.assertRaisesRegex(TaskError, "SPEC_VERSION_CONFLICT"):
            self.store.create(changed, max_cost_micros=1000)
        changed["version"] = 2
        self.store.create(changed, max_cost_micros=1000)
        self.spec["goal"] = "外部字典修改"
        self.store.transition(one, "generating")
        snapshot = self.store.tool(one, "read_spec", {})["result"]["spec"]
        self.assertEqual("快速创建并查看保存在本机的简短笔记。", snapshot["goal"])

    def test_invalid_limits_are_rejected_before_task_creation(self):
        for value in (0, -1, True, 0.1, None):
            with self.subTest(value=value), self.assertRaisesRegex(TaskError, "INVALID_COST_LIMIT"):
                self.store.create(self.spec, max_cost_micros=value)
        for field, value in (("max_duration_seconds", 601), ("max_duration_seconds", 0),
                             ("max_repair_rounds", 4), ("max_repair_rounds", -1), ("max_tool_calls", 129)):
            with self.subTest(field=field), self.assertRaises(TaskError):
                self.create(**{field: value})

    def test_lifecycle_rejects_publish_and_terminal_restart(self):
        task_id = self.create()
        self.assertEqual("TASK_NOT_RUNNING", self.patch(task_id)["code"])
        self.store.transition(task_id, "generating")
        self.store.transition(task_id, "validating")
        for target in ("deliverable", "preview_ready", "ready"):
            self.assertEqual("STATUS_NOT_ALLOWED", self.store.transition(task_id, target)["code"])
        self.store.transition(task_id, "failed")
        self.assertEqual("TASK_TERMINAL", self.store.transition(task_id, "generating")["code"])
        self.assertFalse(self.store.inspect(task_id)["deliverable"])

    def test_task_id_validation_does_not_create_external_paths(self):
        for task_id in ("../outside", "task-" + "0" * 31, "C:/outside", "task-" + "a" * 32):
            with self.subTest(task_id=task_id), self.assertRaises(TaskError):
                self.patch(task_id)
        self.assertFalse((self.base / "outside").exists())

    def test_atomic_patch_conflict_and_fixed_tools(self):
        task_id = self.running()
        first = self.patch(task_id, content="first")
        self.assertTrue(first["ok"])
        self.assertEqual("PATCH_CONFLICT", self.patch(task_id, content="second")["code"])
        second = self.patch(task_id, content="second", expected=first["result"]["sha256"])
        self.assertTrue(second["ok"])
        read = self.store.tool(task_id, "read_file", {"path": "index.html"})
        self.assertEqual("second", read["result"]["content"])
        self.assertEqual(hashlib.sha256(b"second").hexdigest(), read["result"]["sha256"])
        self.assertTrue(self.patch(task_id, "spec.json", json.dumps(self.spec))["ok"])
        self.assertTrue(self.store.tool(task_id, "validate_spec", {"path": "spec.json"})["result"]["valid"])
        self.assertFalse(self.store.tool(task_id, "run_tests", {"suite": "spec"})["result"]["executedGeneratedCode"])

    def test_cross_task_files_cannot_be_read(self):
        one, two = self.running(), self.running()
        self.patch(one, content="fictional private task data")
        self.assertEqual("FILE_NOT_FOUND", self.store.tool(two, "read_file", {"path": "index.html"})["code"])
        self.assertEqual("WORKSPACE_ESCAPE", self.store.tool(two, "read_file", {"path": f"../{one}/index.html"})["code"])
        self.assert_stopped(two, "WORKSPACE_ESCAPE")
        self.assertEqual("generating", self.store.inspect(one)["status"])

    def test_agent_task_identity_cannot_override_coordinator_context(self):
        one, two = self.running(), self.running()
        result = self.store.tool(one, "read_spec", {}, request_task_id=two)
        self.assertEqual("TASK_ID_MISMATCH", result["code"])
        self.assert_stopped(one, "TASK_ID_MISMATCH")
        self.assertEqual("generating", self.store.inspect(two)["status"])

    def test_invalid_requests_consume_tool_budget(self):
        task_id = self.running(max_tool_calls=1)
        self.assertEqual("INVALID_TOOL_INPUT", self.store.tool(task_id, "read_file", [])["code"])
        self.assertEqual("TOOL_LIMIT", self.store.tool(task_id, "read_spec", {})["code"])
        self.assert_stopped(task_id, "TOOL_LIMIT")

    def test_source_revisions_survive_cancellation_without_entering_event_logs(self):
        task_id = self.running()
        first = self.patch(task_id, content="fictional first source")
        self.patch(task_id, content="fictional second source", expected=first["result"]["sha256"])
        self.store.cancel(task_id)
        state = self.store.inspect(task_id)
        self.assertEqual(2, len(state["patches"]))
        self.assertEqual(first["result"]["sha256"], state["patches"][1]["before_sha256"])
        self.assertNotIn("fictional first source", json.dumps(state))
        with self.store._transaction() as db:
            patch = db.execute("SELECT before_content, after_content FROM patches WHERE task_id = ? ORDER BY seq DESC",
                               (task_id,)).fetchone()
        self.assertEqual((b"fictional first source", b"fictional second source"), tuple(patch))

    def test_failure_fingerprint_includes_file_and_check_and_security_stops_first_round(self):
        task_id = self.running()
        self.store.record_failure(task_id, "validating", "FICTIONAL_ERROR", path="a.js", check="syntax")
        self.store.begin_repair(task_id)
        self.store.record_failure(task_id, "validating", "FICTIONAL_ERROR", path="b.js", check="syntax")
        self.assertEqual("generating", self.store.inspect(task_id)["status"])
        self.store.begin_repair(task_id)
        self.store.record_failure(task_id, "validating", "RESOURCE_HASH_MISMATCH", path="b.js", check="hash")
        self.assert_stopped(task_id, "RESOURCE_HASH_MISMATCH")
        task_id = self.running()
        self.store.record_failure(task_id, "validating", "FICTIONAL_ERROR")
        self.store.record_failure(task_id, "validating", "PERMISSION_DENIED")
        self.assert_stopped(task_id, "PERMISSION_DENIED")

    def test_elapsed_time_freezes_at_stop_even_with_late_billing(self):
        task_id = self.running()
        call_id = self.store.reserve_model_call(task_id, 10)["callId"]
        self.now = 1003.0
        self.store.cancel(task_id)
        self.now = 1100.0
        self.store.settle_model_call(task_id, call_id, 5)
        self.assertEqual(3.0, self.store.inspect(task_id)["elapsedSeconds"])

    def test_path_traversal_windows_aliases_and_urls_stop_immediately(self):
        for path in ("../outside.txt", "/outside.txt", "C:/outside.txt", "C:outside.txt", "a\\outside.txt",
                     "a/../../outside.txt", "http://127.0.0.1/a", "//server/share", "a.txt:stream",
                     "a/./file.txt", "a//file.txt", "con.txt", "nul", "com1/file.txt", "file.txt.",
                     "%2e%2e/outside.txt", ".git/config"):
            with self.subTest(path=path):
                task_id = self.running()
                self.assertEqual("WORKSPACE_ESCAPE", self.patch(task_id, path)["code"])
                self.assert_stopped(task_id, "WORKSPACE_ESCAPE")

    def test_hardlink_does_not_expose_or_modify_external_file(self):
        task_id = self.running()
        outside = self.base / "outside.txt"
        outside.write_text("fictional external content", encoding="utf-8")
        os.link(outside, self.store.workspaces / task_id / "linked.txt")
        self.assertEqual("WORKSPACE_ESCAPE", self.store.tool(task_id, "read_file", {"path": "linked.txt"})["code"])
        self.assert_stopped(task_id, "WORKSPACE_ESCAPE")
        self.assertEqual("fictional external content", outside.read_text(encoding="utf-8"))

    def test_directory_link_does_not_expose_external_files(self):
        task_id = self.running()
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "private.txt").write_text("fictional external content", encoding="utf-8")
        link = self.store.workspaces / task_id / "linked"
        if os.name == "nt":
            # A junction requires no symlink privilege. This trusted test fixture
            # is created by the harness, never by the agent tool dispatcher.
            result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(outside)],
                                    capture_output=True, check=False)
            self.assertEqual(0, result.returncode, result.stdout.decode(errors="replace"))
        else:
            link.symlink_to(outside, target_is_directory=True)
        try:
            self.assertEqual("WORKSPACE_ESCAPE", self.store.tool(task_id, "read_file", {"path": "linked/private.txt"})["code"])
            self.assert_stopped(task_id, "WORKSPACE_ESCAPE")
        finally:
            if os.name == "nt":
                link.rmdir()  # Remove the checked fixture junction only, never recurse.
            else:
                link.unlink()
        self.assertTrue((outside / "private.txt").exists())

    def test_network_shell_publish_and_custom_tests_are_not_tools(self):
        for name in ("shell", "exec", "network", "fetch", "publish", "reserve_model_call", "transition"):
            with self.subTest(name=name):
                task_id = self.running()
                self.assertEqual("TOOL_NOT_ALLOWED", self.store.tool(task_id, name, {})["code"])
                self.assert_stopped(task_id, "TOOL_NOT_ALLOWED")
        task_id = self.running()
        self.assertEqual("TOOL_NOT_ALLOWED", self.store.tool(task_id, "run_tests", {"suite": "node app.js"})["code"])
        self.assert_stopped(task_id, "TOOL_NOT_ALLOWED")

    def test_malformed_input_and_sensitive_text_are_not_logged(self):
        task_id = self.running()
        for params in ([], {"path": "index.html", "extra": "fictional secret placeholder"}, {"path": None}):
            self.assertFalse(self.store.tool(task_id, "read_file", params)["ok"])
            if self.store.inspect(task_id)["status"] == "stopped":
                break
        self.assertNotIn("fictional secret placeholder", json.dumps(self.store.inspect(task_id)))
        task_id = self.running()
        self.assertEqual("TOOL_NOT_ALLOWED", self.store.tool(task_id, [], {})["code"])

    def test_file_workspace_and_tool_quotas(self):
        task_id = self.running()
        self.assertEqual("FILE_LIMIT", self.patch(task_id, content="x" * (FILE_BYTES + 1))["code"])
        self.assert_stopped(task_id, "FILE_LIMIT")
        task_id = self.running()
        for index in range(8):
            self.assertTrue(self.patch(task_id, f"file{index}.txt", "x" * FILE_BYTES)["ok"])
        self.assertEqual("WORKSPACE_LIMIT", self.patch(task_id, "overflow.txt", "x")["code"])
        self.assert_stopped(task_id, "WORKSPACE_LIMIT")
        task_id = self.running()
        for index in range(64):
            self.assertTrue(self.patch(task_id, f"file{index}.txt", "")["ok"])
        self.assertEqual("WORKSPACE_LIMIT", self.patch(task_id, "overflow.txt", "")["code"])
        self.assert_stopped(task_id, "WORKSPACE_LIMIT")
        task_id = self.running(max_tool_calls=1)
        self.store.tool(task_id, "read_spec", {})
        self.assertEqual("TOOL_LIMIT", self.patch(task_id)["code"])
        self.assert_stopped(task_id, "TOOL_LIMIT")

    def test_timeout_at_exact_deadline_and_supervisor_sweep(self):
        task_id = self.running(max_duration_seconds=10)
        self.now = 1009.9
        self.assertTrue(self.patch(task_id)["ok"])
        self.now = 1010.0
        self.store.sweep()
        self.assert_stopped(task_id, "TIME_LIMIT")
        self.assertTrue((self.store.workspaces / task_id / "index.html").exists())

    def test_patch_expiring_before_commit_has_no_file_effect(self):
        task_id = self.running(max_duration_seconds=10)
        original_dispatch = self.store._dispatch

        def delayed(db, task, name, params):
            self.now = 1010.0
            return original_dispatch(db, task, name, params)

        self.store._dispatch = delayed
        self.assertEqual("TIME_LIMIT", self.patch(task_id)["code"])
        self.assertFalse((self.store.workspaces / task_id / "index.html").exists())
        self.assert_stopped(task_id, "TIME_LIMIT")

    def test_cancel_keeps_diagnostics_and_prevents_further_writes(self):
        task_id = self.running()
        self.patch(task_id)
        self.store.record_failure(task_id, "validating", "FICTIONAL_BUILD_ERROR")
        self.store.cancel(task_id)
        state = self.store.inspect(task_id)
        self.assertEqual("cancelled", state["status"])
        self.assertTrue(any(event["code"] == "FILE_PATCHED" for event in state["events"]))
        self.assertTrue(any(event["code"] == "ROUND_FAILED" for event in state["events"]))
        self.assertEqual("TASK_TERMINAL", self.patch(task_id, "after.txt")["code"])
        self.store.transition(task_id, "generating")
        self.assertEqual("cancelled", self.store.inspect(task_id)["status"])

    def test_same_failure_in_two_rounds_stops_and_requires_repair_tracking(self):
        task_id = self.running()
        self.store.record_failure(task_id, "validating", "FICTIONAL_BUILD_ERROR")
        self.assertEqual("REPAIR_REQUIRED", self.patch(task_id)["code"])
        self.assertEqual("ROUND_ALREADY_RECORDED", self.store.record_failure(task_id, "validating", "FICTIONAL_BUILD_ERROR")["code"])
        self.assertTrue(self.store.begin_repair(task_id)["ok"])
        self.store.record_failure(task_id, "validating", "FICTIONAL_BUILD_ERROR")
        self.assert_stopped(task_id, "REPEATED_ERROR")

    def test_distinct_failures_stop_at_repair_limit(self):
        task_id = self.running()
        for index in range(4):
            self.store.record_failure(task_id, "validating", f"FICTIONAL_ERROR_{index}")
            if index < 3:
                self.assertTrue(self.store.begin_repair(task_id)["ok"])
        self.assertEqual(3, self.store.inspect(task_id)["repairRounds"])
        self.assert_stopped(task_id, "REPAIR_LIMIT")
        task_id = self.running(max_repair_rounds=0)
        self.store.record_failure(task_id, "generating", "FICTIONAL_ERROR")
        self.assert_stopped(task_id, "REPAIR_LIMIT")

    def test_budget_reserves_before_dispatch_and_exact_limit_stops(self):
        task_id = self.running(max_cost_micros=10)
        call = self.store.reserve_model_call(task_id, 10)
        self.assertTrue(call["ok"])
        self.assertEqual(10, self.store.inspect(task_id)["reservedMicros"])
        self.store.settle_model_call(task_id, call["callId"], 10)
        self.assert_stopped(task_id, "COST_LIMIT")
        task_id = self.running(max_cost_micros=10)
        self.assertEqual("COST_LIMIT", self.store.reserve_model_call(task_id, 11)["code"])
        self.assertEqual([], self.store.inspect(task_id)["modelCalls"])
        self.assert_stopped(task_id, "COST_LIMIT")

    def test_concurrent_reservations_cannot_overspend_budget(self):
        task_id = self.running(max_cost_micros=10)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.store.reserve_model_call(task_id, 6), range(2)))
        self.assertEqual(1, sum(result["ok"] for result in results))
        self.assertEqual(6, self.store.inspect(task_id)["reservedMicros"])
        self.assert_stopped(task_id, "COST_LIMIT")

    def test_late_settlement_is_idempotent_and_cannot_resume_cancelled_task(self):
        task_id = self.running(max_cost_micros=100)
        call_id = self.store.reserve_model_call(task_id, 50)["callId"]
        self.store.cancel(task_id)
        self.store.settle_model_call(task_id, call_id, 25)
        self.store.settle_model_call(task_id, call_id, 25)
        state = self.store.inspect(task_id)
        self.assertEqual((25, 0, "cancelled"), (state["spentMicros"], state["reservedMicros"], state["status"]))
        with self.assertRaisesRegex(TaskError, "CALL_ALREADY_SETTLED"):
            self.store.settle_model_call(task_id, call_id, 20)
        with self.assertRaisesRegex(TaskError, "INVALID_COST"):
            self.store.settle_model_call(task_id, call_id, -1)

    def test_cost_bound_breach_retains_actual_spend_and_stops(self):
        task_id = self.running(max_cost_micros=100)
        call_id = self.store.reserve_model_call(task_id, 10)["callId"]
        self.store.settle_model_call(task_id, call_id, 11)
        self.assertEqual(11, self.store.inspect(task_id)["spentMicros"])
        self.assert_stopped(task_id, "COST_BOUND_BREACH")

    def test_reopen_and_recover_keep_files_spec_events_and_unsettled_cost(self):
        task_id = self.running()
        self.patch(task_id)
        self.store.reserve_model_call(task_id, 20)
        second = TaskStore(self.base / "tasks", clock=lambda: self.now)
        self.assertEqual("generating", second.inspect(task_id)["status"])
        second.sweep(recover=True)
        self.assert_stopped(task_id, "PROCESS_INTERRUPTED")
        state = second.inspect(task_id)
        self.assertEqual(20, state["reservedMicros"])
        self.assertEqual(1, state["specVersion"])
        self.assertTrue((second.workspaces / task_id / "index.html").exists())

    def test_model_call_count_is_bounded_even_when_cost_is_zero(self):
        task_id = self.running()
        for _ in range(32):
            call_id = self.store.reserve_model_call(task_id, 1)["callId"]
            self.store.settle_model_call(task_id, call_id, 0)
        self.assertEqual("MODEL_CALL_LIMIT", self.store.reserve_model_call(task_id, 1)["code"])
        self.assert_stopped(task_id, "MODEL_CALL_LIMIT")

    def test_cli_confirm_create_and_read_persisted_state(self):
        runtime = ROOT / "scripts/task_runtime.py"
        args = [sys.executable, str(runtime), "--store", str(self.base / "cli-tasks")]
        create = subprocess.run([*args, "create", str(self.confirmed_path), "--max-cost-micros", "1000"],
                                capture_output=True, text=True, encoding="utf-8", check=True)
        state = json.loads(create.stdout)
        task_id = state["taskId"]
        for command in (["transition", task_id, "generating"], ["tool", task_id, "read_spec", "{}"],
                        ["cancel", task_id], ["show", task_id]):
            result = subprocess.run([*args, *command], capture_output=True, text=True, encoding="utf-8", check=True)
            state = json.loads(result.stdout)
        self.assertEqual("cancelled", state["status"])
        self.assertFalse(state["deliverable"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
