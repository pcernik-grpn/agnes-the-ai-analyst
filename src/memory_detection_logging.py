"""Best-effort writer for ``memory_detection_runs`` (issue #1971 Part 3).

One function, ``record_detection_run``, is the ONLY sanctioned way either
corporate-memory extractor path (the session-transcript verification
processor, the CLAUDE.local.md collector wrapper) writes a run-log row.
It must NEVER raise: a run-log write is observability, not the detection
run itself, and this is the fallback boundary that guarantees a DuckDB-backed
instance — where ``memory_detection_runs_repo()`` raises
``RequiresPostgresBackend`` — degrades to one warning log line instead of
ever failing (or even slowing down the retry semantics of) the run it
describes.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def policy_fingerprint(policy_text: Optional[str]) -> Optional[str]:
    """sha256 hex digest of the policy text a run actually used, or ``None``
    when the source consults no editable policy at all (as opposed to an
    empty-string policy, which would still hash to a real, comparable
    digest) — the two must stay tellable apart so "no policy applies here"
    is never confused with "the policy happens to be blank"."""
    if policy_text is None:
        return None
    return hashlib.sha256(policy_text.encode("utf-8")).hexdigest()


def record_detection_run(
    *,
    source: str,
    started_at: datetime,
    finished_at: Optional[datetime] = None,
    sessions_scanned: int = 0,
    items_proposed: int = 0,
    items_filtered: int = 0,
    items_inserted: int = 0,
    items_routed_side_domain: int = 0,
    token_usage: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    dry_run: bool = False,
    policy_text: Optional[str] = None,
) -> Optional[str]:
    """Write one ``memory_detection_runs`` row; returns its id, or ``None``
    on ANY failure (including — expectedly — a DuckDB-backed instance,
    where the repo factory raises ``RequiresPostgresBackend``).

    ``policy_text`` is hashed here (never stored raw) via
    :func:`policy_fingerprint` — the row carries a fingerprint an admin can
    correlate against a policy edit, never the policy content itself.
    """
    try:
        from src.repositories import memory_detection_runs_repo

        return memory_detection_runs_repo().create(
            source=source,
            started_at=started_at,
            finished_at=finished_at or started_at,
            sessions_scanned=sessions_scanned,
            items_proposed=items_proposed,
            items_filtered=items_filtered,
            items_inserted=items_inserted,
            items_routed_side_domain=items_routed_side_domain,
            token_usage=token_usage,
            error=error,
            dry_run=dry_run,
            policy_fingerprint=policy_fingerprint(policy_text),
        )
    except Exception as exc:  # noqa: BLE001 — observability only, never fatal
        logger.warning("memory_detection_runs: could not record %s run: %s", source, exc)
        return None
