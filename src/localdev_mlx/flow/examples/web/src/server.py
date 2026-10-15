"""Disposable example adapter. No real files, accounts, models or external services."""

import json
import os
import random
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingTCPServer

scenario = os.environ.get("PROJECT_FLOW_SCENARIO", "small")
rng = random.Random(int(os.environ.get("PROJECT_FLOW_SEED", "42")))
items = [
    {"id": index, "title": f"Example task {index + 1}", "done": rng.choice([True, False])}
    for index in range({"empty": 0, "small": 6, "large": 40}.get(scenario, 6))
]


class Preview(BaseHTTPRequestHandler):
    def reply(self, status, data, media):
        self.send_response(status)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/favicon.ico":
            self.reply(204, b"", "image/x-icon")
        elif self.path == "/api/items":
            self.reply(200, json.dumps(items).encode(), "application/json")
        elif self.path in {"/", "/app.js", "/style.css"}:
            name = "index.html" if self.path == "/" else self.path[1:]
            self.reply(
                200,
                Path("src", name).read_bytes(),
                {
                    "index.html": "text/html",
                    "app.js": "text/javascript",
                    "style.css": "text/css",
                }[name],
            )
        else:
            self.reply(404, b'{"error":"Synthetic example failure"}', "application/json")

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size < 4096 or self.path != "/api/items":
            self.reply(400, b"{}", "application/json")
            return
        data = json.loads(self.rfile.read(size))
        items.append(
            {"id": len(items), "title": str(data.get("title", "New task"))[:120], "done": False}
        )
        self.reply(201, b'{"saved":true}', "application/json")


# The sample serves a literal loopback address; it does not need reverse DNS.
ThreadingTCPServer(("127.0.0.1", int(sys.argv[1])), Preview).serve_forever()
