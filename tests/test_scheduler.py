"""Tests for src.scheduler - schedule parsing and sync-due evaluation."""

from datetime import datetime, timedelta, timezone

import pytest

from src.scheduler import (
    _is_daily_due,
    _parse_daily_times,
    _parse_timestamp,
    is_table_due,
    is_valid_schedule,
    next_due_at,
    parse_interval_minutes,
)

# Fixed reference time: 2026-03-15 12:00:00 UTC
NOW = datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# parse_interval_minutes
# ---------------------------------------------------------------------------


class TestParseIntervalMinutes:
    """Tests for parse_interval_minutes()."""

    def test_minutes_basic(self) -> None:
        assert parse_interval_minutes("every 15m") == 15

    def test_minutes_single_digit(self) -> None:
        assert parse_interval_minutes("every 5m") == 5

    def test_minutes_large(self) -> None:
        assert parse_interval_minutes("every 120m") == 120

    def test_hours_basic(self) -> None:
        assert parse_interval_minutes("every 2h") == 120

    def test_hours_single(self) -> None:
        assert parse_interval_minutes("every 1h") == 60

    def test_hours_large(self) -> None:
        assert parse_interval_minutes("every 24h") == 1440

    def test_daily_returns_none(self) -> None:
        assert parse_interval_minutes("daily 05:00") is None

    def test_invalid_format_returns_none(self) -> None:
        assert parse_interval_minutes("not a schedule") is None

    def test_empty_string_returns_none(self) -> None:
        assert parse_interval_minutes("") is None

    def test_missing_unit_returns_none(self) -> None:
        assert parse_interval_minutes("every 15") is None

    def test_wrong_unit_returns_none(self) -> None:
        assert parse_interval_minutes("every 15s") is None

    def test_no_space_returns_none(self) -> None:
        assert parse_interval_minutes("every15m") is None

    def test_extra_whitespace_returns_none(self) -> None:
        # Strict parsing: extra whitespace is rejected
        assert parse_interval_minutes("every  15m") is None

    def test_negative_not_matched(self) -> None:
        # Regex uses \d+ so negative sign won't match
        assert parse_interval_minutes("every -5m") is None

    def test_zero_minutes(self) -> None:
        # "every 0m" matches the pattern, returns 0
        assert parse_interval_minutes("every 0m") == 0


# ---------------------------------------------------------------------------
# is_table_due - interval schedules
# ---------------------------------------------------------------------------


class TestIsTableDueInterval:
    """Tests for is_table_due() with interval-based schedules."""

    def test_never_synced_is_due(self) -> None:
        assert is_table_due("every 15m", last_sync_iso=None, now=NOW) is True

    def test_empty_last_sync_is_due(self) -> None:
        assert is_table_due("every 15m", last_sync_iso="", now=NOW) is True

    def test_every_0m_is_always_due(self) -> None:
        # ``every 0m`` opts out of rate limiting — used to force-resync
        # a row whose previous attempt errored without recording
        # last_sync. Even a sync seconds ago must come back as due.
        last_sync = (NOW - timedelta(seconds=5)).isoformat()
        assert is_table_due("every 0m", last_sync_iso=last_sync, now=NOW) is True
        assert is_table_due("every 0m", last_sync_iso=None, now=NOW) is True

    def test_synced_10min_ago_every_15m_not_due(self) -> None:
        last_sync = (NOW - timedelta(minutes=10)).isoformat()
        assert is_table_due("every 15m", last_sync_iso=last_sync, now=NOW) is False

    def test_synced_20min_ago_every_15m_is_due(self) -> None:
        last_sync = (NOW - timedelta(minutes=20)).isoformat()
        assert is_table_due("every 15m", last_sync_iso=last_sync, now=NOW) is True

    def test_synced_exactly_15min_ago_every_15m_is_due(self) -> None:
        last_sync = (NOW - timedelta(minutes=15)).isoformat()
        assert is_table_due("every 15m", last_sync_iso=last_sync, now=NOW) is True

    def test_synced_30min_ago_every_1h_not_due(self) -> None:
        last_sync = (NOW - timedelta(minutes=30)).isoformat()
        assert is_table_due("every 1h", last_sync_iso=last_sync, now=NOW) is False

    def test_synced_90min_ago_every_1h_is_due(self) -> None:
        last_sync = (NOW - timedelta(minutes=90)).isoformat()
        assert is_table_due("every 1h", last_sync_iso=last_sync, now=NOW) is True

    def test_synced_exactly_1h_ago_every_1h_is_due(self) -> None:
        last_sync = (NOW - timedelta(hours=1)).isoformat()
        assert is_table_due("every 1h", last_sync_iso=last_sync, now=NOW) is True

    def test_synced_59min_ago_every_1h_not_due(self) -> None:
        last_sync = (NOW - timedelta(minutes=59)).isoformat()
        assert is_table_due("every 1h", last_sync_iso=last_sync, now=NOW) is False

    def test_synced_3h_ago_every_2h_is_due(self) -> None:
        last_sync = (NOW - timedelta(hours=3)).isoformat()
        assert is_table_due("every 2h", last_sync_iso=last_sync, now=NOW) is True


# ---------------------------------------------------------------------------
# is_table_due - daily schedules
# ---------------------------------------------------------------------------


