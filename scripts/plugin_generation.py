"""Stage 2.3 bounded Quick Notes generation and trusted static-data build.

No model, generated program, shell, browser or dependency installer is invoked.
Only confirmed specifications matching the documented template profile run.
"""

from __future__ import annotations

import argparse
import copy
import html
import json
import re
import sqlite3
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from plugin_package import PackageError, build_package, validate_package, validate_source
from spec_workflow import SpecError, validate_spec
from task_runtime import DEFAULT_STORE, FILE_BYTES, TaskError, TaskStore, digest, encoded


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "desktop-generator" / "quick-notes-v1"
TEMPLATE_VERSION = "quick-notes-v1.0.0"
PROFILE = ROOT / "desktop-spec" / "examples" / "quick-notes-v1.json"
TEMPLATES = {
    "quick-notes": (TEMPLATE, TEMPLATE_VERSION, PROFILE),
    "manual-contacts": (
        ROOT / "desktop-generator" / "manual-contacts-v1",
        "manual-contacts-v1.0.0",
        ROOT / "desktop-spec" / "examples" / "manual-contacts-v1.json",
    ),
}


class GenerationError(ValueError):
    def __init__(self, code: str, path: str | None = None, check: str | None = None) -> None:
        super().__init__(code)
        self.code, self.path, self.check = code, path, check


def require(condition: Any, code: str, path: str | None = None, check: str | None = None) -> None:
    if not condition:
        raise GenerationError(code, path, check)


def _template_id(spec: dict[str, Any]) -> str:
    collections = spec.get("collections")
    if isinstance(collections, list) and collections and isinstance(collections[0], dict):
        if collections[0].get("id") == "contacts":
            return "manual-contacts"
    return "quick-notes"


def _profile(spec: dict[str, Any]) -> str:
    try:
        validate_spec(spec, require_confirmable=True)
    except (SpecError, ValueError, TypeError, RecursionError):
        raise GenerationError("INVALID_SPEC", check="confirmed_spec") from None
    require(spec["status"] == "confirmed", "SPEC_NOT_CONFIRMED", check="confirmed_spec")
    title = spec["title"]
    try:
        title_length = len(title.encode("utf-16-le")) // 2
    except UnicodeError:
        raise GenerationError("GENERATOR_TITLE_INVALID", check="manifest_name") from None
    require(1 <= title_length <= 64 and not any(ord(c) < 32 or ord(c) == 127 for c in title),
            "GENERATOR_TITLE_INVALID", check="manifest_name")
    # The prose in spec v1 has no executable semantics. Explicitly match the
    # supported workflow instead of silently promising arbitrary functionality.
    template_id = _template_id(spec)
    expected = json.loads(TEMPLATES[template_id][2].read_text(encoding="utf-8"))
    candidate = copy.deepcopy(spec)
    candidate.pop("revision", None)
    for key in ("projectId", "version", "status", "title"):
        candidate[key] = expected[key]
    try:
        for field in candidate["collections"][0]["fields"]:
            maximum = field["maxLength"]
            require(type(maximum) is int and 1 <= maximum <= 65536,
                    "GENERATOR_UNSUPPORTED_SPEC", check="template_profile")
            field["maxLength"] = next(f["maxLength"] for f in expected["collections"][0]["fields"] if f["id"] == field["id"])
    except (KeyError, IndexError, StopIteration, TypeError):
        raise GenerationError("GENERATOR_UNSUPPORTED_SPEC", check="template_profile") from None
    require(encoded(candidate) == encoded(expected), "GENERATOR_UNSUPPORTED_SPEC", check="template_profile")
    return template_id


