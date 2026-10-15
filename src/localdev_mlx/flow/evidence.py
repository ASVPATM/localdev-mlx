"""Optional browser recorder and bounded, private evidence shared by all adapters.

No models or browser dependencies are imported on the normal text-only path.
Artifacts contain sanitized events, not HAR files, credentials or response bodies.
"""

from __future__ import annotations

import hashlib
import io
import json
import queue
import re
import threading
import time
from collections import deque
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

SECRET = re.compile(r"(?i)(password|passwd|secret|token|api[_ -]?key|authorization|cookie)")


def clean(value, limit=1000):
    text = str(value or "")[:limit]
    text = re.sub(r"(?i)bearer\s+[^\s\"']+", "Bearer [redacted]", text)
    return re.sub(
        r"(?i)((?:password|passwd|secret|token|api[_ -]?key|authorization|cookie)[\"']?\s*[:=]\s*[\"']?)[^\s,;\"'&]+",
        r"\1[redacted]",
        text,
    )


def safe_url(value, keys=()):
    try:
        parsed = urlsplit(str(value or ""))
        if parsed.scheme not in {"", "http", "https"} or parsed.username or parsed.password:
            return ""
        query = urlencode(
            [
                (key, clean(value, 120))
                for key, value in parse_qsl(parsed.query)
                if key in keys and not SECRET.search(key)
            ]
        )
        return urlunsplit((parsed.scheme, parsed.netloc, clean(parsed.path, 500), query, ""))
    except ValueError:
        return ""


def sanitized(value, key="", depth=0):
    if depth > 8 or SECRET.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {
            clean(k, 80): sanitized(v, str(k), depth + 1)
            for k, v in list(value.items())[:40]
            if k not in {"html", "headers", "cookies", "body", "storage", "value"}
        }
    if isinstance(value, list):
        return [sanitized(v, key, depth + 1) for v in value[-100:]]
    if isinstance(value, str):
        return safe_url(value) if key in {"url", "path", "start_url"} else clean(value)
    return value if isinstance(value, (int, float, bool)) or value is None else clean(value)


def digest_manifest(manifest):
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def surface_details(value):
    """Optional, bounded display labels; selectors remain the technical anchors."""
    if not isinstance(value, dict):
        return {}
    return {
        key: " ".join(clean(value[key], 80).split())
        for key in ("page", "dialog", "section", "element")
        if isinstance(value.get(key), str) and value[key].strip()
    }


def surface_label(value):
    return " · ".join(dict.fromkeys(surface_details(value).values()))


def stamp():
    return {
        "id": uuid4().hex[:16],
        "ts_ms": round(time.time() * 1000),
        "at": datetime.now(UTC).isoformat(),
    }


def compact_capture(capture):
    """Keep observations bounded and label temporal proximity as a hint, not causation."""
    capture = sanitized(capture)
    actions = capture.get("actions", [])
    for rows in (capture.get("network", []), capture.get("console", [])):
        for row in rows:
            before = [
                action
                for action in actions
                if isinstance(action.get("ts_ms"), (int, float))
                and 0 <= row.get("ts_ms", 0) - action["ts_ms"] <= 3000
            ]
            if before:
                row["action_hint"] = before[-1].get("id")
                row["correlation"] = "temporal-only"
    grouped = {}
    for row in capture.get("console", []):
        signature = (
            row.get("level"),
            row.get("text"),
            json.dumps(row.get("location", {}), sort_keys=True),
        )
        if signature not in grouped:
            grouped[signature] = {**row, "count": 0, "first_at": row.get("at")}
        item = grouped[signature]
        item["count"] += 1
        item["last_at"] = row.get("at")
    capture["console"] = list(grouped.values())
    timeline = []
    for kind, rows in (
        ("action", actions),
        ("request", capture.get("network", [])),
        ("console", capture.get("console", [])),
    ):
        for row in rows:
            timeline.append(
                {
                    "kind": kind,
                    **{
                        key: row[key]
                        for key in ("id", "ts_ms", "action_hint", "trace_id", "status", "count")
                        if key in row
                    },
                }
            )
    capture["timeline"] = sorted(timeline, key=lambda row: row.get("ts_ms", 0))[-100:]
    capture["correlation_note"] = (
        "Trace IDs identify the same synthetic proxy request. Time-nearby actions are hints only; backend application causality requires adapter instrumentation."
    )
    return capture


