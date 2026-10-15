"""Run PMT's own fixtures with its installed dependencies, only inside a Flow snapshot.

This is a script, not a dependency of PMT. The original checkout, PMT Flow runtime,
real library, environment files, and OS credential vault are never used for app state.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path


def protect_runtime(root):
    """Extra Python/SQLite write protection, not a general-purpose OS sandbox."""

    def writable(path):
        if isinstance(path, (str, bytes, os.PathLike)):
            target = Path(os.fsdecode(path)).resolve()
            if not target.is_relative_to(root):
                raise PermissionError("Synthetic PMT preview cannot write outside its source copy")

    def audit(event, args):
        if event == "open":
            path, mode, flags = args
            if (mode and any(c in mode for c in "wax+")) or flags & (
                os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            ):
                writable(path)
        elif event == "sqlite3.connect" and args[0] != ":memory:":
            if str(args[0]).startswith("file:"):
                raise PermissionError("Synthetic PMT preview does not accept SQLite URI targets")
            writable(args[0])
        elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.truncate"}:
            writable(args[0])
        elif event in {"os.rename", "os.link", "os.symlink"}:
            writable(args[0])
            writable(args[1])
        elif event == "subprocess.Popen":
            raise PermissionError("Synthetic PMT preview cannot launch external programs")

    sys.addaudithook(audit)


def snapshot_root():
    root = Path.cwd().resolve(strict=True)
    # Refuse to run this adapter against a normal project checkout, including when
    # someone copies the command out of the adapter configuration by hand.
    if (
        root.name != "workspace"
        or root.parent.parent.name != "inspections"
        or root.parent.parent.parent.name != ".project-flow"
        or not (root.parent / "baseline.json").is_file()
        or (root / ".flow-data").exists()
    ):
        raise ValueError("PMT preview requires a fresh, disposable Flow inspection snapshot")
    for path in (root, *root.parents):
        if path.is_symlink():
            raise ValueError("PMT preview cannot traverse a symlink")
    return root


def settings_values(data):
    return {
        "_env_file": None,
        "data_dir": data / "library",
        "database_path": data / "library/watchtracker.sqlite3",
        "config_dir": data / "config",
        "env_path": data / "config/preview.env",
        "cache_dir": data / "cache",
        "log_dir": data / "logs",
        "backups_dir": data / "backups",
        "access_mode": "local",
        "database_url_override": None,
        "public_base_url": None,
        "application_secret": None,
        "server_bootstrap_token": None,
        "personal_tailscale_enabled": False,
        "native_actions": False,
        "release_scheduler_enabled": False,
        "release_mode": False,
        "tmdb_token": None,
    }


def main():
    root = snapshot_root()
    protect_runtime(root)
    port = int(sys.argv[1])
    if not 1 <= port <= 65535:
        raise ValueError("A loopback preview port is required")
    scenario = os.environ.get("PROJECT_FLOW_SCENARIO", "balanced")
    seed = int(os.environ.get("PROJECT_FLOW_SEED", "42"))
    # Additional defense if called outside the normal sanitized preview launcher.
    for key in list(os.environ):
        if key.startswith(("WATCHTRACKER_", "PMT_FLOW_")):
            os.environ.pop(key)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    from tools.pmt_flow import preview as fixtures

    if not Path(fixtures.__file__).resolve().is_relative_to(root):
        raise ValueError("PMT fixtures must come from the disposable snapshot")
    data = root / ".flow-data"
    data.mkdir(mode=0o700)
    helper = fixtures.PreviewServer(
        root,
        data / "library",
        lambda message: print(message, flush=True),
        preferred_port=port,
        environment={"PMT_FLOW_SAMPLE_SCENARIO": scenario, "PMT_FLOW_SAMPLE_SEED": str(seed)},
    )
    server, seeding = None, None
    try:
        overlay = helper._prepare_source_overlay()
        sys.path.insert(0, str(overlay))
        import uvicorn
        from watchtracker import remote_client
        from watchtracker.services import secrets

        # These substitutions live only in this disposable process. Even explicit
        # preview UI actions cannot read or write the user's system Keychain.
        remote_client.KeyringCredentialVault = remote_client.MemoryCredentialVault
        secrets._system_keyring = lambda: None
        import watchtracker.app as application
        from watchtracker.app import create_app
        from watchtracker.config import Settings

        if not Path(application.__file__).resolve().is_relative_to(overlay):
            raise ValueError("PMT application must come from the disposable overlay")
        settings = Settings(**settings_values(data))
        settings.ensure_runtime_directories()
        app = create_app(
            settings, secret_store=secrets.SecretStore(settings, keyring_enabled=False)
        )
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        failures = []

        def seed_library():
            try:
                deadline = time.monotonic() + 75
                while not server.started:
                    if server.should_exit or time.monotonic() > deadline:
                        raise ValueError("Synthetic PMT server did not start")
                    time.sleep(0.05)
                helper._seed_if_empty()
                request = urllib.request.Request(
                    f"{helper.url}/api/settings/general",
                    data=json.dumps({"onboarding_complete": True}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="PUT",
                )
                with urllib.request.urlopen(request, timeout=5):
                    pass
                (data / "ready.json").write_text(
                    json.dumps(
                        {
                            "synthetic": True,
                            "scenario": scenario,
                            "seed": seed,
                            "data_dir": str(settings.resolved_data_dir),
                            "keychain": False,
                        }
                    )
                )
                print(
                    "PMT synthetic preview ready; real library and Keychain are disconnected",
                    flush=True,
                )
            except Exception as exc:
                failures.append(exc)
                print(f"PMT synthetic preview failed: {exc}", file=sys.stderr, flush=True)
                server.should_exit = True

        seeding = threading.Thread(target=seed_library, daemon=True)
        seeding.start()
        server.run()
        if failures:
            raise ValueError("Synthetic PMT fixture preparation failed") from failures[0]
    finally:
        if server:
            server.should_exit = True
        if seeding:
            seeding.join(timeout=5)
        helper.stop()


if __name__ == "__main__":
    main()
