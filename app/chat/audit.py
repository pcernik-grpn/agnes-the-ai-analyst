"""audit_log writer for chat events. Re-uses Agnes's existing audit table."""

from __future__ import annotations

import logging
import time
from typing import Any

from src.audit_context import run_without_request_timing
from src.audit_helpers import hash_args  # noqa: F401 — re-exported, see below

logger = logging.getLogger(__name__)

#: Default for ``write_audit(duration_ms=...)``: leave the value to the
#: repository's request-context autofill. Distinct from ``None``, which is
#: an explicit "unmeasured" and is stored as NULL — see ``write_audit``.
AUTOFILL_DURATION: Any = object()

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
    duration_ms: int | None | Any = AUTOFILL_DURATION,
    result: str | None = None,
) -> None:
    """Best-effort insert into audit_log; failure is logged, not raised.

    Maps to the existing audit_log schema:
      user_id     → ``users.id`` resolved from *user_email* (pass ``user_id``
                    explicitly to skip the lookup); unresolvable emails are
                    stored as-is rather than dropping the row
      action      → action
      params      → details dict
      duration_ms → three intents. An ``int`` is the event's own measured
                    wall time (``chat.tool_call`` times call→result).
                    ``None`` is "explicitly unmeasured" and is stored as a
                    real NULL whatever context the caller runs in — the
                    repositories would otherwise autofill it from the
                    request context, and a chat pump task inherits the
                    context of the HTTP handler that created it, so an
                    unfinished tool call flushed much later would carry an
                    unrelated request's age (``src.audit_context.
                    run_without_request_timing``). Leaving the default
                    keeps the repository autofill, which is right for the
                    chat events written from inside their own request
                    (approval decisions, kills, ...).
      result      → ``success`` / ``error…`` per ``src.audit_helpers.
                    RESULT_CLASS_CASE_SQL``; ``None`` when no verdict exists

    Routes through the ``src.repositories`` factory (``audit_repo().log()``)
    so the row lands in whichever backend (DuckDB or Postgres) the
    deployment runs on — the prior raw ``conn.execute`` always targeted the
    DuckDB system connection, silently dropping chat audit rows on
    Postgres-backed instances.
    """
    try:
        from src.repositories import audit_repo

        kwargs = dict(
            user_id=user_id if user_id is not None else _resolve_user_id(user_email),
            action=action,
            params=details,
            result=result,
        )
        if duration_ms is AUTOFILL_DURATION:
            audit_repo().log(**kwargs)
        elif duration_ms is None:
            run_without_request_timing(audit_repo().log, duration_ms=None, **kwargs)
        else:
            audit_repo().log(duration_ms=duration_ms, **kwargs)
    except Exception:
        logger.exception("audit_log write failed: action=%s", action)
