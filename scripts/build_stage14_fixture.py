"""Build a deterministic Quick Notes package for stage 1.4 Android acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "plugin-protocol" / "fixtures" / "source" / "quick-notes-v1"
FIXED_TIME = (2020, 1, 1, 0, 0, 0)


def build_package(
    plugin_id: str,
    output: Path,
    version: str = "1.1.0",
    model_version: str = "1.0.0",
) -> None:
    files = {
        path.name: path.read_bytes().replace(b"__PLUGIN_ID__", plugin_id.encode("ascii"))
        for path in sorted(SOURCE.iterdir())
        if path.is_file()
    }
    if set(files) != {"app.css", "app.js", "index.html"}:
        raise ValueError("Quick Notes source files are incomplete")
    manifest = {
        "manifestVersion": 1,
        "id": plugin_id,
        "name": "Quick Notes",
        "version": version,
        "hostApi": {"min": "1.0.0", "max": "1.0.0"},
        "entry": "index.html",
        "capabilities": ["data.read", "data.write"],
        "dataModel": {
            "version": model_version,
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
            {"path": name, "sha256": hashlib.sha256(content).hexdigest()}
            for name, content in sorted(files.items())
        ],
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w") as archive:
        entries = [("manifest.json", manifest_bytes), *sorted(files.items())]
        for name, content in entries:
            info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, required=True, help="output directory, preferably a temporary folder")
    parser.add_argument("--id", default="org.example.quick-notes", help="plugin ID; use a second ID to test storage isolation")
    parser.add_argument("--version", default="1.1.0", help="stable plugin version for upgrade and rollback fixtures")
    parser.add_argument("--model-version", default="1.0.0", help="stable data model version; incompatible changes are rejected")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+", args.id):
        parser.error("--id must be a lowercase reverse-domain plugin ID")
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", args.version):
        parser.error("--version must be a stable MAJOR.MINOR.PATCH version")
    output = args.out_dir / f"{args.id.rsplit('.', 1)[-1]}.zip"
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9]*)", args.model_version):
        parser.error("--model-version must be a stable MAJOR.MINOR.PATCH version")
    build_package(args.id, output, args.version, args.model_version)
    print(f"{output} {hashlib.sha256(output.read_bytes()).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
