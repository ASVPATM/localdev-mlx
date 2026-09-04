from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from contextlib import ExitStack
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
from localdev_mlx.diagnostics import installation_identity
from localdev_mlx.escalation import export_bundle
from localdev_mlx.git import GitRepository
from localdev_mlx.models import ModelManager
from localdev_mlx.progress import ProgressReporter
from localdev_mlx.project import initialize_project
from localdev_mlx.providers import MLXOpenAIProvider, MockStructuredProvider
from localdev_mlx.schemas import (
    DeferredIssue,
    ExternalReviewState,
    TaskKind,
    TaskRecord,
    TaskStatus,
)
from localdev_mlx.tasks import TaskStore
from localdev_mlx.workflows import (
    DesignWorkflow,
    FrontierStore,
    QueueWorkflow,
    ReleaseWorkflow,
    TaskRunner,
    build_frontier_batch,
    defer_task,
    reopen_frontier,
    resolve_frontier,
    supersede_frontier,
    sync_frontier_ref,
)

app = typer.Typer(
    name="localdev-mlx",
    help="Route bounded Git work through configurable local MLX models, tests, and review gates.",
    no_args_is_help=True,
    invoke_without_command=True,
)
model_app = typer.Typer(help="Manage the LocalDev-controlled MLX server.")
sample_app = typer.Typer(help="Create and test a disposable sample repository.")
frontier_app = typer.Typer(
    help="Defer, batch, inspect, and resolve work for an external/frontier reviewer."
)
app.add_typer(model_app, name="model")
app.add_typer(sample_app, name="sample")
app.add_typer(frontier_app, name="frontier")
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


