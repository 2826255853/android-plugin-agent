"""Deterministic Stage 2.1 specification intake and version storage.

This module does not call a model. It validates user-confirmed structure and
turns missing decisions into explicit clarification questions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from math import isfinite
from pathlib import Path
from typing import Any


SUPPORTED_CAPABILITIES = {
    "data.read": "读取插件自己声明的数据集合",
    "data.write": "写入插件自己声明的数据集合",
    "ui.toast": "请求宿主显示简短提示",
}
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{2,63}")
SHA256 = re.compile(r"[a-f0-9]{64}")
SPEC_METADATA = {"projectId", "schemaVersion", "version", "status", "revision"}
SPEC_DIRECTORY = Path(__file__).resolve().parents[1] / "desktop-spec"
SPEC_SCHEMA = json.loads((SPEC_DIRECTORY / "spec.schema.json").read_text(encoding="utf-8"))


class SpecError(ValueError):
    pass


def clarification_questions(spec: dict[str, Any]) -> list[dict[str, str]]:
    """Return the decisions needed before this draft can be confirmed."""
    questions: list[dict[str, str]] = []
    if not isinstance(spec.get("goal"), str) or not spec["goal"].strip():
        questions.append({"field": "goal", "question": "这个工具要帮用户完成什么目标？"})
    if not spec.get("pages"):
        questions.append({"field": "pages", "question": "需要哪些页面？每个页面展示或完成什么？"})
    if not spec.get("workflows"):
        questions.append({"field": "workflows", "question": "用户从打开工具到完成目标，需要经过哪些操作步骤？"})
    if spec.get("storesData") is None:
        questions.append({"field": "storesData", "question": "是否需要保存数据？如果需要，保存哪些内容？"})
    elif spec.get("storesData") is True and not spec.get("collections"):
        questions.append({"field": "collections", "question": "需要保存哪些字段？哪些字段必填？"})
    if not spec.get("acceptanceCriteria"):
        questions.append({"field": "acceptanceCriteria", "question": "用什么具体结果判断这个工具符合预期？"})
    return questions


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise SpecError(message)


def validate_schema(spec: dict[str, Any]) -> None:
    """Apply the JSON Schema constraints used by the v1 specification."""
    def check(value, rules):
        if "$ref" in rules:
            check(value, SPEC_SCHEMA["$defs"][rules["$ref"].removeprefix("#/$defs/")])
            return
        kind = rules.get("type")
        if isinstance(kind, list):
            _need(kind == ["boolean", "null"] and (type(value) is bool or value is None), "invalid schema type")
        elif kind == "object":
            _need(isinstance(value, dict), "schema requires an object")
            properties = rules["properties"]
            _need(set(rules["required"]) <= value.keys() <= properties.keys(), "invalid schema properties")
            for field, child in value.items():
                check(child, properties[field])
        elif kind == "array":
            _need(isinstance(value, list) and len(value) >= rules.get("minItems", 0), "invalid schema array")
            for child in value:
                check(child, rules["items"])
            if rules.get("uniqueItems"):
                _need(len({spec_json(child) for child in value}) == len(value), "duplicate schema items")
        elif kind == "string":
            _need(isinstance(value, str), "schema requires a string")
            _need(rules.get("minLength", 0) <= len(value) <= rules.get("maxLength", len(value)),
                  "invalid schema string length")
            if "pattern" in rules:
                _need(re.fullmatch(rules["pattern"], value) is not None, "invalid schema string pattern")
        elif kind in {"integer", "number"}:
            allowed = (int,) if kind == "integer" else (int, float)
            _need(type(value) in allowed and (type(value) is int or isfinite(value)), "invalid schema number")
            _need(rules.get("minimum", value) <= value <= rules.get("maximum", value), "invalid schema number range")
        if "const" in rules:
            _need(type(value) is type(rules["const"]) and value == rules["const"], "invalid schema constant")
        if "enum" in rules:
            _need(value in rules["enum"], "invalid schema choice")

    check(spec, SPEC_SCHEMA)


def _only_keys(item: dict[str, Any], allowed: set[str], label: str) -> None:
    extras = set(item) - allowed
    _need(not extras, f"unexpected {label} fields: " + ", ".join(sorted(extras)))


def _unique_ids(items: Any, label: str) -> None:
    _need(isinstance(items, list), f"{label} must be an array")
    ids: set[str] = set()
    for item in items:
        _need(isinstance(item, dict), f"each {label} item must be an object")
        value = item.get("id")
        _need(isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None, f"invalid {label} id")
        _need(value not in ids, f"duplicate {label} id: {value}")
        ids.add(value)


def unsupported_requests(spec: dict[str, Any]) -> list[dict[str, str]]:
    """Describe requested host capabilities outside the Stage 1 contract."""
    result = []
    for permission in spec.get("permissions", []):
        capability = permission["capability"]
        if capability not in SUPPORTED_CAPABILITIES:
            entry = next((item for item in spec.get("unsupportedItems", []) if item.get("request") == capability), None)
            result.append({
                "capability": capability,
                "reason": entry["reason"] if entry else "该能力不在当前宿主白名单中。",
                "alternative": entry["alternative"] if entry else "移除该权限，或改用当前支持的离线页面、插件数据接口或提示能力。",
            })
    return result


def validate_spec(spec: dict[str, Any], *, require_confirmable: bool = False) -> None:
    _need(isinstance(spec, dict), "spec must be an object")
    _only_keys(spec, SPEC_METADATA | {"title", "goal", "storesData", "pages", "collections", "workflows", "permissions", "acceptanceCriteria", "unsupportedItems"}, "spec")
    _need(spec.get("schemaVersion") == 1, "schemaVersion must be 1")
    _need(isinstance(spec.get("projectId"), str) and PROJECT_ID.fullmatch(spec["projectId"]) is not None, "invalid projectId")
    _need(isinstance(spec.get("version"), int) and not isinstance(spec["version"], bool) and spec["version"] > 0, "version must be a positive integer")
    _need(spec.get("status") in {"draft", "confirmed"}, "status must be draft or confirmed")
    if "revision" in spec:
        revision = spec["revision"]
        _need(isinstance(revision, dict) and revision.keys() == {"baseVersion", "baseSpecSha256"},
              "invalid revision metadata")
        _need(type(revision["baseVersion"]) is int and 0 < revision["baseVersion"] < spec["version"],
              "invalid revision base version")
        _need(isinstance(revision["baseSpecSha256"], str) and SHA256.fullmatch(revision["baseSpecSha256"]),
              "invalid revision base digest")
    _need(isinstance(spec.get("title"), str) and 1 <= len(spec["title"].strip()) <= 120, "title must contain 1..120 characters")
    _need(isinstance(spec.get("goal"), str), "goal must be a string")
    _need(isinstance(spec.get("storesData"), bool) or spec.get("storesData") is None, "storesData must be true, false, or null")
    for key in ("pages", "collections", "workflows", "permissions", "acceptanceCriteria", "unsupportedItems"):
        _need(isinstance(spec.get(key), list), f"{key} must be an array")
    _unique_ids(spec["pages"], "pages")
    _unique_ids(spec["collections"], "collections")
    _unique_ids(spec["workflows"], "workflows")
    _unique_ids(spec["acceptanceCriteria"], "acceptanceCriteria")
    _need(spec["storesData"] is not False or not spec["collections"], "collections must be empty when storesData is false")
    for page in spec["pages"]:
        _only_keys(page, {"id", "name", "purpose"}, "page")
        _need(all(isinstance(page.get(k), str) and page[k].strip() for k in ("name", "purpose")), "each page needs name and purpose")
    for flow in spec["workflows"]:
        _only_keys(flow, {"id", "trigger", "steps"}, "workflow")
        _need(isinstance(flow.get("trigger"), str) and flow["trigger"].strip(), "each workflow needs a trigger")
        _need(isinstance(flow.get("steps"), list) and flow["steps"] and all(isinstance(s, str) and s.strip() for s in flow["steps"]), "each workflow needs non-empty steps")
    for collection in spec["collections"]:
        _only_keys(collection, {"id", "fields"}, "collection")
        _need(isinstance(collection.get("fields"), list), "each collection needs fields")
        _need(bool(collection["fields"]), "each collection needs at least one field")
        field_ids: set[str] = set()
        for field in collection["fields"]:
            _need(isinstance(field, dict), "each field must be an object")
            _only_keys(field, {"id", "type", "required", "maxLength", "minimum", "maximum"}, "field")
            fid = field.get("id")
            _need(isinstance(fid, str) and ID_PATTERN.fullmatch(fid) is not None and fid not in field_ids, "invalid or duplicate field id")
            field_ids.add(fid)
            _need(isinstance(field.get("type"), str) and field["type"] in {"string", "integer", "number", "boolean"}, "unsupported field type")
            _need(isinstance(field.get("required"), bool), "field required must be boolean")
    requested = set()
    for permission in spec["permissions"]:
        _need(isinstance(permission, dict) and isinstance(permission.get("capability"), str), "each permission needs a capability")
        _only_keys(permission, {"capability", "justification"}, "permission")
        _need(permission["capability"] not in requested, "duplicate permission capability")
        requested.add(permission["capability"])
        _need(isinstance(permission.get("justification"), str) and permission["justification"].strip(), "each permission needs a justification")
    unsupported_ids: set[str] = set()
    for item in spec["unsupportedItems"]:
        _need(isinstance(item, dict) and isinstance(item.get("request"), str), "each unsupported item needs a request")
        _only_keys(item, {"request", "reason", "alternative"}, "unsupported item")
        _need(item["request"] not in unsupported_ids, "duplicate unsupported request")
        unsupported_ids.add(item["request"])
        _need(all(isinstance(item.get(k), str) and item[k].strip() for k in ("reason", "alternative")), "unsupported items need a reason and alternative")
    for criterion in spec["acceptanceCriteria"]:
        _only_keys(criterion, {"id", "scenario", "expected"}, "acceptance criterion")
        _need(all(isinstance(criterion.get(k), str) and criterion[k].strip() for k in ("scenario", "expected")), "each acceptance criterion needs scenario and expected result")
    if require_confirmable:
        questions = clarification_questions(spec)
        _need(not questions, "spec needs clarification: " + ", ".join(item["field"] for item in questions))
        missing = [cap for cap in requested if cap not in SUPPORTED_CAPABILITIES and cap not in unsupported_ids]
        _need(not missing, "unsupported capabilities need a reason and alternative: " + ", ".join(sorted(missing)))


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SpecError(f"cannot read JSON spec: {exc}") from exc


def _latest_version(project_dir: Path) -> int:
    return max((int(path.stem[1:]) for path in project_dir.glob("v*.json")
                if re.fullmatch(r"v[1-9][0-9]*", path.stem)), default=0)


def _confirmed_at(store: Path, project_id: str, version: int) -> dict[str, Any]:
    _need(PROJECT_ID.fullmatch(project_id) is not None and version > 0, "invalid project or base version")
    spec = load_json(store / project_id / f"v{version}.json")
    validate_spec(spec, require_confirmable=True)
    _need(spec["status"] == "confirmed" and spec["projectId"] == project_id and spec["version"] == version,
          "confirmed specification does not match the requested project and version")
    return spec


def next_version(store: Path, project_id: str) -> int:
    _need(PROJECT_ID.fullmatch(project_id) is not None, "invalid project")
    return _latest_version(store / project_id) + 1


def spec_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _spec_digest(value: Any) -> str:
    return hashlib.sha256(spec_json(value).encode("utf-8")).hexdigest()


def revise(store: Path, project_id: str, base_version: int) -> dict[str, Any]:
    base = _confirmed_at(store, project_id, base_version)
    return {**base, "version": _latest_version(store / project_id) + 1, "status": "draft",
            "revision": {"baseVersion": base_version, "baseSpecSha256": _spec_digest(base)}}


def _changes(reference: dict[str, Any], draft: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"field": key, "before": reference[key], "after": draft[key]}
            for key in sorted(draft.keys() - SPEC_METADATA)
            if _spec_digest(reference[key]) != _spec_digest(draft[key])]


def review_spec(spec: dict[str, Any], store: Path) -> dict[str, Any]:
    validate_spec(spec)
    result = {"questions": clarification_questions(spec), "unsupported": unsupported_requests(spec)}
    if "revision" not in spec:
        return result
    _need(spec["status"] == "draft", "only revision drafts can be reviewed")
    project_id = spec["projectId"]
    latest_version = _latest_version(store / project_id)
    _need(spec["version"] == latest_version + 1, f"next version must be {latest_version + 1}")
    revision = spec["revision"]
    base = _confirmed_at(store, project_id, revision["baseVersion"])
    _need(revision["baseSpecSha256"] == _spec_digest(base), "revision base has changed")
    latest = base if base["version"] == latest_version else _confirmed_at(store, project_id, latest_version)
    result.update(projectId=project_id, version=spec["version"], baseVersion=base["version"],
                  baseChanges=_changes(base, spec), latestVersion=latest_version,
                  changes=_changes(latest, spec), reviewSha256=None)
    if not result["changes"]:
        result["questions"].append({"field": "changes", "question": "相对于当前确认版本，需要修改哪些内容？"})
    try:
        validate_spec(spec, require_confirmable=True)
    except SpecError:
        pass  # The questions and capability explanations above describe missing answers.
    else:
        if not result["questions"]:
            result["reviewSha256"] = _spec_digest({"draft": spec, "latestSpecSha256": _spec_digest(latest)})
    return result


def save_confirmed(spec: dict[str, Any], store: Path, *, review_sha256: str | None = None) -> Path:
    validate_spec(spec, require_confirmable=True)
    _need(spec["status"] == "draft", "only drafts can be confirmed")
    if "revision" in spec:
        review = review_spec(spec, store)
        _need(review_sha256 is not None and review_sha256 == review["reviewSha256"],
              "review this revision and confirm its current reviewSha256")
    else:
        _need(review_sha256 is None, "review digest is only used for revision drafts")
    project_dir = store / spec["projectId"]
    project_dir.mkdir(parents=True, exist_ok=True)
    expected = _latest_version(project_dir) + 1
    _need(spec["version"] == expected, f"next version must be {expected}")
    destination = project_dir / f"v{expected}.json"
    _need(not destination.exists(), "confirmed version already exists")
    saved = dict(spec)
    saved["status"] = "confirmed"
    payload = (json.dumps(saved, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=".spec-", suffix=".tmp", dir=project_dir)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp_name, destination)
        except FileExistsError as exc:
            raise SpecError("confirmed version already exists") from exc
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="Review and version a Stage 2.1 plugin specification.")
    sub = parser.add_subparsers(dest="command", required=True)
    review = sub.add_parser("review", help="validate a draft and print clarification questions")
    review.add_argument("spec", type=Path)
    revise_parser = sub.add_parser("revise", help="start a new draft from a confirmed version")
    revise_parser.add_argument("project_id")
    revise_parser.add_argument("base_version", type=int)
    revise_parser.add_argument("--output", type=Path, required=True)
    confirm = sub.add_parser("confirm", help="save a confirmable draft as an immutable version")
    confirm.add_argument("spec", type=Path)
    confirm.add_argument("--review-sha256")
    for command in (review, revise_parser, confirm):
        command.add_argument("--store", type=Path, default=Path("desktop-spec/projects"))
    args = parser.parse_args()
    try:
        if args.command == "revise":
            draft = revise(args.store, args.project_id, args.base_version)
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(draft, ensure_ascii=False, indent=2) + "\n")
            print(args.output.as_posix())
            return 0
        spec = load_json(args.spec)
        if args.command == "review":
            result = review_spec(spec, args.store)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        path = save_confirmed(spec, args.store, review_sha256=args.review_sha256)
        print(path.as_posix())
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
