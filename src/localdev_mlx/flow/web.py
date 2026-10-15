from __future__ import annotations

import http.client
import json
import re
import secrets
import threading
import time
from contextlib import suppress
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from .engine import Flow
from .workspace import atomic_write

ASSETS = Path(__file__).parent
MAX_BODY = 6 * 1024 * 1024


class LoopbackHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer resolves its display name with getfqdn(), which can stall
        # offline or on CI. This server only uses the literal loopback address.
        if self.server_address[0] != "127.0.0.1":
            raise ValueError("Flow servers must bind to IPv4 loopback")
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address


def create_servers(flow: Flow, port=43823):
    """Separate origins: untrusted synthetic app cannot approve workflow edits."""
    control_token, capture_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    cookie_prefix = "pf_" + secrets.token_hex(8) + "_"

    class Base(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def origin(self):
            return f"http://127.0.0.1:{self.server.server_port}"

        def valid_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def json_body(self):
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BODY or self.headers.get("Transfer-Encoding"):
                raise ValueError("Invalid or oversized request")
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("JSON required")
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError("Object required")
            return body

        def reply(self, status, body, media="application/json", filename=None):
            body = json.dumps(body).encode() if media == "application/json" else body
            self.send_response(status)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            )
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(body)

    class Proxy(Base):
        def do_POST(self):
            self.forward()

        do_PUT = do_PATCH = do_DELETE = do_POST

        def do_GET(self):
            self.forward()

        def forward(self):
            if not self.valid_host():
                self.reply(403, {"error": "Host rejected"})
                return
            path = urlsplit(self.path).path
            if path == "/__project_flow_inspector.js" and self.command == "GET":
                body = (
                    (ASSETS / "inspector.js")
                    .read_text()
                    .replace("__CAPTURE_TOKEN__", capture_token)
                    .replace("__FLOW_MODE__", flow.config.mode)
                    .replace("__SAFE_QUERY_KEYS__", json.dumps(list(flow.config.safe_query_keys)))
                    .replace("__PREVIEW_REVISION__", flow.state.get("preview_revision", "initial"))
                    .encode()
                )
                self.reply(200, body, "text/javascript")
                return
            if path == "/__project_flow_capture" and self.command == "POST":
                if self.headers.get("Origin") != self.origin() or not secrets.compare_digest(
                    self.headers.get("X-Flow-Capture", ""), capture_token
                ):
                    self.reply(403, {"error": "Capture origin/token rejected"})
                    return
                try:
                    selection = flow.add_selection(self.json_body())
                    self.reply(201, {"id": selection["id"], "summary": selection["summary"]})
                except (ValueError, OSError, TypeError, KeyError) as exc:
                    self.reply(400, {"error": str(exc)[:1000]})
                return
            if path.startswith("/__project_flow"):
                self.reply(404, {"error": "Not found"})
                return
            if (
                not flow.preview_port
                or not flow.preview_process
                or flow.preview_process.poll() is not None
            ):
                self.reply(
                    503,
                    {"error": "Synthetic preview stopped; refresh from Project Flow"},
                )
                return
            if not self.path.startswith("/") or self.path.startswith("//"):
                self.reply(400, {"error": "Invalid preview path"})
                return
            if self.command != "GET" and (
                not flow.config.preview_mutations or self.headers.get("Origin") != self.origin()
            ):
                self.reply(
                    403,
                    {
                        "error": "Synthetic mutations require preview_mutations=true and same-origin requests"
                    },
                )
                return
            connection = http.client.HTTPConnection("127.0.0.1", flow.preview_port, timeout=15)
            trace_id, started = uuid4().hex, time.time()
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 0 or size > MAX_BODY or self.headers.get("Transfer-Encoding"):
                    raise ValueError("Preview request exceeds limit")
                payload = self.rfile.read(size) if size else None
                upstream = f"http://127.0.0.1:{flow.preview_port}"
                headers = {
                    "Host": f"127.0.0.1:{flow.preview_port}",
                    "Accept-Encoding": "identity",
                    "X-Project-Flow-Trace-Id": trace_id,
                }
                for name in (
                    "Content-Type",
                    "Accept",
                    "X-CSRF-Token",
                    "X-CSRFToken",
                    "Authorization",
                ):
                    if self.headers.get(name):
                        headers[name] = self.headers[name]
                incoming = SimpleCookie(self.headers.get("Cookie", ""))
                cookies = SimpleCookie()
                for name, morsel in incoming.items():
                    if name.startswith(cookie_prefix):
                        cookies[name[len(cookie_prefix) :]] = morsel.value
                if cookies:
                    headers["Cookie"] = "; ".join(m.OutputString() for m in cookies.values())
                if self.headers.get("Origin"):
                    headers["Origin"] = upstream
                connection.request(self.command, self.path, body=payload, headers=headers)
                response = connection.getresponse()
                body = response.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    raise ValueError("Preview response exceeds 8 MB")
                media = response.getheader("Content-Type", "application/octet-stream")
                if "text/html" in media:
                    injection = b'<script src="/__project_flow_inspector.js"></script>'
                    if re.search(b"</body\\s*>", body, re.IGNORECASE):
                        body = re.sub(
                            b"</body\\s*>",
                            lambda match: injection + match.group(),
                            body,
                            count=1,
                            flags=re.IGNORECASE,
                        )
                    else:
                        body += injection
                location = response.getheader("Location")
                if location:
                    parsed = urlsplit(location)
                    if parsed.netloc and parsed.netloc != f"127.0.0.1:{flow.preview_port}":
                        raise ValueError("External preview redirects are blocked")
                    location = location.replace(upstream, self.origin(), 1)
                self.send_response(response.status)
                self.send_header("Content-Type", media)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Project-Flow-Trace-Id", trace_id)
                from .evidence import safe_url

                flow.proxy_events.append(
                    {
                        "trace_id": trace_id,
                        "method": self.command,
                        "path": safe_url(self.path),
                        "status": response.status,
                        "ts_ms": round(started * 1000),
                        "duration_ms": round((time.time() - started) * 1000, 2),
                    }
                )
                # Synthetic-only policy; production CSP is never modified.
                self.send_header(
                    "Content-Security-Policy",
                    flow.config.preview_csp,
                )
                if location:
                    self.send_header("Location", location)
                for name, value in response.getheaders():
                    if name.lower() == "content-disposition":
                        self.send_header(name, value)
                    if name.lower() == "set-cookie":
                        for cookie_name, morsel in SimpleCookie(value).items():
                            rewritten = SimpleCookie()
                            rewritten[cookie_prefix + cookie_name] = morsel.value
                            cookie = rewritten[cookie_prefix + cookie_name]
                            for attr in (
                                "path",
                                "max-age",
                                "expires",
                                "httponly",
                                "samesite",
                            ):
                                if morsel[attr]:
                                    cookie[attr] = morsel[attr]
                            self.send_header("Set-Cookie", cookie.OutputString())
                self.end_headers()
                with suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(body)
            except (OSError, ValueError, http.client.HTTPException):
                self.reply(502, {"error": "Preview request failed; inspect its log"})
            finally:
                connection.close()

    class Handler(Base):
        def do_GET(self):
            if not self.valid_host():
                self.reply(403, {"error": "Host rejected; use 127.0.0.1"})
                return
            parsed = urlsplit(self.path)
            if parsed.path == "/api/state":
                self.reply(200, {**flow.view(), "control_token": control_token})
            elif parsed.path == "/api/capabilities":
                self.reply(
                    200,
                    {
                        "api": "project-flow.control/v1",
                        "version": "0.3.0",
                        "project": str(flow.config.root),
                        "mode": flow.config.mode,
                        "hosted": flow.host.embedded,
                        "controller": getattr(flow, "controller_identity", None),
                        "capabilities": [
                            "issue-composer",
                            "capture",
                            "command-evidence",
                            "export-jobs",
                            "host-services-v1",
                            "evidence-v1",
                            "resume-preview",
                        ],
                    },
                )
            elif parsed.path == "/api/export":
                self.reply(
                    200,
                    flow.export().encode(),
                    "text/markdown",
                    "project-flow-workspace.md",
                )
            elif parsed.path == "/api/artifact":
                args = parse_qs(parsed.query)
                selection = flow.selection(args.get("id", [""])[0])
                path = Path(
                    (selection or {}).get("artifacts", {}).get(args.get("name", [""])[0], "")
                )
                root = flow.config.data / "evidence"
                if (
                    not path.is_file()
                    or path.is_symlink()
                    or not path.resolve().is_relative_to(root.resolve())
                ):
                    self.reply(404, {"error": "Artifact unavailable"})
                else:
                    media = "image/png" if path.suffix == ".png" else "text/plain; charset=utf-8"
                    self.reply(200, path.read_bytes(), media)
            elif parsed.path in {"/", "/app.js", "/style.css"}:
                name = "dashboard.html" if parsed.path == "/" else parsed.path[1:]
                self.reply(
                    200,
                    (ASSETS / name).read_bytes(),
                    {
                        "dashboard.html": "text/html",
                        "app.js": "text/javascript",
                        "style.css": "text/css",
                    }[name],
                )
            else:
                self.reply(404, {"error": "Not found"})

        def do_POST(self):
            if (
                not self.valid_host()
                or self.headers.get("Origin") not in {None, self.origin()}
                or not secrets.compare_digest(self.headers.get("X-Flow-Token", ""), control_token)
            ):
                self.reply(403, {"error": "Control origin/token rejected"})
                return
            try:
                body = self.json_body()
                action = urlsplit(self.path).path
                if (flow.host.embedded or flow.config.mode == "personal") and action in {
                    "/api/begin",
                    "/api/approve-plan",
                    "/api/check",
                    "/api/accept",
                    "/api/reject",
                    "/api/preview",
                }:
                    raise ValueError(
                        "The host owns implementation and review; standalone editing is disabled"
                    )
                if action == "/api/open-test-app":
                    flow.open_test_app(body)
                elif action == "/api/inspect":
                    flow.inspect(body)
                elif action == "/api/open-preview":
                    flow.open_preview()
                elif action == "/api/stop-preview":
                    if flow.inspection_thread and flow.inspection_thread.is_alive():
                        flow.cancelled.set()
                        flow.state["evidence_warning"] = "Stopping preview preparation…"
                    else:
                        flow.stop_preview()
                        flow.state["inspector_status"] = "stopped"
                    flow._save()
                elif action == "/api/issues":
                    flow.save_issues(body.get("issues"), body.get("revision"))
                elif action == "/api/capture-options":
                    with flow.lock:
                        flow.state["capture_narrow"] = body.get("narrow") is True
                        flow._save()
                elif action == "/api/export-start":
                    self.reply(
                        202,
                        flow.exports.start(
                            body.get("level", "standard"),
                            body.get("organize", True) is True,
                            body.get("format", "md"),
                        ),
                    )
                    return
                elif action == "/api/export-skip":
                    job = flow.exports.view()
                    if not job or job["id"] != body.get("id"):
                        raise ValueError("That export is no longer active")
                    flow.exports.skip()
                elif action == "/api/export-download":
                    format = body.get("format", "md")
                    path, filename = flow.exports.artifact(body.get("id"), format)
                    self.reply(
                        200,
                        path.read_bytes(),
                        "text/markdown" if format == "md" else "application/zip",
                        filename,
                    )
                    return
                elif action == "/api/export-link":
                    flow.exports.link_export(body.get("id"))
                elif action == "/api/selection":
                    flow.add_selection(body)
                elif action == "/api/remove-selection":
                    flow.remove_selection(body.get("id"))
                elif action == "/api/clear-selections":
                    flow.clear_selections(
                        body.get("ids"),
                        body.get("revision"),
                        confirmed=body.get("confirmed") is True,
                    )
                elif action == "/api/clear-issues":
                    flow.clear_issues(
                        body.get("ids"),
                        body.get("revision"),
                        confirmed=body.get("confirmed") is True,
                    )
                elif action == "/api/attach":
                    flow.attach(
                        body.get("id"),
                        str(body.get("filename", "")),
                        str(body.get("data", "")),
                    )
                elif action == "/api/locate":
                    selection = flow.selection(body.get("id"))
                    if not selection:
                        raise ValueError("Unknown capture")
                    flow.browser.call("locate", selector=selection["selector"])
                elif action == "/api/replay":
                    if body.get("confirmed") is not True:
                        raise ValueError("Replay requires confirmation of synthetic side effects")
                    self.reply(200, flow.replay_selection(body.get("id")))
                    return
                elif action in {"/api/export-handoff", "/api/export-bundle"}:
                    directory = flow.evidence_export(body.get("level", "standard"))
                    if flow.host.exported:
                        # Legacy downloads also record evidence; linkage never blocks export success.
                        try:
                            linkage = flow.host.exported(directory)
                        except Exception:
                            linkage = {
                                "state": "not_linked",
                                "message": "Session link unavailable; export preserved",
                            }
                        with flow.lock:
                            flow.state["last_legacy_session_link"] = linkage
                            flow._save()
                    path = (
                        directory / "handoff.md"
                        if action.endswith("handoff")
                        else directory.with_suffix(".zip")
                    )
                    self.reply(
                        200,
                        path.read_bytes(),
                        "text/markdown" if action.endswith("handoff") else "application/zip",
                        directory.name + (".md" if action.endswith("handoff") else ".zip"),
                    )
                    return
                elif action == "/api/begin":
                    flow.begin(str(body.get("request", "")))
                elif action == "/api/approve-plan":
                    flow.approve_plan()
                elif action == "/api/check":
                    flow.check()
                elif action == "/api/accept":
                    if body.get("confirmed") is not True:
                        raise ValueError("Explicit apply confirmation is required")
                    flow.accept(risk_confirmed=body.get("risk_confirmed") is True)
                elif action == "/api/reject":
                    flow.reject()
                elif action == "/api/preview":
                    flow.start_preview()
                    flow.open_preview()
                elif action == "/api/shutdown":
                    self.reply(200, {"stopping": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                else:
                    raise ValueError("Unknown action")
                self.reply(200, flow.view())
            except (ValueError, OSError, KeyError, TypeError) as exc:
                self.reply(400, {"error": str(exc)[:1000]})

    server = LoopbackHTTPServer(("127.0.0.1", port), Handler)
    try:
        proxy = LoopbackHTTPServer(("127.0.0.1", 0), Proxy)
    except BaseException:
        server.server_close()
        raise
    flow.proxy_url = f"http://127.0.0.1:{proxy.server_port}/"
    atomic_write(
        flow.config.data / "control.json",
        json.dumps({"port": server.server_port, "token": control_token}).encode(),
    )
    return server, proxy


def serve(flow: Flow, port=43823):
    server, proxy = create_servers(flow, port)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    print(
        f"Project Flow: http://127.0.0.1:{server.server_port} — {flow.config.root}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        proxy.shutdown()
        proxy.server_close()
        server.server_close()
        flow.close()
