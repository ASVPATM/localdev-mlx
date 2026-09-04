from __future__ import annotations

import signal
import threading
import time
from contextlib import contextmanager
from typing import Callable

_guards: list[tuple[float, str, float, Callable[[], bool] | None]] = []


class DeadlineExceeded(TimeoutError):
    pass


@contextmanager
def deadline(seconds: float, *, label: str, cancelled: Callable[[], bool] | None = None):
    """Interrupt synchronous I/O on the CLI thread, honoring nested deadlines."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Bounded execution must run on the main thread")
    started = time.monotonic()
    old_handler = signal.getsignal(signal.SIGALRM)
    old_delay, old_interval = signal.getitimer(signal.ITIMER_REAL)
    outermost = not _guards
    guard = (started + seconds, label, seconds, cancelled)
    _guards.append(guard)

    def alarm(signum, frame):
        now = time.monotonic()
        for end, name, budget, check in _guards:
            if check and check():
                signal.setitimer(signal.ITIMER_REAL, 0)
                raise KeyboardInterrupt("Cancellation requested")
            if now >= end:
                signal.setitimer(signal.ITIMER_REAL, 0)
                raise DeadlineExceeded(f"{name} exceeded {budget:g}s deadline")

    if outermost:
        signal.signal(signal.SIGALRM, alarm)
        signal.setitimer(signal.ITIMER_REAL, 0.05, 0.05)
    try:
        yield
    finally:
        _guards.remove(guard)
        if _guards:
            signal.setitimer(signal.ITIMER_REAL, 0.05, 0.05)
        if outermost:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old_handler)
            if old_delay:
                signal.setitimer(
                    signal.ITIMER_REAL,
                    max(0.001, old_delay - (time.monotonic() - started)),
                    old_interval,
                )
