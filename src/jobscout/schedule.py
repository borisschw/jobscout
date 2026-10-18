"""Daily check while the report viewer stays open."""

from __future__ import annotations

import asyncio
import threading
import traceback
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from jobscout.config import parse_hhmm
from jobscout.runner import RunOptions, run_pipeline

if TYPE_CHECKING:
    from jobscout.config import AppConfig
    from jobscout.models import RunSummary
    from jobscout.store import Store

# Re-check the wall clock often enough that a laptop waking from sleep
# notices a missed 23:00 within half a minute. Event.wait follows a clock
# that pauses during suspend, so one long sleep would miss the slot.
WAKE_SECONDS = 30.0

Runner = Callable[["AppConfig", "Store"], object]


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("schedule times must be timezone-aware")
    return now


def _on_clock(now: datetime, at: str) -> datetime:
    hour, minute = parse_hhmm(at)
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def most_recent_slot(now: datetime, at: str) -> datetime:
    """The latest scheduled instant at or before ``now``."""
    now = _aware(now)
    slot = _on_clock(now, at)
    if slot > now:
        slot -= timedelta(days=1)
    return slot


def next_run_at(now: datetime, at: str) -> datetime:
    """The next scheduled instant strictly after ``now``."""
    now = _aware(now)
    slot = _on_clock(now, at)
    if slot <= now:
        slot += timedelta(days=1)
    return slot


def next_check_label(now: datetime, at: str) -> str:
    nxt = next_run_at(now, at)
    if nxt.date() == now.date():
        when = "today"
    elif nxt.date() == (now + timedelta(days=1)).date():
        when = "tomorrow"
    else:
        when = nxt.date().isoformat()
    return f"Next check {when} at {at}"


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def is_due(now: datetime, at: str, last_finished: datetime | None) -> bool:
    """Whether the latest slot still needs a run.

    A first start before today's slot waits. After today's slot, or when a
    previous run finished before the latest slot, one catch-up is due.
    Older missed days are not replayed; the latest slot is the only one.
    """
    now = _aware(now)
    slot = most_recent_slot(now, at)
    if last_finished is None:
        return slot.date() == now.date()
    return last_finished.astimezone(slot.tzinfo) < slot


def _default_runner(config: AppConfig, store: Store) -> RunSummary:
    return asyncio.run(run_pipeline(config, store, RunOptions()))


class DailyScheduler:
    """Run the pipeline once a day from the viewer process."""

    def __init__(
        self,
        config: AppConfig,
        store: Store,
        *,
        clock: Callable[[], datetime] | None = None,
        runner: Runner | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._runner = runner or _default_runner
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._last_error: str | None = None
        self._attempted_slot: datetime | None = None

    @property
    def at(self) -> str:
        return self.config.settings.schedule.at

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def due(self) -> bool:
        if not self._slot_needs_run():
            return False
        return self._attempted_slot != most_recent_slot(self._clock(), self.at)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="jobscout-schedule",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def status(self) -> dict:
        now = self._clock()
        latest = self.store.latest_run()
        started = latest["started_at"] if latest else None
        finished = latest["finished_at"] if latest else None
        nxt = next_run_at(now, self.at)
        with self._lock:
            running = self._running
            error = self._last_error
        return {
            "enabled": True,
            "running": running,
            "at": self.at,
            "last_started_at": started,
            "last_finished_at": finished,
            "next_run_at": nxt.isoformat(timespec="seconds"),
            "next_label": next_check_label(now, self.at),
            "error": error,
        }

    def wait_seconds(self) -> float:
        now = self._clock()
        delay = (next_run_at(now, self.at) - now).total_seconds()
        return min(WAKE_SECONDS, max(0.0, delay))

    def tick(self) -> None:
        """Run the pipeline when the current slot has not been attempted."""
        if not self.due():
            return
        slot = most_recent_slot(self._clock(), self.at)
        self._attempted_slot = slot
        with self._lock:
            self._running = True
            self._last_error = None
        print(f"Scheduled check started ({self.at})", flush=True)
        try:
            summary = self._runner(self.config, self.store)
            fetched = getattr(summary, "fetched", None)
            new = getattr(summary, "new", None)
            scored = getattr(summary, "scored", None)
            if fetched is not None:
                print(
                    f"Scheduled check finished: fetched {fetched}, new {new}, scored {scored}",
                    flush=True,
                )
            else:
                print("Scheduled check finished", flush=True)
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
            print("Scheduled check failed:", flush=True)
            traceback.print_exc()
        finally:
            with self._lock:
                self._running = False

    def _slot_needs_run(self) -> bool:
        latest = self.store.latest_run()
        finished = parse_timestamp(latest["finished_at"]) if latest else None
        return is_due(self._clock(), self.at, finished)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            if self._stop.is_set():
                return
            if self._stop.wait(self.wait_seconds()):
                return
