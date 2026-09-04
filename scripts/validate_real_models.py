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
from localdev_mlx.models import ModelManager
from localdev_mlx.models.capabilities import probe_capabilities
from localdev_mlx.progress import ProgressReporter
from localdev_mlx.providers import MLXOpenAIProvider
from localdev_mlx.schemas import TaskKind
from localdev_mlx.workflows.task_runner import TaskRunner


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
    report = {"identity": installation_identity(), "probes": [], "direct": None}
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
        created = CliRunner().invoke(app, ["sample", "create", str(sample)])
        if created.exit_code:
            raise RuntimeError(created.stdout)
        started = time.monotonic()
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
        ).run(
            repository=sample,
            kind=TaskKind.BUG,
            description="subtract(7, 2) returns 9 instead of 5. Fix subtraction while preserving addition.",
            direct_allowed_paths=["src/samplecalc/core.py"],
            direct_read_paths=["tests/test_core.py"],
            direct_test_commands=["python3 -m unittest discover -s tests -v"],
        )
        report["direct"] = {
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "task": task.model_dump(mode="json"),
        }
        save()
    finally:
        manager.stop()
        report["managed_server_stopped"] = not manager.status().get("running")
        save()
    passed = (
        all(p["passed"] for p in report["probes"])
        and report["direct"]["task"]["status"] == "integrated"
    )
    print(f"Real model validation passed={passed}; results={report_path}", flush=True)
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
