"""Explicit real-data validation on copies; never import app code against live storage."""

from __future__ import annotations

import argparse
import json
import shutil
import threading
import time
from pathlib import Path

from localdev_mlx.flow.detection import configuration
from localdev_mlx.flow.engine import Flow
from localdev_mlx.flow.evidence import BrowserRecorder
from localdev_mlx.flow.host import HostServices
from localdev_mlx.flow.personal import for_mode
from localdev_mlx.flow.session_evidence import Bundle, validate_bundle
from localdev_mlx.flow.web import create_servers
from localdev_mlx.flow.workspace import atomic_write, files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--real-data", required=True, action="store_true")
    args = parser.parse_args()
    original = args.project.resolve(strict=True)
    config = for_mode(configuration(original), "personal")
    baseline = files(config, original)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    project = output / "project"
    project.mkdir()
    for name in baseline:
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original / name, target)
    (project / ".venv").symlink_to(original / ".venv", target_is_directory=True)
    config = for_mode(configuration(project), "personal")
    flow = Flow(
        config, host=HostServices("Personal validation", lambda: {"state": "not_configured"})
    )
    flow.browser = BrowserRecorder(headless=True)
    dashboard, proxy = create_servers(flow, 0)
    for server in (dashboard, proxy):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    report = {"models_loaded": False, "live_app_stopped": False, "source_files": len(baseline)}
    try:
        started = time.monotonic()
        flow.inspect({"confirmed": True})
        while flow.inspection_thread.is_alive():
            flow.inspection_thread.join(10)
            print(flow.state.get("evidence_warning") or flow.state["inspector_status"], flush=True)
            if time.monotonic() - started > 750:
                raise AssertionError("Personal preview timed out")
        view = flow.view()
        assert view["inspector_status"] == "ready", view.get("evidence_warning")
        assert view["browser_status"] == "recording", view.get("evidence_warning")
        report["preview_seconds"] = round(time.monotonic() - started, 3)
        report["snapshot"] = flow.state["inspection"]["personal_snapshot"]

        def exercise(page):
            from playwright.sync_api import expect

            page.wait_for_load_state("domcontentloaded")
            expect(page.locator("aside[data-project-flow-overlay]")).to_be_visible()
            assert "Personal copy" in page.locator("aside[data-project-flow-overlay]").inner_text()
            # A deep route verifies reinjection and the shortcut outside the landing page.
            links = page.locator("a[href^='/courses/']")
            if links.count():
                response = page.goto(flow.proxy_url.rstrip("/") + links.first.get_attribute("href"))
                assert response.status == 200
                page.wait_for_load_state("domcontentloaded")
            heading = page.locator("h1:visible, h2:visible").first
            expect(heading).to_be_visible()
            page.keyboard.press("Alt+Shift+KeyI")
            assert page.evaluate(
                "document.documentElement.hasAttribute('data-project-flow-selecting')"
            )
            heading.click()
            page.screenshot(path=str(output / "personal-preview.png"))
            return {"route": page.url.split(":", 2)[-1].split("/", 1)[-1], "shortcut": True}

        report["browser"] = flow.browser.call("observe", callback=exercise)
        end = time.monotonic() + 15
        while time.monotonic() < end and not flow.state.get("selections"):
            time.sleep(0.1)
        assert flow.state.get("selections"), "Selection did not produce evidence"
        capture = flow.state["selections"][-1]
        assert capture["summary"]["has_screenshot"], capture.get("warning")
        flow.save_issues(
            [
                {
                    "id": "validation",
                    "prompt": "Validation only: review shared behavior behind this area.",
                    "type": "question",
                    "scope": "shared",
                    "selection_ids": [capture["id"]],
                }
            ]
        )
        job = flow.exports.start(organize=False, format="zip")
        flow.exports.thread.join(20)
        assert flow.exports.view()["stage"] == "ready", flow.exports.view()
        artifact, _ = flow.exports.artifact(job["id"], "zip")
        bundle = Bundle(artifact)
        try:
            manifest, _, _ = validate_bundle(bundle, project)
            assert manifest["flow_mode"] == "personal"
        finally:
            bundle.close()
        flow.clear_issues(["validation"], flow.state["issues_revision"], confirmed=True)
        assert not flow.state["issues"] and len(flow.state["selections"]) == 1
        assert artifact.is_file()
        assert files(configuration(original), original) == baseline
        report.update(capture=True, export=True, clear_changes=True, source_unchanged=True)
    finally:
        flow.close()
        for server in (dashboard, proxy):
            server.shutdown()
            server.server_close()
    report["preview_stopped"] = flow.preview_process is None
    atomic_write(output / "report.json", json.dumps(report, indent=2).encode())
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
