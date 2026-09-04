#!/usr/bin/env bash
set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required: https://docs.astral.sh/uv/" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
uv tool install --force --editable "$ROOT"

echo
echo "Installed: localdev-mlx"
echo "Next: localdev-mlx configure YOUR_MODEL_ID --server /path/to/mlx_vlm.server"
