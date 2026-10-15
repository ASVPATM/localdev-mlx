from __future__ import annotations

import base64
import copy
import json
import os
import socket
import subprocess
import sys
import threading
import time
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from localdev_mlx.cli import app
from localdev_mlx.flow import controller, localdev_host, organizer_worker
from localdev_mlx.flow.config import Config
from localdev_mlx.flow.engine import Flow
from localdev_mlx.flow.host import ORGANIZER_VERSION, HostServices
from localdev_mlx.flow.session_evidence import Bundle, link_export, validate_bundle
from localdev_mlx.flow.web import create_servers
from localdev_mlx.models.manager import ModelManager, ModelManagerError
from localdev_mlx.sessions import SessionStore

from .test_organization import advice, export, issue


def manual(root, **kwargs):
    return Flow(
        Config(root, preview_kind="command"),
        host=HostServices("LocalDev fixture", lambda: {"state": "not_configured"}, **kwargs),
    )


def test_session_link_is_canonical_immutable_idempotent_and_retryable(sample_repo):
    flow = manual(sample_repo, exported=lambda path: link_export(sample_repo, path))
    try:
        capture = flow.add_selection(
            {"text": "Native evidence", "anchor": {"path": "src/samplecalc/core.py", "line": 1}}
        )
        originals = [issue("a", selections=[capture["id"]]), issue("b")]
        flow.save_issues(originals)
        store = SessionStore(sample_repo)
        with store._lock():
            job, manifest, _ = export(flow, organize=False)
            assert job["stage"] == "ready" and job["session_link"]["retryable"]
            assert "busy" in job["session_link"]["message"].lower()
        flow.exports.link_export(job["id"])
        linked = flow.exports.view()["session_link"]
        assert linked["state"] == "linked", linked
        session = store.current()
        evidence = session["entries"][0]["flow_evidence"]
        assert evidence["manifest"] == manifest
        original_markdown = Path(evidence["markdown"]).read_bytes()
        assert all(row["prompt"] in store.handoff(session).read_text() for row in originals)
        repeated, _, _ = export(flow, organize=False)
        assert repeated["session_link"]["digest"] == linked["digest"]
        assert len(store.current()["entries"]) == 1
        # ZIP reimport is inert and uses the same canonical identity.
        assert (
            link_export(sample_repo, Path(job["directory"]).with_suffix(".zip"))["state"]
            == "linked"
        )
        assert len(store.current()["entries"]) == 1
        flow.save_issues([issue("a", prompt="Changed original", selections=[capture["id"]])])
        changed, _, _ = export(flow, organize=False)
        assert changed["session_link"]["digest"] != linked["digest"]
        assert len(store.current()["entries"]) == 2
        assert Path(evidence["markdown"]).read_bytes() == original_markdown
        Path(evidence["markdown"]).write_text("tampered historical handoff")
        failed = link_export(sample_repo, Path(job["directory"]))
        assert failed["state"] == "not_linked" and "mismatch" in failed["message"]
        assert len(store.current()["entries"]) == 2
    finally:
        flow.close()


def test_link_failure_and_restart_preserve_ready_download(tmp_path):
    flow = manual(tmp_path, exported=lambda path: link_export(tmp_path, path))
    flow.save_issues([issue("a")])
    job, _, _ = export(flow, organize=False)
    assert job["session_link"]["state"] == "not_linked"
    flow.close()
    reopened = manual(tmp_path)
    try:
        artifact, _ = reopened.exports.artifact(job["id"], "md")
        assert artifact.read_text()
        assert not (tmp_path / ".git").exists()
    finally:
        reopened.close()


