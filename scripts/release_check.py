#!/usr/bin/env python3
"""Validate source and the exact wheel in isolated environments; never tag/publish."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def run(args, *, cwd, env):
    print("+ " + " ".join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), cwd=cwd, env=env, check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Development check only; release requires clean tracked files.",
    )
    parser.add_argument(
        "--artifacts", type=Path, help="Copy the verified wheel and sdist to this directory."
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    if not args.allow_dirty:
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=no"], cwd=root, text=True
        )
        if dirty.strip():
            raise SystemExit("Commit tracked changes before release validation")
    with tempfile.TemporaryDirectory(prefix="localdev-release-") as temporary:
        temp = Path(temporary)
        artifact_dir = args.artifacts.resolve() if args.artifacts else None
        if not args.allow_dirty:
            checkout = temp / "checkout"
            checkout.mkdir()
            archive = subprocess.check_output(["git", "archive", "HEAD"], cwd=root)
            subprocess.run(["tar", "-x", "-C", str(checkout)], input=archive, check=True)
            root = checkout
        env["LOCALDEV_MLX_HOME"] = str(temp / "runtime")
        run(["uv", "sync", "--locked", "--group", "dev"], cwd=root, env=env)
        for command in (
            ["uv", "run", "ruff", "check", "."],
            ["uv", "run", "pytest", "-o", "addopts=", "-q", "-ra"],
            ["uv", "run", "python", "-m", "compileall", "-q", "src", "tests"],
            ["uv", "build", "--out-dir", temp / "dist"],
        ):
            run(command, cwd=root, env=env)
        wheel = next((temp / "dist").glob("*.whl"))
        run(["uv", "venv", "--python", sys.executable, temp / "wheel-env"], cwd=root, env=env)
        python = temp / "wheel-env/bin/python"
        cli = temp / "wheel-env/bin/localdev-mlx"
        run(["uv", "pip", "install", "--python", python, wheel, "pytest>=8,<10"], cwd=root, env=env)
        identity = subprocess.check_output(
            [
                python,
                "-I",
                "-c",
                "import json; from localdev_mlx.diagnostics import installation_identity; print(json.dumps(installation_identity()))",
            ],
            cwd=temp,
            env=env,
            text=True,
        )
        values = json.loads(identity)
        assert values["versions_agree"], values
        assert str(temp / "wheel-env") in values["module_path"], values
        print(identity, flush=True)
        for command in ("--version", "--help", "guide", "diagnostics", "doctor"):
            run([cli, command], cwd=temp, env=env)
        run(
            [python, "-I", "-m", "pytest", "-o", "addopts=", "-q", "-ra", root / "tests"],
            cwd=temp,
            env=env,
        )
        run([cli, "sample", "create", temp / "sample"], cwd=temp, env=env)
        run([cli, "sample", "mock-run", temp / "sample"], cwd=temp, env=env)
        if artifact_dir:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            for artifact in (temp / "dist").iterdir():
                if artifact.is_file():
                    shutil.copy2(artifact, artifact_dir / artifact.name)
        print(
            "Release checks passed: source, wheel, CLI, mock workflow. Real MLX checks are separate.",
            flush=True,
        )


if __name__ == "__main__":
    main()
