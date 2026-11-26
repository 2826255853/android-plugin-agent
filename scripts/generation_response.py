"""Versioned, bounded model response validation for controlled plugin patches."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from dialogue_protocol import strict_object
from model_boundary import MAX_OUTPUT_BYTES
from plugin_generation import GenerationError, STATIC_CHECKS, TEMPLATES, _profile, _template_id
from task_runtime import FILE_BYTES, TaskError, TaskStore


RESPONSE_VERSION = "plugin-development-v1"
MAX_RESPONSE_BYTES = MAX_OUTPUT_BYTES
CHECKS = set(STATIC_CHECKS)
SPEC_REFERENCE_TYPES = {"pages", "workflows", "acceptanceCriteria", "collections", "fields", "unsupportedItems"}
CAPABILITY_CALL = re.compile(r"(?:call\s*\(\s*|[\"']capability[\"']\s*:\s*)[\"']([a-z][a-z0-9_.]*)[\"']")
DEVICE_CAPABILITY = re.compile(r"\bdevice\.[a-z][a-z0-9_.]*")


class PatchError(ValueError):
    def __init__(self, code: str, path: str | None = None, check: str | None = None) -> None:
        super().__init__(code)
        self.code, self.path, self.check = code, path, check


def response_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def parse_response(raw: str | bytes) -> dict[str, Any]:
    try:
        content = raw.encode("utf-8") if isinstance(raw, str) else raw
        if not isinstance(content, bytes):
            raise TypeError
        if len(content) > MAX_RESPONSE_BYTES:
            raise PatchError("MODEL_OUTPUT_TOO_LARGE")
        return strict_object(content)
    except PatchError:
        raise
    except (UnicodeError, TypeError, ValueError, RecursionError):
        raise PatchError("MODEL_INVALID_OUTPUT") from None


def _spec_items(spec: dict[str, Any]) -> dict[str, set[str]]:
    list_types = SPEC_REFERENCE_TYPES - {"fields"}
    items = {key: {item["id"] for item in spec[key]} for key in list_types - {"unsupportedItems"}}
    items["fields"] = {field["id"] for collection in spec["collections"] for field in collection["fields"]}
    items["unsupportedItems"] = {item["request"] for item in spec["unsupportedItems"]}
    return items


def validate_response(candidate: dict[str, Any], spec: dict[str, Any], allowed_paths: set[str]) -> list[dict[str, Any]]:
    if not isinstance(candidate, dict) or set(candidate) != {"version", "patches"}:
        raise PatchError("PATCH_RESPONSE_INVALID")
    if candidate["version"] != RESPONSE_VERSION:
        raise PatchError("PATCH_VERSION_UNSUPPORTED")
    patches = candidate["patches"]
    if not isinstance(patches, list) or not 1 <= len(patches) <= 16:
        raise PatchError("PATCH_RESPONSE_INVALID")
    spec_items = _spec_items(spec)
    permissions = {item["capability"] for item in spec["permissions"]}
    seen: set[str] = set()
    result = []
    for patch in patches:
        if not isinstance(patch, dict) or set(patch) != {
            "plan", "specRef", "path", "expectedSha256", "replacement", "expectedChecks"
        }:
            raise PatchError("PATCH_RESPONSE_INVALID")
        plan, reference = patch["plan"], patch["specRef"]
        path, expected, replacement, checks = (patch[key] for key in (
            "path", "expectedSha256", "replacement", "expectedChecks"))
        if not isinstance(plan, str) or not plan.strip() or len(plan.encode("utf-8")) > 1024:
            raise PatchError("PATCH_RESPONSE_INVALID")
        if not isinstance(reference, dict) or set(reference) != {"type", "id"}:
            raise PatchError("PATCH_SPEC_REFERENCE_INVALID")
        kind, identifier = reference["type"], reference["id"]
        if kind not in SPEC_REFERENCE_TYPES or not isinstance(identifier, str) or identifier not in spec_items[kind]:
            raise PatchError("PATCH_SPEC_REFERENCE_INVALID")
        if not isinstance(path, str) or path not in allowed_paths:
            raise PatchError("PATCH_PATH_NOT_ALLOWED", path if isinstance(path, str) else None)
        if path in seen:
            raise PatchError("PATCH_RESPONSE_INVALID", path)
        seen.add(path)
        if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
            raise PatchError("PATCH_RESPONSE_INVALID", path)
        if not isinstance(replacement, str) or len(replacement.encode("utf-8")) > FILE_BYTES:
            raise PatchError("PATCH_RESPONSE_INVALID", path)
        if (not isinstance(checks, list) or not checks or len(checks) != len(set(checks))
                or any(not isinstance(check, str) or check not in CHECKS for check in checks)):
            raise PatchError("PATCH_CHECK_NOT_ALLOWED", path)
        used_capabilities = set(CAPABILITY_CALL.findall(replacement)) | set(DEVICE_CAPABILITY.findall(replacement))
        if not used_capabilities <= permissions:
            raise PatchError("PATCH_CAPABILITY_NOT_ALLOWED", path)
        result.append({"path": path, "expectedSha256": expected, "content": replacement,
                       "expectedChecks": checks, "plan": plan, "specRef": reference})
    return result


class GenerationPatchCoordinator:
    """Validate one model response, then commit its complete patch set atomically."""

    def __init__(self, store: TaskStore) -> None:
        self.store = store

    def apply(self, task_id: str, raw_response: str | bytes) -> dict[str, Any]:
        before = self.store.inspect(task_id)
        try:
            candidate = parse_response(raw_response)
            spec_result = self.store.tool(task_id, "read_spec", {})
            if not spec_result["ok"]:
                raise PatchError(spec_result["code"], check="task_guard")
            spec = spec_result["result"]["spec"]
            template_id = _template_id(spec)
            _profile(spec)
            template_dir = TEMPLATES[template_id][0]
            allowed = {path.name for path in template_dir.iterdir()
                       if path.is_file() and path.suffix in {".html", ".js", ".css"}}
            patches = validate_response(candidate, spec, allowed)
            for patch in patches:
                current = self.store.tool(task_id, "read_file", {"path": patch["path"]})
                if not current["ok"]:
                    raise PatchError(current["code"], patch["path"], "patch_base")
                if current["result"]["sha256"] != patch["expectedSha256"]:
                    raise PatchError("PATCH_CONFLICT", patch["path"], "patch_base")
            result = self.store.apply_patch_batch(task_id, [
                {"path": patch["path"], "expectedSha256": patch["expectedSha256"],
                 "content": patch["content"]} for patch in patches
            ])
            if not result["ok"]:
                raise PatchError(result["code"], check="patch_commit")
            committed = self.store.inspect(task_id)
            applied = {item["path"]: item for item in result["patches"]}
            return {"ok": True, "taskId": task_id, "responseVersion": RESPONSE_VERSION,
                    "responseSha256": response_digest(raw_response.encode("utf-8") if isinstance(raw_response, str) else raw_response),
                    "repairRound": committed["repairRounds"], "patches": [
                        {"path": patch["path"], "plan": patch["plan"], "specRef": patch["specRef"],
                         "beforeSha256": applied[patch["path"]]["beforeSha256"],
                         "afterSha256": applied[patch["path"]]["afterSha256"],
                         "expectedChecks": patch["expectedChecks"]} for patch in patches],
                    "deliverable": False}
        except OSError as exc:
            code = "PATCH_APPLY_FAILED"
            return {"ok": False, "taskId": task_id, "responseVersion": RESPONSE_VERSION,
                    "repairRound": before["repairRounds"], "code": code,
                    "path": None, "check": "patch_commit", "deliverable": False}
        except (PatchError, TaskError, GenerationError, UnicodeError, ValueError, TypeError, KeyError, RecursionError) as exc:
            code = exc.code if isinstance(exc, (PatchError, TaskError, GenerationError)) else "PATCH_RESPONSE_INVALID"
            return {"ok": False, "taskId": task_id, "responseVersion": RESPONSE_VERSION,
                    "repairRound": before["repairRounds"], "code": code,
                    "path": getattr(exc, "path", None), "check": getattr(exc, "check", None),
                    "deliverable": False}
