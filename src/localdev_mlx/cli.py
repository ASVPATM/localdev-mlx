from __future__ import annotations

import json
import re
import shlex
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from localdev_mlx import __version__
from localdev_mlx.config import load_global_config, write_global_config
from localdev_mlx.diagnostics import installation_identity
from localdev_mlx.git import GitRepository
from localdev_mlx.models import ModelManager
from localdev_mlx.models.capabilities import probe_capabilities
from localdev_mlx.progress import ProgressReporter
from localdev_mlx.project import initialize_project
from localdev_mlx.providers import MLXOpenAIProvider
from localdev_mlx.schemas import TaskKind, TaskStatus
from localdev_mlx.sessions import SessionStore
from localdev_mlx.workflows.session_plan import full_plan
from localdev_mlx.workflows.task_runner import TaskRunner

app = typer.Typer(
    name="localdev-mlx",
    help="One session, one handoff. Requests defer by default; --local opts into local code changes.",
    invoke_without_command=True,
    no_args_is_help=False,
    add_completion=False,
)
console = Console()
_current_repo: ContextVar[Path] = ContextVar("localdev_repository", default=Path("."))
_interactive_store: ContextVar[SessionStore | None] = ContextVar("localdev_shell", default=None)


def _error(exc: Exception) -> None:
    console.print(f"Error: {exc}", style="red", markup=False)
    raise typer.Exit(1) from exc


def _repo(path: Path | None = None) -> Path:
    if path is not None:
        return path
    return _current_repo.get()


def _progress() -> ProgressReporter:
    def report(message: str):
        # The engine retains internal recovery identifiers; users operate on the session.
        message = re.sub(r"\[[A-Z]+-\d{8}-\d{6}(?:-\d+)?\]", "[local]", message)
        console.print(f"{datetime.now():%H:%M:%S} {message}", markup=False)

    return ProgressReporter(callback=report, heartbeat_seconds=20)


def _show_session(store: SessionStore, session: dict) -> None:
    console.print(
        f"Session {session['number']:04d} ({'closed' if session['ended'] else 'open'}) — {len(session['entries'])} request(s)",
        markup=False,
    )
    for entry in session["entries"][-5:]:
        console.print(
            f"  {entry['number']}. {entry['kind']}: {entry['state']} — {entry['description'].splitlines()[0][:90]}",
            markup=False,
        )
    typer.echo(f"Handoff: {store.handoff(session)}")


