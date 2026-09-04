from __future__ import annotations

import os
import shlex
import signal
import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from localdev_mlx.config import PrepareConfig, TestConfig
from localdev_mlx.schemas import TestCommandResult, TestRunResult


class TestExecutionError(RuntimeError):
    """A configured command violates policy or has no usable configuration."""


SHELL_TOKENS = {"|", "||", "&&", ";", ">", ">>", "<", "2>", "2>&1", "&"}
PROTECTED_ENV = {
    "PATH",
    "HOME",
    "SHELL",
    "DYLD_INSERT_LIBRARIES",
    "LD_PRELOAD",
    "PYTHONHOME",
    "VIRTUAL_ENV",
    "UV_PROJECT_ENVIRONMENT",
}


def parse_safe_command(command: str, allowed_executables: tuple[str, ...]) -> list[str]:
    try:
        args = shlex.split(command)
    except ValueError as exc:
        raise TestExecutionError(f"Invalid command syntax: {command!r}") from exc
    if not args or any(token in SHELL_TOKENS for token in args):
        raise TestExecutionError(f"Empty command or shell operators are not permitted: {command}")
    if Path(args[0]).name not in allowed_executables:
        raise TestExecutionError(f"Executable {args[0]!r} is not in the project test allowlist")
    return args


def execution_environment(root: Path, config: TestConfig) -> dict[str, str]:
    environment = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"):
        environment.pop(key, None)
    paths = [
        p
        for p in environment.get("PATH", "").split(os.pathsep)
        if not (Path(p).parent / "pyvenv.cfg").exists()
    ]
    environment["PATH"] = os.pathsep.join([str(root / ".venv/bin"), *paths])
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    for key, value in config.env.items():
        if key.upper() in PROTECTED_ENV:
            raise TestExecutionError(f"Refusing to override protected environment variable: {key}")
        environment[key] = value
    return environment


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
        except ProcessLookupError:
            process.wait(timeout=2)


def _tail(handle) -> str:
    handle.seek(0, os.SEEK_END)
    handle.seek(max(0, handle.tell() - 100_000))
    return handle.read().decode("utf-8", errors="replace")


def run_commands(
    root: Path, config: TestConfig, commands: tuple[str, ...], profile: str
) -> TestRunResult:
    if not commands:
        raise TestExecutionError(
            f"No commands configured for the {profile!r} test profile in .localdev/config.toml"
        )
    parsed = [parse_safe_command(raw, config.allowed_executables) for raw in commands]
    environment = execution_environment(root, config)
    results: list[TestCommandResult] = []
    for args in parsed:
        started = time.monotonic()
        failure = None
        timed_out = False
        process = None
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                process = subprocess.Popen(
                    args,
                    cwd=root,
                    stdout=stdout,
                    stderr=stderr,
                    stdin=subprocess.DEVNULL,
                    env=environment,
                    start_new_session=True,
                )
                code = process.wait(timeout=config.timeout_seconds)
            except OSError as exc:
                code, failure = 127, "spawn"
                stderr.write(str(exc).encode())
            except subprocess.TimeoutExpired:
                code, failure, timed_out = 124, "timeout", True
            finally:
                if process:
                    _stop(process)
            out, err = _tail(stdout), _tail(stderr)
        if code and failure is None:
            text = out + err
            if any(
                marker in text
                for marker in ("Failed to spawn", "No module named", "command not found")
            ):
                failure = "spawn"
            elif "ERROR collecting" in text or "errors during collection" in text:
                failure = "collection"
            elif any(
                marker in text for marker in ("AssertionError", "FAILED", "FAIL:", "DID NOT RAISE")
            ):
                failure = "assertion"
            else:
                failure = "exit"
        result = TestCommandResult(
            command=args,
            exit_code=code,
            stdout=out,
            stderr=err,
            timed_out=timed_out,
            failure_kind=failure,
            duration_seconds=time.monotonic() - started,
        )
        results.append(result)
        if config.fail_fast and not result.passed:
            break
    return TestRunResult(profile=profile, commands=results)


def prepare_project(root: Path, config: TestConfig, prepare: PrepareConfig) -> TestRunResult | None:
    for name, commands in (("quick", config.quick), ("full", config.full)):
        if not commands:
            raise TestExecutionError(f"No commands configured for {name}")
        for raw in commands:
            parse_safe_command(raw, config.allowed_executables)
    execution_environment(root, config)
    if not prepare.commands:
        return None
    return run_commands(
        root, replace(config, timeout_seconds=prepare.timeout_seconds), prepare.commands, "prepare"
    )


def run_tests(root: Path, config: TestConfig, profile: str) -> TestRunResult:
    if profile not in {"quick", "full"}:
        raise TestExecutionError(f"Unknown test profile: {profile}")
    return run_commands(root, config, config.quick if profile == "quick" else config.full, profile)
