# LocalDev MLX

A lightweight Git workflow for local MLX planning, coding, testing, and review. Optimized for Apple Silicon, `mlx_vlm.server`, and bounded bugs, features, and tweaks.

LocalDev uses isolated worktrees, exact file allowlists, configured tests, and an `ai/integration` branch. It never calls a paid/frontier model automatically or integrates failed work into `main`.

> Early alpha: use trusted repositories and models, and review important generated changes. Test commands execute project code; this is not a security sandbox.

## Install or upgrade

Requirements: Python 3.11+, Git, [`uv`](https://docs.astral.sh/uv/), and a separately installed `mlx-vlm` server with a schema-capable local model.

```bash
git clone https://github.com/ASVPATM/localdev-mlx.git
cd localdev-mlx
bash scripts/install.sh
uv tool update-shell
exec zsh
localdev-mlx --version
localdev-mlx diagnostics
```

The installer builds and installs an exact wheel. For upgrades, update the checkout and rerun it. Diagnostics reports the executable, interpreter, distribution version, and imported module path.

## Configure models

Use one model for every role, or a smaller worker with a stronger planner/reviewer:

```bash
localdev-mlx configure PLANNER_MODEL_ID \
  --worker-model WORKER_MODEL_ID \
  --reviewer-model PLANNER_MODEL_ID \
  --server /absolute/path/to/mlx_vlm.server
localdev-mlx doctor
localdev-mlx model probe planner --capabilities --request-timeout 180
localdev-mlx model probe worker --capabilities --request-timeout 180
```

Omit the worker/reviewer options for a single model. Capability probes test actual triage, edit, and review schemas. They disable thinking and limit output to 1,200 tokens per request.

## Initialize a project

Start with a clean Git repository containing at least one commit. For a Python project with a locked `uv` development group:

```bash
cd /path/to/project
localdev-mlx init . \
  --quick-test "uv run --locked --group dev pytest -q" \
  --full-test "uv run --locked --group dev pytest -q" \
  --full-test "uv run --locked --group dev ruff check ."
```

Initialization switches to `ai/integration`. In the generated `.localdev/config.toml`, update the existing preparation section and commit the configuration:

```toml
[prepare]
commands = ["uv sync --locked --group dev"]
timeout_seconds = 600
```

```bash
localdev-mlx doctor --prepare --repo .
```

Preparation and baseline tests run in a fresh task worktree before inference. Ambient `PYTHONPATH` and activated Python environments are not inherited; declare necessary project-specific test variables in `[tests.env]`. Missing tools, collection failures, and preparation errors stop as diagnostics.

## Workflows

```bash
localdev-mlx bug "Observed behavior and expected behavior" --repo .
localdev-mlx feature "Feature and acceptance criteria" --repo .
localdev-mlx tweak "Small adjustment" --repo .

# Exact files known: skip planner inference, retain all other gates.
localdev-mlx bug "Fix the parser defect" --repo . --direct \
  --allow src/package/parser.py --allow tests/test_parser.py \
  --read src/package/contracts.py \
  --test "uv run --locked --group dev pytest tests/test_parser.py -q"

# Inspect planned authority before worker inference.
localdev-mlx bug "Fix the parser defect" --repo . --plan-only

localdev-mlx status --repo .
localdev-mlx explain-task TASK-ID --repo .
localdev-mlx cancel TASK-ID --repo .
localdev-mlx cleanup TASK-ID --repo .
localdev-mlx guide
```

Default limits are two attempts per work unit (primary plus one alternate-model fallback), 180 seconds per worker request, and 900 seconds per task. Planning and review have shorter limits. Override with `--max-attempts`, `--no-fallback`, `--request-timeout`, or `--task-timeout`; see `[execution]` for role-specific limits. `--depth fast|balanced|deep` changes inference/context budgets, not the validation gates.

Applied edits remain available when tests fail; a retry sees current files and the current diff. No-change claims require controller verification. Full tests and reviewer approval precede commit and integration. Cleanup preserves artifacts and refuses dirty task worktrees unless `--force` is explicitly supplied.

| Local outcome | Meaning |
|---|---|
| `integrated` | Tested, reviewed, committed, and fast-forwarded into `ai/integration`. |
| `approved_not_integrated` | Approved task commit retained because integration was disabled or its base advanced. |
| `planned` | Authority validated; no worker ran. |
| `deferred` | Intentionally saved for external review without inference. |
| `escalated` | Engineering work stopped with an external-review bundle. |
| `failed` / `cancelled` | Diagnostic failure or cancellation; inspect the stored reason and artifacts. |

Local outcomes and external-review states are separate. Resolving an external issue does not rewrite the original attempt as successful. Failed/escalated maintenance commands exit 1; cancellation exits 130.

Task state, prompts, manifests, patches, responses, and test results live in application data. Maintenance tasks no longer create tracked per-task documentation handoffs. `LOCALDEV_MLX_HOME` can isolate all runtime data for testing.

## External review and release preparation

```bash
localdev-mlx bug "Issue to handle later" --repo . --frontier-only
localdev-mlx frontier status --repo .
localdev-mlx frontier bundle --repo .

# After fixes are committed to ai/integration:
localdev-mlx frontier resolve --batch BATCH-ID --commit HEAD --repo .
localdev-mlx release-candidate --repo .
```

Batches include unresolved issues and the latest integration snapshot/diff, not just historical task patches. Visible copies are ignored under `.localdev/runtime/`. Resolve selected issues with repeated `--task` options; use `frontier reopen` or `frontier supersede` to maintain explicit history. Independent work may continue while dependent queue entries remain blocked. Release-candidate preparation never publishes a release.

Models unload automatically unless `--keep-model-loaded` is supplied. `localdev-mlx model status` and `model stop` inspect or stop the verified managed process; unrelated servers are not killed.

Project design and queue commands remain available: `idea`, `plan`, and `run-queue`. Use command help for their existing project-document workflow.

## Validate the tool

```bash
uv run python scripts/release_check.py --artifacts dist/stabilized
```

Requires committed source; validates a clean checkout, wheel installation, CLI, and mock workflow. Real MLX validation is separate. See the [stabilization report](docs/development/STABILIZATION_HANDOFF.md) for measured results, compatibility notes, and limitations.

## License

MIT
