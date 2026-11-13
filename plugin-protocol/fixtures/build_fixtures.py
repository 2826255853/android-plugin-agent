"""Build deterministic ZIP fixtures for the stage 0.3 protocol draft."""

from __future__ import annotations

import hashlib
import json
import copy
import warnings
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "source" / "minimal"
FIXTURES = ROOT
FIXED_TIME = (2020, 1, 1, 0, 0, 0)


def manifest_for(files: dict[str, bytes]) -> bytes:
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
            {"path": path, "sha256": hashlib.sha256(content).hexdigest()}
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


def write_zip(path: Path, entries: list[tuple[str, bytes]], allow_duplicate: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with warnings.catch_warnings():
        if allow_duplicate:
            warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "w") as archive:
            for name, content in entries:
                archive.writestr(zip_info(name), content)


def write_package(name: str, manifest: bytes, files: dict[str, bytes], extra=None) -> None:
    entries = [("manifest.json", manifest), *sorted(files.items()), *(extra or [])]
    write_zip(FIXTURES / name, entries)


def manifest_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def main() -> None:
    files = {file.name: file.read_bytes() for file in sorted(SOURCE.iterdir()) if file.is_file()}
    manifest = manifest_for(files)

    write_zip(
        FIXTURES / "valid-minimal-v1.zip",
        [("manifest.json", manifest), *sorted(files.items())],
    )
    write_zip(FIXTURES / "invalid-missing-manifest.zip", [("index.html", files["index.html"])])

    traversal_files = {**files, "../escape.js": b"must not be extracted"}
    traversal_manifest = manifest_for(traversal_files)
    write_zip(
        FIXTURES / "invalid-path-traversal.zip",
        [("manifest.json", traversal_manifest), ("../escape.js", traversal_files["../escape.js"]), *sorted(files.items())],
    )

    write_zip(
        FIXTURES / "invalid-duplicate-entry.zip",
        [("manifest.json", manifest), *sorted(files.items()), ("index.html", files["index.html"])],
        allow_duplicate=True,
    )

    valid_bytes = (FIXTURES / "valid-minimal-v1.zip").read_bytes()
    (FIXTURES / "invalid-truncated.zip").write_bytes(valid_bytes[:-22])

    cases = []

    def altered(name: str, expected: str, change) -> None:
        value = json.loads(manifest)
        change(value)
        cases.append((name, value, expected))

    altered("invalid-unknown-field.zip", "MANIFEST_INVALID", lambda value: value.update({"unexpected": True}))
    altered("invalid-manifest-version.zip", "MANIFEST_VERSION_UNSUPPORTED", lambda value: value.update({"manifestVersion": 2}))
    altered(
        "invalid-host-api-range.zip",
        "MANIFEST_INVALID",
        lambda value: value["hostApi"].update({"min": "1.2.0", "max": "1.1.0"}),
    )
    altered(
        "invalid-duplicate-field-id.zip",
        "MANIFEST_INVALID",
        lambda value: value["dataModel"]["collections"][0]["fields"].append(
            copy.deepcopy(value["dataModel"]["collections"][0]["fields"][0])
        ),
    )
    altered(
        "invalid-default-type.zip",
        "MANIFEST_INVALID",
        lambda value: value["dataModel"]["collections"][0]["fields"][0].update({"default": 7}),
    )
    altered(
        "invalid-unknown-capability.zip",
        "CAPABILITY_UNKNOWN",
        lambda value: value.update({"capabilities": ["device.camera"]}),
    )
    altered("invalid-entry-not-declared.zip", "RESOURCE_MISSING", lambda value: value.update({"entry": "missing.html"}))
    altered(
        "invalid-hash-mismatch.zip",
        "RESOURCE_HASH_MISMATCH",
        lambda value: value["files"][0].update({"sha256": "0" * 64}),
    )
    altered(
        "invalid-duplicate-file-declaration.zip",
        "MANIFEST_INVALID",
        lambda value: value["files"].append(copy.deepcopy(value["files"][0])),
    )

    for name, value, _expected in cases:
        write_package(name, manifest_bytes(value), files)

    duplicate_json = manifest.replace(b'"manifestVersion": 1,', b'"manifestVersion": 1,\n  "manifestVersion": 1,', 1)
    write_package("invalid-duplicate-json-key.zip", duplicate_json, files)

    write_package(
        "invalid-missing-resource.zip",
        manifest,
        {name: content for name, content in files.items() if name != "app.js"},
    )
    write_package("invalid-undeclared-resource.zip", manifest, files, [("extra.json", b"{}")])

    external_files = dict(files)
    external_files["index.html"] = files["index.html"].replace(
        b'src="app.js"', b'src="https://example.invalid/app.js"'
    )
    write_package("invalid-external-resource.zip", manifest_for(external_files), external_files)
    known_capability = manifest.replace(b'"capabilities": [],', b'"capabilities": [\n    "data.read"\n  ],', 1)
    write_package("valid-known-capability-v1.zip", known_capability, files)

    expected_errors = {name: expected for name, _value, expected in cases}
    expected_errors.update(
        {
            "invalid-missing-manifest.zip": "MANIFEST_MISSING",
            "invalid-path-traversal.zip": "PACKAGE_ENTRY_INVALID",
            "invalid-duplicate-entry.zip": "PACKAGE_DUPLICATE_ENTRY",
            "invalid-truncated.zip": "PACKAGE_CORRUPT",
            "invalid-duplicate-json-key.zip": "MANIFEST_INVALID",
            "invalid-missing-resource.zip": "RESOURCE_MISSING",
            "invalid-undeclared-resource.zip": "RESOURCE_UNDECLARED",
            "invalid-external-resource.zip": "RESOURCE_REFERENCE_INVALID",
        }
    )
    (FIXTURES / "expected-errors.json").write_text(
        json.dumps(expected_errors, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
