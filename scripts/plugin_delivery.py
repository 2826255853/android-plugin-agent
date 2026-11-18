"""Explicit local package handoff; never installs or grants publication status.

The task store is the trusted source. A copied receipt alone is not proof of
origin, and transport success is not Android import or device acceptance.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any

from plugin_generation import TEMPLATE_VERSION, GenerationError, run_static_tests
from plugin_package import MAX_ZIP_BYTES, PackageError, validate_package
from task_runtime import DEFAULT_STORE, FILE_BYTES, TaskError, TaskStore, digest, encoded, no_links


class DeliveryError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def require(condition: Any, code: str) -> None:
    if not condition:
        raise DeliveryError(code)


def candidate(store: TaskStore, task_id: str) -> tuple[dict[str, Any], bytes]:
    """Recheck the retained successful build, not an editable exported ZIP."""
    with store._transaction() as db:
        task = store._task(db, task_id)
        require(task["status"] == "built", "BUILD_NOT_AVAILABLE")
        row = db.execute("SELECT seq, report_json, package FROM builds WHERE task_id = ? "
                         "AND package IS NOT NULL ORDER BY seq DESC LIMIT 1", (task_id,)).fetchone()
        require(row is not None, "BUILD_NOT_AVAILABLE")
        report = json.loads(row["report_json"])
        spec = json.loads(task["spec_json"])
        payload = bytes(row["package"])
        require(report.get("ok") is True and report.get("status") == "built"
                and report.get("stage") == "complete" and report.get("deliverable") is False
                and report.get("taskId") == task_id
                and report.get("specSha256") == task["spec_sha256"] == digest(encoded(spec))
                and report.get("specVersion") == task["spec_version"] == spec["version"]
                and task["project_id"] == spec["projectId"]
                and report.get("templateVersion") == TEMPLATE_VERSION,
                "BUILD_BINDING_MISMATCH")
        require(report.get("artifact", {}).get("sha256") == digest(payload)
                and report["artifact"].get("sizeBytes") == len(payload), "PACKAGE_HASH_MISMATCH")
        manifest = validate_package(payload, "org.generated." + task["project_id"])
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        hashes = {name: digest(raw) for name, raw in sorted(files.items())}
        require(report.get("files") == hashes and report.get("sourceSha256") == digest(encoded(hashes)),
                "BUILD_BINDING_MISMATCH")
        require(report.get("checks") == run_static_tests(files, spec), "BUILD_CHECKS_MISMATCH")
        receipt = {
            "deliveryVersion": 1, "purpose": "local-development-import", "deliverable": False,
            "taskId": task_id, "projectId": task["project_id"], "buildId": row["seq"],
            "specVersion": task["spec_version"], "specSha256": task["spec_sha256"],
            "templateVersion": report["templateVersion"], "checks": report["checks"],
            "pluginId": manifest["id"], "pluginName": manifest["name"], "pluginVersion": manifest["version"],
            "manifestSha256": digest(files["manifest.json"]),
            "packageFile": f"{manifest['id']}-{manifest['version']}.zip",
            "packageSha256": digest(payload), "sizeBytes": len(payload),
        }
        return receipt, payload


def _bounded_read(path: Path, maximum: int) -> bytes:
    no_links(path)
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    require(len(data) <= maximum, "DELIVERY_LIMIT_EXCEEDED")
    return data


def verify_delivery(store: TaskStore, task_id: str, directory: Path) -> dict[str, Any]:
    expected, payload = candidate(store, task_id)
    directory = Path(os.path.abspath(directory))
    no_links(directory)
    require(directory.is_dir(), "DELIVERY_NOT_FOUND")
    require({p.name for p in directory.iterdir()} == {"delivery.json", expected["packageFile"]},
            "DELIVERY_FILES_MISMATCH")
    # Exact canonical receipt bytes reject duplicate keys and altered qualification
    # claims without interpreting a sidecar as an independent trust authority.
    require(_bounded_read(directory / "delivery.json", FILE_BYTES) == encoded(expected) + b"\n",
            "RECEIPT_MISMATCH")
    require(_bounded_read(directory / expected["packageFile"], MAX_ZIP_BYTES) == payload,
            "PACKAGE_HASH_MISMATCH")
    return expected


def export_delivery(store: TaskStore, task_id: str, directory: Path) -> dict[str, Any]:
    receipt, payload = candidate(store, task_id)
    directory = Path(os.path.abspath(directory))
    no_links(directory)
    if directory.exists():
        return verify_delivery(store, task_id, directory)
    directory.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".delivery-", dir=directory.parent))
    try:
        (temporary / receipt["packageFile"]).write_bytes(payload)
        (temporary / "delivery.json").write_bytes(encoded(receipt) + b"\n")
        # Publish both files together; no existing destination is overwritten.
        temporary.rename(directory)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return receipt


class AdbTransport:
    """Opt-in, explicitly selected ADB transport with bounded commands."""
    def __init__(self, executable: str, serial: str, *, allow_physical: bool = False) -> None:
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", serial), "INVALID_DEVICE_SERIAL")
        self.executable, self.serial = executable, serial
        devices = self.run("devices", selected=False)
        selected = [line.split() for line in devices.splitlines() if line.split()[:1] == [serial]]
        require(len(selected) == 1 and selected[0][1:] == ["device"], "DEVICE_NOT_READY")
        emulator = self.run("shell", "getprop", "ro.kernel.qemu").strip() == "1"
        require(emulator or allow_physical, "PHYSICAL_DEVICE_REQUIRES_OPT_IN")

    def run(self, *args: str, selected: bool = True) -> str:
        command = [self.executable, *(["-s", self.serial] if selected else []), *args]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise DeliveryError("ADB_UNAVAILABLE_OR_TIMEOUT") from None
        require(result.returncode == 0, "ADB_COMMAND_FAILED")
        return result.stdout

    def remote_hash(self, path: str) -> str:
        result = self.run("shell", "toybox", "sha256sum", path).split()
        require(len(result) >= 2 and re.fullmatch(r"[0-9a-f]{64}", result[0]), "DEVICE_HASH_UNAVAILABLE")
        return result[0]


def push_delivery(store: TaskStore, task_id: str, directory: Path, transport: AdbTransport) -> dict[str, Any]:
    receipt = verify_delivery(store, task_id, directory)
    # Never use a name/path supplied by a copied receipt or arbitrary shell input.
    remote = f"/sdcard/Download/stage31-{receipt['taskId']}.zip"
    temporary = remote + ".part-" + uuid.uuid4().hex
    try:
        transport.run("push", str(Path(directory).absolute() / receipt["packageFile"]), temporary)
        require(transport.remote_hash(temporary) == receipt["packageSha256"], "DEVICE_HASH_MISMATCH")
        # Android shared storage does not support hardlinks. Toybox mv -n keeps
        # existing files intact; the following hash check detects a conflict.
        transport.run("shell", "mv", "-n", temporary, remote)
        require(transport.remote_hash(remote) == receipt["packageSha256"], "DEVICE_HASH_MISMATCH")
    finally:
        transport.run("shell", "rm", "-f", temporary)
    return {"ok": True, "status": "transferred", "deviceSerial": transport.serial,
            "remotePath": remote, "packageSha256": receipt["packageSha256"],
            "pluginId": receipt["pluginId"], "pluginVersion": receipt["pluginVersion"],
            "imported": False, "deliverable": False}


def main() -> int:
    parser = argparse.ArgumentParser(description="Export, verify and explicitly transfer a local development package.")
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("export", "verify", "push"):
        sub = commands.add_parser(name)
        sub.add_argument("task_id")
        sub.add_argument("--directory", type=Path, required=True)
        if name == "push":
            sub.add_argument("--serial", required=True, help="Explicit serial from adb devices; never chooses a default device")
            sub.add_argument("--adb", default=str(Path(os.environ.get("LOCALAPPDATA", "")) / "Android/Sdk/platform-tools/adb.exe") if os.name == "nt" else "adb")
            sub.add_argument("--allow-physical", action="store_true", help="Explicitly permit physical-device transfer")
    args = parser.parse_args()
    try:
        store = TaskStore(args.store)
        if args.command == "export":
            result = {"ok": True, "status": "exported", **export_delivery(store, args.task_id, args.directory)}
        elif args.command == "verify":
            result = {"ok": True, "status": "verified", **verify_delivery(store, args.task_id, args.directory)}
        else:
            verify_delivery(store, args.task_id, args.directory)
            transport = AdbTransport(args.adb, args.serial, allow_physical=args.allow_physical)
            result = push_delivery(store, args.task_id, args.directory, transport)
    except (DeliveryError, TaskError, PackageError, GenerationError) as error:
        result = {"ok": False, "code": error.code, "deliverable": False}
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
        result = {"ok": False, "code": "DELIVERY_INVALID_OR_UNAVAILABLE", "deliverable": False}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
