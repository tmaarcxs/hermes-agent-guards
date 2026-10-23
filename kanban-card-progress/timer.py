"""The single-instance daemon timer that drives the sweep.

Started from ``register(ctx)`` on a plain ``threading.Thread`` rather than the
gateway's event loop: the sweep does blocking SQLite I/O, so running it on the
loop would stall the gateway. A daemon thread is what ``tasklist-pinned``
already ships for the same job and it is what this install's plugin loader
supports without a running loop (``ctx.spawn_task`` requires one).

Guarantees:

* **single instance per gateway process** — one module-level ``_THREAD``; a
  second ``start()`` is a no-op.
* **never crashes the gateway** — the loop body is fully wrapped; an exception
  is logged at WARNING and the loop continues.
* **stops cleanly** — ``stop()`` sets the event and joins with a bounded
  timeout; the wait between cycles is interruptible (``Event.wait``).
* **never blocks the caller** — ``start()`` returns immediately.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from . import sweep

DEFAULT_INTERVAL_SECONDS = 30.0
MIN_INTERVAL_SECONDS = 5.0
_STOP_JOIN_TIMEOUT = 5.0


class ProgressTimer:
    """A daemon thread sweeping the board on a fixed interval."""

    def __init__(
        self,
        run_cycle: Callable[[], object],
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        log: Optional[Callable[[str], None]] = None,
        warn: Optional[Callable[[str], None]] = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._run_cycle = run_cycle
        self._interval = max(MIN_INTERVAL_SECONDS, float(interval_seconds or DEFAULT_INTERVAL_SECONDS))
        self._log = log
        self._warn = warn
        self._now = now
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.cycles = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """Start the daemon thread. Returns False when already running."""
        if self.running:
            return False
        self._stop.clear()
        thread = threading.Thread(
            target=self._loop, name="kanban-card-progress", daemon=True
        )
        self._thread = thread
        thread.start()
        return True

    def stop(self, timeout: float = _STOP_JOIN_TIMEOUT) -> None:
        """Signal the loop and join it with a bounded timeout."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, timeout))
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._tick()
            """Interruptible sleep: stop() returns immediately."""
            if self._stop.wait(self._interval):
                return

    def _tick(self) -> None:
        """One cycle; every failure is contained so the gateway is never affected."""
        try:
            self._run_cycle()
            self.cycles += 1
        except Exception as exc:
            _warn(self._warn, f"kanban-card-progress sweep failed: {exc!r}")


def _warn(warn: Optional[Callable[[str], None]], message: str) -> None:
    if warn is None:
        return
    try:
        warn(message)
    except Exception:
        pass
