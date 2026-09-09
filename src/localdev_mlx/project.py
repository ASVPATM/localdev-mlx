from __future__ import annotations

from pathlib import Path

from localdev_mlx.config import load_project_config, write_default_project_config
from localdev_mlx.git.edits import DEFAULT_SECRET_PATTERNS, _matches_any
from localdev_mlx.git.repository import GitError, GitRepository

AGENTS_TEMPLATE = """# Repository rules

- Current source, public interfaces, and tests are authoritative.
- Preserve unrelated work and never read or edit secrets or credentials.
- Make focused changes and add regression tests for behavior changes.
- Validation commands are in `.localdev/config.toml`; record only actual results.
- External coding work uses the user's project checkout; no LocalDev branch workflow is required.
- Commit or push only when the user requests it. LocalDev's optional local attempts use isolated worktrees.
- For external work, use the relevant `.localdev/runtime/sessions/SESSION-*.md` handoff.
  Plans are provisional; inspect current source and adapt them. No other generated docs are required.
"""


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
) -> tuple[Path, list[Path]]:
    """Prepare metadata only; the CLI also commits it. Never start a model or tests."""
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
    if not all(git.configured_identity()):
        raise GitError(
            "Configure Git user.name and user.email, then rerun init; LocalDev does not invent a commit identity"
        )
    config_path = root / ".localdev/config.toml"
    for path in (root / ".localdev", config_path, root / ".gitignore", root / "AGENTS.md"):
        if path.is_symlink():
            raise GitError(f"Refusing symlinked initialization metadata: {path}")
    if not initial and not git.is_clean():
        raise GitError(
            "Commit or stash existing changes before init; LocalDev will not commit unrelated work"
        )
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
            raise GitError(
                "Refusing to include credential-like files in the initial commit. Ignore or remove them, then retry: "
                + ", ".join(secrets)
            )
        git._run(root, ["add", "-A"])
        git._run(root, ["commit", "--allow-empty", "-m", "chore: initial project snapshot"])
    if switch_integration:
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
    agents = root / "AGENTS.md"
    if not agents.exists():
        agents.write_text(AGENTS_TEMPLATE)
        changed.append(agents)
    if commit and not git.is_clean():
        git._run(root, ["add", "--", *[str(p.relative_to(root)) for p in changed]])
        git._run(root, ["commit", "-m", "chore: prepare LocalDev session workflow"])
    return root, changed