def replay_draft(capture):
    """Reviewable draft; the operator must supply a fresh synthetic loopback URL."""
    actions = json.dumps(json.dumps(capture.get("actions", [])[:100]))
    return (
        '''"""Draft reproduction, not an assertion that the issue is fixed.
Review side effects and supply ONLY a disposable synthetic preview URL.
Form values are deliberately omitted; input steps stop for manual completion.
"""
import argparse
import json
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument("--url", required=True)
args = parser.parse_args()
url = urlsplit(args.url)
if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"} or url.username or url.password:
    parser.error("Use a credential-free synthetic loopback URL")
ACTIONS = json.loads('''
        + actions
        + """)
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context(service_workers="block")
    def restrict(route):
        target = urlsplit(route.request.url)
        if (target.scheme, target.netloc) == (url.scheme, url.netloc):
            route.continue_()
        else:
            route.abort("blockedbyclient")
    context.route("**/*", restrict)
    page = context.new_page()
    page.goto(args.url)
    for step, action in enumerate(ACTIONS, 1):
        if action.get("type") != "click":
            raise RuntimeError(f"Step {step} needs manual input; no form values were recorded")
        target = page.locator(action["selector"])
        if target.count() != 1:
            raise RuntimeError(f"Step {step} is missing or ambiguous")
        target.click(timeout=3000)
    page.pause()
"""
    )


def compare_images(before: Path, after: Path, output: Path):
    try:
        from PIL import Image, ImageChops
    except ImportError:
        return {
            "available": False,
            "reason": "Install the browser extra for image comparison",
        }
    with (
        Image.open(before).convert("RGB") as old,
        Image.open(after).convert("RGB") as new,
    ):
        if old.size != new.size:
            return {
                "available": False,
                "reason": "Viewport/image sizes differ; no misleading pixel score",
            }
        diff = ImageChops.difference(old, new)
        pixels = max(1, old.width * old.height)
        changed = sum(diff.convert("L").histogram()[10:])
        diff.point(lambda value: min(255, value * 4)).save(output)
        return {
            "available": True,
            "changed_percent": round(100 * changed / pixels, 3),
            "artifact": str(output),
        }


