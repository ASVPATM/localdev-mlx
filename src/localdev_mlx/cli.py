from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

import typer
from platformdirs import user_config_dir, user_data_dir, user_state_dir
from pydantic import BaseModel, ConfigDict
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from localdev_mlx import __version__
from localdev_mlx.config import (
    APP_NAME,
    GLOBAL_CONFIG_PATH,
    GlobalConfig,
    ModelProfile,
    RoleConfig,
    load_global_config,
    load_project_config,
    write_global_config,
)
from localdev_mlx.escalation import export_bundle
from localdev_mlx.git import GitRepository
from localdev_mlx.models import ModelManager
from localdev_mlx.progress import ProgressReporter
from localdev_mlx.project import initialize_project
from localdev_mlx.providers import MLXOpenAIProvider, MockStructuredProvider
from localdev_mlx.schemas import TaskKind, TaskStatus
from localdev_mlx.tasks import TaskStore
from localdev_mlx.workflows import DesignWorkflow, QueueWorkflow, ReleaseWorkflow, TaskRunner

app = typer.Typer(
    name="localdev-mlx",
    help="Route bounded Git work through configurable local MLX models, tests, and review gates.",
    no_args_is_help=True,
    invoke_without_command=True,
)
model_app = typer.Typer(help="Manage the LocalDev-controlled MLX server.")
sample_app = typer.Typer(help="Create and test a disposable sample repository.")
app.add_typer(model_app, name="model")
app.add_typer(sample_app, name="sample")
console = Console()


class ProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"]
    number: int


def _repository(path: Path) -> Path:
    return GitRepository(path).root


def _global() -> GlobalConfig:
    return load_global_config()


def _progress(interval_seconds: float = 30.0) -> ProgressReporter:
    return ProgressReporter(
        callback=lambda message: console.print(
            f"{datetime.now().astimezone().strftime('%H:%M:%S')} {message}",
            markup=False,
        ),
        heartbeat_seconds=interval_seconds,
    )


def _handle_error(exc: Exception) -> None:
    console.print(Panel(str(exc), title="LocalDev MLX error", border_style="red"))
    raise typer.Exit(1) from exc


def _mock_global() -> GlobalConfig:
    profile = ModelProfile(
        name="mock",
        model="mock/local",
        executable=Path("/bin/true"),
        enable_thinking=False,
        thinking_budget=0,
        max_tokens=4096,
    )
    return GlobalConfig(
        models={"mock": profile},
        roles=RoleConfig(planner="mock", worker="mock", reviewer="mock"),
    )


def _cleanup_models(config: GlobalConfig, *, keep_model_loaded: bool) -> None:
    if config.stop_after_run and not keep_model_loaded:
        ModelManager().stop()


def _run_maintenance(
    *,
    kind: TaskKind,
    description: str,
    repo: Path,
    depth: str,
    no_integrate: bool,
    progress_interval: float,
    keep_model_loaded: bool,
    mock: bool = False,
) -> None:
    config: GlobalConfig | None = None
    try:
        config = _mock_global() if mock else _global()
        runner = TaskRunner(
            global_config=config,
            provider=MockStructuredProvider() if mock else MLXOpenAIProvider(),
            manage_models=not mock,
            progress=_progress(progress_interval),
            depth=depth,  # type: ignore[arg-type]
        )
        task = runner.run(
            repository=repo,
            kind=kind,
            description=description,
            auto_integrate=not no_integrate,
        )
        console.print(
            Panel(
                f"Task: {task.id}\n"
                f"Status: {task.status.value}\n"
                f"Commit: {task.final_commit or '-'}\n"
                f"External review: {task.escalation_path or '-'}",
                title="LocalDev task result",
            )
        )
    except Exception as exc:
        _handle_error(exc)
    finally:
        if config is not None and not mock:
            _cleanup_models(config, keep_model_loaded=keep_model_loaded)


@app.callback()
def root_callback(
    version: Annotated[
        bool,
        typer.Option("--version", help="Show version and exit.", is_eager=True),
    ] = False,
) -> None:
    if version:
        console.print(__version__)
        raise typer.Exit()


