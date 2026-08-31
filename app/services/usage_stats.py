"""One read model for "how many tokens did this cost".

Four surfaces answer that question — the analyst's own ``/api/me/stats/*``,
the admin adoption drill-down, the admin telemetry KPI cards, and an analyst
SELECT over the internal ``agnes_sessions`` table — and until this module they
each computed their own aggregate over ``usage_session_summary``. That is how
``/me/activity`` was able to render zeros for a user the admin dashboard listed
as the busiest on the instance: two queries, two identity columns, no test that
compared them. ``tests/test_usage_surfaces_agree.py`` is now that test, and this
module is the single implementation it pins.

**Identity.** Every self-scoped read keys on ``users.id``. ``username`` holds
the display email and is a grouping/display field, never a filter here.

**Cost is computed, never stored.** ``src.llm_pricing`` turns token counts into
USD at read time, so a price change re-prices history instead of leaving a
column nobody can reproduce. An unknown model prices at a conservative
built-in tier rather than at zero — "free" is not a safe reading of "unknown".

**Where the per-model split comes from.** ``usage_turns`` (Postgres-only)
records a model per assistant turn, which is strictly better attribution than
a session summary's ``primary_model`` (merely the session's modal model). It is
used when it is available AND when its token sums reconcile with the summary
totals for the same user and window; otherwise the summaries' own split is used.
That reconciliation check is deliberate: turns only exist for sessions the
post-A3 processor has walked, so on an instance mid-backfill the turn rows cover
a strict subset of the summaries, and pricing that subset while displaying the
full totals next to it would put a cost figure on screen that does not add up to
the tokens beside it. A coarser breakdown that reconciles beats a finer one that
does not.

**Instance-wide (``user_id=None``) needs Postgres.** There is no summaries-backed
repository read that returns instance-wide per-model token sums, and adding one
would mean a new method on the frozen ``usage`` pair. So an instance-wide cost is
answered from ``usage_turns`` when the backend has it and reported as ``None``
— unavailable, never a zero that reads like "free" — when it does not. Callers
surface that as a null field, never as a 501: a DuckDB instance's dashboards
must keep rendering.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from src.llm_pricing import cost_usd
from src.repositories import usage_repo, usage_turns_repo

# Imported from the error module rather than re-exported through
# ``src.repositories``: the backend-parametrized test fixtures reload that
# package, which rebinds its names to NEW class objects and would make an
# ``except`` clause bound at import time silently stop matching.
from src.repository_errors import RequiresPostgresBackend

logger = logging.getLogger(__name__)

#: The four token counts, in this module's canonical (summary-shaped) key
#: names. ``usage_turns`` rows use the ``*_tokens`` spelling; :func:`_tokens`
#: normalizes both onto these.
TOKEN_KEYS = ("input", "output", "cache_read", "cache_creation")

_TURN_KEY = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_tokens",
    "cache_creation": "cache_creation_tokens",
}

#: USD are rounded for transport so a float artefact does not reach a template
#: as ``0.30000000000000004``. Six places keeps sub-cent figures meaningful for
#: the small token counts a single turn produces.
_COST_PLACES = 6


def _tokens(row: Dict[str, Any]) -> Dict[str, int]:
    """The four counts out of either row shape (summary or turn)."""
    out: Dict[str, int] = {}
    for key in TOKEN_KEYS:
        value = row.get(key)
        if value is None:
            value = row.get(_TURN_KEY[key])
        out[key] = int(value or 0)
    return out


def _zero() -> Dict[str, int]:
    return {key: 0 for key in TOKEN_KEYS}


def _add(into: Dict[str, int], more: Dict[str, int]) -> None:
    for key in TOKEN_KEYS:
        into[key] += more[key]


def price(model: Optional[str], tokens: Dict[str, int]) -> float:
    """USD for one model's token counts. The only pricing call in this module,
    so every surface prices the same tokens the same way."""
    return round(
        cost_usd(
            model,
            input_tokens=tokens["input"],
            output_tokens=tokens["output"],
            cache_read_tokens=tokens["cache_read"],
            cache_creation_tokens=tokens["cache_creation"],
        ),
        _COST_PLACES,
    )


def _as_utc(value: Any) -> Optional[datetime]:
    """A timestamp column as an aware UTC datetime, or ``None`` when it cannot
    be placed in time. A naive value is read as UTC — that is what both
    backends store."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def sessions_for_user(user_id: str) -> List[Dict[str, Any]]:
    """The caller's own session summaries, newest first.

    Rows written before the ``user_id`` column existed carry no id and so do
    not appear in a self-view; they stay reachable through the admin session
    browser, which also matches on ``username``. No backfill.
    """
    return usage_repo().list_sessions_for_user_self(user_id)


