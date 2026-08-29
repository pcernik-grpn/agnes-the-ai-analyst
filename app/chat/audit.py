"""audit_log writer for chat events. Re-uses Agnes's existing audit table."""

from __future__ import annotations

import logging
import time
from typing import Any

from src.audit_helpers import hash_args  # noqa: F401 — re-exported, see below

logger = logging.getLogger(__name__)

# email → (users.id, monotonic-stamp). Chat emits an audit row per tool call,
# so the resolution result is cached briefly instead of hitting the users
# table on every frame.
_EMAIL_ID_CACHE: dict[str, tuple[str, float]] = {}
_EMAIL_ID_TTL_SECONDS = 300.0


def _resolve_user_id(user_email: str) -> str:
    """Map an email to ``users.id``; fall back to the email when the account
    doesn't exist (better a searchable email than a dropped row)."""
    now = time.monotonic()
    hit = _EMAIL_ID_CACHE.get(user_email)
    if hit is not None and (now - hit[1]) < _EMAIL_ID_TTL_SECONDS:
        return hit[0]
    from src.repositories import users_repo

    row = users_repo().get_by_email(user_email)
    resolved = row["id"] if row else user_email
    _EMAIL_ID_CACHE[user_email] = (resolved, now)
    return resolved


def write_audit(
    *,
    user_email: str,
    action: str,
    details: dict[str, Any],
    user_id: str | None = None,
) -> None:
    """Best-effort insert into audit_log; failure is logged, not raised.

    Maps to the existing audit_log schema:
      user_id  → ``users.id`` resolved from *user_email* (pass ``user_id``
                 explicitly to skip the lookup); unresolvable emails are
                 stored as-is rather than dropping the row
      action   → action
      params   → details dict

    Routes through the ``src.repositories`` factory (``audit_repo().log()``)
    so the row lands in whichever backend (DuckDB or Postgres) the
    deployment runs on — the prior raw ``conn.execute`` always targeted the
    DuckDB system connection, silently dropping chat audit rows on
    Postgres-backed instances.
    """
    try:
        from src.repositories import audit_repo

        audit_repo().log(
            user_id=user_id if user_id is not None else _resolve_user_id(user_email),
            action=action,
            params=details,
        )
    except Exception:
        logger.exception("audit_log write failed: action=%s", action)