def _parse_test_env(values: list[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in values or []:
        key, separator, value = raw.partition("=")
        key = key.strip()
        if not separator or not key:
            raise ValueError(f"Invalid --test-env value {raw!r}; expected KEY=VALUE")
        result[key] = value
    return result


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


def _cleanup_models(config: GlobalConfig, *, keep_model_loaded: bool, manager=None) -> None:
    if not config.stop_after_run or keep_model_loaded:
        return
    try:
        (manager or ModelManager()).stop()
    except Exception as exc:  # Cleanup must not hide a successful workflow result.
        console.print(
            "Warning: the model request succeeded, but automatic MLX shutdown failed. "
            f"Run 'localdev-mlx model status' and then 'localdev-mlx model stop'. "
            f"Details: {exc}",
            style="yellow",
            markup=False,
        )


def _next_action(task: TaskRecord) -> str:
    if task.status == TaskStatus.FAILED:
        return f"Fix the recorded {task.failure_category or 'workflow'} error; inspect localdev-mlx show-task {task.id}."
    if task.status == TaskStatus.CANCELLED:
        return "Cancelled; inspect preserved artifacts and start a new task when ready."
    if task.status == TaskStatus.PLANNED:
        return "Inspect triage.json and context manifests before running without --plan-only."
    if task.status == TaskStatus.INTEGRATED:
        return "Saved on ai/integration; include it in a later release-candidate audit."
    if task.status == TaskStatus.APPROVED:
        return "Committed on the task branch; integrate it manually when ready."
    if task.status == TaskStatus.DEFERRED:
        return "Waiting for frontier review; add it to a batch with localdev-mlx frontier bundle."
    if task.status == TaskStatus.ESCALATED:
        return "Local work stopped; inspect the review bundle or add it to a frontier batch."
    return "Inspect with localdev-mlx show-task TASK-ID --repo PATH."


def _print_task_result(task: TaskRecord) -> None:
    category = task.external_review_category.value if task.external_review_category else "-"
    external_state = task.external_review_state.value
    console.print(
        Panel(
            f"Task: {task.id}\n"
            f"Local outcome: {task.status.value}\n"
            f"Commit: {task.final_commit or '-'}\n"
            f"Frontier state: {external_state}\n"
            f"Frontier category: {category}\n"
            f"Failure category: {task.failure_category or '-'}\n"
            f"Canonical review bundle: {task.escalation_path or '-'}\n"
            f"Project-visible review copy: {task.visible_review_path or '-'}\n"
            f"Next: {_next_action(task)}",
            title="LocalDev task result",
        )
    )


def _run_maintenance(
    *,
    kind: TaskKind,
    description: str,
    repo: Path,
    depth: str,
    no_integrate: bool,
    progress_interval: float,
    keep_model_loaded: bool,
    frontier_only: bool = False,
    direct: bool = False,
    allow_paths: list[str] | None = None,
    read_paths: list[str] | None = None,
    mock: bool = False,
    max_attempts: int | None = None,
    no_fallback: bool = False,
    request_timeout: int | None = None,
    task_timeout: int | None = None,
    plan_only: bool = False,
    test_commands: list[str] | None = None,
) -> None:
    config: GlobalConfig | None = None
    try:
        if frontier_only and (direct or allow_paths or read_paths):
            raise ValueError("--frontier-only cannot be combined with --direct, --allow, or --read")
        if direct and not allow_paths:
            raise ValueError("--direct requires at least one --allow PATH")
        if not direct and (allow_paths or read_paths):
            raise ValueError("--allow and --read require --direct")
        if frontier_only:
            task = defer_task(
                repository=repo,
                kind=kind,
                description=description,
            )
            _print_task_result(task)
            return

        config = _mock_global() if mock else _global()
        runner = TaskRunner(
            global_config=config,
            provider=MockStructuredProvider() if mock else MLXOpenAIProvider(),
            manage_models=not mock,
            progress=_progress(progress_interval),
            depth=depth,  # type: ignore[arg-type]
            max_attempts=max_attempts,
            no_fallback=no_fallback,
            request_timeout=request_timeout,
            task_timeout=task_timeout,
            keep_model_loaded=keep_model_loaded,
        )
        task = runner.run(
            repository=repo,
            kind=kind,
            description=description,
            auto_integrate=not no_integrate,
            direct_allowed_paths=list(allow_paths or []) if direct else None,
            direct_read_paths=list(read_paths or []) if direct else None,
            direct_test_commands=test_commands,
            plan_only=plan_only,
        )
        _print_task_result(task)
        if task.status in {TaskStatus.FAILED, TaskStatus.ESCALATED, TaskStatus.CANCELLED}:
            raise typer.Exit(130 if task.status == TaskStatus.CANCELLED else 1)
    except typer.Exit:
        raise
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
def guide() -> None:
    """Explain the normal, deferred, and frontier-review paths."""
    console.print(
        Panel(
            "idea\n"
            "  Planner writes a project brief and proposed architecture; no app code is changed.\n\n"
            "plan\n"
            "  Planner writes canonical architecture, contracts, and a task queue.\n\n"
            "run-queue\n"
            "  Execute dependency-ready queue items; independent items may continue after an "
            "escalation.\n\n"
            "bug / feature / tweak\n"
            "  Planner → Worker → tests → Reviewer → commit to ai/integration\n\n"
            "--direct with repeated --allow PATH\n"
            "  Skip planner inference for a known bounded change; Worker → tests → Reviewer.\n\n"
            "--frontier-only (alias --escalate-now)\n"
            "  Record the issue without loading a model; no code is changed.\n\n"
            "integrated\n"
            "  The local change and handoff are committed on ai/integration.\n\n"
            "escalated\n"
            "  Local work stopped. A canonical bundle is stored in Application Support and "
            "an ignored visible copy is placed under .localdev/runtime/reviews/.\n\n"
            "frontier bundle\n"
            "  Combine every unresolved deferred/escalated issue with the latest integration "
            "state into one external-review packet.\n\n"
            "frontier resolve\n"
            "  After external fixes are committed to ai/integration, mark the corresponding "
            "issues resolved. New local tasks automatically start from that latest commit.\n\n"
            "frontier supersede\n"
            "  Close an older duplicate escalation after a later task or commit replaces it.\n\n"
            "frontier sync\n"
            "  Safely fast-forward ai/integration when an external reviewer committed on another "
            "branch such as main.\n\n"
            "status / show-task\n"
            "  Inspect local outcomes, frontier state, commits, and saved artifacts.\n\n"
            "release-candidate\n"
            "  Package the full base→integration diff, local validation, and open external issues "
            "for final independent review.",
            title="LocalDev MLX workflow guide",
        )
    )


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
        typer.Option(
            "--thinking/--no-thinking", help="Enable thinking mode for configured models."
        ),
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
    repo: Annotated[
        Path | None, typer.Option("--repo", help="Optional initialized project.")
    ] = None,
    prepare: Annotated[
        bool,
        typer.Option(help="Run preparation and both test gates in a fresh worktree (no models)."),
    ] = False,
) -> None:
    """Check the CLI, MLX configuration, Git, and an optional project."""
    table = Table(title="LocalDev MLX doctor")
    table.add_column("Check")
    table.add_column("Result")
    table.add_column("Details")
    identity = installation_identity()
    table.add_row(
        "Install identity", "PASS" if identity["versions_agree"] else "FAIL", json.dumps(identity)
    )
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
            if prepare:
                from localdev_mlx.execution.tests import prepare_project, run_tests

                store = TaskStore(root)
                task = store.create(
                    TaskKind.AUDIT, "Doctor fresh-worktree validation", "controller"
                )
                workspace = git.create_task_workspace(project, task.id)
                task.task_branch, task.task_worktree = workspace.branch, str(workspace.path)
                task.base_commit, task.integration_branch = (
                    workspace.base_commit,
                    workspace.integration_branch,
                )
                store.save(task)
                results = []
                try:
                    prepared = prepare_project(workspace.path, project.tests, project.prepare)
                    if prepared:
                        results.append(prepared)
                    if prepared is None or prepared.passed:
                        results.extend(
                            run_tests(workspace.path, project.tests, gate)
                            for gate in ("quick", "full")
                        )
                    for result in results:
                        store.write_json(task.id, f"doctor-{result.profile}.json", result)
                        table.add_row(
                            f"Fresh {result.profile}",
                            "PASS" if result.passed else "FAIL",
                            str(store.path(task.id)),
                        )
                    task.transition(
                        TaskStatus.PLANNED if all(r.passed for r in results) else TaskStatus.FAILED,
                        "Doctor validation complete; no implementation attempted",
                    )
                    if git.is_clean(workspace.path):
                        git.cleanup_task_workspace(workspace)
                        task.task_worktree = None
                except Exception as exc:
                    task.transition(TaskStatus.FAILED, str(exc))
                    raise
                finally:
                    store.save(task)
        except Exception as exc:
            table.add_row("Project", "FAIL", str(exc))

    console.print(table)
    console.print(f"Config: {user_config_dir(APP_NAME)}")
    console.print(f"State:  {user_state_dir(APP_NAME)}")
    console.print(f"Data:   {user_data_dir(APP_NAME)}")


@app.command()
def diagnostics() -> None:
    """Report executable, interpreter, module, distribution, and source identity."""
    console.print_json(data=installation_identity())


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
    test_env: Annotated[
        list[str] | None,
        typer.Option("--test-env", help="Test environment variable as KEY=VALUE; repeatable."),
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
            test_env=_parse_test_env(test_env),
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
            role: manager.status(config.profile(role)) for role in ("planner", "worker", "reviewer")
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
    capabilities: Annotated[
        bool, typer.Option(help="Probe real triage, edit, and review schemas.")
    ] = False,
    request_timeout: Annotated[int, typer.Option(min=1)] = 120,
) -> None:
    """Verify model loading and JSON-schema output."""
    config: GlobalConfig | None = None
    runtime = ExitStack()
    manager = ModelManager()
    try:
        config = _global()
        selected = config.profile(profile)
        runtime.enter_context(manager.lease())
        progress = _progress(progress_interval)
        with progress.operation(f"PROBE-{profile}", f"Preparing {selected.model}"):
            manager.ensure(selected)
        provider = MLXOpenAIProvider()
        if capabilities:
            from localdev_mlx.models.capabilities import probe_capabilities

            with progress.operation(
                f"PROBE-{profile}", f"Three capability probes; deadline {request_timeout}s each"
            ):
                results = probe_capabilities(selected, provider, timeout=request_timeout)
            target = ModelManager().root / f"capabilities-{selected.name}.json"
            target.write_text(json.dumps(results, indent=2), encoding="utf-8")
            console.print_json(data=results)
            if not all(result["passed"] for result in results):
                raise typer.Exit(1)
            return
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
            _cleanup_models(config, keep_model_loaded=keep_model_loaded, manager=manager)
        runtime.close()


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
    no_integrate: Annotated[
        bool, typer.Option(help="Leave the approved commit on its task branch.")
    ] = False,
    frontier_only: Annotated[
        bool,
        typer.Option(
            "--frontier-only",
            "--escalate-now",
            help="Record this issue for later frontier review without invoking local models.",
        ),
    ] = False,
    direct: Annotated[
        bool,
        typer.Option(
            "--direct",
            help="Skip planner inference and use one work unit authorized by --allow paths.",
        ),
    ] = False,
    allow_paths: Annotated[
        list[str] | None,
        typer.Option(
            "--allow",
            help="Repository-relative path the direct worker may edit; repeat as needed.",
        ),
    ] = None,
    read_paths: Annotated[
        list[str] | None,
        typer.Option(
            "--read",
            help="Additional repository-relative path supplied as direct worker context.",
        ),
    ] = None,
    keep_model_loaded: Annotated[bool, typer.Option("--keep-model-loaded")] = False,
    progress_interval: Annotated[float, typer.Option("--progress-interval", min=5)] = 30,
    max_attempts: Annotated[int | None, typer.Option(min=1, max=4)] = None,
    no_fallback: Annotated[bool, typer.Option()] = False,
    request_timeout: Annotated[int | None, typer.Option(min=1)] = None,
    task_timeout: Annotated[int | None, typer.Option(min=1)] = None,
    plan_only: Annotated[bool, typer.Option("--plan-only", "--dry-run")] = False,
    test_commands: Annotated[
        list[str] | None,
        typer.Option("--test", help="Targeted acceptance command; full gate still required."),
    ] = None,
) -> None:
    """Triage, implement, test, and review a bug fix."""
    _run_maintenance(
        kind=TaskKind.BUG,
        max_attempts=max_attempts,
        no_fallback=no_fallback,
        request_timeout=request_timeout,
        task_timeout=task_timeout,
        plan_only=plan_only,
        test_commands=test_commands,
        description=description,
        repo=repo,
        depth=depth,
        no_integrate=no_integrate,
        progress_interval=progress_interval,
        keep_model_loaded=keep_model_loaded,
        frontier_only=frontier_only,
        direct=direct,
        allow_paths=allow_paths,
        read_paths=read_paths,
    )


