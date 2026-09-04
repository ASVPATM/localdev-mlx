from __future__ import annotations

import json
from pathlib import Path

import pytest

from localdev_mlx.config import ModelProfile
from localdev_mlx.models.manager import ModelManager, ModelManagerError


def _profile(model: str) -> ModelProfile:
    return ModelProfile(
        name=model.rsplit("/", 1)[-1],
        model=model,
        executable=Path("/bin/true"),
        enable_thinking=False,
    )


def _manager(tmp_path: Path) -> ModelManager:
    manager = ModelManager()
    manager.root = tmp_path
    manager.state_path = tmp_path / "state.json"
    manager.log_path = tmp_path / "server.log"
    return manager


def test_managed_state_is_authoritative_when_models_endpoint_lists_every_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    manager.state_path.write_text(
        json.dumps(
            {
                "pid": 123,
                "profile": "planner",
                "model": "example/active",
                "host": "127.0.0.1",
                "port": 8080,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(manager, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(manager, "_port_open", lambda _host, _port: True)
    monkeypatch.setattr(
        manager,
        "_served_models",
        lambda _profile: ["example/active", "example/requested"],
    )

    status = manager.status(_profile("example/requested"))

    assert status["managed"] is True
    assert status["matches_requested"] is False


def test_ensure_switches_a_different_managed_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    manager.state_path.write_text(
        json.dumps(
            {
                "pid": 123,
                "profile": "old",
                "model": "example/old",
                "host": "127.0.0.1",
                "port": 8080,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(manager, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(manager, "_port_open", lambda _host, _port: True)
    calls: list[str] = []
    monkeypatch.setattr(manager, "stop", lambda: calls.append("stop"))
    monkeypatch.setattr(manager, "start", lambda _profile: calls.append("start"))

    manager.ensure(_profile("example/new"))

    assert calls == ["stop", "start"]


def test_external_server_must_advertise_requested_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    monkeypatch.setattr(manager, "_port_open", lambda _host, _port: True)
    monkeypatch.setattr(manager, "_served_models", lambda _profile: ["example/other"])

    with pytest.raises(ModelManagerError):
        manager.ensure(_profile("example/requested"))