@app.command()
def configure(
    planner_model: Annotated[str, typer.Argument(help="MLX model ID for planning/triage.")],
    worker_model: Annotated[
        str | None,
        typer.Option("--worker-model", help="Optional model ID for implementation."),
    ] = None,
    reviewer_model: Annotated[
        str | None,
        typer.Option("--reviewer-model", help="Optional model ID for review."),
    ] = None,
    server: Annotated[
        Path | None,
        typer.Option("--server", help="Path to mlx_vlm.server."),
    ] = None,
    host: Annotated[str, typer.Option(help="Local server host.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Local server port.")] = 8080,
    keep_model_loaded: Annotated[
        bool,
        typer.Option("--keep-model-loaded", help="Do not unload the final model after workflows."),
    ] = False,
    thinking: Annotated[
        bool,
        typer.Option("--thinking/--no-thinking", help="Enable thinking mode for configured models."),
    ] = True,
    thinking_budget: Annotated[
        int,
        typer.Option("--thinking-budget", min=0, help="Base thinking-token budget."),
    ] = 4096,
    max_tokens: Annotated[
        int,
        typer.Option("--max-tokens", min=256, help="Base maximum response tokens."),
    ] = 8192,
    server_arg: Annotated[
        list[str] | None,
        typer.Option(
            "--server-arg",
            help="Extra mlx_vlm.server argument; repeat as needed. Use --server-arg=--flag for flag values.",
        ),
    ] = None,
    force: Annotated[bool, typer.Option(help="Replace an existing global config.")] = False,
) -> None:
    """Create machine-level MLX model configuration."""
    try:
        path = write_global_config(
            planner_model=planner_model,
            worker_model=worker_model,
            reviewer_model=reviewer_model,
            server_executable=server,
            host=host,
            port=port,
            stop_after_run=not keep_model_loaded,
            enable_thinking=thinking,
            thinking_budget=thinking_budget,
            max_tokens=max_tokens,
            server_args=tuple(server_arg or ()),
            force=force,
        )
        console.print(f"Created [bold]{path}[/bold]")
        console.print("Run: localdev-mlx model probe planner")
    except Exception as exc:
        _handle_error(exc)


@app.command()
def doctor(
    repo: Annotated[Path | None, typer.Option("--repo", help="Optional initialized project.")] = None,
) -> None:
    """Check the CLI, MLX configuration, Git, and an optional project."""
    table = Table(title="LocalDev MLX doctor")
    table.add_column("Check")
    table.add_column("Result")
    table.add_column("Details")
    table.add_row(
        "Python >= 3.11",
        "PASS" if sys.version_info >= (3, 11) else "FAIL",
        sys.version.split()[0],
    )
    for executable in ("git", "uv"):
        path = shutil.which(executable)
        table.add_row(executable, "PASS" if path else "WARN", path or "not found")

    config: GlobalConfig | None = None
    try:
        config = _global()
        table.add_row("Global config", "PASS", str(GLOBAL_CONFIG_PATH))
        for name, profile in config.models.items():
            executable_ok = profile.executable.exists() or shutil.which(str(profile.executable))
            table.add_row(
                f"MLX profile: {name}",
                "PASS" if executable_ok else "FAIL",
                f"{profile.model} via {profile.executable}",
            )
        table.add_row(
            "Role mapping",
            "PASS",
            f"planner={config.roles.planner}, worker={config.roles.worker}, reviewer={config.roles.reviewer}",
        )
        status = ModelManager().status(config.planner)
        table.add_row(
            "Managed server",
            "PASS" if status.get("running") else "INFO",
            json.dumps(status, default=str)[:500],
        )
    except Exception as exc:
        table.add_row("Global config", "FAIL", str(exc))

    if repo is not None:
        try:
            root = _repository(repo)
            project = load_project_config(root)
            git = GitRepository(root)
            name, email = git.configured_identity()
            table.add_row("Project config", "PASS", str(root / ".localdev/config.toml"))
            table.add_row("Git commits", "PASS" if git.has_commits() else "FAIL", str(root))
            table.add_row(
                "Git identity",
                "PASS" if name and email else "WARN",
                f"{name or 'missing name'} <{email or 'missing email'}>",
            )
            table.add_row(
                "Configured tests",
                "PASS" if project.tests.quick and project.tests.full else "FAIL",
                f"quick={list(project.tests.quick)} full={list(project.tests.full)}",
            )
        except Exception as exc:
            table.add_row("Project", "FAIL", str(exc))

    console.print(table)
    console.print(f"Config: {user_config_dir(APP_NAME)}")
    console.print(f"State:  {user_state_dir(APP_NAME)}")
    console.print(f"Data:   {user_data_dir(APP_NAME)}")


