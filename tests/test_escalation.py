from __future__ import annotations

from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from localdev_mlx.config import ModelProfile
from localdev_mlx.providers.base import StructuredProvider
from localdev_mlx.schemas import (
    ExternalReviewCategory,
    ExternalReviewState,
    RiskLevel,
    TaskKind,
    TaskStatus,
    TriageResult,
    WorkUnit,
)
from localdev_mlx.workflows.task_runner import TaskRunner

T = TypeVar("T", bound=BaseModel)


class ExternalReviewProvider(StructuredProvider):
    def complete_structured(
        self,
        *,
        profile: ModelProfile,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
        schema_name: str,
    ) -> T:
        value = TriageResult(
            task_summary="High-risk migration",
            risk=RiskLevel.EXTERNAL,
            confidence=0.9,
            should_escalate=True,
            escalation_reasons=["Destructive data migration requires independent review"],
            work_units=[
                WorkUnit(
                    title="Do not execute",
                    goal="Request external review",
                    allowed_paths=["src/samplecalc/core.py"],
                )
            ],
        )
        return response_model.model_validate(value.model_dump())


def test_external_triage_builds_bundle(sample_repo: Path, global_config) -> None:
    runner = TaskRunner(
        global_config=global_config,
        provider=ExternalReviewProvider(),
        manage_models=False,
    )
    task = runner.run(
        repository=sample_repo,
        kind=TaskKind.FEATURE,
        description="Replace all stored data using a destructive migration",
    )
    assert task.status == TaskStatus.ESCALATED
    assert task.escalation_path
    assert task.visible_review_path
    assert task.external_review_state == ExternalReviewState.PENDING
    assert task.external_review_category == ExternalReviewCategory.PLANNER_RISK
    bundle = Path(task.escalation_path)
    assert (bundle / "EXTERNAL_REVIEW_PROMPT.md").exists()
    assert (bundle / "SUMMARY.md").exists()
    assert (bundle / "triage.json").exists()
