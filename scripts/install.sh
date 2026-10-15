#!/usr/bin/env bash
set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required: https://docs.astral.sh/uv/" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RELEASE_BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/localdev-install.XXXXXX")"
EXTRA=""
if [[ "${1:-}" == "--flow-browser" ]]; then
  EXTRA="[flow-browser]"
elif [[ $# -ne 0 ]]; then
  echo "Usage: bash scripts/install.sh [--flow-browser]" >&2
  exit 2
fi
uv build "$ROOT" --wheel --out-dir "$RELEASE_BUILD_DIR"
for wheel in "$RELEASE_BUILD_DIR"/*.whl; do
  uv tool install --force "${wheel}${EXTRA}"
done
localdev-mlx configure --check

echo
echo "Installed: localdev-mlx"
echo "Next: run localdev-mlx init in your project, then localdev-mlx to begin a session."
echo "Flow: localdev-mlx flow (Git/model optional). Browser setup is explicit: localdev-mlx flow browser-setup."
