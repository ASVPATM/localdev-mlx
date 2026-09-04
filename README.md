# LocalDev MLX

LocalDev MLX is a lightweight Git workflow for using local MLX models as a planner, code worker, and reviewer.

It is optimized for:

- Apple Silicon and `mlx_vlm.server`
- one canonical Git repository
- one, two, or three configurable local models
- bounded bugs, features, tweaks, and project scaffolding
- saving difficult work for a stronger model or human without losing context

LocalDev uses isolated Git worktrees, path allowlists, configured tests, and an `ai/integration` branch. It never calls a paid or frontier model automatically.

> Early alpha: generated code is still code. Review important changes and use trusted repositories and models.

## Install

Requirements: Python 3.11+, Git, [`uv`](https://docs.astral.sh/uv/), `mlx-vlm`, and at least one local model that can return schema-valid JSON.

```bash
git clone https://github.com/ASVPATM/localdev-mlx.git
cd localdev-mlx
uv tool install .
uv tool update-shell
exec zsh
```

## Configure models

Find the server executable:

```bash
which mlx_vlm.server
```

One model for every role:

```bash
localdev-mlx configure YOUR_MODEL_ID \
  --server /absolute/path/to/mlx_vlm.server
```

Stronger planner/reviewer plus smaller worker:

```bash
localdev-mlx configure PLANNER_MODEL_ID \
  --worker-model WORKER_MODEL_ID \
  --reviewer-model PLANNER_MODEL_ID \
  --server /absolute/path/to/mlx_vlm.server
```

Verify:

```bash
localdev-mlx doctor
localdev-mlx model probe planner
localdev-mlx model probe worker
```

## Initialize a project

The project must be a clean Git repository with at least one commit.

```bash
cd /path/to/project

localdev-mlx init . \
  --quick-test "uv run pytest -q" \
  --full-test "uv run pytest" \
  --full-test "uv run ruff check ."
```

Local work is committed to `ai/integration`, not directly to `main`.

## Main commands

```bash
# Understand a new project
localdev-mlx idea "Describe the product, users, workflow, and constraints" --repo .
localdev-mlx plan "Create a dependency-ordered implementation plan" --repo . --skip-external-review
localdev-mlx run-queue --repo . --max-tasks 1

# Maintain an existing project
localdev-mlx bug "Observed behavior and expected behavior" --repo .
localdev-mlx feature "Feature and acceptance criteria" --repo .
localdev-mlx tweak "Small adjustment" --repo .

# Inspect and prepare release review
localdev-mlx status --repo .
localdev-mlx show-task TASK-ID --repo .
localdev-mlx release-candidate --repo .
```

Use `--depth fast`, `balanced`, or `deep`. Every depth still attempts real edits, runs tests, requests local review, and either integrates the result or preserves an external-review bundle.

Run this for a concise explanation of the workflow:

```bash
localdev-mlx guide
```

## Task outcomes

| Outcome | Meaning |
|---|---|
| `integrated` | Locally implemented, tested, reviewed, committed, and fast-forwarded into `ai/integration`. |
| `approved` | Committed on its isolated task branch because `--no-integrate` was used. |
| `deferred` | No local model was called. The issue was intentionally placed in the frontier backlog. |
| `escalated` | Local planning, implementation, validation, or review stopped. No change was integrated. |

Every integrated task creates a tracked handoff under `docs/ai/handoffs/`. All integrated changes are included later in the base-to-integration release diff.

Every deferred or escalated task creates:

- a canonical bundle under the LocalDev application-data directory
- a visible ignored copy under `.localdev/runtime/reviews/TASK-ID/`

## Defer work immediately

Record an issue without loading any model:

```bash
localdev-mlx bug "Issue to fix later" --repo . --frontier-only
```

`--escalate-now` is an alias. The same option works with `feature` and `tweak`.

Or use the frontier command directly:

```bash
localdev-mlx frontier defer bug "Issue to fix later" --repo .
```

Record several issues at once from JSON without loading a model:

```json
[
  {"kind": "bug", "description": "First issue"},
  {"kind": "feature", "description": "Second issue", "reason": "Needs API review"}
]
```

```bash
localdev-mlx frontier defer-file issues.json --repo .
```

You may continue implementing unrelated tasks. Open frontier items do not block direct work. Queue tasks depending on an unresolved item remain blocked; independent queue tasks continue by default.

## Combine issues for one frontier session

```bash
localdev-mlx frontier status --repo .
localdev-mlx frontier bundle --repo .
```

The batch contains:

- every unresolved deferred/escalated issue
- the latest `ai/integration` commit
- the current base-to-integration diff
- an index of integrated local tasks
- one `FRONTIER_BATCH_PROMPT.md`

Individual issue patches are historical. The external reviewer is instructed to work from the latest integration branch so later successful local work is preserved.

After the external fixes are committed to `ai/integration`:

```bash
localdev-mlx frontier resolve \
  --batch FRONTIER-YYYYMMDD-HHMMSS \
  --commit HEAD \
  --repo .
```

LocalDev verifies that the recorded commit is reachable from `ai/integration`. The next local task automatically starts from that current commit and reads the latest repository files and tests. Resolution is explicit because LocalDev cannot safely infer that a commit fixed a specific issue.

If the external reviewer committed on `main` or another branch that cleanly contains all current integration work, fast-forward the integration branch first:

```bash
localdev-mlx frontier sync --from main --repo .
```

If a later local or external task made an older escalation obsolete, close the duplicate without claiming that the original attempt succeeded:

```bash
localdev-mlx frontier supersede \
  --task OLD-TASK-ID \
  --commit HEAD \
  --reason "Replaced by FEAT-..." \
  --repo .
```

Use `localdev-mlx frontier status --all --repo .` to see pending, bundled, resolved, and superseded history.

## Continue after an escalation

An escalated task does not modify `main` or block unrelated direct commands:

```bash
localdev-mlx feature "Another independent feature" --repo .
```

For a machine-readable task queue:

```bash
localdev-mlx run-queue --repo . --continue-on-escalation
```

Dependent tasks stay blocked; independent tasks may continue.

## Model memory

Models unload automatically by default.

```bash
localdev-mlx model status
localdev-mlx model stop
```

Use `--keep-model-loaded` on a workflow to keep the final model resident.

## External reviewers

A frontier model or human remains outside the automation loop. Give it either:

- `.localdev/runtime/reviews/TASK-ID/EXTERNAL_REVIEW_PROMPT.md` for one issue
- `.localdev/runtime/frontier/BATCH-ID/FRONTIER_BATCH_PROMPT.md` for several issues
- the bundle from `localdev-mlx release-candidate` for final review

After external work, keep the fixes on `ai/integration`, run the full tests, create a handoff, and record resolution with `localdev-mlx frontier resolve`.

## Validate without a model

```bash
./scripts/validate_without_models.sh
```

## License

MIT