@app.command()
def feature(
    description: Annotated[str, typer.Argument(help="Feature behavior and acceptance criteria.")],
    repo: Annotated[Path, typer.Option("--repo")] = Path("."),
    depth: Annotated[str, typer.Option(help="fast, balanced, or deep")] = "balanced",
    no_integrate: Annotated[
        bool, typer.Option(help="Leave the approved commit on its task branch.")
    ] = False,
    frontier_only: Annotated[
        bool,
        typer.Option(
            "--frontier-only",
            "--escalate-now",
            help="Record this issue for later frontier review without invoking local models.",
        ),
    ] = False,
    direct: Annotated[
        bool,
        typer.Option(
            "--direct",
            help="Skip planner inference and use one work unit authorized by --allow paths.",
        ),
    ] = False,
    allow_paths: Annotated[
        list[str] | None,
        typer.Option(
            "--allow",
            help="Repository-relative path the direct worker may edit; repeat as needed.",
        ),
    ] = None,
    read_paths: Annotated[
        list[str] | None,
        typer.Option(
            "--read",
            help="Additional repository-relative path supplied as direct worker context.",
        ),
    ] = None,
    keep_model_loaded: Annotated[bool, typer.Option("--keep-model-loaded")] = False,
    progress_interval: Annotated[float, typer.Option("--progress-interval", min=5)] = 30,
    max_attempts: Annotated[int | None, typer.Option(min=1, max=4)] = None,
    no_fallback: Annotated[bool, typer.Option()] = False,
    request_timeout: Annotated[int | None, typer.Option(min=1)] = None,
    task_timeout: Annotated[int | None, typer.Option(min=1)] = None,
    plan_only: Annotated[bool, typer.Option("--plan-only", "--dry-run")] = False,
    test_commands: Annotated[list[str] | None, typer.Option("--test")] = None,
) -> None:
    """Implement a bounded feature through the local review loop."""
    _run_maintenance(
        kind=TaskKind.FEATURE,
        max_attempts=max_attempts,
        no_fallback=no_fallback,
        request_timeout=request_timeout,
        task_timeout=task_timeout,
        plan_only=plan_only,
        test_commands=test_commands,
        description=description,
        repo=repo,
        depth=depth,
        no_integrate=no_integrate,
        progress_interval=progress_interval,
        keep_model_loaded=keep_model_loaded,
        frontier_only=frontier_only,
        direct=direct,
        allow_paths=allow_paths,
        read_paths=read_paths,
    )


