from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from localdev_mlx.config import ProjectConfig, load_project_config
from localdev_mlx.config import TestConfig as CommandConfig
from localdev_mlx.context.builder import ContextError, build_context
from localdev_mlx.execution.budget import DeadlineExceeded, deadline
from localdev_mlx.execution.tests import run_tests
from localdev_mlx.git import GitRepository
from localdev_mlx.git.edits import EditError, apply_edits, file_hash
from localdev_mlx.providers.mock import MockStructuredProvider
from localdev_mlx.schemas import (
    ExternalReviewState,
    FileEdit,
    ImplementationResult,
    LocalOutcome,
    TaskKind,
    TaskPhase,
    TaskRecord,
    TaskStatus,
    TriageResult,
    WorkUnit,
)
from localdev_mlx.tasks import TaskStore
from localdev_mlx.workflows.planning import requirements, triage_plan_problems
from localdev_mlx.workflows.task_runner import TaskRunner


class Responses(MockStructuredProvider):
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["response_model"] is ImplementationResult:
            value = next(self.responses)
            if callable(value):
                return value(kwargs)
            return ImplementationResult.model_validate(value)
        return super().complete_structured(**kwargs)


def edit(old="return a + b  # intentional demo bug", new="return a - b"):
    return {
        "summary": "Repair subtraction",
        "result_type": "edits",
        "edits": [
            {
                "operation": "replace_text",
                "path": "src/samplecalc/core.py",
                "old_text": old,
                "new_text": new,
                "reason": "Use subtraction semantics",
            }
        ],
    }


NO_CHANGE = {
    "summary": "Cannot identify edits",
    "result_type": "no_change",
    "notes": ["Inspected current contents, no controller verification available."],
}


def run_direct(repo, config, provider, **options):
    return TaskRunner(global_config=config, provider=provider, manage_models=False, **options).run(
        repository=repo,
        kind=TaskKind.BUG,
        description="Correct subtraction",
        direct_allowed_paths=["src/samplecalc/core.py"],
        direct_read_paths=["tests/test_core.py"],
    )


@pytest.mark.parametrize("payload", [{"summary": "prose only"}, NO_CHANGE])
def test_unproductive_worker_ends_after_one_fallback(sample_repo, global_config, payload):
    provider = Responses([payload, payload])
    task = run_direct(sample_repo, global_config, provider, max_attempts=4)
    assert task.failure_category.value == "worker_limit"
    assert task.attempts == 2
    assert len(provider.calls) == 2
    assert task.external_review_state == ExternalReviewState.PENDING
    assert all(
        a.ended_at and a.response_path and a.context_manifest_path for a in task.attempt_records
    )
    assert TaskStore(sample_repo).load(task.id).phase == TaskPhase.STOPPED


def test_no_fallback_stops_after_primary(sample_repo, global_config):
    provider = Responses([NO_CHANGE])
    task = run_direct(sample_repo, global_config, provider, no_fallback=True)
    assert task.attempts == 1 and task.failure_category.value == "worker_limit"


def test_duplicate_edit_stops_and_preserves_diff(sample_repo, global_config):
    bad = edit(new="return a * b")
    task = run_direct(sample_repo, global_config, Responses([bad, bad]))
    assert task.attempts == 2 and "Duplicate edit digest" in task.external_review_reason
    assert "return a * b" in (Path(task.task_worktree) / "src/samplecalc/core.py").read_text()
    assert "intentional demo bug" in (sample_repo / "src/samplecalc/core.py").read_text()


def test_retry_sees_current_files_and_diff(sample_repo, global_config):
    def repair(kwargs):
        assert "return a * b" in kwargs["user_prompt"]
        assert "Current diff" in kwargs["user_prompt"]
        assert "Repair the CURRENT" in kwargs["user_prompt"]
        return ImplementationResult.model_validate(edit("return a * b", "return a - b"))

    task = run_direct(sample_repo, global_config, Responses([edit(new="return a * b"), repair]))
    assert task.status == TaskStatus.INTEGRATED
    assert len({a.edit_digest for a in task.attempt_records if a.edit_digest}) == 2


@pytest.mark.parametrize("field,value", [("work_units", []), ("work_units", None)])
def test_empty_or_omitted_plan_rejected(field, value):
    data = {"task_summary": "repair", "risk": "low", "confidence": 1, "should_escalate": False}
    if value is not None:
        data[field] = value
    with pytest.raises(ValidationError):
        TriageResult.model_validate(data)


@pytest.mark.parametrize("path", ["/tmp/a.py", "../a.py", ".", "C:\\a.py", "src/../../a.py"])
def test_unsafe_authority_rejected_at_schema(path):
    with pytest.raises(ValidationError):
        WorkUnit(title="repair", goal="repair", allowed_paths=[path])


