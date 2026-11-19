"""Verify project/version history bindings and failed-upgrade preservation."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from plugin_generation import GenerationPipeline  # noqa: E402
from project_history import project_history  # noqa: E402
from spec_workflow import save_confirmed  # noqa: E402
from task_runtime import TaskStore  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="stage32-") as temporary:
        base = Path(temporary)
        spec_root = base / "specs"
        sample = json.loads((ROOT / "desktop-spec/examples/quick-notes-v1.json").read_text(encoding="utf-8"))
        sample["projectId"] = "history-sample"
        first_path = save_confirmed(copy.deepcopy(sample), spec_root)
        first = json.loads(first_path.read_text(encoding="utf-8"))
        store = TaskStore(base / "tasks")
        first_task = store.create(first, max_cost_micros=100_000)["taskId"]
        first_report = GenerationPipeline(store).run(first_task)
        assert first_report["ok"] is True

        second_draft = copy.deepcopy(sample)
        second_draft.update(version=2, goal="A deliberately unsupported workflow")
        second_path = save_confirmed(second_draft, spec_root)
        second = json.loads(second_path.read_text(encoding="utf-8"))
        second_task = store.create(second, max_cost_micros=100_000)["taskId"]
        second_report = GenerationPipeline(store).run(second_task)
        assert second_report["ok"] is False

        history = project_history("history-sample", spec_root, store.root)
        assert [item["version"] for item in history["versions"]] == [1, 2]
        assert history["versions"][0]["tasks"][0]["taskId"] == first_task
        assert history["versions"][0]["tasks"][0]["builds"][0]["checks"]
        assert history["versions"][1]["tasks"][0]["taskId"] == second_task
        assert history["versions"][1]["tasks"][0]["builds"][0]["ok"] is False
        assert history["latestUsableBuild"]["taskId"] == first_task
        assert history["latestUsableBuild"]["pluginVersion"] == "1.0.0"
        assert history["installedVersion"] is None
        assert history["deliverable"] is False
    print("PASS: project history links immutable specs, task outcomes, checks and the last usable build")
    print("PASS: a failed newer version does not replace the prior usable build")
    print("PASS: installed device version remains explicitly unsynchronized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
