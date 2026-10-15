"""Exercise the PMT adapter on copied sources, never on the user's real library."""

from __future__ import annotations

import argparse
import json
import shutil
import threading
import time
import urllib.request
from dataclasses import replace
from pathlib import Path

from localdev_mlx.flow.detection import configuration
from localdev_mlx.flow.engine import Flow
from localdev_mlx.flow.evidence import BrowserRecorder
from localdev_mlx.flow.host import HostServices
from localdev_mlx.flow.session_evidence import Bundle, validate_bundle
from localdev_mlx.flow.web import create_servers
from localdev_mlx.flow.workspace import files


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    original = args.project.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    project = output / "project"
    project.mkdir()
    cfg = configuration(original)
    assert cfg.adapter_name == "PMT synthetic preview"
    baseline = files(cfg, original)
    for name in baseline:
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original / name, target)
    # Reuse already installed dependencies only; no pip/uv sync, no dependency downloads.
    cfg = replace(
        configuration(project), preview=(str(original / ".venv/bin/python"), *cfg.preview[1:])
    )
    flow = Flow(
        cfg, host=HostServices("PMT adapter validation", lambda: {"state": "not_configured"})
    )
    flow.browser = BrowserRecorder(headless=True)
    dashboard, proxy = create_servers(flow, 0)
    for server in (dashboard, proxy):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    report = {"project": str(original), "source_files": len(baseline), "models_loaded": False}
    try:
        started = time.monotonic()
        flow.inspect({"confirmed": True, "scenario": "balanced"})
        flow.inspection_thread.join(100)
        view = flow.view()
        assert view["inspector_status"] == "ready", view.get("evidence_warning")
        assert view["browser_status"] == "recording", view.get("evidence_warning")
        report["cold_preview_seconds"] = round(time.monotonic() - started, 3)
        with urllib.request.urlopen(
            f"http://127.0.0.1:{flow.preview_port}/api/entries", timeout=5
        ) as response:
            library = json.load(response)
        assert library["total"] == 60, library["total"]
        ready = json.loads((flow.preview_workspace / cfg.preview_ready_file).read_text())
        assert ready["synthetic"] and ready["keychain"] is False
        assert Path(ready["data_dir"]).is_relative_to(flow.preview_workspace)
        report["synthetic_entries"] = library["total"]
        report["private_runtime"] = ready

        # Interaction is through the same owned recording browser users open from Flow.
        def exercise(page):
            from playwright.sync_api import expect

            page.locator("#library-heading").wait_for(timeout=20000)
            page.locator("#library [data-details]").first.click()
            dialog = page.locator("#entry-dialog")
            expect(dialog).to_be_visible()
            title = page.locator("#entry-dialog-title").inner_text()
            toolbar = dialog.locator("aside[data-project-flow-overlay]")
            expect(toolbar).to_be_visible()
            toolbar.get_by_role("button", name="Select", exact=True).click(timeout=2000)
            page.keyboard.press("Escape")
            assert dialog.evaluate("el => el.open")
            rating = page.locator("#entry-rating")
            value = rating.input_value()
            rating.focus()
            # Mac's dead/composing Option-I must not be discarded in a form field.
            rating.dispatch_event(
                "keydown",
                {
                    "code": "KeyI",
                    "key": "Dead",
                    "altKey": True,
                    "shiftKey": True,
                    "isComposing": True,
                },
            )
            expect(toolbar.get_by_role("status")).to_have_text("Click an element · Esc cancels")
            page.keyboard.press("Escape")
            assert dialog.evaluate("el => el.open") and rating.input_value() == value
            page.locator('[data-entry-tab="metadata"]').click()
            page.locator("#entry-metadata-heading").wait_for()
            page.locator("#entry-metadata-query").focus()
            page.keyboard.press("Alt+Shift+KeyI")
            assert page.evaluate(
                "document.documentElement.hasAttribute('data-project-flow-selecting')"
            )
            page.locator("#entry-metadata-heading").hover()
            expect(page.locator("#entry-metadata-heading")).to_have_attribute(
                "data-project-flow-target", ""
            )
            page.screenshot(path=str(output / "pmt-media-selection.png"))
            page.locator("#entry-metadata-heading").click()
            assert dialog.evaluate("el => el.open") and rating.input_value() == value
            return {
                "title": page.title(),
                "url": page.url,
                "dialog": title,
                "shortcut_in_media_dialog": True,
                "form_values_unchanged": True,
            }

        report["browser"] = flow.browser.call("observe", callback=exercise)
        end = time.monotonic() + 15
        while time.monotonic() < end and not flow.state.get("selections"):
            time.sleep(0.1)
        assert flow.state.get("selections"), "Selection shortcut did not produce evidence"
        selection = flow.state["selections"][-1]
        assert selection["selector"] == "#entry-metadata-heading"
        assert selection["surface"]["dialog"] == report["browser"]["dialog"]
        assert selection["surface"]["section"] == "Metadata"
        assert selection["surface"]["element"] == "Catalog metadata"
        report["capture_label"] = selection["label"]
        assert selection["summary"]["has_screenshot"], selection.get("warning")
        flow.save_issues(
            [
                {
                    "id": "preview-check",
                    "prompt": "Validation only: inspect this title's catalog metadata.",
                    "selection_ids": [selection["id"]],
                }
            ]
        )
        job = flow.exports.start(organize=False, format="zip")
        flow.exports.thread.join(15)
        assert flow.exports.view()["stage"] == "ready", flow.exports.view()
        artifact, _ = flow.exports.artifact(job["id"], "zip")
        bundle = Bundle(artifact)
        try:
            manifest, _, hashes = validate_bundle(bundle, project)
            report["export"] = {"issues": len(manifest["issues"]), "files": len(hashes)}
        finally:
            bundle.close()

        def dashboard_labels(page):
            from playwright.sync_api import expect

            dashboard_page = page.context.new_page()
            try:
                dashboard_page.goto(f"http://127.0.0.1:{dashboard.server_port}")
                expect(dashboard_page.locator(".part-copy")).to_contain_text(selection["label"])
                expect(dashboard_page.locator(".part-copy .part-location")).to_contain_text(
                    "#entry-metadata-heading"
                )
                for width in (1280, 390):
                    dashboard_page.set_viewport_size({"width": width, "height": 950})
                    dashboard_page.screenshot(path=str(output / f"dashboard-{width}.png"))
                    assert dashboard_page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth"
                    )
            finally:
                dashboard_page.close()

        flow.browser.call("observe", callback=dashboard_labels)
        first = flow.preview_process.pid
        first_workspace = flow.preview_workspace
        flow.browser.call(
            "observe", callback=lambda page: page.evaluate("window.__flow_resume_check = 123")
        )
        flow.open_test_app({})
        assert flow.preview_process.pid == first
        assert (
            flow.browser.call(
                "observe", callback=lambda page: page.evaluate("window.__flow_resume_check")
            )
            == 123
        )
        report["reopen_preserves_state"] = True
        flow.inspect({"scenario": "compact", "seed": 7})
        flow.inspection_thread.join(100)
        assert flow.state["browser_status"] == "recording", flow.state.get("evidence_warning")
        assert flow.preview_workspace != first_workspace
        assert (first_workspace / cfg.preview_ready_file).exists()
        with urllib.request.urlopen(
            f"http://127.0.0.1:{flow.preview_port}/api/entries", timeout=5
        ) as response:
            assert json.load(response)["total"] == 24
        assert len(flow.state["issues"]) == 1 and len(flow.state["selections"]) >= 1
        report["refresh_preserves_evidence_and_reseeds"] = True

        # Inspect both dashboard and test app at desktop/mobile widths.
        def screenshots(page):
            for width in (1280, 390):
                page.set_viewport_size({"width": width, "height": 950})
                page.wait_for_function(
                    "document.querySelectorAll('#library .entry-card:not(.skeleton-card)').length > 0",
                    timeout=10000,
                )
                page.wait_for_timeout(1200)
                page.screenshot(path=str(output / f"pmt-{width}.png"))
            return page.evaluate(
                "Array.from(document.querySelectorAll('#library img')).slice(0,3).map(img => ({src: img.src, loaded: img.complete && img.naturalWidth > 0}))"
            )

        report["posters"] = flow.browser.call("observe", callback=screenshots)
        report["original_sources_unchanged"] = files(configuration(original), original) == baseline
        assert report["original_sources_unchanged"]
    finally:
        process = flow.preview_process
        for server in (dashboard, proxy):
            server.shutdown()
            server.server_close()
        flow.close()
        report["owned_preview_stopped"] = process is None or process.poll() is not None
        (output / "result.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