@pytest.mark.parametrize("path", ["src", ".env", "missing/test.py", ".git/config"])
def test_invalid_repository_authority_stops_before_worker(sample_repo, global_config, path):
    task = TaskRunner(global_config=global_config, provider=Responses([]), manage_models=False).run(
        repository=sample_repo,
        kind=TaskKind.BUG,
        description="Repair subtraction",
        direct_allowed_paths=["src/samplecalc/core.py"],
        direct_read_paths=[path],
    )
    assert task.status == TaskStatus.FAILED and not task.attempt_records
    assert task.failure_category.value == "planner_invalid"


def test_dependency_and_coverage_validation():
    plan = TriageResult(
        task_summary="repair",
        risk="low",
        confidence=1,
        should_escalate=False,
        work_units=[
            WorkUnit(title="repair", goal="repair", allowed_paths=["a.py"], dependencies=[1, 9])
        ],
    )
    coverage = requirements(
        "1. First defect\n2. Second defect", "FAILED tests/a.py::test_a - AssertionError"
    )
    problems = triage_plan_problems(plan, task_kind=TaskKind.BUG, coverage=coverage)
    assert any("dependencies" in p for p in problems)
    assert all(any(key in p for p in problems) for key in coverage)


@pytest.mark.parametrize(
    "missing",
    ["task_summary", "reproduction_plan", "relevant_paths", "acceptance_criteria", "test_focus"],
)
def test_semantic_plan_requires_verifiable_work(missing):
    command = "python3 -m unittest discover -s tests -v"
    plan = TriageResult(
        task_summary="Repair subtraction",
        risk="low",
        confidence=1,
        should_escalate=False,
        reproduction_plan=["Run the baseline command"],
        relevant_paths=["calc.py"],
        work_units=[
            WorkUnit(
                title="Repair",
                goal="Subtract correctly",
                allowed_paths=["calc.py"],
                acceptance_criteria=["subtract(7, 2) is 5"],
                test_focus=[command],
            )
        ],
    )
    coverage = requirements("", f"Command: {command}\nExit code: 1\n")
    assert command in coverage
    assert triage_plan_problems(plan, task_kind=TaskKind.BUG, coverage=coverage) == []
    if missing in {"acceptance_criteria", "test_focus"}:
        setattr(plan.work_units[0], missing, [])
    else:
        setattr(plan, missing, " " if missing == "task_summary" else [])
    assert any(
        missing in p for p in triage_plan_problems(plan, task_kind=TaskKind.BUG, coverage=coverage)
    )


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("missing", "missing"),
        ("large", "truncated"),
        ("binary", "binary"),
        ("link", "symlink"),
        ("budget", "budget"),
    ],
)
def test_required_context_manifest(tmp_path, scenario, expected):
    target = tmp_path / "required.py"
    if scenario == "large":
        target.write_text("x" * 30)
    elif scenario == "binary":
        target.write_bytes(b"abc\x00def")
    elif scenario == "link":
        (tmp_path / "real.py").write_text("ok")
        target.symlink_to(tmp_path / "real.py")
    elif scenario == "budget":
        target.write_text("print('ok')")
    config = ProjectConfig(repository=tmp_path, stable_docs=(), max_file_chars=10)
    bundle = build_context(
        tmp_path,
        config,
        required_paths=["required.py"],
        include_map=False,
        char_budget=10 if scenario == "budget" else 1000,
    )
    assert bundle.entries[0].status == expected
    with pytest.raises(ContextError):
        bundle.require_complete()


def test_symlink_parent_and_secret_cannot_be_edited(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "a.py").write_text("original")
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    for path in ["link/a.py", ".env", ".git/config"]:
        with pytest.raises(EditError):
            apply_edits(
                tmp_path,
                [FileEdit(operation="replace_file", path=path, content="bad", reason="test")],
                allowed_paths={path},
                deny_patterns=(),
            )
    assert (real / "a.py").read_text() == "original"


def test_hash_precondition_prevents_stale_replace(tmp_path):
    path = tmp_path / "a.py"
    path.write_text("before")
    digest = file_hash(path)
    path.write_text("concurrent change")
    proposal = FileEdit(
        operation="replace_file", path="a.py", content="after", reason="test", base_hash=digest
    )
    with pytest.raises(EditError, match="Stale"):
        apply_edits(tmp_path, [proposal], allowed_paths={"a.py"}, deny_patterns=())
    assert path.read_text() == "concurrent change"


@pytest.mark.parametrize(
    "command,kind",
    [
        ("python3 -m nonexistent_test_dependency", "spawn"),
        ("python3 -c 'assert False'", "assertion"),
        ("python3 -c 'import time; time.sleep(3)'", "timeout"),
    ],
)
def test_command_failure_categories(tmp_path, command, kind):
    result = run_tests(tmp_path, CommandConfig(quick=(command,), timeout_seconds=1), "quick")
    assert result.commands[0].failure_kind == kind


def test_missing_executable_and_collect_all(tmp_path):
    config = CommandConfig(
        quick=("no-such-executable", "python3 -c 'print(123)'"),
        allowed_executables=("no-such-executable", "python3"),
    )
    result = run_tests(tmp_path, config, "quick")
    assert result.environment_failed and len(result.commands) == 2
    assert len(run_tests(tmp_path, replace(config, fail_fast=True), "quick").commands) == 1