def test_flow_export_links_after_init_skips_initial_snapshot(tmp_path, monkeypatch):
    from localdev_mlx.git import GitRepository

    monkeypatch.setattr("localdev_mlx.config.DATA_ROOT", tmp_path / "private-runtime")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env.example").write_text("TOKEN=placeholder\n")
    result = CliRunner().invoke(app, ["init", str(project)])
    assert result.exit_code == 0, result.output
    flow = manual(project, exported=lambda path: link_export(project, path))
    try:
        capture = flow.add_selection({"text": "Application area to improve"})
        flow.save_issues([issue("a", selections=[capture["id"]])])
        job, _, _ = export(flow, organize=False)
        assert job["stage"] == "ready" and job["session_link"]["state"] == "linked", job
        store = SessionStore(project)
        session = store.current()
        assert session["baseline"]["commit"] is None
        assert len(session["entries"]) == 1
        assert "no commits yet" in store.handoff(session).read_text()
        assert not GitRepository(project).has_commits()
        assert GitRepository._run(project, ["ls-files", "--stage"]).stdout == ""
        assert (project / ".env.example").read_text() == "TOKEN=placeholder\n"
    finally:
        flow.close()


@pytest.mark.parametrize(
    "kind", ["traversal", "absolute", "duplicate", "symlink", "oversized", "unknown-file"]
)
def test_zip_import_rejects_unsafe_entries(tmp_path, kind):
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        name = {"traversal": "../outside", "absolute": "/outside"}.get(kind, "file")
        info = zipfile.ZipInfo(name)
        if kind == "symlink":
            info.external_attr = 0o120777 << 16
        archive.writestr(info, b"data")
        if kind == "duplicate":
            with pytest.warns(UserWarning):
                archive.writestr("file", b"again")
    if kind == "oversized":
        from localdev_mlx.flow import session_evidence

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(session_evidence, "MAX_FILE", 2)
            with pytest.raises(ValueError, match="budget"):
                Bundle(archive_path)
    elif kind == "unknown-file":
        bundle = Bundle(archive_path)
        try:
            with pytest.raises(ValueError):
                validate_bundle(bundle, tmp_path)
        finally:
            bundle.close()
    else:
        with pytest.raises(ValueError):
            Bundle(archive_path)


@pytest.mark.parametrize(
    "kind", ["hash", "project", "schema", "id", "capture", "absolute", "omission", "symlink"]
)
def test_manifest_import_rejects_tampering(tmp_path, kind):
    flow = manual(tmp_path)
    try:
        capture = flow.add_selection({"text": "Manual"})
        flow.save_issues([issue("a", selections=[capture["id"]])])
        path = flow.evidence_export()
        manifest_path = path / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        selection = manifest["selections"][capture["id"]]
        if kind == "hash":
            (path / selection["artifacts"]["capture"]).write_text("changed")
        elif kind == "project":
            manifest["project"] = str(tmp_path.parent)
        elif kind == "schema":
            manifest["schema"] = "project-flow.evidence/v2"
        elif kind == "id":
            manifest["issues"].append(copy.deepcopy(manifest["issues"][0]))
        elif kind == "capture":
            manifest["issues"][0]["selection_ids"] = ["missing"]
        elif kind == "absolute":
            selection["artifacts"]["capture"] = "/etc/passwd"
        elif kind == "omission":
            manifest["omitted_artifacts"] = [
                {"selection_id": capture["id"], "artifact": "missing", "reason": ""}
            ]
        else:
            (path / "linked").symlink_to(tmp_path)
        manifest_path.write_text(json.dumps(manifest))
        with pytest.raises(ValueError):
            bundle = Bundle(path)
            try:
                validate_bundle(bundle, tmp_path)
            finally:
                bundle.close()
    finally:
        flow.close()


def test_optional_fields_omissions_and_source_drift_preserved(sample_repo):
    flow = manual(sample_repo)
    try:
        capture = flow.add_selection({"text": "Manual"})
        flow.attach(capture["id"], "diagnostic.log", base64.b64encode(b"diagnostic").decode())
        flow.save_issues([issue("a", selections=[capture["id"]])])
        flow.state["inspection"] = {"fingerprint": "old-source"}
        path = flow.evidence_export("light")
        manifest_path = path / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["optional_future_field"] = {"opaque": "preserve as data"}
        manifest_path.write_text(json.dumps(manifest))
        result = link_export(sample_repo, path)
        assert result["state"] == "linked", result
        stored = SessionStore(sample_repo).current()["entries"][0]["flow_evidence"]
        assert stored["manifest"]["optional_future_field"] == manifest["optional_future_field"]
        assert stored["manifest"]["omitted_artifacts"]
        assert any("changed" in warning for warning in stored["warnings"])
    finally:
        flow.close()


