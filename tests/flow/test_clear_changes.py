from __future__ import annotations

import copy
import json
import threading
from pathlib import Path

import httpx
import pytest

from localdev_mlx.flow.config import Config
from localdev_mlx.flow.engine import Flow
from localdev_mlx.flow.web import create_servers

from .test_clear_captures import seed


@pytest.fixture(params=["sample", "personal"])
def flow(tmp_path, request):
    active = Flow(Config(tmp_path, mode=request.param, preview_kind="command"))
    seed(active)
    yield active
    active.close()


def test_clear_changes_preserves_captures_exports_and_recovery(flow):
    exported = flow.evidence_export("full")
    handoff = (exported / "handoff.md").read_bytes()
    before = copy.deepcopy(flow.state)
    flow.clear_issues(["one"], before["issues_revision"], confirmed=True)
    assert not flow.state["issues"]
    assert flow.state["selections"] == before["selections"]
    assert flow.state["issues_revision"] == before["issues_revision"] + 1
    archive = Path(flow.state["last_issue_clear"]["archive"])
    assert json.loads(archive.read_text())["issues"] == before["issues"]
    assert archive.stat().st_mode & 0o077 == 0
    assert (exported / "handoff.md").read_bytes() == handoff
    flow.close()
    reopened = Flow(flow.config)
    try:
        assert not reopened.state["issues"]
        assert reopened.state["selections"] == before["selections"]
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "wrong",
    [
        {"confirmed": False},
        {"confirmed": "true"},
        {"expected_revision": True},
        {"expected_revision": None},
        {"expected_revision": 0},
        {"issue_ids": []},
        {"issue_ids": ["one", "one"]},
        {"issue_ids": None},
        {"issue_ids": [1]},
    ],
)
def test_clear_requires_exact_ids_revision_and_confirmation(flow, wrong):
    before = copy.deepcopy(flow.state)
    arguments = {
        "issue_ids": ["one"],
        "expected_revision": before["issues_revision"],
        "confirmed": True,
    }
    arguments.update(wrong)
    with pytest.raises(ValueError):
        flow.clear_issues(**arguments)
    assert flow.state == before
    assert not (flow.config.data / "cleared-changes").exists()


def test_export_and_save_failures_cannot_lose_active_changes(flow, monkeypatch):
    before = copy.deepcopy(flow.state)
    with monkeypatch.context() as scoped:
        scoped.setattr(flow.exports, "view", lambda: {"stage": "organizing"})
        with pytest.raises(ValueError, match="export"):
            flow.clear_issues(["one"], before["issues_revision"], confirmed=True)
    assert flow.state == before

    def fail(*args):
        raise OSError("Synthetic disk error")

    for target, attribute in [("localdev_mlx.flow.inspection", "atomic_write"), (flow, "_save")]:
        with monkeypatch.context() as scoped:
            if isinstance(target, str):
                scoped.setattr(target + "." + attribute, fail)
            else:
                scoped.setattr(target, attribute, fail)
            with pytest.raises(OSError):
                flow.clear_issues(["one"], before["issues_revision"], confirmed=True)
            assert flow.state == before


@pytest.fixture
def server(flow):
    dashboard, proxy = create_servers(flow, 0)
    for http in (dashboard, proxy):
        threading.Thread(target=http.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{dashboard.server_port}"
    for http in (dashboard, proxy):
        http.shutdown()
        http.server_close()


def test_clear_endpoint_rejects_cross_origin_and_stale_revisions(flow, server):
    with httpx.Client(base_url=server) as client:
        state = client.get("/api/state").json()
        headers = {"X-Flow-Token": state["control_token"]}
        body = {"ids": ["one"], "revision": state["issues_revision"], "confirmed": True}
        assert client.post("/api/clear-issues", json=body).status_code == 403
        assert (
            client.post(
                "/api/clear-issues", headers={**headers, "Origin": "https://example.org"}, json=body
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/clear-issues", headers=headers, json={**body, "confirmed": False}
            ).status_code
            == 400
        )
        flow.save_issues([{"id": "other", "prompt": "Newer edit from another tab"}])
        assert client.post("/api/clear-issues", headers=headers, json=body).status_code == 400
        assert flow.state["issues"][0]["id"] == "other"


def test_clear_browser_flushes_unsaved_changes_and_survives_reload(flow, server):
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(server)
        button = page.get_by_role("button", name="Clear all changes", exact=True)
        expect(button).to_be_enabled()
        assert page.locator("#clear-captures + #clear-changes").count() == 1
        page.once("dialog", lambda dialog: dialog.dismiss())
        button.click()
        expect(button).to_be_enabled()
        page.wait_for_function("!document.getElementById('issues').inert")
        expect(page.locator(".issue textarea")).to_have_value("Keep my description")
        page.locator(".issue textarea").fill("Unsaved latest request")
        page.once("dialog", lambda dialog: dialog.accept())
        button.click()
        expect(page.locator(".issue textarea")).to_have_value("")
        expect(button).to_be_disabled()
        expect(page.locator("#capture-count")).to_have_text("2 app parts")
        archive = Path(flow.state["last_issue_clear"]["archive"])
        assert json.loads(archive.read_text())["issues"][0]["prompt"] == "Unsaved latest request"
        page.reload()
        expect(page.locator(".issue textarea")).to_have_value("")
        expect(page.locator("#capture-count")).to_have_text("2 app parts")
        page.locator(".issue textarea").fill("Next batch of work")
        expect(page.locator("#save-state")).to_have_text("Saved locally")
        assert flow.state["issues"][0]["prompt"] == "Next batch of work"
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert errors == []
        browser.close()
