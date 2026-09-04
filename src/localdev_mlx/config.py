from __future__ import annotations

import hashlib
import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir, user_data_dir, user_state_dir

APP_NAME = "localdev-mlx"


class ConfigurationError(RuntimeError):
    """Raised when LocalDev configuration is missing or invalid."""


@dataclass(frozen=True)
class ModelProfile:
    """One mlx_vlm.server model profile."""

    name: str
    model: str
    executable: Path
    host: str = "127.0.0.1"
    port: int = 8080
    enable_thinking: bool = True
    thinking_budget: int = 4096
    max_tokens: int = 8192
    startup_timeout_seconds: int = 900
    request_timeout_seconds: int = 900
    server_args: tuple[str, ...] = ()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"


@dataclass(frozen=True)
class RoleConfig:
    """Map workflow roles to named model profiles."""

    planner: str
    worker: str
    reviewer: str


@dataclass(frozen=True)
class GlobalConfig:
    """Machine-level model configuration."""

    models: dict[str, ModelProfile]
    roles: RoleConfig
    stop_after_run: bool = True

    def profile(self, name_or_role: str) -> ModelProfile:
        profile_name = getattr(self.roles, name_or_role, name_or_role)
        try:
            return self.models[profile_name]
        except KeyError as exc:
            raise ConfigurationError(
                f"Unknown model profile {profile_name!r} for {name_or_role!r}"
            ) from exc

    @property
    def planner(self) -> ModelProfile:
        return self.profile("planner")

    @property
    def worker(self) -> ModelProfile:
        return self.profile("worker")

    @property
    def reviewer(self) -> ModelProfile:
        return self.profile("reviewer")


@dataclass(frozen=True)
class TestConfig:
    quick: tuple[str, ...] = ()
    full: tuple[str, ...] = ()
    timeout_seconds: int = 600
    allowed_executables: tuple[str, ...] = (
        "python",
        "python3",
        "uv",
        "pytest",
        "ruff",
        "npm",
        "pnpm",
        "yarn",
        "cargo",
        "go",
        "make",
    )
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectConfig:
    repository: Path
    integration_branch: str = "ai/integration"
    base_branch: str = "main"
    auto_integrate: bool = True
    max_repair_rounds: int = 2
    planner_context_chars: int = 150_000
    worker_context_chars: int = 70_000
    reviewer_context_chars: int = 120_000
    max_file_chars: int = 100_000
    stable_docs: tuple[str, ...] = (
        "AGENTS.md",
        "README.md",
        "docs/ai/PROJECT_BRIEF.md",
        "docs/ai/ARCHITECTURE.md",
        "docs/ai/CONTRACTS.md",
        "docs/ai/CURRENT_STATE.md",
        "docs/ai/DECISIONS.md",
        "docs/ai/TASK_QUEUE.md",
    )
    deny_paths: tuple[str, ...] = (
        ".git",
        ".venv",
        "venv",
        "node_modules",
        ".env",
        ".env.*",
        "*.pem",
        "*.key",
        "*.p12",
        "*.pfx",
        "*credentials*",
        "*secret*",
        "dist",
        "build",
        "__pycache__",
    )
    tests: TestConfig = field(default_factory=TestConfig)


GLOBAL_CONFIG_PATH = Path(user_config_dir(APP_NAME)) / "config.toml"
STATE_ROOT = Path(user_state_dir(APP_NAME))
DATA_ROOT = Path(user_data_dir(APP_NAME))


def _expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def _require_table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ConfigurationError(f"Missing [{key}] table in {GLOBAL_CONFIG_PATH}")
    return value


def _model_profile(
    name: str,
    raw: dict[str, Any],
    *,
    executable: Path,
    default_host: str,
    default_port: int,
    default_server_args: tuple[str, ...],
) -> ModelProfile:
    model = raw.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ConfigurationError(f"models.{name}.model must be a non-empty string")
    args = raw.get("server_args", list(default_server_args))
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise ConfigurationError(f"models.{name}.server_args must be a list of strings")
    return ModelProfile(
        name=name,
        model=model.strip(),
        executable=_expand_path(str(raw.get("executable", executable))),
        host=str(raw.get("host", default_host)),
        port=int(raw.get("port", default_port)),
        enable_thinking=bool(raw.get("enable_thinking", True)),
        thinking_budget=int(raw.get("thinking_budget", 4096)),
        max_tokens=int(raw.get("max_tokens", 8192)),
        startup_timeout_seconds=int(raw.get("startup_timeout_seconds", 900)),
        request_timeout_seconds=int(raw.get("request_timeout_seconds", 900)),
        server_args=tuple(args),
    )


