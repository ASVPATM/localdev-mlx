"""Explicit offline MLX validation. Uses configured weights; preserves preexisting residency."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path

from localdev_mlx.config import load_global_config
from localdev_mlx.flow.config import Config
from localdev_mlx.flow.engine import Flow
from localdev_mlx.flow.localdev_host import localdev_host, selected_model
from localdev_mlx.models.manager import ModelManager


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--allow-load",
        action="store_true",
        help="Authorize this check to load existing cached worker weights offline.",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    config, manager = load_global_config(), ModelManager()
    with manager.lease():
        profile, resident = selected_model(manager, config)
        if not resident and not args.allow_load:
            raise SystemExit(
                "No verified resident model. Use --allow-load only to authorize cached local weights."
            )
    root = Path(tempfile.mkdtemp(prefix="flow-real-mlx-")).resolve()
    flow = Flow(
        Config(root, preview_kind="command"), host=localdev_host(root, allow_load=args.allow_load)
    )
    flow.save_issues(
        [
            {
                "id": "save-feedback",
                "prompt": "Show a clear confirmation after saving a note; keep the text intact.",
                "selection_ids": [],
            },
            {
                "id": "search-titles",
                "prompt": "Add case-insensitive title search and a helpful empty result message.",
                "selection_ids": [],
            },
        ]
    )
    report = {
        "project": str(root),
        "model": profile.model,
        "initially_resident": resident,
        "exports": [],
    }
    started_by_check = False

    def stop_owned():
        with manager.lease():
            state = manager._read_state()
            if state and state.get("owner_pid") == os.getpid() and manager._owned(state):
                manager.stop()

    def export(label):
        started = time.monotonic()
        flow.exports.start()
        flow.exports.thread.join(35)
        job = flow.exports.view()
        if job["stage"] != "ready":
            raise RuntimeError(f"Export not ready: {job['stage']}")
        manifest = json.loads((Path(job["directory"]) / "manifest.json").read_text())
        assert len(manifest["issues"]) == 2
        result = {
            "case": label,
            "wall_seconds": round(time.monotonic() - started, 3),
            "organized": job["organized"],
            "organization": job["organization"],
            "advice_characters": len(json.dumps(manifest["advice"])),
            "directory": job["directory"],
        }
        report["exports"].append(result)
        print(json.dumps(result), flush=True)

    try:
        export("resident export" if resident else "cold helper, default 20-second budget")
        if flow.exports.model_thread:
            flow.exports.model_thread.join(12)
        if not resident:
            with manager.lease():
                profile, loaded = selected_model(manager, config)
                if not loaded:
                    previous = manager._read_state()
                    if previous and manager._owned(previous):
                        raise RuntimeError("Another model became resident; preserving it")
                    started = time.monotonic()
                    manager.ensure(config.worker)
                    report["explicit_preload_seconds"] = round(time.monotonic() - started, 3)
                    started_by_check = True
            # Change one revision deliberately so this checks real warm inference, not the cold cache.
            rows = flow.state["issues"]
            rows[1]["prompt"] += " Preserve the current sort order."
            flow.save_issues(rows)
            export("warm helper, changed batch")
        export("unchanged batch cache")
        if started_by_check and config.stop_after_run:
            stop_owned()
            started_by_check = False
            export("unchanged batch cache after unload")
    finally:
        flow.close()
        if started_by_check and config.stop_after_run:
            stop_owned()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    if not any(
        row["organized"] and not row["organization"].get("cached") for row in report["exports"]
    ):
        raise SystemExit("No real organized export passed; ordinary fallback results recorded")


if __name__ == "__main__":
    main()
