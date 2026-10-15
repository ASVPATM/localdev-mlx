"""Bounded, advisory handoff organization; original requests stay authoritative."""

from __future__ import annotations

import copy
import hashlib
import json
import queue
import threading
import time
from uuid import uuid4

from .evidence import clean
from .host import ORGANIZER_VERSION, model_status
from .workspace import atomic_write, safe_path


def validate_advice(raw, issues):
    ids = [issue["id"] for issue in issues]
    if len(ids) != len(set(ids)) or not isinstance(raw, dict) or set(raw) != {"issues"}:
        raise ValueError("Invalid organization shape")
    rows = raw["issues"]
    if not isinstance(rows, list) or len(rows) != len(ids):
        raise ValueError("Organization omitted or added an issue")
    checked = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "id",
            "title",
            "group",
            "depends_on",
            "effort",
        }:
            raise ValueError("Invalid organization fields")
        key, deps = row["id"], row["depends_on"]
        if not isinstance(key, str) or key not in ids or key in checked:
            raise ValueError("Organization contains an unknown or repeated issue")
        if (
            not isinstance(deps, list)
            or any(not isinstance(dep, str) or dep not in ids or dep == key for dep in deps)
            or len(set(deps)) != len(deps)
        ):
            raise ValueError("Organization contains invalid dependencies")
        if not isinstance(row["effort"], str) or row["effort"] not in {
            "small",
            "medium",
            "large",
            "unknown",
        }:
            raise ValueError("Invalid effort category")
        if any(
            not isinstance(row[k], str) or not 1 <= len(row[k]) <= limit
            for k, limit in (("title", 120), ("group", 80))
        ):
            raise ValueError("Invalid organization label")
        checked[key] = {
            **row,
            "title": " ".join(clean(row["title"], 120).split()),
            "group": " ".join(clean(row["group"], 80).split()),
        }
        if not checked[key]["title"] or not checked[key]["group"]:
            raise ValueError("Organization labels must not be blank")
    ordered_ids(issues, checked)  # reject dependency cycles
    return checked


def ordered_ids(issues, advice):
    """Dependencies first, then user priority, related work, and estimated effort."""
    remaining = {row["id"]: row for row in issues}
    original = {row["id"]: index for index, row in enumerate(issues)}
    order, last_group = [], None
    while remaining:
        ready = [
            key
            for key in remaining
            if all(dep in order for dep in advice.get(key, {}).get("depends_on", []))
        ]
        if not ready:
            raise ValueError("Organization contains a dependency cycle")

        def rank(key, previous_group=last_group):
            row, hint = remaining[key], advice.get(key, {})
            return (
                {"blocking": 0, "high": 1, "normal": 2, "low": 3}.get(row.get("priority"), 2),
                0 if previous_group and hint.get("group") == previous_group else 1,
                {"small": 0, "medium": 1, "large": 2, "unknown": 3}.get(hint.get("effort"), 3),
                original[key],
            )

        key = min(ready, key=rank)
        order.append(key)
        last_group = advice.get(key, {}).get("group")
        del remaining[key]
    return order