@app.command()
def tweak(
    description: Annotated[str, typer.Argument(help="Small requested adjustment.")],
    repo: Annotated[Path, typer.Option("--repo")] = Path("."),
    depth: Annotated[str, typer.Option(help="fast, balanced, or deep")] = "fast",
    no_integrate: Annotated[
        bool, typer.Option(help="Leave the approved commit on its task branch.")
    ] = False,
    frontier_only: Annotated[
        bool,
        typer.Option(
            "--frontier-only",
            "--escalate-now",
            help="Record this issue for later frontier review without invoking local models.",
        ),
    ] = False,
    direct: Annotated[
        bool,
        typer.Option(
            "--direct",
            help="Skip planner inference and use one work unit authorized by --allow paths.",
        ),
    ] = False,
    allow_paths: Annotated[
        list[str] | None,
        typer.Option(
            "--allow",
            help="Repository-relative path the direct worker may edit; repeat as needed.",
        ),
    ] = None,
    read_paths: Annotated[
        list[str] | None,
        typer.Option(
            "--read",
            help="Additional repository-relative path supplied as direct worker context.",
        ),
    ] = None,
    keep_model_loaded: Annotated[bool, typer.Option("--keep-model-loaded")] = False,
    progress_interval: Annotated[float, typer.Option("--progress-interval", min=5)] = 30,
    max_attempts: Annotated[int | None, typer.Option(min=1, max=4)] = None,
    no_fallback: Annotated[bool, typer.Option()] = False,
    request_timeout: Annotated[int | None, typer.Option(min=1)] = None,
    task_timeout: Annotated[int | None, typer.Option(min=1)] = None,
    plan_only: Annotated[bool, typer.Option("--plan-only", "--dry-run")] = False,
    test_commands: Annotated[list[str] | None, typer.Option("--test")] = None,
) -> None:
    """Apply a narrow project tweak through the same safety gates."""
    _run_maintenance(
        kind=TaskKind.TWEAK,
        max_attempts=max_attempts,
        no_fallback=no_fallback,
        request_timeout=request_timeout,
        task_timeout=task_timeout,
        plan_only=plan_only,
        test_commands=test_commands,
        description=description,
        repo=repo,
        depth=depth,
        no_integrate=no_integrate,
        progress_interval=progress_interval,
        keep_model_loaded=keep_model_loaded,
        frontier_only=frontier_only,
        direct=direct,
        allow_paths=allow_paths,
        read_paths=read_paths,
    )


