"""Run the real Android importer/storage regression without third-party test dependencies.

Builds disposable fixtures into the instrumentation APK, never the host APK.
UI/WebView acceptance remains in verify-plugin-data.mjs and verify-bridge.mjs.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "plugin-protocol" / "fixtures"


def prepare_assets(directory: Path) -> None:
    spec = importlib.util.spec_from_file_location("contract", FIXTURES / "verify_fixtures.py")
    contract = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(contract)
    directory.mkdir(parents=True)
    expected = json.loads((FIXTURES / "expected-errors.json").read_text(encoding="utf-8"))
    expected.update({"valid-minimal-v1.zip": "ACCEPTED", "valid-known-capability-v1.zip": "ACCEPTED"})
    for name in expected:
        shutil.copyfile(FIXTURES / name, directory / name)
    for name, _, code in contract.boundary_cases(directory):
        expected[name] = code

    # Keep the payload/hash valid and corrupt only its advertised CRC, for both ZIP methods.
    for method, label in [(zipfile.ZIP_STORED, "stored"), (zipfile.ZIP_DEFLATED, "deflated")]:
        path = directory / f"invalid-crc-{label}.zip"
        contract.build_package(path, {"index.html": b"<html>CRC sample</html>"}, compression=method)
        data = bytearray(path.read_bytes())
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo("index.html")
            struct.pack_into("<I", data, info.header_offset + 14, info.CRC ^ 1)
            offset = archive.start_dir
            for entry in archive.infolist():
                if entry.filename == "index.html":
                    struct.pack_into("<I", data, offset + 16, info.CRC ^ 1)
                offset += 46 + sum(struct.unpack_from("<HHH", data, offset + 28))
        path.write_bytes(data)
        expected[path.name] = "PACKAGE_CORRUPT"

    # These bounds distinguish adding a restriction from relaxing an existing restriction.
    fields = [
        {"id": "title", "type": "string", "required": True},
        {"id": "amount", "type": "number", "required": False},
        {"id": "bounded", "type": "string", "required": False, "maxLength": 120},
        {"id": "score", "type": "integer", "required": False, "minimum": 0, "maximum": 10},
        {"id": "large", "type": "integer", "required": False, "minimum": 9007199254740992, "maximum": 9007199254740993},
    ]
    baseline = json.loads(contract.base_manifest({"index.html": b"<html></html>"}))
    baseline["dataModel"]["collections"] = [{"id": "notes", "fields": fields}]

    def package(name: str, manifest: dict) -> None:
        contract.write_zip(directory / name, [
            ("manifest.json", json.dumps(manifest).encode("utf-8")),
            ("index.html", b"<html></html>"),
        ])

    package("upgrade-base.zip", baseline)
    upgrades = {}
    for change in ["same", "optional", "maxLength", "minimum", "maximum", "shorter", "required",
                   "delete", "type", "new-required", "major", "relaxed", "patch-change", "same-version-change",
                   "rollback", "reordered", "reencoded-number", "precise-minimum", "precise-maximum"]:
        manifest = copy.deepcopy(baseline)
        manifest["version"] = "1.1.0"
        model = manifest["dataModel"]
        model["version"] = "1.1.0"
        updated = model["collections"][0]["fields"]
        if change in {"optional", "new-required", "patch-change", "same-version-change"}:
            updated.append({"id": "extra", "type": "string", "required": change == "new-required"})
        if change == "maxLength": updated[0]["maxLength"] = 120
        if change == "minimum": updated[1]["minimum"] = 0
        if change == "maximum": updated[1]["maximum"] = 10
        if change == "shorter": updated[2]["maxLength"] = 119
        if change == "required": updated[1]["required"] = True
        if change == "delete": updated.pop()
        if change == "type": updated[1]["type"] = "string"
        if change == "major": model["version"] = "2.0.0"
        if change == "relaxed":
            updated[2].pop("maxLength")
            updated[3].pop("minimum")
            updated[3].pop("maximum")
        if change == "patch-change": model["version"] = "1.0.1"
        if change == "same-version-change": model["version"] = "1.0.0"
        if change == "rollback":
            manifest["version"] = "1.2.0"
            model["version"] = "1.0.0"
        if change == "reordered":
            model["version"] = "1.0.1"
            updated.reverse()
        if change == "reencoded-number":
            model["version"] = "1.0.1"
            updated[3]["minimum"] = 0.0
            updated[3]["maximum"] = 10.0
        if change == "precise-minimum": updated[4]["minimum"] = 9007199254740993
        if change == "precise-maximum": updated[4]["maximum"] = 9007199254740992
        name = f"upgrade-{change}.zip"
        package(name, manifest)
        upgrades[name] = "ACCEPTED" if change in {"same", "optional", "relaxed", "reordered", "reencoded-number"} else "DATA_MODEL_INCOMPATIBLE"
    (directory / "matrix.json").write_text(json.dumps({"imports": expected, "upgrades": upgrades}), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", default=os.environ.get("ANDROID_SERIAL", "emulator-5554"))
    args = parser.parse_args()
    if not args.serial.startswith("emulator-"):
        parser.error("This regression uses an Android emulator; physical-device testing is optional and separate.")
    sdk = Path(os.environ.get("ANDROID_HOME", Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk"))
    adb = str(sdk / "platform-tools" / "adb.exe")

    def run(*command: str, **kwargs):
        return subprocess.run(command, check=True, **kwargs)

    run(adb, "-s", args.serial, "get-state")
    with tempfile.TemporaryDirectory(prefix="stage15-") as temp:
        assets = Path(temp) / "assets"
        prepare_assets(assets / "stage15")
        run(str(ROOT / "android-app" / "gradlew.bat"), ":app:assembleDebug", ":app:assembleDebugAndroidTest",
            f"-Pstage15Assets={assets.as_posix()}", "--offline", cwd=ROOT / "android-app")
        for apk in ["apk/debug/app-debug.apk", "apk/androidTest/debug/app-debug-androidTest.apk"]:
            run(adb, "-s", args.serial, "install", "-r", str(ROOT / "android-app" / "app" / "build" / "outputs" / apk))
        result = run(adb, "-s", args.serial, "shell", "am", "instrument", "-w",
                     "com.example.pluginhost.test/com.example.pluginhost.Stage15Instrumentation",
                     capture_output=True, text=True, encoding="utf-8")
        print(result.stdout)
        if "STAGE15 PASSED" not in result.stdout or "STAGE15 FAILED" in result.stdout:
            raise RuntimeError("Android stage 1.5 regression failed; see instrumentation results above")


if __name__ == "__main__":
    main()
