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

### Choose compatible models

Use instruction-following chat models that can reason about code. There is no brand or size allowlist, but each model must load in your installed **MLX-VLM**, handle text-only chat, return schema-valid JSON, and fit in your Mac's memory with room for context. An "MLX" label alone does not guarantee compatibility. Supply a Hugging Face model ID or an absolute path to a compatible local model directory.

| Role | What to prioritize | Tested model |
|---|---|---|
| Planner | Accurate reasoning, concrete plans, and precise file authority. | [orcarouter/Qwen3.8-27B-Uncensored-MLX](https://huggingface.co/orcarouter/Qwen3.8-27B-Uncensored-MLX) |
| Worker | Reliable code edits and JSON; a smaller, faster model can work well. | [mlx-community/Qwen3.5-9B-4bit](https://huggingface.co/mlx-community/Qwen3.5-9B-4bit) |
| Reviewer | Careful review of diffs and test evidence; use your stronger model. | The same 27B model as the planner. |

These are tested starting points, not a universal ranking. Both passed the workflow-schema probes and a sample repair with thinking disabled; see the [measured results](docs/development/STABILIZATION_HANDOFF.md#real-model-validation). One model can fill all roles; omit `--worker-model` and `--reviewer-model` to do that.

### Connect the server and verify

Install [MLX-VLM separately](https://github.com/Blaizzy/mlx-vlm#installation). `--server` is the absolute path to its **executable**, not a URL or model directory. LocalDev starts it for you on `127.0.0.1:8080` by default; you normally should not launch a second server yourself. The local validation environment uses `mlx-vlm 0.6.15`; other versions must support the [chat/JSON-schema server API](https://github.com/Blaizzy/mlx-vlm#structured-outputs).

```bash
command -v mlx_vlm.server  # Use this path below; no output means it is not on PATH.
localdev-mlx configure orcarouter/Qwen3.8-27B-Uncensored-MLX \
  --worker-model mlx-community/Qwen3.5-9B-4bit \
  --server /absolute/path/to/mlx_vlm.server --no-thinking
localdev-mlx doctor
localdev-mlx model probe planner --capabilities --request-timeout 180
localdev-mlx model probe worker --capabilities --request-timeout 180
```

`configure` saves role assignments; it does not load a model. The first load may download weights. For gated models, accept the model's terms and follow the [Hugging Face login/download guide](https://huggingface.co/docs/huggingface_hub/guides/cli). Probe each distinct model, including `reviewer` if it differs. Probes disable thinking, cap output at 1,200 tokens, and normally unload afterward; passing them is a compatibility check, not a guarantee of coding quality.

### Load, switch, and unload

```bash
localdev-mlx model start planner  # Load the planner and leave it available.
localdev-mlx model status        # Check running/managed state and model identity.
localdev-mlx model start worker  # Stop the managed planner and load the worker.
localdev-mlx model stop          # Unload and stop the LocalDev-managed server.
localdev-mlx model logs          # Inspect startup/loading errors.
```

Workflows switch models automatically, one managed model at a time, and normally unload at the end. Use `--keep-model-loaded` on a workflow/probe to retain the final model. To stop an active maintenance task, use `cancel TASK-ID` and wait for it to finish before `model stop`.

To change assignments, stop the managed server and rerun `configure ... --force` (replaces the saved configuration), then probe again. **Stopping is not deleting:** downloaded weights and saved profiles remain; there is no model-download, model-delete, or profile-remove command.

### Compatibility limits

- LocalDev sends text only, even to vision-capable models. Embedding, speech, and image-only models cannot fill these coding roles. It does not convert model formats or provide Ollama/GGUF, `mlx_lm.server`, or hosted-API integrations.
- Leave draft-model/speculative-decoding options off: MLX-VLM currently [does not combine them with structured outputs](https://github.com/Blaizzy/mlx-vlm#structured-outputs).
- An existing external server is reused only when it advertises exactly the requested model at `/v1/models`. LocalDev never takes ownership of or stops it. If the port is occupied or identity is ambiguous, stop that server yourself or configure a different `--port`. See the [upstream server guide](https://github.com/Blaizzy/mlx-vlm#server-fastapi).

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

## Commands

Prefix each command with `localdev-mlx`. Use `--help` for options and `--repo PATH` on project workflows when running outside the project directory.

| Command | Purpose |
|---|---|
| `configure MODEL_ID` | Configure the local models and server executable. |
| `doctor` | Check setup; add `--prepare` to validate a fresh project worktree. |
| `diagnostics` | Show executable, interpreter, package path, and versions. |
| `init PATH` | Initialize a Git project for LocalDev. |
| `idea "DESCRIPTION"` | Turn a project idea into a brief, provisional architecture, and review prompt. |
| `plan "INSTRUCTIONS"` | Create architecture, contracts, and a dependency-ordered task queue. |
| `run-queue` | Execute ready queue entries; defaults to one task. |
| `bug "DESCRIPTION"` | Implement, test, and review a bug fix. |
| `feature "DESCRIPTION"` | Implement, test, and review a bounded feature. |
| `tweak "DESCRIPTION"` | Make a small adjustment through the same safety gates. |
| `status` | List local task outcomes and external-review states. |
| `follow [TASK-ID]` | Follow task progress; defaults to the newest task. |
| `show-task TASK-ID` / `explain-task TASK-ID` | Inspect complete stored task state. |
| `cancel TASK-ID` | Request cancellation of an active task. |
| `cleanup TASK-ID` | Remove a stopped task's worktree while preserving artifacts. |
| `export-review TASK-ID` | Export a task's external-review bundle. |
| `release-candidate` | Run full validation and prepare an independent release-review bundle. |
| `frontier defer KIND "DESCRIPTION"` | Save a bug, feature, tweak, or audit without inference. |
| `frontier defer-file FILE.json` | Import multiple deferred issues. |
| `frontier status` | List frontier issues and batches. |
| `frontier bundle` | Bundle open issues with the latest integration state. |
| `frontier resolve --task TASK-ID --commit COMMIT` | Record an issue as externally resolved; `--batch` can select a batch. |
| `frontier reopen --task TASK-ID` | Reopen an external-review item. |
| `frontier supersede --task TASK-ID` | Close an obsolete or duplicate external-review item. |
| `frontier sync --from BRANCH` | Fast-forward integration to externally committed fixes. |
| `model status` | Inspect managed model-server state. |
| `model start PROFILE` | Load a configured model or role. |
| `model stop` | Stop the verified managed server. |
| `model logs` | Show recent server logs. |
| `model probe PROFILE` | Check structured output; add `--capabilities` for workflow-schema probes. |
| `sample create PATH` | Create a disposable project with an intentional bug. |
| `sample mock-run PATH` | Exercise the sample workflow without loading models. |
| `guide` | Explain the workflow and outcomes. |

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

## Validate the tool

```bash
uv run python scripts/release_check.py --artifacts dist/stabilized
```

Requires committed source; validates a clean checkout, wheel installation, CLI, and mock workflow. Real MLX validation is separate. See the [stabilization report](docs/development/STABILIZATION_HANDOFF.md) for measured results, compatibility notes, and limitations.

## License

MIT
