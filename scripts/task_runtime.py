"""Stage 2.2 local task ledger and bounded tools; no model or code execution.

Only the trusted coordinator uses TaskStore lifecycle/accounting methods.
Untrusted agent requests enter through TaskStore.tool, never through a shell.
The store and workspace parent must be private to that coordinator. Path checks
are not an OS sandbox against another process that can rewrite those parents.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from spec_workflow import SpecError, validate_spec


DEFAULT_STORE = Path(__file__).resolve().parents[1] / "desktop-tasks"
ACTIVE = {"generating", "validating"}
TERMINAL = {"failed", "cancelled", "stopped"}
TOOLS = {"read_spec", "read_file", "apply_patch", "validate_spec", "run_tests"}
FILE_BYTES = 256 * 1024
WORKSPACE_BYTES = 2 * 1024 * 1024
FILE_COUNT = 64
INPUT_BYTES = 512 * 1024
MODEL_CALLS = 32
SECURITY_ERRORS = {"WORKSPACE_ESCAPE", "WORKSPACE_UNSAFE", "TASK_ID_MISMATCH", "TOOL_NOT_ALLOWED",
                   "PERMISSION_DENIED", "RESOURCE_HASH_MISMATCH", "PACKAGE_CORRUPT"}
TASK_ID = re.compile(r"task-[0-9a-f]{32}")
CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
PART = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
RESERVED = re.compile(r"(?:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?", re.I)


class TaskError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def need(condition: Any, code: str) -> None:
    if not condition:
        raise TaskError(code)


def encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def integer(value: Any, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def no_links(path: Path) -> None:
    """Reject symlinks, Windows junctions/reparse points and hardlinked files."""
    for part in (*reversed(path.parents), path):
        if not os.path.lexists(part):
            continue
        info = part.lstat()
        need(not stat.S_ISLNK(info.st_mode), "WORKSPACE_ESCAPE")
        need(not (getattr(info, "st_file_attributes", 0) & 0x400), "WORKSPACE_ESCAPE")
        need(not stat.S_ISREG(info.st_mode) or info.st_nlink == 1, "WORKSPACE_ESCAPE")


class TaskStore:
    def __init__(self, root: Path = DEFAULT_STORE, *, clock: Callable[[], float] = time.time) -> None:
        self.root = Path(os.path.abspath(root))
        self.clock = clock
        no_links(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.workspaces = self.root / "workspaces"
        no_links(self.workspaces)
        self.workspaces.mkdir(exist_ok=True)
        self.database = self.root / "tasks.sqlite3"
        no_links(self.database)
        with self._transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, spec_version INTEGER NOT NULL,
                    spec_sha256 TEXT NOT NULL, spec_json TEXT NOT NULL, status TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL, started_at REAL,
                    deadline REAL, finished_at REAL, limits_json TEXT NOT NULL, spent_micros INTEGER NOT NULL DEFAULT 0,
                    reserved_micros INTEGER NOT NULL DEFAULT 0, repair_rounds INTEGER NOT NULL DEFAULT 0,
                    tool_calls INTEGER NOT NULL DEFAULT 0, model_calls INTEGER NOT NULL DEFAULT 0,
                    last_error_fp TEXT, last_error_round INTEGER, stop_code TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    at REAL NOT NULL, stage TEXT NOT NULL, code TEXT NOT NULL, details_json TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                );
                CREATE INDEX IF NOT EXISTS task_events ON events(task_id, seq);
                CREATE TABLE IF NOT EXISTS model_calls (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, reserved INTEGER NOT NULL,
                    actual INTEGER, FOREIGN KEY(task_id) REFERENCES tasks(id)
                );
                CREATE TABLE IF NOT EXISTS patches (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, path TEXT NOT NULL,
                    before_sha256 TEXT, after_sha256 TEXT NOT NULL, before_content BLOB, after_content BLOB NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                );
            """)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        no_links(self.database)
        db = sqlite3.connect(self.database, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _task(self, db: sqlite3.Connection, task_id: str) -> dict[str, Any]:
        need(isinstance(task_id, str) and TASK_ID.fullmatch(task_id), "INVALID_TASK_ID")
        row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        need(row is not None, "TASK_NOT_FOUND")
        return dict(row)

    def _event(self, db: sqlite3.Connection, task: dict[str, Any], code: str, **details: Any) -> None:
        db.execute("INSERT INTO events(task_id, at, stage, code, details_json) VALUES (?, ?, ?, ?, ?)",
                   (task["id"], self.clock(), task["status"], code, encoded(details).decode("utf-8")))
        db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (self.clock(), task["id"]))

    def _stop(self, db: sqlite3.Connection, task: dict[str, Any], code: str, *, status: str = "stopped") -> None:
        # Terminal outcomes are immutable, including when late billing arrives.
        if task["status"] in TERMINAL:
            return
        self._event(db, task, code, repairRound=task["repair_rounds"])
        finished = self.clock()
        db.execute("UPDATE tasks SET status = ?, stop_code = ?, finished_at = ? WHERE id = ?",
                   (status, code, finished, task["id"]))
        task.update(status=status, stop_code=code, finished_at=finished)

    def _guard(self, db: sqlite3.Connection, task: dict[str, Any]) -> str | None:
        if task["status"] in TERMINAL:
            return "TASK_TERMINAL"
        if task["status"] not in ACTIVE:
            return "TASK_NOT_RUNNING"
        if self.clock() >= task["deadline"]:
            self._stop(db, task, "TIME_LIMIT")
            return "TIME_LIMIT"
        limits = json.loads(task["limits_json"])
        if task["spent_micros"] >= limits["maxCostMicros"]:
            self._stop(db, task, "COST_LIMIT")
            return "COST_LIMIT"
        return None

    @staticmethod
    def _result(task: dict[str, Any], code: str | None = None, **result: Any) -> dict[str, Any]:
        return {"taskId": task["id"], "status": task["status"], "ok": code is None,
                "deliverable": False, **({"code": code} if code else {}), **result}

    def create(self, spec: dict[str, Any], *, max_cost_micros: int,
               max_duration_seconds: int = 600, max_repair_rounds: int = 3,
               max_tool_calls: int = 128) -> dict[str, Any]:
        try:
            payload = encoded(spec)
            need(len(payload) <= FILE_BYTES, "SPEC_TOO_LARGE")
            validate_spec(spec, require_confirmable=True)
        except (SpecError, TypeError, ValueError, RecursionError) as exc:
            if isinstance(exc, TaskError):
                raise
            raise TaskError("INVALID_SPEC") from exc
        need(spec["status"] == "confirmed", "SPEC_NOT_CONFIRMED")
        need(integer(max_cost_micros, 1, 10**12), "INVALID_COST_LIMIT")
        need(integer(max_duration_seconds, 1, 600), "INVALID_TIME_LIMIT")
        need(integer(max_repair_rounds, 0, 3), "INVALID_REPAIR_LIMIT")
        need(integer(max_tool_calls, 1, 128), "INVALID_TOOL_LIMIT")
        limits = {"maxCostMicros": max_cost_micros, "currency": "USD",
                  "maxDurationSeconds": max_duration_seconds, "maxRepairRounds": max_repair_rounds,
                  "maxToolCalls": max_tool_calls}
        task_id = "task-" + uuid.uuid4().hex
        now = self.clock()
        with self._transaction() as db:
            existing = db.execute("SELECT spec_sha256 FROM tasks WHERE project_id = ? AND spec_version = ?",
                                  (spec["projectId"], spec["version"])).fetchone()
            need(existing is None or existing[0] == digest(payload), "SPEC_VERSION_CONFLICT")
            db.execute("""INSERT INTO tasks(id, project_id, spec_version, spec_sha256, spec_json,
                       status, created_at, updated_at, limits_json) VALUES (?, ?, ?, ?, ?, 'ready', ?, ?, ?)""",
                       (task_id, spec["projectId"], spec["version"], digest(payload), payload.decode("utf-8"),
                        now, now, encoded(limits).decode("utf-8")))
            task = self._task(db, task_id)
            self._event(db, task, "TASK_CREATED")
        return self.inspect(task_id)

    def transition(self, task_id: str, target: str) -> dict[str, Any]:
        with self._transaction() as db:
            task = self._task(db, task_id)
            if task["status"] == "ready" and target == "generating":
                path = self.workspaces / task_id
                try:
                    no_links(path)
                    path.mkdir(exist_ok=False)
                except (TaskError, OSError):
                    self._stop(db, task, "WORKSPACE_UNSAFE")
                    return self._result(task, "WORKSPACE_UNSAFE")
                now = self.clock()
                deadline = now + json.loads(task["limits_json"])["maxDurationSeconds"]
                db.execute("UPDATE tasks SET started_at = ?, deadline = ?, status = 'generating' WHERE id = ?",
                           (now, deadline, task_id))
                task["status"] = "generating"
            else:
                error = self._guard(db, task)
                if error:
                    return self._result(task, error)
                if target == "failed":
                    self._stop(db, task, "TASK_FAILED", status="failed")
                    return self._result(task)
                if (task["status"], target) not in {("generating", "validating"), ("validating", "generating")}:
                    self._event(db, task, "STATUS_NOT_ALLOWED")
                    return self._result(task, "STATUS_NOT_ALLOWED")
                db.execute("UPDATE tasks SET status = ? WHERE id = ?", (target, task_id))
                task["status"] = target
            self._event(db, task, "STATE_CHANGED", target=target)
            return self._result(task)

    def cancel(self, task_id: str) -> dict[str, Any]:
        with self._transaction() as db:
            task = self._task(db, task_id)
            self._stop(db, task, "USER_CANCELLED", status="cancelled")
            return self._result(task)

    def inspect(self, task_id: str) -> dict[str, Any]:
        with self._transaction() as db:
            task = self._task(db, task_id)
            self._guard(db, task)
            task = self._task(db, task_id)
            events = [dict(row) for row in db.execute("SELECT * FROM events WHERE task_id = ? ORDER BY seq", (task_id,))]
            for event in events:
                event["details"] = json.loads(event.pop("details_json"))
            calls = [dict(row) for row in db.execute("SELECT id, reserved, actual FROM model_calls WHERE task_id = ?", (task_id,))]
            patches = [dict(row) for row in db.execute(
                "SELECT seq, path, before_sha256, after_sha256 FROM patches WHERE task_id = ? ORDER BY seq", (task_id,))]
            elapsed = 0 if task["started_at"] is None else max(0, (task["finished_at"] or self.clock()) - task["started_at"])
            return self._result(task, projectId=task["project_id"], specVersion=task["spec_version"],
                                specSha256=task["spec_sha256"], limits=json.loads(task["limits_json"]),
                                startedAt=task["started_at"], deadline=task["deadline"],
                                finishedAt=task["finished_at"], elapsedSeconds=elapsed, patches=patches,
                                spentMicros=task["spent_micros"], reservedMicros=task["reserved_micros"],
                                repairRounds=task["repair_rounds"], toolCalls=task["tool_calls"],
                                stopCode=task["stop_code"], events=events, modelCalls=calls)

    def sweep(self, *, recover: bool = False) -> list[dict[str, Any]]:
        """Trusted supervisor tick; recover only after the previous worker exited."""
        with self._transaction() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM tasks WHERE status IN ('generating', 'validating')")]
            for task_id in ids:
                task = self._task(db, task_id)
                self._guard(db, task)
                if recover:
                    self._stop(db, task, "PROCESS_INTERRUPTED")
        return [self.inspect(task_id) for task_id in ids]

    def record_failure(self, task_id: str, stage: str, code: str, *, path: str | None = None,
                       check: str | None = None) -> dict[str, Any]:
        need(stage in {"generating", "validating"} and isinstance(code, str) and CODE.fullmatch(code), "INVALID_FAILURE")
        need(check is None or (isinstance(check, str) and re.fullmatch(r"[a-z0-9_]{1,64}", check)), "INVALID_FAILURE")
        with self._transaction() as db:
            task = self._task(db, task_id)
            error = self._guard(db, task)
            if error:
                return self._result(task, error)
            if task["last_error_round"] == task["repair_rounds"] and code not in SECURITY_ERRORS:
                return self._result(task, "ROUND_ALREADY_RECORDED")
            if path is not None:
                self._path(task, path)
            fp = digest(encoded({"stage": stage, "code": code, "path": path, "check": check}))
            self._event(db, task, "ROUND_FAILED", failureStage=stage, errorCode=code,
                        fingerprint=fp, repairRound=task["repair_rounds"], path=path, check=check)
            db.execute("UPDATE tasks SET last_error_fp = ?, last_error_round = ? WHERE id = ?",
                       (fp, task["repair_rounds"], task_id))
            if code in SECURITY_ERRORS:
                self._stop(db, task, code)
            elif task["last_error_fp"] == fp and task["last_error_round"] == task["repair_rounds"] - 1:
                self._stop(db, task, "REPEATED_ERROR")
            elif task["repair_rounds"] >= json.loads(task["limits_json"])["maxRepairRounds"]:
                self._stop(db, task, "REPAIR_LIMIT")
            return self._result(task)

    def begin_repair(self, task_id: str) -> dict[str, Any]:
        with self._transaction() as db:
            task = self._task(db, task_id)
            error = self._guard(db, task)
            if error:
                return self._result(task, error)
            if task["last_error_round"] != task["repair_rounds"]:
                return self._result(task, "NO_FAILED_ROUND")
            if task["repair_rounds"] >= json.loads(task["limits_json"])["maxRepairRounds"]:
                self._stop(db, task, "REPAIR_LIMIT")
                return self._result(task, "REPAIR_LIMIT")
            db.execute("UPDATE tasks SET repair_rounds = repair_rounds + 1, status = 'generating' WHERE id = ?", (task_id,))
            task.update(repair_rounds=task["repair_rounds"] + 1, status="generating")
            self._event(db, task, "REPAIR_STARTED", repairRound=task["repair_rounds"])
            return self._result(task)

    def reserve_model_call(self, task_id: str, upper_cost_micros: int) -> dict[str, Any]:
        """Reserve an enforced provider upper bound BEFORE dispatch, not an estimate."""
        need(integer(upper_cost_micros, 1, 10**12), "INVALID_COST")
        with self._transaction() as db:
            task = self._task(db, task_id)
            error = self._guard(db, task)
            if error:
                return self._result(task, error)
            if task["last_error_round"] == task["repair_rounds"]:
                return self._result(task, "REPAIR_REQUIRED")
            if task["model_calls"] >= MODEL_CALLS:
                self._stop(db, task, "MODEL_CALL_LIMIT")
                return self._result(task, "MODEL_CALL_LIMIT")
            if task["spent_micros"] + task["reserved_micros"] + upper_cost_micros > json.loads(task["limits_json"])["maxCostMicros"]:
                self._stop(db, task, "COST_LIMIT")
                return self._result(task, "COST_LIMIT")
            call_id = "call-" + uuid.uuid4().hex
            db.execute("INSERT INTO model_calls(id, task_id, reserved) VALUES (?, ?, ?)", (call_id, task_id, upper_cost_micros))
            db.execute("UPDATE tasks SET reserved_micros = reserved_micros + ?, model_calls = model_calls + 1 WHERE id = ?",
                       (upper_cost_micros, task_id))
            self._event(db, task, "MODEL_COST_RESERVED", callId=call_id, upperCostMicros=upper_cost_micros)
            return self._result(task, callId=call_id)

    def settle_model_call(self, task_id: str, call_id: str, actual_cost_micros: int) -> dict[str, Any]:
        # Late accounting must be retained after cancellation; it cannot resume work.
        need(integer(actual_cost_micros, 0, 10**12), "INVALID_COST")
        with self._transaction() as db:
            task = self._task(db, task_id)
            call = db.execute("SELECT * FROM model_calls WHERE id = ? AND task_id = ?", (call_id, task_id)).fetchone()
            need(call is not None, "CALL_NOT_FOUND")
            if call["actual"] is not None:
                need(call["actual"] == actual_cost_micros, "CALL_ALREADY_SETTLED")
                return self._result(task)
            db.execute("UPDATE model_calls SET actual = ? WHERE id = ?", (actual_cost_micros, call_id))
            db.execute("UPDATE tasks SET reserved_micros = reserved_micros - ?, spent_micros = spent_micros + ? WHERE id = ?",
                       (call["reserved"], actual_cost_micros, task_id))
            self._event(db, task, "MODEL_COST_SETTLED", callId=call_id, actualCostMicros=actual_cost_micros)
            task = self._task(db, task_id)
            if actual_cost_micros > call["reserved"]:
                self._event(db, task, "COST_BOUND_BREACH", callId=call_id)
                self._stop(db, task, "COST_BOUND_BREACH")
            self._guard(db, task)
            return self._result(task)

    def _path(self, task: dict[str, Any], relative: Any) -> Path:
        need(isinstance(relative, str) and 0 < len(relative) <= 192, "WORKSPACE_ESCAPE")
        parts = relative.split("/")
        need(len(parts) <= 8 and all(PART.fullmatch(p) and p not in {".", ".."}
                                    and not p.endswith(".") and not RESERVED.fullmatch(p) for p in parts), "WORKSPACE_ESCAPE")
        base = self.workspaces / task["id"]
        path = base.joinpath(*parts)
        no_links(path)
        need(path.resolve().is_relative_to(base.resolve()), "WORKSPACE_ESCAPE")
        return path

    def _inventory(self, task: dict[str, Any]) -> dict[str, int]:
        base = self.workspaces / task["id"]
        no_links(base)
        need(base.is_dir(), "WORKSPACE_UNSAFE")
        files: dict[str, int] = {}
        directories = 0
        for parent, dirs, names in os.walk(base, followlinks=False):
            directories += len(dirs)
            need(directories <= FILE_COUNT * 8, "WORKSPACE_LIMIT")
            for name in dirs + names:
                self._path(task, (Path(parent) / name).relative_to(base).as_posix())
            for name in names:
                path = Path(parent) / name
                need(path.is_file(), "WORKSPACE_UNSAFE")
                size = path.stat().st_size
                need(size <= FILE_BYTES, "FILE_LIMIT")
                files[path.relative_to(base).as_posix()] = size
                need(len(files) <= FILE_COUNT and sum(files.values()) <= WORKSPACE_BYTES, "WORKSPACE_LIMIT")
        return files

    @staticmethod
    def _params(params: dict[str, Any], keys: set[str]) -> None:
        need(set(params) == keys, "INVALID_TOOL_INPUT")

    def _dispatch(self, db: sqlite3.Connection, task: dict[str, Any], name: str,
                  params: dict[str, Any]) -> dict[str, Any]:
        if name == "read_spec":
            self._params(params, set())
            return {"spec": json.loads(task["spec_json"])}
        if name == "run_tests":
            self._params(params, {"suite"})
            need(params["suite"] == "spec", "TOOL_NOT_ALLOWED")
            validate_spec(json.loads(task["spec_json"]), require_confirmable=True)
            files = self._inventory(task)
            return {"suite": "spec", "passed": True, "checks": ["confirmed_spec", "workspace_limits"],
                    "fileCount": len(files), "executedGeneratedCode": False}
        if name in {"read_file", "validate_spec"}:
            self._params(params, {"path"})
            path = self._path(task, params["path"])
            self._inventory(task)
            need(path.is_file(), "FILE_NOT_FOUND")
            with path.open("rb") as stream:
                content = stream.read(FILE_BYTES + 1)
            need(len(content) <= FILE_BYTES, "FILE_LIMIT")
            text = content.decode("utf-8")
            if name == "read_file":
                return {"content": text, "sha256": digest(content)}
            validate_spec(json.loads(text), require_confirmable=True)
            return {"valid": True, "deliverable": False}
        self._params(params, {"path", "expectedSha256", "content"})
        path = self._path(task, params["path"])
        files = self._inventory(task)
        need(isinstance(params["content"], str), "INVALID_TOOL_INPUT")
        content = params["content"].encode("utf-8")
        need(len(content) <= FILE_BYTES, "FILE_LIMIT")
        expected = params["expectedSha256"]
        need(expected is None or (isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected)), "INVALID_TOOL_INPUT")
        before = None
        if path.exists():
            need(path.is_file(), "INVALID_FILE")
            before = path.read_bytes()
            need(expected is not None and digest(before) == expected, "PATCH_CONFLICT")
        else:
            need(expected is None, "PATCH_CONFLICT")
        count = len(files) + (params["path"] not in files)
        size = sum(files.values()) - files.get(params["path"], 0) + len(content)
        need(count <= FILE_COUNT and size <= WORKSPACE_BYTES, "WORKSPACE_LIMIT")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".patch-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            need(self.clock() < task["deadline"], "TIME_LIMIT")
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        db.execute("""INSERT INTO patches(task_id, path, before_sha256, after_sha256, before_content, after_content)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                   (task["id"], params["path"], expected, digest(content), before, content))
        self._event(db, task, "FILE_PATCHED", path=params["path"], beforeSha256=expected,
                    sha256=digest(content), sizeBytes=len(content))
        return {"sha256": digest(content), "sizeBytes": len(content)}

    def tool(self, task_id: str, name: str, params: dict[str, Any], *,
             request_task_id: str | None = None) -> dict[str, Any]:
        """task_id comes from coordinator context, never from the agent payload.

        If a protocol includes an agent-supplied task ID, pass it separately as
        request_task_id; mismatches stop the bound task before accessing files.
        """
        with self._transaction() as db:
            task = self._task(db, task_id)
            error = self._guard(db, task)
            if error:
                return self._result(task, error)
            if task["last_error_round"] == task["repair_rounds"]:
                return self._result(task, "REPAIR_REQUIRED")
            try:
                need(request_task_id is None or request_task_id == task_id, "TASK_ID_MISMATCH")
                need(task["tool_calls"] < json.loads(task["limits_json"])["maxToolCalls"], "TOOL_LIMIT")
                db.execute("UPDATE tasks SET tool_calls = tool_calls + 1 WHERE id = ?", (task_id,))
                need(isinstance(name, str) and name in TOOLS, "TOOL_NOT_ALLOWED")
                need(isinstance(params, dict) and len(encoded(params)) <= INPUT_BYTES, "INVALID_TOOL_INPUT")
                result = self._dispatch(db, task, name, params)
                self._event(db, task, "TOOL_SUCCEEDED", tool=name)
                error = self._guard(db, task)
                return self._result(task, error, **({} if error else {"result": result}))
            except (TaskError, SpecError, ValueError, TypeError, OSError, RecursionError) as exc:
                code = exc.code if isinstance(exc, TaskError) else "INVALID_TOOL_INPUT"
                # Never log raw requests, file contents, model responses or exception strings.
                self._event(db, task, "TOOL_REJECTED", tool=name if isinstance(name, str) and name in TOOLS else "unknown", errorCode=code)
                if code in SECURITY_ERRORS | {"FILE_LIMIT", "WORKSPACE_LIMIT", "TOOL_LIMIT", "TIME_LIMIT"}:
                    self._stop(db, task, code)
                return self._result(task, code)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("spec", type=Path)
    create.add_argument("--max-cost-micros", type=int, required=True)
    create.add_argument("--max-duration-seconds", type=int, default=600)
    create.add_argument("--max-repair-rounds", type=int, default=3)
    for command in ("show", "cancel", "repair"):
        sub.add_parser(command).add_argument("task_id")
    transition = sub.add_parser("transition")
    transition.add_argument("task_id")
    transition.add_argument("target", choices=["generating", "validating", "failed"])
    tool = sub.add_parser("tool")
    tool.add_argument("task_id")
    tool.add_argument("name")
    tool.add_argument("params", help="JSON object; no executable expressions")
    failure = sub.add_parser("failure")
    failure.add_argument("task_id")
    failure.add_argument("stage", choices=sorted(ACTIVE))
    failure.add_argument("code")
    failure.add_argument("--path")
    failure.add_argument("--check")
    sub.add_parser("sweep")
    sub.add_parser("recover")
    args = parser.parse_args()
    try:
        store = TaskStore(args.store)
        if args.command == "create":
            need(args.spec.stat().st_size <= FILE_BYTES, "SPEC_TOO_LARGE")
            result = store.create(json.loads(args.spec.read_text(encoding="utf-8")),
                                  max_cost_micros=args.max_cost_micros, max_duration_seconds=args.max_duration_seconds,
                                  max_repair_rounds=args.max_repair_rounds)
        elif args.command == "show":
            result = store.inspect(args.task_id)
        elif args.command == "transition":
            result = store.transition(args.task_id, args.target)
        elif args.command == "tool":
            need(len(args.params.encode("utf-8")) <= INPUT_BYTES, "INVALID_TOOL_INPUT")
            result = store.tool(args.task_id, args.name, json.loads(args.params))
        elif args.command == "failure":
            result = store.record_failure(args.task_id, args.stage, args.code, path=args.path, check=args.check)
        elif args.command in {"sweep", "recover"}:
            result = {"tasks": store.sweep(recover=args.command == "recover"), "deliverable": False}
        else:
            result = getattr(store, "begin_repair" if args.command == "repair" else "cancel")(args.task_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok", True) else 2
    except (TaskError, ValueError, OSError, sqlite3.Error, RecursionError) as exc:
        print(json.dumps({"ok": False, "code": exc.code if isinstance(exc, TaskError) else "INPUT_OR_STORE_ERROR",
                          "deliverable": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