def load_global_config(path: Path = GLOBAL_CONFIG_PATH) -> GlobalConfig:
    if not path.exists():
        raise ConfigurationError(
            f"Global configuration not found at {path}. Run: localdev-mlx configure PLANNER_MODEL"
        )
    with path.open("rb") as handle:
        data = tomllib.load(handle)

    mlx = _require_table(data, "mlx")
    default_executable_raw = mlx.get("server_executable") or shutil.which("mlx_vlm.server")
    if not default_executable_raw:
        raise ConfigurationError(
            "mlx_vlm.server was not found. Set mlx.server_executable in the config."
        )
    default_executable = _expand_path(str(default_executable_raw))
    default_host = str(mlx.get("host", "127.0.0.1"))
    default_port = int(mlx.get("port", 8080))
    raw_server_args = mlx.get("server_args", [])
    if not isinstance(raw_server_args, list) or not all(
        isinstance(item, str) for item in raw_server_args
    ):
        raise ConfigurationError("mlx.server_args must be a list of strings")
    default_server_args = tuple(raw_server_args)

    raw_models = _require_table(data, "models")
    models: dict[str, ModelProfile] = {}
    for name, raw in raw_models.items():
        if not isinstance(raw, dict):
            raise ConfigurationError(f"[models.{name}] must be a table")
        models[name] = _model_profile(
            str(name),
            raw,
            executable=default_executable,
            default_host=default_host,
            default_port=default_port,
            default_server_args=default_server_args,
        )
    if not models:
        raise ConfigurationError("At least one [models.<name>] profile is required")

    raw_roles = data.get("roles", {})
    if not isinstance(raw_roles, dict):
        raise ConfigurationError("[roles] must be a table")
    first_name = next(iter(models))
    roles = RoleConfig(
        planner=str(raw_roles.get("planner", first_name)),
        worker=str(raw_roles.get("worker", raw_roles.get("planner", first_name))),
        reviewer=str(raw_roles.get("reviewer", raw_roles.get("planner", first_name))),
    )
    for role_name in (roles.planner, roles.worker, roles.reviewer):
        if role_name not in models:
            raise ConfigurationError(f"Role references unknown model profile: {role_name}")

    return GlobalConfig(
        models=models,
        roles=roles,
        stop_after_run=bool(mlx.get("stop_after_run", True)),
    )


