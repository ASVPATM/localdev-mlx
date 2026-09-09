from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from localdev_mlx.cli import app
from localdev_mlx.config import load_project_config
from localdev_mlx.git import GitRepository
from localdev_mlx.project import initialize_project
from localdev_mlx.providers import MockStructuredProvider
from localdev_mlx.schemas import ImplementationResult, TaskKind, TaskStatus
from localdev_mlx.sessions import SessionBusy, SessionStore
from localdev_mlx.tasks import TaskStore
from localdev_mlx.workflows.session_plan import SessionProposal, full_plan
from localdev_mlx.workflows.task_runner import TaskRunner

cli = CliRunner()


def test_exactly_eight_public_commands_without_subgroups():
    commands = get_command(app).commands
    assert set(commands) == {
        "init",
        "configure",
        "model",
        "plan",
        "bug",
        "feature",
        "tweak",
        "session",
    }
    assert all(not hasattr(command, "commands") for command in commands.values())


def test_default_deferral_never_loads_config_models_or_changes_code(sample_repo, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("No models/configuration should be needed")

    monkeypatch.setattr("localdev_mlx.cli.load_global_config", forbidden)
    git = GitRepository(sample_repo)
    head = git.resolve_ref(git.root, "HEAD")
    for kind in ("bug", "feature", "tweak"):
        result = cli.invoke(
            app, [kind, f"A {kind} with expected behavior", "--repo", str(sample_repo)]
        )
        assert result.exit_code == 0, result.stdout
    store = SessionStore(sample_repo)
    current = store.current()
    assert len(current["entries"]) == 3
    assert git.is_clean() and git.resolve_ref(git.root, "HEAD") == head
    assert len(list(store.visible.glob("*.md"))) == 1
    assert not (sample_repo / "docs/ai").exists()
    assert TaskStore(sample_repo).list() == []
    handoff = store.handoff(current).read_text()
    assert len(handoff) < 4000
    assert "No application code changed" in handoff


def test_ending_starts_a_new_handoff_without_rewriting_old_one(sample_repo):
    store = SessionStore(sample_repo)
    with store.operation("bug", "First request", "handoff") as (_, entry):
        entry["state"] = "deferred"
    first = store.end()
    old_text = store.handoff(first).read_bytes()
    with store.operation("feature", "Second request", "handoff") as (second, entry):
        entry["state"] = "deferred"
    assert second["number"] == first["number"] + 1
    assert store.handoff(first).read_bytes() == old_text
    assert "First request" not in store.handoff(second).read_text()
    assert store.current()["number"] == second["number"]


def test_new_runtime_does_not_overwrite_existing_handoff(sample_repo, tmp_path, monkeypatch):
    store = SessionStore(sample_repo)
    first = store.start()
    store.end()
    original = store.handoff(first).read_bytes()
    monkeypatch.setattr("localdev_mlx.config.DATA_ROOT", tmp_path / "fresh-runtime")
    moved = SessionStore(sample_repo)
    assert moved.start()["number"] == 2
    assert store.handoff(first).read_bytes() == original


def test_cancellation_targets_only_the_observed_operation(sample_repo):
    store = SessionStore(sample_repo)
    with store.operation("plan", "First", "full") as (first, first_entry):
        store.cancel()
        old_marker = store.cancel_path(first, first_entry)
        assert old_marker.exists()
    store.end()
    with store.operation("plan", "Second", "full") as (second, second_entry):
        assert store.cancel_path(second, second_entry) != old_marker
        assert not store.cancel_path(second, second_entry).exists()


def test_interactive_session_uses_same_parser_and_closes_on_eof(sample_repo):
    result = cli.invoke(
        app,
        ["--repo", str(sample_repo)],
        input='bug "First problem"\nfeature "Second request" --escalate\nsession --show\n',
    )
    assert result.exit_code == 0, result.stdout
    store = SessionStore(sample_repo)
    current = store.current()
    assert current["ended"]
    assert [e["description"] for e in current["entries"]] == ["First problem", "Second request"]
    result = cli.invoke(app, ["--repo", str(sample_repo)], input='tweak "Next session"\n')
    assert result.exit_code == 0, result.stdout
    assert store.current()["number"] == 2


def test_interactive_parser_error_does_not_drop_session(sample_repo):
    result = cli.invoke(
        app, ["--repo", str(sample_repo)], input='not-a-command\nbug "Still captured"\n'
    )
    assert result.exit_code == 0, result.stdout
    assert SessionStore(sample_repo).current()["entries"][0]["description"] == "Still captured"


def test_session_end_and_new_also_work_inside_prompt(sample_repo):
    result = cli.invoke(
        app,
        ["--repo", str(sample_repo)],
        input='bug "First"\nsession --new\ntweak "Second"\nsession --end\nbug "Must not execute"\n',
    )
    assert result.exit_code == 0, result.stdout
    store = SessionStore(sample_repo)
    current = store.current()
    assert current["number"] == 2 and current["ended"]
    assert [e["description"] for e in current["entries"]] == ["Second"]
    assert len(list(store.visible.glob("*.md"))) == 2


def test_sequential_lock_blocks_other_process_but_not_status(sample_repo):
    from localdev_mlx import config

    store = SessionStore(sample_repo)
    with store.operation("plan", "Active work", "full"):
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "localdev_mlx",
                "bug",
                "Do not overlap",
                "--repo",
                str(sample_repo),
            ],
            env={**os.environ, "LOCALDEV_MLX_HOME": str(config.DATA_ROOT)},
            text=True,
            capture_output=True,
        )
        assert result.returncode != 0
        assert "already running" in result.stdout
        result = cli.invoke(app, ["session", "--repo", str(sample_repo)])
        assert result.exit_code == 0 and "running" in result.stdout
        with pytest.raises(SessionBusy):
            SessionStore(sample_repo).end()
    assert len(store.current()["entries"]) == 1


