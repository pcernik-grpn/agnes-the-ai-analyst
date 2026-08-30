"""Policy Engine for Universal MCP passthrough calls (RFC #461 §3).

Three independent gates, each driven by a column on ``tool_registry``:

* ``mutating`` (BOOLEAN) — when true, the tool is invokable by admins and
  by callers whose groups hold a ``tool_grants`` row with
  ``allow_mutating=TRUE`` for it (v121 — the "separate ``mutating_grant``
  row" the POC reserved). Read-only-by-default stands: a plain grant does
  not open a mutating tool; the per-tool opt-in is an explicit admin act.
  An ``AgentPrincipal`` rides its owner's groups with ``is_admin`` forced
  False, so an agent reaches exactly the mutating tools its owner's groups
  were opted into — still narrowed by its connection scope below.

* ``pii_fields`` (JSON list[str]) — recursive-redact every value whose
  *key* matches an entry in the list. Applied to both ``text`` (when
  the upstream returned JSON content) and ``data`` (the parsed dict).
  Replacement token is the string ``"[REDACTED]"`` — picked so it
  survives JSON round-trip and is grep-able in audit logs.

* ``rate_limit_pm`` (INT, per-minute, per-user, per-tool) — in-memory
  token bucket keyed on ``(tool_id, user_id)``. Cleared on app restart
  (the cowork pattern: rate-limit per session, not per ever). When the
  count of timestamps within the last 60s reaches the cap, ``check``
  returns the seconds-until-next-slot for the caller's 429 Retry-After.

Each helper is independent and pure-ish (rate-limit uses a module-level
dict guarded by a lock). The invoke endpoint wires them in order:
mutating → rate-limit → forward → redact response.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.instance_config import get_public_url

# ---------------------------------------------------------------------------
# Mutating gate
# ---------------------------------------------------------------------------


class MutatingNotAllowed(Exception):
    """Raised by ``check_mutating`` when a caller without mutating authority
    (admin, or a group grant with ``allow_mutating=TRUE``) invokes a
    mutating tool."""


def check_mutating(
    tool: Dict[str, Any],
    *,
    is_admin: bool,
    group_ids: Optional[List[str]] = None,
) -> None:
    """Raise ``MutatingNotAllowed`` unless the caller may run a mutating tool.

    No-op for tools whose registry row has ``mutating=False`` (the read-only
    default), for admin callers (curation + testing flow), and — since v121 —
    for callers whose ``group_ids`` hold a ``tool_grants`` row with
    ``allow_mutating=TRUE`` for this tool. ``group_ids`` are the caller's
    resolved groups (an ``AgentPrincipal``'s OWNER groups — the owner's
    grants are the agent's ceiling); ``None``/empty fails closed to the
    pre-v121 admin-or-bust behavior.
    """
    if not bool(tool.get("mutating", False)):
        return
    if is_admin:
        return
    if group_ids:
        from src.repositories import tool_registry_repo

        if tool_registry_repo().is_mutating_granted_to_groups(str(tool.get("tool_id")), list(group_ids)):
            return
    raise MutatingNotAllowed(
        f"tool {tool.get('tool_id')!r} is marked mutating; it requires an admin caller "
        f"or a group grant with allow_mutating=TRUE"
    )


# ---------------------------------------------------------------------------
# Grant gate + combined authorization
# ---------------------------------------------------------------------------


class GrantDenied(Exception):
    """Raised when the caller's groups have no ``tool_grants`` row for a tool."""


class ConnectionNotInScope(GrantDenied):
    """Raised when an agent whose ``connections_mode='selected'`` reaches a
    tool belonging to an MCP source outside its ``connection`` scope.

    Subclasses ``GrantDenied`` so every existing handler (REST → 403, MCP
    transports → tool error) maps it correctly without a new except-arm; the
    distinct type + message keep the *reason* diagnosable.
    """


class SourceNotGranted(GrantDenied):
    """Raised when the caller's groups hold no ``ResourceType.MCP_SOURCE``
    grant for the tool's source (TCRD-236), and the source is not exempt
    under the backward-compat default (see
    :func:`visible_mcp_source_ids`).

    Subclasses ``GrantDenied`` for the same reason ``ConnectionNotInScope``
    does — every existing handler already maps that exception to the right
    transport error with no new except-arm.
    """


@dataclass(frozen=True)
class CallerAuthority:
    """Who a passthrough call runs as, normalized across caller shapes.

    ``enforce_passthrough_access`` and ``_visible_passthrough_tools`` both
    accept a bare user id (transports), a user dict (REST), or a restricted
    ``Principal`` (co-session / agent-session token). This collapses all three
    into the three facts the gates need, so the listing and the call seam can
    never disagree about who the caller is.
    """

    #: Whose group memberships authorize the call. For an ``AgentPrincipal``
    #: this is the OWNER — an agent's authority is a restriction of its
    #: owner's, so the owner's ``tool_grants`` are the ceiling.
    user_id: Optional[str]
    #: NEVER true for a restricted principal, even when the resolved
    #: ``user_id`` belongs to an admin: an admin-owned agent must not inherit
    #: the tool-grant / mutating short-circuit.
    is_admin: bool = False
    #: Set only for an ``AgentPrincipal`` — triggers the connection-scope gate.
    agent_id: Optional[str] = None
    #: True for any ``Principal``; a restricted caller with no resolvable
    #: ``user_id`` (co-session: several owners, none authoritative) fails
    #: closed everywhere.
    restricted: bool = False


def caller_authority(caller: Any) -> CallerAuthority:
    """Normalize a caller (user id / user dict / ``Principal`` / ``None``)
    into a :class:`CallerAuthority`. See that class for the semantics."""
    from app.auth.access import is_user_admin
    from app.auth.session_principal import AgentPrincipal, PRINCIPAL_TYPES

    if caller is None:
        return CallerAuthority(user_id=None)
    if isinstance(caller, PRINCIPAL_TYPES):
        if isinstance(caller, AgentPrincipal):
            return CallerAuthority(
                user_id=caller.owner_user_id or None,
                is_admin=False,
                agent_id=caller.agent_id,
                restricted=True,
            )
        # Co-session: no single owner whose per-user MCP credentials or tool
        # grants could stand in for the session. Fail closed.
        return CallerAuthority(user_id=None, restricted=True)
    user_id = caller.get("id") if isinstance(caller, dict) else caller
    user_id = str(user_id) if user_id else None
    return CallerAuthority(user_id=user_id, is_admin=bool(user_id) and is_user_admin(user_id))


def connection_scope_ids(authority: CallerAuthority) -> Optional[frozenset]:
    """The MCP source ids this caller may reach, or ``None`` for no filter.

    ``None`` for every non-agent caller and for an agent that leaves
    ``connections_mode='all'``; otherwise the agent's declared ``connection``
    scope (possibly empty). One helper for both the listing and the call seam
    so discovery and authorization cannot drift.
    """
    if not authority.agent_id:
        return None
    from src.agent_scope_intersection import agent_scope_filter

    return agent_scope_filter(authority.agent_id, "connections_mode", "connection")


def _mcp_source_ids_without_any_grant(source_ids: Iterable[str]) -> set:
    """Among ``source_ids``, those holding NO ``mcp_source`` grant row for
    ANY group — i.e. a source nobody has ever narrowed. Treated as
    visible-to-everyone (see :func:`visible_mcp_source_ids`), which is what
    keeps every already-registered source working the moment ``ResourceType.
    MCP_SOURCE`` ships even where the boot/registration-time grandfather
    seed (``src/mcp_source_grants.py``) has not yet run for it.
    """
    ids = set(source_ids)
    if not ids:
        return set()
    from src.repositories import resource_grants_repo

    from app.resource_types import ResourceType

    granted_anywhere = {
        g["resource_id"] for g in resource_grants_repo().list_all(resource_type=ResourceType.MCP_SOURCE.value)
    }
    return ids - granted_anywhere


def visible_mcp_source_ids(user_id: Optional[str], source_ids: Iterable[str]) -> frozenset:
    """Subset of ``source_ids`` visible to ``user_id`` under the
    ``ResourceType.MCP_SOURCE`` gate (TCRD-236): explicitly granted to one
    of the caller's groups, OR ungated (the source holds no ``mcp_source``
    grant row at all — the backward-compat default; see
    :func:`_mcp_source_ids_without_any_grant`).

    Never short-circuits for admin — that bypass is the CALLER's job (every
    caller here already branches on ``authority.is_admin`` before reaching
    this), matching every other non-short-circuiting grant primitive in
    ``app.auth.access``.
    """
    ids = set(source_ids)
    if not ids or not user_id:
        return frozenset()
    from app.auth.access import _allowed_ids_for_user
    from app.resource_types import ResourceType

    granted = _allowed_ids_for_user(user_id, ResourceType.MCP_SOURCE.value)
    ungated = _mcp_source_ids_without_any_grant(ids)
    return frozenset((granted | ungated) & ids)


def mcp_source_is_visible_to(user_id: Optional[str], source_id: Optional[str]) -> bool:
    """Single-source convenience wrapper around :func:`visible_mcp_source_ids`
    for the call seam (``enforce_passthrough_access``)."""
    if not user_id or not source_id:
        return False
    return source_id in visible_mcp_source_ids(user_id, [source_id])


def enforce_passthrough_access(tool: Dict[str, Any], caller: Any) -> None:
    """Full authorization gate for a single passthrough invocation.

    The one gate stack shared by the REST endpoint
    (``app/api/mcp_passthrough.invoke_passthrough_tool``) and the SSE /
    Streamable-HTTP transport closures (``app/api/mcp/tools_generator``), so the
    interactive-forward paths can't drift apart. Runs, in order:

    1. **grant** — admin short-circuits; otherwise the caller must be in a group
       listed in ``tool_grants`` for this tool (``GrantDenied`` on miss);
    2. **source** — the tool's MCP source must be visible to the caller under
       ``ResourceType.MCP_SOURCE`` (TCRD-236): granted to one of the caller's
       groups, or ungated (no ``mcp_source`` grant exists for it at all — the
       backward-compat default; see :func:`visible_mcp_source_ids`)
       (``SourceNotGranted``, a ``GrantDenied`` subclass — same "discovery
       must not outrun authorization" reasoning as the connection-scope gate
       below: hiding a source from the listing is not enough on its own);
    3. **mutating** — a ``mutating`` tool needs an admin caller or a caller
       group whose grant carries ``allow_mutating=TRUE``
       (``MutatingNotAllowed``);
    4. **connection scope** — an agent whose ``connections_mode='selected'``
       may only reach tools on an MCP source it declared
       (``ConnectionNotInScope``). This is the *call* seam of the same filter
       the listing applies: hiding a tool from ``tools/list`` is discovery,
       not authorization, so an agent that names an unlisted tool directly is
       refused here;
    5. **rate limit** — per-(tool, user) token bucket (``RateLimited``).

    ``caller`` is a bare user id (transports), a user dict (REST), a restricted
    ``Principal``, or ``None`` when the transport could not resolve an identity
    from the request (absent / invalid token). ``None`` — and a co-session
    principal, which has no single owner — is treated as a non-admin caller
    with no groups, so the grant check **fails closed**; an unauthenticated
    forward is never allowed. An ``AgentPrincipal`` resolves to its owner's
    groups with ``is_admin`` forced false, so an admin-owned agent gets the
    owner's grants and nothing more.

    Callers map the typed exceptions onto their transport's error surface (REST:
    403/429 HTTP; MCP transports: a tool error). Backend-aware: resolves RBAC
    through the repo factory (``tool_registry_repo``) + ``app.auth.access`` so it
    reads the active state backend (DuckDB or Postgres).
    """
    from app.auth.access import _user_group_ids
    from src.repositories import tool_registry_repo

    authority = caller_authority(caller)
    tool_id = tool.get("tool_id")
    group_ids: List[str] = []
    if not authority.is_admin:
        group_ids = list(_user_group_ids(authority.user_id)) if authority.user_id else []
        if not tool_registry_repo().is_granted_to_groups(tool_id, group_ids):
            raise GrantDenied(f"no grant on tool {tool_id!r} for your groups")
        if not mcp_source_is_visible_to(authority.user_id, tool.get("source_id")):
            raise SourceNotGranted(
                f"no grant on source {tool.get('source_id')!r} for your groups",
            )
    check_mutating(tool, is_admin=authority.is_admin, group_ids=group_ids)
    allowed_sources = connection_scope_ids(authority)
    if allowed_sources is not None and tool.get("source_id") not in allowed_sources:
        raise ConnectionNotInScope(
            f"tool {tool_id!r} belongs to a connection outside this agent's scope",
        )
    # An unresolved caller never reaches here (fails closed on grant above), so
    # the rate-bucket key always carries a real user id for identified callers.
    check_rate_limit(tool_id, authority.user_id or "", tool.get("rate_limit_pm"))


# Remedy message templates. Web-first when a public URL is configured so a
# user in web chat / Cowork gets a clickable path; CLI fallback otherwise so an
# unset public URL degrades gracefully instead of emitting a broken link. Both
# live here as constants so every transport emits identical text.
_REMEDY_WEB = (
    "You are not connected to {label!r}. Open {base}/me/connections?source={sid} and add your token, then try again."
)
_REMEDY_CLI = "You are not connected to {label!r}. Run `agnes mcp my-secret set {sid}` to connect your own account."


class PerUserCredentialMissing(Exception):
    """Raised when a ``scope='per_user'`` source is invoked by an identified
    caller who has not stored their own credential.

    ``source_label`` is the human-facing source name for the sentence;
    ``source_id`` is the opaque primary key used to build the deep link
    (``/me/connections?source=<id>``) — never the name, which would break the
    id-based page lookup and leak into browser history / referrers.
    """

    def __init__(self, source_label: str, source_id: str):
        self.source_label = source_label
        self.source_id = source_id
        base = get_public_url()
        if base:
            msg = _REMEDY_WEB.format(label=source_label, base=base, sid=source_id)
        else:
            msg = _REMEDY_CLI.format(label=source_label, sid=source_id)
        super().__init__(msg)


def _oauth_credential_missing(source_id: str, caller_user_id: str) -> bool:
    """True iff the caller's ``mcp_user_oauth_tokens`` row is absent, OR
    present but expired with no refresh token to renew it at call time
    (2026-07-30 outbound MCP OAuth sources spec §4).

    A present-but-expired row that STILL carries a refresh token is not
    "missing" — ``connectors.mcp.client`` refreshes it transparently on the
    next forward. Only the unrefreshable case (dead end, forces re-connect)
    counts as missing, same bar as ``invalid_grant`` deleting the row
    outright in the client's refresh path.
    """
    from src.repositories import mcp_user_oauth_tokens_repo

    row = mcp_user_oauth_tokens_repo().get(source_id, caller_user_id)
    if row is None:
        return True
    expires_at = row.get("expires_at")
    if expires_at is None:
        return False  # non-expiring / unknown expiry — treat as present
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at > datetime.now(timezone.utc):
        return False  # not expired yet
    if not row.get("refresh_token"):
        return True
    # Expired WITH a refresh token — renewable only if the source still has
    # an OAuth client registration to refresh against. Without one the client
    # would forward the stale token and surface an opaque upstream 401
    # instead of this remedy (Devin Review on #1124).
    from src.repositories import mcp_source_oauth_clients_repo

    return mcp_source_oauth_clients_repo().get(source_id) is None


def oauth_connection_usable(source_id: str, caller_user_id: str) -> bool:
    """Status-surface twin of :func:`_oauth_credential_missing`: True iff the
    caller's OAuth connection would pass ``enforce_per_user_credential`` right
    now. The /me/connections page, the admin "Your connection" card, and
    ``GET …/my-secret`` must use THIS — not bare row existence — so a lapsed,
    unrenewable connection never shows a green "Connected" badge while every
    actual call 403s (Devin Review on #1130)."""
    return not _oauth_credential_missing(source_id, caller_user_id)


def enforce_per_user_credential(source: Dict[str, Any], caller_user_id: Optional[str]) -> None:
    """Fail closed when a ``per_user`` source lacks the caller's own credential.

    For a ``scope='per_user'`` source an identified caller (admin included —
    data scoping is per identity) must have their own stored credential;
    otherwise the forward would connect with no token (see
    ``connectors.mcp.client._lookup_secret_for_source``, which returns ``None``
    for a per_user source with an identified caller and no row — it does NOT
    borrow the shared credential). Refuse here with an actionable message
    instead of letting it degrade to an opaque upstream auth error.

    ``auth_method='oauth'`` sources (always ``scope='per_user'`` — enforced
    at ``MCPSourceRepository.upsert()``) route through
    :func:`_oauth_credential_missing` instead of the secret-vault lookup —
    same ``PerUserCredentialMissing`` exception, same remedy string, so
    every caller-facing 403 reads identically regardless of credential kind.

    Shared by the REST endpoint and the SSE / Streamable-HTTP transport
    closures so the pre-forward guard can't drift. No-op for shared sources and
    for the caller-less (materialize) path, which legitimately rides the shared
    vault. Raises ``PerUserCredentialMissing``.
    """
    if (source.get("scope") or "shared").lower() != "per_user":
        return
    if not caller_user_id:
        # Caller-less materialize path — shared vault is the intended source.
        return
    if (source.get("auth_method") or "").lower() == "oauth":
        if _oauth_credential_missing(source["id"], caller_user_id):
            raise PerUserCredentialMissing(
                source_label=source.get("name") or source["id"],
                source_id=source["id"],
            )
        return

    from src.repositories import per_user_secrets_repo

    if not per_user_secrets_repo().get(source["id"], caller_user_id):
        raise PerUserCredentialMissing(
            source_label=source.get("name") or source["id"],
            source_id=source["id"],
        )


# ---------------------------------------------------------------------------
# Source url policy — runtime enforcement (#1216 part 2)
# ---------------------------------------------------------------------------


class SourceUrlRefused(Exception):
    """Raised by :func:`enforce_source_url_runtime_policy` when
    ``mcp.source_url_runtime_enforce`` is on and a source's ``url`` fails the
    DNS-free half of the url policy at a forward seam.

    The three facts an operator needs to route themselves from a broken tool
    call to a fix live on the exception itself, not only folded into
    ``str()``: ``reason`` (the policy verdict, e.g.
    ``address_in_blocked_range: 169.254.169.254``), ``admin_report_hint``
    (where the row was already flagged, before this switch was ever turned
    on), and ``switch`` (the name to flip off to restore the previous,
    unenforced behavior). That split is what lets the REST seam
    (``app/api/mcp_passthrough.py``) render a structured JSON body from the
    attributes directly, while the MCP transport seam
    (``app/api/mcp/tools_generator.py``) falls back to the formatted message
    string — FastMCP tool errors are plain text.
    """

    def __init__(self, source: Dict[str, Any], reason: str):
        self.source_id = source.get("id")
        self.source_name = source.get("name") or self.source_id
        self.reason = reason
        self.switch = "mcp.source_url_runtime_enforce"
        self.admin_report_hint = (
            "GET /api/admin/mcp-sources (or `agnes admin mcp source list`) already "
            "flagged this source's url_policy_verdict as 'would_refuse' before this "
            "switch was turned on"
        )
        super().__init__(
            f"MCP source {self.source_name!r} url refused by the current url policy "
            f"({reason}); {self.admin_report_hint}. Fix the source's url, or turn off "
            f"the {self.switch} switch to restore the previous (unenforced) behavior."
        )


def enforce_source_url_runtime_policy(source: Dict[str, Any]) -> None:
    """Runtime twin of the admin-time ``check_source_url`` gate (#1216).

    ``check_source_url`` gates a source's ``url`` only when it is
    CONFIGURED — a row registered before the guard existed, or before
    ``mcp.source_url_strict`` was turned on, keeps forwarding credentials to
    an address the CURRENT policy would refuse, until an admin happens to PUT
    the row. Behind ``mcp.source_url_runtime_enforce`` (default OFF — see
    ``app.instance_config.get_mcp_source_url_runtime_enforce``), this runs
    the DNS-free half of that policy
    (:func:`src.net.mcp_source_url.check_source_url_dns_free`) at BOTH
    credentialed forward seams — the SSE / Streamable-HTTP transport
    closures (``app/api/mcp/tools_generator.py``) and the REST passthrough
    endpoint (``app/api/mcp_passthrough.py``) — sharing this one helper so
    the two cannot drift apart, the drift class this repo keeps re-fixing.

    DNS-free ONLY, deliberately: a hot forward path must not pay for (or
    block on) a blocking ``getaddrinfo`` call on every dial, so an ordinary
    hostname that has not been resolved here answers ``ok`` regardless of
    what it would really resolve to — the same trade-off the admin
    ``url_policy_verdict`` report already accepts (see the module docstring
    of ``src/net/mcp_source_url.py``). What it does catch, with no resolver
    at all, is the shape #1154 is about: a literal link-local/reserved
    address (``http://169.254.169.254/mcp``), or cleartext http to a literal
    public address.

    No-op for ``stdio``: its ``url`` (if any) is inert documentation nothing
    ever dials — the same exemption every other caller of this policy
    applies. Raises :class:`SourceUrlRefused` on a refused url; returns
    ``None`` otherwise (switch off, non-network transport, or an accepted
    verdict).
    """
    from app.instance_config import (
        get_mcp_source_url_runtime_enforce,
        get_mcp_source_url_strict,
        get_ssrf_allowed_hosts,
    )

    if not get_mcp_source_url_runtime_enforce():
        return
    if (source.get("transport") or "") not in ("http", "sse"):
        return

    from src.net.mcp_source_url import check_source_url_dns_free

    verdict = check_source_url_dns_free(
        source.get("url") or "",
        strict=get_mcp_source_url_strict(),
        allowed_hosts=get_ssrf_allowed_hosts(),
    )
    if verdict.ok:
        return
    raise SourceUrlRefused(source, verdict.reason)


# ---------------------------------------------------------------------------
# PII redaction
# ---------------------------------------------------------------------------

REDACTED_TOKEN = "[REDACTED]"


def redact_pii(value: Any, pii_keys: Iterable[str]) -> Any:
    """Return ``value`` with every entry whose *key* is in ``pii_keys`` masked.

    Recurses through nested dicts and lists. Non-container values pass
    through unchanged — redaction is keyed off the parent's key, not the
    value's content. The match is case-sensitive and exact, mirroring
    how analysts spell column names when they fill ``pii_fields`` on the
    registry row.

    A shallow copy is returned for containers so the caller can keep a
    pristine copy of the upstream payload (useful when the result also
    feeds the audit log on a future iteration).
    """
    keys = set(pii_keys or [])
    if not keys:
        return value
    return _redact_recursive(value, keys)


def _redact_recursive(value: Any, keys: set) -> Any:
    if isinstance(value, dict):
        return {k: (REDACTED_TOKEN if k in keys else _redact_recursive(v, keys)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_recursive(v, keys) for v in value]
    return value


def redact_response(
    *,
    text: str,
    data: Optional[Any],
    pii_fields: Optional[List[str]],
) -> Tuple[str, Optional[Any]]:
    """Apply PII redaction to both ``data`` and ``text`` consistently.

    When the upstream returned valid JSON, ``text`` is the serialized form
    of ``data`` — so we redact ``data`` and re-serialize for ``text``,
    keeping the two in sync. When ``data`` is None (non-JSON text), we
    leave ``text`` unchanged since key-based redaction has no meaning on
    a flat string. A future iteration can layer regex-based redaction
    for free-form text.
    """
    if not pii_fields:
        return text, data
    if data is None:
        return text, data
    redacted = redact_pii(data, pii_fields)
    return json.dumps(redacted), redacted


# ---------------------------------------------------------------------------
# Per-(tool, user) rate limit — in-memory token bucket
# ---------------------------------------------------------------------------

_RATE_BUCKETS: Dict[Tuple[str, str], deque] = {}
_RATE_LOCK = threading.Lock()
_WINDOW_SECONDS = 60.0


class RateLimited(Exception):
    """Raised by ``check_rate_limit`` when the caller has hit the per-minute cap.

    ``retry_after_seconds`` is set on the instance so the HTTP layer can
    surface it in a Retry-After header — RFC 6585 §4 (Status 429).
    """

    def __init__(self, retry_after_seconds: float):
        super().__init__(f"rate limit exceeded; retry after {retry_after_seconds:.1f}s")
        self.retry_after_seconds = retry_after_seconds


def check_rate_limit(
    tool_id: str,
    user_id: str,
    cap_per_minute: Optional[int],
    *,
    now: Optional[float] = None,
) -> None:
    """Raise ``RateLimited`` if the caller has used ``cap_per_minute`` slots
    in the past 60 seconds. Records this call as a fresh slot on success.

    ``cap_per_minute`` of ``None`` or ``<=0`` disables the gate. The bucket
    is keyed on ``(tool_id, user_id)`` so two different tools share no
    quota, and two different callers don't fight for the same slot pool.
    """
    if not cap_per_minute or cap_per_minute <= 0:
        return
    now = now if now is not None else time.monotonic()
    key = (tool_id, user_id)
    cutoff = now - _WINDOW_SECONDS
    with _RATE_LOCK:
        bucket = _RATE_BUCKETS.setdefault(key, deque())
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= cap_per_minute:
            # The next free slot opens when the oldest timestamp ages out.
            retry_after = (bucket[0] + _WINDOW_SECONDS) - now
            raise RateLimited(max(retry_after, 0.0))
        bucket.append(now)


def reset_rate_buckets_for_tests() -> None:
    """Test-only: clear the module-level state between tests."""
    with _RATE_LOCK:
        _RATE_BUCKETS.clear()
