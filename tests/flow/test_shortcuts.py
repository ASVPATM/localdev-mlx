from __future__ import annotations

import json
import threading
import time

import pytest

from localdev_mlx.flow.engine import Flow
from localdev_mlx.flow.evidence import BrowserRecorder
from localdev_mlx.flow.host import HostServices
from localdev_mlx.flow.web import create_servers

from .test_evidence import DEMO, config


@pytest.fixture
def preview(tmp_path):
    pytest.importorskip("playwright.sync_api")
    cfg = config(tmp_path, preview=("{python}", "src/demo.py", "{port}"))
    (cfg.root / "src/demo.py").write_text(
        DEMO.replace("<head><title>", '<head><meta charset="utf-8"><title>')
    )
    (cfg.root / "src/app.js").write_text("""
document.body.insertAdjacentHTML('beforeend', `
  <a id="subpage" href="/details">Subpage</a>
  <button id="open-dialog">Open detail</button>
  <dialog id="detail"><input id="typing" value="keep this">
    <button id="detail-action">Change application state</button></dialog>`);
window.appClicks = 0;
window.appPointerDowns = 0;
document.querySelector('#open-dialog').onclick = () => document.querySelector('#detail').showModal();
document.querySelector('#detail-action').onclick = () => window.appClicks++;
document.querySelector('#detail-action').onpointerdown = () => window.appPointerDowns++;
""")
    flow = Flow(cfg, host=HostServices("test", lambda: {"state": "not_configured"}))
    flow.browser = BrowserRecorder(headless=True)
    dashboard, proxy = create_servers(flow, 0)
    for server in (dashboard, proxy):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        flow.inspect({})
        flow.inspection_thread.join(20)
        assert flow.state["browser_status"] == "recording", flow.state.get("evidence_warning")
        yield flow
    finally:
        for server in (dashboard, proxy):
            server.shutdown()
            server.server_close()
        flow.close()


def test_hotkey_selects_on_subpages_and_in_modal_without_toolbar(preview):
    def select(page):
        page.locator("#subpage").click()
        page.wait_for_url("**/details")
        page.locator("#open-dialog").click()
        page.locator("#typing").focus()
        # Simulate an app's route transition removing the floating inspector menu.
        page.locator("aside[data-project-flow-overlay]").evaluate("el => el.remove()")
        page.keyboard.press("Alt+Shift+KeyI")
        assert page.evaluate("document.documentElement.hasAttribute('data-project-flow-selecting')")
        assert page.locator("#typing").input_value() == "keep this"
        assert (
            page.locator("#detail-action").evaluate("el => getComputedStyle(el).cursor")
            == "crosshair"
        )
        # Escape cancels selection without dismissing the application's open modal.
        page.keyboard.press("Escape")
        assert page.locator("#detail").evaluate("el => el.open")
        assert not page.evaluate(
            "document.documentElement.hasAttribute('data-project-flow-selecting')"
        )
        page.keyboard.press("Alt+Shift+KeyI")
        page.keyboard.press("Alt+Shift+KeyI")  # Enter mode; don't toggle it back off.
        page.locator("#detail-action").click()
        assert page.evaluate("window.appClicks") == 0
        assert page.evaluate("window.appPointerDowns") == 0
        assert not page.evaluate(
            "document.documentElement.hasAttribute('data-project-flow-selecting')"
        )

    preview.browser.call("observe", callback=select)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not preview.state.get("selections"):
        time.sleep(0.05)
    assert len(preview.state.get("selections", [])) == 1
    capture = preview.state["selections"][0]
    assert capture["selector"] == "#detail-action"
    assert capture["path"] == "/details"
    assert capture["summary"]["has_screenshot"], capture.get("warning")

    # Once selection is finished the next ordinary click belongs to the app again.
    def ordinary_click(page):
        page.locator("#detail-action").click()
        assert page.evaluate("window.appClicks") == 1
        page.keyboard.press("Escape")
        assert not page.locator("#detail").evaluate("el => el.open")

    preview.browser.call("observe", callback=ordinary_click)


