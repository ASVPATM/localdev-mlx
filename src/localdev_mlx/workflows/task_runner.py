from __future__ import annotations

import hashlib
import json
import os
import shlex
import time
import traceback
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Callable, Literal

from localdev_mlx.agents import LocalAgents
from localdev_mlx.config import (
    ConfigurationError,
    ExecutionConfig,
    GlobalConfig,
    ModelProfile,
    load_project_config,
)
from localdev_mlx.context import build_context
from localdev_mlx.context.builder import ContextError
from localdev_mlx.execution import run_tests
from localdev_mlx.execution.budget import DeadlineExceeded, deadline
from localdev_mlx.execution.tests import TestExecutionError, prepare_project, run_commands
from localdev_mlx.git import EditError, GitRepository, TaskWorkspace, apply_edits
from localdev_mlx.git.edits import edit_digest, file_hash
from localdev_mlx.models import ModelManager
from localdev_mlx.progress import ProgressReporter, format_duration
from localdev_mlx.providers.base import ProviderError, StructuredProvider
from localdev_mlx.providers.recording import RecordingProvider
from localdev_mlx.schemas import (
    ExternalReviewCategory,
    LocalOutcome,
    RiskLevel,
    TaskKind,
    TaskPhase,
    TaskRecord,
    TaskStatus,
    TestRunResult,
    TriageResult,
    WorkUnit,
)
from localdev_mlx.tasks import TaskStore
from localdev_mlx.workflows.planning import requirements, triage_plan_problems


class WorkflowError(RuntimeError):
    def __init__(self, message: str, *, category=ExternalReviewCategory.WORKFLOW_ERROR):
        super().__init__(message)
        self.category = category