@app.command()
def status(repo: Annotated[Path, typer.Option("--repo")] = Path(".")) -> None:
    """List local outcomes and unresolved frontier-review state."""
    try:
        root = _repository(repo)
        store = TaskStore(root)
        tasks = store.list()
        table = Table(title=f"LocalDev tasks — {root.name}")
        for column in ("ID", "Kind", "Local outcome", "Frontier", "Category", "Updated"):
            table.add_column(column)
        for task in tasks:
            table.add_row(
                task.id,
                task.kind.value,
                task.status.value,
                task.external_review_state.value,
                (task.failure_category or task.external_review_category).value
                if (task.failure_category or task.external_review_category)
                else "",
                task.updated_at.isoformat(timespec="seconds"),
            )
        console.print(table)
        open_count = len(store.open_external())
        integrated_count = sum(task.status == TaskStatus.INTEGRATED for task in tasks)
        console.print(
            f"Integrated local tasks: {integrated_count} | Open frontier items: {open_count}"
        )
        if open_count:
            console.print(
                "Next: localdev-mlx frontier status --repo PATH, then "
                "localdev-mlx frontier bundle --repo PATH",
                markup=False,
            )
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
            TaskStatus.DEFERRED,
            TaskStatus.ESCALATED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.PLANNED,
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
@app.command("explain-task")
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


