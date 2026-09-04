from __future__ import annotations

import threading

from localdev_mlx.progress import ProgressReporter, format_duration


def test_format_duration() -> None:
    assert format_duration(2.25) == "2.2s"
    assert format_duration(91) == "1m 31s"
    assert format_duration(3661) == "1h 1m 1s"


def test_operation_emits_heartbeat_and_records_timing() -> None:
    messages: list[str] = []
    heartbeat_received = threading.Event()

    def record(message: str) -> None:
        messages.append(message)
        if "WORKING" in message:
            heartbeat_received.set()

    reporter = ProgressReporter(callback=record, heartbeat_seconds=5)
    reporter.heartbeat_seconds = 0.01

    with reporter.operation("TASK-1", "slow operation"):
        assert heartbeat_received.wait(timeout=2.0)

    assert any("START" in message for message in messages)
    assert any("WORKING" in message for message in messages)
    assert any("DONE" in message for message in messages)
    assert reporter.timings_for("TASK-1")["slow operation"] > 0
