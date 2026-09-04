from __future__ import annotations

from pathlib import Path

from localdev_mlx.config import (
    load_global_config,
    load_project_config,
    write_default_project_config,
    write_global_config,
)


def test_one_model_can_fill_every_role(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_global_config(
        planner_model="example/model",
        server_executable="/bin/true",
        path=path,
    )
    config = load_global_config(path)
    assert config.planner.model == "example/model"
    assert config.worker.model == "example/model"
    assert config.reviewer.model == "example/model"
    assert len(config.models) == 1


def test_roles_can_use_three_models(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_global_config(
        planner_model="example/planner",
        worker_model="example/worker",
        reviewer_model="example/reviewer",
        server_executable="/bin/true",
        path=path,
    )
    config = load_global_config(path)
    assert config.planner.model == "example/planner"
    assert config.worker.model == "example/worker"
    assert config.reviewer.model == "example/reviewer"
    assert len(config.models) == 3


def test_configure_supports_non_thinking_models_and_server_args(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_global_config(
        planner_model="example/model",
        server_executable="/bin/true",
        enable_thinking=False,
        thinking_budget=0,
        max_tokens=4096,
        server_args=("--max-kv-size", "32768", "--kv-bits", "4"),
        path=path,
    )
    config = load_global_config(path)
    assert config.planner.enable_thinking is False
    assert config.planner.thinking_budget == 0
    assert config.planner.max_tokens == 4096
    assert config.planner.server_args == (
        "--max-kv-size",
        "32768",
        "--kv-bits",
        "4",
    )


def test_project_config_writes_test_environment(tmp_path: Path) -> None:
    write_default_project_config(
        tmp_path,
        quick_tests=["python3 -m unittest"],
        test_env={"PYTHONPATH": "src", "APP_MODE": "test"},
    )

    config = load_project_config(tmp_path)

    assert config.tests.env == {"APP_MODE": "test", "PYTHONPATH": "src"}