@app.callback()
def root_callback(
    ctx: typer.Context,
    version: Annotated[
        bool, typer.Option("--version", is_eager=True, help="Show version and exit.")
    ] = False,
    repo: Annotated[Path, typer.Option("--repo", help="Project directory.")] = Path("."),
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()
    token = _current_repo.set(repo)
    ctx.call_on_close(lambda: _current_repo.reset(token))
    if ctx.invoked_subcommand is not None:
        return
    try:
        store = SessionStore(repo)
        with store.shell():
            session = store.start(new=True)
            shell_token = _interactive_store.set(store)
            _show_session(store, session)
            console.print(
                "Enter plan, bug, feature, tweak, or session; --help lists all eight commands. Ctrl-D ends the session. No shell commands are executed."
            )
            try:
                while True:
                    try:
                        line = input(f"localdev:{session['number']:04d}> ").strip()
                    except EOFError:
                        break
                    except KeyboardInterrupt:
                        console.print("Use Ctrl-D to end this session.")
                        continue
                    if not line:
                        continue
                    try:
                        args = shlex.split(line)
                        if args and args[0] == "localdev-mlx":
                            args = args[1:]
                        if not args:
                            continue
                        app(args=["--repo", str(store.repository), *args], standalone_mode=False)
                        session = store.current()
                        if session["ended"]:
                            break
                    except (typer.Exit, KeyboardInterrupt):
                        console.print("Operation stopped; its evidence remains in the handoff.")
                    except Exception as exc:
                        console.print(str(exc), style="red", markup=False)
            finally:
                _interactive_store.reset(shell_token)
                _show_session(store, store.end())
    except Exception as exc:
        _error(exc)


@app.command("init")
def init_project(
    repository: Annotated[
        Path | None, typer.Argument(help="New directory or clean Git project.")
    ] = None,
    quick_test: Annotated[
        list[str] | None, typer.Option("--quick-test", help="Quick test command; repeatable.")
    ] = None,
    full_test: Annotated[
        list[str] | None, typer.Option("--full-test", help="Full test command; repeatable.")
    ] = None,
    prepare: Annotated[
        list[str] | None,
        typer.Option("--prepare", help="Fresh-worktree setup command; repeatable."),
    ] = None,
    test_env: Annotated[
        list[str] | None, typer.Option("--test-env", help="Test variable KEY=VALUE; repeatable.")
    ] = None,
) -> None:
    """Create Git/initial commit if needed, commit minimal setup, and prepare for sessions."""
    try:
        environment = {}
        for value in test_env or []:
            key, separator, text = value.partition("=")
            if not key.strip() or not separator:
                raise ValueError("--test-env requires KEY=VALUE")
            environment[key.strip()] = text
        root, changed = initialize_project(
            _repo(repository),
            quick_tests=quick_test,
            full_tests=full_test,
            test_env=environment,
            prepare_commands=prepare,
            commit=True,
        )
        console.print(
            f"{'Prepared' if changed else 'Already initialized'}: {root}\nBranch: {GitRepository(root).current_branch()}\nRun localdev-mlx to open a session. No models or tests were run.",
            markup=False,
        )
    except Exception as exc:
        _error(exc)


@app.command()
def configure(
    planner_model: Annotated[
        str | None, typer.Argument(help="MLX planner model ID or local model directory.")
    ] = None,
    worker_model: Annotated[str | None, typer.Option("--worker-model")] = None,
    reviewer_model: Annotated[str | None, typer.Option("--reviewer-model")] = None,
    server: Annotated[
        Path | None, typer.Option("--server", help="Absolute path to mlx_vlm.server executable.")
    ] = None,
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8080,
    keep_model_loaded: Annotated[bool, typer.Option("--keep-model-loaded")] = False,
    thinking: Annotated[bool, typer.Option("--thinking/--no-thinking")] = False,
    thinking_budget: Annotated[int, typer.Option("--thinking-budget", min=0)] = 4096,
    max_tokens: Annotated[int, typer.Option("--max-tokens", min=256)] = 8192,
    server_arg: Annotated[list[str] | None, typer.Option("--server-arg")] = None,
    force: Annotated[bool, typer.Option(help="Replace saved model assignments.")] = False,
    check: Annotated[
        bool, typer.Option("--check", help="Show installation identity without loading a model.")
    ] = False,
) -> None:
    """Save optional local model configuration; deferring requests needs no models."""
    try:
        if check:
            if planner_model:
                raise ValueError("Use --check without a model ID")
            typer.echo(json.dumps(installation_identity(), indent=2))
            return
        if not planner_model:
            raise ValueError("Supply a planner model ID, or use --check")
        path = write_global_config(
            planner_model=planner_model,
            worker_model=worker_model,
            reviewer_model=reviewer_model,
            server_executable=server,
            port=port,
            stop_after_run=not keep_model_loaded,
            enable_thinking=thinking,
            thinking_budget=thinking_budget,
            max_tokens=max_tokens,
            server_args=tuple(server_arg or ()),
            force=force,
        )
        console.print(
            f"Saved {path}\nVerify each distinct model with: localdev-mlx model planner --probe",
            markup=False,
        )
    except Exception as exc:
        _error(exc)


@app.command()
def model(
    role: Annotated[str, typer.Argument(help="Configured role/profile.")] = "planner",
    load: Annotated[bool, typer.Option("--load", help="Load/switch the managed model.")] = False,
    stop: Annotated[
        bool, typer.Option("--stop", help="Unload only the LocalDev-owned server.")
    ] = False,
    probe: Annotated[
        bool, typer.Option("--probe", help="Validate triage, edit, and review JSON schemas.")
    ] = False,
    logs: Annotated[bool, typer.Option("--logs", help="Show recent managed-server logs.")] = False,
    request_timeout: Annotated[int, typer.Option("--request-timeout", min=1)] = 120,
) -> None:
    """Show model status, or load, stop, probe, or inspect logs using one flag."""
    try:
        if sum((load, stop, probe, logs)) > 1:
            raise ValueError("Choose only one of --load, --stop, --probe, --logs")
        manager = ModelManager()
        if logs:
            console.print(manager.log_tail(100), markup=False)
            return
        if stop:
            manager.stop()
            console.print(
                "LocalDev-managed server stopped. Downloaded weights and saved profiles remain."
            )
            return
        config = load_global_config()
        selected = config.profile(role)
        if not load and not probe:
            typer.echo(json.dumps(manager.status(selected), indent=2))
            return
        with manager.lease():
            try:
                with _progress().operation("model", f"Loading {selected.model}"):
                    manager.ensure(selected)
                if probe:
                    results = probe_capabilities(
                        selected, MLXOpenAIProvider(), timeout=request_timeout
                    )
                    typer.echo(json.dumps(results, indent=2))
                    if not all(result["passed"] for result in results):
                        raise typer.Exit(1)
                else:
                    console.print(f"Loaded {selected.model}", markup=False)
            finally:
                if probe and config.stop_after_run:
                    manager.stop()
    except typer.Exit:
        raise
    except Exception as exc:
        _error(exc)


@app.command()
def plan(
    description: Annotated[str, typer.Argument(help="Idea, goal, or planning request.")],
    mode: Annotated[
        str, typer.Option("--mode", help="light: no model; full: two provisional planning passes.")
    ] = "light",
    repo: Annotated[Path | None, typer.Option("--repo")] = None,
    request_timeout: Annotated[int, typer.Option("--request-timeout", min=1)] = 120,
) -> None:
    """Add a lightweight brief or a flexible, detailed local proposal to the handoff."""
    try:
        if mode not in {"light", "full"}:
            raise ValueError("--mode must be light or full")
        store = SessionStore(_repo(repo))
        with store.operation("plan", description, mode) as (session, entry):
            if mode == "full":
                full_plan(
                    store,
                    session,
                    entry,
                    config=load_global_config(),
                    provider=MLXOpenAIProvider(),
                    request_timeout=request_timeout,
                    progress=lambda message: console.print(message, markup=False),
                )
            else:
                entry["state"] = "ready for external planning"
                entry["note"] = (
                    "Establish requirements, propose a flexible approach, then implement and validate. No architecture decisions are fixed by this brief."
                )
        _show_session(store, session)
    except KeyboardInterrupt:
        raise typer.Exit(130)
    except Exception as exc:
        _error(exc)


def _register_maintenance(name: str) -> None:
    @app.command(
        name,
        help=f"Record a {name} in the session handoff by default. Use --local to attempt implementation.",
    )
    def maintenance(
        description: Annotated[
            str,
            typer.Argument(
                help="Specific observation/request, expected behavior, and reproduction if known."
            ),
        ],
        repo: Annotated[Path | None, typer.Option("--repo")] = None,
        local: Annotated[
            bool, typer.Option("--local", help="Opt into local implementation, tests, and review.")
        ] = False,
        escalate: Annotated[
            bool, typer.Option("--escalate", help="Explicitly request handoff only (the default).")
        ] = False,
        allow: Annotated[
            list[str] | None,
            typer.Option(
                "--allow",
                help="Exact writable file; repeat to skip planner triage. Requires --local.",
            ),
        ] = None,
        read: Annotated[
            list[str] | None, typer.Option("--read", help="Additional read file; requires --allow.")
        ] = None,
        no_integrate: Annotated[
            bool,
            typer.Option(
                "--no-integrate", help="Preserve an approved local branch for manual review."
            ),
        ] = False,
        max_attempts: Annotated[int | None, typer.Option("--max-attempts", min=1, max=4)] = None,
        no_fallback: Annotated[bool, typer.Option("--no-fallback")] = False,
        request_timeout: Annotated[int | None, typer.Option("--request-timeout", min=1)] = None,
        task_timeout: Annotated[int | None, typer.Option("--task-timeout", min=1)] = None,
    ) -> None:
        try:
            if local and escalate:
                raise ValueError("--local and --escalate are mutually exclusive")
            if not local and any(
                (
                    allow,
                    read,
                    no_integrate,
                    max_attempts,
                    no_fallback,
                    request_timeout,
                    task_timeout,
                )
            ):
                raise ValueError(
                    "Local execution options require --local; requests defer by default"
                )
            if read and not allow:
                raise ValueError("--read requires at least one --allow file")
            store = SessionStore(_repo(repo))
            failed = None
            with store.operation(name, description, "local" if local else "handoff") as (
                session,
                entry,
            ):
                if not local:
                    entry["state"] = "deferred"
                else:
                    git = GitRepository(store.repository)
                    if not git.is_clean():
                        raise ValueError(
                            "Commit or stash checkout changes before --local; the request is preserved in the handoff"
                        )
                    # External work needs no LocalDev-specific resolve/sync command. Adopt a
                    # newer reviewed base only when it is an ordinary, safe fast-forward.
                    integration = git.ensure_integration_worktree(store.config)
                    base = git.resolve_ref(git.root, store.config.base_branch)
                    current = git.resolve_ref(integration, "HEAD")
                    if not git.is_ancestor(git.root, base, current):
                        if not git.is_ancestor(git.root, current, base):
                            raise ValueError(
                                "Base and integration branches diverged. Reconcile them with Git before --local; no branches were overwritten"
                            )
                        git.fast_forward(integration, base)
                        entry["note"] = (
                            f"Adopted external base commit {base} by fast-forward before local inference."
                        )
                        store.save(session)
                    runner = TaskRunner(
                        global_config=load_global_config(),
                        provider=MLXOpenAIProvider(),
                        progress=_progress(),
                        max_attempts=max_attempts,
                        no_fallback=no_fallback,
                        request_timeout=request_timeout,
                        task_timeout=task_timeout,
                        on_update=lambda task: store.record_task(session, entry, task),
                        cancelled=lambda: store.cancel_path(session, entry).exists(),
                    )
                    task = runner.run(
                        repository=store.repository,
                        kind=TaskKind(name),
                        description=description,
                        auto_integrate=not no_integrate,
                        direct_allowed_paths=allow,
                        direct_read_paths=read,
                    )
                    entry["state"] = task.status.value
                    if task.status not in {TaskStatus.INTEGRATED, TaskStatus.APPROVED}:
                        failed = task.status
            _show_session(store, session)
            if failed:
                console.print(
                    "Local work stopped; the same handoff contains its evidence for external repair."
                )
                raise typer.Exit(130 if failed == TaskStatus.CANCELLED else 1)
        except typer.Exit:
            raise
        except Exception as exc:
            _error(exc)


for _kind in ("bug", "feature", "tweak"):
    _register_maintenance(_kind)


@app.command()
def session(
    repo: Annotated[Path | None, typer.Option("--repo")] = None,
    new: Annotated[
        bool, typer.Option("--new", help="Close the previous session and begin a numbered handoff.")
    ] = False,
    end: Annotated[
        bool,
        typer.Option("--end", help="End the current shell-command session; preserve its handoff."),
    ] = False,
    cancel: Annotated[
        bool,
        typer.Option("--cancel", help="Cancel the single running operation; retain its evidence."),
    ] = False,
    show: Annotated[bool, typer.Option("--show", help="Print the current handoff.")] = False,
) -> None:
    """Show current progress/handoff, start or end a session, or cancel current work. No task IDs."""
    try:
        if sum((new, end, cancel, show)) > 1:
            raise ValueError("Choose only one of --new, --end, --cancel, --show")
        store = SessionStore(_repo(repo))
        interactive = _interactive_store.get()
        if interactive is not None and interactive.repository == store.repository:
            store = interactive
        if cancel:
            store.cancel()
            console.print(
                "Cancellation requested. Wait for the running operation to stop; evidence is preserved."
            )
            return
        current = store.start(new=True) if new else store.end() if end else store.inspect()
        if not current:
            console.print(
                "No session yet. Run plan, bug, feature, or tweak to start one automatically."
            )
            return
        if show:
            typer.echo(store.render(current))
        else:
            _show_session(store, current)
    except Exception as exc:
        _error(exc)
