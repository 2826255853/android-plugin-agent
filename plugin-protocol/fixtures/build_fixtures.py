"""Build deterministic ZIP fixtures for the stage 0.3 protocol draft."""

from __future__ import annotations

import hashlib
import json
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


if __name__ == "__main__":
    main()
