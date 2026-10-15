"""Public Flow command group; no Git or session store needed to capture/export."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from contextvars import ContextVar
from pathlib import Path
from typing import Annotated

import typer

from . import controller

app = typer.Typer(
    help="Capture changes and export evidence. Git/models are optional.",
    invoke_without_command=True,
)
Project = Annotated[
    Path | None,
    typer.Option("--project", help="Project directory (defaults to the current LocalDev project)."),
]
_project: ContextVar[Path | None] = ContextVar("flow_project", default=None)
_personal: ContextVar[bool] = ContextVar("flow_personal", default=False)
Personal = Annotated[bool, typer.Option("--personal", help="Use the isolated personal-data mode.")]
NoOpen = Annotated[
    bool, typer.Option("--no-open", help="Print the URL without opening a dashboard tab.")
]


def resolve_project(value):
    from localdev_mlx.cli import _current_repo

    selected = value if value is not None else _project.get()
    return selected if selected is not None else _current_repo.get()


def invoke(function, *args, **kwargs):
    try:
        result = function(*args, **kwargs)
        typer.echo(json.dumps(result, indent=2) if isinstance(result, dict) else result)
    except (ValueError, OSError) as exc:
        typer.echo(f"Flow: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.callback()
def flow(
    ctx: typer.Context, project: Project = None, no_open: NoOpen = False, personal: Personal = False
):
    token = _project.set(project)
    ctx.call_on_close(lambda: _project.reset(token))
    mode_token = _personal.set(personal)
    ctx.call_on_close(lambda: _personal.reset(mode_token))
    if ctx.invoked_subcommand is None:
        start(project, no_open=no_open, personal=personal)


@app.command()
def start(
    project: Project = None,
    no_open: NoOpen = False,
    personal: Personal = False,
    port: Annotated[int, typer.Option(min=0, max=65535)] = 0,
    allow_model_load: Annotated[
        bool | None,
        typer.Option(
            "--allow-model-load/--no-model-load",
            help="Save explicit helper-only offline loading consent; default off.",
        ),
    ] = None,
):
    """Start or reopen this project's dashboard; no preview/model starts yet."""
    invoke(
        controller.start,
        resolve_project(project),
        port=port,
        open_browser=not no_open,
        allow_load=allow_model_load,
        mode="personal" if personal or _personal.get() else "sample",
    )


@app.command()
def status(project: Project = None, personal: Personal = False):
    """Show the verified controller and dashboard URL."""
    invoke(
        controller.status,
        resolve_project(project),
        mode="personal" if personal or _personal.get() else "sample",
    )


@app.command()
def stop(project: Project = None, personal: Personal = False):
    """Close only this project's owned Flow processes; retain evidence."""
    invoke(
        controller.stop,
        resolve_project(project),
        mode="personal" if personal or _personal.get() else "sample",
    )


@app.command()
def doctor(project: Project = None, personal: Personal = False):
    """Check adapter and installed capabilities without loading a model."""
    invoke(
        controller.doctor,
        resolve_project(project),
        mode="personal" if personal or _personal.get() else "sample",
    )


@app.command("browser-setup")
def browser_setup():
    """Explicitly download Chromium after installing the flow-browser extra."""
    if importlib.util.find_spec("playwright") is None:
        typer.echo(
            'Install "localdev-mlx[flow-browser]" first; text-only Flow needs no extra.', err=True
        )
        raise typer.Exit(1)
    result = subprocess.run(
        [sys.executable, "-I", "-m", "playwright", "install", "chromium"], check=False
    )
    raise typer.Exit(result.returncode)


@app.command("init")
def init_adapter(project: Project = None, demo: bool = False):
    """Save a detected adapter, write a manual template, or copy a demo into an empty folder."""

    def create():
        root = controller.project_path(resolve_project(project))
        if demo:
            if any(root.iterdir()):
                raise ValueError(
                    "The demo needs an empty directory; existing files are never overwritten"
                )
            shutil.copytree(Path(__file__).parent / "examples/web", root, dirs_exist_ok=True)
        else:
            from .detection import adapter_document, detect_config

            detected = detect_config(root)
            with (root / "project-flow.toml").open("x") as handle:
                handle.write(
                    adapter_document(detected)
                    if detected
                    else (Path(__file__).parent / "examples/manual/project-flow.toml").read_text()
                )
        ignore = root / ".gitignore"
        if ignore.is_symlink():
            raise ValueError("Add .project-flow/ to your .gitignore manually; symlink preserved")
        from .workspace import atomic_write

        original = ignore.read_text() if ignore.exists() else ""
        if ".project-flow/" not in original.splitlines():
            atomic_write(ignore, (original.rstrip() + "\n.project-flow/\n").encode())
        return "Adapter created. Review project-flow.toml before Open test app; synthetic commands run with your OS privileges."

    invoke(create)