@app.command()
def cancel(
    task_id: Annotated[str, typer.Argument()],
    repo: Annotated[Path, typer.Option("--repo")] = Path("."),
) -> None:
    """Request cooperative cancellation; artifacts survive and no unrelated PID is signalled."""
    try:
        store = TaskStore(_repository(repo))
        task = store.load(task_id)
        if task.local_outcome is not None:
            console.print(f"Task already stopped: {task.status.value}")
            return
        store.write_text(task_id, "cancel.request", "Cancelled by user\n")
        console.print(f"Cancellation requested for {task_id}; inspect with show-task.")
    except Exception as exc:
        _handle_error(exc)


@app.command()
def cleanup(
    task_id: Annotated[str, typer.Argument()],
    repo: Annotated[Path, typer.Option("--repo")] = Path("."),
    force: Annotated[
        bool, typer.Option(help="Remove uncommitted work in this exact task worktree.")
    ] = False,
) -> None:
    """Remove a stopped task's registered worktree; preserve task artifacts and unmerged commits."""
    from localdev_mlx.git import TaskWorkspace

    try:
        git = GitRepository(repo)
        store = TaskStore(git.root)
        task = store.load(task_id)
        if task.local_outcome is None:
            raise ValueError("Task is not terminal; cancel it first")
        if not task.task_worktree or not Path(task.task_worktree).exists():
            console.print("No remaining task worktree.")
            return
        config = load_project_config(git.root)
        workspace = TaskWorkspace(
            task.id,
            task.task_branch,
            Path(task.task_worktree),
            git.ensure_integration_worktree(config),
            config.integration_branch,
            task.base_commit,
        )
        store.write_text(task.id, "cleanup-preserved.patch", git.diff(workspace.path))
        git.cleanup_task_workspace(workspace, force=force)
        task.task_worktree = None
        store.save(task)
        console.print(
            f"Removed task worktree. Artifacts and cleanup-preserved.patch: {store.path(task.id)}"
        )
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


@frontier_app.command("defer")
def frontier_defer(
    kind: Annotated[
        Literal["bug", "feature", "tweak", "audit"],
        typer.Argument(help="Type of work being deferred."),
    ],
    description: Annotated[str, typer.Argument(help="Issue or work request to record.")],
    repo: Annotated[Path, typer.Option("--repo")] = Path("."),
    reason: Annotated[
        str | None,
        typer.Option("--reason", help="Why this should wait for frontier review."),
    ] = None,
) -> None:
    """Record an issue for frontier review without loading a local model."""
    try:
        task = defer_task(
            repository=repo,
            kind=TaskKind(kind),
            description=description,
            reason=reason,
        )
        _print_task_result(task)
    except Exception as exc:
        _handle_error(exc)