@app.command("init")
def init_project(
    repository: Annotated[Path, typer.Argument(help="Existing clean Git repository.")] = Path("."),
    quick_test: Annotated[
        list[str] | None,
        typer.Option("--quick-test", help="Approved quick test command; repeatable."),
    ] = None,
    full_test: Annotated[
        list[str] | None,
        typer.Option("--full-test", help="Approved full test command; repeatable."),
    ] = None,
    base_branch: Annotated[
        str | None,
        typer.Option("--base-branch", help="Release branch; defaults to the current branch."),
    ] = None,
    integration_branch: Annotated[
        str,
        typer.Option("--integration-branch", help="Branch used for local-model work."),
    ] = "ai/integration",
    commit: Annotated[
        bool,
        typer.Option("--commit/--no-commit", help="Commit metadata on ai/integration."),
    ] = True,
) -> None:
    """Initialize a repository for LocalDev MLX."""
    try:
        root, changed = initialize_project(
            repository,
            quick_tests=quick_test,
            full_tests=full_test,
            switch_integration=True,
            base_branch=base_branch,
            integration_branch=integration_branch,
        )
        git = GitRepository(root)
        if commit and changed:
            commit_id = git.commit_all(root, "chore: initialize LocalDev MLX")
            console.print(f"Initialized and committed [bold]{commit_id}[/bold]")
        elif changed:
            console.print("Initialized files; review and commit them before running tasks.")
        else:
            console.print("Project was already initialized.")
        console.print(f"Repository: [bold]{root}[/bold]")
        console.print(f"Branch: [bold]{git.current_branch(root)}[/bold]")
    except Exception as exc:
        _handle_error(exc)


@model_app.command("status")
def model_status() -> None:
    """Show the managed model-server state."""
    try:
        config = _global()
        manager = ModelManager()
        data = {
            role: manager.status(config.profile(role))
            for role in ("planner", "worker", "reviewer")
        }
        console.print_json(data=data)
    except Exception as exc:
        _handle_error(exc)


@model_app.command("start")
def model_start(
    profile: Annotated[str, typer.Argument(help="Role or configured profile name.")],
) -> None:
    """Load one configured model, replacing a managed model when necessary."""
    try:
        selected = _global().profile(profile)
        progress = _progress(20)
        with progress.operation(f"MODEL-{profile}", f"Loading {selected.model}"):
            ModelManager().ensure(selected)
        console.print(f"Serving [bold]{selected.model}[/bold] at {selected.base_url}")
    except Exception as exc:
        _handle_error(exc)


@model_app.command("stop")
def model_stop() -> None:
    """Unload and stop the server when LocalDev started it."""
    try:
        ModelManager().stop()
        console.print("LocalDev-managed MLX server stopped.")
    except Exception as exc:
        _handle_error(exc)


@model_app.command("logs")
def model_logs(lines: Annotated[int, typer.Option(min=1, max=2000)] = 100) -> None:
    """Show the managed MLX server log tail."""
    console.print(ModelManager().log_tail(lines))


@model_app.command("probe")
def model_probe(
    profile: Annotated[str, typer.Argument(help="Role or configured profile name.")],
    keep_model_loaded: Annotated[bool, typer.Option("--keep-model-loaded")] = False,
    progress_interval: Annotated[float, typer.Option("--progress-interval", min=5)] = 20,
) -> None:
    """Verify model loading and JSON-schema output."""
    config: GlobalConfig | None = None
    try:
        config = _global()
        selected = config.profile(profile)
        progress = _progress(progress_interval)
        with progress.operation(f"PROBE-{profile}", f"Preparing {selected.model}"):
            ModelManager().ensure(selected)
        provider = MLXOpenAIProvider()
        result = provider.complete_structured(
            profile=selected,
            system_prompt="Return only schema-valid JSON.",
            user_prompt='Return status "ok" and number 7.',
            response_model=ProbeResult,
            schema_name="LocalDevProbe",
        )
        console.print_json(
            data={"profile": profile, "model": selected.model, "structured": result.model_dump()}
        )
    except Exception as exc:
        _handle_error(exc)
    finally:
        if config is not None:
            _cleanup_models(config, keep_model_loaded=keep_model_loaded)


