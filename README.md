# LocalDev MLX

Eight commands, one working session, one concise coding handoff. By default, LocalDev records requests for an external coding model **without loading a model or changing application code**. Run that external model yourself; LocalDev never invokes it.

Optional local MLX models can develop a provisional plan or attempt a bounded implementation with Git isolation, tests, and review.

> Early alpha. Review generated changes. Configured test/setup commands execute project code; this is not a security sandbox. Use trusted repositories and models.

## Install or upgrade

Requirements: Python 3.11+, Git, and [uv](https://docs.astral.sh/uv/). Local inference additionally needs Apple Silicon and a separately installed MLX-VLM server.

```bash
git clone https://github.com/ASVPATM/localdev-mlx.git
cd localdev-mlx
bash scripts/install.sh
uv tool update-shell
exec zsh
localdev-mlx --version
```

For upgrades, update the checkout and rerun the installer. It installs an exact wheel; `localdev-mlx configure --check` reports the executable, interpreter, package path, and versions.

## Prepare a project

```bash
cd /path/to/project
localdev-mlx init .
```

`init` creates Git and an initial commit when needed, commits minimal configuration/rules, and switches to `ai/integration`. It does not load models, plan, or run tests. Configure your Git name/email first. The initial snapshot includes non-ignored project files; credential-like filenames are refused, but this is not a secret scanner. Existing repositories must be clean; unrelated edits and custom `AGENTS.md` files are preserved. No `docs/ai` hierarchy is created.

For local implementation, provide the project's real validation commands at initialization. Example for a Python project with a locked `uv` development group:

```bash
localdev-mlx init . \
  --prepare "uv sync --locked --group dev" \
  --quick-test "uv run --locked --group dev pytest -q" \
  --full-test "uv run --locked --group dev pytest -q"
```

For an already initialized project, edit `.localdev/config.toml` and commit the changes; rerunning `init` preserves existing configuration. Setup/tests run in fresh worktrees, without inheriting an activated Python environment or `PYTHONPATH`. Declare necessary test variables with `--test-env KEY=VALUE` during initialization or `[tests.env]` afterward.

## Work in a session

Run `localdev-mlx` in the project to open an interactive session. Enter commands without the `localdev-mlx` prefix:

```text
plan "A small offline reading app"
bug "Saving a note freezes the window; reproduce by opening a note and clicking Save"
feature "Add search across note titles" --escalate
session --show
session --end
```

`--escalate` explicitly selects the default handoff-only behavior. Each request is added to the same `.localdev/runtime/sessions/SESSION-0001.md`. Give that file to your external coding model. No task IDs, export steps, frontier commands, or separate review documents are required.

You can also use ordinary shell commands, such as `localdev-mlx bug "DESCRIPTION"`, while testing the application in another terminal. They join the current project session; the first request starts one automatically if needed. Use `localdev-mlx session --end` to finish or `session --new` to start a fresh numbered handoff. Each interactive launch starts a new session; Ctrl-D also ends it. The interactive prompt is not a system shell.

Only one request runs at a time per project. Closed handoffs remain unchanged; requests are not silently carried into later sessions. Handoffs and optional patch evidence are Git-ignored. Internal recovery records remain outside the repository.

## Commands

Prefix shell commands with `localdev-mlx`; use `--help` for options. Project commands accept `--repo PATH`.

| Command | Purpose |
|---|---|
| `init [PATH]` | Prepare Git, the initial commit, and minimal project configuration. |
| `configure MODEL` | Save model assignments; `--check` shows installation details. |
| `model [ROLE]` | Show status; use `--load`, `--stop`, `--probe`, or `--logs`. |
| `plan "GOAL"` | Add a lightweight brief; `--mode full` adds a provisional local proposal. |
| `bug "DESCRIPTION"` | Record a bug; `--local` opts into local implementation. |
| `feature "DESCRIPTION"` | Record a feature; `--local` opts into local implementation. |
| `tweak "DESCRIPTION"` | Record a small adjustment; `--local` opts into local implementation. |
| `session` | Show current progress/handoff; `--show`, `--new`, `--end`, or `--cancel`. |

### Light and full planning

`plan --mode light` is the default: record the goal, Git snapshot, and configured validation commands for external planning. It makes no inference calls and does not scaffold application code.

`plan --mode full` uses two bounded planner passes—draft, then critique/refinement—and places one proposal in the same handoff. It suggests an approach, checks, alternatives, and open questions. **The proposal is flexible and unverified, not a fixed architecture or an executable task queue.** No application files or tests are changed/run by planning.

### Optional local implementation

```bash
localdev-mlx bug "Fix the subtraction result" --local
# For known files, skip planner triage with explicit file authority:
localdev-mlx bug "Fix subtraction" --local --allow src/calc.py --read tests/test_calc.py
```

Local execution requires a clean checkout, configured models, and test commands. It prepares an isolated worktree, checks the baseline, applies scoped edits, runs tests, and obtains local review before fast-forwarding `ai/integration`. It never merges directly into the base/release branch. `--no-integrate` keeps an approved change separate for manual review. New external commits on the configured base are adopted only by a safe fast-forward; divergent branches require ordinary Git reconciliation.

Every local attempt updates the same handoff with edited files, base/final commits, actual test outcomes, and optional exact patch evidence. Failed worktrees are preserved. Passing tests do not guarantee a correct application: if a regression appears later, the handoff still identifies the earlier local edits. After interruption, inspect current Git state alongside the evidence.

Use `session --cancel` from another terminal, or Ctrl-C during a run, to cancel and preserve evidence. Defaults allow two worker attempts and a 900-second task budget; `--max-attempts`, `--no-fallback`, `--request-timeout`, and `--task-timeout` override local execution limits.

## Configure optional models

Use instruction-following chat models that handle code, text-only chat, and schema-valid JSON. Each must load in your installed [MLX-VLM](https://github.com/Blaizzy/mlx-vlm#installation) and fit in memory with room for context. There is no model-brand or size allowlist; an "MLX" label alone does not guarantee compatibility. Supply a Hugging Face model ID or an absolute local model directory.

| Role | Prioritize | Tested model |
|---|---|---|
| Planner | Reasoning and precise instructions. | [orcarouter/Qwen3.8-27B-Uncensored-MLX](https://huggingface.co/orcarouter/Qwen3.8-27B-Uncensored-MLX) |
| Worker | Reliable code edits/JSON; a smaller model can be faster. | [mlx-community/Qwen3.5-9B-4bit](https://huggingface.co/mlx-community/Qwen3.5-9B-4bit) |
| Reviewer | Careful diff/test review; use your stronger model. | The same 27B model as the planner. |

These models passed schema probes and a sample repair in the roles above; the 27B planner also completed two-pass planning. They are tested starting points, not a quality guarantee. One model can fill all roles by omitting the worker/reviewer options.

```bash
command -v mlx_vlm.server  # Use the absolute executable path below.
localdev-mlx configure orcarouter/Qwen3.8-27B-Uncensored-MLX \
  --worker-model mlx-community/Qwen3.5-9B-4bit \
  --server /absolute/path/to/mlx_vlm.server --no-thinking
localdev-mlx model planner --probe
localdev-mlx model worker --probe
localdev-mlx model planner --load
localdev-mlx model worker --load  # Switch the managed model.
localdev-mlx model --stop         # Unload, without deleting weights or profiles.
```

`configure` saves assignments; it does not load models. LocalDev starts the server for you on `127.0.0.1:8080` by default. `--server` is an executable path, not a URL. The tested server is MLX-VLM 0.6.15; it must support the [structured-output API](https://github.com/Blaizzy/mlx-vlm#structured-outputs). Probe every distinct model. First load may download weights; gated models need terms acceptance and [Hugging Face authentication](https://huggingface.co/docs/huggingface_hub/guides/cli).

Workflows switch managed models one at a time and normally unload afterward. `configure --keep-model-loaded` changes that default. Stop the managed server before changing assignments with `configure ... --force`, then probe again. There is no model-delete or profile-remove command.

Limits: text only, including for vision-capable models; no embedding/speech/image-only roles, format conversion, Ollama/GGUF, `mlx_lm.server`, or hosted-API integration. Leave speculative decoding off with structured outputs. An existing external server is reused only if `/v1/models` identifies exactly the requested model; LocalDev never stops it. For an occupied/mismatched port, stop that server yourself or configure another `--port`; see the [server guide](https://github.com/Blaizzy/mlx-vlm#server-fastapi).

## Upgrading from 0.3.x

This is a breaking CLI simplification: `idea` is now `plan`; model subcommands use flags; task-ID, frontier, queue, release-candidate, and sample commands are removed. Requests now **defer by default**; use `--local` to implement. Existing configuration and historical records are retained, not converted into new session handoffs. Review any custom rules/configuration that still reference old workflows. Normal release checks and independent review remain your responsibility outside the CLI.
