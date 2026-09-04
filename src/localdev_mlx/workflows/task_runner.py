from __future__ import annotations

import json
import time
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Literal

from localdev_mlx.agents import LocalAgents
from localdev_mlx.config import GlobalConfig, ModelProfile, ProjectConfig, load_project_config
from localdev_mlx.context import build_context
from localdev_mlx.execution import run_tests
from localdev_mlx.git import EditError, GitRepository, TaskWorkspace, apply_edits
from localdev_mlx.models import ModelManager
from localdev_mlx.progress import ProgressReporter, format_duration
from localdev_mlx.providers.base import StructuredProvider
from localdev_mlx.schemas import (
    ImplementationResult,
    ReviewResult,
    RiskLevel,
    TaskKind,
    TaskRecord,
    TaskStatus,
    TestRunResult,
    TriageResult,
    WorkUnit,
)
from localdev_mlx.tasks import TaskStore


class WorkflowError(RuntimeError):
    """Raised when a LocalDev workflow cannot proceed safely."""


def render_tests(result: TestRunResult) -> str:
    lines = [f"Test profile: {result.profile}", f"Overall passed: {result.passed}"]
    for command in result.commands:
        lines.extend(
            [
                "",
                f"Command: {' '.join(command.command)}",
                f"Exit code: {command.exit_code}",
                f"Timed out: {command.timed_out}",
                f"Duration: {command.duration_seconds:.3f}s",
                "STDOUT:",
                command.stdout,
                "STDERR:",
                command.stderr,
            ]
        )
    return "\n".join(lines)


