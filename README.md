# LocalDev MLX

Inspect your app, describe changes, and export a focused handoff for your coding model. **Project Flow** uses sample data; **Personal-data Flow** uses an isolated copy of your saved data. Models are optional.

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). Coding sessions need Git; Flow capture does not. Local inference requires Apple Silicon and [MLX-VLM](https://github.com/Blaizzy/mlx-vlm#installation).

```bash
git clone https://github.com/ASVPATM/localdev-mlx.git
cd localdev-mlx
bash scripts/install.sh
uv tool update-shell
exec zsh
localdev-mlx --version
```

To update, pull the latest checkout and rerun your install command (keep `--flow-browser` if used).

## Commands

Prefix commands with `localdev-mlx`, or run `localdev-mlx` for an interactive session. Use `--help` for options.

| Command | Usage |
|---|---|
| `init [PATH]` | Prepare Git/settings and Flow-build agent guidance; keep your branch. |
| `configure MODEL` | Assign models; `--check` checks your installation. |
| `model [ROLE]` | Model status; `--load`, `--stop`, `--probe`, `--logs`. |
| `plan "GOAL"` | Light brief by default; `--mode full` asks questions, then drafts a detailed brief. |
| `bug "DESCRIPTION"` | Record a bug; `--local` attempts a fix. |
| `feature "DESCRIPTION"` | Record a feature; `--local` attempts implementation. |
| `tweak "DESCRIPTION"` | Record an adjustment; `--local` attempts it. |
| `session` | Session status; `--show`, `--new`, `--end`, `--cancel`. |
| `flow` | Sample-data dashboard; `--personal` for personal data. Subcommands: `start`, `stop`, `status`, `doctor`, `init`, `browser-setup`. |

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

`init` keeps your branch and settings (`main` for new projects). New projects normally get an initial snapshot; review secrets first. Dirty existing repositories, staged work, credential-like paths (including `.env.example`) or missing Git identity leave setup uncommitted instead of blocking it. Planning/handoffs still work; `--local` requires a clean committed checkout.

It adds a short Flow-build section to `AGENTS.md` (or active `AGENTS.override.md`), preserving existing rules without duplicates. The builder is asked to maintain preview setup, synthetic data and useful UI labels; the preview still needs validation. To copy the section manually: `localdev-mlx init --print-agent-guidance` (changes nothing).

Requests share `.localdev/runtime/sessions/SESSION-0001.md`; give it to your external model yourself. `--escalate` is the default: no model loading or application edits. `session --new` starts a new numbered handoff. Each interactive launch starts a new session; Ctrl-D ends it. One request runs at a time.

```bash
localdev-mlx plan "A reading app" --mode light  # Brief only; no inference.
localdev-mlx plan "A reading app" --mode full   # Questions, then a detailed, flexible brief.
localdev-mlx bug "Fix subtraction" --local --allow src/calc.py --read tests/test_calc.py
```

Full planning asks focused questions and follow-ups as needed, with no fixed question count. Enter skips one; `/done` finishes; `--no-questions` skips the interview. Your answers stay in the handoff. Small projects get an end-to-end brief, not a forced phased plan. Input is limited to 8,000 characters; there is no prompt-file import.

Local implementation needs configured models, a clean checkout, and real test commands in `.localdev/config.toml`. For a new Python project:

```bash
localdev-mlx init . --prepare "uv sync --locked --group dev" \
  --quick-test "uv run pytest -q" --full-test "uv run pytest -q"
```

Only `--local` uses isolated Git worktrees and test/review gates before integration into `ai/integration`. External coding work uses your normal checkout; commit/push only when you choose. Local attempts, changed files, test results, and patches appear in the same Git-ignored handoff; failed worktrees are preserved. `session --cancel` stops an active request.

## Recommended workflow: Flow → coding model → Flow

1. Open Flow in your project, then **Open test app**. No Git initialization or model is required.
2. Reproduce an issue. Press **Option/Alt + Shift + I**, then select an element; **Esc** cancels. Works on subpages and dialogs.
3. Describe a change or question and attach captures. **Check shared behavior** asks the coding model to investigate related components, not just the selected record. Drafts autosave.
4. Export a **Standard ZIP** with evidence, or **Light Markdown** for text only. Give it to your coding model. After fixes, refresh Flow and check again.

PMT checkouts with existing PMT Flow fixtures and plain HTML sites are detected automatically—no `init` needed. **Open test app** asks for confirmation once. PMT uses a separate sample database and no Keychain access; your real library and existing PMT Flow stay untouched.

```bash
bash scripts/install.sh --flow-browser  # Optional browser/image support; run from this checkout.
localdev-mlx flow browser-setup         # Explicit Chromium download; no models downloaded.
cd /path/to/project
localdev-mlx flow init                  # Optional: write a reviewable adapter.
localdev-mlx flow                       # Open the dashboard; also works inside a LocalDev shell.
localdev-mlx flow stop                  # Close this project's preview/browser; keep evidence.
localdev-mlx flow --personal            # Personal-data mode (supported adapter required).
localdev-mlx flow stop --personal       # Discard temporary data; keep captures and handoffs.
```

For other apps, configure `project-flow.toml` using the [web adapter](src/localdev_mlx/flow/examples/web/project-flow.toml), then restart Flow. `flow init` saves a detected adapter or a manual template; `flow init --demo --project /path/to/empty-folder` copies the demo. The [manual adapter](src/localdev_mlx/flow/examples/manual/project-flow.toml) supports CLI/native attachments and file/line anchors—not native DOM selection.

**Open** reuses the current preview; **Refresh** uses current code and resets preview data. Captures show readable app-area labels, routes, source hints and diagnostics—not a full backend trace. Flow stays open after Ctrl-D; stop it explicitly. `--project PATH` selects another folder; `--no-open` prints the URL. Use `flow status` (or `flow status --personal`) to find an existing dashboard.

Finished a batch? **Clear all app parts** removes captures/assignments; **Clear all changes** removes descriptions. Both ask first, preserve exported handoffs, and save a private recovery copy. The two Flow modes have separate captures, changes, and controllers.

### Personal-data setup

Currently supports **CanvasMaster on macOS**, with dependencies installed in its `.venv`. Other apps need a reviewed snapshot adapter; arbitrary live databases cannot be attached by this feature. Add to `project-flow.toml`:

```toml
[personal]
adapter = "canvasmaster"
source = "~/Library/Application Support/CanvasOptimizer"
```

Check with `localdev-mlx flow doctor --personal`. In the personal dashboard, **Open personal-data app** asks permission before copying the database/assets. First open takes time and free disk space (maximum 20 GB / 100,000 assets). Preview edits affect only the copy; there is no write-back.

The app process is OS-sandboxed; its recording browser blocks requests outside the preview. Background jobs, sync, external sessions, and app model operations are unavailable. **Stop/Refresh discards preview data edits**, not evidence. Interrupted copies are cleaned on the next start. Use this mode to inspect saved results, not live integrations.

Personal text can appear in captures; forms and `data-flow-secret` content remain masked. Handoffs request shared-code investigation, validation on copies, and approval before live-data changes. Instructions cannot guarantee an external coding model's behavior.

### Evidence and optional helper

Export Markdown or ZIP: **Light** omits files, **Standard** links evidence, **Full** expands diagnostics. Exports link into the current initialized LocalDev session; otherwise they remain complete standalone handoffs. Busy/failed session links can be retried without rerunning the model.

Flow uses your LocalDev worker or another verified resident profile, not the adapter's model URL. Organization is advisory, text-only, and limited to 20 seconds by default (`[model].organizer_timeout`, 1–120). Missing/busy/failed models fall back to ordinary export. Preload with `model worker --load`, or explicitly save offline helper-loading consent with `flow start --allow-model-load`; restart Flow to apply preference changes. `--no-model-load` revokes it. Flow never unloads a model that was already resident.

Private evidence lives in `.project-flow/` (personal mode: `.project-flow/personal/`); `flow init` or `localdev-mlx init` adds the Git ignore rule. No automatic uploads. Review exports: masking/redaction is best-effort; uploaded screenshots are not automatically masked. Sample adapters use your normal OS privileges—trust their code. WebSockets/streaming and Streamlit's interactive UI are unsupported; use manual evidence or a served build instead of HMR.

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