@app.command()
def idea(
    description: Annotated[str, typer.Argument(help="Rough project idea.")],
    repo: Annotated[Path, typer.Option("--repo")] = Path("."),
    keep_model_loaded: Annotated[bool, typer.Option("--keep-model-loaded")] = False,
    progress_interval: Annotated[float, typer.Option("--progress-interval", min=5)] = 30,
) -> None:
    """Turn a rough idea into a brief, provisional architecture, and review prompt."""
    config: GlobalConfig | None = None
    try:
        config = _global()
        workflow = DesignWorkflow(
            global_config=config,
            provider=MLXOpenAIProvider(),
            progress=_progress(progress_interval),
        )
        path, commit = workflow.ideate(repository=repo, description=description)
        console.print(f"Design committed as [bold]{commit}[/bold] in {path}")
    except Exception as exc:
        _handle_error(exc)
    finally:
        if config is not None:
            _cleanup_models(config, keep_model_loaded=keep_model_loaded)


@app.command()
def plan(
    description: Annotated[
        str,
        typer.Argument(help="Planning instructions or desired implementation emphasis."),
    ],
    repo: Annotated[Path, typer.Option("--repo")] = Path("."),
    external_review: Annotated[
        Path | None,
        typer.Option("--external-review", help="Optional completed design review file."),
    ] = None,
    skip_external_review: Annotated[
        bool,
        typer.Option(help="Allow planning without an independent design review."),
    ] = False,
    keep_model_loaded: Annotated[bool, typer.Option("--keep-model-loaded")] = False,
    progress_interval: Annotated[float, typer.Option("--progress-interval", min=5)] = 30,
) -> None:
    """Create canonical architecture, contracts, and a machine-readable task queue."""
    config: GlobalConfig | None = None
    try:
        config = _global()
        workflow = DesignWorkflow(
            global_config=config,
            provider=MLXOpenAIProvider(),
            progress=_progress(progress_interval),
        )
        path, commit = workflow.plan(
            repository=repo,
            description=description,
            external_review_path=external_review,
            allow_without_external_review=skip_external_review,
        )
        console.print(f"Plan committed as [bold]{commit}[/bold] in {path}")
    except Exception as exc:
        _handle_error(exc)
    finally:
        if config is not None:
            _cleanup_models(config, keep_model_loaded=keep_model_loaded)


@app.command()
def bug(
    description: Annotated[str, typer.Argument(help="Observed and expected behavior.")],
    repo: Annotated[Path, typer.Option("--repo")] = Path("."),
    depth: Annotated[str, typer.Option(help="fast, balanced, or deep")] = "balanced",
    no_integrate: Annotated[bool, typer.Option(help="Leave the approved commit on its task branch.")] = False,
    keep_model_loaded: Annotated[bool, typer.Option("--keep-model-loaded")] = False,
    progress_interval: Annotated[float, typer.Option("--progress-interval", min=5)] = 30,
) -> None:
    """Triage, implement, test, and review a bug fix."""
    _run_maintenance(
        kind=TaskKind.BUG,
        description=description,
        repo=repo,
        depth=depth,
        no_integrate=no_integrate,
        progress_interval=progress_interval,
        keep_model_loaded=keep_model_loaded,
    )


@app.command()
def feature(
    description: Annotated[str, typer.Argument(help="Feature behavior and acceptance criteria.")],
    repo: Annotated[Path, typer.Option("--repo")] = Path("."),
    depth: Annotated[str, typer.Option(help="fast, balanced, or deep")] = "balanced",
    no_integrate: Annotated[bool, typer.Option(help="Leave the approved commit on its task branch.")] = False,
    keep_model_loaded: Annotated[bool, typer.Option("--keep-model-loaded")] = False,
    progress_interval: Annotated[float, typer.Option("--progress-interval", min=5)] = 30,
) -> None:
    """Implement a bounded feature through the local review loop."""
    _run_maintenance(
        kind=TaskKind.FEATURE,
        description=description,
        repo=repo,
        depth=depth,
        no_integrate=no_integrate,
        progress_interval=progress_interval,
        keep_model_loaded=keep_model_loaded,
    )