def test_every_hosted_edit_route_is_rejected_over_http(tmp_path):
    flow = manual(tmp_path)
    server, proxy = create_servers(flow, 0)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        with httpx.Client(base_url=url, trust_env=False) as client:
            token = client.get("/api/state").json()["control_token"]
            for route in ["begin", "approve-plan", "check", "accept", "reject", "preview"]:
                response = client.post(
                    f"/api/{route}",
                    json={"request": "change", "confirmed": True},
                    headers={"X-Flow-Token": token},
                )
                assert response.status_code == 400
                assert "disabled" in response.json()["error"]
        assert not flow.state.get("run_id")
    finally:
        server.shutdown()
        thread.join(2)
        server.server_close()
        proxy.server_close()
        flow.close()


@pytest.fixture
def private_runtime(tmp_path, monkeypatch):
    runtime = tmp_path / "home"
    monkeypatch.setenv("LOCALDEV_MLX_HOME", str(runtime))
    monkeypatch.setattr(controller, "STATE_ROOT", runtime)
    return runtime


def test_owned_controller_repeat_collision_two_projects_and_non_git_shell(
    tmp_path, private_runtime
):
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    try:
        one = controller.start(first, port=blocker.getsockname()[1], open_browser=False)
        assert one["state"] == "running"
        assert controller.start(first, open_browser=False)["pid"] == one["pid"]
        two = controller.start(second, open_browser=False)
        assert two["pid"] != one["pid"]
        result = CliRunner().invoke(
            app, ["--repo", str(first)], input="flow status\nflow start --no-open\n"
        )
        assert result.exit_code == 0, result.output
        assert controller.status(first)["pid"] == one["pid"]  # EOF does not close Flow.
        assert controller.stop(first)["state"] == "stopped"
        assert controller.status(second)["state"] == "running"
        assert blocker.fileno() >= 0
        assert not (first / ".git").exists()
        state = controller.read_json(controller.runtime(second) / "controller.json")
        assert os.stat(controller.runtime(second) / "controller.json").st_mode & 0o777 == 0o600
        bad = {**state, "fingerprint": "reused-pid"}
        assert not controller.verified(second, bad)
        bad = {**state, "project": str(first)}
        assert not controller.verified(second, bad)
    finally:
        controller.stop(first)
        controller.stop(second)
        blocker.close()


def test_controller_stale_pid_and_unrelated_process_are_preserved(tmp_path, private_runtime):
    state_path = controller.runtime(tmp_path) / "controller.json"
    state_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "fingerprint": "stale",
                "project": str(tmp_path),
                "api": controller.API,
                "version": "old",
            }
        )
    )
    assert controller.stop(tmp_path)["state"] == "stopped"
    try:
        assert controller.start(tmp_path, open_browser=False)["state"] == "running"
    finally:
        controller.stop(tmp_path)


def test_status_busy_cached_model_list_and_actual_resident_profile(global_config, monkeypatch):
    monkeypatch.setattr(localdev_host, "load_global_config", lambda: global_config)

    class Manager:
        @contextmanager
        def lease(self):
            yield

        def _read_state(self):
            return None

        def _port_open(self, *_):
            return True

        def _served_models(self, _):
            return ["mock/planner", "mock/worker"]

    monkeypatch.setattr(localdev_host, "ModelManager", Manager)
    assert localdev_host.helper_status()["state"] == "not_loaded"
    monkeypatch.setattr(Manager, "_served_models", lambda *args: ["mock/planner"])
    status = localdev_host.helper_status()
    assert status["state"] == "ready" and status["model"] == "mock/planner"

    @contextmanager
    def busy(self):
        raise ModelManagerError("Busy")
        yield

    monkeypatch.setattr(Manager, "lease", busy)
    assert localdev_host.helper_status()["state"] == "busy"


@pytest.mark.parametrize("value", ["", "   ", "first\nsecond", "tab\tvalue", "x" * 121])
def test_strict_worker_labels(value):
    response = advice([issue("a")])
    response["issues"][0]["title"] = value
    with pytest.raises(ValidationError):
        organizer_worker.OrganizerResponse.model_validate(response)


