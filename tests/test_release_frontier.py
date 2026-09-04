from __future__ import annotations

from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from localdev_mlx.config import ModelProfile
from localdev_mlx.providers.base import StructuredProvider
from localdev_mlx.schemas import ReleaseAuditResult, TaskKind
from localdev_mlx.workflows.frontier import defer_task
from localdev_mlx.workflows.release import ReleaseWorkflow

T = TypeVar("T", bound=BaseModel)


class ReleaseProvider(StructuredProvider):
    def complete_structured(
        self,
        *,
        profile: ModelProfile,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
        schema_name: str,
    ) -> T:
        value = ReleaseAuditResult(
            ready_for_external_review=True,
            locally_release_ready=False,
            confidence=0.8,
            summary="External issue remains open.",
            required_external_work=["Resolve the deferred task."],
        )
        return response_model.model_validate(value.model_dump())


def test_release_bundle_includes_open_frontier_tasks(sample_repo: Path, global_config) -> None:
    task = defer_task(
        repository=sample_repo,
        kind=TaskKind.BUG,
        description="Deferred release blocker.",
    )
    bundle = ReleaseWorkflow(
        global_config=global_config,
        provider=ReleaseProvider(),
        manage_models=False,
    ).run(sample_repo)

    open_tasks = (bundle / "OPEN_EXTERNAL_TASKS.md").read_text(encoding="utf-8")
    prompt = (bundle / "EXTERNAL_RELEASE_REVIEW.md").read_text(encoding="utf-8")
    assert task.id in open_tasks
    assert task.id in (bundle / "open-issues").iterdir().__next__().name
    assert "Open external-review task count: `1`" in prompt