class TestIsTableDueDaily:
    """Tests for is_table_due() with daily schedules."""

    def test_before_target_time_not_due(self) -> None:
        now = datetime(2026, 3, 15, 4, 30, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 14, 6, 0, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("daily 05:00", last_sync_iso=last_sync, now=now) is False

    def test_past_target_not_synced_today_is_due(self) -> None:
        now = datetime(2026, 3, 15, 5, 30, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 15, 4, 0, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("daily 05:00", last_sync_iso=last_sync, now=now) is True

    def test_past_target_already_synced_after_target_not_due(self) -> None:
        now = datetime(2026, 3, 15, 5, 30, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 15, 5, 15, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("daily 05:00", last_sync_iso=last_sync, now=now) is False

    def test_evening_schedule_past_target_last_sync_yesterday_is_due(self) -> None:
        now = datetime(2026, 3, 15, 18, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 14, 17, 30, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("daily 17:00", last_sync_iso=last_sync, now=now) is True

    def test_daily_never_synced_is_due(self) -> None:
        now = datetime(2026, 3, 15, 6, 0, 0, tzinfo=timezone.utc)
        assert is_table_due("daily 05:00", last_sync_iso=None, now=now) is True

    def test_daily_never_synced_before_target_still_due(self) -> None:
        # Never synced always returns True regardless of target time
        now = datetime(2026, 3, 15, 3, 0, 0, tzinfo=timezone.utc)
        assert is_table_due("daily 05:00", last_sync_iso=None, now=now) is True

    def test_daily_exactly_at_target_time_is_due(self) -> None:
        now = datetime(2026, 3, 15, 5, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 14, 5, 0, 0, tzinfo=timezone.utc).isoformat()
        # now == today_target, so now < today_target is False
        # last_sync (yesterday) < today_target => due
        assert is_table_due("daily 05:00", last_sync_iso=last_sync, now=now) is True

    def test_daily_synced_at_exactly_target_not_due_again(self) -> None:
        now = datetime(2026, 3, 15, 5, 30, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 15, 5, 0, 0, tzinfo=timezone.utc).isoformat()
        # last_sync == today_target => last_sync >= today_target => not due
        assert is_table_due("daily 05:00", last_sync_iso=last_sync, now=now) is False

    def test_midnight_schedule(self) -> None:
        now = datetime(2026, 3, 15, 0, 30, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 14, 0, 15, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("daily 00:00", last_sync_iso=last_sync, now=now) is True

    def test_end_of_day_schedule(self) -> None:
        now = datetime(2026, 3, 15, 23, 59, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 14, 23, 50, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("daily 23:30", last_sync_iso=last_sync, now=now) is True


# ---------------------------------------------------------------------------
# is_table_due - edge cases
# ---------------------------------------------------------------------------


class TestIsTableDueEdgeCases:
    """Edge case tests for is_table_due()."""

    def test_unparseable_last_sync_returns_true(self) -> None:
        # Fail-safe: if we can't parse last_sync, assume sync is needed
        assert is_table_due("every 15m", last_sync_iso="garbage", now=NOW) is True

    def test_unknown_schedule_format_returns_false(self) -> None:
        last_sync = (NOW - timedelta(hours=2)).isoformat()
        assert is_table_due("weekly", last_sync_iso=last_sync, now=NOW) is False

    def test_unknown_schedule_never_synced_returns_true(self) -> None:
        # Never synced takes priority over unknown schedule
        assert is_table_due("weekly", last_sync_iso=None, now=NOW) is True

    def test_now_defaults_to_current_time(self) -> None:
        # When now is not provided, it defaults to current UTC time
        # A table that was never synced should be due regardless
        assert is_table_due("every 15m", last_sync_iso=None) is True

    def test_naive_last_sync_treated_as_utc(self) -> None:
        # Naive timestamp (no timezone) should be treated as UTC
        naive_ts = "2026-03-15T11:50:00"
        # 10 minutes ago from NOW (12:00), with 15m interval -> not due
        assert is_table_due("every 15m", last_sync_iso=naive_ts, now=NOW) is False

    def test_last_sync_in_future_not_due(self) -> None:
        # Edge case: last_sync in the future (clock skew, etc.)
        future = (NOW + timedelta(hours=1)).isoformat()
        assert is_table_due("every 15m", last_sync_iso=future, now=NOW) is False


# ---------------------------------------------------------------------------
# _is_daily_due (internal function, direct tests)
# ---------------------------------------------------------------------------


class TestIsDailyDue:
    """Direct tests for _is_daily_due() internal function."""

    def test_before_target_not_due(self) -> None:
        now = datetime(2026, 3, 15, 4, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 14, 5, 30, 0, tzinfo=timezone.utc)
        assert _is_daily_due(last_sync, now, [(5, 0)]) is False

    def test_after_target_last_sync_before_target_is_due(self) -> None:
        now = datetime(2026, 3, 15, 6, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 15, 4, 0, 0, tzinfo=timezone.utc)
        assert _is_daily_due(last_sync, now, [(5, 0)]) is True

    def test_after_target_last_sync_after_target_not_due(self) -> None:
        now = datetime(2026, 3, 15, 6, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 15, 5, 30, 0, tzinfo=timezone.utc)
        assert _is_daily_due(last_sync, now, [(5, 0)]) is False

    def test_target_with_minutes(self) -> None:
        now = datetime(2026, 3, 15, 17, 45, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
        assert _is_daily_due(last_sync, now, [(17, 30)]) is True

    def test_target_with_minutes_not_yet(self) -> None:
        now = datetime(2026, 3, 15, 17, 15, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
        assert _is_daily_due(last_sync, now, [(17, 30)]) is False


class TestMultipleDailyTimes:
    """Tests for multiple daily schedule times."""

    def test_multi_time_first_due(self) -> None:
        now = datetime(2026, 3, 15, 8, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 14, 19, 0, 0, tzinfo=timezone.utc)
        assert _is_daily_due(last_sync, now, [(7, 0), (13, 0), (18, 0)]) is True

    def test_multi_time_second_due(self) -> None:
        now = datetime(2026, 3, 15, 14, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 15, 7, 30, 0, tzinfo=timezone.utc)
        assert _is_daily_due(last_sync, now, [(7, 0), (13, 0), (18, 0)]) is True

    def test_multi_time_third_due(self) -> None:
        now = datetime(2026, 3, 15, 19, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 15, 13, 30, 0, tzinfo=timezone.utc)
        assert _is_daily_due(last_sync, now, [(7, 0), (13, 0), (18, 0)]) is True

    def test_multi_time_between_slots_not_due(self) -> None:
        now = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 15, 7, 30, 0, tzinfo=timezone.utc)
        assert _is_daily_due(last_sync, now, [(7, 0), (13, 0), (18, 0)]) is False

    def test_multi_time_all_done_not_due(self) -> None:
        now = datetime(2026, 3, 15, 20, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 15, 18, 30, 0, tzinfo=timezone.utc)
        assert _is_daily_due(last_sync, now, [(7, 0), (13, 0), (18, 0)]) is False

    def test_is_table_due_multi_time_format(self) -> None:
        now = datetime(2026, 3, 15, 14, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 15, 7, 30, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("daily 07:00,13:00,18:00", last_sync_iso=last_sync, now=now) is True

    def test_is_table_due_multi_time_not_due(self) -> None:
        now = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 15, 7, 30, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("daily 07:00,13:00,18:00", last_sync_iso=last_sync, now=now) is False


class TestParseDailyTimes:
    """Tests for _parse_daily_times()."""

    def test_single_time(self) -> None:
        assert _parse_daily_times("05:00") == [(5, 0)]

    def test_multiple_times(self) -> None:
        assert _parse_daily_times("07:00,13:00,18:00") == [(7, 0), (13, 0), (18, 0)]

    def test_invalid_format(self) -> None:
        assert _parse_daily_times("7:00") == []

    def test_invalid_hour(self) -> None:
        assert _parse_daily_times("25:00") == []

    def test_invalid_minute(self) -> None:
        assert _parse_daily_times("12:60") == []


# ---------------------------------------------------------------------------
# _parse_timestamp
# ---------------------------------------------------------------------------


class TestParseTimestamp:
    """Tests for _parse_timestamp() internal function."""

    def test_iso_with_timezone(self) -> None:
        result = _parse_timestamp("2026-03-15T12:00:00+00:00")
        assert result is not None
        assert result.year == 2026
        assert result.month == 3
        assert result.day == 15
        assert result.hour == 12

    def test_iso_with_z_suffix(self) -> None:
        # Python 3.11+ fromisoformat handles Z
        result = _parse_timestamp("2026-03-15T12:00:00Z")
        assert result is not None
        assert result.hour == 12

    def test_iso_without_timezone(self) -> None:
        result = _parse_timestamp("2026-03-15T12:00:00")
        assert result is not None
        assert result.hour == 12
        assert result.tzinfo is None

    def test_iso_with_microseconds(self) -> None:
        result = _parse_timestamp("2026-03-15T12:00:00.123456")
        assert result is not None
        assert result.microsecond == 123456

    def test_space_separated(self) -> None:
        result = _parse_timestamp("2026-03-15 12:00:00")
        assert result is not None
        assert result.hour == 12

    def test_invalid_string_returns_none(self) -> None:
        assert _parse_timestamp("not-a-date") is None

    def test_empty_string_returns_none(self) -> None:
        assert _parse_timestamp("") is None

    def test_partial_date_returns_none(self) -> None:
        # "2026-03-15" alone - fromisoformat handles date-only in 3.11+
        result = _parse_timestamp("2026-03-15")
        # Should parse as a date (with hour=0, minute=0)
        assert result is not None
        assert result.hour == 0

    def test_iso_with_positive_offset(self) -> None:
        result = _parse_timestamp("2026-03-15T12:00:00+05:30")
        assert result is not None
        assert result.hour == 12
        assert result.utcoffset() is not None

    def test_iso_with_negative_offset(self) -> None:
        result = _parse_timestamp("2026-03-15T12:00:00-07:00")
        assert result is not None
        assert result.utcoffset() is not None

    def test_numeric_garbage_returns_none(self) -> None:
        assert _parse_timestamp("12345") is None

    def test_none_like_string_returns_none(self) -> None:
        assert _parse_timestamp("None") is None


# ---------------------------------------------------------------------------
# LLM pipeline cadence env vars (#179 review)
#
# Three jobs (session-collector, verification-detector, corporate-memory) and
# the health-check staleness grace must all derive from a single SCHEDULER_*
# env var per job, so an operator changing the cadence in one place moves
# both the schedule string and the grace window. The env var name was already
# read in app/api/health.py before this change but didn't actually drive the
# scheduler — this test pins the wired-up behavior.
# ---------------------------------------------------------------------------


_LLM_PIPELINE_ENV = (
    "SCHEDULER_DATA_REFRESH_INTERVAL",
    "SCHEDULER_HEALTH_CHECK_INTERVAL",
    "SCHEDULER_TICK_SECONDS",
    "SCHEDULER_SCRIPT_RUN_INTERVAL",
    "SCHEDULER_SESSION_COLLECTOR_INTERVAL",
    "SCHEDULER_VERIFICATION_DETECTOR_INTERVAL",
    "SCHEDULER_CORPORATE_MEMORY_INTERVAL",
    "SCHEDULER_USAGE_PRUNE_INTERVAL",
    "SCHEDULER_JIRA_SLA_POLL_INTERVAL",
    "SCHEDULER_JIRA_CONSISTENCY_INTERVAL",
)


def _clear_scheduler_env(monkeypatch) -> None:
    for v in _LLM_PIPELINE_ENV:
        monkeypatch.delenv(v, raising=False)


class TestLLMPipelineCadenceEnvVars:
    """Three new env vars drive both the scheduler and the health grace window."""

    def test_default_cadences_preserve_coprime_offset(self, monkeypatch) -> None:
        """Defaults are 10m / 15m / 17m so the three jobs don't fire on the same tick."""
        _clear_scheduler_env(monkeypatch)
        from services.scheduler.__main__ import build_jobs

        jobs = {name: schedule for name, schedule, *_ in build_jobs()}
        assert jobs["session-collector"] == "every 10m"
        assert jobs["session-processor:verification"] == "every 15m"
        assert jobs["corporate-memory"] == "every 17m"

    def test_session_collector_env_override_changes_cadence(self, monkeypatch) -> None:
        _clear_scheduler_env(monkeypatch)
        monkeypatch.setenv("SCHEDULER_SESSION_COLLECTOR_INTERVAL", "300")  # 5m
        from services.scheduler.__main__ import build_jobs

        jobs = {name: schedule for name, schedule, *_ in build_jobs()}
        assert jobs["session-collector"] == "every 5m"
        # Other LLM jobs must be unaffected.
        assert jobs["session-processor:verification"] == "every 15m"
        assert jobs["corporate-memory"] == "every 17m"

    def test_verification_detector_env_override_changes_cadence(self, monkeypatch) -> None:
        _clear_scheduler_env(monkeypatch)
        monkeypatch.setenv("SCHEDULER_VERIFICATION_DETECTOR_INTERVAL", "600")  # 10m
        from services.scheduler.__main__ import build_jobs

        jobs = {name: schedule for name, schedule, *_ in build_jobs()}
        assert jobs["session-processor:verification"] == "every 10m"
        assert jobs["session-collector"] == "every 10m"
        assert jobs["corporate-memory"] == "every 17m"

    def test_corporate_memory_env_override_changes_cadence(self, monkeypatch) -> None:
        _clear_scheduler_env(monkeypatch)
        monkeypatch.setenv("SCHEDULER_CORPORATE_MEMORY_INTERVAL", "1800")  # 30m
        from services.scheduler.__main__ import build_jobs

        jobs = {name: schedule for name, schedule, *_ in build_jobs()}
        assert jobs["corporate-memory"] == "every 30m"
        assert jobs["session-collector"] == "every 10m"
        assert jobs["session-processor:verification"] == "every 15m"

    @pytest.mark.parametrize(
        "var",
        [
            "SCHEDULER_SESSION_COLLECTOR_INTERVAL",
            "SCHEDULER_VERIFICATION_DETECTOR_INTERVAL",
            "SCHEDULER_CORPORATE_MEMORY_INTERVAL",
        ],
    )
    @pytest.mark.parametrize("bad", ["0", "-5", "abc", ""])
    def test_invalid_llm_env_rejected(self, monkeypatch, var, bad) -> None:
        _clear_scheduler_env(monkeypatch)
        monkeypatch.setenv(var, bad)
        from services.scheduler.__main__ import build_jobs

        with pytest.raises(ValueError):
            build_jobs()


class TestVerificationDetectorGraceFollowsCadence:
    """The health-check grace window is 2x the cadence — same env var drives both."""

    def test_grace_doubles_when_env_overrides_cadence(self, monkeypatch) -> None:
        _clear_scheduler_env(monkeypatch)
        monkeypatch.setenv("SCHEDULER_VERIFICATION_DETECTOR_INTERVAL", "600")  # 10m
        from app.api.health import _verification_detector_grace_seconds
        from services.scheduler.__main__ import build_jobs

        jobs = {name: schedule for name, schedule, *_ in build_jobs()}
        # Cadence and grace MUST be derived from the same env var, so an
        # operator who throttles the detector for any reason (rate-limit,
        # cost, debugging) gets a proportionally wider staleness window
        # automatically — no second knob to forget.
        assert jobs["session-processor:verification"] == "every 10m"
        assert _verification_detector_grace_seconds() == 2 * 600

    def test_grace_uses_default_cadence_when_env_unset(self, monkeypatch) -> None:
        _clear_scheduler_env(monkeypatch)
        from app.api.health import _verification_detector_grace_seconds

        # Default cadence 900s -> grace 1800s.
        assert _verification_detector_grace_seconds() == 2 * 900


# ---------------------------------------------------------------------------
# services/scheduler/__main__._run_job — terminal-state bookkeeping
# ---------------------------------------------------------------------------


class TestRunJobBookkeeping:
    """Per-job worker that advances last_run + clears in_flight on terminal
    state (success OR failure). Pre-fix: last_run only advanced on success,
    causing permanently failing jobs to retry every tick (30s) instead of
    on cadence (15min). PR #232 review fix."""

    def _setup(self):
        import threading

        last_run: dict[str, str | None] = {"verification": None}
        in_flight: set[str] = {"verification"}
        return last_run, in_flight, threading.Lock()

    def test_advances_last_run_on_success(self, monkeypatch):
        from services.scheduler import __main__ as sched

        last_run, in_flight, lock = self._setup()
        monkeypatch.setattr(sched, "_call_api", lambda *a, **kw: True)

        sched._run_job(
            "verification",
            "/api/admin/run-x",
            "POST",
            60,
            "2026-01-01T00:00:00",
            last_run,
            in_flight,
            lock,
        )
        assert last_run["verification"] == "2026-01-01T00:00:00"
        assert "verification" not in in_flight

    def test_advances_last_run_on_failure(self, monkeypatch):
        """Permanently-failing jobs must NOT hot-loop every tick — last_run
        advances even when _call_api returns False."""
        from services.scheduler import __main__ as sched

        last_run, in_flight, lock = self._setup()
        monkeypatch.setattr(sched, "_call_api", lambda *a, **kw: False)

        sched._run_job(
            "verification",
            "/api/admin/run-x",
            "POST",
            60,
            "2026-01-01T00:00:00",
            last_run,
            in_flight,
            lock,
        )
        assert last_run["verification"] == "2026-01-01T00:00:00"
        assert "verification" not in in_flight

    def test_advances_last_run_when_call_raises(self, monkeypatch):
        """`_call_api` catches its own exceptions and returns False, but a
        synchronous bug above it (e.g. KeyError on jobs tuple unpacking)
        could still bubble. The finally block must release in_flight either
        way, otherwise the processor wedges until container restart."""
        from services.scheduler import __main__ as sched

        last_run, in_flight, lock = self._setup()

        def _boom(*a, **kw):
            raise RuntimeError("simulated unhandled scheduler bug")

        monkeypatch.setattr(sched, "_call_api", _boom)

        with pytest.raises(RuntimeError):
            sched._run_job(
                "verification",
                "/api/admin/run-x",
                "POST",
                60,
                "2026-01-01T00:00:00",
                last_run,
                in_flight,
                lock,
            )
        # Even on raise, bookkeeping ran.
        assert last_run["verification"] == "2026-01-01T00:00:00"
        assert "verification" not in in_flight


class TestRunLoopParallelism:
    """The scheduler tick must dispatch jobs in parallel — a 900s verification
    run cannot block the 60s health-check from firing on its own cadence.
    PR #232 review fix replaces the `for-loop + synchronous _call_api` with
    a `ThreadPoolExecutor.submit` per due job."""

    def test_in_flight_skip_prevents_duplicate_launches(self, monkeypatch):
        """When a previous tick's job hasn't returned yet, the next tick
        must NOT submit it again — otherwise a 10-min run during which
        20 ticks fire would queue 20 duplicate POSTs against the same
        processor (the admin endpoint's per-processor lock would 409 most
        of them, but they'd still be wasted requests + audit-log noise)."""
        import threading
        import time as _time
        from services.scheduler import __main__ as sched

        # Single job that takes ~0.3s. Tick is 0.05s. Without in_flight
        # protection we'd see >5 launches per the run loop's tick budget.
        call_count = {"n": 0}
        call_count_lock = threading.Lock()

        def slow_call(*a, **kw):
            with call_count_lock:
                call_count["n"] += 1
            _time.sleep(0.3)
            return True

        monkeypatch.setattr(sched, "_call_api", slow_call)
        # Force a single short-cadence job + short tick.
        monkeypatch.setattr(
            sched,
            "build_jobs",
            lambda: [("test-job", "every 1m", "/api/test", "POST", 60)],
        )
        monkeypatch.setattr(sched, "resolved_tick_seconds", lambda: 0)
        # Always-due so the in_flight check is what gates the second launch.
        monkeypatch.setattr(sched, "is_table_due", lambda *a, **kw: True)

        # Kill the run loop after 0.4s — long enough for ≥5 ticks under
        # the 0s tick budget, short enough that the job (0.3s) hasn't
        # finished its first invocation yet.
        sched._running = True

        def _kill():
            _time.sleep(0.4)
            sched._running = False

        threading.Thread(target=_kill, daemon=True).start()
        sched.run()

        # Without in_flight: ≥5 launches. With: exactly 1 (or maybe 2 if
        # the first one finished mid-tick — both are correct, the bug is
        # ≥5).
        assert call_count["n"] <= 2, f"in_flight protection failed; {call_count['n']} launches"


# ---------------------------------------------------------------------------
# usage-prune scheduler job
# ---------------------------------------------------------------------------


class TestUsagePruneJob:
    """SCHEDULER_USAGE_PRUNE_INTERVAL drives the usage-prune job cadence."""

    def test_usage_prune_job_present_in_defaults(self, monkeypatch) -> None:
        _clear_scheduler_env(monkeypatch)
        from services.scheduler.__main__ import build_jobs

        jobs = {name: (schedule, endpoint) for name, schedule, endpoint, *_ in build_jobs()}
        assert "usage-prune" in jobs, "usage-prune job must be registered in build_jobs()"
        _, endpoint = jobs["usage-prune"]
        assert endpoint == "/api/admin/usage/prune"

    def test_usage_prune_default_cadence_is_daily(self, monkeypatch) -> None:
        _clear_scheduler_env(monkeypatch)
        from services.scheduler.__main__ import build_jobs

        jobs = {name: schedule for name, schedule, *_ in build_jobs()}
        # Default 86400s → every 24h
        assert jobs["usage-prune"] == "every 24h"

    def test_usage_prune_env_override_changes_cadence(self, monkeypatch) -> None:
        _clear_scheduler_env(monkeypatch)
        monkeypatch.setenv("SCHEDULER_USAGE_PRUNE_INTERVAL", "3600")  # 1h
        from services.scheduler.__main__ import build_jobs

        jobs = {name: schedule for name, schedule, *_ in build_jobs()}
        assert jobs["usage-prune"] == "every 1h"
        # Other jobs unaffected
        assert jobs["session-collector"] == "every 10m"

    def test_usage_prune_invalid_env_rejected(self, monkeypatch) -> None:
        _clear_scheduler_env(monkeypatch)
        monkeypatch.setenv("SCHEDULER_USAGE_PRUNE_INTERVAL", "0")
        from services.scheduler.__main__ import build_jobs

        with pytest.raises(ValueError):
            build_jobs()


class TestJiraSelfHealingJobs:
    """SCHEDULER_JIRA_SLA_POLL_INTERVAL + SCHEDULER_JIRA_CONSISTENCY_INTERVAL
    drive the two Jira maintenance jobs that bring Agnes back to parity with
    the legacy Data Broker systemd timers."""

    def test_jira_jobs_present_in_defaults(self, monkeypatch) -> None:
        _clear_scheduler_env(monkeypatch)
        from services.scheduler.__main__ import build_jobs

        jobs = {name: (schedule, endpoint) for name, schedule, endpoint, *_ in build_jobs()}
        assert "jira-sla-poll" in jobs
        assert "jira-consistency-check" in jobs
        assert jobs["jira-sla-poll"][1] == "/api/admin/run-jira-sla-poll"
        assert jobs["jira-consistency-check"][1] == "/api/admin/run-jira-consistency-check"

    def test_jira_default_cadences(self, monkeypatch) -> None:
        """Consistency matches connectors/jira/systemd/jira-consistency.timer.

        The SLA poll intentionally diverges from its 15min systemd unit: a
        serial pass over every open ticket outruns that cadence, and unlike
        systemd this scheduler re-fires an abandoned call onto the still
        running pass. See the DEFAULTS comment in services/scheduler.
        """
        _clear_scheduler_env(monkeypatch)
        from services.scheduler.__main__ import build_jobs

        jobs = {name: schedule for name, schedule, *_ in build_jobs()}
        assert jobs["jira-sla-poll"] == "every 45m"
        assert jobs["jira-consistency-check"] == "every 30m"

    def test_sla_poll_timeout_outlasts_a_pass(self, monkeypatch) -> None:
        """The scheduler must not abandon an SLA pass that is still running.

        _run_job's finally releases in_flight and advances last_run on a
        timeout as readily as on success, and a sync handler is not cancelled
        when its client disconnects. A timeout shorter than a real pass is
        therefore what stacks a second pass onto the unfinished first — the
        wide cadence alone only makes that unlikely, not impossible.
        """
        _clear_scheduler_env(monkeypatch)
        from services.scheduler.__main__ import build_jobs

        timeout = next(j for j in build_jobs() if j[0] == "jira-sla-poll")[4]
        assert timeout == 3600
        # Outlasts the default cadence, so the guard is still held at the tick
        # that would otherwise find the job due again.
        assert timeout > 45 * 60

    def test_jira_sla_env_override_changes_cadence(self, monkeypatch) -> None:
        _clear_scheduler_env(monkeypatch)
        monkeypatch.setenv("SCHEDULER_JIRA_SLA_POLL_INTERVAL", "300")  # 5m
        from services.scheduler.__main__ import build_jobs

        jobs = {name: schedule for name, schedule, *_ in build_jobs()}
        assert jobs["jira-sla-poll"] == "every 5m"
        # Other Jira job unaffected.
        assert jobs["jira-consistency-check"] == "every 30m"

    def test_jira_consistency_env_override_changes_cadence(self, monkeypatch) -> None:
        _clear_scheduler_env(monkeypatch)
        monkeypatch.setenv("SCHEDULER_JIRA_CONSISTENCY_INTERVAL", "3600")  # 1h
        from services.scheduler.__main__ import build_jobs

        jobs = {name: schedule for name, schedule, *_ in build_jobs()}
        assert jobs["jira-consistency-check"] == "every 1h"
        assert jobs["jira-sla-poll"] == "every 45m"

    @pytest.mark.parametrize(
        "var",
        [
            "SCHEDULER_JIRA_SLA_POLL_INTERVAL",
            "SCHEDULER_JIRA_CONSISTENCY_INTERVAL",
        ],
    )
    @pytest.mark.parametrize("bad", ["0", "-5", "abc", ""])
    def test_invalid_jira_env_rejected(self, monkeypatch, var, bad) -> None:
        _clear_scheduler_env(monkeypatch)
        monkeypatch.setenv(var, bad)
        from services.scheduler.__main__ import build_jobs

        with pytest.raises(ValueError):
            build_jobs()


# ---------------------------------------------------------------------------
# cron schedules — is_valid_schedule (#608)
# ---------------------------------------------------------------------------


class TestIsValidScheduleCron:
    """is_valid_schedule() accepts well-formed 5-field cron expressions and
    rejects malformed ones so the admin API returns 422 (consistent with the
    `daily 25:00` rejection contract)."""

    def test_simple_monthly_valid(self) -> None:
        # 05:00 UTC on the 7th of every month — the motivating use case.
        assert is_valid_schedule("cron 0 5 7 * *") is True

    def test_minute_out_of_range_invalid(self) -> None:
        assert is_valid_schedule("cron 99 5 7 * *") is False

    def test_weekly_valid(self) -> None:
        # 05:00 UTC every Monday.
        assert is_valid_schedule("cron 0 5 * * 1") is True

    def test_all_wildcards_valid(self) -> None:
        assert is_valid_schedule("cron * * * * *") is True

    def test_comma_list_valid(self) -> None:
        assert is_valid_schedule("cron 30 6 1,15 * *") is True

    def test_range_valid(self) -> None:
        assert is_valid_schedule("cron 0 9-17 * * 1-5") is True

    def test_step_valid(self) -> None:
        assert is_valid_schedule("cron */15 * * * *") is True

    def test_step_on_range_valid(self) -> None:
        assert is_valid_schedule("cron 0 0-12/2 * * *") is True

    def test_hour_out_of_range_invalid(self) -> None:
        assert is_valid_schedule("cron 0 24 * * *") is False

    def test_dom_zero_invalid(self) -> None:
        # day-of-month is 1-31; 0 is out of range.
        assert is_valid_schedule("cron 0 5 0 * *") is False

    def test_month_out_of_range_invalid(self) -> None:
        assert is_valid_schedule("cron 0 5 1 13 *") is False

    def test_dow_out_of_range_invalid(self) -> None:
        # day-of-week is 0-6; 7 is out of range in this matcher.
        assert is_valid_schedule("cron 0 5 * * 7") is False

    def test_too_few_fields_invalid(self) -> None:
        assert is_valid_schedule("cron 0 5 7 *") is False

    def test_too_many_fields_invalid(self) -> None:
        assert is_valid_schedule("cron 0 5 7 * * *") is False

    def test_garbage_field_invalid(self) -> None:
        assert is_valid_schedule("cron 0 5 abc * *") is False

    def test_reversed_range_invalid(self) -> None:
        assert is_valid_schedule("cron 0 17-9 * * *") is False

    def test_step_zero_invalid(self) -> None:
        assert is_valid_schedule("cron */0 * * * *") is False

    def test_empty_cron_invalid(self) -> None:
        assert is_valid_schedule("cron ") is False
        assert is_valid_schedule("cron") is False


# ---------------------------------------------------------------------------
# cron schedules — is_table_due (#608)
# ---------------------------------------------------------------------------


# Fixed reference time for cron tests: 2026-06-07 05:00:00 UTC (a Sunday).
CRON_NOW = datetime(2026, 6, 7, 5, 0, 0, tzinfo=timezone.utc)


class TestIsTableDueCron:
    """is_table_due() with cron schedules. A cron occurrence is "due" when a
    fire time falls in the half-open window (last_sync, now] — mirroring the
    daily catch-up contract (fires on the next tick after a missed
    occurrence)."""

    def test_monthly_fire_just_passed_is_due(self) -> None:
        # cron fires at 05:00 on the 7th; last_sync at 04:59 same day,
        # now == 05:00 → the 05:00 occurrence is in (04:59, 05:00] → due.
        last_sync = datetime(2026, 6, 7, 4, 59, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("cron 0 5 7 * *", last_sync_iso=last_sync, now=CRON_NOW) is True

    def test_monthly_already_synced_at_fire_not_due(self) -> None:
        # last_sync == the fire time (05:00); window (05:00, 05:00] is empty
        # → not due again.
        last_sync = datetime(2026, 6, 7, 5, 0, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("cron 0 5 7 * *", last_sync_iso=last_sync, now=CRON_NOW) is False

    def test_monthly_missed_tick_catch_up_is_due(self) -> None:
        # Scheduler was down across the 05:00 fire; it's now 06:30 and the
        # last sync was the previous day. The missed 05:00 occurrence is in
        # (prev-day, 06:30] → catch-up fire.
        now = datetime(2026, 6, 7, 6, 30, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 6, 6, 5, 0, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("cron 0 5 7 * *", last_sync_iso=last_sync, now=now) is True

    def test_monthly_before_fire_not_due(self) -> None:
        # now is 04:30 on the 7th, before the 05:00 fire → not due.
        now = datetime(2026, 6, 7, 4, 30, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 6, 6, 5, 0, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("cron 0 5 7 * *", last_sync_iso=last_sync, now=now) is False

    def test_wrong_day_of_month_not_due(self) -> None:
        # 8th of the month, fire is on the 7th; nothing fired in the window.
        now = datetime(2026, 6, 8, 5, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 6, 7, 5, 0, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("cron 0 5 7 * *", last_sync_iso=last_sync, now=now) is False

    def test_month_end_31_never_fires_in_30_day_month(self) -> None:
        # June has 30 days; `cron 0 0 31 * *` must never fire in June.
        # Window spans the whole month — still no occurrence.
        now = datetime(2026, 6, 30, 23, 59, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("cron 0 0 31 * *", last_sync_iso=last_sync, now=now) is False

    def test_month_end_31_fires_in_31_day_month(self) -> None:
        # July has 31 days — the 31st at 00:00 fires.
        now = datetime(2026, 7, 31, 0, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 7, 30, 0, 0, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("cron 0 0 31 * *", last_sync_iso=last_sync, now=now) is True

    def test_weekly_monday_fire_is_due(self) -> None:
        # 2026-06-08 is a Monday; fire at 05:00 Monday.
        now = datetime(2026, 6, 8, 5, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 6, 8, 4, 0, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("cron 0 5 * * 1", last_sync_iso=last_sync, now=now) is True

    def test_weekly_non_monday_not_due(self) -> None:
        # 2026-06-09 is a Tuesday; the Monday-only cron does not fire.
        now = datetime(2026, 6, 9, 5, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 6, 9, 4, 0, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("cron 0 5 * * 1", last_sync_iso=last_sync, now=now) is False

    def test_cron_never_synced_is_due(self) -> None:
        # Never synced → due regardless of cron expression.
        assert is_table_due("cron 0 5 7 * *", last_sync_iso=None, now=CRON_NOW) is True

    def test_step_every_15m_fires_in_window(self) -> None:
        # `*/15` fires at :00 :15 :30 :45; from 05:00 to 05:20 the :15
        # occurrence is in the window.
        now = datetime(2026, 6, 7, 5, 20, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 6, 7, 5, 0, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("cron */15 * * * *", last_sync_iso=last_sync, now=now) is True

    def test_dow_sunday_zero(self) -> None:
        # 2026-06-07 is a Sunday (dow 0). cron `* * * * 0` fires.
        last_sync = datetime(2026, 6, 7, 4, 59, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("cron 0 5 * * 0", last_sync_iso=last_sync, now=CRON_NOW) is True

    # -- catch-up across gaps longer than a month (#627 review BUG_0001) --

    def test_catchup_across_59_day_gap_is_due(self) -> None:
        # `cron 0 0 31 * *` has a 59-day gap Jan 31 → Mar 31 (no Feb 31).
        # Instance offline since Jan 31; at May 15 the missed Mar 31
        # occurrence must still fire. The old 32-day lookback cap returned
        # False here.
        now = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 1, 31, 0, 0, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("cron 0 0 31 * *", last_sync_iso=last_sync, now=now) is True

    def test_feb29_cron_fires_across_multi_year_gap(self) -> None:
        # `cron 0 0 29 2 *` fires once every 4 years. last_sync just before
        # the 2028-02-29 occurrence, now well past it → due.
        now = datetime(2028, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2028, 2, 28, 0, 0, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("cron 0 0 29 2 *", last_sync_iso=last_sync, now=now) is True

    def test_feb29_cron_synced_at_fire_not_due_years_later(self) -> None:
        # Synced exactly at the 2028-02-29 fire; the next occurrence is
        # 2032 — at 2030 nothing new fired. Must also terminate fast (the
        # day-walk stops at last_sync's date, not after scanning years of
        # minutes).
        now = datetime(2030, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2028, 2, 29, 0, 0, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("cron 0 0 29 2 *", last_sync_iso=last_sync, now=now) is False


# ---------------------------------------------------------------------------
# next_due_at — display-only "next scheduled run" estimate (TCRD-226).
# Never used to gate an actual dispatch decision (is_table_due, called fresh
# at dispatch time, stays the single source of truth for that); this is a UI
# hint only.
# ---------------------------------------------------------------------------


class TestNextDueAtInterval:
    def test_never_run_is_due_now(self) -> None:
        assert next_due_at("every 15m", None, now=NOW) == NOW

    def test_adds_the_interval_to_last_run(self) -> None:
        last = (NOW - timedelta(minutes=5)).isoformat()
        assert next_due_at("every 15m", last, now=NOW) == NOW + timedelta(minutes=10)

    def test_hour_interval(self) -> None:
        last = NOW.isoformat()
        assert next_due_at("every 2h", last, now=NOW) == NOW + timedelta(hours=2)


class TestNextDueAtDaily:
    def test_next_time_today_when_still_ahead(self) -> None:
        # NOW is 12:00 UTC; "daily 18:00" hasn't fired yet today.
        assert next_due_at("daily 18:00", NOW.isoformat(), now=NOW) == NOW.replace(
            hour=18, minute=0, second=0, microsecond=0
        )

    def test_rolls_to_tomorrow_when_today_already_passed(self) -> None:
        # "daily 03:00" already passed at 12:00 — next fire is tomorrow.
        expected = (NOW + timedelta(days=1)).replace(hour=3, minute=0, second=0, microsecond=0)
        assert next_due_at("daily 03:00", NOW.isoformat(), now=NOW) == expected

    def test_multiple_times_picks_the_earliest_upcoming(self) -> None:
        # 12:00 now; "daily 03:00,15:00,20:00" → the next is 15:00 today.
        expected = NOW.replace(hour=15, minute=0, second=0, microsecond=0)
        assert next_due_at("daily 03:00,15:00,20:00", NOW.isoformat(), now=NOW) == expected


class TestNextDueAtUnsupported:
    def test_cron_returns_none(self) -> None:
        """No general next-occurrence solver for cron — display degrades to
        "no estimate" rather than a wrong guess; is_table_due is unaffected."""
        assert next_due_at("cron 0 5 * * 1", None, now=NOW) is None

    def test_invalid_schedule_returns_none(self) -> None:
        assert next_due_at("not-a-schedule", None, now=NOW) is None

    def test_empty_schedule_returns_none(self) -> None:
        assert next_due_at("", None, now=NOW) is None
        assert next_due_at(None, None, now=NOW) is None
