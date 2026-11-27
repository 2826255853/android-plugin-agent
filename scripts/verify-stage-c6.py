"""Fixed-provider and recovery regression for Stage C.6."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from execution_isolation import SandboxConfig
from model_boundary import Config, MOCK_MODEL, MockProvider, ProviderReply, Usage
from plugin_generation import GenerationPipeline
from plugin_repair import RepairCoordinator
from task_runtime import TaskStore


QUICK = json.loads((ROOT / "desktop-spec/examples/quick-notes-v1.json").read_text(encoding="utf-8"))
PASS_DOM = '<output id="c5-result">PASS</output>'
BUILD_FAILURE = {"code": "ISOLATION_WORKLOAD_FAILED", "exitCode": 1, "output": "",
                 "guestProcesses": 0, "hostProcesses": 0, "hostProcessesStarted": 2}


class FixedProvider:
    def __init__(self, action=None):
        self.action = action
        self.calls = 0

    def invoke(self, request, stop):
        self.calls += 1
        if self.action:
            return self.action(request, stop)
        payload = json.loads(request.input_text)
        source = payload["files"]["app.js"]
        response = {"version": "plugin-development-v1", "patches": [{
            "plan": "Retain the current implementation while addressing the reported check.",
            "specRef": {"type": "acceptanceCriteria", "id": "save_note"},
            "path": "app.js", "expectedSha256": source["sha256"],
            "replacement": source["content"] + "\n/* C6 fixed response */\n",
            "expectedChecks": ["resource_integrity"],
        }]}
        return ProviderReply(json.dumps(response), Usage(100, 50))


class StageC6Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stage-c6-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = TaskStore(self.root / "tasks")
        self.config = SandboxConfig(Path("qemu.exe"), Path("image"), "0" * 64, "0" * 64)
        self.iso_calls = 0

    def create_task(self, *, max_cost=100_000, max_rounds=3):
        spec = copy.deepcopy(QUICK)
        spec["status"] = "confirmed"
        return self.store.create(spec, max_cost_micros=max_cost, max_repair_rounds=max_rounds)["taskId"]

    @staticmethod
    def pass_isolation(_config, profile, files, **_kwargs):
        return {"code": "OK", "exitCode": 0, "output": PASS_DOM if profile == "preview" else "",
                "guestProcesses": 0, "hostProcesses": 0, "hostProcessesStarted": 2,
                "inputNames": sorted(files)}

    def pipeline(self):
        return GenerationPipeline(self.store, self.config, self.root / "runs")

    def run_repair(self, task_id, provider, outcomes):
        def isolation(config, profile, files, **kwargs):
            if outcomes:
                value = outcomes.pop(0)
                if value is not None:
                    return value
            return self.pass_isolation(config, profile, files, **kwargs)

        coordinator = RepairCoordinator(
            self.store, Config("mock", MOCK_MODEL, 100_000_000), provider, self.pipeline(), output_tokens=256)
        with patch("plugin_generation.run_isolated", side_effect=isolation):
            return coordinator.run(task_id)

    def test_fixed_response_repairs_once_then_builds(self):
        task_id = self.create_task()
        provider = FixedProvider()
        report = self.run_repair(task_id, provider, [BUILD_FAILURE, None, None, None])
        state = self.store.inspect(task_id)
        self.assertTrue(report["ok"], report)
        self.assertEqual("built", state["status"])
        self.assertEqual(1, state["repairRounds"])
        self.assertEqual("settled", state["modelCalls"][0]["status"])
        self.assertEqual(1, provider.calls)

    def test_default_mock_provider_returns_a_valid_development_patch(self):
        task_id = self.create_task()
        report = self.run_repair(task_id, MockProvider(), [BUILD_FAILURE, None, None, None])
        self.assertTrue(report["ok"], report)
        self.assertEqual("built", self.store.inspect(task_id)["status"])

    def test_repeated_failure_stops_after_same_fingerprint(self):
        task_id = self.create_task()
        provider = FixedProvider()
        report = self.run_repair(task_id, provider, [BUILD_FAILURE, BUILD_FAILURE])
        state = self.store.inspect(task_id)
        self.assertFalse(report["ok"])
        self.assertEqual("REPEATED_ERROR", state["stopCode"])
        self.assertEqual(1, provider.calls)
        self.assertEqual(0, state["reservedMicros"])

    def test_permission_violation_stops_without_another_model_call(self):
        def overreach(request, _stop):
            payload = json.loads(request.input_text)
            source = payload["files"]["app.js"]
            response = {"version": "plugin-development-v1", "patches": [{
                "plan": "Attempt to add a capability outside the confirmed permissions.",
                "specRef": {"type": "acceptanceCriteria", "id": "save_note"},
                "path": "app.js", "expectedSha256": source["sha256"],
                "replacement": source["content"] + "\ncall('device.contacts');\n",
                "expectedChecks": ["resource_integrity"],
            }]}
            return ProviderReply(json.dumps(response), Usage(100, 50))

        task_id = self.create_task()
        provider = FixedProvider(overreach)
        report = self.run_repair(task_id, provider, [BUILD_FAILURE])
        state = self.store.inspect(task_id)
        self.assertFalse(report["ok"])
        self.assertEqual("PATCH_CAPABILITY_NOT_ALLOWED", state["stopCode"])
        self.assertEqual(1, provider.calls)

    def test_repair_limit_and_insufficient_cost_stop_before_dispatch(self):
        for max_cost, max_rounds, expected in ((100_000, 0, "REPAIR_LIMIT"), (1_000, 3, "COST_LIMIT")):
            with self.subTest(expected=expected):
                task_id = self.create_task(max_cost=max_cost, max_rounds=max_rounds)
                provider = FixedProvider()
                report = self.run_repair(task_id, provider, [BUILD_FAILURE])
                state = self.store.inspect(task_id)
                self.assertFalse(report["ok"])
                self.assertEqual(expected, state["stopCode"])
                self.assertEqual(0, provider.calls)

    def test_uncertain_timeout_retains_reservation_and_is_not_replayed(self):
        task_id = self.create_task()
        provider = FixedProvider(lambda _request, _stop: ProviderReply(None, None, "MODEL_TIMEOUT"))
        report = self.run_repair(task_id, provider, [BUILD_FAILURE])
        state = self.store.inspect(task_id)
        self.assertEqual("MODEL_TIMEOUT", state["stopCode"])
        self.assertGreater(state["reservedMicros"], 0)
        self.assertEqual("uncertain", state["modelCalls"][0]["status"])
        self.assertEqual(1, provider.calls)
        self.assertFalse(report["ok"])

    def test_cancellation_during_call_keeps_queryable_terminal_result(self):
        task_id = self.create_task()

        def cancel_call(_request, _stop):
            self.store.cancel(task_id)
            return ProviderReply(None, None, "MODEL_CANCELLED")

        provider = FixedProvider(cancel_call)
        report = self.run_repair(task_id, provider, [BUILD_FAILURE])
        state = self.store.inspect(task_id)
        self.assertEqual("cancelled", state["status"])
        self.assertGreater(state["reservedMicros"], 0)
        self.assertEqual(1, provider.calls)
        self.assertFalse(report["ok"])

    def test_concurrent_claim_and_process_interruption_preserve_unique_result(self):
        task_id = self.create_task()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.store.claim_build(task_id), range(2)))
        self.assertEqual(1, sum(result["ok"] for result in results))

        self.store.record_failure(task_id, "generating", "ISOLATION_WORKLOAD_FAILED", check="isolated_build")
        self.store.begin_repair(task_id)
        reserved = self.store.reserve_model_call(task_id, 20_000)
        self.assertTrue(reserved["ok"])
        TaskStore(self.root / "tasks").sweep(recover=True)
        state = self.store.inspect(task_id)
        self.assertEqual("PROCESS_INTERRUPTED", state["stopCode"])
        self.assertEqual(20_000, state["reservedMicros"])
        self.assertEqual(reserved["callId"], state["modelCalls"][0]["id"])
        self.assertEqual("reserved", state["modelCalls"][0]["status"])
        self.assertFalse(self.store.begin_repair(task_id)["ok"])

    def test_project_budget_accounts_for_other_tasks_reservations(self):
        self.store = TaskStore(self.root / "project-limited", project_max_cost_micros=20_000)
        first = self.create_task(max_cost=100_000)
        second_spec = copy.deepcopy(QUICK)
        second_spec["status"] = "confirmed"
        second_spec["version"] = 2
        second = self.store.create(second_spec, max_cost_micros=100_000)["taskId"]
        for task_id in (first, second):
            self.store.transition(task_id, "generating")
            self.store.record_failure(task_id, "generating", "ISOLATION_WORKLOAD_FAILED", check="isolated_build")
            self.store.begin_repair(task_id)
        reserved = self.store.reserve_model_call(first, 20_000)
        self.assertTrue(reserved["ok"])
        stopped = self.store.reserve_model_call(second, 1)
        self.assertFalse(stopped["ok"])
        self.assertEqual("PROJECT_COST_LIMIT", stopped["code"])
        self.assertEqual(20_000, self.store.inspect(first)["projectBudget"]["reservedMicros"])


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(StageC6Tests))
    raise SystemExit(0 if result.wasSuccessful() else 1)