def render_tests(result: TestRunResult) -> str:
    lines = [f"Test profile: {result.profile}", f"Overall passed: {result.passed}"]
    for command in result.commands:
        lines.extend(
            [
                "",
                f"Command: {shlex.join(command.command)}",
                f"Exit code: {command.exit_code}",
                f"Failure kind: {command.failure_kind}",
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
    """Bounded, durable controller; model responses never determine validation results."""

    def __init__(
        self,
        *,
        global_config: GlobalConfig,
        provider: StructuredProvider,
        model_manager: ModelManager | None = None,
        manage_models: bool = True,
        progress: ProgressReporter | None = None,
        depth: Literal["fast", "balanced", "deep"] = "balanced",
        max_attempts: int | None = None,
        no_fallback: bool = False,
        request_timeout: int | None = None,
        task_timeout: int | None = None,
        keep_model_loaded: bool = False,
        on_update: Callable[[TaskRecord], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ):
        if depth not in {"fast", "balanced", "deep"}:
            raise ValueError("depth must be fast, balanced, or deep")
        self.global_config, self.provider = global_config, provider
        self.agents = LocalAgents(provider)
        self.model_manager = model_manager or ModelManager()
        self.manage_models, self.depth = manage_models, depth
        self.progress = progress or ProgressReporter()
        self.overrides = {
            key: value
            for key, value in {
                "max_attempts": max_attempts,
                "request_timeout_seconds": request_timeout,
                "task_timeout_seconds": task_timeout,
            }.items()
            if value is not None
        }
        if no_fallback:
            self.overrides["fallback"] = False
        self.policy = ExecutionConfig(**self.overrides)
        self.keep_model_loaded = keep_model_loaded
        self.on_update, self.cancelled = on_update, cancelled
        self.workspace: TaskWorkspace | None = None
        self.applied_digests: set[str] = set()

    def _limited_profile(self, profile: ModelProfile, *, role: str) -> ModelProfile:
        cap = {
            "planner": self.policy.planner_timeout_seconds,
            "reviewer": self.policy.reviewer_timeout_seconds,
        }.get(role, self.policy.request_timeout_seconds)
        cap = min(cap, self.policy.request_timeout_seconds)
        factor = {"fast": 0.25, "balanced": 0.5, "deep": 1.0}[self.depth]
        return replace(
            profile,
            request_timeout_seconds=min(profile.request_timeout_seconds, cap),
            thinking_budget=int(profile.thinking_budget * factor) if profile.enable_thinking else 0,
            max_tokens=min(
                profile.max_tokens, max(1024, int(profile.max_tokens * (0.5 + factor / 2)))
            ),
        )

    def _planner_profile(self):
        return self._limited_profile(self.global_config.planner, role="planner")

    def _worker_profile(self):
        return self._limited_profile(self.global_config.worker, role="worker")

    def _reviewer_profile(self):
        return self._limited_profile(self.global_config.reviewer, role="reviewer")

    def _fallback_worker_profile(self, primary):
        if not self.policy.fallback:
            return None
        candidates = (
            [self.global_config.profile(self.policy.fallback_profile)]
            if self.policy.fallback_profile
            else [self.global_config.planner, self.global_config.reviewer]
        )
        for candidate in candidates:
            if candidate.model != primary.model:
                return self._limited_profile(candidate, role="worker")
        return None

    def _phase(self, phase, reason, status=None):
        evidence = {}
        if self.workspace and self.workspace.path.exists():
            git = GitRepository(self.workspace.path)
            evidence = {
                "worktree_commit": git.resolve_ref(self.workspace.path, "HEAD"),
                "diff_hash": hashlib.sha256(git.diff(self.workspace.path).encode()).hexdigest(),
            }
        if self.task.attempt_records:
            last = self.task.attempt_records[-1]
            evidence.update(
                model=last.model,
                input_hash=last.prompt_hash,
                output_hash=last.edit_digest or last.response_digest,
            )
        self.task.advance(phase, reason, attempt=self.task.attempts, **evidence)
        if status:
            self.task.transition(status, reason)
        self.store.save(self.task)
        self.progress.emit(f"[{self.task.id}] {phase.value.upper()} — {reason}")

    def _ensure(self, profile):
        if self.manage_models:
            with self.progress.operation(self.task.id, f"Preparing model {profile.model}"):
                self.model_manager.ensure(profile)

    def _context(
        self,
        name,
        *,
        required=(),
        optional=(),
        budget=None,
        include_map=False,
        creatable=(),
        minimal=False,
        checkpoint=None,
    ):
        bundle = build_context(
            self.workspace.path,
            replace(self.config, stable_docs=()) if minimal else self.config,
            requested_paths=list(optional),
            required_paths=list(required),
            char_budget=budget or self.config.worker_context_chars,
            include_map=include_map,
            requested_first=True,
        )
        manifest = bundle.manifest()
        if checkpoint is not None:
            for entry in manifest:
                entry["checkpoint_hash"] = checkpoint.get(entry["path"])
                entry["modified_since_checkpoint"] = entry["sha256"] != checkpoint.get(
                    entry["path"]
                )
        self.recorder.manifest_path = str(
            self.store.write_json(self.task.id, f"{name}-manifest.json", manifest)
        )
        self.store.write_text(self.task.id, f"{name}-context.txt", bundle.render())
        bundle.require_complete(creatable=set(creatable))
        return bundle

    def _tests(self, name, profile="quick", commands=()):
        self._phase(TaskPhase.VALIDATING, f"Running {name}", TaskStatus.TESTING)
        with self.progress.operation(self.task.id, name):
            result = (
                run_commands(self.workspace.path, self.config.tests, tuple(commands), "targeted")
                if commands
                else run_tests(self.workspace.path, self.config.tests, profile)
            )
        self.store.write_json(self.task.id, f"{name}.json", result)
        path = self.store.write_text(self.task.id, f"{name}.txt", render_tests(result))
        if self.task.attempt_records:
            self.task.attempt_records[-1].test_result_path = str(path)
        if result.environment_failed:
            raise WorkflowError(
                f"Test environment failed: see {path}. Configure [prepare].commands and test dependencies.",
                category=ExternalReviewCategory.PREFLIGHT_FAILURE,
            )
        return result

    def _triage(self, baseline, direct_allowed, direct_reads, plan_only):
        coverage = requirements(self.task.description, render_tests(baseline))
        self.store.write_json(self.task.id, "requirements.json", coverage)
        if direct_allowed is not None:
            unit = WorkUnit(
                title=f"Direct {self.task.kind.value} implementation",
                goal=self.task.description,
                allowed_paths=direct_allowed,
                read_paths=list(dict.fromkeys(direct_reads)),
                acceptance_criteria=[self.task.description],
                requirement_ids=list(coverage),
                test_focus=list(coverage) or [shlex.join(c.command) for c in baseline.commands],
            )
            triage = TriageResult(
                task_summary=self.task.description[:3000],
                risk=RiskLevel.MEDIUM,
                confidence=1,
                should_escalate=False,
                work_units=[unit],
                relevant_paths=list(dict.fromkeys([*direct_allowed, *direct_reads])),
                reproduction_plan=["Use controller baseline and explicit user authority."],
            )
            self.progress.emit(f"[{self.task.id}] DIRECT — planner skipped")
        else:
            self._phase(TaskPhase.PLANNING, "Preparing planner triage", TaskStatus.TRIAGING)
            failing_paths = list(
                dict.fromkeys(
                    key.split("::")[0]
                    for key in coverage
                    if "::" in key and not any(c.isspace() for c in key)
                )
            )
            context = self._context(
                "triage",
                optional=failing_paths,
                budget=self.config.planner_context_chars,
                include_map=True,
            ).render()
            profile = self._planner_profile()
            feedback = (
                "Required coverage IDs (map each to requirement_ids or deferred_requirements):\n"
                + json.dumps(coverage)
            )
            triage = None
            for attempt in range(2):
                if attempt:
                    profile = replace(
                        profile,
                        enable_thinking=False,
                        thinking_budget=0,
                        max_tokens=min(2048, profile.max_tokens),
                        request_timeout_seconds=min(
                            profile.request_timeout_seconds,
                            self.policy.planner_repair_timeout_seconds,
                        ),
                    )
                self._ensure(profile)
                try:
                    with self.progress.operation(
                        self.task.id,
                        "Planner triage inference" if not attempt else "Planner structural repair",
                    ):
                        triage = self.agents.triage(
                            profile=profile,
                            kind=self.task.kind,
                            description=self.task.description,
                            context=context,
                            baseline_tests=render_tests(baseline)[-16000:] if not attempt else "",
                            planner_feedback=feedback,
                        )
                    problems = triage_plan_problems(
                        triage,
                        task_kind=self.task.kind,
                        root=self.workspace.path,
                        config=self.config,
                        coverage=coverage,
                    )
                    if not problems:
                        break
                    self.store.write_json(
                        self.task.id, f"triage-invalid-{attempt + 1}.json", triage
                    )
                    previous = triage.model_dump_json()
                except ProviderError as exc:
                    problems, previous = [str(exc)], "Unavailable: invalid JSON/schema."
                self.store.write_text(
                    self.task.id, f"triage-invalid-{attempt + 1}.txt", "\n".join(problems)
                )
                if attempt:
                    raise WorkflowError(
                        "Planner remained invalid: " + "; ".join(problems),
                        category=ExternalReviewCategory.PLANNER_INVALID,
                    )
                feedback = (
                    "Repair only the structural errors; preserve task scope.\n"
                    + "\n".join(problems)
                    + "\nOriginal plan:\n"
                    + previous
                    + "\nCoverage IDs:\n"
                    + json.dumps(coverage)
                )
                context = context[-12000:]
                self.progress.emit(f"[{self.task.id}] REPLANNING — one compact structural repair")
        self._phase(TaskPhase.PLAN_VALIDATION, "Validating exact authority and coverage")
        problems = triage_plan_problems(
            triage,
            task_kind=self.task.kind,
            root=self.workspace.path,
            config=self.config,
            coverage=coverage,
        )
        if problems:
            raise WorkflowError(
                "Invalid plan: " + "; ".join(problems),
                category=ExternalReviewCategory.PLANNER_INVALID,
            )
        self.task.triage = triage
        self.store.write_json(self.task.id, "triage.json", triage)
        self.store.save(self.task)
        if triage.should_escalate or triage.risk == RiskLevel.EXTERNAL:
            raise WorkflowError(
                "; ".join(triage.escalation_reasons) or "Planner requires external review",
                category=ExternalReviewCategory.PLANNER_RISK,
            )
        if triage.deferred_requirements and not plan_only:
            raise WorkflowError(
                "Plan defers requested requirements: " + json.dumps(triage.deferred_requirements),
                category=ExternalReviewCategory.PLANNER_RISK,
            )
        return triage

    def _target_commands(self, unit):
        if self.target_commands:
            return self.target_commands
        nodes = [
            node for node in unit.test_focus if "::" in node and not any(c.isspace() for c in node)
        ]
        if not nodes:
            return ()
        # A model can choose pytest nodes, never an executable or arbitrary command.
        for raw in self.config.tests.quick:
            args = shlex.split(raw)
            if "pytest" in args:
                return (shlex.join([*args[: args.index("pytest") + 1], "-q", *nodes]),)
        return ()

    def _apply(self, result, unit, bundle):
        digest = edit_digest(result.edits)
        record = self.task.attempt_records[-1]
        record.edit_digest = digest
        if digest in self.applied_digests:
            raise WorkflowError(
                "Duplicate edit digest: refusing to replay previously applied edits",
                category=ExternalReviewCategory.WORKER_LIMIT,
            )
        hashes = {entry.path: entry.sha256 or "missing" for entry in bundle.entries}
        for edit in result.edits:
            # Every edit is tied to the exact snapshot shown to the model, even
            # when a legacy model omitted the optional hash field.
            expected = hashes.get(edit.path)
            if expected is None or expected != file_hash(self.workspace.path / edit.path):
                raise EditError(f"Stale or unauthorized edit snapshot for {edit.path}")
            if edit.base_hash is None:
                edit.base_hash = expected
        record.files_changed = apply_edits(
            self.workspace.path,
            result.edits,
            allowed_paths=set(unit.allowed_paths),
            deny_patterns=self.config.deny_paths,
        )
        self.applied_digests.add(digest)
        record.outcome = "edits_applied"
        self.store.save(self.task)

    def _implement(
        self, unit, index, total, evidence, *, allow_cumulative=False, review_issues=None
    ):
        primary = self._worker_profile()
        fallback = self._fallback_worker_profile(primary)
        current = primary
        previous_state = None
        checkpoint = {
            path: file_hash(self.workspace.path / path)
            for path in [*unit.allowed_paths, *unit.read_paths]
        }
        self.store.write_json(self.task.id, f"unit-{index}-checkpoint.json", checkpoint)
        for attempt in range(1, self.policy.max_attempts + 1):
            self.task.attempts += 1
            self._phase(
                TaskPhase.REPAIRING if review_issues else TaskPhase.IMPLEMENTING,
                f"Worker unit {index}/{total}, attempt {attempt}/{self.policy.max_attempts}",
                TaskStatus.REPAIRING if review_issues else TaskStatus.IMPLEMENTING,
            )
            prefix = f"unit-{index}-attempt-{attempt}"
            self.recorder.work_unit = str(index)
            required = list(dict.fromkeys([*unit.allowed_paths, *unit.read_paths]))
            bundle = self._context(
                prefix,
                required=required,
                creatable=unit.allowed_paths,
                minimal=attempt > 1,
                checkpoint=checkpoint,
            )
            diff = GitRepository(self.workspace.path).diff(self.workspace.path)
            state = hashlib.sha256((bundle.selected_files + diff + evidence).encode()).hexdigest()
            if attempt > 2 and state == previous_state:
                raise WorkflowError(
                    "No new evidence for another worker attempt",
                    category=ExternalReviewCategory.WORKER_LIMIT,
                )
            previous_state = state
            self._ensure(current)
            failure = ""
            try:
                with self.progress.operation(
                    self.task.id, f"Worker inference {index}, attempt {attempt} ({current.model})"
                ):
                    result = self.agents.implement(
                        profile=current,
                        kind=self.task.kind,
                        description=self.task.description,
                        unit=unit,
                        context=bundle.render()
                        + "\n# Current diff from task checkpoint\n"
                        + diff[-20000:],
                        previous_failures=evidence[-20000:],
                        review_issues=review_issues,
                    )
                self.store.write_json(self.task.id, f"{prefix}-implementation.json", result)
                if unit.mode == "analysis" and result.result_type == "analysis":
                    self.task.attempt_records[-1].outcome = "analysis"
                    self.progress.emit(f"[{self.task.id}] ANALYZED — {unit.title}")
                    return "\n".join([result.summary, *result.notes])
                if (
                    result.result_type == "no_change"
                    and self.task.kind == TaskKind.BUG
                    and self._target_commands(unit)
                ):
                    verified = self._tests(
                        f"{prefix}-acceptance", commands=self._target_commands(unit)
                    )
                    if verified.passed:
                        self.task.attempt_records[-1].outcome = "verified_no_change"
                        return render_tests(verified)
                if result.result_type != "edits" or unit.mode != "edit":
                    failure = (
                        result.escalation_reason
                        or "Worker returned no verifiable edits for this unit"
                    )
                else:
                    self._apply(result, unit, bundle)
                    targeted = self._target_commands(unit)
                    acceptance = (
                        self._tests(f"{prefix}-acceptance", commands=targeted) if targeted else None
                    )
                    tested = self._tests(f"{prefix}-tests")
                    if tested.passed and (acceptance is None or acceptance.passed):
                        self.task.attempt_records[-1].outcome = "validated"
                        return render_tests(tested)
                    failure = render_tests(tested) + (
                        render_tests(acceptance) if acceptance else ""
                    )
                    if allow_cumulative and (acceptance is None or acceptance.passed):
                        self.progress.emit(
                            f"[{self.task.id}] DEFERRED — remaining baseline failures go to later units"
                        )
                        return failure
            except (ProviderError, EditError) as exc:
                failure = str(exc)
            self.store.write_text(self.task.id, f"{prefix}-failure.txt", failure)
            evidence = "Repair the CURRENT files and diff; do not replay earlier edits.\n" + failure
            if attempt == 1 and fallback is not None:
                current = fallback
                self.progress.emit(
                    f"[{self.task.id}] PROMOTING — one alternate attempt with {current.model}"
                )
            elif attempt == 1 or attempt >= 2:
                # Same-model continuation requires changed code/test evidence.
                changed = GitRepository(self.workspace.path).diff(self.workspace.path) != diff
                if not changed or attempt >= self.policy.max_attempts:
                    break
        raise WorkflowError(
            f"Worker stopped after bounded attempts for {unit.title}: {failure[:3000]}",
            category=ExternalReviewCategory.WORKER_LIMIT,
        )

    def _review(self, triage, tests, suffix):
        self._verify_scope(triage)
        self._phase(
            TaskPhase.REVIEWING, "Reviewing real diff and full validation", TaskStatus.REVIEWING
        )
        required = list(
            dict.fromkeys(
                path
                for unit in triage.work_units
                for path in [*unit.allowed_paths, *unit.read_paths]
            )
        )
        # Deleted files are represented by the diff; remaining files must be complete.
        required = [p for p in required if (self.workspace.path / p).exists()]
        context = self._context(
            f"review-{suffix}", required=required, budget=self.config.reviewer_context_chars
        ).render()
        diff = GitRepository(self.workspace.path).diff(self.workspace.path)
        self.store.write_text(self.task.id, f"review-{suffix}-diff.patch", diff)
        profile = self._reviewer_profile()
        self._ensure(profile)
        self.recorder.work_unit = None
        with self.progress.operation(self.task.id, "Reviewer code-review inference"):
            review = self.agents.review(
                profile=profile,
                kind=self.task.kind,
                description=self.task.description,
                triage=triage,
                diff=diff,
                tests=render_tests(tests),
                context=context,
            )
        self.store.write_json(self.task.id, f"review-{suffix}.json", review)
        return review

    def _verify_scope(self, triage):
        git = GitRepository(self.workspace.path)
        allowed = {p for unit in triage.work_units for p in unit.allowed_paths}
        git.diff(self.workspace.path)
        changed = set(
            git._run(self.workspace.path, ["diff", "--name-only", "-z", "HEAD"])
            .stdout.strip("\x00")
            .split("\x00")
        ) - {""}
        if changed - allowed:
            raise WorkflowError(
                "Unauthorized changes produced during validation: "
                + ", ".join(sorted(changed - allowed))
            )
        return changed

    def _execute(self, git, direct_allowed, direct_reads, auto_integrate, plan_only):
        self._phase(TaskPhase.PREFLIGHT, "Creating fresh task worktree and preparing validation")
        self.workspace = git.create_task_workspace(self.config, self.task.id)
        self.task.base_commit = self.workspace.base_commit
        self.task.task_branch = self.workspace.branch
        self.task.task_worktree = str(self.workspace.path)
        self.task.integration_branch = self.workspace.integration_branch
        self.store.save(self.task)
        prepared = prepare_project(self.workspace.path, self.config.tests, self.config.prepare)
        if prepared:
            self.store.write_json(self.task.id, "prepare.json", prepared)
            self.store.write_text(self.task.id, "prepare.txt", render_tests(prepared))
            if not prepared.passed:
                raise WorkflowError(
                    "Project preparation failed; see prepare.txt",
                    category=ExternalReviewCategory.PREFLIGHT_FAILURE,
                )
        self._phase(TaskPhase.BASELINE, "Baseline quick validation before inference")
        baseline = self._tests("tests-baseline")
        # Full commands must also spawn in the fresh worktree before inference.
        if self.config.tests.full != self.config.tests.quick:
            full_baseline = self._tests("tests-baseline-full", "full")
            baseline = TestRunResult(
                profile="baseline", commands=[*baseline.commands, *full_baseline.commands]
            )
        if self.target_commands:
            targeted_baseline = self._tests(
                "tests-baseline-targeted", commands=self.target_commands
            )
            baseline = TestRunResult(
                profile="baseline", commands=[*baseline.commands, *targeted_baseline.commands]
            )
        if not git.is_clean(self.workspace.path):
            raise WorkflowError(
                "Preparation/baseline changed versioned or unignored files; fix project configuration",
                category=ExternalReviewCategory.PREFLIGHT_FAILURE,
            )
        if self.task.kind != TaskKind.BUG and not baseline.passed:
            raise WorkflowError(
                "Baseline already fails; repair it before non-bug work",
                category=ExternalReviewCategory.VALIDATION_FAILURE,
            )
        triage = self._triage(baseline, direct_allowed, direct_reads, plan_only)
        # Check all required paths before any worker is loaded.
        for index, unit in enumerate(triage.work_units, 1):
            self._context(
                f"unit-{index}-preflight",
                required=list(dict.fromkeys([*unit.allowed_paths, *unit.read_paths])),
                creatable=unit.allowed_paths,
            )
        if plan_only:
            self.task.transition(
                TaskStatus.PLANNED, "Validated plan only; no worker/reviewer inference or edits"
            )
            self._phase(TaskPhase.COMPLETED, "Plan ready in triage.json")
            return
        evidence = render_tests(baseline)
        for index, unit in enumerate(triage.work_units, 1):
            later = any(u.mode == "edit" for u in triage.work_units[index:])
            evidence = self._implement(
                unit,
                index,
                len(triage.work_units),
                evidence,
                allow_cumulative=self.task.kind == TaskKind.BUG and not baseline.passed and later,
            )
        quick = self._tests("tests-after-work-units")
        if not quick.passed:
            raise WorkflowError(
                "Final quick validation failed", category=ExternalReviewCategory.VALIDATION_FAILURE
            )
        for review_round in range(self.config.max_repair_rounds + 1):
            full = self._tests(f"tests-full-{review_round}", "full")
            if not full.passed:
                raise WorkflowError(
                    "Full test profile failed", category=ExternalReviewCategory.VALIDATION_FAILURE
                )
            review = self._review(triage, full, str(review_round))
            if review.approved and not review.should_escalate:
                break
            if review.should_escalate or review_round == self.config.max_repair_rounds:
                raise WorkflowError(review.summary, category=ExternalReviewCategory.REVIEW_REJECTED)
            if not review.issues:
                raise WorkflowError(
                    "Review rejected without actionable findings: " + review.summary,
                    category=ExternalReviewCategory.REVIEW_REJECTED,
                )
            allowed = list(dict.fromkeys(p for u in triage.work_units for p in u.allowed_paths))
            repair = WorkUnit(
                title=f"Repair reviewer findings round {review_round + 1}",
                goal=review.summary,
                allowed_paths=allowed,
                read_paths=allowed,
                acceptance_criteria=[i.required_fix for i in review.issues],
            )
            self._implement(
                repair,
                f"review-{review_round + 1}",
                1,
                render_tests(full),
                review_issues=review.issues,
            )
        self._phase(TaskPhase.COMMITTING, "Creating approved Git commit")
        changed = self._verify_scope(triage)
        if changed:
            self.task.final_commit = git.commit_all(
                self.workspace.path,
                f"localdev({self.task.kind.value}): {triage.task_summary[:72]} [{self.task.id}]",
            )
        else:
            self.task.final_commit = self.workspace.base_commit
        self.task.transition(TaskStatus.APPROVED, "Full validation and review approved")
        self.store.save(self.task)
        integrate = self.config.auto_integrate if auto_integrate is None else auto_integrate
        if integrate:
            self._phase(TaskPhase.INTEGRATING, "Verified fast-forward to integration")
            try:
                self.task.final_commit = git.integrate(self.workspace)
            except Exception as exc:
                self.task.events.append(f"Integration refused; approved commit preserved: {exc}")
                self.task.failure_category = ExternalReviewCategory.WORKFLOW_ERROR
                self._phase(
                    TaskPhase.COMPLETED, "Approved commit preserved; integration needs fresh review"
                )
                return
            self.task.transition(
                TaskStatus.INTEGRATED, f"Fast-forwarded {self.config.integration_branch}"
            )
            self.task.local_outcome = LocalOutcome.INTEGRATED
            self.store.save(self.task)
            try:
                git.cleanup_task_workspace(self.workspace)
                self.task.task_worktree = None
            except Exception as exc:
                self.task.events.append(f"Worktree cleanup warning: {exc}")
        self._phase(TaskPhase.COMPLETED, "Task completed")

    def run(
        self,
        *,
        repository: Path,
        kind: TaskKind,
        description: str,
        auto_integrate: bool | None = None,
        direct_allowed_paths: list[str] | None = None,
        direct_read_paths: list[str] | None = None,
        direct_test_commands: list[str] | None = None,
        plan_only: bool = False,
    ) -> TaskRecord:
        git = GitRepository(repository)
        self.store = TaskStore(git.root, on_save=self.on_update)
        self.task = self.store.create(kind, description, "local")
        self.task.controller_pid = os.getpid()
        self.workspace, self.applied_digests = None, set()
        self.target_commands = tuple(direct_test_commands or [])
        self.recorder = RecordingProvider(self.provider, self.store, self.task, self.progress)
        self.agents = LocalAgents(self.recorder)
        started = time.monotonic()
        runtime = ExitStack()
        self.progress.emit(f"[{self.task.id}] CREATED — {kind.value} workflow (depth={self.depth})")
        try:
            self.config = load_project_config(git.root)
            self.policy = replace(self.config.execution, **self.overrides)
            if self.manage_models:
                runtime.enter_context(self.model_manager.lease())
            with deadline(
                self.policy.task_timeout_seconds,
                label="Whole task",
                cancelled=lambda: (
                    (self.store.path(self.task.id) / "cancel.request").exists()
                    or bool(self.cancelled and self.cancelled())
                ),
            ):
                self._execute(
                    git, direct_allowed_paths, direct_read_paths or [], auto_integrate, plan_only
                )
        except (Exception, KeyboardInterrupt) as exc:
            category = getattr(exc, "category", ExternalReviewCategory.WORKFLOW_ERROR)
            if isinstance(exc, (TestExecutionError, ConfigurationError)):
                category = ExternalReviewCategory.CONFIGURATION_ERROR
            elif isinstance(exc, ContextError):
                category = ExternalReviewCategory.PREFLIGHT_FAILURE
            elif isinstance(exc, DeadlineExceeded):
                category = ExternalReviewCategory.WORKER_LIMIT
            external = category in {
                ExternalReviewCategory.PLANNER_RISK,
                ExternalReviewCategory.WORKER_LIMIT,
                ExternalReviewCategory.VALIDATION_FAILURE,
                ExternalReviewCategory.REVIEW_REJECTED,
            }
            status = (
                TaskStatus.CANCELLED
                if isinstance(exc, KeyboardInterrupt)
                else (TaskStatus.ESCALATED if external else TaskStatus.FAILED)
            )
            self.task.transition(status, str(exc) or "Interrupted by user")
            self.task.failure_category = category
            if external and status != TaskStatus.CANCELLED:
                self.task.mark_external_review(category=category, reason=str(exc))
            self.task.events.append(traceback.format_exc(limit=6))
            self.task.advance(TaskPhase.STOPPED, str(exc) or "Cancelled")
            self.store.save(self.task)
            try:
                if self.workspace and self.workspace.path.exists():
                    self.store.write_text(
                        self.task.id, "CURRENT_DIFF.patch", git.diff(self.workspace.path)
                    )
                if external and status != TaskStatus.CANCELLED and self.on_update is None:
                    from localdev_mlx.escalation.bundle import build_escalation_bundle

                    bundle, visible = build_escalation_bundle(
                        task=self.task,
                        store=self.store,
                        repository=git,
                        workspace=self.workspace,
                        error=str(exc),
                        category=category,
                    )
                    self.task.escalation_path, self.task.visible_review_path = (
                        str(bundle),
                        str(visible),
                    )
            except Exception as artifact_error:
                self.task.events.append(f"Artifact export warning: {artifact_error}")
            self.progress.emit(
                f"[{self.task.id}] {status.value.upper()} category={category.value} — {exc}"
            )
        finally:
            if (
                self.manage_models
                and self.global_config.stop_after_run
                and not self.keep_model_loaded
            ):
                try:
                    self.model_manager.stop()
                except Exception as exc:
                    self.task.events.append(f"Model cleanup warning: {exc}")
                    self.progress.emit(f"[{self.task.id}] WARNING — model cleanup: {exc}")
            self.task.controller_pid = None
            runtime.close()
            self.task.timings_seconds = self.progress.timings_for(self.task.id)
            self.task.timings_seconds["Total workflow"] = round(time.monotonic() - started, 3)
            self.store.save(self.task)
            self.progress.emit(
                f"[{self.task.id}] COMPLETE — status={self.task.status.value}, "
                f"elapsed={format_duration(time.monotonic() - started)}"
            )
        return self.task
