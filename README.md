# LocalDev MLX

Collect coding requests into one compact handoff per session. Requests defer to your external coding model by default; optional local models can plan or implement changes.

## Install

Requires Python 3.11+, Git, and [uv](https://docs.astral.sh/uv/). Local inference also requires Apple Silicon and [MLX-VLM](https://github.com/Blaizzy/mlx-vlm#installation).

```bash
git clone https://github.com/ASVPATM/localdev-mlx.git
cd localdev-mlx
bash scripts/install.sh
uv tool update-shell
exec zsh
localdev-mlx --version
```

To update, pull the latest checkout and rerun `bash scripts/install.sh`.

## Commands

Prefix commands with `localdev-mlx`, or run `localdev-mlx` for an interactive session. Use `--help` for options.

| Command | Usage |
|---|---|
| `init [PATH]` | Prepare Git/first commit and settings; keep your branch. |
| `configure MODEL` | Assign models; `--check` checks your installation. |
| `model [ROLE]` | Model status; `--load`, `--stop`, `--probe`, `--logs`. |
| `plan "GOAL"` | Light brief by default; `--mode full` asks questions, then drafts a detailed brief. |
| `bug "DESCRIPTION"` | Record a bug; `--local` attempts a fix. |
| `feature "DESCRIPTION"` | Record a feature; `--local` attempts implementation. |
| `tweak "DESCRIPTION"` | Record an adjustment; `--local` attempts it. |
| `session` | Session status; `--show`, `--new`, `--end`, `--cancel`. |

## Simple workflow

```bash
cd /path/to/project
localdev-mlx init .
localdev-mlx plan "A small offline reading app"
localdev-mlx bug "Saving a note freezes the window"
localdev-mlx feature "Search note titles" --escalate
localdev-mlx session --show
localdev-mlx session --end
```

`init` requires your Git name/email and a clean existing repository. It keeps your branch (`main` for new projects). New repositories get an initial commit of non-ignored files; check for secrets first.

Requests share `.localdev/runtime/sessions/SESSION-0001.md`; give it to your external model yourself. `--escalate` is the default: no model loading or application edits. `session --new` starts a new numbered handoff. Each interactive launch starts a new session; Ctrl-D ends it. One request runs at a time.

```bash
localdev-mlx plan "A reading app" --mode light  # Brief only; no inference.
localdev-mlx plan "A reading app" --mode full   # Questions, then a detailed, flexible brief.
localdev-mlx bug "Fix subtraction" --local --allow src/calc.py --read tests/test_calc.py
```

Full planning asks focused questions and follow-ups as needed, with no fixed question count. Enter skips one; `/done` finishes; `--no-questions` skips the interview. Your answers stay in the handoff. Small projects get an end-to-end brief, not a forced phased plan.

Local implementation needs configured models, a clean checkout, and real test commands in `.localdev/config.toml`. For a new Python project:

```bash
localdev-mlx init . --prepare "uv sync --locked --group dev" \
  --quick-test "uv run pytest -q" --full-test "uv run pytest -q"
```

Only `--local` uses isolated Git worktrees and test/review gates before integration into `ai/integration`. External coding work uses your normal checkout; commit/push only when you choose. Local attempts, changed files, test results, and patches appear in the same Git-ignored handoff; failed worktrees are preserved. `session --cancel` stops an active request.

## Optional models

Use MLX-VLM-compatible instruction/chat models that handle code and structured JSON. Supply a Hugging Face ID or absolute local model directory; models must fit in memory. There is no brand/size allowlist—probe compatibility before use.

| Role | Look for | Tested model |
|---|---|---|
| Planner / reviewer | Strong reasoning and careful review | [Qwen3.8 27B](https://huggingface.co/orcarouter/Qwen3.8-27B-Uncensored-MLX) |
| Worker | Reliable code edits; smaller can be faster | [Qwen3.5 9B 4-bit](https://huggingface.co/mlx-community/Qwen3.5-9B-4bit) |

```bash
command -v mlx_vlm.server  # Use this executable's absolute path below.
localdev-mlx configure orcarouter/Qwen3.8-27B-Uncensored-MLX \
  --worker-model mlx-community/Qwen3.5-9B-4bit \
  --server /absolute/path/to/mlx_vlm.server --no-thinking
localdev-mlx model planner --probe
localdev-mlx model worker --probe
localdev-mlx model planner --load
localdev-mlx model worker --load  # Switch models.
localdev-mlx model --stop         # Unload; keep downloaded weights.
```

`configure` saves settings without loading. Omit role overrides to use one model for everything. LocalDev manages the [MLX-VLM server](https://github.com/Blaizzy/mlx-vlm#server-fastapi) at `127.0.0.1:8080`, switches models, and normally unloads after workflows. Stop it before reconfiguring with `configure ... --force`. External servers are never stopped by LocalDev; free an occupied port yourself or use `--port`.

Tested with MLX-VLM 0.6.15. First load may download weights; gated models need [Hugging Face authentication](https://huggingface.co/docs/huggingface_hub/guides/cli). Text only: no Ollama/GGUF, `mlx_lm.server`, hosted APIs, or image/audio inputs. Disable speculative decoding with structured outputs.

Early alpha: review changes yourself. Passing tests or model review does not guarantee correctness. Setup/test commands execute project code—use trusted repositories and models.
