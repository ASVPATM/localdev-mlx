"""Private, installed-module controller and fingerprint-checked lifecycle."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from contextlib import contextmanager
from pathlib import Path

from localdev_mlx import __version__
from localdev_mlx.config import STATE_ROOT
from localdev_mlx.models.manager import ModelManager

from .host import NoRedirect
from .workspace import atomic_write

API = "localdev.flow-controller/v1"


def project_path(path):
    root = Path(path).resolve(strict=True)
    if not root.is_dir() or root in {Path(root.anchor), Path.home().resolve()}:
        raise ValueError("Choose a specific project directory, not a home/filesystem root")
    if (root / ".project-flow").is_symlink():
        raise ValueError("Flow runtime must not be a symlink")
    return root


def runtime(root, mode="sample"):
    if mode not in {"sample", "personal"}:
        raise ValueError("Unknown Flow mode")
    identity = str(root) if mode == "sample" else str(root) + "\0personal"
    path = STATE_ROOT / "flow" / hashlib.sha256(identity.encode()).hexdigest()
    for parent in [STATE_ROOT / "flow", path]:
        if parent.is_symlink():
            raise ValueError("Flow controller state must not be a symlink")
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent.chmod(0o700)
    return path


def read_json(path):
    if path.is_symlink():
        raise ValueError("Refusing symlinked Flow control state")
    if not path.exists():
        return {}
    if path.stat().st_size > 65536:
        raise ValueError("Oversized Flow control state")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Invalid Flow control state")
    return value


@contextmanager
def lifecycle_lock(root, mode="sample"):
    path = runtime(root, mode) / "lifecycle.lock"
    if path.is_symlink():
        raise ValueError("Refusing symlinked lifecycle lock")
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another Flow start/stop is in progress; retry shortly") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def request(state, endpoint, *, token=None):
    port = state.get("port")
    if type(port) is not int or not 0 < port < 65536:
        raise ValueError("Invalid controller port")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{endpoint}",
        data=b"{}" if token else None,
        headers={"Content-Type": "application/json", **({"X-Flow-Token": token} if token else {})},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(req, timeout=1) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError("Oversized controller response")
    return json.loads(raw)


def verified(root, state, mode="sample"):
    if (
        state.get("api") != API
        or state.get("project") != str(root)
        or not isinstance(state.get("version"), str)
        or state.get("mode", "sample") != mode
        or type(state.get("pid")) is not int
        or not state.get("fingerprint")
        or ModelManager._fingerprint(state["pid"]) != state["fingerprint"]
    ):
        return False
    try:
        capabilities = request(state, "/api/capabilities")
    except (OSError, ValueError):
        return False
    return (
        capabilities.get("project") == str(root)
        and capabilities.get("mode", "sample") == mode
        and capabilities.get("hosted") is True
        and capabilities.get("api") == "project-flow.control/v1"
        and capabilities.get("controller")
        == {key: state[key] for key in ("api", "instance", "version")}
        and "export-jobs" in capabilities.get("capabilities", [])
    )


def status(project, *, mode="sample"):
    root = project_path(project)
    state = read_json(runtime(root, mode) / "controller.json")
    running = verified(root, state, mode)
    unverified = bool(
        not running
        and state.get("fingerprint")
        and ModelManager._fingerprint(state.get("pid", 0)) == state["fingerprint"]
    )
    return {
        "project": str(root),
        "mode": mode,
        "state": "running" if running else "unverified" if unverified else "stopped",
        "url": f"http://127.0.0.1:{state['port']}/" if running else None,
        "pid": state.get("pid") if running else None,
        "message": "Process is alive but its endpoint/version could not be verified; no process was signalled"
        if unverified
        else "Use flow stop to close Flow"
        if running
        else "Saved evidence is preserved",
    }


def start(project, *, port=0, open_browser=True, allow_load=None, mode="sample"):
    root = project_path(project)
    with lifecycle_lock(root, mode):
        config_for(root, mode)  # Validate personal isolation before spawning a controller.
        private = runtime(root, mode)
        preferences = read_json(private / "preferences.json")
        if allow_load is not None:
            preferences["allow_model_load"] = allow_load
            atomic_write(private / "preferences.json", json.dumps(preferences).encode())
        state = read_json(private / "controller.json")
        if verified(root, state, mode):
            result = status(root, mode=mode)
            if allow_load is not None:
                result["message"] = "Helper preference saved; stop/start Flow to apply it"
        else:
            if (
                state.get("fingerprint")
                and ModelManager._fingerprint(state.get("pid", 0)) == state["fingerprint"]
            ):
                raise ValueError(
                    "Existing Flow process cannot be verified; preserve it and inspect flow doctor"
                )
            instance = secrets.token_hex(16)
            from .processes import OwnedProcesses

            OwnedProcesses.recover(private / "children.json")
            # No issue text, model settings or control capabilities in argv or logs.
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "localdev_mlx.flow.controller",
                    "--project",
                    str(root),
                    "--mode",
                    mode,
                    "--port",
                    str(port),
                    "--instance",
                    instance,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                end = time.monotonic() + 12
                while time.monotonic() < end:
                    state = read_json(private / "controller.json")
                    if state.get("instance") == instance and verified(root, state, mode):
                        result = status(root, mode=mode)
                        break
                    if process.poll() is not None:
                        error = read_json(private / "startup-error.json")
                        raise ValueError(
                            error.get("message", "Flow failed to start; run flow doctor")
                        )
                    time.sleep(0.1)
                else:
                    raise ValueError("Flow startup timed out; run flow doctor")
            except BaseException:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                raise
        if open_browser:
            webbrowser.open(result["url"])
        return result


def stop(project, *, mode="sample"):
    root = project_path(project)
    with lifecycle_lock(root, mode):
        state = read_json(runtime(root, mode) / "controller.json")
        if not verified(root, state, mode):
            if (
                state.get("fingerprint")
                and ModelManager._fingerprint(state.get("pid", 0)) == state["fingerprint"]
            ):
                raise ValueError("Flow process is alive but unverified; no process was signalled")
            from .processes import OwnedProcesses

            OwnedProcesses.recover(runtime(root, mode) / "children.json")
            return {
                "state": "stopped",
                "message": "No verified Flow controller; no process was signalled. Evidence is preserved.",
            }
        request(state, "/api/shutdown", token=state["token"])
        end = time.monotonic() + 15
        while time.monotonic() < end:
            if ModelManager._fingerprint(state["pid"]) != state["fingerprint"]:
                return {
                    "state": "stopped",
                    "message": "Flow stopped; captures and exports are preserved.",
                }
            time.sleep(0.1)
        return {
            "state": "stopping",
            "message": "Owned preview/browser cleanup is still finishing; check flow status.",
        }


def config_for(root, mode="sample"):
    from .detection import configuration
    from .personal import for_mode

    return for_mode(configuration(root), mode)


def doctor(project, *, mode="sample"):
    from .detection import setup_message

    root = project_path(project)
    config = config_for(root, mode)
    return {
        **status(root, mode=mode),
        "version": __version__,
        "bundled": True,
        "adapter": str(root / "project-flow.toml"),
        "preview_kind": config.preview_kind,
        "preview_configured": bool(config.preview),
        "adapter_name": config.adapter_name,
        "autodetected": config.autodetected,
        "setup": setup_message(config),
        "browser_extra": importlib.util.find_spec("playwright") is not None,
        "image_support": importlib.util.find_spec("PIL") is not None,
        "browser_setup": "localdev-mlx flow browser-setup",
        "evidence": str(config.data),
        "manual_evidence": "Available without an adapter, Git, browser, or model",
    }


def main():
    parser = argparse.ArgumentParser(description="Private LocalDev Flow controller")
    parser.add_argument("--project", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--mode", choices=("sample", "personal"), default="sample")
    args = parser.parse_args()
    root = project_path(args.project)
    private = runtime(root, args.mode)
    os.umask(0o077)
    flow = server = proxy = journal = None

    def interrupted(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        from .engine import Flow
        from .localdev_host import localdev_host
        from .processes import OwnedProcesses
        from .web import create_servers

        preferences = read_json(private / "preferences.json")
        journal = OwnedProcesses(private / "children.json")
        flow = Flow(
            config_for(root, args.mode),
            host=localdev_host(
                root, allow_load=preferences.get("allow_model_load") is True, journal=journal
            ),
        )
        flow.controller_identity = {"api": API, "instance": args.instance, "version": __version__}
        try:
            server, proxy = create_servers(flow, args.port)
        except OSError:
            if not args.port:
                raise
            server, proxy = create_servers(flow, 0)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        control = read_json(flow.config.data / "control.json")
        state = {
            **control,
            **flow.controller_identity,
            "project": str(root),
            "mode": args.mode,
            "pid": os.getpid(),
            "fingerprint": ModelManager._fingerprint(os.getpid()),
        }
        atomic_write(private / "controller.json", json.dumps(state).encode())
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        atomic_write(
            private / "startup-error.json",
            json.dumps(
                {
                    "message": f"Flow startup failed ({type(exc).__name__}); check adapter configuration with flow doctor"
                }
            ).encode(),
        )
        raise
    finally:
        if proxy:
            proxy.shutdown()
            proxy.server_close()
        if server:
            server.server_close()
        if flow:
            flow.close()
        if journal:
            journal.close()


if __name__ == "__main__":
    main()
