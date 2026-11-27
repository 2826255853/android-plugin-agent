"""Budgeted, resumable-by-query repair orchestration for Stage C.6."""

from __future__ import annotations

import json
import threading
import time
from math import ceil
from uuid import UUID

from generation_response import GenerationPatchCoordinator, _profile, _template_id
from model_boundary import (
    DEVELOPMENT_PROMPT_VERSION, MAX_INPUT_BYTES, MAX_OUTPUT_TOKENS, RATES, Config,
    ModelBoundary, ModelError, Request,
)
from plugin_generation import GenerationPipeline, STATIC_CHECKS, TEMPLATES
from task_runtime import TaskError, TaskStore, encoded


class RepairCoordinator:
    def __init__(self, store: TaskStore, provider_config: Config, provider,
                 pipeline: GenerationPipeline, *, output_tokens: int = MAX_OUTPUT_TOKENS,
                 timeout_seconds: int = 30) -> None:
        self.store = store
        self.config = provider_config
        self.provider = provider
        self.pipeline = pipeline
        self.output_tokens = output_tokens
        self.timeout_seconds = timeout_seconds
        self.boundary = ModelBoundary(provider_config, provider)
        self.patch_coordinator = GenerationPatchCoordinator(store)

    def run(self, task_id: str) -> dict:
        while True:
            report = self.pipeline.run(task_id)
            if report.get("ok"):
                return report
            task = self.store.inspect(task_id)
            if task["status"] != "validating" and task["status"] != "generating":
                return report
            if not self._has_recorded_build_failure(task, report):
                return report
            if report.get("code") in {"WORKSPACE_ESCAPE", "WORKSPACE_UNSAFE", "TASK_ID_MISMATCH",
                                       "TOOL_NOT_ALLOWED", "PERMISSION_DENIED", "RESOURCE_HASH_MISMATCH",
                                       "PACKAGE_CORRUPT"}:
                return report
            fixed = self._repair_until_patch(task_id, report)
            if not fixed["ok"]:
                report.update(status=fixed["status"], repairStopCode=fixed["code"])
                if fixed.get("callId"):
                    report["modelCallId"] = fixed["callId"]
                return report

    @staticmethod
    def _has_recorded_build_failure(task: dict, report: dict) -> bool:
        if report.get("code") in {"BUILD_IN_PROGRESS", "REPAIR_REQUIRED", "TASK_TERMINAL", "TASK_NOT_RUNNING"}:
            return False
        return any(event["code"] == "BUILD_FAILED"
                   and event["details"].get("repairRound") == task["repairRounds"]
                   for event in reversed(task["events"]))

    def _repair_until_patch(self, task_id: str, failure: dict) -> dict:
        while True:
            started = self.store.begin_repair(task_id)
            if not started["ok"]:
                return {"ok": False, "status": started["status"], "code": started.get("code")}
            call_result = self._request_patch(task_id, failure)
            if call_result.get("uncertain"):
                return {"ok": False, "status": self.store.inspect(task_id)["status"],
                        "code": call_result["code"], "callId": call_result["callId"]}
            if not call_result["ok"]:
                recorded = self.store.record_failure(
                    task_id, "generating", call_result["code"], path=call_result.get("path"),
                    check=call_result.get("check"))
                if recorded["status"] not in {"generating", "validating"}:
                    return {"ok": False, "status": recorded["status"], "code": recorded.get("stopCode", call_result["code"])}
                failure = {"stage": "generating", "code": call_result["code"],
                           "path": call_result.get("path"), "check": call_result.get("check")}
                continue
            applied = self.patch_coordinator.apply(task_id, call_result["content"])
            if applied["ok"]:
                return {"ok": True, "status": self.store.inspect(task_id)["status"]}
            recorded = self.store.record_failure(
                task_id, "generating", applied["code"], path=applied.get("path"), check=applied.get("check"))
            if recorded["status"] not in {"generating", "validating"}:
                return {"ok": False, "status": recorded["status"], "code": recorded.get("stopCode", applied["code"])}
            failure = {"stage": "generating", "code": applied["code"],
                       "path": applied.get("path"), "check": applied.get("check")}

    def _request_patch(self, task_id: str, failure: dict) -> dict:
        task = self.store.inspect(task_id)
        spec = self.store.tool(task_id, "read_spec", {})["result"]["spec"]
        template_id = _template_id(spec)
        _profile(spec)
        template_dir = TEMPLATES[template_id][0]
        names = sorted(path.name for path in template_dir.iterdir()
                       if path.is_file() and path.suffix in {".html", ".js", ".css", ".json"})
        files = {}
        for name in names:
            read = self.store.tool(task_id, "read_file", {"path": name})
            if read["ok"]:
                files[name] = {"sha256": read["result"]["sha256"], "content": read["result"]["content"]}
        request_payload = {
            "version": "plugin-development-request-v1",
            "spec": spec,
            "failure": {key: failure.get(key) for key in ("stage", "code", "path", "check")},
            "allowedPaths": names,
            "expectedChecks": list(STATIC_CHECKS),
            "files": files,
        }
        input_text = json.dumps(request_payload, ensure_ascii=False, separators=(",", ":"))
        try:
            if len(input_text.encode("utf-8")) > MAX_INPUT_BYTES:
                return {"ok": False, "callId": None, "code": "MODEL_INPUT_TOO_LARGE"}
        except UnicodeError:
            return {"ok": False, "callId": None, "code": "MODEL_CONFIG_INVALID"}

        reserve = ceil(RATES[self.config.provider].ceiling(self.output_tokens) / 1000)
        reservation = self.store.reserve_model_call(task_id, reserve)
        if not reservation["ok"]:
            return {"ok": False, "callId": None, "code": reservation["code"]}
        call_id = reservation["callId"]
        request_id = UUID(hex=call_id.removeprefix("call-"))
        request = Request(
            request_id=request_id, project_id=spec["projectId"], session_id=request_id,
            input_text=input_text, timeout_seconds=self.timeout_seconds,
            output_tokens=self.output_tokens, prompt_version=DEVELOPMENT_PROMPT_VERSION,
        )
        cancelled = threading.Event()
        watcher_done = threading.Event()

        def watch_task():
            while not watcher_done.wait(0.1):
                try:
                    current = self.store.inspect(task_id)
                    if current["status"] in {"cancelled", "stopped", "failed", "built"}:
                        cancelled.set()
                        return
                except (TaskError, OSError):
                    cancelled.set()
                    return

        watcher = threading.Thread(target=watch_task, daemon=True)
        watcher.start()
        try:
            result = self.boundary.call(request, cancelled, pre_reserved=True)
        except ModelError as error:
            self.store.mark_model_call_uncertain(task_id, call_id, error.code)
            return {"ok": False, "uncertain": True, "callId": call_id, "code": error.code}
        finally:
            watcher_done.set()
            watcher.join(timeout=1)

        receipt = result.receipt
        if receipt.usage_uncertain:
            self.store.mark_model_call_uncertain(task_id, call_id, receipt.error or "MODEL_USAGE_UNCERTAIN")
            return {"ok": False, "uncertain": True, "callId": call_id,
                    "code": receipt.error or "MODEL_USAGE_UNCERTAIN"}
        actual = ceil(receipt.charged_nano_usd / 1000)
        settled = self.store.settle_model_call(task_id, call_id, actual)
        if not settled["ok"]:
            return {"ok": False, "callId": call_id, "code": settled.get("code", "MODEL_SETTLEMENT_FAILED")}
        if receipt.error:
            return {"ok": False, "callId": call_id, "code": receipt.error}
        if result.candidate is None:
            return {"ok": False, "callId": call_id, "code": "MODEL_INVALID_OUTPUT"}
        return {"ok": True, "callId": call_id, "content": json.dumps(result.candidate, ensure_ascii=False)}
