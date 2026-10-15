"""Trusted LocalDev services. UI polling cannot load or switch a model."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import selectors
import subprocess
import sys
import time
from dataclasses import asdict

from localdev_mlx.config import ConfigurationError, load_global_config
from localdev_mlx.models.manager import ModelManager, ModelManagerError

from .host import ORGANIZER_PROMPT, HostServices

MAX_RESPONSE = 128 * 1024


def identity(profile):
    return hashlib.sha256(
        json.dumps(
            {"profile": asdict(profile), "prompt": ORGANIZER_PROMPT, "protocol": 1},
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()


def selected_model(manager, config):
    """Call only under the nonblocking model lease; never mutate residency."""
    profiles = list(dict.fromkeys([config.worker.name, *config.models]))
    state = manager._read_state()
    owned = bool(state and manager._owned(state))
    for name in profiles:
        profile = config.models[name]
        if profile.host not in {"127.0.0.1", "localhost", "::1"}:
            continue
        if not manager._port_open(profile.host, profile.port):
            continue
        served = manager._served_models(profile)
        if owned:
            if (
                state.get("model") == profile.model
                and state.get("host") == profile.host
                and state.get("port") == profile.port
                and profile.model in served
            ):
                return profile, True
        elif served == [profile.model]:
            # Preserve ModelManager's strict, single-model external-server rule.
            return profile, True
    return config.worker, False


def helper_status(allow_load=False):
    try:
        config = load_global_config()
    except (ConfigurationError, OSError, ValueError):
        return {
            "provider": "MLX",
            "model": "",
            "state": "not_configured",
            "identity": "",
            "can_organize": False,
            "can_load": False,
        }
    profile = config.worker
    state = "not_loaded"
    try:
        manager = ModelManager()
        with manager.lease():
            profile, resident = selected_model(manager, config)
            if resident:
                state = "ready"
    except ModelManagerError:
        state = "busy"
    except (OSError, ValueError):
        state = "unavailable"
    return {
        "provider": "MLX",
        "model": profile.model
        if not profile.model.startswith("/")
        else profile.model.rsplit("/", 1)[-1],
        "state": state,
        "identity": identity(profile),
        "can_organize": state == "ready" or (state == "not_loaded" and allow_load),
        "can_load": bool(allow_load),
        "message": "Preload with localdev-mlx model worker --load" if state == "not_loaded" else "",
    }


class OrganizedAdvice(dict):
    def __init__(self, value, metadata):
        super().__init__(value)
        self.metadata = metadata


def cleanup_worker_model(pid, token):
    """Only a model started by this exact worker may be cleaned after cancellation."""
    manager = ModelManager()
    try:
        with manager.lease():
            state = manager._read_state()
            if (
                state
                and state.get("owner_pid") == pid
                and state.get("flow_worker_token") == token
                and state.get("flow_stop_after_run") is True
                and manager._owned(state)
            ):
                manager.stop()
    except (ModelManagerError, OSError):
        pass


def organize_worker(payload, cancel, timeout, *, allow_load=False, journal=None):
    token = secrets.token_hex(24)
    request = json.dumps(
        {
            "payload": payload,
            "timeout": min(120, timeout),
            "allow_load": allow_load,
            "token": token,
        },
        ensure_ascii=False,
    ).encode()
    if len(json.dumps(payload, ensure_ascii=False)) > 24000 or len(request) > 100000:
        raise ValueError("Batch exceeds the local-analysis input budget")
    end = time.monotonic() + timeout
    environment = {
        **os.environ,
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
    }
    environment.pop("PYTHONPATH", None)
    process = subprocess.Popen(
        [sys.executable, "-I", "-m", "localdev_mlx.flow.organizer_worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        start_new_session=True,
    )
    output = bytearray()
    # Drain stderr without retaining private provider diagnostics or allowing pipe deadlock.
    stderr_size = 0
    try:
        if journal:
            journal.add(process, kind="organizer", token=token)
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdin, process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE)
            selector.register(process.stdout, selectors.EVENT_READ)
            selector.register(process.stderr, selectors.EVENT_READ)
            pending = memoryview(request)
            while selector.get_map():
                if cancel.is_set():
                    raise ValueError("Local organization cancelled; ordinary handoff used")
                if time.monotonic() >= end:
                    raise ValueError(
                        "Local organization timed out; preload the helper or export ordinarily"
                    )
                for key, _ in selector.select(min(0.05, max(0.001, end - time.monotonic()))):
                    stream = key.fileobj
                    if stream is process.stdin:
                        try:
                            pending = pending[os.write(stream.fileno(), pending) :]
                        except BrokenPipeError:
                            pending = memoryview(b"")
                        if not pending:
                            selector.unregister(stream)
                            stream.close()
                        continue
                    chunk = os.read(stream.fileno(), 16384)
                    if not chunk:
                        selector.unregister(stream)
                    elif stream is process.stdout:
                        output.extend(chunk)
                        if len(output) > MAX_RESPONSE:
                            raise ValueError("Local model response exceeds 128 KB")
                    else:
                        stderr_size += len(chunk)
                        if stderr_size > MAX_RESPONSE:
                            raise ValueError("Organizer worker exceeded its diagnostic budget")
            process.wait(timeout=max(0.001, end - time.monotonic()))
        body = json.loads(output)
        if process.returncode or not body.get("ok"):
            raise ValueError(body.get("error", "Local organization unavailable"))
        return OrganizedAdvice(body["advice"], body["metadata"])
    finally:
        interrupted = process.poll() is None
        if interrupted:
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
        if interrupted and allow_load:
            # This background callback may finish cleanup after ordinary export is ready.
            # ExportService.close joins it; a controller crash has the private journal.
            cleanup_worker_model(process.pid, token)


def localdev_host(project, *, allow_load=False, journal=None):
    from .session_evidence import link_export

    return HostServices(
        name="LocalDev MLX",
        status=lambda: helper_status(allow_load),
        organize=lambda payload, cancel, timeout: organize_worker(
            payload, cancel, timeout, allow_load=allow_load, journal=journal
        ),
        embedded=True,
        exported=lambda directory: link_export(project, directory),
        process_started=journal.add if journal else None,
        browser_started=journal.browser_started if journal else None,
    )