class ExportService:
    def __init__(self, flow):
        self.flow = flow
        self.thread = self.model_thread = self.status_thread = None
        self.cancel = threading.Event()
        self.lock = threading.RLock()
        self.job = copy.deepcopy(flow.state.get("last_export"))
        if self.job and self.job.get("stage") != "ready":
            self.job = None
        self.status_value = {
            "host": flow.host.name,
            "state": "not_configured",
            "model": "",
            "can_organize": False,
        }
        self.status_checked = 0.0

    def status(self):
        # Polling the dashboard never loads a model or waits for a model endpoint.
        if time.monotonic() - self.status_checked > 15 and not (
            self.status_thread and self.status_thread.is_alive()
        ):
            self.status_checked = time.monotonic()

            def refresh():
                self.status_value = model_status(self.flow.host)

            self.status_thread = threading.Thread(target=refresh, daemon=True)
            self.status_thread.start()
        return copy.deepcopy(self.status_value)

    def view(self):
        with self.lock:
            return copy.deepcopy(self.job)

    def update(self, **fields):
        with self.lock:
            self.job.update(fields)

    def start(self, level="standard", organize=True, format="md"):
        if level not in {"light", "standard", "full"} or format not in {"md", "zip"}:
            raise ValueError("Invalid export options")
        with self.flow.lock, self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError("An export is already preparing")
            with self.flow.lock:
                state = copy.deepcopy(self.flow.state)
                state["preview_stale"] = self.flow.inspection_view()["preview_stale"]
                issues = self.flow.export_issues(state)
            self.cancel = threading.Event()
            self.job = {
                "id": uuid4().hex,
                "stage": "collecting",
                "format": format,
                "message": "Collecting the saved issues and evidence",
                "level": level,
                "issues_revision": state.get("issues_revision", 0),
                "organized": False,
            }

            def worker():
                try:
                    self.freeze_evidence(state, issues)
                    advice, metadata = (
                        self.organize(state, issues)
                        if organize
                        else ({}, {"reason": "Local organization disabled for this export"})
                    )
                    self.update(
                        stage="checking", message="Checking completeness and writing evidence"
                    )
                    directory = self.flow.evidence_export(
                        level, snapshot=state, advice=advice, organization=metadata
                    )
                    self.update(
                        stage="ready",
                        message="Handoff ready",
                        directory=str(directory),
                        organized=bool(advice),
                        organization=metadata,
                    )
                    self.persist()
                    self.link_export(self.job["id"])
                except Exception as exc:
                    if self.view().get("stage") == "ready":
                        self.update(
                            session_link={
                                "state": "not_linked",
                                "retryable": True,
                                "message": "Export is complete; saving its session link failed. Retry linking.",
                            }
                        )
                    else:
                        self.update(stage="failed", message=clean(exc, 600))

            self.thread = threading.Thread(target=worker, daemon=True)
            self.thread.start()
            return self.view()

    def freeze_evidence(self, state, issues):
        """Copy only referenced artifact versions before inference, never under a session lock."""
        from pathlib import Path

        selected = {sid for row in issues for sid in row.get("selection_ids", [])}
        total, identities = 0, {}
        root = self.flow.config.data / "evidence"
        for selection in state.get("selections", []):
            if selection["id"] not in selected:
                continue
            hashes = {}
            for name, value in selection.get("artifacts", {}).items():
                path = Path(value)
                if (
                    path.is_symlink()
                    or not path.resolve().is_relative_to(root.resolve())
                    or any(p.is_symlink() for p in path.parents)
                    or not path.is_file()
                ):
                    hashes[name] = "unavailable"
                    continue  # Export preserves the omission reason.
                if path.stat().st_size > 100 * 1024 * 1024:
                    raise ValueError("Evidence file exceeds export budget")
                content = path.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
                if digest not in identities:
                    total += len(content)
                    if total > 100 * 1024 * 1024:
                        raise ValueError("Export exceeds 100 MB; select fewer captures")
                    identities[digest] = True
                target = safe_path(root, "blobs/" + digest + path.suffix)
                if target.exists():
                    if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                        raise ValueError("Content-addressed evidence was modified")
                else:
                    atomic_write(target, content)
                selection["artifacts"][name] = str(target)
                hashes[name] = digest
            selection["frozen_artifact_sha256"] = hashes
        state["evidence_digest"] = hashlib.sha256(
            json.dumps(
                [s for s in state.get("selections", []) if s["id"] in selected],
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()

    def organize(self, state, issues):
        cancel = self.cancel
        payload = {
            "schema": ORGANIZER_VERSION,
            "issues": [],
            "evidence_digest": state.get("evidence_digest", ""),
        }
        selections = {row["id"]: row for row in state.get("selections", [])}
        for issue in issues:
            payload["issues"].append(
                {
                    **issue,
                    "captures": [
                        {
                            "id": sid,
                            "summary": selections[sid].get("summary", {}),
                            "source_hints": [
                                {"path": h.get("path"), "line": h.get("line")}
                                for h in selections[sid].get("source_hints", [])[:3]
                            ],
                        }
                        for sid in issue.get("selection_ids", [])
                        if sid in selections
                    ],
                }
            )
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        if len(encoded) > 24000:
            return {}, {
                "reason": "Batch exceeds the 24,000-character local-analysis budget; all requests remain in the ordinary handoff"
            }
        self.update(
            stage="organizing",
            message="Checking the local model connection; ordinary export is available",
        )
        deadline = time.monotonic() + self.flow.config.organizer_timeout
        responses = queue.Queue(maxsize=1)
        if self.model_thread and self.model_thread.is_alive():
            return {}, {
                "reason": "A previous local request is still finishing; ordinary export used"
            }

        def infer():
            try:
                status = model_status(self.flow.host)
                self.status_value = status
                if not status.get("identity"):
                    raise ValueError("No configured local helper; ordinary export used")
                key = hashlib.sha256(
                    (ORGANIZER_VERSION + encoded + status["identity"]).encode()
                ).hexdigest()
                cache = safe_path(self.flow.config.data, "organization-cache/" + key + ".json")
                metadata = {
                    "provider": status.get("provider"),
                    "model": status.get("model"),
                    "identity": status["identity"],
                    "version": ORGANIZER_VERSION,
                    "cached": False,
                    "input_characters": len(encoded),
                    "advisory": True,
                }
                try:
                    if (
                        cache.is_file()
                        and not cache.is_symlink()
                        and cache.stat().st_size <= 131072
                    ):
                        advice = validate_advice(json.loads(cache.read_text()), issues)
                        responses.put((advice, {**metadata, "cached": True}))
                        return
                except (OSError, ValueError, TypeError):
                    pass  # Bad cache is not trusted and never poisons the ordinary handoff.
                if (
                    not status.get("can_organize")
                    or status.get("state") not in {"ready", "not_loaded"}
                    or (status.get("state") == "not_loaded" and not status.get("can_load"))
                ):
                    raise ValueError(
                        f"Local helper is {status.get('state', 'unavailable')}; ordinary export used"
                    )
                if cancel.is_set():
                    return
                result = self.flow.host.organize(
                    payload, cancel, max(0.1, deadline - time.monotonic())
                )
                if cancel.is_set():
                    return
                raw = json.dumps(result)
                if len(raw.encode()) > 131072:
                    raise ValueError("Local organization exceeds the output budget")
                advice = validate_advice(result, issues)
                usage = getattr(result, "metadata", {})
                if usage.get("identity", status["identity"]) != status["identity"]:
                    raise ValueError("Selected model changed during export; ordinary handoff used")
                metadata.update(usage)
                atomic_write(cache, raw.encode())
                responses.put((advice, metadata))
            except Exception as exc:
                responses.put(({}, {"reason": clean(exc, 400)}))

        # Host callbacks must honor deadlines. A slow/failed callback cannot prevent
        # ordinary export, and its late result is never applied to another batch.
        self.model_thread = threading.Thread(target=infer, daemon=True)
        self.model_thread.start()
        while time.monotonic() < deadline and not cancel.is_set():
            try:
                response = responses.get(timeout=0.05)
                if cancel.is_set():
                    break
                return response
            except queue.Empty:
                pass
        cancel.set()
        return {}, {"reason": "Local organization skipped or timed out; every issue is preserved"}

    def skip(self):
        self.cancel.set()

    def persist(self):
        job = self.view()
        with self.flow.lock:
            self.flow.state["last_export"] = job
            self.flow._save()

    def link_export(self, job_id):
        from pathlib import Path

        with self.lock:
            job = self.view()
            if not job or job["id"] != job_id or job["stage"] != "ready":
                raise ValueError("That export is not ready for session linkage")
            if not self.flow.host.exported:
                return
        try:
            result = self.flow.host.exported(Path(job["directory"]))
        except Exception:
            result = {
                "state": "not_linked",
                "message": "Session link failed; export is preserved",
                "retryable": True,
            }
        with self.lock:
            if self.job["id"] == job_id:
                self.update(session_link=result)
        self.persist()

    def artifact(self, job_id, format):
        job = self.view()
        if not job or job["id"] != job_id or job["stage"] != "ready" or format not in {"md", "zip"}:
            raise ValueError("The requested export is not ready")
        from pathlib import Path

        directory = Path(job["directory"])
        path = directory / "handoff.md" if format == "md" else directory.with_suffix(".zip")
        root = self.flow.config.data / "exports"
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(root.resolve())
            or not path.is_file()
        ):
            raise ValueError("Export file is unavailable")
        return path, directory.name + "." + format

    def close(self):
        self.skip()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)
        if self.model_thread and self.model_thread.is_alive():
            self.model_thread.join(timeout=10)
