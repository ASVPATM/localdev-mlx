from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text
from typer.testing import CliRunner

from localdev_mlx import __version__
from localdev_mlx.cli import app

runner = CliRunner()


def test_editable_cache_tracks_authoritative_version() -> None:
    project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    files = {entry.get("file") for entry in project["tool"]["uv"]["cache-keys"]}
    assert {"pyproject.toml", "src/localdev_mlx/__init__.py"} <= files


@pytest.mark.parametrize("force_color", [False, True])
def test_version_option(monkeypatch, force_color) -> None:
    monkeypatch.setattr("localdev_mlx.cli.console", Console(force_terminal=force_color))
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_help_lists_core_workflows() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "configure" in result.stdout
    assert "bug" in result.stdout
    assert "feature" in result.stdout
    assert "release-candidate" in result.stdout


def test_configure_forwards_general_model_options(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_write_global_config(**kwargs):
        captured.update(kwargs)
        return tmp_path / "config.toml"

    monkeypatch.setattr("localdev_mlx.cli.write_global_config", fake_write_global_config)
    result = runner.invoke(
        app,
        [
            "configure",
            "example/model",
            "--server",
            "/bin/true",
            "--no-thinking",
            "--thinking-budget",
            "0",
            "--max-tokens",
            "4096",
            "--server-arg=--max-kv-size",
            "--server-arg=32768",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert captured["enable_thinking"] is False
    assert captured["thinking_budget"] == 0
    assert captured["max_tokens"] == 4096
    assert captured["server_args"] == ("--max-kv-size", "32768")


def test_bug_help_documents_direct_path_options() -> None:
    result = runner.invoke(app, ["bug", "--help"])
    assert result.exit_code == 0
    output = Text.from_ansi(result.stdout).plain
    assert "--direct" in output
    assert "--allow" in output
    assert "--read" in output


def test_direct_bug_requires_allow_path() -> None:
    result = runner.invoke(app, ["bug", "example", "--direct"])
    assert result.exit_code != 0 or "requires at least one --allow" in result.stdout
