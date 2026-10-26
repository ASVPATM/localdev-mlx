# LocalDev MLX

Select parts of your app, describe changes, and export a handoff for your coding model. Use sample data or an isolated copy of personal data. Local models are optional.

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). Local models also need Apple Silicon and [MLX-VLM](https://github.com/Blaizzy/mlx-vlm#installation).

```bash
git clone https://github.com/ASVPATM/localdev-mlx.git
cd localdev-mlx
bash scripts/install.sh --flow-browser
uv tool update-shell
exec zsh
localdev-mlx flow browser-setup
```

To update, run `git pull` and the same install command from this checkout. For text/manual capture only, omit `--flow-browser` and `browser-setup`.

## Project Flow

```bash
cd /path/to/project
localdev-mlx flow          # Start the dashboard
localdev-mlx flow status   # Show its URL
localdev-mlx flow stop     # Stop; keep captures and handoffs
```

1. Click **Open test app**.
2. Press **Option/Alt + Shift + I**, then select an app part. **Esc** cancels.
3. Write a change or question. Use **Check shared behavior** for related parts of the app.
4. Export a **Standard ZIP**, give it to your coding model, then **Refresh** after fixes.

No Git setup or model is required. PMT projects with preview fixtures and plain HTML sites are detected automatically. Other apps: run `localdev-mlx flow init` and configure the [web adapter](src/localdev_mlx/flow/examples/web/project-flow.toml), or use [manual attachments](src/localdev_mlx/flow/examples/manual/project-flow.toml).

**Open** keeps the current preview; **Refresh** uses the latest code and resets preview data. **Clear all app parts** clears captures; **Clear all changes** clears descriptions. Both keep exported handoffs.

### Personal-data Flow

On macOS, the SQLite snapshot adapter supports projects with an explicit offline preview entrypoint and installed Python dependencies. Include that entrypoint in `[project].include`, then configure:

```toml
[personal]
adapter = "sqlite"
source = "/absolute/path/to/your/app/data"
database = "app.sqlite3"
assets = ["objects"] # Only the asset folders needed by this preview
python = ".venv/bin/python" # Project-relative; nested .venv directories also work
entrypoint = "scripts/flow_personal.py"
```

```bash
localdev-mlx flow doctor --personal  # Check setup
localdev-mlx flow --personal         # Start the separate dashboard
localdev-mlx flow stop --personal    # Discard preview data; keep evidence
```

This mode uses an offline, sandboxed **copy**—never writes back to live data. First open needs time and disk space. Stop/Refresh discards preview edits. Sync, background jobs, and app model operations are disabled. Captures and changes stay separate from sample mode.

The entrypoint receives the port as its sole argument and runs from copied source with isolated Python (`-I`). It must explicitly import that copied source, read/write only `PROJECT_FLOW_DATA`, bind `127.0.0.1`, and disable background workers, scheduling, sync and account access. Flow makes a consistent SQLite backup and copies only named asset directories; it does not copy credentials, browser profiles, old backups or the entire live data folder. The OS sandbox denies live-storage access, child processes and outbound networking. Unsupported platforms fail without an unsafe fallback. This adapter does not support arbitrary non-SQLite databases.

## Commands

Run `localdev-mlx COMMAND`, or `localdev-mlx` for an interactive session. Add `--help` for options.

| Command | Purpose |
|---|---|
| `flow` | Preview, capture, and export changes. |
| `init [PATH]` | Prepare Git, settings, and brief agent instructions. |
| `plan "GOAL"` | Create a brief; `--mode full` adds questions and detail. |
| `bug "DESCRIPTION"` | Record a bug. |
| `feature "DESCRIPTION"` | Record a feature. |
| `tweak "DESCRIPTION"` | Record an adjustment. |
| `session` | Manage handoffs: `--show`, `--new`, `--end`, `--cancel`. |
| `configure MODEL` | Save model settings; `--check` checks setup. |
| `model [ROLE]` | Model status; `--load`, `--stop`, `--probe`, `--logs`. |

## Plan from the terminal

```bash
cd /path/to/project
localdev-mlx init .
localdev-mlx plan "An offline reading app"
localdev-mlx plan "Add search" --mode full
localdev-mlx bug "Saving a note freezes the window"
localdev-mlx session --show
localdev-mlx session --new
```

Requests append to one numbered handoff in `.localdev/runtime/sessions/`. By default, they do not edit your app. Full planning needs a configured model and asks questions; Enter skips one and `/done` finishes. Plans are flexible, not fixed phases.

`init` preserves existing work and adds a short Flow setup guide for coding agents. It creates an initial commit when safe. Your external coding model uses your normal checkout.

Optional `--local` runs edits through local models, tests, and review in isolated worktrees, then integrates into `ai/integration`. It needs a clean committed checkout and test commands in `.localdev/config.toml`.

## Optional models

Use MLX-VLM-compatible instruction/chat models that support code and structured JSON and fit in memory. Pass a Hugging Face ID or absolute local model directory.

| Role | Best fit | Tested model |
|---|---|---|
| Planner / reviewer | Strong reasoning and review | [Qwen3.8 27B](https://huggingface.co/orcarouter/Qwen3.8-27B-Uncensored-MLX) |
| Worker | Code edits; smaller models can be faster | [Qwen3.5 9B 4-bit](https://huggingface.co/mlx-community/Qwen3.5-9B-4bit) |

```bash
command -v mlx_vlm.server  # Use this path below
localdev-mlx configure orcarouter/Qwen3.8-27B-Uncensored-MLX \
  --worker-model mlx-community/Qwen3.5-9B-4bit \
  --server /absolute/path/to/mlx_vlm.server --no-thinking
localdev-mlx model worker --probe
localdev-mlx model worker --load
localdev-mlx model --stop
```

`configure` does not load models. `--load` starts or switches the managed [MLX-VLM server](https://github.com/Blaizzy/mlx-vlm#server-fastapi); `--stop` unloads it but keeps downloaded weights. Omit role overrides to use one model.

Flow works without a model. A loaded helper can organize handoffs; it does not edit your app. Tested with MLX-VLM 0.6.15. No Ollama/GGUF, `mlx_lm.server`, hosted APIs, or image/audio inputs.

## Important limits

- Keep `.project-flow/` Git-ignored (`flow init` adds the rule). Nothing uploads automatically. Review exports for personal data; masking is best-effort and uploaded screenshots are not masked.
- Sample adapters run with your normal permissions; use trusted projects. Only personal-data previews are OS-sandboxed.
- WebSockets, streaming, and interactive Streamlit are unsupported. Use a served build or manual evidence.
- Early alpha: review generated changes and back up important data.