class TaskRunner:
    """Run one bounded maintenance task through plan, edit, test, and review."""

    def __init__(
        self,
        *,
        global_config: GlobalConfig,
        provider: StructuredProvider,
        model_manager: ModelManager | None = None,
        manage_models: bool = True,
        progress: ProgressReporter | None = None,
        depth: Literal["fast", "balanced", "deep"] = "balanced",
    ) -> None:
        if depth not in {"fast", "balanced", "deep"}:
            raise ValueError("depth must be fast, balanced, or deep")
        self.global_config = global_config
        self.provider = provider
        self.agents = LocalAgents(provider)
        self.model_manager = model_manager or ModelManager()
        self.manage_models = manage_models
        self.progress = progress or ProgressReporter()
        self.depth = depth

    def _limited_profile(self, profile: ModelProfile, *, role: str) -> ModelProfile:
        if self.depth == "deep":
            return profile
        thinking_factor = 0.25 if self.depth == "fast" else 0.50
        output_factor = 0.50 if self.depth == "fast" else 0.75
        thinking_floor = 192 if role == "worker" else 256
        thinking = (
            0
            if not profile.enable_thinking
            else max(thinking_floor, int(profile.thinking_budget * thinking_factor))
        )
        return replace(
            profile,
            thinking_budget=min(profile.thinking_budget, thinking),
            max_tokens=min(profile.max_tokens, max(1024, int(profile.max_tokens * output_factor))),
        )

    def _planner_profile(self) -> ModelProfile:
        return self._limited_profile(self.global_config.planner, role="planner")

    def _worker_profile(self) -> ModelProfile:
        return self._limited_profile(self.global_config.worker, role="worker")

    def _reviewer_profile(self) -> ModelProfile:
        return self._limited_profile(self.global_config.reviewer, role="reviewer")

    def _ensure_profile(self, task_id: str, role: str, profile: ModelProfile) -> None:
        if self.manage_models:
            with self.progress.operation(task_id, f"Preparing {role} model {profile.model}"):
                self.model_manager.ensure(profile)

    def _save_task(
        self,
        store: TaskStore,
        task: TaskRecord,
        status: TaskStatus,
        message: str,
    ) -> None:
        task.transition(status, message)
        store.save(task)
        self.progress.emit(f"[{task.id}] {status.value.upper()} — {message}")

    def _triage(
        self,
        *,
        task: TaskRecord,
        store: TaskStore,
        root: Path,
        config: ProjectConfig,
    ) -> TriageResult:
        self._save_task(
            store,
            task,
            TaskStatus.TRIAGING,
            "Preparing repository map and planner triage",
        )
        context = build_context(
            root,
            config,
            char_budget=config.planner_context_chars,
            include_map=True,
        ).render()
        store.write_text(task.id, "triage-context.txt", context)
        self.progress.emit(
            f"[{task.id}] TRIAGING — prepared {len(context):,} characters of planner context"
        )
        profile = self._planner_profile()
        self._ensure_profile(task.id, "planner", profile)
        with self.progress.operation(task.id, "Planner triage inference"):
            triage = self.agents.triage(
                profile=profile,
                kind=task.kind,
                description=task.description,
                context=context,
            )
        task.triage = triage
        store.write_json(task.id, "triage.json", triage)
        store.save(task)
        return triage

    def _worker_context(
        self,
        workspace: TaskWorkspace,
        config: ProjectConfig,
        unit: WorkUnit,
    ) -> str:
        requested = list(dict.fromkeys([*unit.read_paths, *unit.allowed_paths]))
        return build_context(
            workspace.path,
            config,
            requested_paths=requested,
            char_budget=config.worker_context_chars,
            include_map=False,
        ).render()

    def _review_context(
        self,
        workspace: TaskWorkspace,
        config: ProjectConfig,
        triage: TriageResult,
    ) -> str:
        requested = list(triage.relevant_paths)
        for unit in triage.work_units:
            requested.extend(unit.read_paths)
            requested.extend(unit.allowed_paths)
        return build_context(
            workspace.path,
            config,
            requested_paths=list(dict.fromkeys(requested)),
            char_budget=config.reviewer_context_chars,
            include_map=False,
        ).render()

    @staticmethod
    def _all_allowed_paths(triage: TriageResult) -> set[str]:
        result: set[str] = set()
        for unit in triage.work_units:
            result.update(unit.allowed_paths)
        return result

    def _apply_result(
        self,
        *,
        workspace: TaskWorkspace,
        config: ProjectConfig,
        unit: WorkUnit,
        result: ImplementationResult,
    ) -> list[str]:
        if result.needs_escalation:
            raise WorkflowError(result.escalation_reason or "Worker requested escalation")
        return apply_edits(
            workspace.path,
            result.edits,
            allowed_paths=set(unit.allowed_paths),
            deny_patterns=config.deny_paths,
        )

    def _implement_units(
        self,
        *,
        task: TaskRecord,
        store: TaskStore,
        workspace: TaskWorkspace,
        config: ProjectConfig,
        triage: TriageResult,
        baseline_tests: str,
    ) -> tuple[TestRunResult, list[ImplementationResult]]:
        profile = self._worker_profile()
        self._ensure_profile(task.id, "worker", profile)
        implementations: list[ImplementationResult] = []
        latest_tests: TestRunResult | None = None

        for index, unit in enumerate(triage.work_units, start=1):
            self._save_task(
                store,
                task,
                TaskStatus.IMPLEMENTING,
                f"Worker implementing unit {index}/{len(triage.work_units)}: {unit.title}",
            )
            failures = baseline_tests if index == 1 else ""
            completed = False

            for attempt in range(1, config.max_repair_rounds + 2):
                task.attempts += 1
                context = self._worker_context(workspace, config, unit)
                store.write_text(
                    task.id,
                    f"unit-{index:02d}-attempt-{attempt}-context.txt",
                    context,
                )
                with self.progress.operation(
                    task.id,
                    f"Worker inference for unit {index}/{len(triage.work_units)}, attempt {attempt}",
                ):
                    result = self.agents.implement(
                        profile=profile,
                        kind=task.kind,
                        description=task.description,
                        unit=unit,
                        context=context,
                        previous_failures=failures,
                    )
                store.write_json(
                    task.id,
                    f"unit-{index:02d}-attempt-{attempt}-implementation.json",
                    result,
                )
                implementations.append(result)

                try:
                    self._apply_result(
                        workspace=workspace,
                        config=config,
                        unit=unit,
                        result=result,
                    )
                except (EditError, WorkflowError) as exc:
                    failures = f"Edit application failed: {exc}"
                    store.write_text(
                        task.id,
                        f"unit-{index:02d}-attempt-{attempt}-error.txt",
                        failures,
                    )
                    if attempt > config.max_repair_rounds:
                        raise WorkflowError(failures) from exc
                    continue

                self._save_task(
                    store,
                    task,
                    TaskStatus.TESTING,
                    f"Running quick tests for unit {index}",
                )
                with self.progress.operation(task.id, f"Quick tests for unit {index}"):
                    latest_tests = run_tests(workspace.path, config.tests, "quick")
                test_text = render_tests(latest_tests)
                store.write_text(
                    task.id,
                    f"unit-{index:02d}-attempt-{attempt}-tests.txt",
                    test_text,
                )
                if latest_tests.passed:
                    completed = True
                    break
                failures = test_text
                if attempt > config.max_repair_rounds:
                    raise WorkflowError(f"Quick tests failed for work unit {unit.title!r}")

            if not completed:
                raise WorkflowError(f"Worker did not complete work unit {unit.title!r}")

        if latest_tests is None:
            raise WorkflowError("No implementation work units were completed")
        return latest_tests, implementations

    def _review(
        self,
        *,
        task: TaskRecord,
        store: TaskStore,
        workspace: TaskWorkspace,
        config: ProjectConfig,
        triage: TriageResult,
        tests: TestRunResult,
        suffix: str,
    ) -> ReviewResult:
        self._save_task(store, task, TaskStatus.REVIEWING, "Switching to reviewer")
        diff = GitRepository(workspace.path).diff(workspace.path)
        context = self._review_context(workspace, config, triage)
        store.write_text(task.id, f"review-{suffix}-diff.patch", diff)
        store.write_text(task.id, f"review-{suffix}-context.txt", context)
        profile = self._reviewer_profile()
        self._ensure_profile(task.id, "reviewer", profile)
        with self.progress.operation(task.id, "Reviewer code-review inference"):
            review = self.agents.review(
                profile=profile,
                kind=task.kind,
                description=task.description,
                triage=triage,
                diff=diff,
                tests=render_tests(tests),
                context=context,
            )
        store.write_json(task.id, f"review-{suffix}.json", review)
        return review

    def _repair_from_review(
        self,
        *,
        task: TaskRecord,
        store: TaskStore,
        workspace: TaskWorkspace,
        config: ProjectConfig,
        triage: TriageResult,
        review: ReviewResult,
        round_number: int,
    ) -> TestRunResult:
        allowed = sorted(self._all_allowed_paths(triage))
        if not allowed:
            raise WorkflowError("Reviewer requested repair but no paths are allowlisted")
        repair_unit = WorkUnit(
            title=f"Repair reviewer findings round {round_number}",
            goal="Resolve every required reviewer finding without expanding task scope.",
            allowed_paths=allowed,
            read_paths=allowed,
            acceptance_criteria=[issue.required_fix for issue in review.issues],
            test_focus=review.additional_test_requests,
        )
        self._save_task(store, task, TaskStatus.REPAIRING, repair_unit.title)
        profile = self._worker_profile()
        self._ensure_profile(task.id, "worker", profile)
        context = self._worker_context(workspace, config, repair_unit)
        with self.progress.operation(task.id, f"Worker repair inference round {round_number}"):
            result = self.agents.implement(
                profile=profile,
                kind=task.kind,
                description=task.description,
                unit=repair_unit,
                context=context,
                review_issues=review.issues,
            )
        store.write_json(task.id, f"repair-{round_number}-implementation.json", result)
        self._apply_result(
            workspace=workspace,
            config=config,
            unit=repair_unit,
            result=result,
        )
        with self.progress.operation(task.id, f"Quick tests after repair round {round_number}"):
            tests = run_tests(workspace.path, config.tests, "quick")
        store.write_text(task.id, f"repair-{round_number}-tests.txt", render_tests(tests))
        return tests

    def _write_handoff(
        self,
        *,
        task: TaskRecord,
        workspace: TaskWorkspace,
        tests: TestRunResult,
        review: ReviewResult,
    ) -> Path:
        path = workspace.path / "docs" / "ai" / "handoffs" / f"{task.id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        changed = GitRepository(workspace.path).status_porcelain(workspace.path)
        content = f"""# {task.id} — LocalDev MLX handoff

## Task

- Kind: `{task.kind.value}`
- Description: {task.description}
- Worker: `{task.worker}`

## Triage

- Risk: `{task.triage.risk.value if task.triage else 'unknown'}`
- Confidence: `{task.triage.confidence if task.triage else 'unknown'}`

## Completed

{review.summary}

## Working-tree changes before commit

```text
{changed}
```

## Validation

```text
{render_tests(tests)}
```

## Local review

- Approved: `{review.approved}`
- Confidence: `{review.confidence}`
- External review requested: `{review.should_escalate}`

## Review issues

{json.dumps([issue.model_dump() for issue in review.issues], indent=2)}

## Review note

This change was generated and reviewed locally. Include it in the independent release review appropriate for the project.
"""
        path.write_text(content, encoding="utf-8")
        return path

    def run(
        self,
        *,
        repository: Path,
        kind: TaskKind,
        description: str,
        auto_integrate: bool | None = None,
    ) -> TaskRecord:
        git = GitRepository(repository)
        config = load_project_config(git.root)
        store = TaskStore(git.root)
        task = store.create(kind, description, "local")
        workflow_started = time.monotonic()
        self.progress.emit(
            f"[{task.id}] CREATED — {kind.value} workflow started (depth={self.depth})"
        )
        workspace: TaskWorkspace | None = None

        try:
            integration_path = git.ensure_integration_worktree(config)
            triage = self._triage(
                task=task,
                store=store,
                root=integration_path,
                config=config,
            )
            if triage.should_escalate or triage.risk == RiskLevel.EXTERNAL:
                reason = "; ".join(triage.escalation_reasons) or (
                    "Planner classified the task as requiring external review"
                )
                raise WorkflowError(reason)

            workspace = git.create_task_workspace(config, task.id)
            task.task_branch = workspace.branch
            task.task_worktree = str(workspace.path)
            task.integration_branch = workspace.integration_branch
            store.save(task)

            self._save_task(
                store,
                task,
                TaskStatus.TESTING,
                "Running baseline quick validation",
            )
            with self.progress.operation(task.id, "Baseline quick validation"):
                baseline = run_tests(workspace.path, config.tests, "quick")
            baseline_text = render_tests(baseline)
            store.write_text(task.id, "tests-baseline.txt", baseline_text)
            if task.kind != TaskKind.BUG and not baseline.passed:
                raise WorkflowError(
                    "The configured baseline tests already fail before this non-bug task. "
                    "Repair or document the baseline first."
                )

            tests, _ = self._implement_units(
                task=task,
                store=store,
                workspace=workspace,
                config=config,
                triage=triage,
                baseline_tests=baseline_text,
            )

            review = self._review(
                task=task,
                store=store,
                workspace=workspace,
                config=config,
                triage=triage,
                tests=tests,
                suffix="initial",
            )
            repair_round = 0
            while not review.approved and not review.should_escalate:
                repair_round += 1
                if repair_round > config.max_repair_rounds:
                    break
                tests = self._repair_from_review(
                    task=task,
                    store=store,
                    workspace=workspace,
                    config=config,
                    triage=triage,
                    review=review,
                    round_number=repair_round,
                )
                if not tests.passed:
                    continue
                review = self._review(
                    task=task,
                    store=store,
                    workspace=workspace,
                    config=config,
                    triage=triage,
                    tests=tests,
                    suffix=f"repair-{repair_round}",
                )

            if not review.approved or review.should_escalate:
                reasons = review.escalation_reasons or [
                    issue.description
                    for issue in review.issues
                    if issue.severity in {"high", "critical"}
                ]
                raise WorkflowError(
                    "; ".join(reasons) or "Local review did not approve the implementation"
                )

            self._save_task(
                store,
                task,
                TaskStatus.TESTING,
                "Running full configured validation",
            )
            with self.progress.operation(task.id, "Full configured validation"):
                full_tests = run_tests(workspace.path, config.tests, "full")
            store.write_text(task.id, "tests-full.txt", render_tests(full_tests))
            if not full_tests.passed:
                raise WorkflowError("Full test profile failed")

            self._write_handoff(
                task=task,
                workspace=workspace,
                tests=full_tests,
                review=review,
            )
            with self.progress.operation(task.id, "Creating approved Git commit"):
                commit = git.commit_all(
                    workspace.path,
                    f"localdev({kind.value}): {triage.task_summary[:72]} [{task.id}]",
                )
            task.final_commit = commit
            self._save_task(
                store,
                task,
                TaskStatus.APPROVED,
                f"Committed approved task as {commit}",
            )

            integrate = config.auto_integrate if auto_integrate is None else auto_integrate
            if integrate:
                with self.progress.operation(
                    task.id,
                    f"Integrating into {config.integration_branch}",
                ):
                    integrated_commit = git.integrate(workspace)
                task.final_commit = integrated_commit
                self._save_task(
                    store,
                    task,
                    TaskStatus.INTEGRATED,
                    f"Fast-forwarded {config.integration_branch} to {integrated_commit}",
                )
                git.cleanup_task_workspace(workspace)
                task.task_worktree = None
                store.save(task)

            elapsed_seconds = time.monotonic() - workflow_started
            task.timings_seconds = self.progress.timings_for(task.id)
            task.timings_seconds["Total workflow"] = round(elapsed_seconds, 3)
            store.save(task)
            self.progress.emit(
                f"[{task.id}] COMPLETE — status={task.status.value}, "
                f"total elapsed={format_duration(elapsed_seconds)}"
            )
            return task

        except Exception as exc:
            task.events.append(traceback.format_exc(limit=8))
            task.transition(TaskStatus.ESCALATED, str(exc))
            store.save(task)
            from localdev_mlx.escalation.bundle import build_escalation_bundle

            bundle = build_escalation_bundle(
                task=task,
                store=store,
                repository=git,
                workspace=workspace,
                error=str(exc),
            )
            task.escalation_path = str(bundle)
            elapsed_seconds = time.monotonic() - workflow_started
            task.timings_seconds = self.progress.timings_for(task.id)
            task.timings_seconds["Total workflow"] = round(elapsed_seconds, 3)
            store.save(task)
            self.progress.emit(
                f"[{task.id}] ESCALATED — {exc} "
                f"(total elapsed={format_duration(elapsed_seconds)})"
            )
            return task
