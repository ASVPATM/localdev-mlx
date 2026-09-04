from __future__ import annotations

from pathlib import Path

import pytest

from localdev_mlx.config import TestConfig as LocalTestConfig
from localdev_mlx.execution.tests import TestExecutionError as LocalTestExecutionError
from localdev_mlx.execution.tests import parse_safe_command, run_tests


def test_shell_operator_rejected() -> None:
    with pytest.raises(LocalTestExecutionError):
        parse_safe_command("python3 -m pytest && rm -rf x", ("python3",))


def test_unapproved_executable_rejected() -> None:
    with pytest.raises(LocalTestExecutionError):
        parse_safe_command("curl example.com", ("python3",))


def test_run_tests(tmp_path: Path) -> None:
    config = LocalTestConfig(
        quick=("python3 -c 'print(123)'",),
        full=("python3 -c 'print(123)'",),
        allowed_executables=("python3",),
    )
    result = run_tests(tmp_path, config, "quick")
    assert result.passed
    assert "123" in result.commands[0].stdout
