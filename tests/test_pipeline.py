from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from localdev_mlx.config import ModelProfile
from localdev_mlx.progress import ProgressReporter
from localdev_mlx.providers.base import StructuredProvider
from localdev_mlx.providers.mock import MockStructuredProvider
from localdev_mlx.schemas import (
    FileEdit,
    ImplementationResult,
    ReviewResult,
    RiskLevel,
    TaskKind,
    TaskStatus,
    TriageResult,
    WorkUnit,
)
from localdev_mlx.workflows.task_runner import TaskRunner

T = TypeVar("T", bound=BaseModel)


class MultiUnitBugProvider(StructuredProvider):
    """Exercise a no-edit diagnostic unit followed by cumulative bug edits."""

    def complete_structured(
        self,
        *,
        profile: ModelProfile,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
        schema_name: str,
    ) -> T:
        if response_model is TriageResult:
            value = TriageResult(
                task_summary="Repair a bug through cumulative work units.",
                risk=RiskLevel.LOW,
                confidence=0.99,
                should_escalate=False,
                reproduction_plan=["Run the failing unit tests."],
                relevant_paths=[
                    "README.md",
                    "src/samplecalc/core.py",
                    "tests/test_core.py",
                ],
                work_units=[
                    WorkUnit(
                        title="Reproduce failures and confirm contracts",
                        goal="Inspect the current behavior before editing.",
                        allowed_paths=["tests/test_core.py"],
                        read_paths=["src/samplecalc/core.py", "tests/test_core.py"],
                    ),
                    WorkUnit(
                        title="Record the diagnosed bug",
                        goal="Add a narrow diagnostic note before the code repair.",
                        allowed_paths=["README.md"],
                        read_paths=["README.md"],
                    ),
                    WorkUnit(
                        title="Correct subtraction",
                        goal="Return a minus b without changing addition.",
                        allowed_paths=["src/samplecalc/core.py"],
                        read_paths=["src/samplecalc/core.py", "tests/test_core.py"],
                    ),
                ],
            )
        elif response_model is ImplementationResult:
            if '"title": "Reproduce failures and confirm contracts"' in user_prompt:
                value = ImplementationResult(
                    summary="Confirmed the subtraction implementation uses addition.",
                    tests_added_or_changed=["tests/test_core.py"],
                )
            elif '"title": "Record the diagnosed bug"' in user_prompt:
                value = ImplementationResult(
                    summary="Record the diagnosis.",
                    edits=[
                        FileEdit(
                            operation="replace_text",
                            path="README.md",
                            old_text="# Sample\n",
                            new_text="# Sample\n\nSubtraction behavior is covered by tests.\n",
                            reason="Document the bounded diagnosis.",
                        )
                    ],
                )
            elif '"title": "Correct subtraction"' in user_prompt:
                value = ImplementationResult(
                    summary="Correct subtraction operator.",
                    edits=[
                        FileEdit(
                            operation="replace_text",
                            path="src/samplecalc/core.py",
                            old_text="return a + b  # intentional demo bug",
                            new_text="return a - b",
                            reason="Subtraction must subtract the second operand.",
                        )
                    ],
                )
            else:  # pragma: no cover - guards the deterministic test fixture
                raise AssertionError(user_prompt)
        elif response_model is ReviewResult:
            value = ReviewResult(
                approved=True,
                confidence=0.99,
                summary="The cumulative changes fix the bug and retain test coverage.",
            )
        else:  # pragma: no cover - guards the deterministic test fixture
            raise AssertionError(response_model)
        return response_model.model_validate(value.model_dump())


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


def test_bug_pipeline_accepts_no_edit_unit_and_cumulative_repairs(
    sample_repo: Path,
    global_config,
) -> None:
    messages: list[str] = []
    runner = TaskRunner(
        global_config=global_config,
        provider=MultiUnitBugProvider(),
        manage_models=False,
        progress=ProgressReporter(callback=messages.append, heartbeat_seconds=0),
        depth="fast",
    )

    task = runner.run(
        repository=sample_repo,
        kind=TaskKind.BUG,
        description="Repair the subtraction bug through bounded units.",
    )

    assert task.status == TaskStatus.INTEGRATED, task.model_dump_json(indent=2)
    core = (sample_repo / "src/samplecalc/core.py").read_text(encoding="utf-8")
    assert "return a - b" in core
    assert any("NO-CHANGE" in message for message in messages)
    assert any("DEFERRED" in message for message in messages)
    assert any("Final quick validation" in message for message in messages)


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
