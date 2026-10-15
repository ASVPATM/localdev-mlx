from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def release_check():
    script = Path(__file__).resolve().parents[1] / "scripts/release_check.py"
    spec = importlib.util.spec_from_file_location("release_check", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("flow_browser", [False, True])
def test_wheel_validation_reuses_locked_source_dependencies(
    release_check, tmp_path, monkeypatch, flow_browser
):
    calls = []
    monkeypatch.setattr(release_check, "run", lambda args, **kwargs: calls.append((args, kwargs)))
    wheel = tmp_path / "localdev_mlx-0.6.0-py3-none-any.whl"
    python = tmp_path / "wheel-env/bin/python"
    constraints = tmp_path / "wheel-constraints.txt"
    environment = {"LOCALDEV_MLX_HOME": str(tmp_path / "runtime")}
    release_check.install_wheel(
        wheel, python, constraints, cwd=tmp_path, env=environment, flow_browser=flow_browser
    )
    export, install = [args for args, _ in calls]
    assert export[:3] == ["uv", "export", "--locked"]
    assert "--no-emit-project" in export  # Never replace the wheel with an editable checkout.
    assert "--no-hashes" in export
    assert export[export.index("--output-file") + 1] == constraints
    assert ("--extra" in export) == flow_browser
    if flow_browser:
        assert export[export.index("--extra") + 1] == "flow-browser"
    assert install[:3] == ["uv", "pip", "install"]
    assert install[install.index("--constraint") + 1] == constraints
    assert install[install.index("--python") + 1] == python
    assert str(wheel) + ("[flow-browser]" if flow_browser else "") in install
    assert "pytest>=8,<10" in install
    assert all(kwargs == {"cwd": tmp_path, "env": environment} for _, kwargs in calls)


def test_lock_export_failure_never_falls_back_to_unlocked_install(
    release_check, tmp_path, monkeypatch
):
    calls = []

    def fail(args, **kwargs):
        calls.append(args)
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(release_check, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        release_check.install_wheel(
            tmp_path / "package.whl",
            tmp_path / "python",
            tmp_path / "constraints.txt",
            cwd=tmp_path,
            env={},
            flow_browser=True,
        )
    assert len(calls) == 1 and calls[0][:2] == ["uv", "export"]
