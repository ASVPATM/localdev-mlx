from __future__ import annotations

from typer.testing import CliRunner

from localdev_mlx.cli import app

runner = CliRunner()


def test_version_option() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "0.2.0"


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