@app.command()
def tweak(
    description: Annotated[str, typer.Argument(help="Small requested adjustment.")],
    repo: Annotated[Path, typer.Option("--repo")] = Path("."),
    depth: Annotated[str, typer.Option(help="fast, balanced, or deep")] = "fast",
    no_integrate: Annotated[bool, typer.Option(help="Leave the approved commit on its task branch.")] = False,
    keep_model_loaded: Annotated[bool, typer.Option("--keep-model-loaded")] = False,
    progress_interval: Annotated[float, typer.Option("--progress-interval", min=5)] = 30,
) -> None:
    """Apply a narrow project tweak through the same safety gates."""
    _run_maintenance(
        kind=TaskKind.TWEAK,
        description=description,
        repo=repo,
        depth=depth,
        no_integrate=no_integrate,
        progress_interval=progress_interval,
        keep_model_loaded=keep_model_loaded,
    )


@app.command()
def status(repo: Annotated[Path, typer.Option("--repo")] = Path(".")) -> None:
    """List stored task states for a project."""
    try:
        tasks = TaskStore(_repository(repo)).list()
        table = Table(title=f"LocalDev tasks — {_repository(repo).name}")
        for column in ("ID", "Kind", "Status", "Updated", "External review"):
            table.add_column(column)
        for task in tasks:
            table.add_row(
                task.id,
                task.kind.value,
                task.status.value,
                task.updated_at.isoformat(timespec="seconds"),
                task.escalation_path or "",
            )
        console.print(table)
    except Exception as exc:
        _handle_error(exc)


@app.command()
def follow(
    task_id: Annotated[str | None, typer.Argument(help="Task ID; defaults to newest.")] = None,
    repo: Annotated[Path, typer.Option("--repo")] = Path("."),
    interval: Annotated[float, typer.Option("--interval", min=0.5)] = 2,
) -> None:
    """Follow persisted task transitions from another terminal."""
    try:
        store = TaskStore(_repository(repo))
        selected = task_id
        if selected is None:
            tasks = store.list()
            if not tasks:
                raise RuntimeError("No LocalDev tasks exist for this repository")
            selected = tasks[0].id
        console.print(f"Following [bold]{selected}[/bold]. Press Ctrl-C to stop.")
        seen = 0
        terminal = {
            TaskStatus.INTEGRATED,
            TaskStatus.APPROVED,
            TaskStatus.ESCALATED,
            TaskStatus.FAILED,
        }
        while True:
            task = store.load(selected)
            for event in task.events[seen:]:
                console.print(event, markup=False)
            seen = len(task.events)
            if task.status in terminal:
                console.print(f"Final status: [bold]{task.status.value}[/bold]")
                return
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("Stopped following task.")
    except Exception as exc:
        _handle_error(exc)


@app.command("show-task")
def show_task(
    task_id: Annotated[str, typer.Argument()],
    repo: Annotated[Path, typer.Option("--repo")] = Path("."),
) -> None:
    """Show complete stored state for one task."""
    try:
        task = TaskStore(_repository(repo)).load(task_id)
        console.print_json(task.model_dump_json(indent=2))
    except Exception as exc:
        _handle_error(exc)


@app.command("export-review")
def export_review(
    task_id: Annotated[str, typer.Argument()],
    destination: Annotated[Path | None, typer.Option("--to")] = None,
    repo: Annotated[Path, typer.Option("--repo")] = Path("."),
) -> None:
    """Copy a failed task's external-review bundle into the repository."""
    try:
        root = _repository(repo)
        task = TaskStore(root).load(task_id)
        if not task.escalation_path:
            raise RuntimeError(f"Task {task_id} has no external-review bundle")
        target = destination or root / "docs" / "ai" / "reviews" / task_id
        exported = export_bundle(Path(task.escalation_path), target)
        console.print(f"Exported to [bold]{exported}[/bold]")
        console.print(f"Review prompt: [bold]{exported / 'EXTERNAL_REVIEW_PROMPT.md'}[/bold]")
    except Exception as exc:
        _handle_error(exc)


