from __future__ import annotations

import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import localdev_mlx


def installation_identity() -> dict:
    module = Path(localdev_mlx.__file__).resolve()
    try:
        distribution = version("localdev-mlx")
    except PackageNotFoundError:
        distribution = None
    commit = None
    root = module.parent.parent.parent
    if (root / ".git").exists() and (root / "pyproject.toml").exists():
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        commit = result.stdout.strip() if result.returncode == 0 else None
    return {
        "executable": shutil.which("localdev-mlx"),
        "interpreter": sys.executable,
        "distribution_version": distribution,
        "module_version": localdev_mlx.__version__,
        "module_path": str(module),
        "source_commit": commit,
        "versions_agree": distribution == localdev_mlx.__version__,
        "capabilities": [
            "task-schema-v2",
            "bounded-attempts",
            "context-manifests",
            "verified-process-ownership",
            "fresh-worktree-preparation",
        ],
    }
