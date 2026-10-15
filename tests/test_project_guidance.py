from __future__ import annotations

import pytest
from typer.testing import CliRunner

from localdev_mlx.cli import app
from localdev_mlx.git.repository import GitRepository
from localdev_mlx.project import AGENTS_TEMPLATE, FLOW_AGENT_GUIDANCE, FLOW_GUIDANCE_MARKER
from localdev_mlx.sessions import SessionStore


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr("localdev_mlx.config.DATA_ROOT", tmp_path / "localdev-data")
    for key, value in {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "user.name",
        "GIT_CONFIG_VALUE_0": "Test User",
        "GIT_CONFIG_KEY_1": "user.email",
        "GIT_CONFIG_VALUE_1": "test@example.invalid",
    }.items():
        monkeypatch.setenv(key, value)
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("print('example')\n")
    return root


def init(root):
    result = CliRunner().invoke(app, ["init", str(root)])
    assert result.exit_code == 0, result.output
    return result


def test_print_guidance_has_no_project_or_git_side_effects(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Printing guidance must not initialize a project")

    monkeypatch.setattr("localdev_mlx.cli.initialize_project", forbidden)
    root = tmp_path / "not-created"
    result = CliRunner().invoke(app, ["init", str(root), "--print-agent-guidance"])
    assert result.exit_code == 0, result.output
    assert result.output == FLOW_AGENT_GUIDANCE
    assert not root.exists()


def test_init_adds_lightweight_build_guidance_without_starting_flow(project, monkeypatch):
    from localdev_mlx.flow import controller
    from localdev_mlx.models.manager import ModelManager

    def forbidden(*args, **kwargs):
        pytest.fail("init must not launch Flow, load a model or inspect the app")

    monkeypatch.setattr(controller, "start", forbidden)
    monkeypatch.setattr(controller, "doctor", forbidden)
    monkeypatch.setattr(ModelManager, "__init__", forbidden)
    result = init(project)
    assert (project / "AGENTS.md").read_text() == AGENTS_TEMPLATE
    assert len(FLOW_AGENT_GUIDANCE.split()) <= 200
    assert "Flow-ready build guidance" in result.output
    assert "preview still needs validation" in " ".join(result.output.split())
    assert not (project / "project-flow.toml").exists()  # Don't defeat future autodetection.
    assert not (project / ".project-flow").exists()
    assert not (project / "docs").exists()
    assert sorted(p.name for p in (project / ".localdev").iterdir()) == ["config.toml"]
    git = GitRepository(project)
    assert git.current_branch() == "main" and git.is_clean()
    assert (
        git._run(project, ["check-ignore", ".project-flow/evidence/capture.json"]).returncode == 0
    )
    first = git.resolve_ref(project, "HEAD")
    init(project)
    assert git.resolve_ref(project, "HEAD") == first


@pytest.mark.parametrize("original", [b"# My rules", b"# My rules\n", b"# My rules\r\n\r\n", b""])
def test_existing_agent_rules_are_preserved_and_guidance_is_appended_once(project, original):
    agents = project / "AGENTS.md"
    agents.write_bytes(original)
    init(project)
    added = agents.read_bytes()
    assert added.startswith(original)
    assert added.count(FLOW_GUIDANCE_MARKER.encode()) == 1
    if b"\r\n" in original:
        assert b"\n" not in added.replace(b"\r\n", b"")
    git = GitRepository(project)
    head = git.resolve_ref(project, "HEAD")
    init(project)
    assert agents.read_bytes() == added
    assert git.resolve_ref(project, "HEAD") == head and git.is_clean()


def test_active_agent_override_receives_guidance_without_changing_regular_rules(project):
    (project / "AGENTS.md").write_text("# Regular rules\n")
    override = project / "AGENTS.override.md"
    override.write_text("# Active rules\nUse the existing preview script.\n")
    init(project)
    assert (project / "AGENTS.md").read_text() == "# Regular rules\n"
    assert override.read_text().startswith("# Active rules\nUse the existing preview script.\n")
    assert FLOW_GUIDANCE_MARKER in override.read_text()
    before = override.read_bytes()
    init(project)
    assert override.read_bytes() == before


def test_empty_override_does_not_hide_regular_agent_guidance(project):
    override = project / "AGENTS.override.md"
    override.write_text("\n")
    init(project)
    assert (project / "AGENTS.md").read_text() == AGENTS_TEMPLATE
    assert override.read_text() == "\n"


def test_reinitializing_preserves_customized_flow_guidance_and_existing_adapter(project):
    agents = project / "AGENTS.md"
    original = "# My integration\n" + FLOW_GUIDANCE_MARKER + "\nUse our existing preview.\n"
    agents.write_text(original)
    adapter = project / "project-flow.toml"
    adapter.write_text('[project]\npreview_kind = "command"\n')
    init(project)
    assert agents.read_text() == original
    assert adapter.read_text() == '[project]\npreview_kind = "command"\n'


@pytest.mark.parametrize("name", ["AGENTS.md", "AGENTS.override.md"])
def test_init_never_appends_guidance_through_an_agent_symlink(project, name):
    target = project.parent / "outside.md"
    target.write_text("Leave me alone\n")
    (project / name).symlink_to(target)
    result = CliRunner().invoke(app, ["init", str(project)])
    assert result.exit_code == 1 and "symlink" in result.output
    assert target.read_text() == "Leave me alone\n"


def test_legacy_init_upgrades_only_metadata_and_preserves_branch_and_source(sample_repo):
    git = GitRepository(sample_repo)
    git._run(sample_repo, ["switch", "-c", "existing-work"])
    agents = sample_repo / "AGENTS.md"
    original = b"# Existing project instructions\nKeep the working server setup.\n"
    agents.write_bytes(original)
    git._run(sample_repo, ["add", "AGENTS.md"])
    git._run(sample_repo, ["commit", "-m", "customize instructions"])
    before = git.resolve_ref(sample_repo, "HEAD")
    source = (sample_repo / "src/samplecalc/core.py").read_bytes()
    init(sample_repo)
    assert agents.read_bytes().startswith(original)
    assert git.current_branch() == "existing-work" and git.is_clean()
    assert git._run(sample_repo, ["diff", "--name-only", before, "HEAD"]).stdout.splitlines() == [
        "AGENTS.md"
    ]
    assert (sample_repo / "src/samplecalc/core.py").read_bytes() == source


@pytest.mark.parametrize(
    "name", [".env.example", ".env", "config/.env.local", "private.key", "credentials.json"]
)
def test_credential_like_files_skip_commits_not_initialization(project, name, monkeypatch):
    from localdev_mlx.git.edits import EditError, safe_target

    protected = project / name
    protected.parent.mkdir(parents=True, exist_ok=True)
    original = b"TOKEN=synthetic-test-value\n"
    protected.write_bytes(original)
    result = init(project)
    assert "Automatic commits skipped" in result.output
    assert "Setup succeeded" in result.output
    git = GitRepository(project)
    assert not git.has_commits()
    assert git._run(project, ["ls-files", "--stage"]).stdout == ""
    assert protected.read_bytes() == original
    assert git.current_branch() == "main"
    assert (project / "AGENTS.md").read_text() == AGENTS_TEMPLATE
    with pytest.raises(EditError):
        safe_target(project, name, ())  # Setup does not weaken model edit/context rules.

    def forbidden(*args, **kwargs):
        pytest.fail("Deferred requests must not load models")

    monkeypatch.setattr("localdev_mlx.cli.load_global_config", forbidden)
    for command in ("plan", "bug", "feature", "tweak"):
        result = CliRunner().invoke(
            app, [command, "Keep the existing app working", "--repo", str(project)]
        )
        assert result.exit_code == 0, result.output
    store = SessionStore(project)
    session = store.current()
    assert session["baseline"]["commit"] is None
    assert len(session["entries"]) == 4
    text = store.handoff(session).read_text()
    assert "no commits yet" in text and "`None`" not in text
    assert "synthetic-test-value" not in text
    before = (project / "AGENTS.md").read_bytes()
    init(project)
    assert (project / "AGENTS.md").read_bytes() == before
    assert protected.read_bytes() == original and not git.has_commits()


def test_initialization_preserves_existing_staging_without_a_first_commit(project):
    git = GitRepository._run
    git(project, ["init", "-b", "user-work"])
    git(project, ["add", "app.py"])
    staged = git(project, ["ls-files", "--stage"]).stdout
    (project / "app.py").write_text("print('new unstaged work')\n")
    result = init(project)
    assert "existing staged files" in " ".join(result.output.split())
    assert not GitRepository(project).has_commits()
    assert GitRepository(project).current_branch() == "user-work"
    assert git(project, ["ls-files", "--stage"]).stdout == staged
    assert (project / "app.py").read_text() == "print('new unstaged work')\n"


def test_init_preserves_dirty_index_config_adapter_and_branch(sample_repo):
    git = GitRepository(sample_repo)
    git._run(sample_repo, ["switch", "-c", "user-work"])
    agents = sample_repo / "AGENTS.md"
    agents.write_text("# Staged user rules\n")
    git._run(sample_repo, ["add", "AGENTS.md"])
    agents.write_text("# Staged user rules\nAdditional unstaged rules.\n")
    (sample_repo / "README.md").write_text("Uncommitted app instructions\n")
    adapter = sample_repo / "project-flow.toml"
    adapter.write_text('[project]\npreview_kind = "command"\n')
    config = (sample_repo / ".localdev/config.toml").read_bytes()
    staged = git._run(sample_repo, ["diff", "--cached", "--binary"]).stdout
    head = git.resolve_ref(sample_repo, "HEAD")
    result = init(sample_repo)
    assert "existing changes" in result.output
    assert agents.read_text().startswith("# Staged user rules\nAdditional unstaged rules.\n")
    assert agents.read_text().count(FLOW_GUIDANCE_MARKER) == 1
    assert (sample_repo / "README.md").read_text() == "Uncommitted app instructions\n"
    assert adapter.read_text() == '[project]\npreview_kind = "command"\n'
    assert (sample_repo / ".localdev/config.toml").read_bytes() == config
    assert git._run(sample_repo, ["diff", "--cached", "--binary"]).stdout == staged
    assert git.resolve_ref(sample_repo, "HEAD") == head
    assert git.current_branch() == "user-work"
    init(sample_repo)
    assert git.resolve_ref(sample_repo, "HEAD") == head
    assert agents.read_text().count(FLOW_GUIDANCE_MARKER) == 1


def test_missing_git_identity_does_not_block_setup_or_sessions(project, monkeypatch):
    monkeypatch.setattr(GitRepository, "configured_identity", lambda self: (None, None))
    result = init(project)
    assert "user.name and user.email" in result.output
    assert not GitRepository(project).has_commits()
    result = CliRunner().invoke(app, ["session", "--new", "--repo", str(project)])
    assert result.exit_code == 0, result.output


def test_local_execution_still_requires_reviewed_commits(project, monkeypatch):
    (project / ".env.example").write_text("TOKEN=placeholder\n")
    init(project)
    monkeypatch.setattr(
        "localdev_mlx.cli.load_global_config", lambda: pytest.fail("Must not load models")
    )
    result = CliRunner().invoke(app, ["bug", "Fix the app", "--local", "--repo", str(project)])
    assert result.exit_code == 1 and "Commit or stash" in result.output
    assert not GitRepository(project).has_commits()
    store = SessionStore(project)
    assert "Fix the app" in store.handoff(store.current()).read_text()


def test_full_planning_can_clarify_an_uncommitted_project(project, global_config):
    from localdev_mlx.workflows.session_plan import SessionProposal, full_plan

    class Planner:
        def __init__(self):
            self.batches = iter([["Should existing application behavior be preserved?"], []])

        def complete_structured(self, **kwargs):
            if kwargs["response_model"] is SessionProposal:
                return SessionProposal(
                    proposal="Preserve the app and validate the requested change."
                )
            return kwargs["response_model"](questions=next(self.batches))

    (project / ".env.example").write_text("TOKEN=placeholder\n")
    init(project)
    store = SessionStore(project)
    with store.operation("plan", "Improve this existing app", "full") as (session, entry):
        full_plan(
            store,
            session,
            entry,
            config=global_config,
            provider=Planner(),
            manager=object(),
            manage_models=False,
            ask=lambda _: "Yes, preserve existing behavior.",
        )
    assert entry["state"] == "ready for external implementation"
    assert "Yes, preserve existing behavior." in store.handoff(session).read_text()
    assert not GitRepository(project).has_commits()
