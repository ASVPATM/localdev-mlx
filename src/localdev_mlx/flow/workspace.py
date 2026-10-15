from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from .config import Config


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()) or any(
        p.is_symlink() for p in [path, *path.parents] if p != root.parent
    ):
        raise ValueError("Path escapes workspace or traverses a symlink")
    return path


def files(config: Config, root: Path) -> dict[str, str]:
    result = {}
    total = 0
    for prefix in config.include:
        top = root / prefix
        if not top.exists():
            continue
        paths = [top] if top.is_file() else top.rglob("*")
        for path in paths:
            relative = path.relative_to(root).as_posix()
            if not config.allows(relative) or not path.is_file():
                continue
            safe_path(root, relative)
            if path.stat().st_size > config.max_file_bytes:
                raise ValueError(f"Included file exceeds the snapshot bound: {relative}")
            data = path.read_bytes()
            total += len(data)
            if total > config.max_total_bytes:
                raise ValueError("Snapshot exceeds total byte budget; narrow the include list")
            result[relative] = digest(data)
    return result


def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    descriptor, name = tempfile.mkstemp(prefix=".flow-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def snapshot(config: Config, run: Path) -> dict[str, str]:
    baseline = files(config, config.root)
    workspace = run / "workspace"
    workspace.mkdir(parents=True, exist_ok=False)
    for relative in baseline:
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(safe_path(config.root, relative), target)
    if files(config, config.root) != baseline or files(config, workspace) != baseline:
        raise ValueError("Project changed while taking snapshot; retry when edits are settled")
    atomic_write(run / "baseline.json", json.dumps(baseline, sort_keys=True).encode())
    return baseline


def changes(config: Config, run: Path) -> tuple[list[str], str, dict[str, str]]:
    baseline = json.loads((run / "baseline.json").read_text())
    current = files(config, run / "workspace")
    changed = sorted(
        p for p in baseline.keys() | current.keys() if baseline.get(p) != current.get(p)
    )
    output = []
    for relative in changed:
        old = safe_path(config.root, relative)
        new = safe_path(run / "workspace", relative)
        before = old.read_bytes() if old.is_file() else b""
        after = new.read_bytes() if new.is_file() else b""
        try:
            output.extend(
                difflib.unified_diff(
                    before.decode().splitlines(True),
                    after.decode().splitlines(True),
                    fromfile=f"a/{relative}",
                    tofile=f"b/{relative}",
                )
            )
        except UnicodeDecodeError:
            output.append(f"Binary file changed: {relative}\n")
    return changed, "".join(output), current


def apply_edits(config: Config, run: Path, edits: list[dict]):
    if not isinstance(edits, list) or len(edits) > 12:
        raise ValueError("Model output must contain at most 12 exact-match edits")
    prepared = {}
    for edit in edits:
        relative, old, new = edit.get("path"), edit.get("old"), edit.get("new")
        if (
            not isinstance(relative, str)
            or not config.allows(relative)
            or not isinstance(old, str)
            or not isinstance(new, str)
        ):
            raise ValueError("Model requested an invalid or out-of-scope edit")
        path = safe_path(run / "workspace", relative)
        text = prepared.get(relative, path.read_text() if path.exists() else "")
        if old:
            if text.count(old) != 1:
                raise ValueError(f"Edit must match exactly once: {relative}")
            text = text.replace(old, new, 1)
        elif text or path.exists():
            raise ValueError("Empty old text is only valid for new files")
        else:
            text = new
        if len(text.encode()) > config.max_file_bytes:
            raise ValueError("Model edit exceeds file budget")
        prepared[relative] = text
    for relative, text in prepared.items():
        atomic_write(safe_path(run / "workspace", relative), text.encode())


def integrate(config: Config, run: Path, checked: dict[str, str]) -> list[str]:
    changed, _, current = changes(config, run)
    if current != checked:
        raise ValueError("Workspace changed after checks; run checks again")
    baseline = json.loads((run / "baseline.json").read_text())
    # Check all included files, not only edited ones: test results depended on this source snapshot.
    if files(config, config.root) != baseline:
        raise ValueError(
            "Original project changed since the snapshot; nothing was applied. Start a new run."
        )
    backups = run / "originals"
    backups.mkdir(exist_ok=False)
    for relative in changed:
        target = safe_path(config.root, relative)
        if target.is_file():
            saved = backups / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
    applied = []
    try:
        for relative in changed:
            target = safe_path(config.root, relative)
            staged = safe_path(run / "workspace", relative)
            if staged.is_file():
                atomic_write(target, staged.read_bytes())
            else:
                target.unlink()  # Exact reviewed file; its recoverable copy is above.
            applied.append(relative)
    except Exception:
        for relative in reversed(applied):
            saved, target = backups / relative, safe_path(config.root, relative)
            if saved.exists():
                atomic_write(target, saved.read_bytes())
            else:
                target.unlink(missing_ok=True)
        raise
    return changed
