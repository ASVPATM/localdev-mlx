from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from localdev_mlx.config import STATE_ROOT, ModelProfile


class ModelManagerError(RuntimeError):
    """Raised when a managed MLX server cannot be started, stopped, or identified."""


class ModelManager:
    def __init__(self) -> None:
        self.root = STATE_ROOT / "model-server"
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "state.json"
        self.log_path = self.root / "mlx-server.log"

    def _read_state(self) -> dict[str, Any] | None:
        if not self.state_path.exists():
            return None
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _write_state(self, state: dict[str, Any]) -> None:
        temp = self.state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        temp.replace(self.state_path)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def _port_open(host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            return False

    @staticmethod
    def _served_models(profile: ModelProfile) -> list[str]:
        try:
            response = httpx.get(
                f"http://{profile.host}:{profile.port}/v1/models",
                timeout=5,
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError):
            return []
        values = body.get("data", []) if isinstance(body, dict) else []
        return [
            str(item.get("id"))
            for item in values
            if isinstance(item, dict) and item.get("id")
        ]

    def status(self, profile: ModelProfile | None = None) -> dict[str, Any]:
        state = self._read_state()
        result: dict[str, Any] = {
            "managed": False,
            "running": False,
            "profile": None,
            "pid": None,
            "log": str(self.log_path),
        }
        if state:
            pid = int(state.get("pid", 0))
            alive = pid > 0 and self._pid_alive(pid)
            result.update(
                {
                    "managed": alive,
                    "running": alive,
                    "profile": state.get("profile"),
                    "pid": pid,
                    "model": state.get("model"),
                    "started_at": state.get("started_at"),
                }
            )
            if not alive:
                self.state_path.unlink(missing_ok=True)
                state = None
        if profile and self._port_open(profile.host, profile.port):
            served = self._served_models(profile)
            result["running"] = True
            result["served_models"] = served
            if result["managed"]:
                # /v1/models may list every cached model, so managed state is the
                # authoritative record of which profile LocalDev launched.
                result["matches_requested"] = result.get("model") == profile.model
            else:
                result["matches_requested"] = profile.model in served
                result["external_server"] = True
        elif profile:
            result["matches_requested"] = False
        return result

    def ensure(self, profile: ModelProfile) -> None:
        state = self._read_state()
        if state and self._pid_alive(int(state.get("pid", 0))):
            if (
                state.get("model") == profile.model
                and str(state.get("host")) == profile.host
                and int(state.get("port", 0)) == profile.port
                and self._port_open(profile.host, profile.port)
            ):
                return
            self.stop()
            self.start(profile)
            return

        if self._port_open(profile.host, profile.port):
            served = self._served_models(profile)
            if profile.model in served:
                # Respect an externally managed MLX server. The request's model
                # field selects the configured model when the server supports it.
                return
            raise ModelManagerError(
                f"A non-managed process is listening on {profile.host}:{profile.port} "
                f"but does not advertise {profile.model!r}. Stop it or change the LocalDev port."
            )

        self.start(profile)

    def start(self, profile: ModelProfile) -> None:
        if not profile.executable.exists():
            raise ModelManagerError(f"mlx_vlm.server executable not found: {profile.executable}")
        status = self.status(profile)
        if status.get("matches_requested"):
            return
        if status.get("running"):
            if status.get("managed"):
                self.stop()
            else:
                raise ModelManagerError(
                    f"A non-managed process is already listening on {profile.host}:{profile.port}."
                )

        command = [
            str(profile.executable),
            "--model",
            profile.model,
            "--host",
            profile.host,
            "--port",
            str(profile.port),
        ]
        if profile.enable_thinking:
            command.extend(["--enable-thinking", "--thinking-budget", str(profile.thinking_budget)])
        command.extend(profile.server_args)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write(
                f"\n\n[{datetime.now(UTC).isoformat()}] Starting profile {profile.name}: "
                f"{' '.join(command)}\n"
            )
            log.flush()
            process = subprocess.Popen(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                text=True,
            )
        self._write_state(
            {
                "pid": process.pid,
                "profile": profile.name,
                "model": profile.model,
                "host": profile.host,
                "port": profile.port,
                "command": command,
                "started_at": datetime.now(UTC).isoformat(),
            }
        )

        deadline = time.monotonic() + profile.startup_timeout_seconds
        last_models: list[str] = []
        while time.monotonic() < deadline:
            if process.poll() is not None:
                tail = self.log_tail(80)
                self.state_path.unlink(missing_ok=True)
                raise ModelManagerError(
                    f"MLX server exited with code {process.returncode}. Log tail:\n{tail}"
                )
            last_models = self._served_models(profile)
            if profile.model in last_models:
                return
            time.sleep(2)
        self.stop()
        raise ModelManagerError(
            f"Timed out waiting for {profile.model}. Last models response: {last_models}. "
            f"See {self.log_path}"
        )

    @staticmethod
    def _signal_pid(pid: int, sig: signal.Signals) -> None:
        """Signal only the managed server PID.

        ``mlx_vlm.server`` runs as a single Python server process. Targeting the
        recorded PID is safer than assuming that the PID is also a process-group
        ID, an assumption that can fail on macOS during shutdown.
        """
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            raise ModelManagerError(
                f"Permission denied while sending {sig.name} to managed MLX PID {pid}."
            ) from exc

    def _wait_until_stopped(
        self,
        *,
        pid: int,
        host: str,
        port: int,
        timeout_seconds: float,
    ) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            # A terminated child may briefly remain as a zombie, for which
            # os.kill(pid, 0) still reports success. Once the listening socket
            # is closed, the managed server is no longer serving or retaining
            # the model and cleanup can safely finish.
            if not self._pid_alive(pid) or not self._port_open(host, port):
                return True
            time.sleep(0.25)
        return not self._pid_alive(pid) or not self._port_open(host, port)

    def stop(self) -> None:
        state = self._read_state()
        if not state:
            return

        pid = int(state.get("pid", 0))
        host = str(state.get("host", "127.0.0.1"))
        port = int(state.get("port", 8080))

        if pid <= 0 or not self._pid_alive(pid):
            self.state_path.unlink(missing_ok=True)
            return

        # A stale state file must never cause LocalDev to signal an unrelated
        # process that later reused the recorded PID. If the managed endpoint is
        # already gone, discard the stale state instead.
        if not self._port_open(host, port):
            self.state_path.unlink(missing_ok=True)
            return

        try:
            httpx.post(f"http://{host}:{port}/unload", timeout=10)
        except httpx.HTTPError:
            pass

        self._signal_pid(pid, signal.SIGTERM)
        if self._wait_until_stopped(
            pid=pid,
            host=host,
            port=port,
            timeout_seconds=20,
        ):
            self.state_path.unlink(missing_ok=True)
            return

        self._signal_pid(pid, signal.SIGKILL)
        if self._wait_until_stopped(
            pid=pid,
            host=host,
            port=port,
            timeout_seconds=5,
        ):
            self.state_path.unlink(missing_ok=True)
            return

        raise ModelManagerError(
            f"Managed MLX server PID {pid} is still listening on {host}:{port} after shutdown."
        )

    def log_tail(self, lines: int = 80) -> str:
        if not self.log_path.exists():
            return ""
        content = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(content[-lines:])
