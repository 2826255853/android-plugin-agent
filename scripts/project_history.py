"""Read-only project and version history across confirmed specs and local tasks."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

from task_runtime import DEFAULT_STORE, digest, encoded


DEFAULT_SPEC_STORE = Path(__file__).resolve().parents[1] / "desktop-spec" / "projects"


def project_history(project_id: str, spec_store: Path, task_store: Path) -> dict[str, Any]:
    if re.fullmatch(r"[a-z][a-z0-9-]{2,63}", project_id) is None:
        raise ValueError("INVALID_PROJECT_ID")
    project_dir = spec_store / project_id
    specs: dict[int, dict[str, Any]] = {}
    if project_dir.exists():
        for path in project_dir.glob("v*.json"):
            if re.fullmatch(r"v[1-9][0-9]*\.json", path.name) is None:
                continue
            spec = json.loads(path.read_text(encoding="utf-8"))
            version = int(path.stem[1:])
            if (spec.get("projectId") != project_id or spec.get("version") != version
                    or spec.get("status") != "confirmed"):
                raise ValueError("SPEC_VERSION_BINDING_MISMATCH")
            specs[version] = {"version": version, "title": spec["title"],
                              "sha256": digest(encoded(spec)), "tasks": []}
            if "revision" in spec:
                specs[version]["revision"] = spec["revision"]

    database = task_store / "tasks.sqlite3"
    if database.exists():
        with closing(sqlite3.connect(database)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT id, project_id, spec_version, spec_sha256, spec_json, status, "
                              "created_at, updated_at, finished_at FROM tasks WHERE project_id = ? "
                              "ORDER BY spec_version, created_at, id", (project_id,)).fetchall()
            for row in rows:
                task_spec = json.loads(row["spec_json"])
                version = row["spec_version"]
                item = specs.setdefault(version, {"version": version, "title": task_spec.get("title", ""),
                                                   "sha256": row["spec_sha256"], "tasks": []})
                if item["sha256"] != row["spec_sha256"] or digest(encoded(task_spec)) != row["spec_sha256"]:
                    raise ValueError("TASK_SPEC_BINDING_MISMATCH")
                if "revision" in task_spec:
                    item["revision"] = task_spec["revision"]
                builds = db.execute("SELECT seq, repair_round, report_json, package FROM builds "
                                    "WHERE task_id = ? ORDER BY seq", (row["id"],)).fetchall()
                build_history = []
                for build in builds:
                    report = json.loads(build["report_json"])
                    if (report.get("taskId") != row["id"] or report.get("specSha256") != row["spec_sha256"]
                            or report.get("specVersion") != version):
                        raise ValueError("BUILD_TASK_BINDING_MISMATCH")
                    artifact = report.get("artifact", {})
                    package = bytes(build["package"]) if build["package"] is not None else None
                    if package is not None and digest(package) != artifact.get("sha256"):
                        raise ValueError("BUILD_PACKAGE_HASH_MISMATCH")
                    if (package is not None) != (report.get("ok") is True):
                        raise ValueError("BUILD_PACKAGE_STATUS_MISMATCH")
                    build_history.append({"buildId": build["seq"], "repairRound": build["repair_round"],
                                          "ok": report.get("ok") is True, "stage": report.get("stage"),
                                          "code": report.get("code"), "checks": report.get("checks", []),
                                          "pluginId": "org.generated." + project_id if package is not None else None,
                                          "pluginVersion": f"{version}.0.0" if package is not None else None,
                                          "packageSha256": artifact.get("sha256") if package is not None else None})
                item["tasks"].append({"taskId": row["id"], "status": row["status"],
                                       "specSha256": row["spec_sha256"], "createdAt": row["created_at"],
                                       "updatedAt": row["updated_at"], "finishedAt": row["finished_at"],
                                       "builds": build_history})

    versions = sorted(specs.values(), key=lambda item: item["version"])
    latest_usable = None
    for version in versions:
        for task in version["tasks"]:
            for build in task["builds"]:
                if build["ok"] and task["status"] == "built":
                    latest_usable = {"specVersion": version["version"], "taskId": task["taskId"],
                                     "buildId": build["buildId"], "pluginId": build["pluginId"],
                                     "pluginVersion": build["pluginVersion"],
                                     "packageSha256": build["packageSha256"]}
    return {"projectId": project_id, "versions": versions, "latestUsableBuild": latest_usable,
            "installedVersion": None, "installedVersionSource": "Android host list; not synchronized",
            "deliverable": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_id")
    parser.add_argument("--spec-store", type=Path, default=DEFAULT_SPEC_STORE)
    parser.add_argument("--task-store", type=Path, default=DEFAULT_STORE)
    args = parser.parse_args()
    try:
        result = project_history(args.project_id, args.spec_store, args.task_store)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError, KeyError) as exc:
        code = str(exc) if str(exc) and str(exc).isupper() else "PROJECT_HISTORY_UNAVAILABLE"
        print(json.dumps({"ok": False, "code": code, "deliverable": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
