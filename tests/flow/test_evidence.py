from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

from localdev_mlx.flow.config import Config
from localdev_mlx.flow.engine import Flow
from localdev_mlx.flow.evidence import BrowserRecorder, clean, replay_draft, sanitized
from localdev_mlx.flow.web import create_servers


def config(tmp_path, **kwargs):
    root = tmp_path / "project"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src/App.swift").write_text('Text("A native library")\n')
    (root / "src/view.tsx").write_text('<button data-testid="save">Save</button>\n')
    return Config(root, **kwargs)


def test_native_source_anchor_grouped_exports_and_restart_preserve_evidence(tmp_path):
    cfg = config(tmp_path)
    flow = Flow(cfg)
    selected = flow.add_selection(
        {
            "text": "Native screen",
            "anchor": {"path": "src/App.swift", "line": 1},
            "value": "NEVER_EXPORT",
        }
    )
    flow.save_issues(
        [
            {
                "prompt": "Fix the native layout",
                "expected": "Text fits",
                "selection_ids": [selected["id"]],
            }
        ]
    )
    flow.attach(
        selected["id"],
        "diagnostic.log",
        base64.b64encode(b"token=NEVER_EXPORT\nknown failure").decode(),
    )
    standard = flow.evidence_export()
    light, full = flow.evidence_export("light"), flow.evidence_export("full")
    assert "Fix the native layout" in (standard / "handoff.md").read_text()
    assert "src/App.swift" in (standard / "handoff.md").read_text()
    assert "Captured diagnostics" not in (standard / "handoff.md").read_text()
    assert "Captured diagnostics" in (full / "handoff.md").read_text()
    assert not (light / "evidence").exists()
    with zipfile.ZipFile(standard.with_suffix(".zip")) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        for path in manifest["selections"][selected["id"]]["artifacts"].values():
            assert (
                not Path(path).is_absolute() and "NEVER_EXPORT" not in archive.read(path).decode()
            )
    flow.close()
    reopened = Flow(cfg)
    try:
        assert reopened.view()["issues"][0]["prompt"] == "Fix the native layout"
        assert len(reopened.view()["selections"]) == 1
        reopened.remove_selection(selected["id"])
        assert reopened.view()["issues"][0]["selection_ids"] == []
        assert Path(selected["artifacts"]["capture"]).exists()
    finally:
        reopened.close()


def test_secret_redaction_and_unknown_issue_anchor_rejected(tmp_path):
    assert "MY_SECRET" not in clean("Authorization: Bearer MY_SECRET password=MY_SECRET")
    assert "PRIVATE" not in json.dumps(
        sanitized(
            {
                "value": "PRIVATE",
                "headers": {"hello": "PRIVATE"},
                "token": "PRIVATE",
                "url": "http://localhost/path?token=PRIVATE",
            }
        )
    )
    flow = Flow(config(tmp_path))
    try:
        with pytest.raises(ValueError):
            flow.save_issues([{"prompt": "bad", "selection_ids": ["missing"]}])
        with pytest.raises(ValueError):
            flow.add_selection({"anchor": {"path": "../secret.txt", "line": 1}})
        assert not flow.view()["selections"]
    finally:
        flow.close()


def test_surface_labels_are_bounded_and_survive_exports_and_restart(tmp_path):
    cfg = config(tmp_path)
    flow = Flow(cfg)
    try:
        selection = flow.add_selection(
            {
                "selector": "#search",
                "text": "Search providers",
                "path": "/details?token=NEVER_EXPORT",
                "capture": {
                    "surface": {
                        "page": "Library",
                        "dialog": "Arrival (2016)",
                        "section": " Metadata\n ",
                        "element": "Search providers",
                        "unknown": "NEVER_EXPORT",
                    }
                },
            }
        )
        assert selection["label"] == "Library · Arrival (2016) · Metadata · Search providers"
        assert selection["selector"] == "#search" and selection["path"] == "/details"
        flow.save_issues([{"prompt": "Fix search", "selection_ids": [selection["id"]]}])
        export = flow.evidence_export("light")
        assert "App area: " + selection["label"] in (export / "handoff.md").read_text()
        manifest = json.loads((export / "manifest.json").read_text())
        assert manifest["selections"][selection["id"]]["surface"] == selection["surface"]
        assert "NEVER_EXPORT" not in (export / "manifest.json").read_text()
        bounded = flow.add_selection({"capture": {"surface": {"page": "x" * 1000}}})
        assert len(bounded["label"]) == 80
        malformed = flow.add_selection({"capture": {"surface": ["not a mapping"]}})
        assert "label" not in malformed
    finally:
        flow.close()
    reopened = Flow(cfg)
    try:
        assert reopened.state["selections"][0]["label"] == selection["label"]
    finally:
        reopened.close()