def _tuple_strings(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigurationError("Expected a list of strings")
    return tuple(value)


def load_project_config(repository: Path) -> ProjectConfig:
    repository = repository.resolve()
    path = repository / ".localdev" / "config.toml"
    if not path.exists():
        raise ConfigurationError(
            f"Project is not initialized for LocalDev: {repository}\n"
            f"Run: localdev-mlx init {repository}"
        )
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    context = data.get("context", {})
    tests_raw = data.get("tests", {})
    paths = data.get("paths", {})
    if not all(isinstance(value, dict) for value in (context, tests_raw, paths)):
        raise ConfigurationError("[context], [tests], and [paths] must be TOML tables")
    test_env = tests_raw.get("env", {})
    if not isinstance(test_env, dict) or not all(
        isinstance(key, str) and isinstance(value, (str, int, float, bool))
        for key, value in test_env.items()
    ):
        raise ConfigurationError("[tests.env] must contain scalar values")
    default = ProjectConfig(repository=repository)
    return ProjectConfig(
        repository=repository,
        integration_branch=str(data.get("integration_branch", default.integration_branch)),
        base_branch=str(data.get("base_branch", default.base_branch)),
        auto_integrate=bool(data.get("auto_integrate", default.auto_integrate)),
        max_repair_rounds=int(data.get("max_repair_rounds", default.max_repair_rounds)),
        planner_context_chars=int(
            context.get("planner_chars", default.planner_context_chars)
        ),
        worker_context_chars=int(context.get("worker_chars", default.worker_context_chars)),
        reviewer_context_chars=int(
            context.get("reviewer_chars", default.reviewer_context_chars)
        ),
        max_file_chars=int(context.get("max_file_chars", default.max_file_chars)),
        stable_docs=_tuple_strings(paths.get("stable_docs"), default.stable_docs),
        deny_paths=_tuple_strings(paths.get("deny"), default.deny_paths),
        tests=TestConfig(
            quick=_tuple_strings(tests_raw.get("quick"), default.tests.quick),
            full=_tuple_strings(tests_raw.get("full"), default.tests.full),
            timeout_seconds=int(tests_raw.get("timeout_seconds", default.tests.timeout_seconds)),
            allowed_executables=_tuple_strings(
                tests_raw.get("allowed_executables"),
                default.tests.allowed_executables,
            ),
            env={str(key): str(value) for key, value in test_env.items()},
        ),
    )


def project_id(repository: Path) -> str:
    normalized = str(repository.resolve()).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()[:16]


def project_state_dir(repository: Path) -> Path:
    path = DATA_ROOT / "projects" / project_id(repository)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_global_config(
    *,
    planner_model: str,
    worker_model: str | None = None,
    reviewer_model: str | None = None,
    server_executable: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8080,
    stop_after_run: bool = True,
    enable_thinking: bool = True,
    thinking_budget: int = 4096,
    max_tokens: int = 8192,
    startup_timeout_seconds: int = 900,
    request_timeout_seconds: int = 900,
    server_args: tuple[str, ...] = (),
    path: Path = GLOBAL_CONFIG_PATH,
    force: bool = False,
) -> Path:
    """Write a concise machine-level configuration.

    Equal model IDs share one profile, allowing a single local model to fill every role.
    """

    if path.exists() and not force:
        raise ConfigurationError(f"Configuration already exists: {path}. Use --force to replace it.")
    executable = str(server_executable or shutil.which("mlx_vlm.server") or "mlx_vlm.server")
    role_models = {
        "planner": planner_model,
        "worker": worker_model or planner_model,
        "reviewer": reviewer_model or planner_model,
    }
    profile_for_model: dict[str, str] = {}
    role_profiles: dict[str, str] = {}
    for role, model in role_models.items():
        if model not in profile_for_model:
            profile_for_model[model] = role
        role_profiles[role] = profile_for_model[model]

    lines = [
        "[mlx]",
        f"server_executable = {_quote(executable)}",
        f"host = {_quote(host)}",
        f"port = {port}",
        f"stop_after_run = {'true' if stop_after_run else 'false'}",
        "server_args = [" + ", ".join(_quote(item) for item in server_args) + "]",
        "",
        "[roles]",
        f"planner = {_quote(role_profiles['planner'])}",
        f"worker = {_quote(role_profiles['worker'])}",
        f"reviewer = {_quote(role_profiles['reviewer'])}",
        "",
    ]
    for model, profile_name in profile_for_model.items():
        lines.extend(
            [
                f"[models.{profile_name}]",
                f"model = {_quote(model)}",
                f"enable_thinking = {'true' if enable_thinking else 'false'}",
                f"thinking_budget = {thinking_budget}",
                f"max_tokens = {max_tokens}",
                f"startup_timeout_seconds = {startup_timeout_seconds}",
                f"request_timeout_seconds = {request_timeout_seconds}",
                "",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def write_default_project_config(
    repository: Path,
    *,
    quick_tests: list[str] | None = None,
    full_tests: list[str] | None = None,
    base_branch: str = "main",
    integration_branch: str = "ai/integration",
) -> Path:
    path = repository / ".localdev" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    quick_tests = quick_tests or []
    full_tests = full_tests or quick_tests

    def toml_array(values: list[str]) -> str:
        return "[" + ", ".join(_quote(value) for value in values) + "]"

    content = f'''integration_branch = {_quote(integration_branch)}
base_branch = {_quote(base_branch)}
auto_integrate = true
max_repair_rounds = 2

[context]
planner_chars = 150000
worker_chars = 70000
reviewer_chars = 120000
max_file_chars = 100000

[paths]
stable_docs = [
  "AGENTS.md",
  "README.md",
  "docs/ai/PROJECT_BRIEF.md",
  "docs/ai/ARCHITECTURE.md",
  "docs/ai/CONTRACTS.md",
  "docs/ai/CURRENT_STATE.md",
  "docs/ai/DECISIONS.md",
  "docs/ai/TASK_QUEUE.md",
]
deny = [
  ".git", ".venv", "venv", "node_modules", ".env", ".env.*",
  "*.pem", "*.key", "*.p12", "*.pfx", "*credentials*", "*secret*",
  "dist", "build", "__pycache__",
]

[tests]
quick = {toml_array(quick_tests)}
full = {toml_array(full_tests)}
timeout_seconds = 600
allowed_executables = [
  "python", "python3", "uv", "pytest", "ruff", "npm", "pnpm", "yarn",
  "cargo", "go", "make",
]

[tests.env]
'''
    path.write_text(content, encoding="utf-8")
    return path