def annotated_image(path, capture):
    """Derive an annotated export on demand; don't store redundant captures by default."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    bounds = capture.get("annotation", {}).get("bounds") or {}
    if not all(isinstance(bounds.get(key), (int, float)) for key in ("x", "y", "width", "height")):
        return None
    with Image.open(path).convert("RGB") as picture:
        ratio = picture.width / max(1, capture.get("environment", {}).get("width") or picture.width)
        box = [
            bounds["x"] * ratio,
            bounds["y"] * ratio,
            (bounds["x"] + bounds["width"]) * ratio,
            (bounds["y"] + bounds["height"]) * ratio,
        ]
        ImageDraw.Draw(picture).rectangle(box, outline="#24cd09", width=3)
        output = io.BytesIO()
        picture.save(output, format="PNG")
        return output.getvalue()


class BrowserRecorder:
    def __init__(self, *, headless=False, started=None):
        self.headless = headless
        self.started = started
        self.commands = queue.Queue()
        self.thread = None

    def call(self, command, **data):
        if not self.thread or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._worker, daemon=True)
            self.thread.start()
        reply = queue.Queue(maxsize=1)
        self.commands.put((command, data, reply))
        try:
            result = reply.get(timeout=40)
        except queue.Empty:
            raise ValueError("Evidence browser timed out; reopen it and retry") from None
        if isinstance(result, Exception):
            raise ValueError(str(result)) from result
        return result

    def stop(self):
        if self.thread and self.thread.is_alive():
            with suppress(ValueError):
                self.call("stop")
            self.thread.join(timeout=5)

    def _worker(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            while True:
                _, _, reply = self.commands.get()
                reply.put(
                    ValueError(
                        "Automatic screenshots need localdev-mlx[flow-browser]. Install that extra, then run: localdev-mlx flow browser-setup"
                    )
                )
                return
        browser = context = page = None
        console, network = deque(maxlen=80), deque(maxlen=100)
        active_requests = {}
        with sync_playwright() as playwright:

            def request_started(request):
                row = {
                    **stamp(),
                    "method": request.method,
                    "url": safe_url(request.url),
                    "resource_type": request.resource_type,
                    "pending": True,
                }
                active_requests[request] = row
                if len(active_requests) > 100:
                    active_requests.pop(next(iter(active_requests)))
                network.append(row)

            def request_response(response):
                row = active_requests.get(response.request)
                if row is None:
                    return
                row["status"] = response.status
                trace = response.headers.get("x-project-flow-trace-id", "")
                if re.fullmatch(r"[a-f0-9]{32}", trace):
                    row["trace_id"] = trace

            def request_finished(request):
                row = active_requests.pop(request, None)
                if row is not None:
                    row["pending"] = False
                    row["timing"] = {
                        key: value
                        for key, value in request.timing.items()
                        if isinstance(value, (int, float))
                    }
                    if request.failure:
                        row["failure"] = clean(request.failure)

            while True:
                try:
                    command, data, reply = self.commands.get(timeout=0.05)
                except queue.Empty:
                    if page and not page.is_closed():
                        with suppress(Exception):
                            page.wait_for_timeout(10)
                    continue
                try:
                    if command == "stop":
                        if browser:
                            browser.close()
                        reply.put(True)
                        return
                    if command == "open":
                        if (
                            data.get("resume")
                            and browser
                            and browser.is_connected()
                            and page
                            and not page.is_closed()
                            and urlsplit(page.url).netloc == urlsplit(data["url"]).netloc
                        ):
                            page.bring_to_front()
                            reply.put(True)
                            continue  # No navigation, log reset, viewport reset or lost test state.
                        if not browser or not browser.is_connected():
                            browser = playwright.chromium.launch(headless=self.headless)
                            if self.started:
                                self.started()
                            context = browser.new_context(
                                accept_downloads=True,
                                service_workers="block" if data.get("offline") else "allow",
                            )
                            if data.get("offline"):
                                allowed = urlsplit(data["url"])

                                def offline_only(route, _request, expected=allowed):
                                    target = urlsplit(route.request.url)
                                    if (target.scheme, target.netloc) == (
                                        expected.scheme,
                                        expected.netloc,
                                    ):
                                        route.continue_()
                                    else:
                                        route.abort("blockedbyclient")

                                context.route("**/*", offline_only)
                                context.route_web_socket("**/*", lambda ws: ws.close())
                            context.on(
                                "console",
                                lambda item: console.append(
                                    {
                                        **stamp(),
                                        "level": item.type,
                                        "text": clean(item.text),
                                        "location": {
                                            "url": safe_url(item.location.get("url")),
                                            "line": item.location.get("lineNumber"),
                                            "column": item.location.get("columnNumber"),
                                        },
                                    }
                                ),
                            )
                            context.on(
                                "page",
                                lambda p: p.on(
                                    "pageerror",
                                    lambda exc: console.append(
                                        {**stamp(), "level": "error", "text": clean(exc)}
                                    ),
                                ),
                            )
                            context.on("request", request_started)
                            context.on("response", request_response)
                            context.on("requestfinished", request_finished)
                            context.on("requestfailed", request_finished)
                        if not page or page.is_closed():
                            page = context.new_page()
                        console.clear()
                        network.clear()
                        active_requests.clear()
                        page.set_viewport_size({"width": data.get("width", 1440), "height": 900})
                        page.goto(data["url"], wait_until="domcontentloaded", timeout=15000)
                        page.bring_to_front()
                        reply.put(True)
                        continue
                    if not page or page.is_closed():
                        raise ValueError("Open the recording test app first")
                    if command == "observe" and callable(data.get("callback")):
                        # In-process integration/testing hook; never exposed over HTTP.
                        reply.put(data["callback"](page))
                    elif command == "locate":
                        locator = page.locator(data["selector"])
                        if locator.count() != 1:
                            raise ValueError(
                                "Selector is missing or ambiguous; select the element again"
                            )
                        locator.scroll_into_view_if_needed(timeout=2000)
                        locator.evaluate(
                            "el => el.animate([{outline:'3px solid #24cd09'}, {outline:'3px solid transparent'}], {duration:1800})"
                        )
                        page.bring_to_front()
                        reply.put(True)
                    elif command == "capture":
                        directory, selector = (
                            Path(data["directory"]),
                            data.get("selector") or "body",
                        )
                        directory.mkdir(parents=True, exist_ok=True)
                        capture = page.evaluate(
                            "selector => window.__projectFlow?.snapshot(document.querySelector(selector)) || {}",
                            selector,
                        )
                        if data.get("page_id") and capture.get("page_id") != data["page_id"]:
                            raise ValueError(
                                "Selection came from a different browser tab; open the recording browser for matching screenshots"
                            )
                        capture.update(
                            console=list(console),
                            network=list(network),
                            captured_at=datetime.now(UTC).isoformat(),
                        )
                        artifacts = {}
                        original = page.viewport_size
                        style = page.add_style_tag(
                            content="[data-project-flow-overlay] { visibility:hidden!important }"
                        )
                        masks = [
                            page.locator(
                                "input,textarea,select,[contenteditable],[data-flow-secret]"
                                + ("" if data.get("include_personal") else ",[data-flow-private]")
                            )
                        ]
                        try:
                            viewport = directory / "viewport.png"
                            page.screenshot(path=str(viewport), mask=masks, animations="disabled")
                            artifacts["viewport"] = str(viewport)
                            locator = page.locator(selector)
                            if locator.count() == 1 and locator.is_visible():
                                element = directory / "element.png"
                                bounds = locator.bounding_box()
                                try:
                                    from PIL import Image

                                    with Image.open(viewport).convert("RGB") as picture:
                                        if bounds:
                                            box = (
                                                max(0, int(bounds["x"])),
                                                max(0, int(bounds["y"])),
                                                min(
                                                    picture.width,
                                                    int(bounds["x"] + bounds["width"]),
                                                ),
                                                min(
                                                    picture.height,
                                                    int(bounds["y"] + bounds["height"]),
                                                ),
                                            )
                                            if box[2] > box[0] and box[3] > box[1]:
                                                picture.crop(box).save(element)
                                                artifacts["element"] = str(element)
                                            capture["annotation"] = {
                                                "bounds": bounds,
                                                "source": "viewport",
                                                "coordinate_space": "CSS pixels",
                                            }
                                except ImportError:
                                    pass
                            if data.get("narrow"):
                                page.set_viewport_size({"width": 720, "height": 900})
                                page.wait_for_timeout(100)
                                narrow = directory / "narrow.png"
                                page.screenshot(path=str(narrow), mask=masks, animations="disabled")
                                artifacts["narrow"] = str(narrow)
                                capture["narrow_audit"] = page.evaluate(
                                    "selector => window.__projectFlow?.snapshot(document.querySelector(selector)).audit || {}",
                                    selector,
                                )
                                capture["narrow_note"] = (
                                    "Opt-in resize of live preview; application state may have changed. Original viewport restored."
                                )
                        finally:
                            if page.viewport_size != original:
                                page.set_viewport_size(original)
                            style.evaluate("el => el.remove()")
                        reply.put({"capture": sanitized(capture), "artifacts": artifacts})
                    elif command == "replay":
                        # Never replay against arbitrary user-provided origins. No raw inputs
                        # are recorded; missing values are reported, not fabricated as passes.
                        origin = urlsplit(page.url)
                        requested = urlsplit(data["url"])
                        if (origin.scheme, origin.netloc) != (
                            requested.scheme,
                            requested.netloc,
                        ):
                            raise ValueError("Replay is restricted to the active synthetic preview")
                        # Separate replay context also contains popups and service workers.
                        # Session state stays in memory and is never exported.
                        replay_context = browser.new_context(
                            storage_state=context.storage_state(), service_workers="block"
                        )

                        def restrict(route, _request, expected=requested):
                            target = urlsplit(route.request.url)
                            if (target.scheme, target.netloc) == (
                                expected.scheme,
                                expected.netloc,
                            ):
                                route.continue_()
                            else:
                                route.abort("blockedbyclient")

                        replay_context.route("**/*", restrict)
                        replay_context.route_web_socket("**/*", lambda ws: ws.close())
                        replay = replay_context.new_page()
                        completed, skipped = 0, []
                        try:
                            replay.goto(data["url"], wait_until="domcontentloaded")
                            for index, action in enumerate(data.get("actions", [])[-100:]):
                                if action.get("type") != "click":
                                    skipped.append(index + 1)
                                    break
                                locator = replay.locator(action["selector"])
                                if locator.count() != 1:
                                    raise ValueError(
                                        f"Replay step {index + 1}: selector missing or ambiguous"
                                    )
                                locator.click(timeout=2500)
                                completed += 1
                            path = Path(data["directory"]) / "replay.png"
                            replay.screenshot(
                                path=str(path),
                                mask=[
                                    replay.locator(
                                        "input,textarea,select,[contenteditable],[data-flow-secret]"
                                        + (
                                            ""
                                            if data.get("include_personal")
                                            else ",[data-flow-private]"
                                        )
                                    )
                                ],
                            )
                            reply.put(
                                {
                                    "state": "needs_input" if skipped else "replayed",
                                    "steps": completed,
                                    "blocked_steps": skipped,
                                    "artifact": str(path),
                                    "note": "Replay is reproduction evidence, not proof the issue is fixed.",
                                }
                            )
                        finally:
                            replay_context.close()
                    else:
                        raise ValueError("Unknown browser operation")
                except Exception as exc:
                    reply.put(exc)
