"""Tests for the daily viewer schedule."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from jobscout.schedule import (
    WAKE_SECONDS,
    DailyScheduler,
    is_due,
    most_recent_slot,
    next_check_label,
    next_run_at,
)

IL = ZoneInfo("Asia/Jerusalem")


def _at(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
    second: int = 0,
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=IL)


def _z(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _save_finished(store, finished: datetime) -> None:
    started = finished - timedelta(minutes=5)
    store.save_run(
        started_at=_z(started),
        finished_at=_z(finished),
        fetched=1,
        new=0,
        changed=0,
        closed=0,
        scored=0,
        llm_calls=0,
    )
    store._conn.commit()


def test_next_run_is_later_today_or_tomorrow():
    assert next_run_at(_at(2026, 1, 15, 15, 0), "23:00") == _at(2026, 1, 15, 23, 0)
    assert next_run_at(_at(2026, 1, 15, 23, 0), "23:00") == _at(2026, 1, 16, 23, 0)
    assert next_check_label(_at(2026, 1, 15, 15, 0), "23:00") == "Next check today at 23:00"
    assert next_check_label(_at(2026, 1, 15, 23, 30), "23:00") == "Next check tomorrow at 23:00"


def test_most_recent_slot_rolls_back_before_the_clock_time():
    assert most_recent_slot(_at(2026, 1, 15, 9, 0), "23:00") == _at(2026, 1, 14, 23, 0)
    assert most_recent_slot(_at(2026, 1, 15, 23, 0), "23:00") == _at(2026, 1, 15, 23, 0)


def test_first_start_waits_until_todays_slot():
    assert is_due(_at(2026, 1, 15, 15, 0), "23:00", None) is False
    assert is_due(_at(2026, 1, 15, 23, 10), "23:00", None) is True


def test_morning_after_a_missed_night_is_due_once():
    missed = _at(2026, 1, 14, 22, 0)
    done = _at(2026, 1, 14, 23, 20)
    morning = _at(2026, 1, 15, 9, 0)
    assert is_due(morning, "23:00", missed) is True
    assert is_due(morning, "23:00", done) is False


def test_two_missed_nights_are_still_one_slot():
    saturday = _at(2026, 1, 10, 23, 30)
    tuesday_morning = _at(2026, 1, 13, 9, 0)
    assert is_due(tuesday_morning, "23:00", saturday) is True
    assert most_recent_slot(tuesday_morning, "23:00") == _at(2026, 1, 12, 23, 0)


def test_naive_clock_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        next_run_at(datetime(2026, 1, 15, 15, 0), "23:00")


def test_wait_wakes_often_enough_to_notice_a_resume(app_config, store):
    morning = _at(2026, 1, 15, 9, 0)
    sched = DailyScheduler(app_config, store, clock=lambda: morning, runner=lambda *_: None)
    assert sched.wait_seconds() == WAKE_SECONDS

    almost = _at(2026, 1, 15, 22, 59, 50)
    soon = DailyScheduler(app_config, store, clock=lambda: almost, runner=lambda *_: None)
    assert soon.wait_seconds() == pytest.approx(10)


def test_tick_runs_a_missed_slot_once(app_config, store):
    runs: list[str] = []

    def runner(_config, _store):
        runs.append("run")
        raise RuntimeError("llm down")

    evening = _at(2026, 1, 15, 23, 30)
    sched = DailyScheduler(app_config, store, clock=lambda: evening, runner=runner)
    sched.tick()
    sched.tick()
    assert runs == ["run"]
    assert sched.status()["error"] == "llm down"
    assert sched.running is False


def test_tick_skips_the_morning_when_last_night_finished(app_config, store):
    runs: list[str] = []
    _save_finished(store, _at(2026, 1, 14, 23, 20))
    sched = DailyScheduler(
        app_config,
        store,
        clock=lambda: _at(2026, 1, 15, 9, 0),
        runner=lambda *_: runs.append("run"),
    )
    sched.tick()
    assert runs == []


def test_tick_catches_up_a_missed_night(app_config, store):
    runs: list[str] = []
    _save_finished(store, _at(2026, 1, 14, 22, 0))
    sched = DailyScheduler(
        app_config,
        store,
        clock=lambda: _at(2026, 1, 15, 9, 0),
        runner=lambda *_: runs.append("run"),
    )
    sched.tick()
    assert runs == ["run"]


def test_stop_ends_the_loop_without_a_run(app_config, store):
    runs: list[str] = []
    sched = DailyScheduler(
        app_config,
        store,
        clock=lambda: _at(2026, 1, 15, 9, 0),
        runner=lambda *_: runs.append("run"),
    )
    sched.start()
    sched.stop()
    assert runs == []
    assert sched._thread is not None
    assert not sched._thread.is_alive()
