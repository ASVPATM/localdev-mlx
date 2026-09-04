#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SAMPLE="${TMPDIR:-/tmp}/localdev-mlx-sample"

cd "$ROOT"
PYTHONPATH=src python3 -m pytest -q
PYTHONPATH=src python3 -m localdev_mlx sample create "$SAMPLE" --force
PYTHONPATH=src python3 -m localdev_mlx sample mock-run "$SAMPLE"
PYTHONPATH="$SAMPLE/src" python3 -m unittest discover -s "$SAMPLE/tests" -v