@frontier_app.command("defer-file")
def frontier_defer_file(
    path: Annotated[
        Path,
        typer.Argument(help="JSON file containing an array of deferred issue objects."),
    ],
    repo: Annotated[Path, typer.Option("--repo")] = Path("."),
) -> None:
    """Record several issues for one later frontier session without loading a model."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_issues = payload.get("issues") if isinstance(payload, dict) else payload
        if not isinstance(raw_issues, list) or not raw_issues:
            raise ValueError("Deferred issue JSON must be a non-empty array or {'issues': [...]}")
        issues = [DeferredIssue.model_validate(item) for item in raw_issues]
        tasks = [
            defer_task(
                repository=repo,
                kind=TaskKind(issue.kind),
                description=issue.description,
                reason=issue.reason,
            )
            for issue in issues
        ]
        console.print(
            Panel(
                "Recorded without local inference:\n"
                + "\n".join(f"- {task.id}: {task.description}" for task in tasks)
                + "\n\nCreate one combined prompt with:\n"
                + f"localdev-mlx frontier bundle --repo {_repository(repo)}",
                title="Frontier backlog updated",
            )
        )
    except Exception as exc:
        _handle_error(exc)


@frontier_app.command("status")
def frontier_status(
    repo: Annotated[Path, typer.Option("--repo")] = Path("."),
    all_items: Annotated[
        bool,
        typer.Option("--all", help="Include resolved and superseded external items."),
    ] = False,
) -> None:
    """List unresolved external items and existing frontier batches."""
    try:
        root = _repository(repo)
        store = TaskStore(root)
        tasks = (
            [
                task
                for task in store.list()
                if task.external_review_state != ExternalReviewState.NONE
            ]
            if all_items
            else store.open_external()
        )
        table = Table(
            title=("All frontier items" if all_items else "Open frontier items") + f" — {root.name}"
        )
        for column in ("ID", "Kind", "Outcome", "State", "Category", "Batches"):
            table.add_column(column)
        for task in tasks:
            table.add_row(
                task.id,
                task.kind.value,
                task.status.value,
                task.external_review_state.value,
                task.external_review_category.value if task.external_review_category else "",
                ", ".join(task.frontier_batch_ids),
            )
        console.print(table)

        batches = FrontierStore(root).list()
        batch_table = Table(title="Frontier batches")
        for column in ("ID", "Status", "Snapshot", "Tasks", "Visible path"):
            batch_table.add_column(column)
        for batch in batches:
            batch_table.add_row(
                batch.id,
                batch.status.value,
                batch.integration_commit[:12],
                str(len(batch.task_ids)),
                batch.visible_path or batch.path,
            )
        console.print(batch_table)
        counts = {
            state: sum(task.external_review_state == state for task in store.list())
            for state in ExternalReviewState
            if state != ExternalReviewState.NONE
        }
        console.print(
            "Frontier states: "
            + " | ".join(f"{state.value}={count}" for state, count in counts.items())
        )
        if tasks:
            console.print(
                "Create one combined handoff with: localdev-mlx frontier bundle --repo PATH",
                markup=False,
            )
    except Exception as exc:
        _handle_error(exc)


@frontier_app.command("bundle")
def frontier_bundle(
    repo: Annotated[Path, typer.Option("--repo")] = Path("."),
    task: Annotated[
        list[str] | None,
        typer.Option("--task", help="Include only this open task; repeatable."),
    ] = None,
    include_integrated: Annotated[
        bool,
        typer.Option(
            "--include-integrated/--issues-only",
            help="Include the base-to-integration diff and integrated-task index.",
        ),
    ] = True,
    destination: Annotated[
        Path | None,
        typer.Option("--to", help="Optional additional exported copy."),
    ] = None,
) -> None:
    """Combine unresolved issues and latest integration state for one frontier session."""
    try:
        batch = build_frontier_batch(
            repository=repo,
            task_ids=task,
            include_integrated=include_integrated,
            destination=destination,
        )
        console.print(
            Panel(
                f"Batch: {batch.id}\n"
                f"Snapshot: {batch.integration_commit}\n"
                f"Open tasks: {len(batch.task_ids)}\n"
                f"Canonical bundle: {batch.path}\n"
                f"Project-visible copy: {batch.visible_path}\n"
                f"Prompt: {Path(batch.visible_path or batch.path) / 'FRONTIER_BATCH_PROMPT.md'}",
                title="Frontier batch ready",
            )
        )
    except Exception as exc:
        _handle_error(exc)


@frontier_app.command("resolve")
def frontier_resolve(
    repo: Annotated[Path, typer.Option("--repo")] = Path("."),
    batch: Annotated[str | None, typer.Option("--batch")] = None,
    task: Annotated[
        list[str] | None,
        typer.Option("--task", help="Resolved task ID; repeatable."),
    ] = None,
    commit: Annotated[
        str,
        typer.Option("--commit", help="Commit reachable from the integration branch."),
    ] = "HEAD",
    note: Annotated[str | None, typer.Option("--note")] = None,
) -> None:
    """Mark frontier items resolved after fixes are committed to ai/integration."""
    try:
        resolved_commit, tasks, batch_record = resolve_frontier(
            repository=repo,
            batch_id=batch,
            task_ids=task,
            commit=commit,
            note=note,
        )
        console.print(
            Panel(
                f"Resolved commit: {resolved_commit}\n"
                f"Tasks: {', '.join(item.id for item in tasks)}\n"
                f"Batch: {batch_record.id if batch_record else '-'}\n"
                "New LocalDev tasks will start from this current integration state.",
                title="Frontier work recorded",
            )
        )
    except Exception as exc:
        _handle_error(exc)


@frontier_app.command("reopen")
def frontier_reopen(
    task: Annotated[
        list[str],
        typer.Option("--task", help="Task ID to reopen; repeatable."),
    ],
    repo: Annotated[Path, typer.Option("--repo")] = Path("."),
    reason: Annotated[str | None, typer.Option("--reason")] = None,
) -> None:
    """Return previously resolved tasks to the open frontier backlog."""
    try:
        tasks = reopen_frontier(repository=repo, task_ids=task, reason=reason)
        console.print("Reopened: " + ", ".join(item.id for item in tasks))
    except Exception as exc:
        _handle_error(exc)


@frontier_app.command("supersede")
def frontier_supersede(
    task: Annotated[
        list[str],
        typer.Option("--task", help="Obsolete or duplicate task ID; repeatable."),
    ],
    repo: Annotated[Path, typer.Option("--repo")] = Path("."),
    commit: Annotated[
        str | None,
        typer.Option(
            "--commit",
            help="Optional replacing commit already reachable from ai/integration.",
        ),
    ] = None,
    reason: Annotated[
        str,
        typer.Option("--reason", help="Why these external items are obsolete."),
    ] = "Replaced by later verified work.",
) -> None:
    """Close duplicate external items after later work makes them obsolete."""
    try:
        resolved_commit, tasks = supersede_frontier(
            repository=repo,
            task_ids=task,
            commit=commit,
            note=reason,
        )
        console.print(
            Panel(
                f"Superseded tasks: {', '.join(item.id for item in tasks)}\n"
                f"Replacing commit: {resolved_commit or '-'}\n"
                f"Reason: {reason}",
                title="Frontier items superseded",
            )
        )
    except Exception as exc:
        _handle_error(exc)


@frontier_app.command("sync")
def frontier_sync(
    source: Annotated[
        str,
        typer.Option(
            "--from",
            help="Branch or commit containing external fixes; must fast-forward integration.",
        ),
    ] = "main",
    repo: Annotated[Path, typer.Option("--repo")] = Path("."),
) -> None:
    """Fast-forward ai/integration to externally committed work without discarding changes."""
    try:
        before, after = sync_frontier_ref(repository=repo, source_ref=source)
        console.print(
            Panel(
                f"Previous integration commit: {before}\n"
                f"Current integration commit:  {after}\n"
                f"Source: {source}\n"
                "Run configured tests, then use 'frontier resolve' for the fixed task IDs.",
                title="Frontier work synchronized",
            )
        )
    except Exception as exc:
        _handle_error(exc)


@app.command("run-queue")
def run_queue(
    repo: Annotated[Path, typer.Option("--repo")] = Path("."),
    max_tasks: Annotated[int, typer.Option("--max-tasks", min=0)] = 1,
    depth: Annotated[str, typer.Option(help="fast, balanced, or deep")] = "balanced",
    continue_on_escalation: Annotated[
        bool,
        typer.Option(
            "--continue-on-escalation/--stop-on-escalation",
            help="Continue with independent queue tasks after an item is deferred or escalated.",
        ),
    ] = True,
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
        results = workflow.run(
            repository=repo,
            max_tasks=max_tasks,
            continue_on_escalation=continue_on_escalation,
        )
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
            raise RuntimeError(
                f"Preserving existing directory {target}; choose a new disposable sample path"
            )
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
        subprocess.run(
            ["git", "-C", str(target), "config", "user.name", "LocalDev Sample"], check=True
        )
        subprocess.run(
            ["git", "-C", str(target), "config", "user.email", "sample@local.invalid"], check=True
        )
        subprocess.run(["git", "-C", str(target), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(target), "commit", "-m", "test: create broken sample"],
            check=True,
            capture_output=True,
        )
        root, _ = initialize_project(
            target,
            quick_tests=["python3 -m unittest discover -s tests -v"],
            full_tests=["python3 -m unittest discover -s tests -v"],
            test_env={"PYTHONPATH": "src"},
        )
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-m", "chore: initialize LocalDev MLX"],
            check=True,
            capture_output=True,
        )
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
