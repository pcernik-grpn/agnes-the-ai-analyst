"""Retention-based pruning of ``audit_log`` rows.

Run daily by the scheduler (``audit-prune``, ``services/scheduler/__main__.py``)
via ``POST /api/admin/run-audit-prune`` (``app/api/admin.py``). Mirrors the
short-circuit convention of ``src/store_guardrails/purge.py::purge_blocked_bundles``:
a non-positive retention window means "keep forever" and the DELETE never runs.

Only ``audit_log`` has a retention policy today. The other six audit/
observability trails (chat transcripts, CLI session JSONLs, usage rollups,
sync_history, llm_usage, agent-runtime forensics) are documented but
unmanaged — see docs/observability.md.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

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
