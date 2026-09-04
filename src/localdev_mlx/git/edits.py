from __future__ import annotations

import fnmatch
import os
import tempfile
from pathlib import Path

from localdev_mlx.schemas import FileEdit


class EditError(RuntimeError):
    """Raised when a model-proposed edit is unsafe or cannot be applied exactly."""


DEFAULT_SECRET_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*credentials*",
    "*secret*",
)


def _matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    parts = path.replace("\\", "/").split("/")
    return any(
        fnmatch.fnmatch(path, pattern)
        or any(fnmatch.fnmatch(part, pattern) for part in parts)
        for pattern in patterns
    )


def safe_target(root: Path, relative: str, deny_patterns: tuple[str, ...]) -> Path:
    if _matches_any(relative, deny_patterns + DEFAULT_SECRET_PATTERNS):
        raise EditError(f"Refusing to edit denied or secret-like path: {relative}")
    target = (root / relative).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as exc:
        raise EditError(f"Edit escapes worktree: {relative}") from exc
    if target.exists() and target.is_symlink():
        raise EditError(f"Refusing to edit symlink: {relative}")
    return target


def _atomic_write_bytes(path: Path, content: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        if mode is not None:
            os.chmod(temporary_path, mode)
        temporary_path.replace(path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _atomic_write(path: Path, content: str) -> None:
    mode = path.stat().st_mode & 0o7777 if path.exists() else None
    _atomic_write_bytes(path, content.encode("utf-8"), mode)


def apply_edits(
    root: Path,
    edits: list[FileEdit],
    *,
    allowed_paths: set[str],
    deny_patterns: tuple[str, ...],
) -> list[str]:
    """Apply one model response as a small filesystem transaction.

    If any edit cannot be applied exactly, every path touched by this response is
    restored to its pre-response state. Test failures are handled separately and
    intentionally keep the completed edit set available for the next repair round.
    """

    normalized_allowed = {path.replace("\\", "/") for path in allowed_paths}
    changed: list[str] = []
    backups: dict[Path, tuple[bool, bytes, int | None]] = {}

    try:
        for edit in edits:
            if edit.path not in normalized_allowed:
                raise EditError(
                    f"Model attempted to change {edit.path}, which is outside the work unit allowlist"
                )
            target = safe_target(root, edit.path, deny_patterns)
            if target.exists() and target.is_dir():
                raise EditError(f"Directory edits are not supported: {edit.path}")
            if target not in backups:
                if target.exists():
                    backups[target] = (
                        True,
                        target.read_bytes(),
                        target.stat().st_mode & 0o7777,
                    )
                else:
                    backups[target] = (False, b"", None)

            if edit.operation == "create":
                if target.exists():
                    raise EditError(f"create target already exists: {edit.path}")
                _atomic_write(target, edit.content or "")
            elif edit.operation == "replace_file":
                if not target.exists():
                    raise EditError(f"replace_file target does not exist: {edit.path}")
                _atomic_write(target, edit.content or "")
            elif edit.operation == "replace_text":
                if not target.exists():
                    raise EditError(f"replace_text target does not exist: {edit.path}")
                try:
                    content = target.read_text(encoding="utf-8")
                except UnicodeDecodeError as exc:
                    raise EditError(f"replace_text target is not UTF-8 text: {edit.path}") from exc
                old = edit.old_text or ""
                count = content.count(old)
                if count != edit.expected_replacements:
                    raise EditError(
                        f"Expected {edit.expected_replacements} occurrence(s) in "
                        f"{edit.path}, found {count}"
                    )
                _atomic_write(target, content.replace(old, edit.new_text or ""))
            elif edit.operation == "delete":
                if not target.exists():
                    raise EditError(f"delete target does not exist: {edit.path}")
                target.unlink()
            else:
                raise EditError(f"Unsupported edit operation: {edit.operation}")
            changed.append(edit.path)
    except Exception as exc:
        rollback_errors: list[str] = []
        for target, (existed, content, mode) in reversed(list(backups.items())):
            try:
                if existed:
                    _atomic_write_bytes(target, content, mode)
                else:
                    target.unlink(missing_ok=True)
            except Exception as rollback_exc:  # pragma: no cover - exceptional filesystem failure
                rollback_errors.append(f"{target}: {rollback_exc}")
        if rollback_errors:
            raise EditError(
                f"Edit failed ({exc}) and rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from exc
        if isinstance(exc, EditError):
            raise
        raise EditError(str(exc)) from exc

    return changed