def generate_files(spec: dict[str, Any]) -> dict[str, bytes]:
    """Render the fixed append/read profile with escaped name and field limits."""
    template_id = _profile(spec)
    template = TEMPLATES[template_id][0]
    plugin_id = "org.generated." + spec["projectId"]
    limits = {f["id"]: f["maxLength"] for f in spec["collections"][0]["fields"]}
    files: dict[str, bytes] = {}
    for name in ("index.html", "app.js", "app.css"):
        path = template / name
        require(path.stat().st_size <= FILE_BYTES, "TEMPLATE_INVALID", name, "template_size")
        content = path.read_text(encoding="utf-8")
        replacements = ({"__TITLE__": html.escape(spec["title"], quote=True), **{
                            f"__{field['id'].upper()}_LIMIT__": str(limits[field["id"]])
                            for field in spec["collections"][0]["fields"] if "maxLength" in field}}
                        if name == "index.html" else {"__PLUGIN_ID__": plugin_id} if name == "app.js" else {})
        # A single substitution pass prevents user text from becoming a second
        # template expansion (for example a title containing another marker).
        if replacements:
            content = re.sub("|".join(re.escape(key) for key in replacements),
                             lambda match: replacements[match[0]], content)
        files[name] = content.encode("utf-8")
    manifest = {
        "manifestVersion": 1, "id": plugin_id, "name": spec["title"],
        "version": f"{spec['version']}.0.0", "hostApi": {"min": "1.0.0", "max": "1.0.0"},
        "entry": "index.html", "capabilities": ["data.read", "data.write"],
        "dataModel": {"version": f"1.{spec['version'] - 1}.0", "collections": copy.deepcopy(spec["collections"])},
        "files": [{"path": name, "sha256": digest(content)} for name, content in sorted(files.items())],
    }
    files["manifest.json"] = encoded(manifest) + b"\n"
    return files


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: dict[str, dict[str, str | None]] = {}
        self.resources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if "id" in values:
            identifier = values["id"]
            require(identifier not in self.ids, "STATIC_TEST_FAILED", "index.html", "unique_element_ids")
            self.ids[identifier] = values
        require(not any(key.startswith("on") for key in values), "STATIC_TEST_FAILED", "index.html", "inline_handlers")
        if tag == "script":
            require(values.get("src") == "app.js", "STATIC_TEST_FAILED", "index.html", "script_source")
            self.resources.append(values["src"])
        if tag == "link":
            self.resources.append(values.get("href"))


def run_static_tests(files: dict[str, bytes], spec: dict[str, Any]) -> list[str]:
    """Fixed structure/contract checks, with no generated-code execution claim."""
    manifest = validate_source(files, "org.generated." + spec["projectId"])
    require(manifest["dataModel"]["collections"] == spec["collections"]
            and set(manifest["capabilities"]) == {p["capability"] for p in spec["permissions"]}
            and manifest["name"] == spec["title"] and manifest["version"] == f"{spec['version']}.0.0",
            "STATIC_TEST_FAILED", "manifest.json", "spec_binding")
    parser = _Page()
    parser.feed(files["index.html"].decode("utf-8"))
    parser.close()
    template_id = _template_id(spec)
    required = ({"status", "list-page", "editor-page", "new-note", "note-form", "title", "body", "notes", "refresh", "load-more", "cancel-edit"}
                if template_id == "quick-notes" else
                {"status", "list-page", "editor-page", "new-contact", "contact-form", "name", "phone", "email", "contacts", "refresh", "cancel-edit"})
    require(required <= parser.ids.keys() and set(parser.resources) == {"app.js", "app.css"},
            "STATIC_TEST_FAILED", "index.html", "page_structure")
    for field in spec["collections"][0]["fields"]:
        require(parser.ids[field["id"]].get("maxlength") == str(field["maxLength"]),
                "STATIC_TEST_FAILED", "index.html", "field_limits")
    primary_field = "title" if template_id == "quick-notes" else "name"
    require("required" in parser.ids[primary_field] and "hidden" in parser.ids["editor-page"],
            "STATIC_TEST_FAILED", "index.html", "form_contract")
    return ["confirmed_spec_binding", "manifest_contract", "resource_integrity", "local_resource_references",
            "page_structure", "field_limits", "package_roundtrip"]


