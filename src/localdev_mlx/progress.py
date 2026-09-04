from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

ProgressCallback = Callable[[str], None]


def format_duration(seconds: float) -> str:
    """Format elapsed seconds for concise terminal progress messages."""
    value = max(0.0, seconds)
    if value < 60:
        return f"{value:.1f}s"
    minutes, remaining = divmod(value, 60)
    if minutes < 60:
        return f"{int(minutes)}m {remaining:.0f}s"
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours}h {minutes}m {remaining:.0f}s"


@dataclass
class ProgressReporter:
    """Print stage transitions and periodic heartbeats for blocking operations."""

    callback: ProgressCallback | None = None
    heartbeat_seconds: float = 30.0
    _timings: dict[str, dict[str, float]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.heartbeat_seconds = max(5.0, float(self.heartbeat_seconds))

    def emit(self, message: str) -> None:
        if self.callback is not None:
            self.callback(message)

    def _record_timing(self, task_id: str, label: str, elapsed: float) -> None:
        task_timings = self._timings.setdefault(task_id, {})
        key = label
        suffix = 2
        while key in task_timings:
            key = f"{label} #{suffix}"
            suffix += 1
        task_timings[key] = round(elapsed, 3)

    def timings_for(self, task_id: str) -> dict[str, float]:
        return dict(self._timings.get(task_id, {}))

    @contextmanager
    def operation(self, task_id: str, label: str) -> Iterator[None]:
        started = time.monotonic()
        stop = threading.Event()
        self.emit(f"[{task_id}] START — {label}")

        def heartbeat() -> None:
            while not stop.wait(self.heartbeat_seconds):
                elapsed = time.monotonic() - started
                self.emit(
                    f"[{task_id}] WORKING — {label} "
                    f"({format_duration(elapsed)} elapsed)"
                )

        thread = threading.Thread(
            target=heartbeat,
            name=f"localdev-progress-{task_id}",
            daemon=True,
        )
        thread.start()
        succeeded = False
        try:
            yield
            succeeded = True
        finally:
            stop.set()
            thread.join(timeout=1.0)
            elapsed = time.monotonic() - started
            self._record_timing(task_id, label, elapsed)
            outcome = "DONE" if succeeded else "FAILED"
            self.emit(
                f"[{task_id}] {outcome} — {label} "
                f"({format_duration(elapsed)})"
            )
