from __future__ import annotations

from pathlib import Path

from localdev_mlx.config import load_project_config, write_default_project_config
from localdev_mlx.git.edits import DEFAULT_SECRET_PATTERNS, _matches_any
from localdev_mlx.git.repository import GitError, GitRepository

FLOW_GUIDANCE_MARKER = "<!-- localdev-mlx:flow-integration -->"
FLOW_AGENT_GUIDANCE = """## LocalDev Flow integration

<!-- localdev-mlx:flow-integration -->

- Make the planned app Flow-ready without a production SDK or per-element registration.
- Check `localdev-mlx flow doctor --project .` in this checkout; reuse working adapters. If configuration is missing, `localdev-mlx flow init --project .` creates a template. Maintain `project-flow.toml` as the app evolves.
- Under `[project]`, set explicit `include` (source/config/assets), `checks` and one `preview` command (argv arrays). For web, set `preview_kind="web"`, bind `127.0.0.1:{port}`, and support deep routes; prefer served builds (no WebSocket/HMR proxy).
- Serve copied code; handle dependencies/build explicitly (`.venv`/`node_modules` aren't copied). Keep synthetic data/writes under `PROJECT_FLOW_DATA`, never real accounts/databases. Enable `preview_mutations` only for isolated test writes.
- Label key pages/dialogs/forms/actions; reuse stable IDs or `data-flow-id`. Mark personal non-input content `data-flow-private`, credentials `data-flow-secret`. Do not annotate everything.
- Personal-data Flow (`flow --personal`) requires a supported snapshot adapter. Never attach live storage; discard preview edits on Stop/Refresh, retain evidence. Validate on copies; live-data changes need approval.
- Use `localdev-mlx flow --project .` and Open test app to verify one page/dialog capture; stop your test services. Native/CLI use manual evidence; disclose unsupported/untested areas.
- No automatic model/browser downloads, model loading, extra agent documents or historical-session scans. Keep existing project workflows working.
"""

AGENTS_TEMPLATE = (
    """# Repository rules

- Current source, public interfaces, and tests are authoritative.
- Preserve unrelated work and never read or edit secrets or credentials.
- Make focused changes and add regression tests for behavior changes.
- Validation commands are in `.localdev/config.toml`; record only actual results.
- External coding work uses the user's project checkout; no LocalDev branch workflow is required.
- Commit or push only when the user requests it. LocalDev's optional local attempts use isolated worktrees.
- For external work, use the relevant `.localdev/runtime/sessions/SESSION-*.md` handoff.
  Plans are provisional; inspect current source and adapt them. No other generated docs are required.
"""
    + "\n"
    + FLOW_AGENT_GUIDANCE
)


def _prepare_agent_guidance(root: Path) -> Path | None:
    """Append once; preserve existing rules, including the active root override."""
    agents, override = root / "AGENTS.md", root / "AGENTS.override.md"
    if override.is_file() and override.read_bytes().strip():
        agents = override
    if not agents.exists():
        agents.write_text(AGENTS_TEMPLATE, encoding="utf-8")
        return agents
    original = agents.read_bytes()
    if FLOW_GUIDANCE_MARKER.encode() in original:
        return None
    newline = b"\r\n" if b"\r\n" in original else b"\n"
    separator = b"" if not original or original.endswith(newline * 2) else newline
    if original and not original.endswith(newline):
        separator += newline
    guidance = FLOW_AGENT_GUIDANCE.encode().replace(b"\n", newline)
    agents.write_bytes(original + separator + guidance)
    return agents


