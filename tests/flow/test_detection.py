from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import replace

import pytest

from localdev_mlx.flow.config import Config, load_config
from localdev_mlx.flow.detection import PMT_FIXTURES, adapter_document, configuration, detect_config
from localdev_mlx.flow.engine import Flow
from localdev_mlx.flow.host import HostServices
from localdev_mlx.flow.pmt_preview import settings_values, snapshot_root
from localdev_mlx.flow.workspace import files


def pmt(root):
    for name in (*PMT_FIXTURES, "src/watchtracker/app.py", "src/watchtracker/services/secrets.py"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fixture source\n")
    (root / "pyproject.toml").write_text('[project]\nname = "personal-media-tracker"\n')
    return configuration(root)


def host():
    return HostServices("test", lambda: {"state": "not_configured"})


def test_pmt_detection_is_read_only_narrow_and_keeps_credentials_excluded(tmp_path):
    cfg = pmt(tmp_path)
    screenshots = tmp_path / "docs/screenshots"
    screenshots.mkdir(parents=True)
    (screenshots / "large.png").write_bytes(b"x" * 524289)
    (tmp_path / "src/watchtracker/.env").write_text("do-not-copy")
    (tmp_path / "src/watchtracker/services/secrets.env").write_text("do-not-copy")
    original = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    assert cfg.autodetected and cfg.preview_kind == "web"
    assert cfg.scenarios[0] == "balanced" and cfg.supports_seed
    assert cfg.preview_ready_file == ".flow-data/ready.json"
    manifest = files(cfg, tmp_path)
    assert "src/watchtracker/services/secrets.py" in manifest
    assert all(".env" not in name and "docs/" not in name for name in manifest)
    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")) == original
    (tmp_path / "project-flow.toml").write_text(adapter_document(cfg))
    loaded = configuration(tmp_path)
    assert not loaded.autodetected  # An explicitly reviewed config wins.
    assert loaded.preview == cfg.preview
    assert loaded.allowed_source_files == cfg.allowed_source_files
    assert loaded.preview_ready_file == cfg.preview_ready_file


def test_partial_pmt_and_arbitrary_manifests_are_not_executed(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "personal-media-tracker"\n')
    assert detect_config(tmp_path) is None
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "touch never-run"}}))
    assert not configuration(tmp_path).preview
    assert not (tmp_path / "never-run").exists()


def test_static_detection_ignores_docs_and_never_runs_node_scripts(tmp_path):
    (tmp_path / "index.html").write_text("<h1>Example</h1>")
    (tmp_path / "app.js").write_text("const ready = true;")
    (tmp_path / ".env").write_text("do-not-copy")
    cfg = configuration(tmp_path)
    assert cfg.autodetected and not cfg.preview_mutations
    assert cfg.include == ("app.js", "index.html")
    (tmp_path / "package.json").write_text('{"scripts":{"dev":"vite"}}')
    assert detect_config(tmp_path) is None