def test_reviewable_replay_is_valid_python_and_requires_loopback_manual_inputs():
    draft = replay_draft(
        {"actions": [{"type": "change", "needs_input": True, "selector": "#input"}]}
    )
    compile(draft, "replay.py", "exec")
    assert "manual input" in draft
    assert 'context.route("**/*", restrict)' in draft
    assert "127.0.0.1" in draft


def test_missing_browser_preserves_manual_evidence_workflow(tmp_path):
    flow = Flow(config(tmp_path))

    def unavailable(*args, **kwargs):
        raise ValueError("Install the optional browser extra")

    flow.browser.call = unavailable
    try:
        flow.proxy_url, flow.preview_port = "http://127.0.0.1:12345/", 12344
        flow.open_preview()
        assert flow.state["browser_status"] == "unavailable"
        assert "browser extra" in flow.state["evidence_warning"]
        selected = flow.add_selection({"text": "Native or manual evidence"})
        assert selected["artifacts"]["capture"]
    finally:
        flow.close()


def test_command_adapter_runs_without_git_browser_or_model_and_detects_source_change(tmp_path):
    cfg = config(
        tmp_path,
        preview_kind="command",
        preview=(("{python}"), "-c", "print('Synthetic CLI preview')"),
        scenarios=("empty", "large"),
    )
    flow = Flow(cfg)
    try:
        flow.inspect({"scenario": "large", "seed": 123})
        flow.inspection_thread.join(timeout=15)
        assert flow.view()["inspector_status"] == "ready", flow.view()
        assert not flow.browser.thread
        assert not flow.view()["preview_stale"]
        assert flow.state["inspection"]["seed"] == 123
        (cfg.root / "src/App.swift").write_text('Text("New code")\n')
        assert flow.view()["preview_stale"]
        capture = flow.add_selection({"anchor": {"path": "src/App.swift", "line": 1}})
        assert capture["source_hints"][0]["excerpt"] == 'Text("A native library")'
        flow.inspect({"scenario": "empty"})
        flow.inspection_thread.join(timeout=15)
        assert not flow.view()["preview_stale"]
        assert flow.view()["selections"][0]["id"] == capture["id"]
        assert not (cfg.root / ".git").exists()
    finally:
        flow.close()


def test_upload_rejects_active_content_and_symlink_exports(tmp_path):
    flow = Flow(config(tmp_path))
    try:
        selection = flow.add_selection({"text": "Evidence"})
        with pytest.raises(ValueError):
            flow.attach(selection["id"], "exploit.svg", base64.b64encode(b"<svg/>").decode())
        secret = tmp_path / "secret.log"
        secret.write_text("MUST_NOT_EXPORT")
        linked = flow.config.data / "evidence" / selection["id"] / "link.log"
        linked.symlink_to(secret)
        selection["artifacts"]["bad"] = str(linked)
        directory = flow.evidence_export()
        with zipfile.ZipFile(directory.with_suffix(".zip")) as archive:
            assert not any("link.log" in name for name in archive.namelist())
            assert "MUST_NOT_EXPORT" not in archive.read("handoff.md").decode()
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["omitted_artifacts"][0]["artifact"] == "bad"
            assert "Evidence availability" in archive.read("handoff.md").decode()
    finally:
        flow.close()


