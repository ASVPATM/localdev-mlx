from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from localdev_mlx.flow.config import Config
from localdev_mlx.flow.engine import Flow
from localdev_mlx.flow.evidence import compact_capture
from localdev_mlx.flow.host import HostServices
from localdev_mlx.flow.organization import ordered_ids, validate_advice


def issue(key, *, priority="normal", prompt=None, selections=()):
    return {
        "id": key,
        "prompt": prompt or f"Original user request {key}",
        "expected": "Keep original expected behavior",
        "priority": priority,
        "type": "behavior",
        "selection_ids": list(selections),
    }


def advice(issues):
    return {
        "issues": [
            {
                "id": row["id"],
                "title": f"Suggested {row['id']}",
                "group": "Related changes",
                "depends_on": [],
                "effort": "small",
            }
            for row in issues
        ]
    }


def make_flow(tmp_path, organize=None, state="ready", **config):
    root = tmp_path / "project"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src/app.py").write_text("value = 1\n")
    host = HostServices(
        "Synthetic LocalDev host",
        lambda: {
            "model": "fixture-model",
            "provider": "MLX fixture",
            "state": state,
            "identity": "fixture-v1",
            "can_organize": organize is not None,
        },
        organize,
    )
    return Flow(Config(root, **config), host=host)


def export(flow, **options):
    flow.exports.start(**options)
    flow.exports.thread.join(timeout=5)
    job = flow.exports.view()
    assert job["stage"] == "ready", job
    directory = Path(job["directory"])
    return (
        job,
        json.loads((directory / "manifest.json").read_text()),
        (directory / "handoff.md").read_text(),
    )


def test_hosted_export_organizes_caches_and_preserves_originals(tmp_path):
    calls = []

    def organize(payload, cancel, timeout):
        assert timeout > 0 and not cancel.is_set()
        calls.append(payload)
        result = advice(payload["issues"])
        result["issues"][0]["depends_on"] = [payload["issues"][1]["id"]]
        return result

    flow = make_flow(tmp_path, organize)
    try:
        selected = flow.add_selection({"text": "Fixture capture"})
        originals = [
            issue("large-ui", selections=[selected["id"]]),
            issue("data-foundation", selections=[selected["id"]]),
        ]
        flow.save_issues(originals)
        assert not calls
        with pytest.raises(ValueError, match="host owns"):
            flow.begin("Must not run a competing edit controller")
        job, manifest, markdown = export(flow)
        assert job["organized"] and len(calls) == 1
        assert manifest["issues"] == originals
        assert manifest["suggested_order"] == ["data-foundation", "large-ui"]
        assert set(manifest["selections"]) == {selected["id"]}
        assert all(row["prompt"] in markdown and row["expected"] in markdown for row in originals)
        assert "advisory" in markdown and "not proof" in markdown
        again, _, _ = export(flow)
        assert again["organization"]["cached"] and len(calls) == 1
        assert (flow.config.root / "src/app.py").read_text() == "value = 1\n"
        assert not (flow.config.root / ".git").exists()
        assert (flow.config.root / ".project-flow/organization-cache").is_dir()
    finally:
        flow.close()


@pytest.mark.parametrize(
    "invalid", ["missing", "unknown", "duplicate", "cycle", "rewrite", "long", "bad-type"]
)
def test_invalid_model_output_cannot_drop_or_rewrite_an_issue(tmp_path, invalid):
    def organize(payload, *_):
        result = advice(payload["issues"])
        if invalid == "missing":
            result["issues"].pop()
        elif invalid == "unknown":
            result["issues"][0]["id"] = "invented"
        elif invalid == "duplicate":
            result["issues"][0]["id"] = result["issues"][1]["id"]
        elif invalid == "cycle":
            result["issues"][0]["depends_on"] = ["b"]
            result["issues"][1]["depends_on"] = ["a"]
        elif invalid == "rewrite":
            result["issues"][0]["prompt"] = "A rewritten request"
        elif invalid == "long":
            result["issues"][0]["title"] = "x" * 121
        else:
            result["issues"] = "wrong type"
        return result

    flow = make_flow(tmp_path, organize)
    try:
        originals = [issue("a"), issue("b")]
        flow.save_issues(originals)
        job, manifest, markdown = export(flow)
        assert not job["organized"] and job["organization"]["reason"]
        assert manifest["issues"] == originals and manifest["advice"] == {}
        assert "A rewritten request" not in markdown and "invented" not in markdown
    finally:
        flow.close()


