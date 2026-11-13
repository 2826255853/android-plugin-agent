"""Build temporary, valid two-plugin packages for Android stage 1.3 isolation checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path


PLUGINS = (
    ("org.example.phase13.alpha", "Phase 1.3 Alpha", "alpha.zip"),
    ("org.example.phase13.beta", "Phase 1.3 Beta", "beta.zip"),
)


def build_package(plugin_id: str, title: str, output: Path) -> None:
    files = {
        "index.html": f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><link rel="stylesheet" href="app.css"></head>
<body><main><h1>{title}</h1><p id="origin"></p><p id="opens"></p><p id="storage"></p><p id="network"></p>
<button id="write">Write storage marker</button><button id="probe">Probe network</button>
<button id="navigate">External navigation</button><script src="app.js"></script></main></body></html>""".encode(),
        "app.js": f"""document.querySelector("#origin").textContent=location.origin;
let n=Number(localStorage.getItem("opens")||"0")+1;
localStorage.setItem("opens",String(n));
document.querySelector("#opens").textContent="opens="+n;
function show(){{document.querySelector("#storage").textContent="marker="+(localStorage.getItem("marker")||"empty")}}
show();
document.querySelector("#write").onclick=()=>{{localStorage.setItem("marker","{plugin_id}");show()}};
document.querySelector("#probe").onclick=()=>fetch("https://example.com",{{mode:"no-cors"}})
  .then(()=>document.querySelector("#network").textContent="network=unexpected")
  .catch(()=>document.querySelector("#network").textContent="network=blocked");
document.querySelector("#navigate").onclick=()=>location.href="https://example.com";""".encode(),
        "app.css": b"body{font:18px sans-serif;padding:24px}button{display:block;margin:16px 0}",
    }
    manifest = {
        "manifestVersion": 1,
        "id": plugin_id,
        "name": title,
        "version": "1.0.0",
        "hostApi": {"min": "1.0.0", "max": "1.0.0"},
        "entry": "index.html",
        "capabilities": [],
        "dataModel": {"version": "1.0.0", "collections": []},
        "files": [
            {"path": name, "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in sorted(files.items())
        ],
    }
    manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        write_entry(archive, "manifest.json", manifest_bytes)
        for name, data in files.items():
            write_entry(archive, name, data)


def write_entry(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(2026, 11, 13, 0, 0, 0))
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True, help="Temporary output directory for the ZIP files")
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for plugin_id, title, filename in PLUGINS:
        output = args.out_dir / filename
        build_package(plugin_id, title, output)
        print(f"{plugin_id}: {output}")


if __name__ == "__main__":
    main()
