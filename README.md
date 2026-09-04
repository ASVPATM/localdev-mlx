# LocalDev MLX

LocalDev MLX is a lightweight Git workflow for planning, implementing, testing, and reviewing code changes with local models served by `mlx_vlm.server`.

It is optimized for:

- Apple Silicon and MLX
- one canonical Git repository
- one, two, or three configurable local models
- bounded bug fixes, features, tweaks, and project scaffolding
- handing uncertain or high-risk work to a stronger model or human

Models return structured plans and file edits rather than shell commands. LocalDev enforces path allowlists, runs approved test commands, and works in isolated Git worktrees.

> **Early alpha:** LocalDev is not a security sandbox. Generated code is executed by your configured tests. Use trusted models and repositories, keep projects committed, and independently review changes before release.

## Workflow

LocalDev assigns three roles:

```text
Planner → Worker → tests → Reviewer → commit or review bundle
```

A single capable model can fill every role. A common two-model setup uses a stronger model for planning/review and a smaller model for implementation.

## Requirements

- Apple Silicon Mac
- Python 3.11+
- Git
- [`uv`](https://docs.astral.sh/uv/)
- `mlx-vlm`
- at least one local model that can return schema-valid structured output

Open WebUI is optional. LocalDev talks directly to the MLX server.

A minimal standalone MLX environment looks like this:

```bash
uv venv ~/.venvs/localdev-mlx --python 3.12
source ~/.venvs/localdev-mlx/bin/activate
uv pip install mlx-vlm
```

LocalDev stores the absolute `mlx_vlm.server` path, so that environment does not need to remain activated afterward.

## Install

```bash
git clone YOUR_REPOSITORY_URL
cd localdev-mlx
uv tool install .
localdev-mlx --version
```

When the command is not found after installation:

```bash
uv tool update-shell
exec zsh
```

For editable development:

```bash
uv tool install --editable .
```

## Configure

Find the MLX server executable:

```bash
which mlx_vlm.server
```

Use one model for all roles:

```bash
localdev-mlx configure YOUR_MODEL_ID \
  --server /absolute/path/to/mlx_vlm.server
```

Use separate models:

```bash
localdev-mlx configure PLANNER_MODEL_ID \
  --worker-model WORKER_MODEL_ID \
  --reviewer-model REVIEWER_MODEL_ID \
  --server /absolute/path/to/mlx_vlm.server
```

For a model without thinking mode:

```bash
localdev-mlx configure YOUR_MODEL_ID \
  --server /absolute/path/to/mlx_vlm.server \
  --no-thinking
```

Extra MLX server flags may be repeated:

```bash
localdev-mlx configure YOUR_MODEL_ID \
  --server /absolute/path/to/mlx_vlm.server \
  --server-arg=--max-kv-size \
  --server-arg=32768
```

Verify the setup:

```bash
localdev-mlx doctor
localdev-mlx model probe planner
localdev-mlx model probe worker
localdev-mlx model probe reviewer
```

`localdev-mlx doctor` shows the generated TOML path. Edit that file for per-role budgets or other advanced options.

## Initialize a project

The project must be a clean Git repository with at least one commit.

```bash
cd /path/to/project

localdev-mlx init . \
  --quick-test "uv run pytest -q" \
  --full-test "uv run pytest" \
  --full-test "uv run ruff check ."
```

Projects that need test-only environment variables can add repeatable options:

```bash
localdev-mlx init . \
  --quick-test "python3 -m unittest discover -s tests -v" \
  --test-env PYTHONPATH=src
```

LocalDev creates an `ai/integration` branch. Local work is not merged directly into `main`.

## Use

Build from a rough idea:

```bash
localdev-mlx idea "Describe the product, users, workflow, and constraints" --repo .
localdev-mlx plan "Create a dependency-ordered implementation plan" --repo . --skip-external-review
localdev-mlx run-queue --repo . --max-tasks 1
```

Maintain an existing project:

```bash
localdev-mlx bug "Describe the observed and expected behavior" --repo .
localdev-mlx feature "Describe the feature and acceptance criteria" --repo .
localdev-mlx tweak "Describe the small adjustment" --repo .
```

Choose inference depth with `--depth fast`, `balanced`, or `deep`. Every depth still attempts real edits, runs tests, requests reviewer-model approval, and either commits the result or creates a review bundle.

Inspect work:

```bash
localdev-mlx status --repo .
localdev-mlx show-task TASK-ID --repo .
localdev-mlx follow TASK-ID --repo .
```

Unload the active managed model:

```bash
localdev-mlx model stop
```

Use `--keep-model-loaded` on a workflow to leave the final model in memory.

## External review

When local work is unsafe or unsuccessful, LocalDev preserves its context, diff, test output, and model responses:

```bash
localdev-mlx export-review TASK-ID --repo .
```

Before release:

```bash
localdev-mlx release-candidate --repo .
```

The resulting prompt and artifacts can be reviewed by any stronger model or human. External reviewers remain outside the automated loop.

## Verify without a model

```bash
./scripts/validate_without_models.sh
```

This runs the test suite and a complete mock edit → test → review → Git integration workflow.

## License

MIT