def test_preparation_failure_never_reaches_inference(sample_repo, global_config):
    path = sample_repo / ".localdev/config.toml"
    path.write_text(
        path.read_text().replace("commands = []", 'commands = ["python3 -m missing_pytest"]')
    )
    GitRepository(sample_repo).commit_all(sample_repo, "configure preparation")
    task = run_direct(sample_repo, global_config, Responses([]))
    assert task.failure_category.value == "preflight_failure" and task.attempts == 0
    assert task.external_review_state == ExternalReviewState.NONE


@pytest.mark.parametrize("cancelled", [False, True])
def test_deadline_and_cancel_preserve_task(sample_repo, global_config, cancelled):
    def wait(kwargs):
        if cancelled:
            raise KeyboardInterrupt()
        time.sleep(4)
        return ImplementationResult.model_validate(NO_CHANGE)

    started = time.monotonic()
    task = run_direct(
        sample_repo, global_config, Responses([wait]), request_timeout=1, no_fallback=True
    )
    assert time.monotonic() - started < 3
    assert task.status == (TaskStatus.CANCELLED if cancelled else TaskStatus.ESCALATED)
    assert TaskStore(sample_repo).load(task.id).controller_pid is None
    assert Path(task.task_worktree).exists()
    assert (TaskStore(sample_repo).path(task.id) / "CURRENT_DIFF.patch").exists()


def test_nested_deadline_uses_earliest_budget():
    started = time.monotonic()
    with pytest.raises(DeadlineExceeded, match="outer"):
        with deadline(0.15, label="outer"):
            with deadline(2, label="inner"):
                time.sleep(2)
    assert time.monotonic() - started < 0.5


def test_dirty_integration_blocks_and_main_stays_unchanged(sample_repo, global_config):
    git = GitRepository(sample_repo)
    original = git.resolve_ref(sample_repo, "main")
    (sample_repo / "README.md").write_text("user work")
    task = run_direct(sample_repo, global_config, Responses([]))
    assert task.status == TaskStatus.FAILED and task.attempts == 0
    assert (sample_repo / "README.md").read_text() == "user work"
    assert git.resolve_ref(sample_repo, "main") == original


def test_integration_advancement_keeps_approved_task(sample_repo, global_config):
    class Advancing(MockStructuredProvider):
        def complete_structured(self, **kwargs):
            if kwargs["schema_name"] == "LocalDevReview":
                (sample_repo / "README.md").write_text("concurrent integration work")
                GitRepository(sample_repo).commit_all(sample_repo, "concurrent")
            return super().complete_structured(**kwargs)

    task = run_direct(sample_repo, global_config, Advancing())
    assert task.local_outcome == LocalOutcome.APPROVED_NOT_INTEGRATED
    assert task.final_commit and task.task_worktree
    assert "intentional demo bug" in (sample_repo / "src/samplecalc/core.py").read_text()


def test_cleanup_refuses_dirty_and_existing_task_branch(sample_repo):
    git = GitRepository(sample_repo)
    config = load_project_config(sample_repo)
    workspace = git.create_task_workspace(config, "BUG-unique")
    (workspace.path / "README.md").write_text("valuable work")
    with pytest.raises(RuntimeError, match="dirty"):
        git.cleanup_task_workspace(workspace)
    with pytest.raises(RuntimeError, match="already exists"):
        git.create_task_workspace(config, "BUG-unique")
    assert (workspace.path / "README.md").read_text() == "valuable work"


def test_legacy_task_migration_preserves_unknown_fields_and_backup(sample_repo):
    store = TaskStore(sample_repo)
    task = store.create(TaskKind.BUG, "legacy", "local")
    path = store.path(task.id) / "task.json"
    raw = task.model_dump(mode="json")
    raw.pop("schema_version")
    raw.update(status="escalated", external_review_state="pending", future_extension={"keep": True})
    path.write_text(json.dumps(raw))
    loaded = store.load(task.id)
    store.save(loaded)
    first_backup = path.with_suffix(".v1.bak").read_bytes()
    store.save(store.load(task.id))
    assert path.with_suffix(".v1.bak").read_bytes() == first_backup
    assert json.loads(path.read_text())["future_extension"] == {"keep": True}
    future = TaskRecord.model_validate({**raw, "schema_version": 99})
    with pytest.raises(ValueError, match="newer"):
        store.save(future)


def test_plan_only_has_authority_and_zero_worker_calls(sample_repo, global_config):
    provider = Responses([])
    task = TaskRunner(global_config=global_config, provider=provider, manage_models=False).run(
        repository=sample_repo,
        kind=TaskKind.BUG,
        description="Repair",
        plan_only=True,
        direct_allowed_paths=["src/samplecalc/core.py"],
        direct_read_paths=["tests/test_core.py"],
    )
    assert task.status == TaskStatus.PLANNED and not provider.calls
    assert task.triage.work_units[0].allowed_paths == ["src/samplecalc/core.py"]
