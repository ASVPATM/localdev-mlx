#!/usr/bin/env bash
set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required: https://docs.astral.sh/uv/" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RELEASE_BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/localdev-install.XXXXXX")"
uv build "$ROOT" --wheel --out-dir "$RELEASE_BUILD_DIR"
uv tool install --force "$RELEASE_BUILD_DIR"/*.whl
localdev-mlx diagnostics

echo
echo "Installed: localdev-mlx"
echo "Next: localdev-mlx configure YOUR_MODEL_ID --server /path/to/mlx_vlm.server"
