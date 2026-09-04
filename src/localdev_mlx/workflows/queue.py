from __future__ import annotations

from pathlib import Path

from localdev_mlx.config import GlobalConfig, load_project_config
from localdev_mlx.git import GitRepository
from localdev_mlx.progress import ProgressReporter
from localdev_mlx.providers.base import StructuredProvider
from localdev_mlx.schemas import (
    MachineTaskQueue,
    PlannedTask,
    PlannedTaskStatus,
    TaskKind,
    TaskStatus,
)
from localdev_mlx.workflows.task_runner import TaskRunner


class QueueWorkflow:
    """Run dependency-ready local tasks from docs/ai/TASK_QUEUE.json."""

    def __init__(
        self,
        *,
        global_config: GlobalConfig,
        provider: StructuredProvider,
        manage_models: bool = True,
        progress: ProgressReporter | None = None,
        depth: str = "balanced",
    ) -> None:
        self.global_config = global_config
        self.provider = provider
        self.manage_models = manage_models
        self.progress = progress or ProgressReporter()
        self.depth = depth

    @staticmethod
    def _load(path: Path) -> MachineTaskQueue:
        if not path.exists():
            raise RuntimeError(f"Task queue not found: {path}. Run localdev-mlx plan first.")
        return MachineTaskQueue.model_validate_json(path.read_text(encoding="utf-8"))

    @staticmethod
    def _save(path: Path, queue: MachineTaskQueue) -> None:
        path.write_text(queue.model_dump_json(indent=2), encoding="utf-8")

    @staticmethod
    def _external_prompt(root: Path, task: PlannedTask) -> Path:
        directory = root / "docs" / "ai" / "reviews" / task.id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "REVIEW_PROMPT.md"
        path.write_text(
            f"""# External Work Packet — {task.id}

## Task

### {task.title}

{task.description}

## Acceptance summary

{chr(10).join(f'- {item}' for item in task.acceptance_summary)}

## Why this is external

The implementation plan marked this work as unsuitable for autonomous local implementation.

## Instructions

Read `AGENTS.md`, the canonical `docs/ai/` documents, dependency handoffs, and relevant source/tests. Independently verify assumptions, implement safely, add strong tests, run full validation, and document the result. Do not weaken tests or silently change contracts merely to finish the task.
""",
            encoding="utf-8",
        )
        return path

    def run(
        self,
        *,
        repository: Path,
        max_tasks: int = 1,
    ) -> list[tuple[str, str]]:
        git = GitRepository(repository)
        config = load_project_config(git.root)
        integration = git.ensure_integration_worktree(config)
        if not git.is_clean(integration):
            raise RuntimeError("Integration branch must be clean before running the queue")

        queue_path = integration / "docs" / "ai" / "TASK_QUEUE.json"
        queue = self._load(queue_path)
        results: list[tuple[str, str]] = []
        executed = 0
        self.progress.emit("[QUEUE] START — scanning for dependency-ready tasks")

        for planned in queue.tasks:
            if max_tasks > 0 and executed >= max_tasks:
                break
            if planned.status != PlannedTaskStatus.PENDING:
                continue

            statuses = {item.id: item.status for item in queue.tasks}
            unmet = [
                dependency
                for dependency in planned.dependencies
                if statuses.get(dependency) != PlannedTaskStatus.COMPLETED
            ]
            if unmet:
                results.append((planned.id, f"blocked by: {', '.join(unmet)}"))
                continue

            if planned.external_only or planned.worker_tier == "external":
                planned.status = PlannedTaskStatus.ESCALATED
                prompt = self._external_prompt(integration, planned)
                self._save(queue_path, queue)
                git.commit_all(integration, f"docs: prepare external work packet {planned.id}")
                results.append((planned.id, f"external review: {prompt}"))
                self.progress.emit(f"[QUEUE] EXTERNAL — {planned.id}: {prompt}")
                break

            self.progress.emit(f"[QUEUE] STARTING — {planned.id}: {planned.title}")
            planned.status = PlannedTaskStatus.RUNNING
            self._save(queue_path, queue)
            git.commit_all(integration, f"chore: start planned task {planned.id}")

            runner = TaskRunner(
                global_config=self.global_config,
                provider=self.provider,
                manage_models=self.manage_models,
                progress=self.progress,
                depth=self.depth,  # type: ignore[arg-type]
            )
            task = runner.run(
                repository=integration,
                kind=TaskKind(planned.kind),
                description=(
                    f"Planned task {planned.id}: {planned.title}\n\n{planned.description}\n\n"
                    "Acceptance summary:\n- " + "\n- ".join(planned.acceptance_summary)
                ),
                auto_integrate=True,
            )

            # The task may have advanced the integration branch. Reload its queue.
            queue = self._load(queue_path)
            current = next(item for item in queue.tasks if item.id == planned.id)
            current.localdev_task_id = task.id
            if task.status == TaskStatus.INTEGRATED:
                current.status = PlannedTaskStatus.COMPLETED
                outcome = f"completed as {task.id}"
            else:
                current.status = PlannedTaskStatus.ESCALATED
                outcome = f"escalated as {task.id}: {task.escalation_path}"
            self._save(queue_path, queue)
            git.commit_all(integration, f"chore: update planned task {planned.id} status")
            results.append((planned.id, outcome))
            executed += 1
            self.progress.emit(f"[QUEUE] RESULT — {planned.id}: {outcome}")
            if current.status == PlannedTaskStatus.ESCALATED:
                break

        self.progress.emit(f"[QUEUE] COMPLETE — processed {executed} local task(s)")
        return results
