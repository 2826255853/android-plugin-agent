"""Run the repeatable Stage 2 regression chain with a temporary artifact."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from plugin_generation import GenerationPipeline  # noqa: E402
from spec_workflow import save_confirmed  # noqa: E402
from task_runtime import TaskStore  # noqa: E402


def run(command: list[str]) -> None:
    print("RUN", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def verify_traceability(store: TaskStore, task_id: str, report: dict) -> Path:
    state = store.inspect(task_id)
    assert state["status"] == "built"
    assert state["deliverable"] is False
    assert state["specVersion"] == report["specVersion"] == 1
    assert state["specSha256"] == report["specSha256"]
    assert len(state["builds"]) == 1
    saved_build = state["builds"][0]
    assert saved_build == report
    assert saved_build["artifact"]["sha256"] == report["artifact"]["sha256"]
    assert saved_build["specSha256"] == state["specSha256"]
    assert saved_build["templateVersion"] == report["templateVersion"]

    reopened = TaskStore(store.root)
    restored = reopened.inspect(task_id)
    assert restored["specSha256"] == state["specSha256"]
    assert restored["builds"] == state["builds"]
    artifact = reopened.export_build(task_id)
    assert artifact.is_file()
    print(json.dumps({
        "traceability": "PASS",
        "taskId": task_id,
        "specVersion": restored["specVersion"],
        "specSha256": restored["specSha256"],
        "templateVersion": restored["builds"][0]["templateVersion"],
        "artifactSha256": restored["builds"][0]["artifact"]["sha256"],
        "deliverable": restored["deliverable"],
    }, ensure_ascii=False))
    return artifact


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="stage25-") as temporary:
        base = Path(temporary)
        run([sys.executable, "scripts/verify-stage21.py"])
        run([sys.executable, "scripts/verify-stage22.py"])
        run([sys.executable, "scripts/verify-stage23.py"])

        sample = json.loads((ROOT / "desktop-spec/examples/quick-notes-v1.json").read_text(encoding="utf-8"))
        confirmed_path = save_confirmed(copy.deepcopy(sample), base / "specs")
        confirmed = json.loads(confirmed_path.read_text(encoding="utf-8"))
        store = TaskStore(base / "tasks")
        task_id = store.create(confirmed, max_cost_micros=100_000)["taskId"]
        report = GenerationPipeline(store).run(task_id)
        assert report["ok"] is True, report
        artifact = verify_traceability(store, task_id, report)

        run(["node", "scripts/verify-stage24.mjs", str(artifact)])

    print("ALL STAGE 2.5 CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
