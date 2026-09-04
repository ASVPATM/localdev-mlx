from __future__ import annotations

import json
import re
import shutil
import tempfile
import warnings
from datetime import UTC, datetime
from pathlib import Path

from localdev_mlx.config import canonical_repository, project_state_dir
from localdev_mlx.schemas import ExternalReviewState, TaskKind, TaskRecord


class TaskStore:
    def __init__(self, repository: Path) -> None:
        self.repository = canonical_repository(repository)
        self.root = project_state_dir(self.repository) / "tasks"
        self.root.mkdir(parents=True, exist_ok=True)

    def new_id(self, kind: TaskKind) -> str:
        prefix = {
            TaskKind.IDEA: "IDEA",
            TaskKind.BUG: "BUG",
            TaskKind.FEATURE: "FEAT",
            TaskKind.TWEAK: "TWEAK",
            TaskKind.AUDIT: "AUDIT",
            TaskKind.RELEASE: "RELEASE",
        }[kind]
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        candidate = f"{prefix}-{stamp}"
        counter = 1
        while (self.root / candidate).exists():
            counter += 1
            candidate = f"{prefix}-{stamp}-{counter}"
        return candidate

    def create(self, kind: TaskKind, description: str, worker: str) -> TaskRecord:
        task = TaskRecord(
            id=self.new_id(kind),
            kind=kind,
            description=description,
            repository=str(self.repository),
            worker=worker,
        )
        while True:
            try:
                self.path(task.id).mkdir()
                break
            except FileExistsError:
                task.id = self.new_id(kind)
        self.save(task)
        return task

    def path(self, task_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", task_id):
            raise ValueError(f"Invalid task ID: {task_id!r}")
        return self.root / task_id

    def save(self, task: TaskRecord) -> None:
        if task.schema_version > 2:
            raise ValueError("Refusing to overwrite a task from a newer schema")
        directory = self.path(task.id)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "task.json"
        if target.exists():
            raw = json.loads(target.read_text(encoding="utf-8"))
            if raw.get("schema_version", 1) > 2:
                raise ValueError("Refusing to overwrite a task from a newer schema")
            backup = target.with_suffix(".v1.bak")
            if raw.get("schema_version", 1) < 2 and not backup.exists():
                shutil.copy2(target, backup)
        task.schema_version = 2
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as handle:
            temporary = Path(handle.name)
        temporary.write_text(task.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(target)

    def load(self, task_id: str) -> TaskRecord:
        path = self.path(task_id) / "task.json"
        if not path.exists():
            raise FileNotFoundError(f"Task not found: {task_id}")
        return TaskRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self) -> list[TaskRecord]:
        tasks: list[TaskRecord] = []
        for path in sorted(self.root.glob("*/task.json"), reverse=True):
            try:
                tasks.append(TaskRecord.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                warnings.warn(f"Cannot read task {path}: {exc}", stacklevel=2)
                continue
        return tasks

    def open_external(self) -> list[TaskRecord]:
        """Return unresolved tasks that need a human or stronger model."""
        return [
            task
            for task in self.list()
            if task.external_review_state
            in {ExternalReviewState.PENDING, ExternalReviewState.BUNDLED}
        ]

    def mark_bundled(self, task_ids: list[str], batch_id: str) -> list[TaskRecord]:
        updated: list[TaskRecord] = []
        for task_id in task_ids:
            task = self.load(task_id)
            if batch_id not in task.frontier_batch_ids:
                task.frontier_batch_ids.append(batch_id)
            task.external_review_state = ExternalReviewState.BUNDLED
            self.save(task)
            updated.append(task)
        return updated

    def mark_resolved(
        self,
        task_ids: list[str],
        *,
        commit: str,
        note: str | None = None,
    ) -> list[TaskRecord]:
        updated: list[TaskRecord] = []
        for task_id in task_ids:
            task = self.load(task_id)
            task.mark_resolved(commit=commit, note=note)
            self.save(task)
            updated.append(task)
        return updated

    def mark_superseded(
        self,
        task_ids: list[str],
        *,
        commit: str | None = None,
        note: str | None = None,
    ) -> list[TaskRecord]:
        updated: list[TaskRecord] = []
        for task_id in task_ids:
            task = self.load(task_id)
            task.mark_superseded(commit=commit, note=note)
            self.save(task)
            updated.append(task)
        return updated

    def write_json(self, task_id: str, name: str, value: object) -> Path:
        target = self.path(task_id) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if hasattr(value, "model_dump_json"):
            content = value.model_dump_json(indent=2)  # type: ignore[union-attr]
        else:
            content = json.dumps(value, indent=2, default=str)
        target.write_text(content, encoding="utf-8")
        return target

    def write_text(self, task_id: str, name: str, content: str) -> Path:
        target = self.path(task_id) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target
