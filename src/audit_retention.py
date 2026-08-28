"""Retention-based pruning of audit/activity trails.

``audit_log`` (B8) was the first trail to get a retention policy —
``prune_audit_log`` below, run daily by the scheduler (``audit-prune``,
``services/scheduler/__main__.py``) via ``POST /api/admin/run-audit-prune``
(``app/api/admin.py``). It stays standalone: its own job, its own endpoint,
its own 365-day default, unchanged by this module's generalization.

Track E3 Slice 1 generalizes the *pattern* — short-circuit on a non-positive
window, delete only the trail's own table, return a deleted-row count — to
the other unbounded trails: ``sync_history``, ``llm_usage``, and
``agent_scope_snapshots``. Each is dispatched through :func:`prune_trail` /
:func:`run_retention_sweep`, wired to the scheduler as one daily
``retention-prune`` job (``POST /api/admin/run-retention-prune``) that
iterates every registered trail. Every trail's window defaults to **0 = keep
forever = off** — the sweep changes nothing until an admin explicitly sets
``retention.<trail>_days`` in ``instance.yaml``.

``usage_events`` already had its own opt-in retention knob
(``USAGE_EVENTS_RETENTION_DAYS`` / ``POST /api/admin/usage/prune``) before
this change; it keeps that standalone job (not folded into the
``retention-prune`` sweep, to avoid double-pruning the same table twice a
day) but now also honors ``retention.usage_events_days`` in
``instance.yaml`` as a config-file alternative to the env var — see
``app.instance_config.get_usage_events_retention_days``.

``chat_messages`` (privacy-sensitive) and the filesystem session JSONLs are
deliberately out of scope — see docs/observability.md.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


def prune_audit_log(*, retention_days: int, repo: Optional[Any] = None) -> Dict[str, Any]:
    """Delete ``audit_log`` rows older than ``retention_days``.

    Args:
        retention_days: rows whose ``timestamp`` is older than this many
            days qualify. ``retention_days <= 0`` short-circuits to a
            no-op so operators can disable the prune cleanly (rows kept
            forever) without ripping the scheduler job.
        repo: injectable audit repo (tests); defaults to ``audit_repo()``.

    Returns ``{"pruned": int, "skipped": bool}``.
    """
    if retention_days <= 0:
        return {"pruned": 0, "skipped": True}

    if repo is None:
        from src.repositories import audit_repo

        repo = audit_repo()

    pruned = repo.prune_older_than(retention_days)
    logger.info(
        "audit-prune: removed %d audit_log row(s) older than %d days",
        pruned,
        retention_days,
    )
    return {"pruned": pruned, "skipped": False}


# ---------------------------------------------------------------------------
# Track E3 Slice 1 — generalized per-trail dispatch
# ---------------------------------------------------------------------------


def _prune_sync_history(days: int, repo: Optional[Any] = None) -> int:
    if repo is None:
        from src.repositories import sync_state_repo

        repo = sync_state_repo()
    return repo.prune_history_older_than(days)


def _prune_llm_usage(days: int, repo: Optional[Any] = None) -> int:
    if repo is None:
        from src.repositories import llm_usage_repo

        repo = llm_usage_repo()
    return repo.prune_older_than(days)


def _prune_agent_scope_snapshots(days: int, repo: Optional[Any] = None) -> int:
    if repo is None:
        from src.repositories import agents_repo

        repo = agents_repo()
    return repo.prune_scope_snapshots_older_than(days)


# ``{trail: prune_fn(days, repo=None) -> deleted_count}``. ``audit_log`` is
# deliberately NOT here — it keeps its own standalone job/endpoint
# (``prune_audit_log`` above); folding it in would change nothing
# functionally but would couple two independently-tested code paths for no
# benefit. ``usage_events`` is deliberately NOT here either — see the module
# docstring for why it stays on its pre-existing standalone job.
_TRAIL_PRUNERS: Dict[str, Callable[..., int]] = {
    "sync_history": _prune_sync_history,
    "llm_usage": _prune_llm_usage,
    "agent_scope_snapshots": _prune_agent_scope_snapshots,
}


def prune_trail(trail: str, *, retention_days: int, repo: Optional[Any] = None) -> Dict[str, Any]:
    """Prune one registered trail by name.

    Mirrors :func:`prune_audit_log`'s contract: ``retention_days <= 0``
    short-circuits to a no-op — the registered prune callable (and any
    injected ``repo``) is never touched — and the return shape is always
    ``{"pruned": int, "skipped": bool}``.

    Raises ``ValueError`` for a trail name not in :data:`_TRAIL_PRUNERS`
    (a programming error, not an operator-facing condition — callers pass
    trail names from a fixed internal registry, never user input).
    """
    if trail not in _TRAIL_PRUNERS:
        raise ValueError(f"unknown retention trail: {trail!r}")

    if retention_days <= 0:
        return {"pruned": 0, "skipped": True}

    pruned = _TRAIL_PRUNERS[trail](retention_days, repo)
    logger.info(
        "retention-prune: removed %d %s row(s) older than %d days",
        pruned,
        trail,
        retention_days,
    )
    return {"pruned": pruned, "skipped": False}


def run_retention_sweep(retention_days: Dict[str, int]) -> Dict[str, Dict[str, Any]]:
    """Dispatch a prune across every registered trail.

    Args:
        retention_days: ``{trail: days}``. A trail absent from this dict is
            treated as ``0`` (keep forever) — the same safe default as an
            explicit ``0``. Trails not in :data:`_TRAIL_PRUNERS` are
            ignored (forward-compatible with a caller passing extra keys).

    Returns ``{trail: {"pruned": int, "skipped": bool}}`` for every
    registered trail.
    """
    return {trail: prune_trail(trail, retention_days=retention_days.get(trail, 0)) for trail in _TRAIL_PRUNERS}
