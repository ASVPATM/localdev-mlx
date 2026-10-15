"""Private bounded RPC entry point. All signal-based deadlines run on this main thread."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from localdev_mlx.config import load_global_config
from localdev_mlx.execution.budget import deadline
from localdev_mlx.models.manager import ModelManager
from localdev_mlx.providers.mlx_openai import MLXOpenAIProvider

from .host import ORGANIZER_PROMPT, ORGANIZER_VERSION
from .localdev_host import identity, selected_model
from .organization import validate_advice


class IssueAdvice(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=120)
    group: str = Field(min_length=1, max_length=80)
    depends_on: list[str] = Field(max_length=60)
    effort: Literal["small", "medium", "large", "unknown"]

    @field_validator("title", "group")
    @classmethod
    def label(cls, value):
        if not value.strip() or len(value.splitlines()) != 1 or any(ord(c) < 32 for c in value):
            raise ValueError("Labels must be nonblank and single-line")
        return value


class OrganizerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    issues: list[IssueAdvice] = Field(min_length=1, max_length=60)


class OrganizerManager(ModelManager):
    token = None
    stop_after_run = True

    def _write_state(self, state):
        super()._write_state(
            {**state, "flow_worker_token": self.token, "flow_stop_after_run": self.stop_after_run}
        )


def model_context(profile):
    """Read only existing local configuration; never contact the model hub."""
    directory = Path(profile.model).expanduser()
    if not directory.is_dir():
        cache = Path(
            os.environ.get(
                "HF_HUB_CACHE",
                str(
                    Path(os.environ.get("HF_HOME", str(Path.home() / ".cache/huggingface"))) / "hub"
                ),
            )
        )
        repo = cache / ("models--" + profile.model.replace("/", "--"))
        ref = repo / "refs/main"
        if not ref.is_file() or ref.stat().st_size > 200:
            raise ValueError(
                "Model context is not known locally; preload/configure the model first"
            )
        revision = ref.read_text().strip()
        if not revision or any(c not in "0123456789abcdef" for c in revision):
            raise ValueError("Invalid cached model revision")
        directory = repo / "snapshots" / revision
    path = directory / "config.json"
    if not path.is_file() or path.stat().st_size > 128000:
        raise ValueError("Model context is not known locally; ordinary export used")
    config = json.loads(path.read_text())
    values = [
        config.get("max_position_embeddings"),
        config.get("text_config", {}).get("max_position_embeddings"),
    ]
    limits = [v for v in values if isinstance(v, int) and v > 0]
    for i, arg in enumerate(profile.server_args):
        if arg == "--max-kv-size" and i + 1 < len(profile.server_args):
            limits.append(int(profile.server_args[i + 1]))
        elif arg.startswith("--max-kv-size="):
            limits.append(int(arg.split("=", 1)[1]))
    if not limits:
        raise ValueError("Model context limit is unknown; ordinary export used")
    return min(limits)


def run(request):
    payload = request["payload"]
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    if len(encoded) > 24000 or payload.get("schema") != ORGANIZER_VERSION:
        raise ValueError("Invalid or oversized organizer input")
    seconds = float(request["timeout"])
    if not 0 < seconds <= 120:
        raise ValueError("Invalid organizer deadline")
    started = time.monotonic()
    manager = OrganizerManager()
    config = load_global_config()
    manager.token = request.get("token")
    manager.stop_after_run = config.stop_after_run
    provider = MLXOpenAIProvider(max_response_bytes=128 * 1024)
    loaded = False
    with manager.lease():
        try:
            with deadline(seconds, label="Flow organizer"):
                profile, resident = selected_model(manager, config)
                model_identity = identity(profile)
                # Bytes are a conservative upper bound, not an asserted tokenizer count.
                budget = (
                    len(
                        (
                            encoded
                            + ORGANIZER_PROMPT
                            + json.dumps(OrganizerResponse.model_json_schema())
                        ).encode()
                    )
                    + 1800
                    + 512
                )
                if budget > model_context(profile):
                    raise ValueError("Batch cannot safely fit the configured model context")
                if not resident:
                    if request.get("allow_load") is not True:
                        raise ValueError("Helper is not loaded; ordinary export used")
                    existing = manager._read_state()
                    if existing and manager._owned(existing):
                        raise ValueError(
                            "Another resident model is preserved; preload the helper explicitly"
                        )
                    if manager._port_open(profile.host, profile.port):
                        raise ValueError(
                            "Unverified model server is preserved; ordinary export used"
                        )
                    loaded = True
                    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
                    manager.ensure(
                        replace(
                            profile,
                            startup_timeout_seconds=max(
                                0.1, seconds - (time.monotonic() - started)
                            ),
                        )
                    )
                remaining = seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise ValueError("Helper readiness exceeded the export budget")
                bounded = replace(
                    profile,
                    enable_thinking=False,
                    thinking_budget=0,
                    max_tokens=min(1800, profile.max_tokens),
                    request_timeout_seconds=remaining,
                )
                advice = provider.complete_structured(
                    profile=bounded,
                    system_prompt=ORGANIZER_PROMPT,
                    user_prompt=encoded,
                    response_model=OrganizerResponse,
                    schema_name="flow_organizer",
                ).model_dump()
                validate_advice(advice, payload["issues"])
                usage = provider.last_metadata.get("usage", {})
                return {
                    "ok": True,
                    "advice": advice,
                    "metadata": {
                        "identity": model_identity,
                        "duration_seconds": round(time.monotonic() - started, 3),
                        "input_characters": len(encoded),
                        "loaded_by_helper": loaded,
                        "usage": {
                            key: value
                            for key, value in usage.items()
                            if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                            and type(value) is int
                            and value >= 0
                        },
                    },
                }
        finally:
            if loaded and config.stop_after_run:
                state = manager._read_state()
                if state and state.get("owner_pid") == os.getpid() and manager._owned(state):
                    manager.stop()


def main():
    def cancel(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, cancel)
    try:
        raw = sys.stdin.buffer.read(100001)
        if len(raw) > 100000:
            raise ValueError("Organizer input too large")
        result = run(json.loads(raw))
    except BaseException as exc:
        # Provider exceptions can contain original issues or raw model output. Never echo them.
        result = {
            "ok": False,
            "error": "Local organization unavailable ("
            + type(exc).__name__
            + "); ordinary handoff used",
        }
    data = json.dumps(result).encode()
    if len(data) > 128 * 1024:
        data = b'{"ok":false,"error":"Local response exceeds 128 KB"}'
    sys.stdout.buffer.write(data)


if __name__ == "__main__":
    main()
