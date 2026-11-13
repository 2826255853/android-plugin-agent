"""Run the stage 1.1 plugin contract fixture matrix.

The script intentionally uses only the Python standard library.  The Android
importer is implemented in stage 1.2; this verifier checks the frozen package
contract and produces deterministic boundary fixtures in a temporary folder.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
EXPECTED = ROOT / "expected-errors.json"
MAX_ZIP_BYTES = 10 * 1024 * 1024
MAX_ENTRIES = 128
MAX_RESOURCE_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
RESOURCE_PATH = re.compile(r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*$")
ALLOWED_EXTENSIONS = {".html", ".js", ".css", ".json", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".woff2", ".ttf", ".ico"}
KNOWN_CAPABILITIES = {"data.read", "data.write", "ui.toast"}


class ContractError(Exception):
    def __init__(self, code: str, **details: Any) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def bounded_payload(length: int) -> bytes:
    """Return deterministic data that stays below the 100:1 ratio limit."""
    seed = b"".join(hashlib.sha256(index.to_bytes(4, "big")).digest() for index in range(2048))
    tail_length = min(32768, length)
    return b"x" * (length - tail_length) + seed[:tail_length]


def duplicate_key_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("MANIFEST_INVALID", reason="duplicate-json-key")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise ContractError("MANIFEST_INVALID", reason=f"non-standard-number:{value}")


def parse_manifest(raw: bytes) -> dict[str, Any]:
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ContractError("MANIFEST_INVALID", reason="bom")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ContractError("MANIFEST_INVALID", reason="utf8") from None
    try:
        value = json.loads(
            text,
            object_pairs_hook=duplicate_key_object,
            parse_constant=reject_constant,
        )
    except ContractError:
        raise
    except (TypeError, json.JSONDecodeError):
        raise ContractError("MANIFEST_INVALID", reason="json") from None
    if not isinstance(value, dict):
        raise ContractError("MANIFEST_INVALID", reason="root-type")
    return value


def version_tuple(value: Any) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", value):
        raise ContractError("MANIFEST_INVALID", reason="version")
    return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]


def validate_manifest(manifest: dict[str, Any], names: set[str]) -> None:
    required = {"manifestVersion", "id", "name", "version", "hostApi", "entry", "capabilities", "dataModel", "files"}
    if set(manifest) != required:
        raise ContractError("MANIFEST_INVALID", reason="fields")
    if manifest["manifestVersion"] != 1:
        raise ContractError("MANIFEST_VERSION_UNSUPPORTED")
    version_tuple(manifest["version"])
    host_api = manifest["hostApi"]
    if not isinstance(host_api, dict) or set(host_api) != {"min", "max"}:
        raise ContractError("MANIFEST_INVALID", reason="host-api")
    minimum, maximum = version_tuple(host_api["min"]), version_tuple(host_api["max"])
    if minimum[0] != maximum[0] or minimum > maximum:
        raise ContractError("MANIFEST_INVALID", reason="host-api-range")
    capabilities = manifest["capabilities"]
    if not isinstance(capabilities, list) or len(capabilities) != len(set(capabilities)):
        raise ContractError("MANIFEST_INVALID", reason="capabilities")
    if any(capability not in KNOWN_CAPABILITIES for capability in capabilities):
        raise ContractError("CAPABILITY_UNKNOWN")
    entry = manifest["entry"]
    if not isinstance(entry, str) or not RESOURCE_PATH.fullmatch(entry) or not entry.endswith(".html"):
        raise ContractError("MANIFEST_INVALID", reason="entry")
    files = manifest["files"]
    if not isinstance(files, list) or not files:
        raise ContractError("MANIFEST_INVALID", reason="files")
    declared: dict[str, str] = {}
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ContractError("MANIFEST_INVALID", reason="file-declaration")
        path, digest = item["path"], item["sha256"]
        if not isinstance(path, str) or path == "manifest.json" or not RESOURCE_PATH.fullmatch(path):
            raise ContractError("MANIFEST_INVALID", reason="file-path")
        if len(path) > 240 or any(len(part) > 64 for part in path.split("/")):
            raise ContractError("MANIFEST_INVALID", reason="file-path-length")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ContractError("MANIFEST_INVALID", reason="hash-format")
        if path in declared:
            raise ContractError("MANIFEST_INVALID", reason="duplicate-file")
        declared[path] = digest
    if entry not in declared:
        raise ContractError("RESOURCE_MISSING", reason="entry")
    if names - {"manifest.json"} != set(declared):
        if names - {"manifest.json"} - set(declared):
            raise ContractError("RESOURCE_UNDECLARED")
        raise ContractError("RESOURCE_MISSING")
    data_model = manifest["dataModel"]
    if not isinstance(data_model, dict) or set(data_model) != {"version", "collections"}:
        raise ContractError("MANIFEST_INVALID", reason="data-model")
    version_tuple(data_model["version"])
    collections = data_model["collections"]
    if not isinstance(collections, list):
        raise ContractError("MANIFEST_INVALID", reason="collections")
    collection_ids: set[str] = set()
    for collection in collections:
        if not isinstance(collection, dict) or set(collection) != {"id", "fields"}:
            raise ContractError("MANIFEST_INVALID", reason="collection")
        collection_id = collection["id"]
        if collection_id in collection_ids:
            raise ContractError("MANIFEST_INVALID", reason="duplicate-collection")
        collection_ids.add(collection_id)
        field_ids: set[str] = set()
        for field in collection["fields"]:
            if not isinstance(field, dict) or not {"id", "type", "required"}.issubset(field):
                raise ContractError("MANIFEST_INVALID", reason="field")
            field_id = field["id"]
            if field_id in field_ids:
                raise ContractError("MANIFEST_INVALID", reason="duplicate-field")
            field_ids.add(field_id)
            field_type = field["type"]
            default = field.get("default")
            if field_type == "string" and default is not None and not isinstance(default, str):
                raise ContractError("MANIFEST_INVALID", reason="default-type")
            if field_type == "integer" and default is not None and (not isinstance(default, int) or isinstance(default, bool)):
                raise ContractError("MANIFEST_INVALID", reason="default-type")
            if field_type == "number" and default is not None and (not isinstance(default, (int, float)) or isinstance(default, bool)):
                raise ContractError("MANIFEST_INVALID", reason="default-type")
            if field_type == "boolean" and default is not None and not isinstance(default, bool):
                raise ContractError("MANIFEST_INVALID", reason="default-type")
            if "minimum" in field and "maximum" in field and field["minimum"] > field["maximum"]:
                raise ContractError("MANIFEST_INVALID", reason="range")


def validate_package(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_ZIP_BYTES:
        raise ContractError("PACKAGE_LIMIT_EXCEEDED", reason="zip-size")
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile):
        raise ContractError("PACKAGE_CORRUPT") from None
    try:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ContractError("PACKAGE_LIMIT_EXCEEDED", reason="entry-count")
        names = [info.filename for info in infos]
        folded = [name.casefold() for name in names]
        if len(folded) != len(set(folded)):
            raise ContractError("PACKAGE_DUPLICATE_ENTRY")
        total = 0
        for info in infos:
            name = info.filename
            if info.is_dir() or info.flag_bits & 0x1 or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise ContractError("PACKAGE_ENTRY_INVALID")
            if not RESOURCE_PATH.fullmatch(name):
                raise ContractError("PACKAGE_ENTRY_INVALID")
            if any(part in {".", ".."} for part in Path(name).parts):
                raise ContractError("PACKAGE_ENTRY_INVALID")
            limit = MAX_MANIFEST_BYTES if name == "manifest.json" else MAX_RESOURCE_BYTES
            if info.file_size > limit:
                raise ContractError("PACKAGE_LIMIT_EXCEEDED", reason="entry-size", path=name)
            if info.file_size and (info.compress_size == 0 or info.file_size > MAX_COMPRESSION_RATIO * info.compress_size):
                raise ContractError("PACKAGE_LIMIT_EXCEEDED", reason="compression-ratio", path=name)
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise ContractError("PACKAGE_LIMIT_EXCEEDED", reason="total-size")
        if "manifest.json" not in names:
            raise ContractError("MANIFEST_MISSING")
        manifest = parse_manifest(archive.read("manifest.json"))
        validate_manifest(manifest, set(names))
        declared = {item["path"]: item["sha256"] for item in manifest["files"]}
        for name, digest in declared.items():
            content = archive.read(name)
            if sha256(content) != digest:
                raise ContractError("RESOURCE_HASH_MISMATCH", path=name)
            if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
                raise ContractError("PACKAGE_ENTRY_INVALID", reason="extension", path=name)
        index = archive.read(manifest["entry"])
        if re.search(rb"(?:https?:|//|file:|content:|javascript:)", index, re.IGNORECASE):
            raise ContractError("RESOURCE_REFERENCE_INVALID")
        return {"valid": True, "sizeBytes": path.stat().st_size, "entries": len(infos)}
    except zipfile.BadZipFile:
        raise ContractError("PACKAGE_CORRUPT") from None
    finally:
        archive.close()


def base_manifest(files: dict[str, bytes], *, manifest_size: int | None = None, file_count: int | None = None) -> bytes:
    if file_count is not None:
        files = {f"f-{index:03d}.json": b"{}" for index in range(file_count)}
    value = {
        "manifestVersion": 1,
        "id": "org.example.quick-notes",
        "name": "Quick Notes",
        "version": "1.0.0",
        "hostApi": {"min": "1.0.0", "max": "1.0.0"},
        "entry": "index.html",
        "capabilities": [],
        "dataModel": {"version": "1.0.0", "collections": []},
        "files": [{"path": name, "sha256": sha256(content)} for name, content in sorted(files.items())],
    }
    raw = (json.dumps(value, separators=(",", ":")) + "\n").encode()
    if manifest_size is not None:
        if len(raw) > manifest_size:
            raise ValueError("manifest base is larger than requested size")
        raw += b" " * (manifest_size - len(raw))
    return raw


def write_zip(path: Path, entries: list[tuple[str, bytes]], *, compression: int = zipfile.ZIP_STORED) -> None:
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for name, content in entries:
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = compression
            archive.writestr(info, content)


def build_package(path: Path, files: dict[str, bytes], *, manifest_size: int | None = None, compression: int = zipfile.ZIP_STORED) -> None:
    manifest = base_manifest(files, manifest_size=manifest_size)
    write_zip(path, [("manifest.json", manifest), *sorted(files.items())], compression=compression)


def expect(path: Path, expected: str) -> dict[str, Any]:
    try:
        result = validate_package(path)
        actual = "ACCEPTED" if result["valid"] else "REJECTED"
    except ContractError as error:
        actual = error.code
    if actual != expected:
        raise AssertionError(f"{path.name}: expected {expected}, got {actual}")
    return {"name": path.name, "expected": expected, "actual": actual, "sizeBytes": path.stat().st_size}


def boundary_cases(directory: Path) -> list[tuple[str, Path, str]]:
    cases: list[tuple[str, Path, str]] = []

    def add(name: str, files: dict[str, bytes], expected: str, **kwargs: Any) -> None:
        path = directory / name
        build_package(path, files, **kwargs)
        cases.append((name, path, expected))

    add("limit-single-file-at.zip", {"index.html": b"a" * MAX_RESOURCE_BYTES}, "ACCEPTED")
    add("limit-single-file-over.zip", {"index.html": b"a" * (MAX_RESOURCE_BYTES + 1)}, "PACKAGE_LIMIT_EXCEEDED")
    add("limit-entry-count-at.zip", {"index.html": b"<html></html>", **{f"f-{i:03d}.json": b"{}" for i in range(126)}}, "ACCEPTED")
    add("limit-entry-count-over.zip", {"index.html": b"<html></html>", **{f"f-{i:03d}.json": b"{}" for i in range(127)}}, "PACKAGE_LIMIT_EXCEEDED")
    add("limit-manifest-at.zip", {"index.html": b"<html></html>"}, "ACCEPTED", manifest_size=MAX_MANIFEST_BYTES)
    add("limit-manifest-over.zip", {"index.html": b"<html></html>"}, "PACKAGE_LIMIT_EXCEEDED", manifest_size=MAX_MANIFEST_BYTES + 1)

    total_files = {"index.html": b"<html></html>"}
    for index in range(9):
        total_files[f"part-{index:02d}.json"] = bounded_payload(MAX_RESOURCE_BYTES)
    total_files["part-09.json"] = b""
    manifest_len = len(base_manifest(total_files))
    total_files["part-09.json"] = bounded_payload(MAX_TOTAL_BYTES - manifest_len - sum(map(len, total_files.values())))
    add("limit-total-at.zip", total_files, "ACCEPTED", compression=zipfile.ZIP_DEFLATED)
    total_files["part-09.json"] += b"x"
    add("limit-total-over.zip", total_files, "PACKAGE_LIMIT_EXCEEDED", compression=zipfile.ZIP_DEFLATED)

    ratio_tail = b"".join(hashlib.sha256(index.to_bytes(4, "big")).digest() for index in range(1024))
    low, high = 0, len(ratio_tail)
    while low < high:
        mid = (low + high) // 2
        payload = b"0" * (MAX_RESOURCE_BYTES // 2) + ratio_tail[:mid]
        probe = directory / "ratio-probe.zip"
        build_package(probe, {"index.html": b"<html></html>", "payload.json": payload}, compression=zipfile.ZIP_DEFLATED)
        with zipfile.ZipFile(probe) as archive:
            info = archive.getinfo("payload.json")
            if info.file_size <= MAX_COMPRESSION_RATIO * info.compress_size:
                high = mid
            else:
                low = mid + 1
    at_payload = b"0" * (MAX_RESOURCE_BYTES // 2) + ratio_tail[:low]
    over_payload = b"0" * (MAX_RESOURCE_BYTES // 2) + ratio_tail[: max(0, low - 1)]
    add("limit-ratio-at.zip", {"index.html": b"<html></html>", "payload.json": at_payload}, "ACCEPTED", compression=zipfile.ZIP_DEFLATED)
    add("limit-ratio-over.zip", {"index.html": b"<html></html>", "payload.json": over_payload}, "PACKAGE_LIMIT_EXCEEDED", compression=zipfile.ZIP_DEFLATED)

    target = MAX_ZIP_BYTES
    fixed_files = {f"part-{index:02d}.json": b"x" * (MAX_RESOURCE_BYTES * 7 // 8) for index in range(5)}
    filler_name = "part-05.json"
    low, high = 0, MAX_RESOURCE_BYTES
    while low < high:
        mid = (low + high + 1) // 2
        fixed_files[filler_name] = b"x" * mid
        probe = directory / "zip-size-probe.zip"
        build_package(probe, {"index.html": b"<html></html>", **fixed_files})
        if probe.stat().st_size <= target:
            low = mid
        else:
            high = mid - 1
    fixed_files[filler_name] = b"x" * low
    add("limit-zip-at.zip", {"index.html": b"<html></html>", **fixed_files}, "ACCEPTED")
    fixed_files[filler_name] += b"x"
    add("limit-zip-over.zip", {"index.html": b"<html></html>", **fixed_files}, "PACKAGE_LIMIT_EXCEEDED")
    return cases


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, help="write a JSON evidence report")
    args = parser.parse_args()
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    results: list[dict[str, Any]] = []
    for name, code in expected.items():
        results.append(expect(ROOT / name, code))
    with tempfile.TemporaryDirectory(prefix="stage-1-1-fixtures-") as temporary:
        directory = Path(temporary)
        for name, path, code in boundary_cases(directory):
            results.append(expect(path, code))
    report = {"status": "passed", "fixedFixtures": len(expected), "boundaryFixtures": len(results) - len(expected), "results": results}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