def test_hotkey_handles_mac_option_key_and_leaves_other_shortcuts_alone(preview):
    def exercise(page):
        state = page.evaluate("""() => {
          const selecting = () => document.documentElement.hasAttribute('data-project-flow-selecting');
          const press = (options) => {
            const event = new KeyboardEvent('keydown', {bubbles:true, cancelable:true, ...options});
            document.querySelector('#password').dispatchEvent(event);
            return {active:selecting(), prevented:event.defaultPrevented};
          };
          const ignored = [
            {code:'KeyI', key:'i', altKey:true},
            {code:'KeyI', key:'I', shiftKey:true},
            {code:'KeyI', key:'I', altKey:true, shiftKey:true, ctrlKey:true},
            {code:'KeyI', key:'I', altKey:true, shiftKey:true, metaKey:true},
            {code:'KeyI', key:'I', altKey:true, shiftKey:true, repeat:true},
          ].map(press);
          const mac = press({code:'KeyI', key:'Dead', altKey:true, shiftKey:true, isComposing:true});
          const pressed = document.querySelector('[aria-keyshortcuts="Alt+Shift+I"]').getAttribute('aria-pressed');
          press({key:'Escape'});
          const reset = document.querySelector('[aria-keyshortcuts="Alt+Shift+I"]').getAttribute('aria-pressed');
          return {ignored, mac, pressed, reset};
        }""")
        assert all(not item["active"] for item in state["ignored"])
        assert all(not item["prevented"] for item in state["ignored"][:-1])
        assert state["mac"] == {"active": True, "prevented": True}
        assert state["pressed"] == "true" and state["reset"] == "false"
        # A toolbar that is merely folded also doesn't block the keyboard shortcut.
        page.locator("aside[data-project-flow-overlay]").get_by_role(
            "button", name="−", exact=True
        ).click()
        page.keyboard.press("Alt+Shift+KeyI")
        assert page.evaluate("document.documentElement.hasAttribute('data-project-flow-selecting')")
        page.keyboard.press("Escape")

    preview.browser.call("observe", callback=exercise)


def test_toolbar_and_selection_feedback_follow_the_active_modal(preview):
    def exercise(page):
        from playwright.sync_api import expect

        page.locator("#open-dialog").click()
        toolbar = page.locator("#detail aside[data-project-flow-overlay]")
        expect(toolbar).to_be_visible()
        # Native showModal() makes a body-level toolbar inert, regardless of z-index.
        toolbar.get_by_role("button", name="Select", exact=True).click(timeout=2000)
        page.locator("#detail-action").hover()
        expect(page.locator("#detail-action")).to_have_attribute("data-project-flow-target", "")
        expect(toolbar.get_by_role("status")).to_have_text("Click an element · Esc cancels")
        page.keyboard.press("Escape")
        expect(page.locator("#detail-action")).not_to_have_attribute("data-project-flow-target")
        assert page.locator("#detail").evaluate("el => el.open")
        # Follow nested modals in opening order, not their order in the DOM.
        page.evaluate("""() => {
          const nested = document.createElement('dialog'); nested.id = 'nested';
          nested.innerHTML = '<button>Nested action</button>';
          document.body.prepend(nested); nested.showModal();
        }""")
        expect(page.locator("#nested aside[data-project-flow-overlay]")).to_be_visible()
        page.keyboard.press("Alt+Shift+KeyI")
        page.keyboard.press("Escape")
        assert page.locator("#nested").evaluate("el => el.open")
        page.keyboard.press("Escape")
        expect(toolbar).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("body > aside[data-project-flow-overlay]")).to_be_visible()

    preview.browser.call("observe", callback=exercise)


def test_capture_surface_uses_visible_headings_and_tabs_not_form_values(preview):
    def exercise(page):
        page.evaluate("""() => {
          document.body.insertAdjacentHTML('afterbegin', '<main><section hidden><h1>HIDDEN_SECRET</h1></section><h1>Library</h1></main>');
          document.querySelector('#detail').innerHTML = `
            <h2>Arrival (2016)</h2>
            <button role="tab" aria-selected="true">Metadata</button>
            <section role="tabpanel">
              <h3>Catalog metadata</h3>
              <button id="lookup" aria-label="Search providers"><span hidden>HIDDEN_SECRET</span><span style="display:none">CSS_SECRET</span>Search</button>
              <input id="lookup-value" value="INPUT_SECRET">
              <div data-flow-private><h3>PRIVATE_SECRET</h3></div>
            </section>`;
          document.querySelector('#detail').showModal();
        }""")
        return page.evaluate("""() => ({
          target:window.__projectFlow.snapshot(document.querySelector('#lookup')).surface,
          panel:window.__projectFlow.snapshot(document.querySelector('[role="tabpanel"]')).surface,
          input:window.__projectFlow.snapshot(document.querySelector('#lookup-value')).surface,
        })""")

    result = preview.browser.call("observe", callback=exercise)
    assert result["target"] == {
        "page": "Library",
        "dialog": "Arrival (2016)",
        "section": "Metadata",
        "element": "Search providers",
    }
    assert result["panel"]["element"] == "Catalog metadata"
    assert "SECRET" not in json.dumps(result)