class GenerationPipeline:
    def __init__(self, store: TaskStore) -> None:
        self.store = store

    @staticmethod
    def _unwrap(result: dict[str, Any]) -> dict[str, Any]:
        if not result["ok"]:
            raise GenerationError(result["code"], check="task_guard")
        return result

    def run(self, task_id: str) -> dict[str, Any]:
        stage = "generating"
        claimed = False
        state = self.store.inspect(task_id)
        report = {"taskId": task_id, "specSha256": state["specSha256"], "specVersion": state["specVersion"],
                  "templateVersion": TEMPLATE_VERSION, "deliverable": False,
                  "executedGeneratedCode": False, "modelCalls": 0, "checks": []}
        try:
            self._unwrap(self.store.claim_build(task_id))
            claimed = True
            spec = self._unwrap(self.store.tool(task_id, "read_spec", {}))["result"]["spec"]
            report["templateVersion"] = TEMPLATES[_template_id(spec)][1]
            files = generate_files(spec)
            # All generated writes use the same quotas, revision history and
            # cancellation boundary as Agent file tools. No arbitrary file I/O.
            for name, content in sorted(files.items()):
                read = self.store.tool(task_id, "read_file", {"path": name})
                if not read["ok"] and read.get("code") != "FILE_NOT_FOUND":
                    self._unwrap(read)
                previous = read["result"]["sha256"] if read["ok"] else None
                self._unwrap(self.store.tool(task_id, "apply_patch", {
                    "path": name, "expectedSha256": previous, "content": content.decode("utf-8")}))
            self._unwrap(self.store.transition(task_id, "validating"))
            stage = "manifest"
            # Build from the actual bounded workspace snapshot, including any
            # unexpected files; do not validate only the generator's dictionary.
            with self.store._transaction() as db:
                task = self.store._task(db, task_id)
                error = self.store._guard(db, task)
                if error:
                    raise GenerationError(error, check="task_guard")
                names = sorted(self.store._inventory(task))
            snapshot = {name: self._unwrap(self.store.tool(task_id, "read_file", {"path": name}))["result"]["content"].encode("utf-8") for name in names}
            report["sourceSha256"] = digest(encoded({name: digest(raw) for name, raw in sorted(snapshot.items())}))
            report["files"] = {name: digest(raw) for name, raw in sorted(snapshot.items())}
            validate_source(snapshot, "org.generated." + spec["projectId"])
            stage = "building"
            package = build_package(snapshot)
            validate_package(package, "org.generated." + spec["projectId"])
            stage = "testing"
            report["checks"] = run_static_tests(snapshot, spec)
            report.update(ok=True, stage="complete", status="built", artifact={
                "sha256": digest(package), "sizeBytes": len(package),
                "path": f"artifacts/{task_id}.zip"})
            self._unwrap(self.store.save_build(task_id, report, package))
            try:
                self.store.export_build(task_id)
            except (OSError, TaskError):
                # Export is recoverable from the committed package snapshot;
                # failure must not turn an immutable built task into a failure.
                report.update(exported=False, exportCode="EXPORT_FAILED")
            return report
        except (GenerationError, PackageError, TaskError, OSError, ValueError, TypeError, RecursionError, sqlite3.Error) as exc:
            code = exc.code if isinstance(exc, (GenerationError, PackageError, TaskError)) else "BUILD_INTERNAL_ERROR"
            # Only normalized identifiers are retained; exception strings can
            # contain paths, credentials or untrusted file contents.
            location = getattr(exc, "path", None)
            check = getattr(exc, "check", None)
            report.pop("artifact", None)
            report.update(ok=False, stage=stage, code=code, path=location, check=check)
            current = self.store.inspect(task_id)
            if claimed and current["status"] != "built":
                if current["status"] in {"generating", "validating"}:
                    self.store.record_failure(task_id, stage, code, path=location, check=check)
                report["status"] = self.store.inspect(task_id)["status"]
                self.store.save_build(task_id, report)
            report["status"] = self.store.inspect(task_id)["status"]
            return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run").add_argument("task_id")
    commands.add_parser("export").add_argument("task_id")
    args = parser.parse_args()
    try:
        store = TaskStore(args.store)
        result = GenerationPipeline(store).run(args.task_id) if args.command == "run" else {
            "ok": True, "taskId": args.task_id, "path": str(store.export_build(args.task_id)), "deliverable": False}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 2
    except (TaskError, ValueError, OSError, sqlite3.Error):
        print(json.dumps({"ok": False, "code": "INPUT_OR_STORE_ERROR", "deliverable": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