# ---------------------------------------------------------------------------
# Tokens + cost
# ---------------------------------------------------------------------------


def _summary_by_model(user_id: str, since_days: Optional[int]) -> tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Per-``primary_model`` token sums for one user, and their total.

    Lifetime reads use the repository's own GROUP BY. A windowed read folds the
    user's session list in Python instead, because the windowed aggregate the
    repository does expose (``tokens_daily_series``) groups by day, not by
    model — and adding a windowed by-model read would mean a new method on the
    frozen ``usage`` pair. The fold is bounded by one user's session count and
    is the same read ``/api/me/stats/sessions`` already performs unpaginated.
    """
    repo = usage_repo()
    grouped: Dict[Any, Dict[str, Any]] = {}
    totals = _zero()

    if since_days is None:
        rows = repo.tokens_by_model(user_id)
        for row in rows:
            tokens = _tokens(row)
            _add(totals, tokens)
            grouped[row.get("model")] = {
                "model": row.get("model"),
                **tokens,
                "sessions": int(row.get("sessions") or 0),
                "turns": None,
                "grain": "session",
            }
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
        for row in repo.list_sessions_for_user_self(user_id):
            started = _as_utc(row.get("started_at"))
            # A session that cannot be placed in time cannot be placed in a
            # window either — the SQL windowed reads drop it too.
            if started is None or started < cutoff:
                continue
            model = row.get("primary_model") or "(unknown)"
            tokens = _tokens(row)
            _add(totals, tokens)
            bucket = grouped.setdefault(
                model,
                {"model": model, **_zero(), "sessions": 0, "turns": None, "grain": "session"},
            )
            _add(bucket, tokens)
            bucket["sessions"] += 1

    rows_out = sorted(
        grouped.values(),
        key=lambda r: (-sum(r[k] for k in TOKEN_KEYS), str(r["model"])),
    )
    return rows_out, totals


def _turns_by_model(
    user_id: Optional[str], since_days: Optional[int]
) -> Optional[tuple[List[Dict[str, Any]], Dict[str, int]]]:
    """Per-model token sums from ``usage_turns``, or ``None`` when the backend
    has no such table (DuckDB app-state, frozen under A3)."""
    try:
        result = usage_turns_repo().totals_for_user(user_id, since_days)
    except RequiresPostgresBackend:
        return None
    except Exception:  # pragma: no cover - telemetry must never break a read
        logger.exception("usage_turns read failed; falling back to session summaries")
        return None

    totals = _zero()
    rows_out: List[Dict[str, Any]] = []
    for row in result.get("by_model") or []:
        tokens = _tokens(row)
        _add(totals, tokens)
        rows_out.append(
            {
                "model": row.get("model"),
                **tokens,
                "sessions": None,
                "turns": int(row.get("turns") or 0),
                "grain": "turn",
            }
        )
    return rows_out, totals


def _session_totals(user_id: str, since_days: Optional[int]) -> Dict[str, Any]:
    """Windowed or lifetime token totals for one user, straight from the
    repository's aggregates (both backends). Carries a ``sessions`` count
    alongside the four token keys."""
    repo = usage_repo()
    if since_days is None:
        raw = repo.tokens_totals(user_id)
        totals = _tokens(raw)
        totals_out: Dict[str, Any] = dict(totals)
        totals_out["sessions"] = int(raw.get("sessions") or 0)
        return totals_out
    totals = _zero()
    sessions = 0
    for day in repo.tokens_daily_series(user_id, since_days):
        _add(totals, _tokens(day))
        sessions += int(day.get("sessions") or 0)
    out: Dict[str, Any] = dict(totals)
    out["sessions"] = sessions
    return out


def token_totals(user_id: Optional[str], since_days: Optional[int] = None) -> Dict[str, Any]:
    """Token totals, their per-model split, and what they cost.

    ``user_id=None`` means instance-wide and is for admin surfaces only — the
    caller is responsible for having established that. ``since_days=None`` means
    all time.

    Returns::

        {"input": …, "output": …, "cache_read": …, "cache_creation": …,
         "total": …, "sessions": …,
         "cost_usd": float | None,
         "by_model": [{"model": …, <the four counts>, "total": …,
                       "sessions": int | None, "turns": int | None,
                       "grain": "session" | "turn", "cost_usd": …}, …],
         "tokens_source": "summaries" | "turns" | "unavailable"}

    ``cost_usd`` is ``None`` only when nothing could be priced — an
    instance-wide read on a backend without ``usage_turns``. It is never a
    zero standing in for "unknown".

    ``sessions`` is meaningful only for a user-scoped read; an instance-wide
    read counts turns, not sessions, and reports ``0`` for it.
    """
    turns = _turns_by_model(user_id, since_days)

    if user_id is None:
        if turns is None:
            return {
                **_zero(),
                "total": 0,
                "sessions": 0,
                "cost_usd": None,
                "by_model": [],
                "tokens_source": "unavailable",
            }
        rows, totals = turns
        return _assemble(rows, totals, sessions=0, source="turns")

    totals = _session_totals(user_id, since_days)
    summary_rows, summary_totals = _summary_by_model(user_id, since_days)

    rows = summary_rows
    source = "summaries"
    if turns is not None:
        turn_rows, turn_totals = turns
        # Only trust the finer split when it accounts for exactly the tokens
        # being reported; see the module docstring.
        if all(turn_totals[key] == totals[key] for key in TOKEN_KEYS):
            rows, source = turn_rows, "turns"
    # The summaries' own split is what the totals came from, so any drift here
    # is a repository-level inconsistency worth seeing in the logs.
    elif any(summary_totals[key] != totals[key] for key in TOKEN_KEYS):
        logger.debug("per-model summary split does not reconcile with the totals for user %s", user_id)

    return _assemble(rows, totals, sessions=int(totals.get("sessions") or 0), source=source)


def _assemble(rows: List[Dict[str, Any]], totals: Dict[str, int], *, sessions: int, source: str) -> Dict[str, Any]:
    priced: List[Dict[str, Any]] = []
    for row in rows:
        tokens = {key: int(row[key]) for key in TOKEN_KEYS}
        priced.append(
            {
                **row,
                **tokens,
                "total": sum(tokens.values()),
                "cost_usd": price(row.get("model"), tokens),
            }
        )
    counts = {key: int(totals[key]) for key in TOKEN_KEYS}
    return {
        **counts,
        "total": sum(counts.values()),
        "sessions": sessions,
        # Summing the per-model costs (rather than pricing the totals once)
        # is what keeps a rendered cost column adding up to the figure above it.
        "cost_usd": round(sum(r["cost_usd"] for r in priced), _COST_PLACES),
        "by_model": priced,
        "tokens_source": source,
    }


def daily_token_series(user_id: str, days: int) -> List[Dict[str, Any]]:
    """Per-day token totals for the caller's chart."""
    return usage_repo().tokens_daily_series(user_id, days)


def top_token_sessions(user_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    """The caller's heaviest sessions, priced. Cost here is per SESSION, so it
    uses the summary's ``primary_model`` — a single session's turns are not
    broken out on this surface."""
    rows = usage_repo().tokens_top_sessions(user_id, limit)
    out: List[Dict[str, Any]] = []
    for row in rows:
        out.append({**row, "cost_usd": price(row.get("primary_model"), _tokens(row))})
    return out


def cost_usd_for_user(user_id: Optional[str], since_days: Optional[int] = None) -> Optional[float]:
    """Just the cost — for the KPI cards that render a currency figure next to
    numbers they compute themselves. Never raises: a dashboard must not 500
    because pricing or telemetry is unavailable."""
    try:
        return token_totals(user_id, since_days)["cost_usd"]
    except Exception:  # pragma: no cover - defensive
        logger.exception("cost computation failed for user_id=%s", user_id)
        return None
