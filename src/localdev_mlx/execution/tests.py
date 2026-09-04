from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path

from localdev_mlx.config import TestConfig
from localdev_mlx.schemas import TestCommandResult, TestRunResult


class TestExecutionError(RuntimeError):
    """Raised when a configured test command violates the execution policy."""


SHELL_TOKENS = {"|", "||", "&&", ";", ">", ">>", "<", "2>", "2>&1", "&"}


def parse_safe_command(command: str, allowed_executables: tuple[str, ...]) -> list[str]:
    try:
        args = shlex.split(command)
    except ValueError as exc:
        raise TestExecutionError(f"Invalid command syntax: {command!r}") from exc
    if not args:
        raise TestExecutionError("Test command cannot be empty")
    if any(token in SHELL_TOKENS for token in args):
        raise TestExecutionError(f"Shell operators are not permitted: {command}")
    executable = Path(args[0]).name
    if executable not in allowed_executables:
        raise TestExecutionError(
            f"Executable {executable!r} is not in the project test allowlist"
        )
    return args


def run_tests(root: Path, config: TestConfig, profile: str) -> TestRunResult:
    if profile not in {"quick", "full"}:
        raise TestExecutionError(f"Unknown test profile: {profile}")
    commands = config.quick if profile == "quick" else config.full
    if not commands:
        raise TestExecutionError(
            f"No commands configured for the {profile!r} test profile in .localdev/config.toml"
        )
    environment = os.environ.copy()
    for key, value in config.env.items():
        if key.upper() in {"PATH", "HOME", "SHELL", "DYLD_INSERT_LIBRARIES"}:
            raise TestExecutionError(f"Refusing to override protected environment variable: {key}")
        environment[key] = value

    results: list[TestCommandResult] = []
    for raw in commands:
        args = parse_safe_command(raw, config.allowed_executables)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                args,
                cwd=root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=config.timeout_seconds,
                env=environment,
                check=False,
            )
            result = TestCommandResult(
                command=args,
                exit_code=completed.returncode,
                stdout=completed.stdout[-200_000:],
                stderr=completed.stderr[-200_000:],
                duration_seconds=time.monotonic() - started,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            result = TestCommandResult(
                command=args,
                exit_code=124,
                stdout=stdout[-200_000:],
                stderr=stderr[-200_000:],
                duration_seconds=time.monotonic() - started,
                timed_out=True,
            )
        results.append(result)
        if not result.passed:
            break
    return TestRunResult(profile=profile, commands=results)
