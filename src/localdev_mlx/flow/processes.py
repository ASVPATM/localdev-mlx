"""Private ownership journal for crash recovery; never signal a PID by number alone."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time

from localdev_mlx.models.manager import ModelManager

from .workspace import atomic_write


class OwnedProcesses:
    def __init__(self, path):
        self.path = path
        self.rows = {}
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.thread = None

    def add(self, process, *, kind="preview", token=None):
        pid = process if isinstance(process, int) else process.pid
        signature = ModelManager._fingerprint(pid)
        if not signature:
            return
        with self.lock:
            self.rows[pid] = {"pid": pid, "fingerprint": signature, "kind": kind, "token": token}
            self.save()
            if not self.thread:
                self.thread = threading.Thread(target=self.watch, daemon=True)
                self.thread.start()

    def save(self):
        atomic_write(
            self.path, json.dumps({"schema": 1, "processes": list(self.rows.values())}).encode()
        )

    @staticmethod
    def table():
        result = subprocess.run(
            ["ps", "-ax", "-o", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        rows = {}
        for line in result.stdout.splitlines():
            fields = line.strip().split(None, 2)
            if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
                rows[int(fields[0])] = (int(fields[1]), fields[2])
        return rows

    def browser_started(self):
        # Only Playwright drivers parented to this controller; never a user's Chrome.
        for pid, (parent, command) in self.table().items():
            if parent == os.getpid() and "playwright/driver/" in command:
                self.add(pid, kind="browser")
        self.observe()

    def observe(self):
        table = self.table()
        with self.lock:
            roots = {
                pid
                for pid, row in self.rows.items()
                if row["kind"] in {"preview", "browser"}
                and ModelManager._fingerprint(pid) == row["fingerprint"]
            }
            descendants = set(roots)
            for _ in range(12):
                children = {pid for pid, (parent, _) in table.items() if parent in descendants}
                if children <= descendants:
                    break
                descendants.update(children)
            for pid in sorted(descendants - self.rows.keys())[:256]:
                signature = ModelManager._fingerprint(pid)
                if signature:
                    self.rows[pid] = {"pid": pid, "fingerprint": signature, "kind": "browser"}
            # Keep only live, verified children; the runtime journal is not historical evidence.
            self.rows = {
                pid: row
                for pid, row in self.rows.items()
                if row.get("kind") == "organizer"
                or ModelManager._fingerprint(pid) == row["fingerprint"]
            }
            organizers = [pid for pid, row in self.rows.items() if row.get("kind") == "organizer"]
            for pid in organizers[:-128]:
                self.rows.pop(pid)
            self.save()

    def watch(self):
        while not self.closed.wait(0.5):
            try:
                self.observe()
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass

    def close(self):
        self.closed.set()
        if self.thread:
            self.thread.join(3)
        self.recover(self.path)

    @staticmethod
    def recover(path):
        from .controller import read_json
        from .localdev_host import cleanup_worker_model

        data = read_json(path)
        if data and data.get("schema") != 1:
            raise ValueError("Unsupported process ownership journal")
        rows = data.get("processes", [])
        if not isinstance(rows, list) or len(rows) > 512:
            raise ValueError("Invalid process ownership journal")

        def matches(row):
            return (
                isinstance(row, dict)
                and type(row.get("pid")) is int
                and row["pid"] > 1
                and row.get("fingerprint")
                and ModelManager._fingerprint(row["pid"]) == row["fingerprint"]
            )

        for sig in (signal.SIGTERM, signal.SIGKILL):
            for row in reversed(rows):
                if matches(row):
                    try:
                        os.kill(row["pid"], sig)
                    except ProcessLookupError:
                        pass
            if sig == signal.SIGTERM and any(matches(row) for row in rows):
                end = time.monotonic() + 2
                while time.monotonic() < end and any(matches(row) for row in rows):
                    time.sleep(0.05)
        for row in rows:
            if row.get("kind") == "organizer" and row.get("token"):
                cleanup_worker_model(row["pid"], row["token"])
