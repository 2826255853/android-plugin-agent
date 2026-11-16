"""Serve a validated plugin package with an explicitly simulated browser host."""

from __future__ import annotations

import argparse
import hashlib
import http.server
import io
import json
import mimetypes
import socketserver
import webbrowser
import zipfile
from pathlib import Path
from typing import Any

from plugin_package import PackageError, validate_package


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop-generator" / "browser-preview" / "index.html"
ADAPTER = ROOT / "desktop-generator" / "browser-preview" / "adapter.js"
MIME = {".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".json": "application/json"}


def package_files(payload: bytes) -> tuple[dict[str, bytes], dict[str, Any]]:
    manifest = validate_package(payload)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        files = {entry["path"]: archive.read(entry["path"]) for entry in manifest["files"]}
        files["manifest.json"] = archive.read("manifest.json")
    return files, manifest


class PreviewServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], files: dict[str, bytes], manifest: dict[str, Any]):
        self.files = files
        self.manifest = manifest
        super().__init__(address, PreviewHandler)


class PreviewHandler(http.server.BaseHTTPRequestHandler):
    server: PreviewServer

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in {"/", "/index.html"}:
            port = str(self.server.server_address[1]).encode("ascii")
            shell = SHELL.read_bytes().replace(b"__ENTRY__", self.server.manifest["entry"].encode("ascii"))
            shell = shell.replace(b"__PORT__", port)
            self.respond(200, "text/html; charset=utf-8", shell)
            return
        if path == "/preview-config.js":
            config = json.dumps({"pluginId": self.server.manifest["id"],
                                 "capabilities": self.server.manifest["capabilities"],
                                 "dataModel": self.server.manifest["dataModel"]}, ensure_ascii=True)
            self.respond(200, "text/javascript; charset=utf-8", f"globalThis.PREVIEW_CONFIG={config};".encode())
            return
        if path == "/preview-adapter.js":
            self.respond(200, "text/javascript; charset=utf-8", ADAPTER.read_bytes())
            return
        if path in {"/preview-ui.js", "/preview.css"}:
            asset = ROOT / "desktop-generator" / "browser-preview" / path.rsplit("/", 1)[1]
            content_type = "text/css; charset=utf-8" if path.endswith(".css") else "text/javascript; charset=utf-8"
            self.respond(200, content_type, asset.read_bytes())
            return
        if path.startswith("/plugin/"):
            relative = path.removeprefix("/plugin/")
            content = self.server.files.get(relative)
            if content is None:
                self.respond(404, "text/plain; charset=utf-8", b"Not found")
                return
            if relative == self.server.manifest["entry"]:
                content = content.replace(b'<script src="app.js"></script>',
                    b'<script src="/preview-config.js"></script><script src="/preview-adapter.js"></script><script src="app.js"></script>')
            mime = MIME.get(Path(relative).suffix.lower()) or mimetypes.guess_type(relative)[0] or "application/octet-stream"
            self.respond(200, f"{mime}; charset=utf-8", content)
            return
        self.respond(404, "text/plain; charset=utf-8", b"Not found")

    def respond(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="A stage 2.3 verified ZIP package")
    parser.add_argument("--port", type=int, default=0, help="Loopback port; 0 selects an available port")
    parser.add_argument("--no-open", action="store_true", help="Do not open the default browser")
    args = parser.parse_args()
    try:
        payload = args.package.read_bytes()
        files, manifest = package_files(payload)
        server = PreviewServer(("127.0.0.1", args.port), files, manifest)
    except (OSError, PackageError, zipfile.BadZipFile, ValueError) as error:
        print(json.dumps({"ok": False, "code": getattr(error, "code", "PACKAGE_INVALID"), "deliverable": False}))
        return 2
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(json.dumps({"ok": True, "url": url, "pluginId": manifest["id"],
                      "packageSha256": hashlib.sha256(payload).hexdigest(), "simulation": True,
                      "simulatedCapabilities": [c for c in manifest["capabilities"] if c.startswith("data.")],
                      "executedGeneratedCode": False, "deliverable": False}), flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