def initialize_project(
    repository: Path,
    *,
    quick_tests: list[str] | None = None,
    full_tests: list[str] | None = None,
    test_env: dict[str, str] | None = None,
    switch_integration: bool = True,
    base_branch: str | None = None,
    integration_branch: str = "ai/integration",
    commit: bool = False,
    prepare_commands: list[str] | None = None,
    notices: list[str] | None = None,
) -> tuple[Path, list[Path]]:
    """Prepare metadata; commit only when safe. Never start a model or tests."""
    notices = notices if notices is not None else []
    repository = repository.expanduser().resolve()
    repository.mkdir(parents=True, exist_ok=True)
    try:
        git = GitRepository(repository)
    except GitError:
        GitRepository._run(repository, ["init", "-b", base_branch or "main"])
        git = GitRepository(repository)
    if git.root != repository:
        raise GitError(f"Initialize the repository root explicitly: {git.root}")
    root = git.root
    initial = not git.has_commits()
    can_commit = True
    if not all(git.configured_identity()):
        can_commit = False
        notices.append("Automatic commits skipped: configure Git user.name and user.email.")
    config_path = root / ".localdev/config.toml"
    for path in (
        root / ".localdev",
        config_path,
        root / ".gitignore",
        root / "AGENTS.md",
        root / "AGENTS.override.md",
    ):
        if path.is_symlink():
            raise GitError(f"Refusing symlinked initialization metadata: {path}")
    if not initial and not git.is_clean():
        can_commit = False
        notices.append("Automatic commits skipped: existing changes and staging are preserved.")
    if initial and git._run(root, ["ls-files", "--stage"]).stdout:
        can_commit = False
        notices.append("Automatic commits skipped: existing staged files are preserved.")
    current_branch = git.current_branch()
    if not current_branch:
        raise GitError("Check out a named branch before initialization")
    if config_path.exists():
        existing = load_project_config(root)
        selected_base, integration_branch = existing.base_branch, existing.integration_branch
    else:
        selected_base = base_branch or current_branch
        if selected_base == integration_branch:
            selected_base = "main" if git.branch_exists("main") else current_branch
    if selected_base == integration_branch:
        raise GitError("Base and local integration branches must differ")
    if not initial and not git.branch_exists(selected_base):
        raise GitError(f"Base branch does not exist: {selected_base}")
    GitRepository._run(root, ["check-ref-format", "--branch", integration_branch])
    changed = []
    gitignore = root / ".gitignore"
    ignore_lines = [
        ".localdev/runtime/",
        ".project-flow/",
        ".DS_Store",
        ".venv/",
        "__pycache__/",
        "*.pyc",
        "*.egg-info/",
    ]
    text = gitignore.read_text() if gitignore.exists() else ""
    additions = [line for line in ignore_lines if line not in text.splitlines()]
    if additions:
        gitignore.write_text(
            text + ("\n" if text and not text.endswith("\n") else "") + "\n".join(additions) + "\n"
        )
        changed.append(gitignore)
    if initial:
        files = git._run(root, ["ls-files", "-co", "--exclude-standard", "-z"]).stdout.split("\0")
        secrets = sorted({p for p in files if p and _matches_any(p, DEFAULT_SECRET_PATTERNS)})
        if secrets:
            can_commit = False
            notices.append(
                "Automatic commits skipped: credential-like paths (including possible templates) were left untouched: "
                + ", ".join(secrets)
            )
    if initial and can_commit:
        git._run(root, ["add", "-A"])
        git._run(root, ["commit", "--allow-empty", "-m", "chore: initial project snapshot"])
    if switch_integration and can_commit:
        if not git.branch_exists(integration_branch):
            git._run(root, ["switch", "-c", integration_branch, selected_base])
        elif git.current_branch() != integration_branch:
            git._run(root, ["switch", integration_branch])
    if not config_path.exists():
        write_default_project_config(
            root,
            quick_tests=quick_tests,
            full_tests=full_tests,
            test_env=test_env,
            base_branch=selected_base,
            integration_branch=integration_branch,
            prepare_commands=prepare_commands,
        )
        changed.append(config_path)
    if agents := _prepare_agent_guidance(root):
        changed.append(agents)
    if commit and can_commit and changed and not git.is_clean():
        git._run(root, ["add", "--", *[str(p.relative_to(root)) for p in changed]])
        git._run(root, ["commit", "-m", "chore: prepare LocalDev session workflow"])
    return root, changed
