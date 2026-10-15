"""Read-only, bounded detection. Never execute a manifest to discover a command."""

from __future__ import annotations

import json
import tomllib

from .config import Config
from .workspace import safe_path

PMT_FIXTURES = (
    "tools/__init__.py",
    "tools/pmt_flow/__init__.py",
    "tools/pmt_flow/config.py",
    "tools/pmt_flow/preview.py",
    "tools/pmt_flow/recommendation_fixtures.py",
    "tools/pmt_flow/recommendation-preview.js",
    "tools/pmt_flow/sample_catalog.json",
)


def detect_config(root):
    """Recognize supported layouts; an explicit adapter always takes precedence."""
    try:
        manifest = safe_path(root, "pyproject.toml")
        raw = (
            tomllib.loads(manifest.read_text())
            if manifest.is_file() and manifest.stat().st_size <= 65536
            else {}
        )
        if raw.get("project", {}).get("name") == "personal-media-tracker" and all(
            safe_path(root, name).is_file() for name in (*PMT_FIXTURES, "src/watchtracker/app.py")
        ):
            return Config(
                root,
                include=("src/watchtracker", *PMT_FIXTURES, "pyproject.toml"),
                # This is the credential-handling implementation, not credential data.
                allowed_source_files=("src/watchtracker/services/secrets.py",),
                preview=("{project_python}", "-I", "{pmt_preview}", "{port}"),
                preview_kind="web",
                preview_mutations=True,
                preview_timeout=90,
                preview_ready_file=".flow-data/ready.json",
                supports_seed=True,
                scenarios=("balanced", "compact", "dense", "anime", "sparse"),
                adapter_name="PMT synthetic preview",
                autodetected=True,
                # PMT's application JS is already close to the generic 512 KB bound.
                max_file_bytes=2 * 1024 * 1024,
            )
        # A plain static site needs no package runner, dependency install, or application data.
        if safe_path(root, "index.html").is_file() and not any(
            (root / name).exists()
            for name in ("package.json", "pyproject.toml", "Cargo.toml", "Gemfile")
        ):
            names = [
                path.name
                for path in root.iterdir()
                if path.is_file()
                and not path.is_symlink()
                and path.suffix.lower() in {".html", ".css", ".js", ".svg", ".png", ".ico"}
            ]
            names.extend(
                name
                for name in ("assets", "css", "js", "images", "fonts", "public")
                if safe_path(root, name).is_dir()
            )
            return Config(
                root,
                include=tuple(sorted(names)),
                preview=("{python}", "-I", "-m", "http.server", "{port}", "--bind", "127.0.0.1"),
                adapter_name="Static HTML preview",
                autodetected=True,
                max_file_bytes=8 * 1024 * 1024,
            )
    except (OSError, ValueError, TypeError, AttributeError):
        pass  # An ambiguous/invalid manifest is never permission to guess and execute.
    return None


def configuration(root):
    from .config import load_config

    if (root / "project-flow.toml").exists() or (root / "project-flow.toml").is_symlink():
        return load_config(root)
    return detect_config(root) or Config(
        root, preview_kind="command", adapter_name="Manual evidence"
    )


def setup_message(config):
    if config.mode == "personal":
        return (
            "Personal-data Flow: an offline, OS-isolated copy of your data; never writes back. "
            "Background jobs, sync, external sessions and app models are disabled. "
            "Open reuses the current preview; Refresh takes a new data/code snapshot. "
            "Stop/Refresh discards preview data edits; captures and handoffs stay private."
        )
    if not config.preview:
        return (
            "No test-app launch configuration. Manual capture/export still works. "
            "Run localdev-mlx flow init, configure the preview command in project-flow.toml, then restart Flow. "
            "PMT with its preview fixtures and plain static sites are detected automatically."
        )
    if config.autodetected:
        return (
            f"Detected {config.adapter_name}. Open test app asks once before running a disposable "
            "source copy; no model or Git initialization is needed."
        )
    return "Test app uses project-flow.toml and a disposable source copy."


def adapter_document(config):
    """Optional portable, reviewable file; automatic detection itself writes nothing."""
    values = {
        "include": list(config.include),
        "exclude": list(config.exclude),
        "allowed_source_files": list(config.allowed_source_files),
        "preview": [list(config.preview)] if config.preview else [],
        "preview_kind": config.preview_kind,
        "preview_mutations": config.preview_mutations,
        "preview_timeout": config.preview_timeout,
        "preview_ready_file": config.preview_ready_file,
        "scenarios": list(config.scenarios),
        "supports_seed": config.supports_seed,
        "max_file_bytes": config.max_file_bytes,
    }
    return (
        "[project]\n"
        + "\n".join(f"{key} = {json.dumps(value)}" for key, value in values.items())
        + "\n"
    )