def test_interactive_session_cannot_be_rotated_by_another_process(sample_repo):
    store = SessionStore(sample_repo)
    with store.shell():
        store.start(new=True)
        with pytest.raises(SessionBusy, match="interactive"):
            SessionStore(sample_repo).start(new=True)
        with pytest.raises(SessionBusy, match="interactive"):
            SessionStore(sample_repo).end()
        with SessionStore(sample_repo).operation("bug", "Joined from shell", "handoff"):
            pass
        store.end()


def test_invalid_flag_combinations_do_not_create_a_session(sample_repo):
    for args in (["--local", "--escalate"], ["--allow", "app.py"], ["--local", "--read", "app.py"]):
        result = cli.invoke(app, ["bug", "request", "--repo", str(sample_repo), *args])
        assert result.exit_code != 0
    assert SessionStore(sample_repo).current() is None


def test_light_plan_preserves_request_without_calling_model(sample_repo, monkeypatch):
    monkeypatch.setattr(
        "localdev_mlx.cli.load_global_config", lambda: pytest.fail("No config required")
    )
    result = cli.invoke(app, ["plan", "A small offline reading app", "--repo", str(sample_repo)])
    assert result.exit_code == 0, result.stdout
    store = SessionStore(sample_repo)
    text = store.handoff(store.current()).read_text()
    assert "offline reading app" in text and "provisional" in text
    assert GitRepository(sample_repo).is_clean()


def test_full_plan_is_bounded_two_passes_in_one_document(sample_repo, global_config):
    class Planner:
        calls = []

        def complete_structured(self, **kwargs):
            self.calls.append(kwargs)
            return SessionProposal(
                proposal="Propose a small module; verify with tests. Alternative: retain the existing interface."
            )

    provider = Planner()
    store = SessionStore(sample_repo)
    with store.operation("plan", "Improve the calculator", "full") as (current, entry):
        full_plan(
            store, current, entry, config=global_config, provider=provider, manage_models=False
        )
    assert len(provider.calls) == 2
    assert provider.calls[0]["profile"].max_tokens <= 1800
    assert not provider.calls[0]["profile"].enable_thinking
    assert "Critique and improve" in provider.calls[1]["user_prompt"]
    assert "flexible" in provider.calls[0]["system_prompt"]
    assert len(list(store.visible.glob("*.md"))) == 1
    assert "Provisional local proposal" in store.handoff(current).read_text()
    assert GitRepository(sample_repo).is_clean()


