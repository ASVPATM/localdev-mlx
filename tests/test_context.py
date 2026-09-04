from __future__ import annotations

from pathlib import Path

from localdev_mlx.config import ProjectConfig
from localdev_mlx.context.builder import build_context


def test_context_omits_secret_and_reads_requested_file(tmp_path: Path) -> None:
    (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=secret", encoding="utf-8")
    # Repository map is disabled, so this test does not require Git.
    config = ProjectConfig(repository=tmp_path, stable_docs=())
    bundle = build_context(
        tmp_path,
        config,
        requested_paths=["code.py", ".env"],
        char_budget=10_000,
        include_map=False,
    )
    rendered = bundle.render()
    assert "x = 1" in rendered
    assert "TOKEN=secret" not in rendered
    assert ".env" in bundle.omitted_paths


def test_requested_files_take_priority_when_budget_is_tight(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("stable " * 100, encoding="utf-8")
    (tmp_path / "target.py").write_text("important = True\n", encoding="utf-8")
    config = ProjectConfig(
        repository=tmp_path,
        stable_docs=("README.md",),
        max_file_chars=10_000,
    )

    bundle = build_context(
        tmp_path,
        config,
        requested_paths=["target.py"],
        char_budget=180,
        include_map=False,
        requested_first=True,
    )

    assert "target.py" in bundle.included_paths
    assert "important = True" in bundle.render()
    assert "README.md" in bundle.omitted_paths