def test_worker_provider_is_bounded_main_thread_and_preserves_resident(
    global_config, monkeypatch, tmp_path
):
    monkeypatch.setattr(organizer_worker, "load_global_config", lambda: global_config)
    monkeypatch.setattr(organizer_worker, "selected_model", lambda *_: (global_config.worker, True))
    monkeypatch.setattr(organizer_worker, "model_context", lambda *_: 32768)
    monkeypatch.setattr(
        ModelManager, "stop", lambda *_: pytest.fail("Preexisting model must not stop")
    )
    import localdev_mlx.models.manager as manager_module

    monkeypatch.setattr(manager_module, "STATE_ROOT", tmp_path)

    def complete(self, **kwargs):
        assert threading.current_thread() is threading.main_thread()
        profile = kwargs["profile"]
        assert (
            not profile.enable_thinking
            and profile.thinking_budget == 0
            and profile.max_tokens == 1800
        )
        assert profile.request_timeout_seconds <= 20
        self.last_metadata = {
            "usage": {"prompt_tokens": 12, "completion_tokens": 9, "secret": "omit"}
        }
        return organizer_worker.OrganizerResponse.model_validate(advice([issue("a")]))

    monkeypatch.setattr(organizer_worker.MLXOpenAIProvider, "complete_structured", complete)
    result = organizer_worker.run(
        {
            "payload": {"schema": ORGANIZER_VERSION, "issues": [issue("a")]},
            "timeout": 20,
            "allow_load": False,
        }
    )
    assert result["ok"] and result["metadata"]["usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 9,
    }


def test_worker_rpc_no_config_is_bounded_and_private(tmp_path, private_runtime):
    with pytest.raises(ValueError, match="unavailable"):
        localdev_host.organize_worker(
            {"schema": ORGANIZER_VERSION, "issues": [issue("a", prompt="PRIVATE_REQUEST")]},
            threading.Event(),
            3,
        )


def test_worker_rpc_cancel_terminates_only_owned_child(monkeypatch, private_runtime):
    real_popen = subprocess.Popen
    children = []

    def child(argv, **kwargs):
        assert "PRIVATE_REQUEST" not in str(argv)
        assert argv[1:4] == ["-I", "-m", "localdev_mlx.flow.organizer_worker"]
        process = real_popen(
            [sys.executable, "-c", "import sys,time;sys.stdin.read();time.sleep(30)"], **kwargs
        )
        children.append(process)
        return process

    monkeypatch.setattr(localdev_host.subprocess, "Popen", child)
    event = threading.Event()
    timer = threading.Timer(0.2, event.set)
    timer.start()
    start = time.monotonic()
    with pytest.raises(ValueError, match="cancelled"):
        localdev_host.organize_worker(
            {"schema": ORGANIZER_VERSION, "issues": [issue("a", prompt="PRIVATE_REQUEST")]},
            event,
            3,
        )
    timer.join()
    assert time.monotonic() - start < 2
    assert children[0].poll() is not None


def test_evidence_changes_invalidate_cache_and_export_is_frozen(tmp_path):
    entered, finish = threading.Event(), threading.Event()
    calls = []

    def organize(payload, *_):
        calls.append(payload)
        entered.set()
        finish.wait(3)
        return advice(payload["issues"])

    flow = Flow(
        Config(tmp_path, preview_kind="command"),
        host=HostServices(
            "fixture",
            lambda: {"state": "ready", "identity": "model", "can_organize": True},
            organize,
        ),
    )
    try:
        capture = flow.add_selection({"text": "One"})
        flow.save_issues([issue("a", selections=[capture["id"]])])
        flow.exports.start()
        assert entered.wait(2)
        flow.attach(capture["id"], "new.log", base64.b64encode(b"New facts after freeze").decode())
        finish.set()
        flow.exports.thread.join(3)
        first = json.loads((Path(flow.exports.view()["directory"]) / "manifest.json").read_text())
        assert not any(
            name.startswith("attachment")
            for name in first["selections"][capture["id"]]["artifacts"]
        )
        _, second, _ = export(flow)
        assert len(calls) == 2
        assert calls[0]["evidence_digest"] != calls[1]["evidence_digest"]
        assert any(
            name.startswith("attachment")
            for name in second["selections"][capture["id"]]["artifacts"]
        )
        export(flow)
        assert len(calls) == 2
    finally:
        finish.set()
        flow.close()


