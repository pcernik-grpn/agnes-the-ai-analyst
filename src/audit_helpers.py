"""Shared helpers for audit logging."""

import hashlib
import json
import logging
import threading
from typing import Any

from app.auth.scheduler_token import SCHEDULER_USER_EMAIL

logger = logging.getLogger(__name__)


def hash_args(args: Any) -> str:
    """Return first 16 hex chars of SHA-256 of the JSON-serialised args.

    Moved here from ``app.chat.audit`` (F2c — audit-full-coverage plan, Task
    5) so the MCP tool-call dispatch wrapper (``app.api.mcp.tools_generator
    .install_tool_call_audit``) can reuse it without importing the
    chat-specific module. ``app.chat.audit.hash_args`` re-exports this same
    function — existing call sites there are unaffected.
    """
    raw = json.dumps(args, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


# Every value ``client_kind_from_user`` / ``src.audit_context.set_client_kind``
# may produce, and every surface a caller can stamp explicitly (F0 —
# audit-full-coverage plan, Task 1). Kept as the single source of truth so a
# new surface doesn't invent its own ad-hoc string.
CLIENT_KINDS = ("web", "cli", "mcp", "slack", "telegram", "agent", "broker", "scheduler", "system")


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
    """Detect CLI vs web vs mcp vs scheduler from the auth state.

    Order of precedence:
    1. scheduler user → 'scheduler'
    2. an MCP-OAuth session JWT (token_type='mcp_oauth', stamped by
       ``app.auth.pat_resolver.resolve_token_to_user`` when the JWT's
       ``scope`` claim is ``mcp-oauth`` — see
       ``app.auth.mcp_oauth.AgnesMCPOAuthProvider``'s ``exchange_*`` mints)
       → 'mcp'. Checked before the PAT check below since an MCP connector
       token is not a PAT and must not be misclassified as one.
    3. PAT-authenticated (token_type='pat' set by get_current_user), or the
       X-StorageApi-Token header credential (token_type='keboola_token',
       Task 7) → 'cli'. Both are non-interactive, programmatic credentials —
       an audit trail that read the header path as an interactive browser
       session ('web') would misrepresent a stack credential as a human
       clicking through the UI.
    4. anything else → 'web'

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
    if user.get("token_type") == "mcp_oauth":
        return "mcp"
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


# ---------------------------------------------------------------------------
# should_sample — per-action sampling gate (Wave 2 — Task 4, audit-coverage
# plan). An operator's only lever against a noisy action besides retention.
# ---------------------------------------------------------------------------

#: Per-action call counters backing the deterministic sampler below. Module
#: state on purpose (mirrors a real process's lifetime), guarded by
#: ``_SAMPLE_LOCK`` for the rare case of concurrent writers on the same
#: action. Tests reach in and ``.clear()`` this between cases.
_SAMPLE_COUNTS: dict[str, int] = {}
_SAMPLE_LOCK = threading.Lock()


def should_sample(action: str) -> bool:
    """True when *this* call for *action* should actually write its audit row.

    Reads ``audit.sampling.<action>`` from instance config (see the
    ``audit:`` block in ``config/instance.yaml.example``) as a ratio in
    ``[0, 1]``:

    - **Unconfigured (the default for every action)** -> always ``True``.
      Sampling is strictly opt-in per action; nothing is throttled unless an
      operator explicitly lists it.
    - ``1.0`` -> always ``True`` (an explicit no-op entry).
    - ``0`` -> always ``False`` — the supported way to silence a noisy,
      low-stakes action entirely without touching its call site.
    - Anything in between -> ``True`` on exactly 1 of every
      ``round(1 / ratio)`` calls.

    Deterministic via a per-action call counter — **not** ``random`` — on
    purpose: a random sampler makes "1 in 10" untrue on a low-traffic
    instance (the 10th call might just never land heads) and makes any test
    of it flaky. A counter makes the ratio exact and reproducible.

    SECURITY: never configure this for an action that matters for security
    or compliance (auth, RBAC/grant changes, secret rotation, admin
    configuration) — see the caveat next to the worked example in
    ``config/instance.yaml.example``. This function has no way to enforce
    that; it is a documentation-level contract for whoever edits the config.
    """
    from app.instance_config import get_value

    sampling = get_value("audit", "sampling", default=None) or {}
    ratio = sampling.get(action) if isinstance(sampling, dict) else None
    if ratio is None:
        return True
    try:
        ratio = float(ratio)
    except (TypeError, ValueError):
        return True
    if ratio >= 1:
        return True
    if ratio <= 0:
        return False

    every_n = max(1, round(1 / ratio))
    with _SAMPLE_LOCK:
        count = _SAMPLE_COUNTS.get(action, 0) + 1
        _SAMPLE_COUNTS[action] = count
    return count % every_n == 1
