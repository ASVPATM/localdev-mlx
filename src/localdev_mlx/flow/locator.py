"""Bounded, framework-neutral selector-to-source hints; never invent source paths."""

import hashlib
import re
from pathlib import Path

from .config import Config
from .workspace import files


def locate(config: Config, root: Path, selection: dict) -> list[dict]:
    selector = selection.get("selector", "")
    needles = [selector, selection.get("text", "")[:100]]
    if selector.startswith("#"):
        needles.extend([f'id="{selector[1:]}"', f"'{selector[1:]}'"])
    needles.extend(re.findall(r"[.#]([A-Za-z_][\w-]{3,})", selector))
    for key, value in re.findall(r'\[(data-[\w-]+|aria-label)=["\']([^"\']+)', selector):
        needles.extend([f'{key}="{value}"', value])
    results = []
    for relative in files(config, root):
        path = root / relative
        if path.suffix not in {
            ".py",
            ".js",
            ".ts",
            ".tsx",
            ".jsx",
            ".html",
            ".css",
            ".vue",
            ".svelte",
            ".swift",
            ".kt",
            ".java",
            ".rs",
            ".go",
            ".c",
            ".cpp",
            ".cs",
            ".dart",
            ".xaml",
            ".xml",
        }:
            continue
        try:
            lines = path.read_text().splitlines()
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(lines, 1):
            if any(len(needle) >= 4 and needle in line for needle in needles):
                results.append(
                    {
                        "path": relative,
                        "line": number,
                        "excerpt": line.strip()[:240],
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "confidence": "text-match hint",
                    }
                )
                if len(results) >= 10:
                    return results
    return results