DEMO = """from http.server import BaseHTTPRequestHandler
from socketserver import TCPServer
from pathlib import Path
import json, sys
class App(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_GET(self):
        if self.path == "/app.js":
            body = Path("src/app.js").read_bytes(); media="text/javascript"
        else:
            body = b'<html><head><title>Synthetic</title></head><body><h1>Example</h1><input id="password" value="VISIBLE_SECRET"><button id="save">Save</button><p id="result">Not saved</p><script src="/app.js"></script></body></html>'; media="text/html"
        self.send_response(200); self.send_header("Content-Type",media);self.send_header("Set-Cookie","session=synthetic; HttpOnly; Path=/");self.end_headers();self.wfile.write(body)
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length",0)))
        assert "session=synthetic" in self.headers.get("Cookie","")
        self.send_response(200);self.send_header("Content-Type","application/json");self.end_headers();self.wfile.write(b'{"saved":true}')
TCPServer(("127.0.0.1",int(sys.argv[1])),App).serve_forever()
"""


def test_real_recording_browser_capture_mutations_export_and_dashboard(tmp_path):
    pytest.importorskip("playwright.sync_api")
    cfg = config(tmp_path, preview=("{python}", "src/demo.py", "{port}"), preview_mutations=True)
    (cfg.root / "src/demo.py").write_text(DEMO)
    (cfg.root / "src/app.js").write_text(
        'document.querySelector("#save").onclick=async()=>{await fetch("/save",{method:"POST",body:"{}"});document.querySelector("#result").textContent="Saved";const b=document.createElement("button");b.id="dynamic";b.textContent="New UI";document.body.append(b);console.error("token=HIDDEN_SECRET");};'
    )
    flow = Flow(cfg)
    flow.browser = BrowserRecorder(headless=True)
    server, proxy = create_servers(flow, 0)
    for http in (server, proxy):
        threading.Thread(target=http.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        flow.inspect({})
        flow.inspection_thread.join(timeout=20)
        assert flow.view()["browser_status"] == "recording", flow.view()
        flow.browser.call("observe", callback=lambda page: page.locator("#save").click())
        flow.browser.call("observe", callback=lambda page: page.locator("#dynamic").wait_for())
        flow.browser.call(
            "observe", callback=lambda page: page.locator("#dynamic").click(modifiers=["Alt"])
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not flow.view()["selections"]:
            time.sleep(0.1)
        selected = flow.view()["selections"][0]
        assert selected["selector"] == "#dynamic"
        assert {"capture", "viewport", "element"} <= selected["artifacts"].keys(), selected
        assert "narrow" not in selected["artifacts"] and "annotated" not in selected["artifacts"]
        capture = Path(selected["artifacts"]["capture"]).read_text()
        assert "HIDDEN_SECRET" not in capture and "VISIBLE_SECRET" not in capture
        assert selected["summary"]["errors"] >= 1
        assert selected["summary"]["failed_requests"] == 0
        parsed = json.loads(capture)
        assert parsed["parents"] and parsed["state"]["target"]["tag"] == "BUTTON"
        assert any(row.get("trace_id") for row in parsed["network"])
        assert parsed["timeline"] and parsed["proxy_events"]
        assert any(row.get("timing") for row in parsed["network"])
        # Resume keeps navigation, form values and diagnostics; it does not reseed.
        flow.browser.call(
            "observe", callback=lambda page: page.locator("#password").fill("TRANSIENT_VALUE")
        )
        flow.open_test_app({})
        assert (
            flow.browser.call(
                "observe", callback=lambda page: page.locator("#password").input_value()
            )
            == "TRANSIENT_VALUE"
        )
        flow.save_issues(
            [{"prompt": "Make dynamic button wider", "selection_ids": [selected["id"]]}]
        )
        flow.browser.call(
            "observe", callback=lambda page: page.locator("#dynamic").click(modifiers=["Alt"])
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and len(flow.view()["selections"]) < 2:
            time.sleep(0.1)
        assert flow.view()["selections"][1]["comparison"]["available"]
        assert flow.view()["selections"][1]["summary"]["errors"] >= 1
        assert (
            flow.view()["selections"][1]["artifacts"]["viewport"]
            == selected["artifacts"]["viewport"]
        )
        directory = flow.evidence_export()
        assert (directory / "handoff.md").is_file()
        # A second browser is only for the dashboard; recording remains on its owned page.
        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 1000})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base)
            expect(page.locator(".part-choice")).to_have_count(2)
            expect(page.locator(".issue textarea")).to_have_value("Make dynamic button wider")
            expect(page.get_by_text("Assign checked captures", exact=True)).to_have_count(0)
            expect(page.locator("#preview-options")).not_to_have_attribute("open", "")
            expect(page.locator("#project")).to_have_text(str(cfg.root))
            page.locator("#preview-options > summary").click()
            expect(page.locator("#seed-field")).to_be_hidden()
            expect(page.locator("#scenario-field")).to_be_hidden()
            expect(page.locator("#viewport-field")).to_be_visible()
            page.locator("#preview-options > summary").click()
            page.locator("#add-issue").click()
            expect(page.locator(".issue")).to_have_count(2)
            page.locator(".issue textarea").last.fill("Second grouped issue")
            page.locator(".issue").last.locator("input[type=checkbox]").first.check()
            expect(page.locator("#save-state")).to_have_text("Saved locally")
            page.reload()
            expect(page.locator(".issue textarea").last).to_have_value("Second grouped issue")
            expect(
                page.locator(".issue").last.locator("input[type=checkbox]").first
            ).to_be_checked()
            page.locator(".issue textarea").last.fill("Updated second issue")
            page.locator("#level").focus()
            expect(page.locator("#save-state")).to_have_text("Saved locally")
            with page.expect_download() as download:
                page.locator("#export-bundle").click()
            with zipfile.ZipFile(download.value.path()) as archive:
                assert "Updated second issue" in archive.read("handoff.md").decode()
                exported = json.loads(archive.read("manifest.json"))
                assert exported["selections"][selected["id"]]["artifact_sha256"]["viewport"]
                assert exported["selections"][selected["id"]]["artifacts"]["annotated"]
            for width in (1280, 390):
                page.set_viewport_size({"width": width, "height": 1000})
                page.evaluate("window.scrollTo(0,0)")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=str(tmp_path / f"project-flow-{width}.png"))
            assert not errors
            browser.close()
        # An extra narrow screenshot is opt-in and restores the original viewport.
        before = flow.browser.call("observe", callback=lambda page: page.viewport_size)
        with flow.lock:
            flow.state["capture_narrow"] = True
        extra = flow.add_selection({"selector": "#dynamic", "path": "/", "text": "New UI"})
        assert "narrow" in extra["artifacts"]
        assert flow.browser.call("observe", callback=lambda page: page.viewport_size) == before
        state = json.loads(urllib.request.urlopen(base + "/api/state").read())
        request = urllib.request.Request(
            base + "/api/issues",
            data=b'{"issues":[]}',
            headers={"Content-Type": "application/json", "Origin": flow.proxy_url.rstrip("/")},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 403
        assert state["issues"]
        replay = flow.browser.call(
            "replay",
            actions=[{"type": "change", "selector": "#password"}],
            url=flow.proxy_url,
            directory=str(Path(selected["artifacts"]["capture"]).parent),
        )
        assert replay["state"] == "needs_input" and replay["steps"] == 0
        # Read-only is a real gate even though this adapter normally permits synthetic saves.
        from dataclasses import replace

        flow.config = replace(cfg, preview_mutations=False)
        request = urllib.request.Request(
            flow.proxy_url + "save",
            data=b"{}",
            method="POST",
            headers={"Origin": flow.proxy_url.rstrip("/")},
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 403
    finally:
        flow.close()
        for http in (server, proxy):
            http.shutdown()
            http.server_close()
