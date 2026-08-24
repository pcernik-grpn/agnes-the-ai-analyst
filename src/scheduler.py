"""
Schedule evaluation for automatic data sync.

Parses sync_schedule strings from table configuration and determines
whether a table is due for synchronization based on its last sync time.

Schedule formats:
    "every 15m"            - every 15 minutes
    "every 1h"             - every hour
    "daily 05:00"          - once per day at 05:00 UTC
    "daily 07:00,13:00,18:00" - multiple times per day (UTC)
    "cron 0 5 7 * *"       - standard 5-field cron, UTC (minute hour
                             day-of-month month day-of-week). Supports
                             ``*``, comma lists (``1,15``), ranges
                             (``9-17``), and steps (``*/15``). day-of-week
                             is 0-6 (0 = Sunday). e.g. ``cron 0 5 * * 1``
                             = 05:00 UTC every Monday.
"""

import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Pattern: "every 15m", "every 2h"
INTERVAL_PATTERN = re.compile(r"^every (\d+)([mh])$")

# Pattern: "daily 05:00", "daily 17:30", "daily 07:00,13:00,18:00"
DAILY_PATTERN = re.compile(r"^daily ([\d:,]+)$")

# Pattern: "cron <5-field expr>". The explicit ``cron `` prefix
# disambiguates from ``every`` / ``daily`` and avoids parser ambiguity
# with a bare 5-field string.
CRON_PATTERN = re.compile(r"^cron (.+)$")

# (min, max) range per cron field, in field order:
# minute, hour, day-of-month, month, day-of-week.
_CRON_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))


def parse_interval_minutes(schedule: str) -> Optional[int]:
    """Parse an interval schedule into minutes.

    Args:
        schedule: Schedule string like "every 15m" or "every 1h"

    Returns:
        Interval in minutes, or None if not an interval schedule.
    """
    match = INTERVAL_PATTERN.match(schedule)
    if not match:
        return None
    value = int(match.group(1))
    unit = match.group(2)
    if unit == "h":
        return value * 60
    return value


