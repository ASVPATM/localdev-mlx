from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from localdev_mlx.flow.config import Config
from localdev_mlx.flow.engine import Flow
from localdev_mlx.flow.host import HostServices
from localdev_mlx.flow.web import create_servers


def test_detected_preview_consent_is_one_time_and_missing_setup_is_clear(tmp_path, monkeypatch):
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import expect, sync_playwright

    from localdev_mlx.flow.detection import configuration

    (tmp_path / "index.html").write_text("<h1>Example</h1>")
    flow = Flow(
        configuration(tmp_path), host=HostServices("test", lambda: {"state": "not_configured"})
    )
    monkeypatch.setattr(flow, "start_preview", lambda: None)
    monkeypatch.setattr(flow, "open_preview", lambda: None)
    server, proxy = create_servers(flow, 0)
    for http in (server, proxy):
        threading.Thread(target=http.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            prompts, errors = [], []

            def confirm(dialog):
                prompts.append(dialog.message)
                dialog.accept()

            page.on("dialog", confirm)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}")
            expect(page.locator("#preview-setup")).to_contain_text("Detected Static HTML preview")
            page.locator('[data-action="open-test-app"]').click()
            expect(page.locator("#preview-status")).to_contain_text("ready")
            page.locator('[data-action="open-test-app"]').click()
            assert len(prompts) == 1 and not errors
            flow.config = Config(tmp_path, preview_kind="command")
            page.reload()
            expect(page.locator("#preview-setup")).to_contain_text(
                "No test-app launch configuration"
            )
            expect(page.locator('[data-action="open-test-app"]')).to_be_disabled()
            expect(page.locator("#stale")).to_be_empty()
            browser.close()
    finally:
        for http in (server, proxy):
            http.shutdown()
            http.server_close()
        flow.close()


def test_two_tabs_keep_their_drafts_and_hosted_export_can_skip_the_helper(tmp_path):
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import expect, sync_playwright

    calls = []

    def organize(payload, cancel, timeout):
        calls.append(payload)
        cancel.wait(timeout)
        raise ValueError("Synthetic helper was cancelled")

    host = HostServices(
        "LocalDev fixture",
        lambda: {
            "provider": "MLX fixture",
            "model": "synthetic-helper",
            "state": "ready",
            "can_organize": True,
            "identity": "fixture-v1",
        },
        organize,
    )
    flow = Flow(Config(tmp_path, preview_kind="command", supports_seed=True), host=host)
    flow.save_issues([{"id": "one", "prompt": "Original request"}])
    server, proxy = create_servers(flow, 0)
    for http in (server, proxy):
        threading.Thread(target=http.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        # Hold the second tab's polls at its initial revision to force a real conflict.
        original_state = urllib.request.urlopen(base + "/api/state").read().decode()
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            first, second = context.new_page(), context.new_page()
            first.goto(base)
            second.route(
                "**/api/state",
                lambda route: route.fulfill(body=original_state, content_type="application/json"),
            )
            second.goto(base)
            expect(second.locator(".issue textarea")).to_have_value("Original request")
            expect(first.locator("#editing-tools")).to_be_hidden()
            first.locator("#preview-options > summary").click()
            expect(first.locator("#seed-field")).to_be_visible()
            expect(first.locator("#viewport-field")).to_be_hidden()
            first.locator(".issue textarea").fill("Saved in the first tab")
            expect(first.locator("#save-state")).to_have_text("Saved locally")
            second.locator(".issue textarea").fill("Unsent draft from the second tab")
            expect(second.locator("#draft-conflict")).to_be_visible()
            second.unroute("**/api/state")
            second.reload()
            expect(second.locator(".issue textarea")).to_have_value(
                "Unsent draft from the second tab"
            )
            expect(second.locator("#draft-conflict")).to_be_visible()
            with second.expect_download() as download:
                second.locator("#backup-draft").click()
            saved = json.loads(download.value.path().read_text())
            assert saved["issues"][0]["prompt"] == "Unsent draft from the second tab"
            second.once("dialog", lambda dialog: dialog.accept())
            second.locator("#use-server-draft").click()
            expect(second.locator(".issue textarea")).to_have_value("Saved in the first tab")
            expect(second.locator("#draft-conflict")).to_be_hidden()
            assert calls == []  # Typing, selecting and status checks require no inference.
            with first.expect_download() as handoff:
                first.locator("#export-handoff").click()
                expect(first.locator("#skip-organization")).to_be_visible()
                expect(first.locator("#model-status")).to_contain_text("synthetic-helper")
                first.locator("#skip-organization").click()
            output = handoff.value.path().read_text()
            assert "Saved in the first tab" in output and "skipped or timed out" in output
            assert len(calls) == 1
            context.close()
            browser.close()
    finally:
        flow.close()
        for http in (server, proxy):
            http.shutdown()
            http.server_close()


def test_capture_cards_show_surface_labels_and_keep_legacy_evidence_unchanged(tmp_path):
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import expect, sync_playwright

    flow = Flow(Config(tmp_path, preview_kind="command"))
    surface = {
        "page": "Library",
        "dialog": "Arrival (2016)",
        "section": "Metadata",
        "element": "Search providers",
    }
    capture = flow.add_selection(
        {
            "selector": "#lookup",
            "text": "Search",
            "path": "/details",
            "capture": {"surface": surface},
        }
    )
    # Simulate a controller already running when the new browser assets are installed.
    capture.pop("surface")
    capture.pop("label")
    legacy = flow.add_selection(
        {"selector": "div:nth-of-type(3)", "text": "Poster artwork", "path": "/library"}
    )
    originals = {row["id"]: json.loads(json.dumps(row)) for row in flow.state["selections"]}
    server, proxy = create_servers(flow, 0)
    for http in (server, proxy):
        threading.Thread(target=http.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            requests, errors = [], []
            page.on("request", lambda request: requests.append(request.url))
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}")
            title = "Library · Arrival (2016) · Metadata · Search providers"
            expect(page.locator(".part-copy").first).to_contain_text(title)
            expect(page.locator(".part-copy").first).to_contain_text("/details · #lookup")
            expect(page.locator(".part-copy").nth(1)).to_contain_text("2. Poster artwork")
            expect(page.locator(".part-copy").nth(1)).to_contain_text(legacy["selector"])
            page.get_by_role("button", name="Evidence for " + title, exact=True).click()
            expect(page.locator("#evidence-title")).to_have_text(title)
            expect(page.locator("#evidence-location")).to_have_text("/details · #lookup")
            page.locator("#close-evidence").click()
            page.evaluate("refresh()")
            # Once for the compatibility label, once for the explicitly opened evidence.
            assert sum("name=capture" in url and capture["id"] in url for url in requests) == 2
            assert not errors
            browser.close()
        assert {row["id"]: row for row in flow.state["selections"]} == originals
    finally:
        flow.close()
        for http in (server, proxy):
            http.shutdown()
            http.server_close()