def test_crashed_controller_preview_cleanup_uses_journal(tmp_path, private_runtime):
    import signal

    from localdev_mlx.models.manager import ModelManager

    (tmp_path / "src").mkdir()
    (tmp_path / "src/preview.py").write_text("import time\ntime.sleep(300)\n")
    (tmp_path / "project-flow.toml").write_text(
        '[project]\ninclude=["src"]\npreview_kind="command"\npreview=[["{python}","src/preview.py"]]\n'
    )
    info = controller.start(tmp_path, open_browser=False)
    saved = controller.read_json(controller.runtime(tmp_path) / "controller.json")
    try:
        with httpx.Client(base_url=info["url"], trust_env=False) as client:
            client.post(
                "/api/open-test-app", headers={"X-Flow-Token": saved["token"]}, json={}
            ).raise_for_status()
            end = time.monotonic() + 5
            children = []
            while time.monotonic() < end:
                children = controller.read_json(controller.runtime(tmp_path) / "children.json").get(
                    "processes", []
                )
                if children:
                    break
                time.sleep(0.05)
        assert children
        os.kill(saved["pid"], signal.SIGKILL)  # This test's exact owned controller only.
        end = time.monotonic() + 3
        while (
            ModelManager._fingerprint(saved["pid"]) == saved["fingerprint"]
            and time.monotonic() < end
        ):
            time.sleep(0.05)
        restarted = controller.start(tmp_path, open_browser=False)
        assert restarted["pid"] != saved["pid"]
        assert all(ModelManager._fingerprint(row["pid"]) != row["fingerprint"] for row in children)
    finally:
        controller.stop(tmp_path)


