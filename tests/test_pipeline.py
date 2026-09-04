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
                        mode="analysis",
                        title="Reproduce failures and confirm contracts",
                        goal="Inspect the current behavior before editing.",
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
    assert any("ANALYZED" in message for message in messages)
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


class BaselineAwareProvider(StructuredProvider):
    """Verify that bug triage receives real controller test output."""

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
            assert "Actual baseline validation produced by the controller" in user_prompt
            assert "test_subtract" in user_prompt
            value = TriageResult(
                task_summary="Use the observed failing test to repair subtraction.",
                risk=RiskLevel.LOW,
                confidence=0.99,
                should_escalate=False,
                reproduction_plan=["Use the supplied baseline failure."],
                relevant_paths=["src/samplecalc/core.py", "tests/test_core.py"],
                work_units=[
                    WorkUnit(
                        title="Correct subtraction",
                        goal="Return a minus b.",
                        allowed_paths=["src/samplecalc/core.py"],
                        read_paths=["src/samplecalc/core.py", "tests/test_core.py"],
                        acceptance_criteria=["The supplied subtraction test passes."],
                    )
                ],
            )
        elif response_model is ImplementationResult:
            value = ImplementationResult(
                summary="Correct subtraction.",
                edits=[
                    FileEdit(
                        operation="replace_text",
                        path="src/samplecalc/core.py",
                        old_text="return a + b  # intentional demo bug",
                        new_text="return a - b",
                        reason="Match subtraction semantics.",
                    )
                ],
            )
        elif response_model is ReviewResult:
            value = ReviewResult(
                approved=True,
                confidence=0.99,
                summary="The failing behavior was repaired and validated.",
            )
        else:  # pragma: no cover
            raise AssertionError(response_model)
        return response_model.model_validate(value.model_dump())


class WorkerPromotionProvider(StructuredProvider):
    """Return no edits from the small worker and a repair from the planner."""

    def __init__(self) -> None:
        self.implementation_models: list[str] = []

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
                task_summary="Repair subtraction with local worker promotion.",
                risk=RiskLevel.LOW,
                confidence=0.99,
                should_escalate=False,
                relevant_paths=["src/samplecalc/core.py", "tests/test_core.py"],
                work_units=[
                    WorkUnit(
                        title="Correct subtraction",
                        goal="Return a minus b.",
                        allowed_paths=["src/samplecalc/core.py"],
                        read_paths=["src/samplecalc/core.py", "tests/test_core.py"],
                        acceptance_criteria=["The subtraction test passes."],
                    )
                ],
            )
        elif response_model is ImplementationResult:
            self.implementation_models.append(profile.model)
            if profile.model == "mock/worker":
                value = ImplementationResult(
                    summary="I could not identify a concrete edit.",
                )
            elif profile.model == "mock/planner":
                value = ImplementationResult(
                    summary="Correct subtraction after local promotion.",
                    edits=[
                        FileEdit(
                            operation="replace_text",
                            path="src/samplecalc/core.py",
                            old_text="return a + b  # intentional demo bug",
                            new_text="return a - b",
                            reason="Match subtraction semantics.",
                        )
                    ],
                )
            else:  # pragma: no cover
                raise AssertionError(profile.model)
        elif response_model is ReviewResult:
            value = ReviewResult(
                approved=True,
                confidence=0.99,
                summary="The promoted local repair is correct.",
            )
        else:  # pragma: no cover
            raise AssertionError(response_model)
        return response_model.model_validate(value.model_dump())


def test_bug_triage_receives_actual_baseline_failures(
    sample_repo: Path,
    global_config,
) -> None:
    runner = TaskRunner(
        global_config=global_config,
        provider=BaselineAwareProvider(),
        manage_models=False,
        depth="fast",
    )

    task = runner.run(
        repository=sample_repo,
        kind=TaskKind.BUG,
        description="Repair the failing subtraction behavior.",
    )

    assert task.status == TaskStatus.INTEGRATED, task.model_dump_json(indent=2)
    assert any("before bug triage" in event for event in task.events)


def test_worker_no_edit_promotes_to_alternate_local_model(
    sample_repo: Path,
    global_config,
) -> None:
    provider = WorkerPromotionProvider()
    messages: list[str] = []
    runner = TaskRunner(
        global_config=global_config,
        provider=provider,
        manage_models=False,
        progress=ProgressReporter(callback=messages.append, heartbeat_seconds=0),
        depth="fast",
    )

    task = runner.run(
        repository=sample_repo,
        kind=TaskKind.BUG,
        description="Repair subtraction even if the primary worker stalls.",
    )

    assert task.status == TaskStatus.INTEGRATED, task.model_dump_json(indent=2)
    assert provider.implementation_models == ["mock/worker", "mock/planner"]
    assert any("PROMOTING" in message for message in messages)


