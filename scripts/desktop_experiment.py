"""Reproducible desktop experiment for stage 0.5.

The experiment intentionally uses only the Python standard library.  It copies a
small static plugin into a task workspace, builds a deterministic ZIP package,
serves the package from a loopback-only preview server, and records expected
failure cases.  It is a build/preview probe, not the production generation
service or an Android importer.
"""

from __future__ import annotations

import argparse
import hashlib
import html.parser
import http.server
import json
import re
import shutil
import socketserver
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "plugin-protocol" / "fixtures" / "source" / "minimal"
DEFAULT_EXPERIMENT = ROOT / "desktop-experiment"
DEFAULT_WORKSPACE = DEFAULT_EXPERIMENT / "workspace" / "quick-notes"
DEFAULT_ARTIFACT = DEFAULT_EXPERIMENT / "artifacts" / "quick-notes-v1.zip"
DEFAULT_REPORT = DEFAULT_EXPERIMENT / "reports" / "stage-0.5-report.json"
FIXED_TIME = (2020, 1, 1, 0, 0, 0)
RESOURCE_PATH = re.compile(r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*$")


class ExperimentError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def fail(code: str, message: str, **details: Any) -> None:
    raise ExperimentError(code, message, **details)


def safe_path(base: Path, relative: str) -> Path:
    """Resolve a relative workspace path without allowing an escape."""
    if not relative or "\\" in relative or PurePosixPath(relative).is_absolute() or Path(relative).drive:
        fail("WORKSPACE_ESCAPE", "workspace paths must use relative POSIX syntax", path=relative)
    candidate = (base / Path(*PurePosixPath(relative).parts)).resolve()
    base_resolved = base.resolve()
    try:
        candidate.relative_to(base_resolved)
    except ValueError:
        fail("WORKSPACE_ESCAPE", "path leaves the task workspace", path=relative)
    return candidate


def copy_source(source: Path, workspace: Path) -> list[str]:
    source = source.resolve()
    if not source.is_dir():
        fail("SOURCE_NOT_FOUND", "sample source directory does not exist", source=str(source))
    workspace.parent.mkdir(parents=True, exist_ok=True)
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    copied: list[str] = []
    for source_file in sorted(source.rglob("*")):
        if not source_file.is_file():
            continue
        relative = source_file.relative_to(source).as_posix()
        target = safe_path(workspace, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_file, target)
        copied.append(relative)
    if not copied:
        fail("SOURCE_EMPTY", "sample source directory contains no files")
    return copied


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return str(resolved)


def experiment_path(path: Path, label: str) -> Path:
    resolved = path.resolve()
    base = DEFAULT_EXPERIMENT.resolve()
    try:
        relative = resolved.relative_to(base)
    except ValueError:
        fail("OUTPUT_OUTSIDE_EXPERIMENT", f"{label} must stay under desktop-experiment", path=str(path))
    if not relative.parts:
        fail("OUTPUT_OUTSIDE_EXPERIMENT", f"{label} cannot be the experiment root", path=str(path))
    return resolved


def make_manifest(files: dict[str, bytes]) -> bytes:
    manifest = {
        "manifestVersion": 1,
        "id": "org.example.quick-notes",
        "name": "Quick Notes",
        "version": "1.0.0",
        "hostApi": {"min": "1.0.0", "max": "1.0.0"},
        "entry": "index.html",
        "capabilities": [],
        "dataModel": {
            "version": "1.0.0",
            "collections": [
                {
                    "id": "notes",
                    "fields": [
                        {"id": "title", "type": "string", "required": True, "maxLength": 120},
                        {"id": "body", "type": "string", "required": False, "maxLength": 5000},
                    ],
                }
            ],
        },
        "files": [
            {"path": path, "sha256": sha256_bytes(content)}
            for path, content in sorted(files.items())
        ],
    }
    return (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def build_package(source: Path, workspace: Path, artifact: Path) -> dict[str, Any]:
    workspace = experiment_path(workspace, "workspace")
    artifact = experiment_path(artifact, "artifact")
    copied = copy_source(source, workspace)
    files = {relative: safe_path(workspace, relative).read_bytes() for relative in copied}
    manifest = make_manifest(files)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    entries = [("manifest.json", manifest), *sorted(files.items())]
    with tempfile.NamedTemporaryFile(prefix="plugin-build-", suffix=".zip", dir=artifact.parent, delete=False) as temp:
        temporary = Path(temp.name)
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            for name, content in entries:
                archive.writestr(zip_info(name), content)
        temporary.replace(artifact)
    finally:
        temporary.unlink(missing_ok=True)
    package = validate_package(artifact)
    package.update({"workspace": display_path(workspace), "artifact": display_path(artifact), "copiedFiles": copied})
    return package


def validate_package(artifact: Path) -> dict[str, Any]:
    if not artifact.is_file():
        fail("ARTIFACT_NOT_FOUND", "package does not exist", artifact=str(artifact))
    try:
        with zipfile.ZipFile(artifact) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                fail("DUPLICATE_ENTRY", "package contains duplicate ZIP entries")
            if any(not RESOURCE_PATH.fullmatch(name) for name in names):
                fail("INVALID_ENTRY_PATH", "package contains an invalid resource path")
            if any(".." in PurePosixPath(name).parts for name in names):
                fail("PATH_TRAVERSAL", "package contains a path traversal entry")
            if "manifest.json" not in names:
                fail("MISSING_MANIFEST", "package does not contain manifest.json")
            try:
                manifest = json.loads(archive.read("manifest.json"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                fail("INVALID_MANIFEST", "manifest.json is not valid UTF-8 JSON", error=str(error))
            if not isinstance(manifest, dict):
                fail("INVALID_MANIFEST", "manifest root must be an object")
            if manifest.get("manifestVersion") != 1 or manifest.get("entry") != "index.html":
                fail("INCOMPATIBLE_MANIFEST", "manifest is not compatible with the stage 0 protocol")
            file_items = manifest.get("files")
            if not isinstance(file_items, list) or any(not isinstance(item, dict) for item in file_items):
                fail("INVALID_MANIFEST", "manifest files must be an array of objects")
            declared = {item.get("path"): item.get("sha256") for item in file_items}
            if len(declared) != len(file_items) or any(
                not isinstance(path, str) or not RESOURCE_PATH.fullmatch(path) or not isinstance(digest, str)
                for path, digest in declared.items()
            ):
                fail("INVALID_MANIFEST", "manifest contains invalid or duplicate file declarations")
            actual = {name: sha256_bytes(archive.read(name)) for name in names if name != "manifest.json"}
            if set(declared) != set(actual):
                fail("FILE_LIST_MISMATCH", "manifest file list does not match ZIP entries")
            for name, digest in actual.items():
                if declared[name] != digest:
                    fail("HASH_MISMATCH", "resource hash differs from manifest", path=name)
            if manifest["entry"] not in actual:
                fail("ENTRY_NOT_FOUND", "manifest entry is missing", entry=manifest["entry"])
            return {
                "valid": True,
                "manifestId": manifest.get("id"),
                "version": manifest.get("version"),
                "entry": manifest["entry"],
                "files": sorted(actual),
                "sha256": sha256_bytes(artifact.read_bytes()),
                "sizeBytes": artifact.stat().st_size,
            }
    except zipfile.BadZipFile as error:
        fail("INVALID_ZIP", "package is not a readable ZIP archive", error=str(error))


class _AssetHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:
        return


class _PreviewServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class _ReferenceParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.references: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if tag == "script" and name == "src" and value:
                self.references.append(value)
            if tag == "link" and name == "href" and value:
                self.references.append(value)


def preview_package(artifact: Path) -> dict[str, Any]:
    validate_package(artifact)
    with tempfile.TemporaryDirectory(prefix="plugin-preview-") as temporary:
        preview_root = Path(temporary)
        with zipfile.ZipFile(artifact) as archive:
            archive.extractall(preview_root)
        handler = lambda *args, **kwargs: _AssetHandler(*args, directory=str(preview_root), **kwargs)
        server = _PreviewServer(("127.0.0.1", 0), handler)
        try:
            origin = f"http://127.0.0.1:{server.server_port}"
            import threading

            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                entry_url = f"{origin}/index.html"
                with urllib.request.urlopen(entry_url, timeout=3) as response:
                    entry = response.read()
                    if response.status != 200:
                        fail("PREVIEW_ENTRY_FAILED", "preview entry did not return HTTP 200", status=response.status)
                parser = _ReferenceParser()
                parser.feed(entry.decode("utf-8"))
                fetched: list[str] = []
                for reference in parser.references:
                    if not reference or reference.startswith(("/", "http:", "https:", "//")) or ".." in PurePosixPath(reference).parts:
                        fail("PREVIEW_REFERENCE_REJECTED", "preview reference leaves the static package", reference=reference)
                    with urllib.request.urlopen(f"{origin}/{reference}", timeout=3) as response:
                        if response.status != 200:
                            fail("PREVIEW_ASSET_FAILED", "preview asset did not return HTTP 200", reference=reference)
                        response.read()
                    fetched.append(reference)
                return {
                    "preview": "passed",
                    "origin": "http://127.0.0.1:<ephemeral-port>",
                    "entry": "/index.html",
                    "fetchedAssets": fetched,
                    "simulatedHostCapabilities": [],
                    "deviceValidationRequired": True,
                }
            finally:
                server.shutdown()
                thread.join(timeout=3)
        finally:
            server.server_close()


def record_failures(artifact: Path, workspace: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def expect(name: str, expected: str, operation: Any) -> None:
        try:
            operation()
        except ExperimentError as error:
            if error.code != expected:
                fail("FAILURE_RECORD_MISMATCH", "failure scenario returned an unexpected code", scenario=name, actual=error.code)
            records.append({"scenario": name, "result": "rejected", "code": error.code, "message": str(error)})
            return
        fail("FAILURE_NOT_REJECTED", "failure scenario unexpectedly succeeded", scenario=name)

    expect("workspace escape", "WORKSPACE_ESCAPE", lambda: safe_path(workspace, "../outside.txt"))
    expect("missing manifest", "MISSING_MANIFEST", lambda: _validate_missing_manifest())
    expect("truncated package", "INVALID_ZIP", lambda: _validate_bytes(artifact.read_bytes()[:-22]))
    return records


def _validate_bytes(content: bytes) -> None:
    with tempfile.NamedTemporaryFile(prefix="plugin-invalid-", suffix=".zip", delete=False) as temp:
        path = Path(temp.name)
        temp.write(content)
    try:
        validate_package(path)
    finally:
        path.unlink(missing_ok=True)


def _validate_missing_manifest() -> None:
    with tempfile.NamedTemporaryFile(prefix="plugin-missing-manifest-", suffix=".zip", delete=False) as temp:
        path = Path(temp.name)
    try:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(zip_info("index.html"), b"<!doctype html>")
        validate_package(path)
    finally:
        path.unlink(missing_ok=True)


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "build":
        return {"build": build_package(args.source, args.workspace, args.artifact)}
    if args.command == "preview":
        return {"preview": preview_package(args.artifact)}
    if args.command == "failures":
        return {"failures": record_failures(args.artifact, args.workspace)}
    build = build_package(args.source, args.workspace, args.artifact)
    preview = preview_package(args.artifact)
    failures = record_failures(args.artifact, args.workspace)
    result = {
        "stage": "0.5",
        "status": "passed",
        "build": build,
        "preview": preview,
        "failures": failures,
    }
    report = experiment_path(args.report, "report")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["all", "build", "preview", "failures"])
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    try:
        result = run(args)
    except ExperimentError as error:
        print(json.dumps({"status": "failed", "code": error.code, "message": str(error), **error.details}, ensure_ascii=False))
        raise SystemExit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
