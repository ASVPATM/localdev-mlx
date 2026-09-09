#!/usr/bin/env python3
"""Opt-in, bounded Apple Silicon validation in a new disposable project."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import replace
from pathlib import Path

from typer.testing import CliRunner

from localdev_mlx.cli import app
from localdev_mlx.config import load_global_config
from localdev_mlx.diagnostics import installation_identity
from localdev_mlx.git import GitRepository
from localdev_mlx.models import ModelManager
from localdev_mlx.models.capabilities import probe_capabilities
from localdev_mlx.progress import ProgressReporter
from localdev_mlx.project import initialize_project
from localdev_mlx.providers import MLXOpenAIProvider
from localdev_mlx.schemas import TaskKind
from localdev_mlx.sessions import SessionStore
from localdev_mlx.workflows.session_plan import full_plan
from localdev_mlx.workflows.task_runner import TaskRunner


def create_validation_project(root: Path) -> None:
    """Private test fixture, not a public sample command."""
    (root / "src/samplecalc").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src/samplecalc/__init__.py").write_text("from .core import add, subtract\n")
    (root / "src/samplecalc/core.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef subtract(a, b):\n    return a + b  # intentional demo bug\n"
    )
    (root / "tests/test_core.py").write_text(
        "import unittest\nfrom samplecalc import add, subtract\n\n"
        "class Tests(unittest.TestCase):\n"
        "    def test_add(self): self.assertEqual(add(2, 3), 5)\n"
        "    def test_subtract(self): self.assertEqual(subtract(7, 2), 5)\n"
    )
    (root / "README.md").write_text("# Calculator validation fixture\n")
    GitRepository._run(root, ["init", "-b", "main"])
    GitRepository._run(root, ["config", "user.name", "LocalDev Validation"])
    GitRepository._run(root, ["config", "user.email", "localdev-validation@example.invalid"])
    initialize_project(
        root,
        quick_tests=["python3 -m unittest discover -s tests -v"],
        full_tests=["python3 -m unittest discover -s tests -v"],
        test_env={"PYTHONPATH": "src"},
        commit=True,
        switch_integration=False,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New directory for disposable project and measured results.",
    )
    parser.add_argument("--request-timeout", type=int, default=180)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"identity": installation_identity(), "probes": [], "plan": None, "direct": None}
    report_path = args.output / "results.json"

    def save():
        report_path.write_text(json.dumps(report, indent=2, default=str))

    config = load_global_config(args.config)
    # Tiny functional checks disable thinking and bound output, retaining model IDs/server args.
    config = replace(
        config,
        models={
            name: replace(
                profile,
                enable_thinking=False,
                thinking_budget=0,
                max_tokens=1800,
                startup_timeout_seconds=120,
                request_timeout_seconds=args.request_timeout,
            )
            for name, profile in config.models.items()
        },
    )
    manager = ModelManager()
    provider = MLXOpenAIProvider()
    save()
    try:
        with manager.lease():
            for name, profile in config.models.items():
                print(f"Capability probes: {name} / {profile.model}", flush=True)
                manager.ensure(profile)
                report["probes"].extend(
                    probe_capabilities(profile, provider, timeout=args.request_timeout)
                )
                save()
                manager.stop()
                print(
                    f"Completed {name}; stopped={not manager.status().get('running')}", flush=True
                )
        sample = args.output / "sample"
        create_validation_project(sample)
        deferred = CliRunner().invoke(
            app,
            [
                "feature",
                "Consider optional multiplication in a future pass; preserve addition and subtraction.",
                "--repo",
                str(sample),
            ],
        )
        if deferred.exit_code:
            raise RuntimeError(deferred.stdout)
        store = SessionStore(sample)
        started = time.monotonic()
        answers = []

        def answer(question):
            # Explicit synthetic-user answers: exercise real question generation without
            # pretending these came from the user or waiting on an unattended terminal.
            if len(answers) >= 12:
                return None
            value = (
                "Validation fixture preference: one user, offline Python standard library only; "
                "preserve the existing add/subtract interface and unittest suite. No new UI, "
                "cloud services, persistence, packaging, or multiplication in this request. "
                "Success means subtraction is correct and addition still passes. "
                "Implement the small scope end to end, with no phased rollout."
            )
            answers.append({"question": question, "answer": value})
            print(f"Synthetic-user clarification: {question}", flush=True)
            return value

        with store.operation(
            "plan",
            "Suggest a flexible approach for maintaining this small calculator CLI; propose tests and note open questions. Do not implement anything.",
            "full",
        ) as (session, entry):
            full_plan(
                store,
                session,
                entry,
                config=config,
                provider=provider,
                manager=manager,
                request_timeout=args.request_timeout,
                progress=lambda message: print(message, flush=True),
                ask=answer,
            )
        report["plan"] = {
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "entry": entry,
            "synthetic_user_answers": answers,
        }
        save()
        started = time.monotonic()
        with store.operation(
            "bug",
            "subtract(7, 2) returns 9 instead of 5. Fix subtraction while preserving addition.",
            "local",
        ) as (session, entry):
            task = TaskRunner(
                global_config=config,
                provider=provider,
                model_manager=manager,
                depth="fast",
                request_timeout=args.request_timeout,
                task_timeout=600,
                max_attempts=2,
                progress=ProgressReporter(
                    callback=lambda text: print(text, flush=True), heartbeat_seconds=15
                ),
                on_update=lambda task: store.record_task(session, entry, task),
                cancelled=lambda: store.cancel_path(session, entry).exists(),
            ).run(
                repository=sample,
                kind=TaskKind.BUG,
                description=entry["description"],
                direct_allowed_paths=["src/samplecalc/core.py"],
                direct_read_paths=["tests/test_core.py"],
                direct_test_commands=["python3 -m unittest discover -s tests -v"],
            )
            entry["state"] = task.status.value
        report["direct"] = {
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "task": task.model_dump(mode="json"),
        }
        store.end()
        report["session"] = {
            "handoff": str(store.handoff(session)),
            "requests": len(session["entries"]),
            "handoff_chars": len(store.handoff(session).read_text()),
            "patch_exists": bool(entry.get("patch") and (sample / entry["patch"]).is_file()),
            "no_docs_hierarchy": not (sample / "docs/ai").exists(),
            "checkout_branch": GitRepository(sample).current_branch(),
            "no_external_branch_restriction": "Local work targets"
            not in store.handoff(session).read_text(),
        }
        save()
    finally:
        manager.stop()
        report["managed_server_stopped"] = not manager.status().get("running")
        save()
    passed = (
        all(p["passed"] for p in report["probes"])
        and report["direct"]["task"]["status"] == "integrated"
        and report["plan"]["entry"]["state"] == "ready for external implementation"
        and report["session"]["requests"] == 3
        and report["session"]["patch_exists"]
        and report["session"]["no_docs_hierarchy"]
        and report["session"]["checkout_branch"] == "main"
        and report["session"]["no_external_branch_restriction"]
        and report["managed_server_stopped"]
    )
    print(f"Real model validation passed={passed}; results={report_path}", flush=True)
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
