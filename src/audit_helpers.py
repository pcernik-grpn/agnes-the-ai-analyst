"""Shared helpers for audit logging."""

import logging

from app.auth.scheduler_token import SCHEDULER_USER_EMAIL

logger = logging.getLogger(__name__)


def log_safe(**kwargs) -> None:
    """``audit_repo().log(**kwargs)``, never raising.

    The audit trail is best-effort by policy: a failed audit write must not
    fail the request it describes. That try/except idiom is open-coded at
    dozens of call sites (e.g. ``app/api/data.py``); new code should call
    this instead of adding another copy, so the failure policy lives in one
    place.
    """
    from src.repositories import audit_repo

    try:
        audit_repo().log(**kwargs)
    except Exception:
        logger.exception("audit_log write failed for %s; continuing", kwargs.get("action", "<unknown action>"))


# One scheduler rule for the whole codebase — the same predicate
# AuditRepository.last_scheduler_tick() has always used. Facet/KPI/timeline
# classification must not maintain a second (stale) list of action names.
SCHEDULER_ACTION_SQL = "(action LIKE 'run_%' OR action = 'marketplace.sync_all')"

# Row → source bucket. Plain SQL, identical semantics on DuckDB and Postgres.
AUDIT_SOURCE_CASE_SQL = (
    "CASE "
    "WHEN client_kind IS NOT NULL AND client_kind != '' THEN client_kind "
    f"WHEN {SCHEDULER_ACTION_SQL} THEN 'scheduler' "
    "WHEN user_id IS NULL THEN 'system' "
    "ELSE 'other' END"
)

# Row → result class. Read-side classification only — raw result values are
# preserved; see classify_result() for the Python mirror the guard test pins.
RESULT_CLASS_CASE_SQL = (
    "CASE "
    "WHEN result IS NULL THEN 'none' "
    "WHEN result IN ('success', 'ok') THEN 'success' "
    "WHEN result LIKE 'error%' THEN 'error' "
    "WHEN result IN ('denied', 'blocked', 'invalid_password', 'deactivated') THEN 'denied' "
    "ELSE 'other' END"
)

RESULT_CLASSES = ("success", "error", "denied", "none", "other")

# The physical trail tables folded into the unified Activity Center timeline
# (``AuditRepository.query_unified`` / ``AuditPgRepository.query_unified``,
# E3 slice 2). ``chat_messages`` is deliberately NOT in this list — customer
# data in transcripts, no admin viewer by design (see docs/observability.md).
UNIFIED_TRAILS = ("audit", "sync", "llm", "agent_scope")


def classify_result(value: "str | None") -> str:
    """Python mirror of RESULT_CLASS_CASE_SQL (kept in lockstep by tests)."""
    if value is None:
        return "none"
    if value in ("success", "ok"):
        return "success"
    if value.startswith("error"):
        return "error"
    if value in ("denied", "blocked", "invalid_password", "deactivated"):
        return "denied"
    return "other"


def client_kind_from_user(user) -> str:
    """Detect CLI vs web vs scheduler from the auth state.

    Order of precedence:
    1. scheduler user → 'scheduler'
    2. PAT-authenticated (token_type='pat' set by get_current_user), or the
       X-StorageApi-Token header credential (token_type='keboola_token',
       Task 7) → 'cli'. Both are non-interactive, programmatic credentials —
       an audit trail that read the header path as an interactive browser
       session ('web') would misrepresent a stack credential as a human
       clicking through the UI.
    3. anything else → 'web'

    ``user`` is a plain dict for almost every caller, but a restricted
    principal (``SessionPrincipal`` / ``AgentPrincipal`` — co-session or
    agent-session, V1d) is a frozen dataclass with no ``.get``. Neither kind
    of token is ever PAT- or scheduler-authenticated, so "web" is the
    correct answer without needing to know which principal shape it is —
    this must stay a supertype-agnostic ``not isinstance(user, dict)`` check
    (not an import of ``PRINCIPAL_TYPES``) so a future principal kind can't
    reintroduce this crash by omission.
    """
    if user is None or not isinstance(user, dict):
        return "web"
    if user.get("email") == SCHEDULER_USER_EMAIL:
        return "scheduler"
    if user.get("token_type") in ("pat", "keboola_token"):
        return "cli"
    return "web"


def identity_for_audit(user) -> tuple:
    """``(user_id, email)`` for audit-log rows and quota-key bookkeeping
    only — NEVER for an authorization decision (a narrowed principal must
    not inherit its owner's admin bit; see ``_bq_guardrail_inputs`` in
    ``app/api/query.py``).

    A restricted principal (co-session / agent-session, V1d) is a frozen
    dataclass with no ``.get``: an ``AgentPrincipal`` reports its owner
    (the request legitimately runs on the owner's behalf, just
    intersection-narrowed); a ``SessionPrincipal`` reports neither.
    Supertype-agnostic ``isinstance(user, dict)`` check for the same
    future-proofing reason as ``client_kind_from_user`` above.
    """
    if user is None:
        return None, None
    if not isinstance(user, dict):
        return getattr(user, "owner_user_id", None), getattr(user, "owner_email", None)
    return user.get("id"), user.get("email")
