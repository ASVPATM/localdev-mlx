from __future__ import annotations

import copy
import json
import threading

import httpx
import pytest

from localdev_mlx.flow.config import Config
from localdev_mlx.flow.engine import Flow
from localdev_mlx.flow.web import create_servers


@pytest.fixture
def flow(tmp_path):
    active = Flow(Config(tmp_path, preview_kind="command"))
    yield active
    active.close()


def seed(flow):
    first = flow.add_selection({"selector": "#one", "text": "First app part"})
    second = flow.add_selection({"selector": "#two", "text": "Second app part"})
    flow.save_issues(
        [
            {
                "id": "one",
                "prompt": "Keep my description",
                "expected": "Keep expected behavior",
                "selection_ids": [first["id"], second["id"]],
            }
        ]
    )
    return [first["id"], second["id"]]


def test_clear_archives_references_preserves_exports_and_survives_restart(flow):
    ids = seed(flow)
    exported = flow.evidence_export("full")
    original_files = {
        path: path.read_bytes() for path in flow.config.data.rglob("*") if path.is_file()
    }
    before = copy.deepcopy(flow.state)
    flow.clear_selections(ids, before["issues_revision"], confirmed=True)
    assert flow.state["selections"] == []
    assert flow.state["issues"][0] == {**before["issues"][0], "selection_ids": []}
    assert flow.state["issues_revision"] == before["issues_revision"] + 1
    from pathlib import Path

    archive = Path(flow.state["last_capture_clear"]["archive"])
    recovery = json.loads(archive.read_text())
    assert recovery["selections"] == before["selections"]
    assert recovery["issues"] == before["issues"]
    assert archive.stat().st_mode & 0o077 == 0
    for path, data in original_files.items():
        if path.name not in {
            "state.json",
            "memory.sqlite3",
            "memory.sqlite3-wal",
            "memory.sqlite3-shm",
        }:
            assert path.read_bytes() == data
    assert (exported / "handoff.md").read_bytes() == original_files[exported / "handoff.md"]
    flow.close()
    reopened = Flow(flow.config)
    try:
        assert reopened.state["selections"] == []
        assert reopened.state["issues"][0]["prompt"] == "Keep my description"
        next_part = reopened.add_selection({"selector": "#new", "text": "New batch"})
        assert len(reopened.state["selections"]) == 1
        assert next_part["id"] not in ids
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "wrong",
    [
        {"confirmed": False},
        {"confirmed": "yes"},
        {"expected_revision": None},
        {"expected_revision": True},
        {"expected_revision": -1},
        {"selection_ids": None},
        {"selection_ids": ["missing"]},
        {"selection_ids": [1]},
        {"selection_ids": ["same", "same"]},
    ],
)
def test_clear_requires_confirmation_and_exact_current_snapshot(flow, wrong):
    ids = seed(flow)
    before = copy.deepcopy(flow.state)
    arguments = {
        "selection_ids": ids,
        "expected_revision": before["issues_revision"],
        "confirmed": True,
    }
    arguments.update(wrong)
    with pytest.raises(ValueError):
        flow.clear_selections(**arguments)
    assert flow.state == before
    assert not (flow.config.data / "cleared-captures").exists()


def test_capture_arriving_after_confirmation_is_not_lost(flow):
    ids = seed(flow)
    revision = flow.state["issues_revision"]
    incoming = flow.add_selection({"text": "Arrived in another tab"})
    with pytest.raises(ValueError, match="App parts changed"):
        flow.clear_selections(ids, revision, confirmed=True)
    assert len(flow.state["selections"]) == 3
    assert flow.selection(incoming["id"])


def test_changes_saved_in_another_tab_are_not_detached(flow):
    ids = seed(flow)
    revision = flow.state["issues_revision"]
    flow.save_issues(
        [{"id": "other", "prompt": "Newer description", "selection_ids": ids}], revision
    )
    with pytest.raises(ValueError, match="another tab"):
        flow.clear_selections(ids, revision, confirmed=True)
    assert flow.state["issues"][0]["selection_ids"] == ids