def test_failed_second_plan_pass_retains_first_draft(sample_repo, global_config):
    class Planner:
        calls = 0

        def complete_structured(self, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("planner unavailable")
            return SessionProposal(proposal="Initial unverified proposal")

    store = SessionStore(sample_repo)
    with pytest.raises(RuntimeError, match="unavailable"):
        with store.operation("plan", "Goal", "full") as (current, entry):
            full_plan(
                store, current, entry, config=global_config, provider=Planner(), manage_models=False
            )
    text = store.handoff(current).read_text()
    assert "Initial unverified proposal" in text and "failed" in text and "unavailable" in text


def test_local_success_records_paths_commits_and_tests_in_same_handoff(sample_repo, global_config):
    store = SessionStore(sample_repo)
    with store.operation("feature", "Later external request", "handoff"):
        pass
    with store.operation("bug", "Repair subtraction", "local") as (current, entry):
        task = TaskRunner(
            global_config=global_config,
            provider=MockStructuredProvider(),
            manage_models=False,
            on_update=lambda task: store.record_task(current, entry, task),
        ).run(repository=sample_repo, kind=TaskKind.BUG, description="Repair subtraction")
        entry["state"] = task.status.value
    assert task.status == TaskStatus.INTEGRATED, task.events
    text = store.handoff(current).read_text()
    assert "Later external request" in text and "Repair subtraction" in text
    assert "src/samplecalc/core.py" in text and task.final_commit in text
    assert "exit 1" in text and "exit 0" in text
    assert (sample_repo / entry["patch"]).exists()
    assert not (sample_repo / "docs/ai").exists()
    assert GitRepository(sample_repo).is_clean()


def test_cli_local_opt_in_runs_existing_gates_and_handoff(sample_repo, global_config, monkeypatch):
    monkeypatch.setattr("localdev_mlx.cli.load_global_config", lambda: global_config)
    monkeypatch.setattr("localdev_mlx.cli.MLXOpenAIProvider", MockStructuredProvider)
    monkeypatch.setattr(
        "localdev_mlx.cli.TaskRunner", lambda **kwargs: TaskRunner(**kwargs, manage_models=False)
    )
    result = cli.invoke(
        app,
        [
            "bug",
            "Repair subtraction",
            "--local",
            "--allow",
            "src/samplecalc/core.py",
            "--read",
            "tests/test_core.py",
            "--repo",
            str(sample_repo),
        ],
    )
    assert result.exit_code == 0, result.stdout
    store = SessionStore(sample_repo)
    entry = store.current()["entries"][0]
    assert entry["state"] == "integrated"
    assert entry["files"] == ["src/samplecalc/core.py"]
    assert "[BUG-" not in result.stdout


def test_local_refuses_dirty_checkout_but_retains_request(sample_repo, monkeypatch):
    monkeypatch.setattr("localdev_mlx.cli.load_global_config", lambda: pytest.fail("Must not load"))
    (sample_repo / "README.md").write_text("Uncommitted user edits\n")
    result = cli.invoke(app, ["bug", "Local request", "--local", "--repo", str(sample_repo)])
    assert result.exit_code != 0
    assert "Commit or stash" in result.stdout
    store = SessionStore(sample_repo)
    assert "Local request" in store.handoff(store.current()).read_text()
    assert (sample_repo / "README.md").read_text() == "Uncommitted user edits\n"


def test_local_adopts_fast_forward_external_base_without_frontier_commands(
    sample_repo, global_config, monkeypatch
):
    git = GitRepository(sample_repo)
    integration = git.resolve_ref(sample_repo, "HEAD")
    git._run(sample_repo, ["switch", "main"])
    git._run(sample_repo, ["merge", "--ff-only", integration])
    (sample_repo / "README.md").write_text("External reviewed update\n")
    base = git.commit_all(sample_repo, "external update")
    monkeypatch.setattr("localdev_mlx.cli.load_global_config", lambda: global_config)
    monkeypatch.setattr("localdev_mlx.cli.MLXOpenAIProvider", MockStructuredProvider)
    monkeypatch.setattr(
        "localdev_mlx.cli.TaskRunner", lambda **kwargs: TaskRunner(**kwargs, manage_models=False)
    )
    result = cli.invoke(app, ["bug", "Repair subtraction", "--local", "--repo", str(sample_repo)])
    assert result.exit_code == 0, result.stdout
    entry = SessionStore(sample_repo).current()["entries"][0]
    assert entry["base_commit"] == base
    assert git.resolve_ref(sample_repo, "main") == base
    assert "external base" in entry["note"]


def test_local_refuses_diverged_external_base_without_changing_branches(sample_repo, monkeypatch):
    git = GitRepository(sample_repo)
    old_integration = git.resolve_ref(sample_repo, "HEAD")
    git._run(sample_repo, ["switch", "main"])
    (sample_repo / "README.md").write_text("Diverged external update\n")
    base = git.commit_all(sample_repo, "external update")
    git._run(sample_repo, ["switch", "ai/integration"])
    monkeypatch.setattr("localdev_mlx.cli.load_global_config", lambda: pytest.fail("Must not load"))
    result = cli.invoke(app, ["bug", "Repair subtraction", "--local", "--repo", str(sample_repo)])
    assert result.exit_code != 0 and "diverged" in result.stdout
    assert git.resolve_ref(sample_repo, "main") == base
    assert git.resolve_ref(sample_repo, "ai/integration") == old_integration


def test_failed_local_edit_has_patch_and_does_not_integrate(sample_repo, global_config):
    class Broken(MockStructuredProvider):
        def complete_structured(self, **kwargs):
            result = super().complete_structured(**kwargs)
            if isinstance(result, ImplementationResult):
                result.edits[0].new_text = "return 999"
            return result

    store = SessionStore(sample_repo)
    before = GitRepository(sample_repo).resolve_ref(sample_repo, "HEAD")
    with store.operation("bug", "Repair subtraction", "local") as (current, entry):
        task = TaskRunner(
            global_config=global_config,
            provider=Broken(),
            manage_models=False,
            max_attempts=1,
            on_update=lambda task: store.record_task(current, entry, task),
        ).run(repository=sample_repo, kind=TaskKind.BUG, description="Repair subtraction")
        entry["state"] = task.status.value
    assert task.status == TaskStatus.ESCALATED, task.events
    assert GitRepository(sample_repo).resolve_ref(sample_repo, "HEAD") == before
    text = store.handoff(current).read_text()
    assert "src/samplecalc/core.py" in text and "exit 1" in text
    assert "999" in (sample_repo / entry["patch"]).read_text()
    assert task.visible_review_path is None  # No duplicate per-task Markdown bundles.
    assert not (sample_repo / ".localdev/runtime/reviews").exists()


def test_crash_recovery_keeps_request_and_marks_interrupted(sample_repo):
    store = SessionStore(sample_repo)
    current = store.start()
    current["entries"].append(
        {
            "number": 1,
            "kind": "bug",
            "description": "Never lose this",
            "mode": "local",
            "state": "running",
            "before": store.snapshot(),
        }
    )
    store.save(current)
    with store.operation("tweak", "Next request", "handoff") as (recovered, _):
        assert recovered["entries"][0]["state"] == "interrupted"
    assert "Never lose this" in store.handoff(recovered).read_text()


def test_status_recovers_stale_running_state_without_a_task_id(sample_repo):
    store = SessionStore(sample_repo)
    current = store.start()
    current["entries"].append(
        {
            "number": 1,
            "kind": "plan",
            "description": "Interrupted plan",
            "mode": "full",
            "state": "running",
            "before": store.snapshot(),
        }
    )
    store.save(current)
    result = cli.invoke(app, ["session", "--repo", str(sample_repo)])
    assert result.exit_code == 0 and "interrupted" in result.stdout
    assert store.current()["entries"][0]["state"] == "interrupted"


def test_project_symlink_cannot_redirect_handoff(sample_repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (sample_repo / ".localdev/runtime").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        SessionStore(sample_repo)
    assert list(outside.iterdir()) == []


def test_init_creates_initial_commit_and_minimal_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Test User")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "user.email")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "test@example.invalid")
    root = tmp_path / "new-project"
    root.mkdir()
    (root / "app.py").write_text("print('hello')\n")
    result = cli.invoke(
        app, ["init", str(root), "--quick-test", "python3 app.py", "--prepare", "python3 --version"]
    )
    assert result.exit_code == 0, result.stdout
    git = GitRepository(root)
    assert git.has_commits() and git.is_clean()
    assert git.current_branch() == "ai/integration"
    assert not (root / "docs").exists()
    assert load_project_config(root).prepare.commands == ("python3 --version",)
    assert load_project_config(root).tests.quick == ("python3 app.py",)
    assert git._run(root, ["show", "main:app.py"]).stdout == "print('hello')\n"
    head = git.resolve_ref(root, "HEAD")
    assert cli.invoke(app, ["init", str(root)]).exit_code == 0
    assert git.resolve_ref(root, "HEAD") == head


def test_init_refuses_credential_snapshot(tmp_path):
    root = tmp_path / "secret-project"
    root.mkdir()
    git = GitRepository._run
    git(root, ["init", "-b", "main"])
    git(root, ["config", "user.name", "Test User"])
    git(root, ["config", "user.email", "test@example.invalid"])
    (root / ".env").write_text("TOKEN=private\n")
    with pytest.raises(RuntimeError, match="credential"):
        initialize_project(root, commit=True)
    assert not GitRepository(root).has_commits()
    assert (root / ".env").read_text() == "TOKEN=private\n"


def test_init_preserves_existing_user_changes(sample_repo):
    before = GitRepository(sample_repo).resolve_ref(sample_repo, "HEAD")
    (sample_repo / "README.md").write_text("User changes\n")
    with pytest.raises(RuntimeError, match="unrelated"):
        initialize_project(sample_repo, commit=True)
    assert (sample_repo / "README.md").read_text() == "User changes\n"
    assert GitRepository(sample_repo).resolve_ref(sample_repo, "HEAD") == before


def test_session_state_has_no_embedded_test_logs(sample_repo):
    store = SessionStore(sample_repo)
    with store.operation("bug", "Small request", "handoff") as (current, _):
        pass
    data = json.loads(store._path(current["number"]).read_text())
    assert data["schema_version"] == 1
    assert "stdout" not in store.render(current)
