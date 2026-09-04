from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from localdev_mlx.config import ModelProfile
from localdev_mlx.models.manager import ModelManager, ModelManagerError
from localdev_mlx.providers.base import ProviderError
from localdev_mlx.providers.mlx_openai import MLXOpenAIProvider, _extract_json
from localdev_mlx.schemas import ImplementationResult


def manager_at(tmp_path):
    manager = ModelManager()
    manager.root = tmp_path
    manager.state_path = tmp_path / "state.json"
    manager.log_path = tmp_path / "server.log"
    return manager


def test_pid_reuse_never_signals_or_unloads(tmp_path, monkeypatch):
    manager = manager_at(tmp_path)
    manager._write_state({"pid": 123, "fingerprint": "original", "host": "127.0.0.1", "port": 8080})
    monkeypatch.setattr(manager, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(manager, "_fingerprint", lambda pid: "reused")
    monkeypatch.setattr(manager, "_signal_pid", lambda *a: pytest.fail("signalled reused PID"))
    monkeypatch.setattr(
        "localdev_mlx.models.manager.httpx.post",
        lambda *a, **k: pytest.fail("unloaded unrelated server"),
    )
    manager.stop()
    assert not manager.state_path.exists()


def test_real_child_is_reaped_even_if_it_never_opens_port(tmp_path, monkeypatch):
    manager = manager_at(tmp_path)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    manager._children[child.pid] = child
    manager._write_state({"pid": child.pid, "host": "127.0.0.1", "port": 65534})
    monkeypatch.setattr(manager, "_port_open", lambda *a: False)
    monkeypatch.setattr("localdev_mlx.models.manager.httpx.post", lambda *a, **k: None)
    try:
        manager.stop()
        assert child.poll() is not None
        assert not manager.state_path.exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_closed_port_is_not_enough_for_live_child(tmp_path, monkeypatch):
    manager = manager_at(tmp_path)
    monkeypatch.setattr(manager, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(manager, "_port_open", lambda *a: False)
    assert not manager._wait_until_stopped(
        pid=123, host="localhost", port=8080, timeout_seconds=0.01
    )


def test_model_lease_prevents_concurrent_switch(tmp_path):
    first, second = manager_at(tmp_path), manager_at(tmp_path)
    with first.lease(), first.lease():
        with pytest.raises(ModelManagerError, match="Another LocalDev"):
            with second.lease():
                pass
    with second.lease():
        pass


@pytest.mark.parametrize(
    "content",
    ['```json\n{"ok": true}\n```', '<think>data</think>{"ok": true}', 'prefix {"ok": true} suffix'],
)
def test_one_controlled_json_extraction(content):
    assert json.loads(_extract_json(content)) == {"ok": True}


def test_invalid_json_is_not_invented():
    with pytest.raises(json.JSONDecodeError):
        json.loads(_extract_json('{"ok": '))


@pytest.mark.parametrize("mode", ["timeout", "bad-shape", "bad-schema"])
def test_provider_errors_and_timeouts_are_bounded(monkeypatch, mode):
    profile = ModelProfile(
        name="test", model="test", executable=Path("/bin/true"), request_timeout_seconds=0.1
    )

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["timeout"].connect == 0.1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, *args, **kwargs):
            if mode == "timeout":
                time.sleep(5)
            payload = (
                {}
                if mode == "bad-shape"
                else {"choices": [{"message": {"content": '{"summary":"no edits"}'}}]}
            )
            return httpx.Response(
                200, json=payload, request=httpx.Request("POST", "http://localhost")
            )

    monkeypatch.setattr("localdev_mlx.providers.mlx_openai.httpx.Client", Client)
    started = time.monotonic()
    with pytest.raises((ProviderError, TimeoutError)):
        MLXOpenAIProvider().complete_structured(
            profile=profile,
            system_prompt="test",
            user_prompt="test",
            response_model=ImplementationResult,
            schema_name="test",
        )
    assert time.monotonic() - started < 1


def test_signal_handler_restored_after_timeout():
    from localdev_mlx.execution.budget import DeadlineExceeded, deadline

    before = signal.getsignal(signal.SIGALRM)
    with pytest.raises(DeadlineExceeded):
        with deadline(0.02, label="test"):
            time.sleep(1)
    assert signal.getsignal(signal.SIGALRM) == before
    assert signal.getitimer(signal.ITIMER_REAL) == (0, 0)
