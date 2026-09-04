from __future__ import annotations

from pathlib import Path

from localdev_mlx.config import write_default_project_config
from localdev_mlx.git.repository import GitError, GitRepository


AGENTS_TEMPLATE = """# AGENTS.md

## Authority order

1. Working code and passing tests
2. Public interfaces and `docs/ai/CONTRACTS.md`
3. `docs/ai/CURRENT_STATE.md`
4. `docs/ai/DECISIONS.md`
5. `docs/ai/MASTER_PLAN.md`
6. Historical handoffs and chat transcripts

## Permanent rules

- Read `.localdev/config.toml` and relevant `docs/ai/` files before changing code.
- Make the smallest change that satisfies the active task.
- Do not fabricate test results, APIs, files, domain facts, or external data.
- Do not read or edit secrets, credentials, key files, or `.env` files.
- Preserve stable contracts unless the task explicitly authorizes a contract change.
- Add or update tests for behavior changes.
- Run the configured quick tests during implementation and full tests before integration.
- Request external review for security, destructive migrations, high-risk concurrency, cryptography, and major architecture work.
- Do not merge local-model work directly into `main`; use `ai/integration` until independent review.
"""

CURRENT_STATE_TEMPLATE = """# Current State

Last updated: not yet populated

## Implemented

- LocalDev project metadata and AI collaboration structure.

## Not implemented

- Project-specific functionality has not yet been documented here.

## Validation

- Add only commands that were actually executed and their real results.
"""

DECISIONS_TEMPLATE = """# Decisions

Record only decisions that have actually been made.

## D-001 — Local-model integration branch

Local model work is accumulated on `ai/integration`. The `main` branch remains the reviewed release branch.
"""

TASK_QUEUE_TEMPLATE = """# Task Queue

This file is the human-readable queue. LocalDev runtime task state is stored outside Git and can be viewed with `localdev-mlx status`.
"""

CONTEXT_MAP_TEMPLATE = """# Context Map

- `AGENTS.md`: permanent repository rules
- `docs/ai/PROJECT_BRIEF.md`: product intent and requirements
- `docs/ai/ARCHITECTURE.md`: component boundaries and data flow
- `docs/ai/CONTRACTS.md`: stable interfaces and invariants
- `docs/ai/CURRENT_STATE.md`: what actually works now
- `docs/ai/DECISIONS.md`: accepted decisions and rationale
- `docs/ai/TASK_QUEUE.md`: planned work
- `docs/ai/handoffs/`: completed task summaries
- `docs/ai/reviews/`: portable external-review bundles when explicitly exported
"""

HANDOFF_TEMPLATE = """# TASK-ID — Handoff

## Goal

## Completed

## Files changed

## Tests run

## Exact results

## Decisions

## Deferred

## Risks and review points

## Recommended next task
"""


def _write_if_missing(path: Path, content: str) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def initialize_project(
    repository: Path,
    *,
    quick_tests: list[str] | None = None,
    full_tests: list[str] | None = None,
    switch_integration: bool = True,
    base_branch: str | None = None,
    integration_branch: str = "ai/integration",
) -> tuple[Path, list[Path]]:
    git = GitRepository(repository)
    root = git.root
    if not git.has_commits():
        raise GitError(
            "LocalDev requires an initial Git commit. Commit the current repository first, then run localdev-mlx init again."
        )
    if not git.is_clean(root):
        raise GitError("The repository must be clean before LocalDev initialization")
    current_branch = git.current_branch(root)
    selected_base = base_branch or current_branch
    if selected_base == integration_branch:
        selected_base = "main" if git.branch_exists("main") else current_branch
    if not git.branch_exists(selected_base):
        raise GitError(f"Base branch does not exist: {selected_base}")
    if switch_integration:
        if not git.branch_exists(integration_branch):
            if current_branch != selected_base:
                GitRepository._run(root, ["switch", selected_base])
            GitRepository._run(root, ["switch", "-c", integration_branch])
        elif git.current_branch(root) != integration_branch:
            GitRepository._run(root, ["switch", integration_branch])

    changed: list[Path] = []
    config_path = root / ".localdev" / "config.toml"
    if not config_path.exists():
        write_default_project_config(
            root,
            quick_tests=quick_tests,
            full_tests=full_tests,
            base_branch=selected_base,
            integration_branch=integration_branch,
        )
        changed.append(config_path)
    templates = {
        root / "AGENTS.md": AGENTS_TEMPLATE,
        root / "docs/ai/CURRENT_STATE.md": CURRENT_STATE_TEMPLATE,
        root / "docs/ai/DECISIONS.md": DECISIONS_TEMPLATE,
        root / "docs/ai/TASK_QUEUE.md": TASK_QUEUE_TEMPLATE,
        root / "docs/ai/CONTEXT_MAP.md": CONTEXT_MAP_TEMPLATE,
        root / "docs/ai/HANDOFF_TEMPLATE.md": HANDOFF_TEMPLATE,
    }
    for path, content in templates.items():
        if _write_if_missing(path, content):
            changed.append(path)
    (root / "docs/ai/handoffs").mkdir(parents=True, exist_ok=True)
    (root / "docs/ai/reviews").mkdir(parents=True, exist_ok=True)

    gitignore = root / ".gitignore"
    ignore_lines = [".localdev/runtime/", ".DS_Store"]
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    additions = [line for line in ignore_lines if line not in existing.splitlines()]
    if additions:
        suffix = "" if not existing or existing.endswith("\n") else "\n"
        gitignore.write_text(existing + suffix + "\n".join(additions) + "\n", encoding="utf-8")
        changed.append(gitignore)
    return root, changed


def finish_initialization(repository: Path) -> Path:
    git = GitRepository(repository)
    if not git.is_clean(git.root):
        raise GitError("Commit or discard project initialization changes before --finish")
    if not git.branch_exists("ai/integration"):
        GitRepository._run(git.root, ["switch", "-c", "ai/integration"])
    elif git.current_branch(git.root) != "ai/integration":
        GitRepository._run(git.root, ["switch", "ai/integration"])
    return git.root