def test_skip_uses_frozen_snapshot_and_does_not_apply_a_late_model_result(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def organize(payload, cancel, timeout):
        entered.set()
        release.wait(3)
        return advice(payload["issues"])

    flow = make_flow(tmp_path, organize)
    try:
        flow.save_issues([issue("before")])
        flow.exports.start()
        assert entered.wait(2)
        assert flow.exports.view()["stage"] == "organizing"
        with pytest.raises(ValueError, match="already"):
            flow.exports.start()
        flow.save_issues([issue("after")])
        start = time.monotonic()
        flow.exports.skip()
        flow.exports.thread.join(2)
        assert time.monotonic() - start < 2
        job = flow.exports.view()
        assert job["stage"] == "ready" and not job["organized"]
        manifest = json.loads((Path(job["directory"]) / "manifest.json").read_text())
        assert manifest["issues"][0]["id"] == "before"
        assert flow.state["issues"][0]["id"] == "after"
        release.set()
        flow.exports.model_thread.join(2)
        assert flow.exports.view() == job
    finally:
        release.set()
        flow.close()


def test_timeout_and_large_batch_export_every_issue_without_inference_retries(tmp_path):
    calls = []

    def organize(payload, cancel, timeout):
        calls.append(payload)
        cancel.wait(2)
        return advice(payload["issues"])

    flow = make_flow(tmp_path, organize, organizer_timeout=1)
    try:
        flow.save_issues([issue("one")])
        job, _, _ = export(flow)
        assert not job["organized"] and "timed out" in job["organization"]["reason"]
        assert len(calls) == 1
        originals = [issue(str(i), prompt="x" * 11000) for i in range(3)]
        flow.save_issues(originals)
        job, manifest, _ = export(flow)
        assert "budget" in job["organization"]["reason"]
        assert manifest["issues"] == originals and len(calls) == 1
    finally:
        flow.close()


def test_priorities_and_prerequisites_beat_effort_order():
    issues = [
        issue("tiny", priority="low"),
        issue("blocking", priority="blocking"),
        issue("foundation"),
    ]
    data = advice(issues)
    data["issues"][1]["depends_on"] = ["foundation"]
    validated = validate_advice(data, issues)
    assert ordered_ids(issues, validated) == ["foundation", "blocking", "tiny"]


def test_organization_labels_are_single_line_and_empty_labels_rejected():
    issues = [issue("one")]
    data = advice(issues)
    data["issues"][0]["title"] = "Heading\n\nother line"
    assert validate_advice(data, issues)["one"]["title"] == "Heading other line"
    data["issues"][0]["group"] = "   "
    with pytest.raises(ValueError, match="blank"):
        validate_advice(data, issues)


def test_cached_organization_works_without_reloading_the_model(tmp_path):
    calls = []

    def organize(payload, *_):
        calls.append(payload)
        return advice(payload["issues"])

    flow = make_flow(tmp_path, organize)
    try:
        flow.save_issues([issue("one")])
        export(flow)
        original = flow.host.status
        flow.host = HostServices(
            "Same host",
            lambda: {**original(), "state": "not_loaded", "can_organize": False},
            organize,
        )
        job, _, _ = export(flow)
        assert job["organization"]["cached"] and len(calls) == 1
    finally:
        flow.close()


def test_autosaved_blank_drafts_conflicts_and_capture_deletion_are_safe(tmp_path):
    flow = make_flow(tmp_path)
    try:
        selected = flow.add_selection({"text": "Keep this capture"})
        draft = issue("draft", selections=[selected["id"]])
        draft["prompt"] = ""
        flow.save_issues([draft], expected_revision=0)
        assert flow.state["issues"][0]["prompt"] == ""
        with pytest.raises(ValueError, match="description"):
            flow.exports.start()
        with pytest.raises(ValueError, match="another tab"):
            flow.save_issues([issue("stale")], expected_revision=0)
        flow.save_issues([issue("ready", selections=[selected["id"]])], expected_revision=1)
        flow.remove_selection(selected["id"])
        assert flow.state["issues_revision"] == 3
        assert flow.state["issues"][0]["selection_ids"] == []
        assert Path(selected["artifacts"]["capture"]).exists()
        with pytest.raises(ValueError, match="unique"):
            flow.save_issues([issue("same"), issue("same")])
    finally:
        flow.close()


def test_repeated_console_events_keep_counts_and_temporal_hints():
    result = compact_capture(
        {
            "actions": [{"id": "click", "ts_ms": 1000}],
            "console": [
                {"id": "error1", "ts_ms": 1200, "at": "a", "level": "error", "text": "Failed"},
                {"id": "error2", "ts_ms": 1300, "at": "b", "level": "error", "text": "Failed"},
            ],
            "network": [{"id": "r1", "ts_ms": 1100, "status": 500, "trace_id": "abc"}],
        }
    )
    assert len(result["console"]) == 1 and result["console"][0]["count"] == 2
    assert result["console"][0]["first_at"] == "a" and result["console"][0]["last_at"] == "b"
    assert result["network"][0]["action_hint"] == "click"
    assert result["network"][0]["correlation"] == "temporal-only"


def test_unavailable_host_does_not_block_export_or_allow_paths_to_other_jobs(tmp_path):
    flow = make_flow(tmp_path, state="unavailable")
    try:
        flow.save_issues([issue("one")])
        job, manifest, _ = export(flow)
        assert not job["organized"] and manifest["issues"][0]["id"] == "one"
        with pytest.raises(ValueError):
            flow.exports.artifact("wrong", "md")
        with pytest.raises(ValueError):
            flow.exports.artifact(job["id"], "../../secret")
    finally:
        flow.close()
