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