class PlannerAllowlistRepairProvider(StructuredProvider):
    """Return one malformed plan, then a corrected plan and implementation."""

    def __init__(self) -> None:
        self.triage_calls = 0

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
            self.triage_calls += 1
            allowed = [] if self.triage_calls == 1 else ["src/samplecalc/core.py"]
            value = TriageResult(
                task_summary="Repair subtraction with a validated write allowlist.",
                risk=RiskLevel.LOW,
                confidence=0.95,
                should_escalate=False,
                relevant_paths=["src/samplecalc/core.py", "tests/test_core.py"],
                work_units=[
                    WorkUnit(
                        title="Correct subtraction",
                        goal="Return a minus b.",
                        allowed_paths=allowed,
                        read_paths=["src/samplecalc/core.py", "tests/test_core.py"],
                        acceptance_criteria=["The subtraction test passes."],
                    )
                ],
            )
        elif response_model is ImplementationResult:
            value = ImplementationResult(
                summary="Correct subtraction.",
                edits=[
                    FileEdit(
                        operation="replace_text",
                        path="src/samplecalc/core.py",
                        old_text="return a + b  # intentional demo bug",
                        new_text="return a - b",
                        reason="Implement subtraction semantics.",
                    )
                ],
            )
        elif response_model is ReviewResult:
            value = ReviewResult(
                approved=True,
                confidence=0.99,
                summary="The bounded repair is correct.",
            )
        else:  # pragma: no cover
            raise AssertionError(response_model)
        return response_model.model_validate(value.model_dump())


def test_invalid_empty_write_allowlist_is_replanned_before_worker(
    sample_repo: Path,
    global_config,
) -> None:
    provider = PlannerAllowlistRepairProvider()
    messages: list[str] = []
    runner = TaskRunner(
        global_config=global_config,
        provider=provider,
        manage_models=False,
        progress=ProgressReporter(callback=messages.append, heartbeat_seconds=0),
        depth="fast",
    )

    task = runner.run(
        repository=sample_repo,
        kind=TaskKind.BUG,
        description="Repair subtraction.",
    )

    assert task.status == TaskStatus.INTEGRATED, task.model_dump_json(indent=2)
    assert provider.triage_calls == 2
    assert any("REPLANNING" in message for message in messages)
    assert task.triage is not None
    assert task.triage.work_units[0].allowed_paths == ["src/samplecalc/core.py"]


class DirectModeProvider(StructuredProvider):
    """Implement and review a direct task without accepting planner calls."""

    def __init__(self) -> None:
        self.response_types: list[type[BaseModel]] = []

    def complete_structured(
        self,
        *,
        profile: ModelProfile,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
        schema_name: str,
    ) -> T:
        self.response_types.append(response_model)
        if response_model is TriageResult:
            raise AssertionError("Direct mode must not invoke the planner")
        if response_model is ImplementationResult:
            value = ImplementationResult(
                summary="Correct subtraction through direct write authority.",
                edits=[
                    FileEdit(
                        operation="replace_text",
                        path="src/samplecalc/core.py",
                        old_text="return a + b  # intentional demo bug",
                        new_text="return a - b",
                        reason="Match subtraction semantics.",
                    )
                ],
            )
        elif response_model is ReviewResult:
            value = ReviewResult(
                approved=True,
                confidence=0.99,
                summary="The direct repair is bounded and validated.",
            )
        else:  # pragma: no cover
            raise AssertionError(response_model)
        return response_model.model_validate(value.model_dump())


def test_direct_bug_pipeline_skips_planner_and_integrates(
    sample_repo: Path,
    global_config,
) -> None:
    provider = DirectModeProvider()
    messages: list[str] = []
    runner = TaskRunner(
        global_config=global_config,
        provider=provider,
        manage_models=False,
        progress=ProgressReporter(callback=messages.append, heartbeat_seconds=0),
        depth="fast",
    )

    task = runner.run(
        repository=sample_repo,
        kind=TaskKind.BUG,
        description="Correct subtraction without planner inference.",
        direct_allowed_paths=["src/samplecalc/core.py"],
        direct_read_paths=["tests/test_core.py"],
    )

    assert task.status == TaskStatus.INTEGRATED, task.model_dump_json(indent=2)
    assert TriageResult not in provider.response_types
    assert any("DIRECT" in message for message in messages)
    assert "return a - b" in (sample_repo / "src/samplecalc/core.py").read_text()


class AnalysisOnlyBugPlanProvider(StructuredProvider):
    """Return a structurally populated but non-executable bug plan."""

    def __init__(self) -> None:
        self.triage_calls = 0
        self.worker_called = False

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
            self.triage_calls += 1
            value = TriageResult(
                task_summary="Inspect subtraction without authorizing a repair.",
                risk=RiskLevel.LOW,
                confidence=0.9,
                should_escalate=False,
                relevant_paths=["src/samplecalc/core.py", "tests/test_core.py"],
                work_units=[
                    WorkUnit(
                        mode="analysis",
                        title="Inspect subtraction",
                        goal="Describe the current failure.",
                        read_paths=["src/samplecalc/core.py", "tests/test_core.py"],
                    )
                ],
            )
        elif response_model is ImplementationResult:
            self.worker_called = True
            raise AssertionError("Invalid analysis-only bug plan must not reach a worker")
        else:  # pragma: no cover
            raise AssertionError(response_model)
        return response_model.model_validate(value.model_dump())


def test_analysis_only_bug_plan_is_rejected_before_worker(
    sample_repo: Path,
    global_config,
) -> None:
    provider = AnalysisOnlyBugPlanProvider()
    runner = TaskRunner(
        global_config=global_config,
        provider=provider,
        manage_models=False,
        depth="fast",
    )

    task = runner.run(
        repository=sample_repo,
        kind=TaskKind.BUG,
        description="Repair subtraction, not just inspect it.",
    )

    assert task.status == TaskStatus.ESCALATED
    assert provider.triage_calls == 2
    assert provider.worker_called is False
    assert task.external_review_category is not None
    assert task.external_review_category.value == "planner_invalid"
