#!/usr/bin/env bash
set -euo pipefail

for role in planner worker reviewer; do
  localdev-mlx model probe "$role" --capabilities --request-timeout 120
done
