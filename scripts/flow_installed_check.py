"""Exercise the actual installed CLI in a disposable project; no model required."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", required=True, type=Path, help="New, empty validation directory"
    )
    parser.add_argument("--browser", action="store_true")
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    project = root / "project"
    project.mkdir()
    os.environ["LOCALDEV_MLX_HOME"] = str(root / "runtime")
    os.environ.pop("PYTHONPATH", None)
    from localdev_mlx import __version__
    from localdev_mlx.flow import controller
    from localdev_mlx.flow.session_evidence import Bundle, validate_bundle

    assert "site-packages" in controller.__file__, "This check requires an installed wheel"
    cli = str(Path(sys.executable).parent / "localdev-mlx")

    def command(*arguments):
        result = subprocess.run(
            [cli, "--repo", str(project), *arguments],
            capture_output=True,
            text=True,
            check=True,
            timeout=35,
        )
        return result.stdout

    if args.browser:
        command("flow", "init", "--demo")
    else:
        assert importlib.util.find_spec("playwright") is None
        assert importlib.util.find_spec("PIL") is None
        command("flow", "init")
    report = {
        "version": __version__,
        "python": sys.version.split()[0],
        "module": controller.__file__,
        "browser": args.browser,
        "project": str(project),
    }
    try:
        info = json.loads(command("flow", "start", "--no-open"))
        with httpx.Client(base_url=info["url"], trust_env=False, timeout=30) as client:
            token = client.get("/api/state").json()["control_token"]

            def post(path, body=None):
                response = client.post(
                    "/api/" + path, json=body or {}, headers={"X-Flow-Token": token}
                )
                response.raise_for_status()
                return response

            def state():
                return client.get("/api/state").json()

            for resource in ("/", "/app.js", "/style.css"):
                assert client.get(resource).status_code == 200
            if args.browser:
                post("open-test-app", {"scenario": "small"})
                end = time.monotonic() + 25
                while time.monotonic() < end and state()["browser_status"] != "recording":
                    time.sleep(0.1)
                assert state()["browser_status"] == "recording", state().get("evidence_warning")
                post("selection", {"selector": "#items", "path": "/", "text": "Dynamic task cards"})
                # Exercise the shipped, explicitly-confirmed synthetic replay endpoint.
                post(
                    "selection",
                    {
                        "text": "Reviewed synthetic error-button action",
                        "capture": {"actions": [{"type": "click", "selector": "#sample-error"}]},
                    },
                )
                replay_id = state()["selections"][-1]["id"]
                replay = post("replay", {"id": replay_id, "confirmed": True}).json()
                assert replay["artifact"] and replay["steps"] == 1
                post(
                    "selection",
                    {"selector": "#message", "path": "/", "text": "Sample error feedback"},
                )
                # Replay has its own context; it must not inject errors into the recording tab.
                assert state()["selections"][-1]["summary"]["has_screenshot"]
                from playwright.sync_api import expect, sync_playwright

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    page = browser.new_page(viewport={"width": 1280, "height": 1000})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(info["url"])
                    page.locator(".issue textarea").first.fill(
                        "Make task cards easier to scan on narrow screens."
                    )
                    page.locator(".issue").first.locator("input[type=checkbox]").first.check()
                    page.locator("#add-issue").click()
                    page.locator(".issue textarea").last.fill(
                        "Keep error feedback clear and accessible."
                    )
                    page.locator(".issue").last.locator("input[type=checkbox]").last.check()
                    expect(page.locator("#save-state")).to_have_text("Saved locally")
                    page.reload()
                    expect(page.locator(".issue textarea").last).to_have_value(
                        "Keep error feedback clear and accessible."
                    )
                    page.locator("#organize").uncheck()
                    with page.expect_download() as download:
                        page.locator("#export-bundle").click()
                    download.value.save_as(root / "evidence.zip")
                    for width in (1280, 390):
                        page.set_viewport_size({"width": width, "height": 1000})
                        page.evaluate("window.scrollTo(0,0)")
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                        page.screenshot(path=str(root / f"dashboard-{width}.png"))
                        page.locator(".export").scroll_into_view_if_needed()
                        page.screenshot(path=str(root / f"dashboard-export-{width}.png"))
                    assert not errors, errors
                    browser.close()
            else:
                post("selection", {"text": "Manual native/CLI evidence"})
                sid = state()["selections"][0]["id"]
                post(
                    "issues",
                    {
                        "revision": 0,
                        "issues": [
                            {
                                "id": "manual",
                                "prompt": "Clarify the CLI error message.",
                                "selection_ids": [sid],
                            }
                        ],
                    },
                )
                post("export-start", {"organize": False, "format": "zip"})
            end = time.monotonic() + 10
            while time.monotonic() < end and not state()["export_job"].get("session_link"):
                time.sleep(0.05)
            job = state()["export_job"]
            assert job["stage"] == "ready" and job["session_link"]["state"] == "not_linked"
            response = post("export-download", {"id": job["id"], "format": "zip"})
            (root / "evidence.zip").write_bytes(response.content)
            bundle = Bundle(root / "evidence.zip")
            try:
                manifest, digest, hashes = validate_bundle(bundle, project)
                assert len(manifest["issues"]) == (2 if args.browser else 1)
                markdown = bundle.read("handoff.md").decode()
                for link in re.findall(r"\]\(([^)]+)\)", markdown):
                    assert link in hashes
                    assert bundle.read(link)
            finally:
                bundle.close()
            with zipfile.ZipFile(root / "evidence.zip") as archive:
                assert archive.testzip() is None
            report.update(
                artifact_count=len(hashes),
                bundle_digest=digest,
                issues=len(manifest["issues"]),
                export="passed",
            )
        assert json.loads(command("flow", "stop"))["state"] == "stopped"
        reopened = json.loads(command("flow", "start", "--no-open"))
        with httpx.Client(base_url=reopened["url"], trust_env=False) as client:
            saved = client.get("/api/state").json()
            assert len(saved["issues"]) == report["issues"]
            assert saved["export_job"]["id"] == job["id"]
        assert not (project / ".git").exists()
        report["restart"] = "passed"
    finally:
        command("flow", "stop")
    (root / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
