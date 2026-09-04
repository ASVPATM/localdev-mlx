from __future__ import annotations

import os
import subprocess
from pathlib import Path

from localdev_mlx.progress import ProgressReporter
from localdev_mlx.providers.mock import MockStructuredProvider
from localdev_mlx.schemas import TaskKind, TaskStatus
from localdev_mlx.workflows.task_runner import TaskRunner


def test_mock_bug_pipeline_integrates(
    sample_repo: Path,
    global_config,
    monkeypatch,
) -> None:
    monkeypatch.delenv("PYTHONPATH", raising=False)
    messages: list[str] = []
    runner = TaskRunner(
        global_config=global_config,
        provider=MockStructuredProvider(),
        manage_models=False,
        progress=ProgressReporter(callback=messages.append, heartbeat_seconds=0),
        depth="fast",
    )
    task = runner.run(
        repository=sample_repo,
        kind=TaskKind.BUG,
        description="subtract returns the wrong result",
    )
    assert task.status == TaskStatus.INTEGRATED, task.model_dump_json(indent=2)
    core = (sample_repo / "src/samplecalc/core.py").read_text(encoding="utf-8")
    assert "return a - b" in core
    environment = os.environ.copy()
    environment["PYTHONPATH"] = "src"
    completed = subprocess.run(
        ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=sample_repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert (sample_repo / "docs/ai/handoffs" / f"{task.id}.md").exists()
    assert task.timings_seconds["Total workflow"] > 0
    assert any("CREATED" in message for message in messages)
    assert any("TRIAGING" in message for message in messages)
    assert any("COMPLETE" in message for message in messages)


def test_maintenance_depth_scales_all_role_budgets(global_config) -> None:
    fast = TaskRunner(
        global_config=global_config,
        provider=MockStructuredProvider(),
        manage_models=False,
        depth="fast",
    )
    balanced = TaskRunner(
        global_config=global_config,
        provider=MockStructuredProvider(),
        manage_models=False,
        depth="balanced",
    )
    deep = TaskRunner(
        global_config=global_config,
        provider=MockStructuredProvider(),
        manage_models=False,
        depth="deep",
    )

    assert fast._planner_profile().thinking_budget < balanced._planner_profile().thinking_budget
    assert balanced._planner_profile().thinking_budget < deep._planner_profile().thinking_budget
    assert fast._worker_profile().max_tokens < deep._worker_profile().max_tokens
    assert fast._reviewer_profile().thinking_budget < deep._reviewer_profile().thinking_budget
    assert deep._planner_profile() == global_config.planner
    assert deep._worker_profile() == global_config.worker
    assert deep._reviewer_profile() == global_config.reviewer
