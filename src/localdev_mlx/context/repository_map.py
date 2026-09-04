from __future__ import annotations

import ast
from pathlib import Path

from localdev_mlx.git.repository import GitRepository

TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".html",
    ".css",
    ".scss",
    ".sql",
    ".sh",
    ".zsh",
    ".rs",
    ".go",
    ".java",
    ".kt",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
}

SPECIAL_TEXT_FILES = {
    "Dockerfile",
    "Makefile",
    "Procfile",
    "LICENSE",
    "AGENTS.md",
}


def is_probably_text(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES or path.name in SPECIAL_TEXT_FILES


def python_symbols(path: Path) -> str:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, UnicodeDecodeError, SyntaxError):
        return ""
    symbols: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(f"fn:{node.name}")
        elif isinstance(node, ast.ClassDef):
            symbols.append(f"class:{node.name}")
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
    return ", ".join(symbols[:20])


def markdown_headings(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return ""
    headings = [line.strip() for line in lines if line.lstrip().startswith("#")]
    return " | ".join(headings[:12])


def build_repository_map(root: Path, *, max_entries: int = 2500) -> str:
    repository = GitRepository(root)
    lines = ["# Repository map", ""]
    for index, relative in enumerate(repository.list_files(root)):
        if index >= max_entries:
            lines.append(f"... truncated after {max_entries} entries")
            break
        path = root / relative
        if not path.exists() or path.is_dir():
            continue
        size = path.stat().st_size
        detail = ""
        if path.suffix == ".py" and size <= 200_000:
            detail = python_symbols(path)
        elif path.suffix == ".md" and size <= 200_000:
            detail = markdown_headings(path)
        suffix = f" — {detail}" if detail else ""
        lines.append(f"- `{relative}` ({size} bytes){suffix}")
    return "\n".join(lines)
