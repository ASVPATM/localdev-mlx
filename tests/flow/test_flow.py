from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler

import httpx
import pytest

from localdev_mlx.flow.config import Config
from localdev_mlx.flow.engine import Flow
from localdev_mlx.flow.web import LoopbackHTTPServer, create_servers
from localdev_mlx.flow.workspace import apply_edits, changes, files, integrate, snapshot


def config(tmp_path):
    root = tmp_path / "example"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src/main.py").write_text("value = 1\n")
    (root / ".env").write_text("PRIVATE_CREDENTIAL=never-copy")
    return Config(root, checks=(("{python}", "-c", "import main; assert main.value == 2"),))


def test_loopback_servers_start_without_hostname_lookup(tmp_path, monkeypatch):
    def no_dns(*_):
        raise AssertionError("Loopback startup must not depend on reverse DNS")

    monkeypatch.setattr(socket, "getfqdn", no_dns)
    flow = Flow(config(tmp_path))
    servers = []
    threads = []
    try:
        servers.extend(create_servers(flow, 0))
        for server in servers:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            threads.append((server, thread))
            assert server.server_name == "127.0.0.1"
            assert server.server_port > 0
        with httpx.Client(trust_env=False) as client:
            response = client.get(f"http://127.0.0.1:{servers[0].server_port}/")
            assert response.status_code == 200
            assert "Project Flow" in response.text
    finally:
        for server, thread in threads:
            server.shutdown()
            thread.join(2)
        for server in servers:
            server.server_close()
        flow.close()


def test_loopback_server_rejects_public_bindings():
    with pytest.raises(ValueError, match="loopback"):
        LoopbackHTTPServer(("0.0.0.0", 0), BaseHTTPRequestHandler)


def test_proxy_bind_failure_releases_dashboard_socket(tmp_path, monkeypatch):
    from localdev_mlx.flow import web

    opened = []

    def bind(*args):
        if opened:
            raise OSError("Proxy bind failed")
        server = LoopbackHTTPServer(*args)
        opened.append(server)
        return server

    monkeypatch.setattr(web, "LoopbackHTTPServer", bind)
    flow = Flow(config(tmp_path))
    try:
        with pytest.raises(OSError, match="Proxy bind failed"):
            create_servers(flow, 0)
        assert opened[0].socket.fileno() == -1
    finally:
        for server in opened:
            server.server_close()
        flow.close()


def test_snapshot_is_git_independent_excludes_credentials_and_preserves_source(
    tmp_path,
):
    cfg = config(tmp_path)
    run = cfg.data / "runs/one"
    snapshot(cfg, run)
    assert not (run / "workspace/.env").exists()
    (run / "workspace/src/main.py").write_text("value = 2\n")
    assert (cfg.root / "src/main.py").read_text() == "value = 1\n"
    changed, diff, manifest = changes(cfg, run)
    assert changed == ["src/main.py"] and "+value = 2" in diff
    integrate(cfg, run, manifest)
    assert (cfg.root / "src/main.py").read_text() == "value = 2\n"
    assert (run / "originals/src/main.py").read_text() == "value = 1\n"


@pytest.mark.parametrize("side", ["original", "workspace"])
def test_stale_source_or_unchecked_changes_never_integrate(tmp_path, side):
    cfg = config(tmp_path)
    run = cfg.data / "runs/one"
    snapshot(cfg, run)
    (run / "workspace/src/main.py").write_text("value = 2\n")
    _, _, checked = changes(cfg, run)
    path = cfg.root if side == "original" else run / "workspace"
    (path / "src/main.py").write_text("value = 3\n")
    with pytest.raises(ValueError):
        integrate(cfg, run, checked)
    assert (cfg.root / "src/main.py").read_text() == (
        "value = 3\n" if side == "original" else "value = 1\n"
    )


@pytest.mark.parametrize(
    "name",
    [
        "../outside.py",
        "/tmp/outside.py",
        "src/../../outside.py",
        "src/.env",
        "src/secrets.json",
    ],
)
def test_model_cannot_write_outside_safe_scope(tmp_path, name):
    cfg = config(tmp_path)
    run = cfg.data / "runs/one"
    snapshot(cfg, run)
    with pytest.raises(ValueError):
        apply_edits(cfg, run, [{"path": name, "old": "", "new": "malicious"}])


