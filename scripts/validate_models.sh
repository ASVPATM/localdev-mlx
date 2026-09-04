#!/usr/bin/env bash
set -euo pipefail

for role in planner worker reviewer; do
  echo "=== ${role} structured-output probe ==="
  localdev-mlx model probe "$role"
  echo
done
