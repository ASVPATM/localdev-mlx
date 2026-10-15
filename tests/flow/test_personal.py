from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
import sys
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from typer.testing import CliRunner

from localdev_mlx.cli import app
from localdev_mlx.flow import controller, personal
from localdev_mlx.flow.config import Config, load_config
from localdev_mlx.flow.engine import Flow
from localdev_mlx.flow.evidence import BrowserRecorder


@pytest.fixture
def personal_config(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "project"
    source = tmp_path.resolve() / "live"
    root.mkdir()
    source.mkdir()
    (root / "src/canvas_optimizer").mkdir(parents=True)
    (root / "src/canvas_optimizer/web.py").write_text("# synthetic app\n")
    (root / ".venv/bin").mkdir(parents=True)
    (root / ".venv/bin/python").symlink_to(sys.executable)
    with sqlite3.connect(source / "app.sqlite3") as conn:
        conn.execute("CREATE TABLE example (value TEXT)")
        conn.execute("INSERT INTO example VALUES ('original')")
    (source / "objects").mkdir()
    (source / "objects/example").write_text("original asset")
    (source / "credentials.json").write_text("must not copy")
    (source / "runtime").mkdir()
    (source / "runtime/browser-state").write_text("must not copy")
    monkeypatch.setattr(personal, "require_sandbox", lambda: None)
    return Config(root, personal_adapter="canvasmaster", personal_source=str(source))


def test_config_separates_modes_and_does_not_inherit_secrets(personal_config, monkeypatch):
    original = personal_config
    config = personal.for_mode(original, "personal")
    assert personal.for_mode(original, "sample") is original
    assert config.data == original.data / "personal"
    assert config.preview_kind == "web" and config.preview_mutations
    assert not config.autodetected and not config.capture_narrow
    assert config.command(config.preview, port=12345)[-1] == "12345"
    monkeypatch.setenv("CANVAS_TOKEN", "not-inherited")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-inherited")
    environment = config.environment(config.root / "copy")
    assert "CANVAS_TOKEN" not in environment and "AWS_SECRET_ACCESS_KEY" not in environment
    assert environment["PROJECT_FLOW_PERSONAL"] == "1"
    assert environment["HOME"] == str(config.root / "copy/.flow-home")
    (original.root / "project-flow.toml").write_text(
        '[personal]\nadapter="canvasmaster"\nsource=' + json.dumps(original.personal_source)
    )
    assert load_config(original.root).personal_source == original.personal_source


@pytest.mark.parametrize("invalid", ["unknown", "missing-source", "symlink", "overlap"])
def test_invalid_adapters_and_live_paths_fail_closed(personal_config, invalid):
    config = personal_config
    if invalid == "unknown":
        config = replace(config, personal_adapter="generic")
    elif invalid == "missing-source":
        config = replace(config, personal_source="")
    elif invalid == "symlink":
        link = config.root / "live-link"
        link.symlink_to(config.personal_source)
        config = replace(config, personal_source=str(link))
    else:
        config = replace(config, personal_source=str(config.root))
    with pytest.raises(ValueError):
        personal.for_mode(config, "personal")
    assert not config.data.exists()


def test_unsupported_os_never_falls_back(monkeypatch):
    monkeypatch.setattr(personal.sys, "platform", "linux")
    with pytest.raises(ValueError, match="no unsafe fallback"):
        personal.require_sandbox()


def test_consistent_wal_snapshot_never_writes_back(personal_config):
    config = personal.for_mode(personal_config, "personal")
    live = Path(config.personal_source)
    workspace = config.root / "workspace"
    workspace.mkdir()
    with sqlite3.connect(live / "app.sqlite3") as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO example VALUES ('latest WAL commit')")
        writer.commit()
        before_db = (live / "app.sqlite3").read_bytes()
        before_wal = (live / "app.sqlite3-wal").read_bytes()
        record = personal.prepare_snapshot(config, workspace, threading.Event())
        assert (live / "app.sqlite3").read_bytes() == before_db
        assert (live / "app.sqlite3-wal").read_bytes() == before_wal
        data = workspace / ".flow-data"
        with sqlite3.connect(data / "app.sqlite3") as copy:
            assert copy.execute("SELECT count(*) FROM example").fetchone()[0] == 2
            copy.execute("DELETE FROM example")
        (data / "objects/example").write_text("changed copy")
        assert (live / "objects/example").read_text() == "original asset"
        assert writer.execute("SELECT count(*) FROM example").fetchone()[0] == 2
        assert not (data / "credentials.json").exists()
        assert not (data / "runtime").exists()
        assert record["offline"] and not record["writeback"]
        assert record["asset_files"] == 1
        assert (data / "app.sqlite3").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("failure", ["cancel", "budget", "symlink"])
def test_snapshot_fails_without_running_project_code(personal_config, monkeypatch, failure):
    config = personal.for_mode(personal_config, "personal")
    workspace = config.root / "workspace"
    workspace.mkdir()
    cancelled = threading.Event()
    if failure == "cancel":
        cancelled.set()
    elif failure == "budget":
        monkeypatch.setattr(personal, "MAX_SNAPSHOT_BYTES", 1)
    else:
        (Path(config.personal_source) / "objects/unsafe").symlink_to("../credentials.json")
    with pytest.raises(ValueError):
        personal.prepare_snapshot(config, workspace, cancelled)
    assert not (workspace / ".flow-data/flow-snapshot.json").exists()


def test_modes_have_independent_state_leases_and_export_context(personal_config, monkeypatch):
    monkeypatch.setattr(controller, "STATE_ROOT", personal_config.root / "runtime")
    sample = Flow(personal_config)
    private = Flow(personal.for_mode(personal_config, "personal"))
    try:
        assert controller.runtime(sample.config.root) != controller.runtime(
            sample.config.root, "personal"
        )
        capture = private.add_selection({"text": "Personal record", "selector": "#item"})
        private.save_issues(
            [{"id": "one", "prompt": "Why is this wrong?", "selection_ids": [capture["id"]]}]
        )
        assert not sample.state["selections"] and not sample.state["issues"]
        output = private.evidence_export("full")
        manifest = json.loads((output / "manifest.json").read_text())
        handoff = (output / "handoff.md").read_text()
        assert manifest["flow_mode"] == "personal"
        assert "offline" in handoff and "shared function" in handoff
        with pytest.raises(ValueError, match="host owns"):
            private.begin("Do not edit through personal-data Flow")
        with pytest.raises(ValueError, match="Confirm"):
            private.inspect({})
    finally:
        sample.close()
        private.close()


def test_verified_previous_version_remains_stoppable(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    state = {
        "api": controller.API,
        "project": str(root),
        "version": "0.5.0",
        "pid": 123,
        "fingerprint": "same-process",
        "instance": "test",
        "port": 12345,
    }
    capabilities = {
        "project": str(root),
        "hosted": True,
        "api": "project-flow.control/v1",
        "controller": {k: state[k] for k in ("api", "instance", "version")},
        "capabilities": ["export-jobs"],
    }
    monkeypatch.setattr(controller.ModelManager, "_fingerprint", lambda _: "same-process")
    monkeypatch.setattr(controller, "request", lambda *a, **k: capabilities)
    assert controller.verified(root, state)
    assert not controller.verified(root, state, "personal")
    capabilities["controller"]["instance"] = "other"
    assert not controller.verified(root, state)


@pytest.mark.parametrize(
    "arguments", [["flow", "--personal", "status"], ["flow", "status", "--personal"]]
)
def test_personal_flag_works_before_and_after_subcommand(tmp_path, monkeypatch, arguments):
    calls = []
    monkeypatch.setattr(
        controller, "status", lambda root, **kw: calls.append(kw) or {"state": "stopped"}
    )
    result = CliRunner().invoke(app, [*arguments, "--project", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert calls == [{"mode": "personal"}]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS personal snapshot sandbox")
def test_real_sandbox_denies_live_access_egress_and_child_processes(tmp_path):
    workspace = tmp_path.resolve() / "workspace"
    workspace.mkdir()
    # These fixtures stand in for live storage, not the user's actual data.
    sentinel = tmp_path.resolve() / "live-data"
    sentinel.write_text("untouched")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        target_port = listener.getsockname()[1]
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            preview_port = reservation.getsockname()[1]
        script = f"""
import json, pathlib, socket, subprocess, sys
root = pathlib.Path.cwd()
(root / 'allowed').write_text('ok')
blocked = []
def denied(name, operation):
    try: operation()
    except (PermissionError, OSError): blocked.append(name)
    else: raise AssertionError(name + ' was allowed')
live = pathlib.Path({str(sentinel)!r})
denied('read', live.read_text)
denied('write', lambda: live.write_text('damage'))
denied('network', lambda: socket.create_connection(('127.0.0.1', {target_port}), timeout=1))
denied('external', lambda: socket.create_connection(('1.1.1.1', 443), timeout=1))
denied('fork', lambda: subprocess.run([sys.executable, '-c', 'pass'], check=True))
with socket.socket() as server:
    server.bind(('127.0.0.1', {preview_port}))
    server.listen()
print(json.dumps(blocked))
"""
        argv = [sys.executable, "-I", "-c", script, str(preview_port)]
        result = subprocess.run(
            personal.sandbox_command(argv, workspace, project=Path(__file__).resolve().parents[2]),
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=15,
        )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["read", "write", "network", "external", "fork"]
    assert sentinel.read_text() == "untouched"
    assert (workspace / "allowed").read_text() == "ok"


def test_discard_and_restart_cleanup_never_touch_live_data_or_evidence(personal_config):
    config = personal.for_mode(personal_config, "personal")
    run = config.data / "inspections" / ("a" * 32)
    workspace = run / "workspace"
    workspace.mkdir(parents=True)
    (run / "baseline.json").write_text("{}")
    (workspace / "source.py").write_text("keep source for diagnostics")
    personal.prepare_snapshot(config, workspace, threading.Event())
    flow = Flow(config)  # Cleans data from abandoned, no-longer-owned previews.
    try:
        assert not (workspace / ".flow-data").exists()
        assert not (workspace / ".flow-home").exists()
        assert (workspace / "source.py").exists()
        assert (Path(config.personal_source) / "objects/example").read_text() == "original asset"
        personal.prepare_snapshot(config, workspace, threading.Event())
        capture = flow.add_selection({"text": "Keep evidence"})
        flow.preview_workspace = workspace
        flow.stop_preview()
        assert not (workspace / ".flow-data").exists()
        assert Path(capture["artifacts"]["capture"]).is_file()
        with pytest.raises(ValueError):
            personal.discard_snapshot(config, Path(config.personal_source))
        (workspace / ".flow-data").symlink_to(config.personal_source)
        with pytest.raises(ValueError, match="symlink"):
            personal.discard_snapshot(config, workspace)
    finally:
        flow.close()


@pytest.mark.parametrize("mode", ["sample", "personal"])
def test_browser_offline_boundary_and_personal_text_masking(tmp_path, mode):
    pytest.importorskip("playwright.sync_api")
    requests = []
    # Also works when running tests against an installed wheel.
    from localdev_mlx.flow import evidence

    inspector = Path(evidence.__file__).with_name("inspector.js")
    script = (
        inspector.read_text()
        .replace("__FLOW_MODE__", mode)
        .replace("__SAFE_QUERY_KEYS__", "[]")
        .replace("__CAPTURE_TOKEN__", "fixture")
    )

    class App(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            requests.append(self.path)
            body = (
                script
                if self.path == "/inspector.js"
                else """<!doctype html><html><body>
<h1>Fixture</h1><p id="personal" data-flow-private>PERSONAL_CONTENT</p>
<input value="FORM_SECRET"><p data-flow-secret>CREDENTIAL_CONTENT</p>
<script src="/inspector.js"></script></body></html>"""
            )
            self.send_response(200)
            self.send_header(
                "Content-Type", "text/javascript" if self.path == "/inspector.js" else "text/html"
            )
            self.end_headers()
            self.wfile.write(body.encode())

    servers = [ThreadingHTTPServer(("127.0.0.1", 0), App) for _ in range(2)]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    browser = BrowserRecorder(headless=True)
    try:
        browser.call(
            "open", url=f"http://127.0.0.1:{servers[0].server_port}/", offline=mode == "personal"
        )

        def exercise(page):
            if mode == "personal":
                # The target is another local service, not the active copied app.
                blocked = page.evaluate(
                    """async url => {
                  try { await fetch(url, {mode:'no-cors'}); return false; }
                  catch (_) { return true; }
                }""",
                    f"http://127.0.0.1:{servers[1].server_port}/must-not-reach",
                )
                assert blocked
                assert "/must-not-reach" not in requests
            snapshot = page.evaluate(
                "window.__projectFlow.snapshot(document.getElementById('personal'))"
            )
            text = json.dumps(snapshot)
            assert "FORM_SECRET" not in text and "CREDENTIAL_CONTENT" not in text
            assert ("PERSONAL_CONTENT" in text) == (mode == "personal")
            return {
                key: page.locator(key).bounding_box()
                for key in ["#personal", "input", "[data-flow-secret]"]
            }

        boxes = browser.call("observe", callback=exercise)
        captured = browser.call(
            "capture",
            directory=str(tmp_path / "capture"),
            selector="#personal",
            include_personal=mode == "personal",
        )
        assert "viewport" in captured["artifacts"]
        from PIL import Image

        with Image.open(captured["artifacts"]["viewport"]) as picture:
            for key, box in boxes.items():
                masked = picture.getpixel((int(box["x"] + 2), int(box["y"] + 2)))[:3] == (
                    255,
                    0,
                    255,
                )
                assert masked == (key != "#personal" or mode == "sample")
    finally:
        browser.stop()
        for server in servers:
            server.shutdown()
            server.server_close()
