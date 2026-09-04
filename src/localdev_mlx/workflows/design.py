from __future__ import annotations

from pathlib import Path

from localdev_mlx.agents import LocalAgents
from localdev_mlx.config import GlobalConfig, load_project_config
from localdev_mlx.context import build_context
from localdev_mlx.git import GitRepository
from localdev_mlx.models import ModelManager
from localdev_mlx.progress import ProgressReporter
from localdev_mlx.providers.base import StructuredProvider
from localdev_mlx.schemas import MachineTaskQueue


class DesignWorkflow:
    """Create local design documents and a compartmentalized task queue."""

    def __init__(
        self,
        *,
        global_config: GlobalConfig,
        provider: StructuredProvider,
        model_manager: ModelManager | None = None,
        manage_models: bool = True,
        progress: ProgressReporter | None = None,
    ) -> None:
        self.global_config = global_config
        self.agents = LocalAgents(provider)
        self.model_manager = model_manager or ModelManager()
        self.manage_models = manage_models
        self.progress = progress or ProgressReporter()

    def _ensure_planner(self, workflow_id: str) -> None:
        if self.manage_models:
            with self.progress.operation(workflow_id, "Preparing planner model"):
                self.model_manager.ensure(self.global_config.planner)

    def ideate(self, *, repository: Path, description: str) -> tuple[Path, str]:
        git = GitRepository(repository)
        config = load_project_config(git.root)
        integration = git.ensure_integration_worktree(config)
        if not git.is_clean(integration):
            raise RuntimeError("Integration branch must be clean before ideation")

        self.progress.emit("[IDEA] START — preparing project context")
        context = build_context(
            integration,
            config,
            char_budget=config.planner_context_chars,
            include_map=True,
        ).render()
        self.progress.emit(f"[IDEA] CONTEXT — {len(context):,} characters prepared")
        self._ensure_planner("IDEA")
        with self.progress.operation("IDEA", "Planner project-design inference"):
            result = self.agents.ideate(
                profile=self.global_config.planner,
                description=description,
                context=context,
            )

        docs = integration / "docs" / "ai"
        docs.mkdir(parents=True, exist_ok=True)
        (docs / "PROJECT_BRIEF.md").write_text(
            result.project_brief_markdown,
            encoding="utf-8",
        )
        (docs / "ARCHITECTURE_PROPOSAL.md").write_text(
            result.architecture_proposal_markdown,
            encoding="utf-8",
        )
        (docs / "EXTERNAL_REVIEW_REQUEST.md").write_text(
            result.external_review_request_markdown,
            encoding="utf-8",
        )
        prompt_path = build_external_design_prompt(integration)

        with self.progress.operation("IDEA", "Committing design documents"):
            commit = git.commit_all(
                integration,
                "docs: add project brief and provisional architecture",
            )
        self.progress.emit(
            f"[IDEA] COMPLETE — committed {commit}; optional review prompt: {prompt_path}"
        )
        return integration, commit

    def plan(
        self,
        *,
        repository: Path,
        description: str,
        external_review_path: Path | None,
        allow_without_external_review: bool = False,
    ) -> tuple[Path, str]:
        git = GitRepository(repository)
        config = load_project_config(git.root)
        integration = git.ensure_integration_worktree(config)
        if not git.is_clean(integration):
            raise RuntimeError("Integration branch must be clean before planning")

        review_content = ""
        default_review = integration / "docs" / "ai" / "EXTERNAL_REVIEW.md"
        if external_review_path:
            review_content = external_review_path.resolve().read_text(encoding="utf-8")
        elif default_review.exists():
            review_content = default_review.read_text(encoding="utf-8")
        elif not allow_without_external_review:
            raise RuntimeError(
                "An external design review is required by default. Save it as "
                "docs/ai/EXTERNAL_REVIEW.md, pass --external-review, or use "
                "--skip-external-review."
            )

        requested = [
            "docs/ai/PROJECT_BRIEF.md",
            "docs/ai/ARCHITECTURE_PROPOSAL.md",
            "docs/ai/EXTERNAL_REVIEW_REQUEST.md",
        ]
        context = build_context(
            integration,
            config,
            requested_paths=requested,
            char_budget=config.planner_context_chars,
            include_map=True,
        ).render()
        if review_content:
            context += "\n\n# External review\n\n" + review_content

        self._ensure_planner("PLAN")
        with self.progress.operation("PLAN", "Planner implementation-plan inference"):
            result = self.agents.plan(
                profile=self.global_config.planner,
                description=description,
                context=context,
            )

        docs = integration / "docs" / "ai"
        (docs / "MASTER_PLAN.md").write_text(result.master_plan_markdown, encoding="utf-8")
        (docs / "ARCHITECTURE.md").write_text(result.architecture_markdown, encoding="utf-8")
        (docs / "CONTRACTS.md").write_text(result.contracts_markdown, encoding="utf-8")
        (docs / "TASK_QUEUE.md").write_text(result.task_queue_markdown, encoding="utf-8")
        queue = MachineTaskQueue(tasks=result.task_queue)
        (docs / "TASK_QUEUE.json").write_text(queue.model_dump_json(indent=2), encoding="utf-8")

        with self.progress.operation("PLAN", "Committing implementation plan"):
            commit = git.commit_all(integration, "docs: add implementation plan and task queue")
        self.progress.emit(f"[PLAN] COMPLETE — committed {commit}")
        return integration, commit


def build_external_design_prompt(repository: Path) -> Path:
    """Create a portable prompt for an optional independent design review."""

    root = GitRepository(repository).root
    docs = root / "docs" / "ai"
    path = docs / "EXTERNAL_DESIGN_REVIEW_PROMPT.md"
    path.write_text(
        """# Independent Design Review

Review this project's provisional design before implementation planning.

Read:

- `AGENTS.md`
- `docs/ai/PROJECT_BRIEF.md`
- `docs/ai/ARCHITECTURE_PROPOSAL.md`
- `docs/ai/EXTERNAL_REVIEW_REQUEST.md`

Provide:

1. missing requirements or user workflows;
2. architecture corrections and better existing tools;
3. security, privacy, data-integrity, and operational risks;
4. decisions that should change before implementation;
5. work that should remain externally reviewed;
6. a clear approve-or-revise verdict.

Write the result to `docs/ai/EXTERNAL_REVIEW.md`. Do not implement code during this review.
""",
        encoding="utf-8",
    )
    return path
