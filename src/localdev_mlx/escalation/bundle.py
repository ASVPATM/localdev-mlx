from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from localdev_mlx.git.repository import GitRepository, TaskWorkspace
from localdev_mlx.schemas import ExternalReviewCategory, TaskRecord, TaskStatus
from localdev_mlx.tasks import TaskStore


def _copy_bundle(source: Path, destination: Path) -> Path:
    destination = destination.resolve()
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)
    return destination


def visible_review_path(repository: Path, task_id: str) -> Path:
    return repository.resolve() / ".localdev" / "runtime" / "reviews" / task_id


def build_escalation_bundle(
    *,
    task: TaskRecord,
    store: TaskStore,
    repository: GitRepository,
    workspace: TaskWorkspace | None,
    error: str,
    category: ExternalReviewCategory,
) -> tuple[Path, Path]:
    """Build the canonical external bundle and a project-visible ignored mirror."""
    source = store.path(task.id)
    bundle = source / "external_bundle"
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True, exist_ok=True)

    diff = ""
    status = ""
    if workspace and workspace.path.exists():
        diff = repository.diff(workspace.path)
        status = repository.status_porcelain(workspace.path)
    (bundle / "CURRENT_DIFF.patch").write_text(diff, encoding="utf-8")
    (bundle / "WORKTREE_STATUS.txt").write_text(status, encoding="utf-8")

    copied: list[str] = []
    for path in source.iterdir():
        if path == bundle or path.is_dir():
            continue
        target = bundle / path.name
        shutil.copy2(path, target)
        copied.append(path.name)

    deliberate = task.status == TaskStatus.DEFERRED
    stop_heading = (
        "Why this was deliberately deferred"
        if deliberate
        else "Why local automation stopped"
    )
    safety_text = (
        "No local implementation was attempted. This item is waiting in the external-review "
        "backlog."
        if deliberate
        else "The task was not integrated. Any partial implementation remains isolated in the "
        "task worktree/branch shown below."
    )

    current_integration_commit = "unknown"
    if task.integration_branch:
        try:
            current_integration_commit = repository.resolve_ref(
                repository.root,
                task.integration_branch,
            )
        except Exception:
            pass

    summary = f"""# External Review Handoff — {task.id}

## Task

- Kind: `{task.kind.value}`
- Description: {task.description}
- Local outcome: `{task.status.value}`
- External category: `{category.value}`
- Worker: `{task.worker}`
- Repository: `{task.repository}`
- Integration branch: `{task.integration_branch or 'unknown'}`
- Task base commit: `{task.base_commit or 'unknown'}`
- Current integration commit at bundle creation: `{current_integration_commit}`
- Task branch: `{task.task_branch or 'not created'}`
- Task worktree: `{task.task_worktree or 'not created'}`

## {stop_heading}

{error}

## Local attempts

- Attempt count: `{task.attempts}`
- Local triage available: `{task.triage is not None}`

## Safety state

{safety_text}

This bundle is historical context. Other tasks may be integrated after it is created. An external reviewer must inspect the latest integration branch before applying or recreating any patch.

## Included artifacts

{chr(10).join(f'- `{name}`' for name in sorted(copied + ['CURRENT_DIFF.patch', 'WORKTREE_STATUS.txt']))}
"""
    (bundle / "SUMMARY.md").write_text(summary, encoding="utf-8")

    external_prompt = f"""# External Implementation / Review Request — {task.id}

Work in the Git repository at:

`{task.repository}`

Start from the latest commit on:

`{task.integration_branch or 'ai/integration'}`

The issue is:

> {task.description}

Local outcome: `{task.status.value}`
External category: `{category.value}`

The local workflow stopped or deferred the item for this reason:

> {error}

Read these bundle files first:

1. `SUMMARY.md`
2. `task.json`
3. `triage.json` if present
4. all `*-tests.txt` files
5. all `review-*.json` files
6. `CURRENT_DIFF.patch`
7. `WORKTREE_STATUS.txt`

Then inspect the latest repository state, `AGENTS.md`, canonical `docs/ai/` files, and relevant source/tests. Treat bundle patches as historical evidence, not as changes that must be applied blindly.

Your responsibilities:

- independently verify the diagnosis and assumptions;
- preserve later integrated work and current contracts;
- implement or finish the issue when safe;
- correct mistakes made by local models;
- strengthen tests rather than merely making weak tests pass;
- run the project’s full configured validation;
- commit the finished work to the integration branch;
- create an accurate handoff under `docs/ai/handoffs/`;
- after the commit, mark this item resolved with:

  `localdev-mlx frontier resolve --task {task.id} --commit HEAD --repo {task.repository}`

Do not merge to the release branch until the project has received the intended independent release review.

Canonical bundle:

`{bundle}`
"""
    (bundle / "EXTERNAL_REVIEW_PROMPT.md").write_text(
        external_prompt,
        encoding="utf-8",
    )
    manifest = {
        "task_id": task.id,
        "generated_at": datetime.now(UTC).isoformat(),
        "repository": task.repository,
        "integration_branch": task.integration_branch,
        "task_base_commit": task.base_commit,
        "current_integration_commit": current_integration_commit,
        "task_branch": task.task_branch,
        "task_worktree": task.task_worktree,
        "category": category.value,
        "reason": error,
        "files": sorted(path.name for path in bundle.iterdir()),
    }
    (bundle / "CONTEXT_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )

    visible = _copy_bundle(bundle, visible_review_path(repository.root, task.id))
    return bundle, visible


def export_bundle(bundle: Path, destination: Path) -> Path:
    return _copy_bundle(bundle, destination)