def is_table_due(
    schedule: str,
    last_sync_iso: Optional[str],
    now: Optional[datetime] = None,
) -> bool:
    """Determine whether a table is due for sync based on its schedule.

    Args:
        schedule: Schedule string from table config (e.g., "every 1h", "daily 05:00")
        last_sync_iso: ISO timestamp of last sync, or None if never synced
        now: Current time (UTC). Defaults to datetime.now(timezone.utc).

    Returns:
        True if the table should be synced now.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Never synced -> always due
    if not last_sync_iso:
        logger.info("Table never synced, marking as due")
        return True

    # Parse last_sync timestamp
    last_sync = _parse_timestamp(last_sync_iso)
    if last_sync is None:
        logger.warning(f"Cannot parse last_sync timestamp: {last_sync_iso}, marking as due")
        return True

    # Ensure timezone-aware comparison
    if last_sync.tzinfo is None:
        last_sync = last_sync.replace(tzinfo=timezone.utc)

    # Check interval schedule: "every Xm" / "every Xh"
    interval_minutes = parse_interval_minutes(schedule)
    if interval_minutes is not None:
        elapsed_minutes = (now - last_sync).total_seconds() / 60
        due = elapsed_minutes >= interval_minutes
        if due:
            logger.debug(f"Interval schedule: {elapsed_minutes:.0f}m elapsed >= {interval_minutes}m interval")
        return due

    # Check daily schedule: "daily HH:MM" or "daily HH:MM,HH:MM,..."
    match = DAILY_PATTERN.match(schedule)
    if match:
        times_str = match.group(1)
        target_times = _parse_daily_times(times_str)
        if not target_times:
            logger.warning(f"Invalid daily schedule times: {schedule}")
            return False
        return _is_daily_due(last_sync, now, target_times)

    # Check cron schedule: "cron <minute hour dom month dow>"
    cron_match = CRON_PATTERN.match(schedule)
    if cron_match:
        fields = _parse_cron_fields(cron_match.group(1))
        if fields is None:
            logger.warning(f"Invalid cron schedule: {schedule}")
            return False
        return _is_cron_due(last_sync, now, fields)

    logger.warning(f"Unknown schedule format: {schedule}")
    return False


def _parse_daily_times(times_str: str) -> list[tuple[int, int]]:
    """Parse comma-separated HH:MM times into list of (hour, minute) tuples."""
    time_pattern = re.compile(r"^(\d{2}):(\d{2})$")
    result = []
    for part in times_str.split(","):
        m = time_pattern.match(part.strip())
        if not m:
            return []
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour > 23 or minute > 59:
            return []
        result.append((hour, minute))
    return result


def _is_daily_due(
    last_sync: datetime,
    now: datetime,
    target_times: list[tuple[int, int]],
) -> bool:
    """Check if a daily schedule is due.

    Supports multiple target times per day. A target time is due when:
    1. Current time is at or past HH:MM today, AND
    2. Last sync was before HH:MM today

    Returns True if ANY of the target times is due.
    """
    for target_hour, target_minute in target_times:
        today_target = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)

        if now >= today_target and last_sync < today_target:
            logger.debug(
                f"Daily schedule: target {target_hour:02d}:{target_minute:02d} UTC, "
                f"last sync {last_sync.isoformat()}, now {now.isoformat()} -> due"
            )
            return True

    return False


def _parse_cron_field(spec: str, lo: int, hi: int) -> Optional[set[int]]:
    """Expand a single cron field into the set of values it matches.

    Supports ``*``, comma lists (``a,b``), ranges (``a-b``), and steps
    (``*/n`` or ``a-b/n``). Returns None if the field is malformed or any
    value falls outside ``[lo, hi]``.
    """
    values: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            return None

        # Optional step: "<base>/<step>"
        step = 1
        if "/" in part:
            base, _, step_str = part.partition("/")
            if not step_str.isdigit():
                return None
            step = int(step_str)
            if step <= 0:
                return None
        else:
            base = part

        # Resolve the base into a [start, end] inclusive span.
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            start_str, _, end_str = base.partition("-")
            if not (start_str.isdigit() and end_str.isdigit()):
                return None
            start, end = int(start_str), int(end_str)
            if start > end:
                return None
        else:
            if not base.isdigit():
                return None
            start = end = int(base)

        if start < lo or end > hi:
            return None

        values.update(range(start, end + 1, step))

    return values or None


def _parse_cron_fields(expr: str) -> Optional[list[set[int]]]:
    """Parse a 5-field cron expression into per-field value sets.

    Field order: minute, hour, day-of-month, month, day-of-week. Returns
    None for any expression that does not have exactly 5 well-formed,
    in-range fields.
    """
    parts = expr.split()
    if len(parts) != len(_CRON_FIELD_RANGES):
        return None
    fields: list[set[int]] = []
    for spec, (lo, hi) in zip(parts, _CRON_FIELD_RANGES):
        values = _parse_cron_field(spec, lo, hi)
        if values is None:
            return None
        fields.append(values)
    return fields


def _cron_date_matches(d: date, fields: list[set[int]]) -> bool:
    """Return True iff date ``d`` matches the cron day-of-month / month /
    day-of-week fields.

    Standard cron day-of-month / day-of-week semantics are NOT OR-combined
    here: both restrictions must hold (the common case has one of them as
    ``*``, which always matches). ``weekday()`` returns 0=Monday..6=Sunday;
    cron uses 0=Sunday..6=Saturday, so we remap with ``(weekday()+1) % 7``.
    """
    _, _, dom_set, month_set, dow_set = fields
    cron_dow = (d.weekday() + 1) % 7
    return d.day in dom_set and d.month in month_set and cron_dow in dow_set


def _is_cron_due(
    last_sync: datetime,
    now: datetime,
    fields: list[set[int]],
) -> bool:
    """Check whether a cron occurrence falls in the half-open window
    ``(last_sync, now]``.

    Mirrors the ``_is_daily_due`` catch-up contract: a missed occurrence
    fires on the next tick after it passed. The search walks DAYS backward
    from ``now``: on the first day matching the date fields it takes the
    latest in-day (hour, minute) candidate that is ``<= now`` and compares
    it against ``last_sync``. Earlier days only hold earlier occurrences,
    so that single comparison decides — no minute-by-minute scan.

    The walk is bounded to 8 years of days. The longest gap between two
    consecutive fires of a valid 5-field cron is a Feb-29 schedule (4
    years; 8 absorbs the skipped-century corner), so the bound cannot skip
    a real occurrence — unlike the previous 32-day minute-walk cap, which
    silently missed e.g. ``cron 0 0 31 * *`` across the Jan 31 → Mar 31
    59-day gap.
    """
    if now <= last_sync:
        return False

    minute_set, hour_set = fields[0], fields[1]
    # Minute resolution: cron never fires on sub-minute boundaries.
    cap = now.replace(second=0, microsecond=0)
    day = cap.date()
    for _ in range(8 * 366):
        if _cron_date_matches(day, fields):
            # Latest candidate on this day that is <= cap. For any day
            # before today the first (max hour, max minute) combo wins
            # immediately; on today the loop skips still-future slots.
            occurrence = None
            for hour in sorted(hour_set, reverse=True):
                for minute in sorted(minute_set, reverse=True):
                    cand = datetime.combine(day, time(hour, minute), tzinfo=now.tzinfo)
                    if cand <= cap:
                        occurrence = cand
                        break
                if occurrence is not None:
                    break
            if occurrence is not None:
                if occurrence > last_sync:
                    logger.debug(
                        "Cron schedule: occurrence at %s in window (%s, %s] -> due",
                        occurrence.isoformat(),
                        last_sync.isoformat(),
                        now.isoformat(),
                    )
                    return True
                # The most recent occurrence predates the window; earlier
                # days are earlier still — nothing new fired.
                return False
        if day <= last_sync.date():
            return False
        day -= timedelta(days=1)
    return False


def _parse_timestamp(iso_string: str) -> Optional[datetime]:
    """Parse an ISO timestamp string, handling various formats.

    Args:
        iso_string: ISO 8601 timestamp string

    Returns:
        datetime object or None if parsing fails
    """
    try:
        # Python 3.11+ fromisoformat handles most formats
        return datetime.fromisoformat(iso_string)
    except (ValueError, TypeError):
        pass

    # Fallback: try common formats
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(iso_string, fmt)
        except ValueError:
            continue

    return None


def is_valid_schedule(schedule: Optional[str]) -> bool:
    """Return True iff ``schedule`` parses as a documented schedule string.

    Accepted forms (mirroring the rest of this module):
      - ``"every Nm"`` / ``"every Nh"`` with N a non-negative integer
        (``every 0m`` = always due, useful for force-resync of a row whose
        previous attempt errored without recording last_sync — bypasses
        the rate limit on the next ``/api/sync/trigger`` tick)
      - ``"daily HH:MM"`` (24-h, UTC) optionally comma-separated:
        ``"daily 07:00,13:00"``
      - ``"cron <5-field expr>"`` (UTC): standard minute/hour/day-of-month/
        month/day-of-week cron with ``*``, comma lists, ranges, and steps.
        Each field is validated against its range (minute 0-59, hour 0-23,
        day-of-month 1-31, month 1-12, day-of-week 0-6).

    Anything else — including ``None``, empty string, or a parseable-looking
    but out-of-range value (``"daily 25:00"`` or ``"cron 99 5 7 * *"``) —
    returns False. Pydantic validators on the admin API call this to reject
    malformed input with 422 instead of accepting it and silently
    no-op'ing later.
    """
    if not schedule or not isinstance(schedule, str):
        return False
    interval = parse_interval_minutes(schedule)
    if interval is not None:
        return interval >= 0
    match = DAILY_PATTERN.match(schedule)
    if match:
        return bool(_parse_daily_times(match.group(1)))
    cron_match = CRON_PATTERN.match(schedule)
    if cron_match:
        return _parse_cron_fields(cron_match.group(1)) is not None
    return False


def filter_due_tables(
    table_configs: list[dict],
    sync_state_repo,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Drop table configs whose ``sync_schedule`` says they are not due.

    Behaviour:
      - ``sync_schedule`` is None / empty / not a valid string → table passes
        through (no schedule = "sync on every tick", existing behaviour).
      - Valid schedule + last_sync within the cadence → drop.
      - Valid schedule + last_sync past cadence (or never) → keep.
      - Invalid schedule string → log a warning and let the table through
        (do NOT silently skip — operator surprise is worse than a redundant
        sync).

    ``sync_state_repo`` is duck-typed: only ``get_last_sync(table_id)`` is
    called — up to twice per table (canonical key first, legacy name
    fallback second, see below) — returning a ``datetime`` (tz-aware
    preferred, naive treated as UTC) or ``None``.
    """
    from src.sync_state_key import resolve_sync_state_key_for_row

    if now is None:
        now = datetime.now(timezone.utc)
    out: list[dict] = []
    for tc in table_configs:
        # B1: sync_state.table_id is the registry `id` when a matching
        # registry row existed at write time (src.sync_state_key), so the
        # lookup resolves the canonical key first — `tc` IS the registry
        # row, so this is a plain field read via the shared resolver. A
        # sync_state row still keyed by `name` (legacy state the backfill
        # migration hasn't reached yet) is picked up by the name fallback
        # below — the same id-first/name-second resolution the other B1
        # readers use (`app/api/admin.py::list_registry`, `app/api/
        # sync.py::_build_manifest_for_user`). Keying name-first here (the
        # pre-B1 contract) would make an id != name table (auto-discovered
        # Keboola rows: id="in_c-crm_company", name="company") read
        # last_sync=None on every tick once writers re-key, degrading the
        # filter to "always sync" and defeating the schedule.
        name = tc.get("name") or tc.get("id")
        table_id = resolve_sync_state_key_for_row(name, tc)
        schedule = tc.get("sync_schedule")
        if not schedule:
            out.append(tc)
            continue
        if not is_valid_schedule(schedule):
            logger.warning(
                "Table %s has malformed sync_schedule %r — syncing anyway "
                "(fix the schedule string to suppress this message)",
                table_id,
                schedule,
            )
            out.append(tc)
            continue
        last_sync = sync_state_repo.get_last_sync(table_id)
        if last_sync is None and name and name != table_id:
            # Legacy fallback: a sync_state row written pre-B1 (or before
            # the 0071 backfill ran) is keyed by the table's name.
            last_sync = sync_state_repo.get_last_sync(name)
        if last_sync is None:
            last_sync_iso = None
        else:
            if last_sync.tzinfo is None:
                last_sync = last_sync.replace(tzinfo=timezone.utc)
            last_sync_iso = last_sync.isoformat()
        if is_table_due(schedule, last_sync_iso, now=now):
            out.append(tc)
        else:
            logger.info(
                "Table %s skipped: schedule=%r, last_sync=%s, not due yet",
                table_id,
                schedule,
                last_sync_iso,
            )
    return out
