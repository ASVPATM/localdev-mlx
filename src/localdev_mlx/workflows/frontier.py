from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

from localdev_mlx.config import load_project_config, project_state_dir
from localdev_mlx.escalation.bundle import build_escalation_bundle
from localdev_mlx.git import GitRepository
from localdev_mlx.schemas import (
    ExternalReviewCategory,
    ExternalReviewState,
    FrontierBatchRecord,
    FrontierBatchStatus,
    TaskKind,
    TaskPhase,
    TaskRecord,
    TaskStatus,
)
from localdev_mlx.tasks import TaskStore


class FrontierStore:
    def __init__(self, repository: Path) -> None:
        self.repository = repository.resolve()
        self.root = project_state_dir(self.repository) / "frontier-batches"
        self.root.mkdir(parents=True, exist_ok=True)

    def new_id(self) -> str:
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        candidate = f"FRONTIER-{stamp}"
        counter = 1
        while (self.root / candidate).exists():
            counter += 1
            candidate = f"FRONTIER-{stamp}-{counter}"
        return candidate

    def path(self, batch_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", batch_id):
            raise ValueError("Invalid frontier batch ID")
        return self.root / batch_id

    def save(self, batch: FrontierBatchRecord) -> None:
        if batch.schema_version > 2:
            raise ValueError("Refusing to overwrite newer frontier schema")
        directory = self.path(batch.id)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "batch.json"
        if target.exists():
            raw = json.loads(target.read_text())
            if raw.get("schema_version", 1) > 2:
                raise ValueError("Refusing to overwrite newer frontier schema")
            if raw.get("schema_version", 1) < 2 and not target.with_suffix(".v1.bak").exists():
                shutil.copy2(target, target.with_suffix(".v1.bak"))
        temporary = target.with_suffix(".tmp")
        temporary.write_text(batch.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(target)

    def load(self, batch_id: str) -> FrontierBatchRecord:
        path = self.path(batch_id) / "batch.json"
        if not path.exists():
            raise FileNotFoundError(f"Frontier batch not found: {batch_id}")
        return FrontierBatchRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self) -> list[FrontierBatchRecord]:
        records: list[FrontierBatchRecord] = []
        for path in sorted(self.root.glob("*/batch.json"), reverse=True):
            try:
                records.append(
                    FrontierBatchRecord.model_validate_json(path.read_text(encoding="utf-8"))
                )
            except (OSError, ValueError):
                continue
        return records


def defer_task(
    *,
    repository: Path,
    kind: TaskKind,
    description: str,
    reason: str | None = None,
    category: ExternalReviewCategory = ExternalReviewCategory.USER_DEFERRED,
) -> TaskRecord:
    """Record an issue for external review without invoking a local model."""
    git = GitRepository(repository)
    config = load_project_config(git.root)
    integration = git.ensure_integration_worktree(config)
    if not git.is_clean(integration):
        raise RuntimeError("Integration branch must be clean before deferring an issue")

    store = TaskStore(git.root)
    task = store.create(kind, description, "external")
    task.base_commit = git.resolve_ref(integration, "HEAD")
    task.integration_branch = config.integration_branch
    explanation = reason or "The user chose to defer this issue directly to external review."
    task.transition(TaskStatus.DEFERRED, explanation)
    task.mark_external_review(category=category, reason=explanation)
    task.advance(TaskPhase.STOPPED, "Deferred without model inference")
    store.save(task)

    bundle, visible = build_escalation_bundle(
        task=task,
        store=store,
        repository=git,
        workspace=None,
        error=explanation,
        category=category,
    )
    task.escalation_path = str(bundle)
    task.visible_review_path = str(visible)
    store.save(task)
    return task


def _render_open_tasks(tasks: list[TaskRecord]) -> str:
    if not tasks:
        return "No unresolved external-review tasks were recorded.\n"
    sections = ["# Open external-review tasks", ""]
    for task in tasks:
        sections.extend(
            [
                f"## {task.id}",
                "",
                f"- Kind: `{task.kind.value}`",
                f"- Local outcome: `{task.status.value}`",
                f"- Category: `{task.external_review_category.value if task.external_review_category else 'unknown'}`",
                f"- Base commit: `{task.base_commit or 'unknown'}`",
                f"- Existing bundle: `{task.escalation_path or 'not available'}`",
                "",
                task.description,
                "",
                "Reason:",
                "",
                task.external_review_reason or "No reason was recorded.",
                "",
            ]
        )
    return "\n".join(sections)


def _render_integrated_tasks(tasks: list[TaskRecord]) -> str:
    integrated = [task for task in tasks if task.status == TaskStatus.INTEGRATED]
    if not integrated:
        return "# Integrated local tasks\n\nNo integrated local tasks were recorded.\n"
    lines = ["# Integrated local tasks", ""]
    for task in integrated:
        lines.append(
            f"- `{task.id}` — `{task.final_commit or 'unknown commit'}` — {task.description}"
        )
    lines.append("")
    lines.append(
        "These entries are an index. The current integration-branch diff and repository files are authoritative."
    )
    return "\n".join(lines) + "\n"


def build_frontier_batch(
    *,
    repository: Path,
    task_ids: list[str] | None = None,
    include_integrated: bool = True,
    destination: Path | None = None,
) -> FrontierBatchRecord:
    """Bundle unresolved issues against the latest integration branch state."""
    git = GitRepository(repository)
    config = load_project_config(git.root)
    integration = git.ensure_integration_worktree(config)
    if not git.is_clean(integration):
        raise RuntimeError("Integration branch must be clean before building a frontier batch")

    task_store = TaskStore(git.root)
    open_tasks = task_store.open_external()
    if task_ids:
        wanted = set(task_ids)
        known = {task.id: task for task in open_tasks}
        missing = sorted(wanted - set(known))
        if missing:
            raise RuntimeError(
                "These tasks are not open external-review items: " + ", ".join(missing)
            )
        selected = [known[task_id] for task_id in task_ids]
    else:
        selected = open_tasks

    if not selected and not include_integrated:
        raise RuntimeError("No unresolved external-review tasks were found")

    store = FrontierStore(git.root)
    batch_id = store.new_id()
    bundle = store.path(batch_id)
    bundle.mkdir(parents=True, exist_ok=True)
    issues_dir = bundle / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)

    for task in selected:
        if task.escalation_path and Path(task.escalation_path).exists():
            shutil.copytree(
                Path(task.escalation_path),
                issues_dir / task.id,
                dirs_exist_ok=True,
            )

    all_tasks = task_store.list()
    integration_commit = git.resolve_ref(integration, "HEAD")
    base_diff = (
        git.diff_between(integration, config.base_branch, "HEAD") if include_integrated else ""
    )
    open_markdown = _render_open_tasks(selected)
    integrated_markdown = (
        _render_integrated_tasks(all_tasks)
        if include_integrated
        else "# Integrated local tasks\n\nOmitted by --issues-only.\n"
    )
    (bundle / "OPEN_EXTERNAL_TASKS.md").write_text(open_markdown, encoding="utf-8")
    (bundle / "INTEGRATED_LOCAL_TASKS.md").write_text(
        integrated_markdown,
        encoding="utf-8",
    )
    (bundle / "BASE_TO_INTEGRATION.patch").write_text(base_diff, encoding="utf-8")
    (bundle / "CURRENT_INTEGRATION_COMMIT.txt").write_text(
        integration_commit + "\n",
        encoding="utf-8",
    )

    prompt = f"""# Frontier Batch — {batch_id}

Repository: `{git.root}`
Base branch: `{config.base_branch}`
Working branch: `{config.integration_branch}`
Batch snapshot commit: `{integration_commit}`

## Scope

Resolve the open external-review tasks listed in `OPEN_EXTERNAL_TASKS.md` against the **latest** `{config.integration_branch}` state. Other local tasks may have been integrated after individual issue bundles were created. Preserve those later changes. Treat copied issue patches as historical evidence only.

## Read first

1. `AGENTS.md`
2. canonical files under `docs/ai/`
3. `{bundle / "OPEN_EXTERNAL_TASKS.md"}`
4. `{bundle / "INTEGRATED_LOCAL_TASKS.md"}`
5. `{bundle / "BASE_TO_INTEGRATION.patch"}`
6. each relevant directory under `{issues_dir}`
7. the latest source and tests in the repository

## Required work

- independently reproduce and verify each selected issue;
- repair planner, worker, or local-model mistakes rather than trusting their diagnosis;
- preserve current contracts unless a deliberate contract change is justified;
- run the full configured test and lint commands;
- create one or more clear Git commits on `{config.integration_branch}`;
- create a frontier handoff under `docs/ai/handoffs/` describing resolved and unresolved items;
- do not merge into `{config.base_branch}` unless this is also the intended release audit.

After the fixes are committed and the integration branch is clean, record completion with:

`localdev-mlx frontier resolve --batch {batch_id} --commit HEAD --repo {git.root}`

If an item remains unresolved, leave it open and document why instead of marking the whole batch complete.
"""
    (bundle / "FRONTIER_BATCH_PROMPT.md").write_text(prompt, encoding="utf-8")

    visible = git.root / ".localdev" / "runtime" / "frontier" / batch_id
    if visible.exists():
        raise FileExistsError(f"Refusing to replace existing frontier export: {visible}")

    if destination:
        exported = destination.resolve()
        if exported.exists():
            raise FileExistsError(f"Refusing to replace existing frontier export: {exported}")

    record = FrontierBatchRecord(
        id=batch_id,
        repository=str(git.root),
        base_branch=config.base_branch,
        integration_branch=config.integration_branch,
        integration_commit=integration_commit,
        task_ids=[task.id for task in selected],
        include_integrated=include_integrated,
        path=str(bundle),
        visible_path=str(visible),
    )
    store.save(record)
    if selected:
        task_store.mark_bundled(record.task_ids, batch_id)

    manifest = {
        "batch": record.model_dump(mode="json"),
        "generated_at": datetime.now(UTC).isoformat(),
        "open_task_count": len(selected),
        "integrated_task_count": sum(
            1 for task in all_tasks if task.status == TaskStatus.INTEGRATED
        ),
        "files": sorted(path.name for path in bundle.iterdir()),
    }
    (bundle / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    # Copy only after the manifest and batch record exist.
    visible.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(bundle, visible)
    if destination:
        exported.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(bundle, exported)
    return record


def resolve_frontier(
    *,
    repository: Path,
    batch_id: str | None = None,
    task_ids: list[str] | None = None,
    commit: str = "HEAD",
    note: str | None = None,
) -> tuple[str, list[TaskRecord], FrontierBatchRecord | None]:
    """Mark external items resolved after their fixes are on the integration branch."""
    git = GitRepository(repository)
    config = load_project_config(git.root)
    integration = git.ensure_integration_worktree(config)
    if not git.is_clean(integration):
        raise RuntimeError("Integration branch must be clean before recording frontier completion")

    resolved_commit = git.resolve_ref(integration, commit)
    integration_head = git.resolve_ref(integration, "HEAD")
    if not git.is_ancestor(integration, resolved_commit, integration_head):
        raise RuntimeError(
            f"Commit {resolved_commit} is not reachable from {config.integration_branch}. "
            "Merge or cherry-pick the frontier work into the integration branch first."
        )

    frontier_store = FrontierStore(git.root)
    batch: FrontierBatchRecord | None = None
    selected_ids = list(dict.fromkeys(task_ids or []))
    if batch_id:
        batch = frontier_store.load(batch_id)
        if selected_ids:
            unknown = sorted(set(selected_ids) - set(batch.task_ids))
            if unknown:
                raise RuntimeError(
                    "Tasks are not part of the selected batch: " + ", ".join(unknown)
                )
        else:
            selected_ids = list(batch.task_ids)
    if not selected_ids:
        raise RuntimeError("Provide --batch or at least one --task")

    task_store = TaskStore(git.root)
    for task_id in selected_ids:
        selected_task = task_store.load(task_id)
        if selected_task.external_review_state == ExternalReviewState.NONE:
            raise RuntimeError(
                f"Task {task_id} is diagnostic, not an external-review item; reopen it explicitly first"
            )
        if selected_task.base_commit and not git.is_ancestor(
            integration,
            selected_task.base_commit,
            resolved_commit,
        ):
            raise RuntimeError(
                f"Resolved commit {resolved_commit} does not contain the repository state "
                f"where task {task_id} was recorded ({selected_task.base_commit})."
            )
    updated = task_store.mark_resolved(
        selected_ids,
        commit=resolved_commit,
        note=note,
    )

    if batch:
        remaining = [
            task_id
            for task_id in batch.task_ids
            if task_store.load(task_id).external_review_state
            not in {
                ExternalReviewState.RESOLVED,
                ExternalReviewState.SUPERSEDED,
            }
        ]
        if not remaining:
            batch.mark_resolved(resolved_commit)
            frontier_store.save(batch)
            resolution = (
                f"# Frontier batch resolution\n\n"
                f"- Batch: `{batch.id}`\n"
                f"- Resolved commit: `{resolved_commit}`\n"
                f"- Resolved at: `{batch.resolved_at}`\n"
                f"- Tasks: {', '.join(batch.task_ids)}\n"
            )
            Path(batch.path, "RESOLUTION.md").write_text(resolution, encoding="utf-8")
            if batch.visible_path:
                visible_resolution = Path(batch.visible_path) / "RESOLUTION.md"
                visible_resolution.parent.mkdir(parents=True, exist_ok=True)
                visible_resolution.write_text(resolution, encoding="utf-8")

    return resolved_commit, updated, batch


def supersede_frontier(
    *,
    repository: Path,
    task_ids: list[str],
    commit: str | None = None,
    note: str | None = None,
) -> tuple[str | None, list[TaskRecord]]:
    """Close duplicate or obsolete external items without claiming a new direct fix."""
    git = GitRepository(repository)
    config = load_project_config(git.root)
    integration = git.ensure_integration_worktree(config)
    if not git.is_clean(integration):
        raise RuntimeError("Integration branch must be clean before superseding tasks")

    resolved_commit: str | None = None
    if commit:
        resolved_commit = git.resolve_ref(integration, commit)
        integration_head = git.resolve_ref(integration, "HEAD")
        if not git.is_ancestor(integration, resolved_commit, integration_head):
            raise RuntimeError(
                f"Commit {resolved_commit} is not reachable from {config.integration_branch}. "
                "Merge or cherry-pick the replacing work first."
            )

    store = TaskStore(git.root)
    for task_id in task_ids:
        task = store.load(task_id)
        if task.external_review_state not in {
            ExternalReviewState.PENDING,
            ExternalReviewState.BUNDLED,
            ExternalReviewState.RESOLVED,
        }:
            raise RuntimeError(
                f"Task {task_id} is not an external-review item that can be superseded"
            )
    updated = store.mark_superseded(
        task_ids,
        commit=resolved_commit,
        note=note,
    )

    frontier_store = FrontierStore(git.root)
    affected_batches = {batch_id for task in updated for batch_id in task.frontier_batch_ids}
    completion_commit = resolved_commit or git.resolve_ref(integration, "HEAD")
    for batch_id in affected_batches:
        batch = frontier_store.load(batch_id)
        if all(
            store.load(task_id).external_review_state
            in {ExternalReviewState.RESOLVED, ExternalReviewState.SUPERSEDED}
            for task_id in batch.task_ids
        ):
            batch.mark_resolved(completion_commit)
            frontier_store.save(batch)
    return resolved_commit, updated


def sync_frontier_ref(
    *,
    repository: Path,
    source_ref: str,
) -> tuple[str, str]:
    """Safely fast-forward the integration branch to externally produced work."""
    git = GitRepository(repository)
    config = load_project_config(git.root)
    integration = git.ensure_integration_worktree(config)
    if not git.is_clean(integration):
        raise RuntimeError("Integration branch must be clean before syncing frontier work")

    before = git.resolve_ref(integration, "HEAD")
    source = git.resolve_ref(integration, source_ref)
    if not git.is_ancestor(integration, before, source):
        raise RuntimeError(
            f"{source_ref} ({source}) does not fast-forward {config.integration_branch} "
            f"from {before}. Merge the branches manually, resolve conflicts, and rerun tests."
        )
    after = git.fast_forward(integration, source_ref)
    return before, after


def reopen_frontier(
    *,
    repository: Path,
    task_ids: list[str],
    reason: str | None = None,
) -> list[TaskRecord]:
    store = TaskStore(GitRepository(repository).root)
    reopened: list[TaskRecord] = []
    selected = [store.load(task_id) for task_id in task_ids]
    for task in selected:
        task.external_review_state = ExternalReviewState.PENDING
        task.resolved_at = None
        task.resolved_commit = None
        task.resolution_note = reason
        task.updated_at = datetime.now(UTC)
        store.save(task)
        reopened.append(task)
    # Reopening a task also reopens every batch that previously claimed completion.
    frontier_store = FrontierStore(store.repository)
    for batch_id in {batch_id for task in reopened for batch_id in task.frontier_batch_ids}:
        batch = frontier_store.load(batch_id)
        batch.status = FrontierBatchStatus.OPEN
        batch.resolved_at = None
        batch.resolved_commit = None
        batch.updated_at = datetime.now(UTC)
        frontier_store.save(batch)
    return reopened