def test_clear_waits_for_export_and_write_errors_keep_active_evidence(flow, monkeypatch):
    ids = seed(flow)
    before = copy.deepcopy(flow.state)
    with monkeypatch.context() as scoped:
        scoped.setattr(flow.exports, "view", lambda: {"stage": "organizing"})
        with pytest.raises(ValueError, match="export"):
            flow.clear_selections(ids, before["issues_revision"], confirmed=True)
    assert flow.state == before

    def failure(*args):
        raise OSError("Synthetic disk failure")

    with monkeypatch.context() as scoped:
        scoped.setattr("localdev_mlx.flow.inspection.atomic_write", failure)
        with pytest.raises(OSError):
            flow.clear_selections(ids, before["issues_revision"], confirmed=True)
    assert flow.state == before
    with monkeypatch.context() as scoped:
        scoped.setattr(flow, "_save", failure)
        with pytest.raises(OSError):
            flow.clear_selections(ids, before["issues_revision"], confirmed=True)
    assert flow.state == before
    assert len(list((flow.config.data / "cleared-captures").glob("*.json"))) == 1


@pytest.fixture
def server(flow):
    dashboard, proxy = create_servers(flow, 0)
    for http in (dashboard, proxy):
        threading.Thread(target=http.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{dashboard.server_port}"
    for http in (dashboard, proxy):
        http.shutdown()
        http.server_close()


def test_bulk_clear_endpoint_is_authenticated_and_explicit(flow, server):
    ids = seed(flow)
    with httpx.Client(base_url=server) as client:
        state = client.get("/api/state").json()
        assert state["can_clear_selections"]
        body = {"ids": ids, "revision": state["issues_revision"], "confirmed": True}
        assert client.post("/api/clear-selections", json=body).status_code == 403
        headers = {"X-Flow-Token": state["control_token"]}
        assert (
            client.post(
                "/api/clear-selections",
                headers={**headers, "Origin": "https://foreign.invalid"},
                json=body,
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/clear-selections", headers=headers, json={**body, "confirmed": False}
            ).status_code
            == 400
        )
        response = client.post("/api/clear-selections", headers=headers, json=body)
        assert response.status_code == 200
        assert response.json()["selections"] == []
        assert response.json()["issues"][0]["prompt"] == "Keep my description"
        revision = response.json()["issues_revision"]
        assert (
            client.post(
                "/api/clear-selections",
                headers=headers,
                json={"ids": [], "revision": revision, "confirmed": True},
            ).json()["issues_revision"]
            == revision
        )


def test_bulk_clear_browser_cancel_save_reload_and_new_batch(flow, server, tmp_path):
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import expect, sync_playwright

    ids = seed(flow)
    exported = flow.evidence_export("full")
    original = (exported / "handoff.md").read_bytes()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(server)
        button = page.get_by_role("button", name="Clear all app parts", exact=True)
        expect(button).to_be_enabled()
        expect(page.locator("#capture-count")).to_have_text("2 app parts")
        page.once("dialog", lambda dialog: dialog.dismiss())
        button.click()
        expect(button).to_be_enabled()
        page.wait_for_function("!document.getElementById('issues').inert")
        expect(page.locator(".part-choice")).to_have_count(2)
        assert flow.state["issues"][0]["selection_ids"] == ids
        # Clear must flush a just-typed description, even before its autosave timer.
        page.locator(".issue textarea").fill("My latest unsaved description")
        page.once("dialog", lambda dialog: dialog.accept())
        button.click()
        expect(page.locator("#capture-count")).to_have_text("0 app parts")
        expect(button).to_be_disabled()
        expect(page.locator(".part-choice")).to_have_count(0)
        expect(page.locator(".issue textarea")).to_have_value("My latest unsaved description")
        expect(page.locator("#capture-state")).to_contain_text("recovery copy")
        assert flow.state["issues"][0]["selection_ids"] == []
        assert flow.state["issues"][0]["prompt"] == "My latest unsaved description"
        page.reload()
        expect(page.locator(".part-choice")).to_have_count(0)
        expect(button).to_be_disabled()
        expect(page.locator(".issue textarea")).to_have_value("My latest unsaved description")
        assert (exported / "handoff.md").read_bytes() == original
        incoming = flow.add_selection({"text": "Next batch capture"})
        expect(page.locator("#capture-count")).to_have_text("1 app part")
        expect(button).to_be_enabled()
        page.locator(".part-choice input").check()
        expect(page.locator("#save-state")).to_have_text("Saved locally")
        assert flow.state["issues"][0]["selection_ids"] == [incoming["id"]]
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(tmp_path / "flow-clear-parts.png"))
        assert errors == []
        browser.close()
