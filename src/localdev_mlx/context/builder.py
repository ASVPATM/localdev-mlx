from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

from localdev_mlx.config import ProjectConfig
from localdev_mlx.context.repository_map import build_repository_map, is_probably_text


class ContextError(RuntimeError):
    """Raised when requested context cannot be read safely."""


@dataclass(frozen=True)
class ContextBundle:
    repository_map: str
    stable_docs: str
    selected_files: str
    included_paths: tuple[str, ...]
    omitted_paths: tuple[str, ...]

    def render(self) -> str:
        sections = [self.repository_map]
        if self.stable_docs:
            sections.extend(["# Stable project documents", self.stable_docs])
        if self.selected_files:
            sections.extend(["# Selected repository files", self.selected_files])
        if self.omitted_paths:
            sections.append(
                "# Omitted requested paths\n" + "\n".join(f"- {path}" for path in self.omitted_paths)
            )
        return "\n\n".join(sections)


def _denied(relative: str, patterns: tuple[str, ...]) -> bool:
    normalized = relative.replace("\\", "/")
    parts = normalized.split("/")
    return any(
        fnmatch.fnmatch(normalized, pattern)
        or any(fnmatch.fnmatch(part, pattern) for part in parts)
        for pattern in patterns
    )


def _safe_read(root: Path, relative: str, config: ProjectConfig) -> str | None:
    normalized = relative.strip().replace("\\", "/")
    if not normalized or _denied(normalized, config.deny_paths):
        return None
    path = (root / normalized).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return None
    if not path.exists() or not path.is_file() or path.is_symlink():
        return None
    if not is_probably_text(path):
        return None
    if path.stat().st_size > config.max_file_chars * 4:
        return None
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None
    if len(content) > config.max_file_chars:
        content = content[: config.max_file_chars] + "\n... [file truncated by LocalDev]"
    return content


def _render_file(relative: str, content: str) -> str:
    return f"\n--- BEGIN FILE: {relative} ---\n{content}\n--- END FILE: {relative} ---"


def build_context(
    root: Path,
    config: ProjectConfig,
    *,
    requested_paths: list[str] | tuple[str, ...] = (),
    char_budget: int,
    include_map: bool = True,
    requested_first: bool = False,
) -> ContextBundle:
    repository_map = build_repository_map(root) if include_map else ""
    budget = max(0, char_budget - len(repository_map))
    stable_sections: list[str] = []
    selected_sections: list[str] = []
    included: list[str] = []
    omitted: list[str] = []

    def add_paths(paths: tuple[str, ...] | list[str], sections: list[str]) -> None:
        nonlocal budget
        for relative in dict.fromkeys(paths):
            if relative in included:
                continue
            content = _safe_read(root, relative, config)
            if content is None:
                omitted.append(relative)
                continue
            rendered = _render_file(relative, content)
            if len(rendered) > budget:
                omitted.append(relative)
                continue
            sections.append(rendered)
            included.append(relative)
            budget -= len(rendered)

    if requested_first:
        add_paths(list(requested_paths), selected_sections)
        add_paths(list(config.stable_docs), stable_sections)
    else:
        add_paths(list(config.stable_docs), stable_sections)
        add_paths(list(requested_paths), selected_sections)

    return ContextBundle(
        repository_map=repository_map,
        stable_docs="\n".join(stable_sections),
        selected_files="\n".join(selected_sections),
        included_paths=tuple(included),
        omitted_paths=tuple(omitted),
    )
