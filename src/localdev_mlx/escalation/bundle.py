from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from localdev_mlx.git.repository import GitRepository, TaskWorkspace
from localdev_mlx.schemas import TaskRecord
from localdev_mlx.tasks import TaskStore


def build_escalation_bundle(
    *,
    task: TaskRecord,
    store: TaskStore,
    repository: GitRepository,
    workspace: TaskWorkspace | None,
    error: str,
) -> Path:
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

    summary = f"""# External Review Handoff — {task.id}

## Task

- Kind: `{task.kind.value}`
- Description: {task.description}
- Status: `{task.status.value}`
- Worker: `{task.worker}`
- Repository: `{task.repository}`
- Integration branch: `{task.integration_branch or 'unknown'}`
- Task branch: `{task.task_branch or 'not created'}`
- Task worktree: `{task.task_worktree or 'not created'}`

## Why local automation stopped

{error}

## Local attempts

- Attempt count: `{task.attempts}`
- Local triage available: `{task.triage is not None}`

## Safety state

The task was not merged into `main`. Any partial implementation remains isolated in the task worktree/branch shown above.
Do not assume local-model changes are correct merely because some tests passed.

## Included artifacts

{chr(10).join(f'- `{name}`' for name in sorted(copied + ['CURRENT_DIFF.patch', 'WORKTREE_STATUS.txt']))}
"""
    (bundle / "SUMMARY.md").write_text(summary, encoding="utf-8")

    external_prompt = f"""# External Implementation / Review Request — {task.id}

Work in the Git repository at:

`{task.repository}`

The local automation attempted this task:

> {task.description}

It stopped for the following reason:

> {error}

Read these bundle files first:

1. `SUMMARY.md`
2. `task.json`
3. `triage.json` if present
4. all `*-tests.txt` files
5. all `review-*.json` files
6. `CURRENT_DIFF.patch`
7. `WORKTREE_STATUS.txt`

Then inspect the actual repository, `AGENTS.md`, canonical `docs/ai/` files, and the task branch/worktree if it still exists.

Your responsibilities:

- independently verify the diagnosis and assumptions;
- preserve the user's intended behavior;
- correct mistakes made by local models;
- implement or finish the task when safe;
- strengthen tests rather than merely making weak tests pass;
- review security, data-integrity, migration, concurrency, and architecture implications;
- run the full project validation commands;
- document exactly what changed and what remains unresolved;
- do not merge to `main` until the implementation is genuinely release-worthy.

The bundle is located at:

`{bundle}`
"""
    (bundle / "EXTERNAL_REVIEW_PROMPT.md").write_text(external_prompt, encoding="utf-8")
    manifest = {
        "task_id": task.id,
        "generated_at": datetime.now(UTC).isoformat(),
        "repository": task.repository,
        "integration_branch": task.integration_branch,
        "task_branch": task.task_branch,
        "task_worktree": task.task_worktree,
        "error": error,
        "files": sorted(path.name for path in bundle.iterdir()),
    }
    (bundle / "CONTEXT_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    return bundle


def export_bundle(bundle: Path, destination: Path) -> Path:
    destination = destination.resolve()
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(bundle, destination)
    return destination