@app.command("run-queue")
def run_queue(
    repo: Annotated[Path, typer.Option("--repo")] = Path("."),
    max_tasks: Annotated[int, typer.Option("--max-tasks", min=0)] = 1,
    depth: Annotated[str, typer.Option(help="fast, balanced, or deep")] = "balanced",
    keep_model_loaded: Annotated[bool, typer.Option("--keep-model-loaded")] = False,
    progress_interval: Annotated[float, typer.Option("--progress-interval", min=5)] = 30,
) -> None:
    """Run dependency-ready local entries from docs/ai/TASK_QUEUE.json."""
    config: GlobalConfig | None = None
    try:
        config = _global()
        workflow = QueueWorkflow(
            global_config=config,
            provider=MLXOpenAIProvider(),
            manage_models=True,
            progress=_progress(progress_interval),
            depth=depth,
        )
        results = workflow.run(repository=repo, max_tasks=max_tasks)
        if not results:
            console.print("No dependency-ready pending tasks were found.")
            return
        table = Table(title="Task queue run")
        table.add_column("Task")
        table.add_column("Outcome")
        for task_id, outcome in results:
            table.add_row(task_id, outcome)
        console.print(table)
    except Exception as exc:
        _handle_error(exc)
    finally:
        if config is not None:
            _cleanup_models(config, keep_model_loaded=keep_model_loaded)


@app.command("release-candidate")
def release_candidate(
    repo: Annotated[Path, typer.Option("--repo")] = Path("."),
    keep_model_loaded: Annotated[bool, typer.Option("--keep-model-loaded")] = False,
    progress_interval: Annotated[float, typer.Option("--progress-interval", min=5)] = 30,
) -> None:
    """Run full local validation and create an independent release-review bundle."""
    config: GlobalConfig | None = None
    try:
        config = _global()
        workflow = ReleaseWorkflow(
            global_config=config,
            provider=MLXOpenAIProvider(),
            progress=_progress(progress_interval),
        )
        bundle = workflow.run(repo)
        console.print(f"Release review bundle: [bold]{bundle}[/bold]")
        console.print(f"Review prompt: [bold]{bundle / 'EXTERNAL_RELEASE_REVIEW.md'}[/bold]")
    except Exception as exc:
        _handle_error(exc)
    finally:
        if config is not None:
            _cleanup_models(config, keep_model_loaded=keep_model_loaded)


@sample_app.command("create")
def sample_create(
    path: Annotated[Path, typer.Argument()] = Path.cwd() / "localdev-sample",
    force: Annotated[bool, typer.Option(help="Delete an existing sample directory.")] = False,
) -> None:
    """Create a tiny repository with one intentional subtraction bug."""
    try:
        target = path.resolve()
        if target.exists():
            if not force:
                raise RuntimeError(f"Sample path already exists: {target}")
            shutil.rmtree(target)
        (target / "src/samplecalc").mkdir(parents=True)
        (target / "tests").mkdir(parents=True)
        (target / "src/samplecalc/__init__.py").write_text(
            "from samplecalc.core import add, subtract\n\n__all__ = ['add', 'subtract']\n",
            encoding="utf-8",
        )
        (target / "src/samplecalc/core.py").write_text(
            "def add(a: int, b: int) -> int:\n    return a + b\n\n\ndef subtract(a: int, b: int) -> int:\n    return a + b  # intentional demo bug\n",
            encoding="utf-8",
        )
        (target / "tests/test_core.py").write_text(
            "import unittest\n\nfrom samplecalc import add, subtract\n\n\nclass CalculatorTests(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n    def test_subtract(self):\n        self.assertEqual(subtract(7, 2), 5)\n\n\nif __name__ == '__main__':\n    unittest.main()\n",
            encoding="utf-8",
        )
        (target / "README.md").write_text("# LocalDev Sample\n", encoding="utf-8")
        (target / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "main", str(target)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "LocalDev Sample"], check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "sample@local.invalid"], check=True)
        subprocess.run(["git", "-C", str(target), "add", "."], check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "test: create broken sample"], check=True, capture_output=True)
        root, _ = initialize_project(
            target,
            quick_tests=["python3 -m unittest discover -s tests -v"],
            full_tests=["python3 -m unittest discover -s tests -v"],
        )
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "chore: initialize LocalDev MLX"], check=True, capture_output=True)
        console.print(f"Created sample repository: [bold]{target}[/bold]")
    except Exception as exc:
        _handle_error(exc)


@sample_app.command("mock-run")
def sample_mock_run(
    path: Annotated[Path, typer.Argument()] = Path.cwd() / "localdev-sample",
) -> None:
    """Exercise the full Git/test/review path without loading a model."""
    _run_maintenance(
        kind=TaskKind.BUG,
        description="subtract(7, 2) returns 9 instead of 5; fix it without changing addition",
        repo=path,
        depth="fast",
        no_integrate=False,
        progress_interval=5,
        keep_model_loaded=False,
        mock=True,
    )