def test_missing_adapter_reports_setup_before_snapshot_and_does_not_claim_stale(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/large.py").write_bytes(b"x" * 524289)
    flow = Flow(configuration(tmp_path), host=host())
    try:
        assert flow.view()["preview_stale"] is False
        assert "No test-app launch configuration" in flow.view()["preview_setup"]
        with pytest.raises(ValueError, match="No test-app launch configuration"):
            flow.open_test_app()
        assert flow.inspection_thread is None
        assert not (tmp_path / ".project-flow/inspections").exists()
    finally:
        flow.close()


def test_first_snapshot_failure_is_not_source_drift(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/large.py").write_bytes(b"x" * 524289)
    flow = Flow(Config(tmp_path, preview=("never-run",)), host=host())
    try:
        flow.inspect({})
        flow.inspection_thread.join(5)
        view = flow.view()
        assert view["inspector_status"] == "error"
        assert "snapshot bound" in view["evidence_warning"]
        assert not view["preview_stale"] and not view["preview_source_error"]
    finally:
        flow.close()


def test_detection_needs_one_time_confirmation_and_preserves_evidence(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<h1>Example</h1>")
    cfg = configuration(tmp_path)
    flow = Flow(cfg, host=host())
    try:
        flow.save_issues([{"id": "one", "prompt": "Keep my description"}])
        with pytest.raises(ValueError, match="Confirm"):
            flow.inspect({})
        assert flow.view()["preview_needs_confirmation"]
        monkeypatch.setattr(flow, "start_preview", lambda: None)
        monkeypatch.setattr(flow, "open_preview", lambda: None)
        flow.inspect({"confirmed": True})
        flow.inspection_thread.join(5)
        assert not flow.view()["preview_needs_confirmation"]
    finally:
        flow.close()
    reopened = Flow(cfg, host=host())
    try:
        assert not reopened.view()["preview_needs_confirmation"]
        assert reopened.state["issues"][0]["prompt"] == "Keep my description"
        reopened.config = replace(cfg, preview=("different-command",))
        assert reopened.view()["preview_needs_confirmation"]
    finally:
        reopened.close()


def test_preview_waits_for_seed_readiness_not_just_listening_port(tmp_path, monkeypatch):
    code = (
        "import pathlib, socket, sys, time; "
        "sock=socket.socket(); sock.bind(('127.0.0.1',int(sys.argv[1]))); sock.listen(); "
        "time.sleep(.4); pathlib.Path('.flow-data').mkdir(); "
        "pathlib.Path('.flow-data/ready.json').write_text('{}'); time.sleep(20)"
    )
    cfg = Config(
        tmp_path,
        preview=(sys.executable, "-c", code, "{port}"),
        preview_ready_file=".flow-data/ready.json",
        preview_timeout=5,
    )
    flow = Flow(cfg, host=host())
    try:
        monkeypatch.setattr(flow, "open_preview", lambda: None)
        started = time.monotonic()
        flow.inspect({})
        flow.inspection_thread.join(8)
        assert flow.state["inspector_status"] == "ready", flow.state
        assert time.monotonic() - started >= 0.4
        assert (flow.preview_workspace / cfg.preview_ready_file).is_file()
    finally:
        process = flow.preview_process
        flow.close()
        assert process.poll() is not None


@pytest.mark.parametrize("path", ["../ready", "/tmp/ready", ".flow-data/../ready", "src/ready"])
def test_readiness_path_must_be_disposable(tmp_path, path):
    with pytest.raises(ValueError, match="preview_ready_file"):
        Config(tmp_path, preview_ready_file=path)


@pytest.mark.parametrize("path", ["src/secrets.env", "../secrets.py", "src/*.py", "/tmp/secret.py"])
def test_source_name_override_cannot_allow_credentials_or_escape_scope(tmp_path, path):
    with pytest.raises(ValueError, match="allowed_source_files"):
        Config(tmp_path, allowed_source_files=(path,))


def test_pmt_bridge_refuses_original_checkout_and_all_runtime_paths_are_private(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="fresh, disposable"):
        snapshot_root()
    data = tmp_path / ".flow-data"
    settings = settings_values(data)
    for key in (
        "data_dir",
        "config_dir",
        "database_path",
        "env_path",
        "cache_dir",
        "log_dir",
        "backups_dir",
    ):
        assert settings[key].is_relative_to(data)
    assert settings["_env_file"] is None
    assert settings["database_url_override"] is None
    assert settings["native_actions"] is False


def test_symlinked_adapter_and_fixture_are_not_followed(tmp_path):
    cfg = pmt(tmp_path)
    source = tmp_path / PMT_FIXTURES[-1]
    source.unlink()
    source.symlink_to(tmp_path / "src/watchtracker/app.py")
    assert detect_config(tmp_path) is None
    (tmp_path / "real.toml").write_text(adapter_document(cfg))
    (tmp_path / "project-flow.toml").symlink_to(tmp_path / "real.toml")
    with pytest.raises(ValueError, match="regular"):
        load_config(tmp_path)


def test_pmt_runtime_guard_blocks_external_writes_databases_and_processes(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sentinel = tmp_path / "real-library.txt"
    sentinel.write_text("keep this")
    script = """
import os, sqlite3, subprocess, sys
from pathlib import Path
from localdev_mlx.flow.pmt_preview import protect_runtime
root, original = map(Path, sys.argv[1:])
protect_runtime(root)
for operation in (
    lambda: original.write_text("bad"),
    lambda: original.unlink(),
    lambda: sqlite3.connect(str(original)),
    lambda: sqlite3.connect("file:" + str(original)),
    lambda: subprocess.run([sys.executable, "-c", "pass"]),
):
    try:
        operation()
    except PermissionError:
        pass
    else:
        raise AssertionError("External operation was not blocked")
(root / "test.txt").write_text("temporary data")
sqlite3.connect(str(root / "test.sqlite3")).close()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(workspace), str(sentinel)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert sentinel.read_text() == "keep this"
    assert (workspace / "test.txt").read_text() == "temporary data"
