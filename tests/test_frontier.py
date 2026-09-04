from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from localdev_mlx.cli import app
from localdev_mlx.config import load_project_config
from localdev_mlx.git import GitRepository
from localdev_mlx.schemas import ExternalReviewState, TaskKind, TaskStatus
from localdev_mlx.tasks import TaskStore
from localdev_mlx.workflows.frontier import (
    build_frontier_batch,
    defer_task,
    resolve_frontier,
    supersede_frontier,
    sync_frontier_ref,
)

runner = CliRunner()


def test_defer_creates_open_review_bundle_without_models(sample_repo: Path) -> None:
    task = defer_task(
        repository=sample_repo,
        kind=TaskKind.BUG,
        description="A difficult issue to fix later.",
    )

    assert task.status == TaskStatus.DEFERRED
    assert task.external_review_state == ExternalReviewState.PENDING
    assert task.escalation_path is not None
    assert task.visible_review_path is not None
    assert (Path(task.escalation_path) / "EXTERNAL_REVIEW_PROMPT.md").exists()
    assert (Path(task.visible_review_path) / "SUMMARY.md").exists()
    assert TaskStore(sample_repo).open_external()[0].id == task.id


def test_frontier_batch_tracks_latest_head_and_resolves_tasks(sample_repo: Path) -> None:
    first = defer_task(
        repository=sample_repo,
        kind=TaskKind.BUG,
        description="First deferred issue.",
    )
    second = defer_task(
        repository=sample_repo,
        kind=TaskKind.FEATURE,
        description="Second deferred issue.",
    )

    batch = build_frontier_batch(repository=sample_repo)
    visible = Path(batch.visible_path or "")
    assert set(batch.task_ids) == {first.id, second.id}
    assert (visible / "FRONTIER_BATCH_PROMPT.md").exists()
    assert (visible / "OPEN_EXTERNAL_TASKS.md").exists()
    assert TaskStore(sample_repo).load(first.id).external_review_state == ExternalReviewState.BUNDLED

    git = GitRepository(sample_repo)
    config = load_project_config(sample_repo)
    integration = git.ensure_integration_worktree(config)
    (integration / "FRONTIER_FIX.md").write_text("resolved\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(integration), "add", "FRONTIER_FIX.md"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(integration), "commit", "-m", "fix: external batch"],
        check=True,
        capture_output=True,
    )
    commit = git.resolve_ref(integration, "HEAD")

    resolved_commit, tasks, resolved_batch = resolve_frontier(
        repository=sample_repo,
        batch_id=batch.id,
        commit="HEAD",
    )

    assert resolved_commit == commit
    assert {task.id for task in tasks} == {first.id, second.id}
    assert all(task.external_review_state == ExternalReviewState.RESOLVED for task in tasks)
    assert resolved_batch is not None
    assert resolved_batch.resolved_commit == commit
    assert TaskStore(sample_repo).open_external() == []


def test_cli_frontier_only_records_issue_without_global_config(sample_repo: Path) -> None:
    result = runner.invoke(
        app,
        [
            "bug",
            "Record this for later.",
            "--repo",
            str(sample_repo),
            "--frontier-only",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "Local outcome: deferred" in result.stdout
    assert "Frontier state: pending" in result.stdout


def test_guide_and_frontier_help_are_discoverable() -> None:
    guide_result = runner.invoke(app, ["guide"])
    assert guide_result.exit_code == 0
    assert "frontier bundle" in guide_result.stdout

    help_result = runner.invoke(app, ["frontier", "--help"])
    assert help_result.exit_code == 0
    assert "bundle" in help_result.stdout
    assert "resolve" in help_result.stdout
    assert "supersede" in help_result.stdout
    assert "sync" in help_result.stdout


def test_new_local_task_starts_from_latest_frontier_commit(
    sample_repo: Path,
    global_config,
) -> None:
    from localdev_mlx.providers.mock import MockStructuredProvider
    from localdev_mlx.workflows.task_runner import TaskRunner

    git = GitRepository(sample_repo)
    config = load_project_config(sample_repo)
    integration = git.ensure_integration_worktree(config)
    (integration / "README.md").write_text(
        "# Sample\n\nFrontier-reviewed context.\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(integration), "add", "README.md"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(integration), "commit", "-m", "docs: frontier update"],
        check=True,
        capture_output=True,
    )
    frontier_commit = git.resolve_ref(integration, "HEAD")

    task = TaskRunner(
        global_config=global_config,
        provider=MockStructuredProvider(),
        manage_models=False,
        depth="fast",
    ).run(
        repository=sample_repo,
        kind=TaskKind.BUG,
        description="Fix subtraction after frontier work.",
    )

    assert task.status == TaskStatus.INTEGRATED
    assert task.base_commit == frontier_commit
    assert "Frontier-reviewed context" in (sample_repo / "README.md").read_text(
        encoding="utf-8"
    )


def test_supersede_closes_duplicate_external_item(sample_repo: Path) -> None:
    task = defer_task(
        repository=sample_repo,
        kind=TaskKind.BUG,
        description="Duplicate diagnostic escalation.",
    )

    commit, updated = supersede_frontier(
        repository=sample_repo,
        task_ids=[task.id],
        commit="HEAD",
        note="A later verified task replaced this report.",
    )

    assert commit is not None
    assert updated[0].external_review_state == ExternalReviewState.SUPERSEDED
    assert TaskStore(sample_repo).open_external() == []


def test_sync_fast_forwards_integration_to_external_branch(sample_repo: Path) -> None:
    git = GitRepository(sample_repo)
    config = load_project_config(sample_repo)
    integration = git.ensure_integration_worktree(config)
    before = git.resolve_ref(integration, "HEAD")

    subprocess.run(
        ["git", "-C", str(sample_repo), "switch", "main"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(sample_repo), "merge", "--ff-only", config.integration_branch],
        check=True,
        capture_output=True,
    )
    (sample_repo / "FRONTIER_MAIN_FIX.md").write_text("frontier fix\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(sample_repo), "add", "FRONTIER_MAIN_FIX.md"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(sample_repo), "commit", "-m", "fix: frontier on main"],
        check=True,
        capture_output=True,
    )

    old, new = sync_frontier_ref(repository=sample_repo, source_ref="main")

    assert old == before
    assert new == git.resolve_ref(sample_repo, "main")
    assert (integration / "FRONTIER_MAIN_FIX.md").exists()


def test_cli_defer_file_records_multiple_issues(sample_repo: Path, tmp_path: Path) -> None:
    issue_file = tmp_path / "frontier-issues.json"
    issue_file.write_text(
        """[
  {"kind": "bug", "description": "First issue"},
  {"kind": "feature", "description": "Second issue", "reason": "Needs API review"}
]""",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["frontier", "defer-file", str(issue_file), "--repo", str(sample_repo)],
    )

    assert result.exit_code == 0, result.stdout
    open_tasks = TaskStore(sample_repo).open_external()
    assert len(open_tasks) == 2
    assert "Create one combined prompt" in result.stdout