def test_symlinks_refused_and_exact_replacement_required(tmp_path):
    cfg = config(tmp_path)
    run = cfg.data / "runs/one"
    snapshot(cfg, run)
    with pytest.raises(ValueError):
        apply_edits(cfg, run, [{"path": "src/main.py", "old": "not present", "new": "bad"}])
    (run / "workspace/src/link.py").symlink_to(cfg.root / "src/main.py")
    with pytest.raises(ValueError):
        files(cfg, run / "workspace")


def test_review_gate_and_persistent_memory(tmp_path):
    cfg = config(tmp_path)
    flow = Flow(cfg)
    try:
        flow.begin("Change value to 2")
        with pytest.raises(ValueError):
            flow.accept()
        flow.approve_plan()
        assert flow.view()["status"] == "editing"
        (flow.run / "workspace/src/main.py").write_text("value = 2\n")
        flow.check()
        flow.thread.join(timeout=10)
        assert flow.view()["status"] == "review", flow.view()
        assert (cfg.root / "src/main.py").read_text() == "value = 1\n"
        flow.accept()
        assert flow.view()["status"] == "accepted"
        assert "Change value to 2" in flow.export()
        assert flow.memory.stats().documents > 0
    finally:
        flow.close()
    reopened = Flow(cfg)
    try:
        assert reopened.view()["status"] == "accepted"
    finally:
        reopened.close()


def test_failed_checks_and_cancel_leave_original_unchanged(tmp_path):
    cfg = config(tmp_path)
    flow = Flow(cfg)
    try:
        flow.begin("Change value")
        flow.approve_plan()
        flow.check()
        flow.thread.join(timeout=10)
        assert flow.view()["status"] == "checks_failed"
        with pytest.raises(ValueError):
            flow.accept()
        flow.reject()
        assert flow.view()["status"] == "rejected"
        assert (cfg.root / "src/main.py").read_text() == "value = 1\n"
    finally:
        flow.close()


def test_deleted_file_recoverable_and_double_process_guard(tmp_path):
    cfg = config(tmp_path)
    flow = Flow(cfg)
    try:
        with pytest.raises(ValueError):
            Flow(cfg)
        flow.begin("Remove source")
        (flow.run / "workspace/src/main.py").unlink()
        _, _, manifest = changes(cfg, flow.run)
        integrate(cfg, flow.run, manifest)
        assert not (cfg.root / "src/main.py").exists()
        assert (flow.run / "originals/src/main.py").exists()
    finally:
        flow.close()


def test_model_endpoint_is_local_and_commands_receive_no_canvas_token(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    monkeypatch.setenv("CANVAS_ACCESS_TOKEN", "must-not-leak")
    assert "CANVAS_ACCESS_TOKEN" not in cfg.environment(tmp_path)
    with pytest.raises(ValueError):
        Config(cfg.root, model_url="https://api.example.com")
    with pytest.raises(ValueError):
        Config(cfg.root, include=("..",))


def test_selections_do_not_store_form_values_or_html(tmp_path):
    flow = Flow(config(tmp_path))
    try:
        flow.add_selection(
            {
                "selector": "#course",
                "text": "Course",
                "html": "<script>unsafe</script>",
                "value": "FORM_VALUE_MUST_NOT_APPEAR",
            }
        )
        encoded = json.dumps(flow.view()["selections"])
        assert "FORM_VALUE_MUST_NOT_APPEAR" not in encoded and "<script>" not in encoded
    finally:
        flow.close()


def test_optional_model_only_stages_edits_checks_require_separate_action(tmp_path, monkeypatch):
    from dataclasses import replace

    from localdev_mlx.flow import engine

    cfg = replace(config(tmp_path), model="synthetic-model")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, _):
            return json.dumps(
                {
                    "response": json.dumps(
                        {
                            "edits": [
                                {
                                    "path": "src/main.py",
                                    "old": "value = 1",
                                    "new": "value = 2",
                                }
                            ],
                            "summary": "Update value",
                        }
                    )
                }
            ).encode()

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "http://127.0.0.1:11434/api/generate"
            assert b"never-copy" not in request.data
            return Response()

    monkeypatch.setattr(engine.urllib.request, "build_opener", lambda *_: Opener())
    flow = Flow(cfg)
    try:
        flow.begin("Change value to 2")
        flow.approve_plan()
        flow.thread.join(timeout=5)
        assert flow.view()["status"] == "editing"
        assert flow.view()["checks"] == []
        assert (cfg.root / "src/main.py").read_text() == "value = 1\n"
        assert (flow.run / "workspace/src/main.py").read_text() == "value = 2\n"
    finally:
        flow.close()