@pytest.mark.parametrize("mode", ["timeout", "malformed", "oversized", "stderr"])
def test_worker_rpc_failure_budgets(monkeypatch, private_runtime, mode):
    real_popen = subprocess.Popen
    processes = []
    scripts = {
        "timeout": "import sys,time;sys.stdin.read();time.sleep(30)",
        "malformed": "import sys;sys.stdin.read();print('not json')",
        "oversized": "import sys;sys.stdin.read();print('x'*150000)",
        "stderr": "import sys;sys.stdin.read();sys.stderr.write('x'*150000)",
    }

    def spawn(argv, **kwargs):
        process = real_popen([sys.executable, "-c", scripts[mode]], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(localdev_host.subprocess, "Popen", spawn)
    with pytest.raises(ValueError):
        localdev_host.organize_worker(
            {"schema": ORGANIZER_VERSION, "issues": [issue("a")]}, threading.Event(), 0.5
        )
    assert processes[0].poll() is not None


@pytest.mark.parametrize(
    "consent,resident,keep",
    [(False, False, False), (True, False, False), (True, False, True), (True, True, False)],
)
def test_cold_load_consent_and_stop_ownership(
    global_config, monkeypatch, tmp_path, consent, resident, keep
):
    config = replace(global_config, stop_after_run=not keep)
    monkeypatch.setattr(organizer_worker, "load_global_config", lambda: config)
    monkeypatch.setattr(organizer_worker, "selected_model", lambda *_: (config.worker, resident))
    monkeypatch.setattr(organizer_worker, "model_context", lambda *_: 32768)
    calls, state = [], {}

    class Manager:
        @contextmanager
        def lease(self):
            yield

        def _read_state(self):
            return state

        def _owned(self, _):
            return True

        def _port_open(self, *_):
            return False

        def ensure(self, profile):
            assert os.environ["HF_HUB_OFFLINE"] == "1"
            assert profile.startup_timeout_seconds <= 20
            calls.append("load")
            state.update(owner_pid=os.getpid())

        def stop(self):
            calls.append("stop")

    monkeypatch.setattr(organizer_worker, "OrganizerManager", Manager)
    monkeypatch.setattr(
        organizer_worker.MLXOpenAIProvider,
        "complete_structured",
        lambda *a, **k: organizer_worker.OrganizerResponse.model_validate(advice([issue("a")])),
    )
    request = {
        "payload": {"schema": ORGANIZER_VERSION, "issues": [issue("a")]},
        "timeout": 20,
        "allow_load": consent,
        "token": "owned-test",
    }
    if not consent and not resident:
        with pytest.raises(ValueError, match="not loaded"):
            organizer_worker.run(request)
        assert not calls
    else:
        assert organizer_worker.run(request)["ok"]
        assert calls == ([] if resident else ["load"] if keep else ["load", "stop"])


def test_context_rejects_oversize_and_unknown_without_inference(
    tmp_path, global_config, monkeypatch
):
    model = tmp_path / "model"
    model.mkdir()
    profile = replace(global_config.worker, model=str(model))
    with pytest.raises(ValueError, match="not known"):
        organizer_worker.model_context(profile)
    (model / "config.json").write_text('{"text_config":{"max_position_embeddings":32768}}')
    assert organizer_worker.model_context(profile) == 32768
    limited = replace(profile, server_args=("--max-kv-size", "1024"))
    assert organizer_worker.model_context(limited) == 1024


def test_zip_case_collisions_are_rejected_on_all_platforms(tmp_path):
    path = tmp_path / "case.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("evidence/FILE.txt", b"one")
        archive.writestr("evidence/file.txt", b"two")
    with pytest.raises(ValueError, match="Duplicate"):
        Bundle(path)


def test_ready_export_survives_link_persistence_failure(tmp_path, monkeypatch):
    flow = manual(tmp_path, exported=lambda directory: {"state": "linked"})
    flow.save_issues([issue("a")])

    def failed():
        raise OSError("Read-only runtime")

    monkeypatch.setattr(flow.exports, "persist", failed)
    try:
        job, manifest, _ = export(flow, organize=False)
        assert job["stage"] == "ready" and len(manifest["issues"]) == 1
        assert job["session_link"]["retryable"]
    finally:
        flow.close()


def test_flow_project_option_before_subcommand_and_copied_non_git_config(tmp_path):
    (tmp_path / ".localdev").mkdir()
    (tmp_path / ".localdev/config.toml").write_text("schema_version = 2\n")
    runner = CliRunner()
    result = runner.invoke(app, ["flow", "--project", str(tmp_path), "doctor"])
    assert result.exit_code == 0 and str(tmp_path) in result.output
    shell = runner.invoke(app, ["--repo", str(tmp_path)], input="flow doctor\n")
    assert shell.exit_code == 0 and "Capture-only shell" in shell.output


def test_cached_advice_does_not_duplicate_session_requests(sample_repo):
    flow = Flow(
        Config(sample_repo, preview_kind="command"),
        host=HostServices(
            "fixture",
            lambda: {"state": "ready", "identity": "model", "can_organize": True},
            lambda payload, *_: localdev_host.OrganizedAdvice(
                advice(payload["issues"]),
                {"identity": "model", "duration_seconds": 1, "usage": {"prompt_tokens": 10}},
            ),
            exported=lambda path: link_export(sample_repo, path),
        ),
    )
    try:
        flow.save_issues([issue("a")])
        first, _, _ = export(flow)
        cached, _, _ = export(flow)
        assert cached["organization"]["cached"]
        assert first["session_link"]["digest"] == cached["session_link"]["digest"]
        assert len(SessionStore(sample_repo).current()["entries"]) == 1
    finally:
        flow.close()


def test_live_unverified_controller_is_reported_truthfully(tmp_path, private_runtime, monkeypatch):
    monkeypatch.setattr(ModelManager, "_fingerprint", staticmethod(lambda pid: "same-process"))
    (controller.runtime(tmp_path) / "controller.json").write_text(
        json.dumps({"pid": 1234, "fingerprint": "same-process", "version": "different"})
    )
    status = controller.status(tmp_path)
    assert status["state"] == "unverified" and status["url"] is None
    with pytest.raises(ValueError, match="no process was signalled"):
        controller.stop(tmp_path)
