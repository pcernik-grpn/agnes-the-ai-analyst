"""Admin endpoints — table discovery, registry management, instance configuration.

Every gate on this router uses ``require_admin`` from ``app.auth.access``,
which checks Admin user_group membership for both OAuth session and PAT
callers via the same ``_user_group_ids`` lookup.
"""

import glob
import json
import logging
import math
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional

import duckdb
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from app.switches import SWITCHES
from connectors.databricks.client import validate_workspace_host
from connectors.snowflake.settings import (
    SF_PRIVATE_KEY_ENV,
    SF_PRIVATE_KEY_PASSPHRASE_ENV,
    SF_TOKEN_ENV,
)
from src.audit_helpers import client_kind_from_user, log_safe
from src.identifier_validation import (
    is_safe_identifier as _is_safe_identifier,
)
from src.identifier_validation import (
    is_safe_quoted_identifier as _is_safe_quoted_identifier,
)
from src.repositories import (
    audit_repo,
    knowledge_repo,
    profile_repo,
    store_entities_repo,
    store_submissions_repo,
    sync_state_repo,
    table_registry_repo,
    usage_repo,
    user_store_installs_repo,
)
from src.scheduler import is_valid_schedule
from src.sql_safe import is_safe_project_id as _is_safe_project_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])

# Serializes the read-modify-write of state/instance.yaml across the two
# endpoints that mutate the overlay (POST /server-config and POST /configure).
# Without it, two admins saving concurrently would each read the same overlay
# snapshot, merge their disjoint patches, and the second os.replace would silently
# drop the first patch. Single-process FastAPI workers; multi-worker deployments
# would need an OS-level file lock — documented limitation.
_overlay_write_lock = threading.Lock()

# Per-processor advisory locks for /api/admin/run-session-processor.
# Two trigger paths exist for the same processor (scheduler tick + manual
# admin POST). Without serialization, overlapping runs would re-process the
# same /data/user_sessions/* set, double-call the LLM, and pile up duplicate
# `verification_evidence` rows — the dedup short-circuit in
# VerificationProcessor only catches the create+contradiction branches, not
# create_evidence (per ADR Decision 3, which expects evidence to accumulate
# per distinct verification event). Lock is non-blocking → second caller
# gets 409 Conflict so the operator sees what happened instead of stacking
# behind a long-running tick.
_processor_run_locks: dict[str, threading.Lock] = {}
_processor_run_locks_mutex = threading.Lock()


def _get_processor_run_lock(name: str) -> threading.Lock:
    """Per-name lock factory; the registry mutex guards dict insertion so
    two threads simultaneously asking for a never-seen processor don't
    each install their own lock instance."""
    with _processor_run_locks_mutex:
        if name not in _processor_run_locks:
            _processor_run_locks[name] = threading.Lock()
        return _processor_run_locks[name]


def _session_processor_max_per_run() -> Optional[int]:
    """Cap on sessions processed per `/run-session-processor` invocation.

    A burst of session closures landing in the same scheduler tick would
    otherwise run unboundedly in one request — each candidate can trigger
    multiple synchronous, blocking LLM calls (verification), holding the
    handling thread and competing for CPU with request-serving for the
    whole batch duration. Configurable via SESSION_PROCESSOR_MAX_PER_RUN;
    "" or an invalid value disables the cap (returns None) rather than
    failing the request — this is a protective default, not a hard
    contract, so a misconfigured env var shouldn't 500 every scheduler tick.
    Default 50: comfortably above a normal tick's session count, low enough
    to bound worst-case tick duration under a burst or backlog.
    """
    raw = os.environ.get("SESSION_PROCESSOR_MAX_PER_RUN", "50")
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.warning("SESSION_PROCESSOR_MAX_PER_RUN=%r is not an integer; cap disabled", raw)
        return None
    if value <= 0:
        return None
    return value


# SSRF protection: reject private/internal URLs for keboola_url
import ipaddress as _ipaddress  # noqa: E402
import socket as _socket  # noqa: E402
from urllib.parse import urlparse as _urlparse  # noqa: E402


def _validate_url_not_private(url: str, field_name: str = "url") -> None:
    """Raise 400 if the URL host points to a private/reserved network.

    Uses DNS resolution + ipaddress checks instead of hostname regex,
    which correctly handles all IPv4/IPv6 addresses including abbreviated
    forms (fe80::1, ::1, etc.) and DNS rebinding (resolves at check time).
    """
    try:
        parsed = _urlparse(url)
    except Exception:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}: not a valid URL")
    host = parsed.hostname or ""
    if not host:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}: missing hostname")

    # Deployer-trusted internal hosts (e.g. an on-prem GitHub Enterprise on a
    # private network) opt out of the private/reserved-network rejection. This
    # is the shared validator, so a listed host is exempt on EVERY SSRF-guarded
    # admin URL that reaches here — clone URLs (marketplace + initial-workspace),
    # the Keboola stack_url, and the _validate_urls_in_patch config fields — not
    # just marketplace clones. The allowlist is operator-set and empty by
    # default, so the OSS ships fail-closed. See
    # app.instance_config.get_ssrf_allowed_hosts.
    from app.instance_config import get_ssrf_allowed_hosts

    if host.lower() in get_ssrf_allowed_hosts():
        return

    # Reject well-known dangerous hostnames before DNS resolution
    if host.lower() in ("localhost", "localhost.localdomain"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name}: must not point to a private or reserved network",
        )

    # Resolve hostname to IP addresses and check each one
    try:
        addrinfos = _socket.getaddrinfo(host, None, proto=_socket.IPPROTO_TCP)
    except Exception:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name}: could not resolve hostname",
        )

    for family, _type, _proto, _canonname, sockaddr in addrinfos:
        ip_str = sockaddr[0]
        try:
            ip = _ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {field_name}: must not point to a private or reserved network",
            )


def _unescape_shell_quoting(s: str | None) -> str | None:
    """Defensive normalization for descriptions arriving via shell-quoting tooling.

    Some operators register tables with bash/curl invocations whose quoting
    injects literal backslash escapes into the payload (e.g. ``Don\\'t`` or
    embedded ``\\n`` instead of real newlines). The backend would otherwise
    persist those bytes verbatim and the UI would render them verbatim too.
    Mirrored in JS as ``unescapeShellQuoting`` in
    ``app/web/templates/admin_tables.html`` for already-stored rows.
    """
    if not s:
        return s
    # Order matters: protect real backslashes first.
    SENTINEL = "\x00"
    return (
        s.replace("\\\\", SENTINEL)
        .replace("\\n", "\n")
        .replace("\\r", "\r")
        .replace("\\t", "\t")
        .replace("\\'", "'")
        .replace('\\"', '"')
        .replace(SENTINEL, "\\")
    )


def _normalize_primary_key(v):
    """Coerce a string primary_key to ``[v]`` for backward compatibility.

    The 0.14.0 contract is ``Optional[List[str]]`` so composite primary keys
    (e.g. session-grain tables keyed on ``(session_id, event_date)``) round-
    trip cleanly. Pre-0.14.0 callers sent a single string; Pydantic v2
    refuses to coerce, so without this validator a CLI script posting
    ``"primary_key": "session_id"`` would now hit a 422. Wrap a bare string
    in a one-element list so old and new callers both work.
    """
    if v is None:
        return v
    if isinstance(v, str):
        return [v]
    return v


# Patches to these section paths must pass _validate_url_not_private. The
# tuple is `(section, *intermediate_keys, leaf_key)` — same SSRF gate the
# /configure wizard applies to keboola_url, so an admin can't sneak
# http://169.254.169.254/ in via the server-config editor's data_source patch.
#
# Intentionally NOT included: ``("ai", "base_url")``. The openai_compat
# provider legitimately points at internal services (LiteLLM proxy on a
# private network, on-cluster vLLM endpoint, etc.) — see
# config/instance.yaml.example "LiteLLM proxy" example. SSRF blocking
# would break those valid setups. Operators with stricter posture should
# enforce the constraint upstream (firewall / egress proxy allowlist).
# Devin ANALYSIS_0001 on PR #141 5f649a4 review.
_URL_BEARING_FIELDS: tuple[tuple[str, ...], ...] = (
    ("data_source", "keboola", "stack_url"),
    ("data_source", "databricks", "host"),
    ("marketplace", "curators_url"),
    ("auth", "keboola", "stack_url"),
    ("auth", "keboola", "oauth_host"),
)


def _validate_urls_in_patch(sections: Dict[str, Dict[str, Any]]) -> None:
    """Apply SSRF protection to every URL-bearing field present in the patch.

    Walks each registered ``(section, *path, leaf)`` against the incoming
    patch and runs ``_validate_url_not_private`` on any string value found.
    Missing intermediate keys / non-dict nodes are silently skipped — the
    patch hasn't touched that field, no validation needed.
    """
    for path in _URL_BEARING_FIELDS:
        section = path[0]
        if section not in sections:
            continue
        node: Any = sections[section]
        for key in path[1:-1]:
            if not isinstance(node, dict) or key not in node:
                node = None
                break
            node = node[key]
        if isinstance(node, dict):
            value = node.get(path[-1])
            if isinstance(value, str) and value:
                field_name = ".".join(path)
                # The auth-provider URLs carry credentials at use time (the
                # Storage token on verify, the OAuth token exchange) and are
                # held to the same bar as the source-connection sibling
                # (_validate_stack_url: "Rejects non-https") — scheme checked
                # at store time, host at store AND use time (Devin Review on
                # PR #1288).
                if path[:2] == ("auth", "keboola") and not value.lower().startswith("https://"):
                    raise HTTPException(status_code=422, detail=f"{field_name} must be https")
                if path == ("data_source", "databricks", "host"):
                    try:
                        value = validate_workspace_host(value)
                    except ValueError as exc:
                        raise HTTPException(status_code=422, detail=f"Invalid {field_name}: {exc}") from exc
                    node[path[-1]] = value
                _validate_url_not_private(value, field_name=field_name)


def _normalize_provider_names(value: Any) -> "list[str] | None":
    """Provider names from the list or the comma-separated string form (matching
    the runtime resolver `configured_allowlist`). ``None`` when the value is
    null/unset. Raises 422 for a scalar the runtime couldn't parse either."""
    if value is None:
        return None
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    raise HTTPException(
        status_code=422,
        detail="auth.providers must be a list or comma-separated string of provider names (or omitted entirely)",
    )


def _validate_auth_providers_in_patch(sections: Dict[str, Dict[str, Any]]) -> None:
    """Refuse an auth-section overlay write that would name no usable sign-in
    method (Devin review on #1288): an empty or all-unknown ``auth.providers``,
    and — whenever the auth section is patched at all — an effective allowlist
    whose named providers are none of them actually available (e.g.
    ``[keboola]`` after its stack config was cleared in a separate save).

    This is NOT the lockout backstop. The runtime fails open on exactly this
    shape: ``provider_registry._rescue_if_unusable`` treats an allowlist with
    zero usable providers as unset (all sign-in methods) with a loud error
    log, so a config that slips past this validator does not lock anyone out
    — it silently means "all providers", the opposite of what the operator
    wrote. The 422 exists so the operator learns that at save time instead of
    shipping it. Known blind spot, caught by that runtime rescue rather than
    here: a patch that never touches ``auth`` (e.g. clearing
    ``data_source.keboola.stack_url``, which ``auth.keboola.stack_url`` falls
    back to) skips this validator entirely, as does the env /
    static-instance.yaml path."""
    auth = sections.get("auth")
    if not isinstance(auth, dict):
        return

    # A non-dict keboola block would crash the availability merge below; reject
    # it with a clear message rather than a 500.
    kb = auth.get("keboola")
    if kb is not None and not isinstance(kb, dict):
        raise HTTPException(status_code=422, detail="auth.keboola must be an object (a map of settings)")

    from app.auth.provider_registry import KNOWN_PROVIDERS

    if "providers" in auth:
        names = _normalize_provider_names(auth["providers"])
        if names is None:
            return  # explicit null clears the override → no allowlist, can't lock out
        if not names:
            raise HTTPException(
                status_code=422,
                detail="auth.providers must not be empty — omit it entirely to keep all sign-in methods",
            )
        if not any(n in KNOWN_PROVIDERS for n in names):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"auth.providers names no known provider (valid: {sorted(KNOWN_PROVIDERS)}); "
                    "only unknown names would silently re-enable all sign-in methods"
                ),
            )
        effective = names
    else:
        # providers isn't in THIS patch. Only a change to auth.keboola config
        # can newly break a currently-available provider via server-config —
        # google/email availability is env-only and no config save can change
        # it. So re-check the EXISTING allowlist ONLY when the patch touches
        # keboola: that catches "clear auth.keboola while providers=[keboola]",
        # without 422-ing an unrelated auth save (e.g. allowed_domain) against a
        # pre-existing env-provider allowlist that has no server-config field to
        # fix (Devin review on #1288).
        if "keboola" not in auth:
            return
        from app.instance_config import get_value

        current_raw = get_value("auth", "providers")
        if not isinstance(current_raw, (str, list)):
            return
        effective = _normalize_provider_names(current_raw) or []
        if not effective:
            return

    # The login page offers a provider only when it is BOTH allowlisted and
    # actually available (configured). `password` is always available, so any
    # list including it passes.
    known = [n for n in effective if n in KNOWN_PROVIDERS]
    if not known:
        # An all-unknown EXISTING value (not being edited here) fails open to
        # all providers at runtime — not a lockout — so don't block this save.
        return
    if not any(_provider_available_after_save(n, auth, sections) for n in known):
        detail = (
            "auth.providers would leave no usable sign-in method: none of the named "
            "providers is configured/available on this instance. Saved anyway it would "
            "not lock anyone out — the runtime treats such a list as unset (ALL sign-in "
            "methods) with a loud error — but that silently means the opposite of what "
            "was written, so it is refused here instead. Configure one of the named "
            "providers (e.g. add the Google or Keboola OAuth credentials), or include a "
            "method that is."
        )
        if "google" in known:
            # Google's availability probe reads env vars captured at process
            # start (see _provider_available_after_save) — without this note,
            # an operator who just filled Google settings into instance.yaml
            # has no way to understand the refusal (Devin Review on PR #1288).
            detail += (
                " Note: Google availability is read from the GOOGLE_CLIENT_ID / "
                "GOOGLE_CLIENT_SECRET environment variables at process start — a Google "
                "OAuth client configured only in instance.yaml is not detected."
            )
        if "sso" in known:
            # SSO is configured on its own admin panel, not in instance.yaml —
            # without this note a refused [sso] allowlist gives the operator
            # no pointer to where the provider actually gets configured.
            detail += (
                " Note: the external SSO provider is configured at runtime on the SSO admin "
                "panel (/api/admin/sso/config) — it reads available only once its config is "
                "saved with a stored client secret and enabled=true."
            )
        if "microsoft" in known:
            # Same env-capture property as Google, and the base detail names
            # neither Microsoft nor its variables — so a Microsoft-only save
            # refused for missing env would otherwise read as a message about
            # some other provider. The tenant clause is not padding: an
            # invalid/multi-tenant MICROSOFT_TENANT_ID makes the provider
            # unavailable too (see app/auth/providers/microsoft.py), which is
            # indistinguishable from "unset" without saying so.
            detail += (
                " Note: Microsoft availability is read from the MICROSOFT_TENANT_ID / "
                "MICROSOFT_CLIENT_ID / MICROSOFT_CLIENT_SECRET environment variables at process "
                "start — Microsoft sign-in cannot be configured from instance.yaml. It also reads "
                "unavailable when MICROSOFT_TENANT_ID is not a single tenant (a directory GUID or a "
                "verified domain); the boot log says which."
            )
        raise HTTPException(status_code=422, detail=detail)


def _provider_available_after_save(name: str, auth_patch: Dict[str, Any], sections: Dict[str, Dict[str, Any]]) -> bool:
    """Whether ``name`` would be an offerable login method once this
    server-config save lands. ``password`` is always available; ``google`` /
    ``email`` are env-configured (untouched by an auth.providers patch), so
    their live ``is_available()`` is authoritative; ``keboola`` is configured
    under ``auth.keboola`` and can be set in the SAME patch, so it is evaluated
    against the current config merged with the patch — including its stack_url
    fallback to ``data_source.keboola.stack_url``, which the admin may also be
    supplying in this same save (or enabling + configuring in one step would be
    wrongly rejected)."""
    if name == "password":
        return True
    if name == "google":
        from app.auth.providers.google import is_available as google_available

        # Env-only, captured at module import (GOOGLE_CLIENT_ID/SECRET) — a
        # yaml-only Google config reads unavailable here; the 422 detail in
        # _validate_auth_providers_in_patch explains that to the operator.
        return google_available()
    if name == "email":
        from app.auth.providers.email import is_available as email_available

        return email_available()
    if name == "keboola":
        from app.instance_config import get_value

        merged = {**(get_value("auth", "keboola") or {}), **(auth_patch.get("keboola") or {})}
        # stack_url falls back to data_source.keboola.stack_url — merge the
        # current value with this patch's data_source block so a one-save
        # "configure login + data-source address" isn't wrongly refused.
        ds_merged = {
            **(get_value("data_source", "keboola") or {}),
            **((sections.get("data_source") or {}).get("keboola") or {}),
        }
        stack = merged.get("stack_url") or ds_merged.get("stack_url")
        return bool(merged.get("client_id") and merged.get("client_secret") and merged.get("project_id") and stack)
    if name == "microsoft":
        # Env-only, like google: the patch cannot make it available or
        # unavailable, so the current runtime answer is the answer after save.
        # Without this branch the name is known (KNOWN_PROVIDERS) but never
        # available, so narrowing an instance to Microsoft-only is refused as
        # "no usable sign-in method" even with all three env vars set.
        from app.auth.providers.microsoft import is_available as microsoft_available

        return microsoft_available()
    if name == "sso":
        # DB-configured (the /api/admin/sso panel), not instance.yaml — this
        # patch cannot change it, so current availability == availability
        # after save (the same argument the env-var branches make). Without
        # this branch an `auth.providers: [sso]` allowlist would always be
        # refused as "no usable sign-in method".
        from app.auth.providers.sso import is_available as sso_available

        return sso_available()
    return False


_LOCK_TTL_MIN = 60
_LOCK_TTL_MAX = 7 * 24 * 3600  # 604800 — one week


def _validate_materialize_section(sections: Dict[str, Dict[str, Any]]) -> None:
    """Validate the materialize section patch when present.

    Checks field-level constraints that the Pydantic envelope can't enforce
    (it only validates the outer shape, not nested leaf values).
    """
    mat = sections.get("materialize")
    if not isinstance(mat, dict):
        return
    ttl = mat.get("lock_ttl_seconds")
    if ttl is None:
        return
    if not isinstance(ttl, int) or isinstance(ttl, bool):
        raise HTTPException(
            status_code=422,
            detail="materialize.lock_ttl_seconds must be an integer",
        )
    if ttl < _LOCK_TTL_MIN or ttl > _LOCK_TTL_MAX:
        raise HTTPException(
            status_code=422,
            detail=(f"materialize.lock_ttl_seconds must be between {_LOCK_TTL_MIN} and {_LOCK_TTL_MAX} (got {ttl})"),
        )


# --- Server-config (instance.yaml) editor -----------------------------------
#
# The /admin/server-config UI POSTs a partial dict here keyed by section
# (instance, data_source, email, telegram, theme, server, auth) with
# the field values to merge into instance.yaml. Each save:
#   1. Loads the current instance.yaml (writable overlay first, then static).
#   2. Deep-merges the patch on top.
#   3. Writes to DATA_DIR/state/instance.yaml (the writable overlay).
#   4. Writes one audit_log entry tagged `instance_config.update` containing
#      a sanitized diff (secret-looking keys are masked).
# Most sections hot-reload on their own (`reset_cache()` below drops the
# in-process instance.yaml cache, and most consumers read it fresh per
# call) — the response's `restart_required`/`sections_effect` say honestly
# which of the sections just saved still need a restart. See
# `_SECTION_BASELINE_EFFECT` / `_effect_for_section` below.

# Sections an admin can mutate.
#
# Two halves. `_STATIC_EDITABLE_SECTIONS` are the sections that carry ordinary
# configuration — hosts, credentials, limits — and own no switch. The rest is
# DERIVED from the switch registry, so adding an editable switch cannot leave
# its section unwritable; that omission shipped twice before this was
# mechanical (`mcp`, then `chat`).
#
# A typo'd section in the request body is still rejected loudly rather than
# being merged into the YAML root.
_STATIC_EDITABLE_SECTIONS: tuple[str, ...] = (
    "instance",
    "data_source",
    "email",
    "telegram",
    "theme",
    "server",
    "auth",
    "ai",
    "corporate_memory",
    "materialize",
    "marketplace",
    "connectors",
)

_EDITABLE_SECTIONS: tuple[str, ...] = tuple(
    sorted(set(_STATIC_EDITABLE_SECTIONS) | {s.config_keys[0] for s in SWITCHES if s.editable and s.config_keys})
)

# "Danger-zone" sections — flipping these can lock operators out (auth.*) or
# break OAuth callbacks (server.hostname/host). The UI shows a confirmation
# dialog before submitting them. The API accepts them; this list exists so
# the audit entry can flag the change as high-risk and the UI can surface
# the right warning copy.
_DANGER_SECTIONS: tuple[str, ...] = ("auth", "server")


# --- Honest restart_required ------------------------------------------------
#
# What POST /api/admin/server-config's response tells the operator about a
# save: whether SOMETHING they touched needs a restart before it applies.
# Historically this was a hardcoded `True` for every save, even though most
# sections are re-read per request (`reset_cache()` above drops the
# in-process instance.yaml cache on every save, so any consumer that calls
# `get_value`/`load_instance_config()` fresh sees the new value immediately).
#
# Per-section baseline: the effect of the keys in a section that are NOT
# covered by any `Switch` (`app.switches.SWITCHES`) whose `config_keys[0]`
# is that section. This is deliberately a per-SECTION verdict, not per-key —
# a section like `auth` has some genuinely live leaves (the two Keboola
# switches below) but is still reported as `restart` overall, because most
# of its OTHER keys (client_id/client_secret/allowed_domain/...) feed OAuth
# provider client objects built once at startup. Evidence per entry (audit,
# 2026-08; see also `_effect_for_section` below for how a touched switch can
# still push a "live" baseline up to `restart`/`deploy`, never down):
_SECTION_BASELINE_EFFECT: dict[str, str] = {
    # --- live: every consumer found reads the section fresh per call/run;
    # no module-level or app.state object caches the pre-save value past
    # the `reset_cache()` every save triggers.
    "instance": "live",  # get_experience()/get_instance_brand()/get_instance_favicon()/get_home_route() are thin get_value() wrappers, called per render
    "theme": "live",  # get_theme_css_overrides() re-reads per render
    "ai": "live",  # every consumer (app/api/memory.py, services/corporate_memory/collector.py, services/session_processors/verification.py, src/knowledge_digests.py) calls load_instance_config().get("ai") fresh per invocation; no LLM client is cached at boot
    "materialize": "live",  # connectors/bigquery/extractor.py::_get_lock_ttl_seconds() re-reads get_value("materialize", "lock_ttl_seconds") on every lock acquire/sweep, not just at import
    "marketplace": "live",  # its one known key (curators_url) is read fresh per page render in app/web/router.py
    "connectors": "live",  # GET /api/connectors/params reads the overlay fresh per request (its own docstring: "editable at runtime")
    "studio": "live",  # matches its switch — no other known key under this section
    "guardrails": "live",  # matches its switch
    "library": "live",  # matches its switch
    "features": "live",  # matches its switch
    "mcp": "live",  # matches all five switches under it
    "access_policies": "live",  # matches its switch
    "facts": "live",  # both switches (enabled/visibility_mode) are read per-call — feature_enabled()/switch_value(), no cached object
    "extraction_webhook": "live",  # matches its switch — no other known key under this section
    # --- restart: something under the section is built once at boot and
    # never rebuilt from a later save.
    "chat": "restart",  # app.state.chat_config is built once in create_app() (matches both switches under it)
    "auth": "restart",  # OAuth provider client objects (app/auth/providers/*) are constructed once at startup and never rebuilt — a minority of this section's keys (the two Keboola switches) ARE read live, but most (client_id/client_secret/allowed_domain/...) are not
    "server": "restart",  # partial and conservative: get_public_url() is read fresh at most call sites, but app.state.public_url is snapshotted ONCE at startup for the Slack Socket-Mode dispatcher (app/main.py — it has no inbound request to derive a host from); no live per-request reader was found for server.host/server.hostname
    "email": "restart",  # conservative: the actual SMTP send path (app/auth/providers/email.py, password.py) reads SMTP_HOST/SMTP_USER/SMTP_PASSWORD straight from os.environ, never from get_value("email", ...) — a save here was not observed to change behavior at all, live or restart; "restart" is the non-overclaiming answer
    "telegram": "restart",  # services/telegram_bot/bot.py reads instance.yaml ONCE at module import, in a separate process — restarting the API alone does not refresh it
    "data_source": "restart",  # cross-process: THIS process reads it live (resolved per call; reset_cache() explicitly clears connectors.bigquery.access.get_bq_access's cache), but reset_cache() drops only the in-process overlay — under a role-split deployment (api/gateway/worker as separate processes, a documented mode) the scheduler and workers keep extracting against the pre-save coordinates until they are bounced, so a connection-settings save must not be reported as fully live; same reasoning as telegram above. NARROWED scope since D2.2/D2.3: this classification covers writes to THIS yaml overlay only — the Add-data wizard's Snowflake/Databricks panes (and every Keboola/BigQuery connection edit) now write the `source_connections` ROW instead (PUT/POST /api/admin/source-connections*), which every process reads live straight off the DB with no cache to bounce, so those saves carry no restart notice at all. A hand-edited `data_source.snowflake.*`/`data_source.databricks.*` yaml block is IGNORED (not merely stale) once a row of that type exists — connections_seed.py's own deprecation-warning pattern, not this restart flag — but still reachable (and still genuinely restart-classified) on an un-migrated instance with no row yet, or for a source this section still owns end-to-end (keboola's stack_url predates the registry too, though its own CRUD long since moved to source_connections as well).
    "corporate_memory": "restart",  # partial: most keys (distribution_mode/approval_mode/sources.*) are read fresh via get_corporate_memory_config() per page render, but corporate_memory.confidence is applied ONCE at startup via services/corporate_memory/confidence.configure() (app/main.py) — conservative for the whole section, same reasoning as auth
}

#: Rank used to pick the "strongest" effect among a section's baseline and
#: any switch actually touched by a given patch. Higher = more conservative;
#: `deploy` outranks `restart` outranks `live` so a save is never reported
#: as less disruptive than the truth.
_EFFECT_RANK: dict[str, int] = {"live": 0, "restart": 1, "deploy": 2}


def _switch_touched_by_patch(switch, patch: Dict[str, Any]) -> bool:
    """True if `patch` (a single section's patch dict) sets the leaf this
    switch's `config_keys[1:]` path points at.

    Used to narrow "sections whose keys map to switches" down to the
    switches actually in THIS request, so an untouched switch's effect can't
    push (or fail to push) the verdict for a key the operator didn't save.
    """
    node: Any = patch
    for key in switch.config_keys[1:]:
        if not isinstance(node, dict) or key not in node:
            return False
        node = node[key]
    return True


def _effect_for_section(section: str, patch: Dict[str, Any]) -> str:
    """The strongest effect among `section`'s non-switch baseline and every
    switch under it that THIS patch actually touches.

    A section with no baseline entry (a new section added without updating
    `_SECTION_BASELINE_EFFECT`) defaults to `"restart"` — the conservative
    choice for an unclassified section, per the same reasoning as `email`/
    `server`/etc. above.
    """
    effects = [_SECTION_BASELINE_EFFECT.get(section, "restart")]
    for switch in SWITCHES:
        if switch.editable and switch.config_keys and switch.config_keys[0] == section:
            if _switch_touched_by_patch(switch, patch):
                effects.append(switch.effect)
    return max(effects, key=lambda e: _EFFECT_RANK.get(e, _EFFECT_RANK["restart"]))


# Every editable section must have an explicit baseline OR full switch
# coverage explaining it — this assertion turns "I added a new editable
# section and forgot to classify it" into a loud import-time error instead
# of a silent fall-through to the conservative default above.
_UNCLASSIFIED_SECTIONS = sorted(section for section in _EDITABLE_SECTIONS if section not in _SECTION_BASELINE_EFFECT)
assert not _UNCLASSIFIED_SECTIONS, (
    f"section(s) {_UNCLASSIFIED_SECTIONS} have no entry in _SECTION_BASELINE_EFFECT — "
    "classify them (see the comment above) before making them admin-editable"
)


# Known-but-optional config fields per section. The /admin/server-config UI
# uses this registry alongside the YAML payload to render fields the operator
# might want to set even though they're not currently in instance.yaml.
#
# Schema per field:
#   {
#     "kind": "string" | "secret" | "bool" | "int" | "select" | "object" | "array",
#     "default": <type-appropriate default>  (optional)
#     "hint": "<one-line operator-facing help>"
#     "options": [...]              (only for kind="select")
#     "fields": {<name>: <fieldspec>}  (only for kind="object", nested fields)
#     "item_kind": "string" | ...   (only for kind="array", element type)
#     "required": bool             (defaults False; UI marks the label)
#   }
#
# Subagents 2-4 will populate the bodies. The registry enables the UI to
# render missing-but-known fields with placeholders + hints rather than
# forcing the operator to discover them via the JSON-patch textarea or
# hitting a runtime error first. The smoke fixture below
# (data_source.bigquery.billing_project) proves the renderer wiring works
# end-to-end so subagents 2-4 only have to add registry entries — they
# don't need to touch admin_server_config.html.
def _flag_default(section: str, key: str, fallback: bool) -> bool:
    """The default the switch registry declares for a flag-backed field.

    Hand-copying it here is how `chat.approvals_enabled` ended up documented
    as off-by-default while the registry and the runtime had it on. `fallback`
    covers a declared field with no registry entry — a plain config boolean
    rather than a switch.
    """
    return _flag_default_path((section, key), fallback)


def _flag_default_path(config_keys: tuple[str, ...], fallback: bool) -> bool:
    """`_flag_default` for a switch whose config path is deeper than
    `section.key` (e.g. `auth.keboola.allow_token_header`). Same no-second-copy
    rationale; matched on the full path so a nested declaration can't silently
    miss the registry entry and fall back to a hand-typed default."""
    for s in SWITCHES:
        if s.config_keys == config_keys:
            return bool(s.default)
    return fallback


def _switch_default_path(config_keys: tuple[str, ...], fallback: Any) -> Any:
    """`_flag_default_path` without the bool coercion — for `select`-kind
    switches (e.g. `auth.keboola.multi_project_mode`), whose registry default
    is a string the panel must render verbatim, not a truthiness."""
    for s in SWITCHES:
        if s.config_keys == config_keys:
            return s.default
    return fallback


_KNOWN_FIELDS: dict[str, dict[str, dict]] = {
    # Both sections became editable alongside `mcp`; declaring their booleans
    # here is what makes the panel render a switch instead of a free-text field,
    # and what keeps them out of the secret redactor via
    # `_declared_boolean_fields()`.
    "chat": {
        "enabled": {
            "kind": "bool",
            "default": _flag_default("chat", "enabled", False),
            "hint": (
                "Expose the cloud chat surface. app/main.py boots chat from the "
                "writable server-config overlay alone, so this editor (or "
                "AGNES_CHAT_ENABLED) is the effective way to turn it on — a value "
                "set only in the static config/instance.yaml never reaches the "
                "chat runtime."
            ),
        },
        "approvals_enabled": {
            "kind": "bool",
            "default": _flag_default("chat", "approvals_enabled", True),
            "hint": (
                "ON by default: a tool call the agent asks about is routed to an "
                "approval card. Turning it OFF auto-allows those calls instead. "
                "Resolved from the same overlay-only source as chat.enabled."
            ),
        },
        "bootstrap_marketplace": {
            "kind": "bool",
            "default": _flag_default("chat", "bootstrap_marketplace", True),
            "hint": (
                "ON by default: the skills in a user's stack are materialized into "
                "their chat session, so the composer's /<skill-name> menu entries "
                "actually resolve. Turning it OFF removes marketplace skills from "
                "that menu too — the menu never offers what nothing delivers. "
                "Resolved from the same overlay-only source as chat.enabled."
            ),
        },
        "broker_admin_reads": {
            "kind": "bool",
            "default": _flag_default("chat", "broker_admin_reads", True),
            "hint": (
                "ON by default: read-only (GET/HEAD) admin API routes are replayed "
                "through the chat secret broker under the session user's own "
                "identity, so `agnes admin list-users`/`list-tables` work for an "
                "actual admin inside a chat sandbox — the route's live "
                "require_admin still refuses everyone else, and admin MUTATIONS "
                "are always refused from sandboxes regardless of this switch. "
                "Read live per request by the broker; no restart needed."
            ),
        },
    },
    "studio": {
        "enabled": {
            "kind": "bool",
            "default": _flag_default("studio", "enabled", True),
            "hint": (
                "Expose the authoring Studio (/admin/studio* including the "
                "moderation queue, plus the public suggestion API). Read per "
                "request, so turning it off hides the nav entries, redirects the "
                "routes home and 403s the suggestion API immediately."
            ),
        },
    },
    "extraction_webhook": {
        "enabled": {
            "kind": "bool",
            "default": _flag_default("extraction_webhook", "enabled", False),
            "hint": (
                "Microsoft Graph change-notification receiver for SharePoint "
                "connections (POST /api/webhooks/sharepoint/{connection_id}) — 404s "
                "the whole route when off. Gates the receiver route only; the "
                "corpus-extraction job it enqueues still needs extraction.enabled + a "
                "configured producer + a worker polling the extraction lane to "
                "actually run. New feature — off by default."
            ),
        },
    },
    "access_policies": {
        "enabled": {
            "kind": "bool",
            "default": _flag_default("access_policies", "enabled", False),
            "hint": (
                "Table access policies — lets an admin attach one row-filtering "
                "and column-masking SQL policy per non-distributed table "
                "(query_mode='remote' or server_only=true), substituted for it "
                "on every server-side read with the caller's identity bound in. "
                "Gates ATTACHING a policy only (PUT /api/admin/registry/{id}); "
                "a table that already carries one stays protected — and the "
                "distribution interlock stays enforced — regardless of this "
                "flag's later state. New feature — off by default."
            ),
        },
    },
    "facts": {
        "enabled": {
            "kind": "bool",
            "default": _flag_default("facts", "enabled", False),
            "hint": (
                "Fact graph over Collections — typed subjects (facts/edges) extracted "
                "from Collections documents, each claim carrying its evidencing "
                "document, verbatim quote and date. Gates the whole /api/facts* router "
                "(404 when off). Postgres-only (A3 ratchet) — a DuckDB-backed instance "
                "answers a typed 501 regardless of this flag. Covers both the read "
                "surface (search/neighbors/claims — any authenticated caller, over "
                "REST, `agnes facts` and the MCP foundation tools) and the write "
                "surface (ingest + corrections, scheduler-token-or-admin, REST only "
                "by design). New feature — off by default."
            ),
        },
        "visibility_mode": {
            "kind": "select",
            "options": ["any_evidence", "all_evidence"],
            "default": _switch_default_path(("facts", "visibility_mode"), "any_evidence"),
            "hint": (
                "Fact-graph subject existence rule. any_evidence (default): a subject "
                "is visible if at least one of its claims is in a readable collection. "
                "all_evidence: visible only if ALL of its claims are readable — hides "
                "strictly more within one grant snapshot. Facts and edges use the same "
                "rule."
            ),
        },
    },
    "features": {
        "stack_auto_membership": {
            "kind": "bool",
            "default": _flag_default("features", "stack_auto_membership", False),
            "hint": (
                "Stack membership mode. OFF (classic, the default): membership "
                "is the subscribe model — required plus subscribed grants, all "
                "downloaded by agnes pull. ON: auto-membership — every granted "
                "resource is in the stack immediately; subscribe/unsubscribe "
                "only control the local copy. Read per request; flips instantly, "
                "subscriptions are interpreted, never rewritten. The "
                "instance.experience: redesign preset defaults this ON."
            ),
        },
        # The three surfaces the admin cleanup retired. Declared here so the
        # panel renders each as a toggle rather than a text box — see
        # `app/switches.py` for why all three live under `features` instead of
        # a top-level section each.
        "news_enabled": {
            "kind": "bool",
            "default": _flag_default("features", "news_enabled", False),
            "hint": (
                "In-product news: the /admin/news editor, the /news reader, the "
                '/home "What\'s new" strip, and their nav + command-palette '
                "entries. OFF by default since the admin cleanup. Hides UI only "
                "— /api/admin/news/* keeps serving and a published version stays "
                "in the table, so turning this back on restores the surface with "
                "its content intact."
            ),
        },
        "knowledge_digests_enabled": {
            "kind": "bool",
            "default": _flag_default("features", "knowledge_digests_enabled", False),
            "hint": (
                "The /admin/knowledge-digests admin PAGE and its nav row. OFF by "
                "default since the admin cleanup. Deliberately narrow — it gates "
                "the page, not the feature: /api/admin/knowledge-digests/*, "
                "`agnes admin digest`, the digest scheduler job and `agnes pull`'s "
                "digest delivery all keep working, so an instance already running "
                "digests keeps running them headlessly."
            ),
        },
        "contribute_skill_enabled": {
            "kind": "bool",
            "default": _flag_default("features", "contribute_skill_enabled", False),
            "hint": (
                "The paste-a-SKILL.md publish page (/admin/contribute-skill), the "
                'landing target for an external "Load skill to Agnes" button. '
                "OFF by default since the admin cleanup — the Library's skill "
                "builder is the supported path. Both POST handlers carry the gate "
                "too, so a stale external button gets a redirect home rather than "
                "a silent publish."
            ),
        },
        "store_moderation_enabled": {
            "kind": "bool",
            "default": _flag_default("features", "store_moderation_enabled", False),
            "hint": (
                "The Moderation & Trust hub (/admin/store), its nav row, its "
                "command-palette row and the `g v` shortcut. OFF by default: each "
                "of its three zones has a nearer door — Submissions is its own nav "
                "row, curation is /admin/marketplaces, and verification has its own "
                "store.verification_enabled — so the hub was a landing page for "
                "links the column already carries. Hides UI only: the store APIs "
                "and /api/admin/share-requests* keep serving, so a queued agent "
                "share stays decidable by API."
            ),
        },
    },
    "mcp": {
        "allow_query_param_token": {
            "kind": "bool",
            "default": _flag_default("mcp", "allow_query_param_token", True),
            "hint": (
                "Accept an MCP access token in the `?token=` query string as "
                "well as the Authorization header. Convenient for clients that "
                "cannot set headers, but a URL travels through proxy logs, "
                "browser history and Referer headers, so turn it off once every "
                "client you use sends the header."
            ),
        },
        "session_pool": {
            "kind": "bool",
            "default": _flag_default("mcp", "session_pool", True),
            "hint": (
                "Keep a stdio MCP server's process warm between tool calls "
                "instead of starting one per call — the upstream's own imports "
                "cost about six seconds every time. Read per call, so a change "
                "applies to the next one. Turn it off for a process per call: "
                "the debugging shape, and the answer for an upstream that "
                "cannot survive being reused. http/sse sources are unaffected."
            ),
        },
        "source_url_strict": {
            "kind": "bool",
            "default": _flag_default("mcp", "source_url_strict", False),
            "hint": (
                "Require a registered MCP source's own address to be https to a "
                "public host, the same bar its OAuth endpoints already meet. Off "
                "by default, which is not unguarded: link-local, metadata, "
                "multicast and reserved addresses are always refused, as is "
                "cleartext http to a public one. Leaving it off is what allows a "
                "source on your own intranet. Turn it on if every MCP service you "
                "use is third-party — it makes an internal source unconfigurable."
            ),
        },
        "connector_ui_enabled": {
            "kind": "bool",
            "default": _flag_default("mcp", "connector_ui_enabled", True),
            "hint": (
                "Expose the user-facing MCP connector surface — /me/ai-connector, "
                "/mcp-connect, and the MCP tab of /how-it-works#connect, plus their "
                "nav/palette entries. On by default. Turn off on a VPN/intranet-only "
                "instance whose cloud-side MCP clients can never reach the endpoint, "
                "so users are not shown a setup path that cannot work for them. UI "
                "only — the MCP protocol endpoints keep serving in-network clients "
                "regardless."
            ),
        },
        "source_url_runtime_enforce": {
            "kind": "bool",
            "default": _flag_default("mcp", "source_url_runtime_enforce", False),
            "hint": (
                "Enforce the scheme/literal-IP half of the url policy at the two "
                "runtime forward seams too, not only when a source is configured "
                "(#1216). Off by default: an already-enabled legacy source keeps "
                "forwarding exactly as it does today. Before turning this on, check "
                "the url_policy_verdict column on the MCP source list for any "
                "would_refuse row and fix its url first — this switch turns each one "
                "into a refused call with no other warning."
            ),
        },
    },
    "instance": {
        # Experience preset — registry-backed (app/switches.py `experience`,
        # kind select); declared here so the panel renders a select rather
        # than a free-text field. Resolved by
        # `app/instance_config.py::get_experience()` via `switch_value`.
        "experience": {
            "kind": "select",
            "options": ["redesign"],
            "default": "redesign",
            "hint": (
                "One-line redesign adoption preset — retired as a choice; "
                "`redesign` is the only option and the default. Changes the "
                "DEFAULTS of theme → paper and features.stack_auto_membership "
                "→ on; either can still be overridden per-knob. ui_layout is "
                "NOT one of those overridable knobs any more — the rail "
                "chrome is hard-wired (Wave 0, 2026-08); a configured "
                "instance.ui_layout is ignored (logged as a startup warning), "
                "not honored. Kept only so an existing "
                "`instance.experience` yaml/env value doesn't error; any "
                "other value (including the old `classic`) falls back to "
                "this default."
            ),
        },
        # UI theme — flips `<html data-theme="...">` so the
        # design-system tokens (`--ds-*`) switch palettes via CSS
        # without any markup change. Resolved by
        # `app/instance_config.py::get_instance_theme()`.
        "theme": {
            "kind": "select",
            # Full valid set per get_instance_theme() — a select whose
            # options lag the resolver can only write values that erase an
            # operator's working choice.
            "options": ["blue", "navy", "dark", "auto", "paper"],
            # Static registry default; `_known_fields_resolved()` patches it
            # per request to the preset-implied default so the panel never
            # renders (and a save never persists) a value the runtime
            # doesn't use (Devin Review on #1199).
            "default": "blue",
            "hint": (
                "UI palette. `blue` (default) uses the brand-blue hero + "
                "blue CTAs; `navy` the darker palette with mint-green CTAs; "
                "`dark` the dark scheme; `auto` follows the OS; `paper` the "
                "prototype-derived light look (redesign). The "
                "`instance.experience: redesign` preset defaults this to "
                "`paper`."
            ),
        },
        # Operator-injected HTML/JS blocks rendered into base.html.
        # `kind: array` renders as a JSON textarea in the admin UI
        # (per admin_server_config.html:702-708 — arrays fall back to
        # the JSON path); the hint documents the per-item shape so the
        # operator knows what to paste. Resolved by
        # `app/instance_config.py::get_custom_scripts()`.
        "custom_scripts": {
            "kind": "array",
            "hint": (
                "Operator-injected HTML/JS blocks rendered into base.html. "
                "Each entry: {name: str, enabled: bool, placement: "
                "head_start|head_end|body_end, html: str}. Used for feedback "
                "widgets (Marker.io), analytics (GTM, PostHog), error capture "
                "(Sentry). Rendered with | safe — admin trust boundary. Review "
                "third-party widget privacy posture before enabling (most "
                "capture session data). Restart required after save."
            ),
        },
        # Operator-authored Support HTML rendered inside the welcome
        # hero on /home, below the operator-owned Overview footnotes.
        # Resolved by `app/instance_config.py::get_instance_support()`.
        # Typical content: a one-line invitation pointing at a chat
        # space, mailing list, or internal runbook. Empty value =
        # block hidden (OSS stays vendor-neutral).
        "support": {
            "kind": "string",
            "hint": (
                "HTML body rendered inside the welcome hero's Support "
                "block on /home (mint-accent panel below the Overview "
                "footnotes). Typically a one-line invitation linking to "
                "a chat space, mailing list, or runbook — e.g. "
                "'<p><strong>Need help?</strong> Drop into our "
                '<a href="https://chat.example.com/room/xxx">Support</a> '
                "chat space.</p>'. Rendered with | safe — admin trust "
                "boundary (link target is operator-controlled). Empty "
                "value hides the block."
            ),
        },
    },
    "data_source": {
        "type": {
            "kind": "select",
            "options": ["keboola", "bigquery", "local", "csv"],
            "default": "local",
            "hint": (
                "Active data source connector. "
                "`keboola` — pulls tables from Keboola Storage API (configure stack_url + token below). "
                "`bigquery` — queries BigQuery remotely via the DuckDB BQ extension (configure bigquery block below). "
                "`local` — CSV/parquet files placed directly in the data directory. "
                "`csv` — alias for local."
            ),
        },
        "bigquery": {
            "kind": "object",
            "hint": "BigQuery connection knobs (read more in docs/DEPLOYMENT.md)",
            "fields": {
                "project": {
                    "kind": "string",
                    "hint": (
                        "GCP project holding the data. Every registered BigQuery "
                        "row resolves under it unless the row sets `bq_fqn` "
                        "(`project.dataset.table`), which overrides all three "
                        "parts for that row alone. Register a table living in "
                        "another project that way rather than repointing this "
                        "global. Analyst `--remote` SQL may only name this "
                        "project directly; other projects are reachable only "
                        "through a registered row."
                    ),
                },
                "location": {
                    "kind": "string",
                    "hint": (
                        "BigQuery location/region the datasets live in (e.g. "
                        "`us-central1`, `EU`). Must match the data's actual "
                        "location: a mismatch surfaces as `404 Not found: "
                        "Table ... was not found in location <location>` even "
                        "when the table exists."
                    ),
                },
                "billing_project": {
                    "kind": "string",
                    "hint": (
                        "GCP project to bill BQ jobs against. Set when SA can read "
                        "the data project but cannot bill there (e.g. shared read-only "
                        "data project). Defaults to data_source.bigquery.project. "
                        "Mismatch → 403 USER_PROJECT_DENIED on every BQ call."
                    ),
                    # Issue #160 §4.7.5: when this field is empty in the
                    # admin form, the JS template shows "(defaults to <project>)"
                    # as placeholder text — surfacing the access.py:339-340
                    # fallback rule directly in the UI without the operator
                    # having to read source. Path is walked against the
                    # `original` config payload from GET /api/admin/server-config.
                    "placeholder_from": ["data_source", "bigquery", "project"],
                },
                "max_bytes_per_materialize": {
                    "kind": "int",
                    "default": 10737418240,
                    "hint": (
                        "Cost guardrail for query_mode='materialized' BQ scans (dry-run "
                        "check before running). Bytes processed; exceeds → registration "
                        "or sync rejected. 0 disables the gate. Default 10737418240 = 10 GiB."
                    ),
                },
                "bq_max_scan_bytes": {
                    "kind": "int",
                    "default": 5368709120,
                    "hint": (
                        "Cost guardrail for `agnes query --remote` against query_mode='remote' "
                        "BQ rows (dry-run check on the underlying SELECT before execute). "
                        "Bytes processed; exceeds → 400 remote_scan_too_large with a "
                        "`agnes snapshot create` suggestion. 0 disables the gate. Default 5368709120 = 5 GiB."
                    ),
                },
                "query_timeout_ms": {
                    "kind": "int",
                    "default": 600000,
                    "hint": (
                        "DuckDB BigQuery extension query timeout (milliseconds). Applied "
                        "via `SET bq_query_timeout_ms` after every `LOAD bigquery` on "
                        "every BQ-touching DuckDB session (orchestrator remote-view "
                        "ATTACH, BqAccess factory, standalone extractor). Extension "
                        "default is 90 000 ms = 90 s, which is too tight for analyst "
                        "queries against view-backed datasets — bumped to 600 000 ms = "
                        "10 min by default. Set 0 to fall through to the extension "
                        "default. Note: the underlying BQ jobs.query RPC caps the wait "
                        "at ~200 s per call; the extension polls on top, so the "
                        "effective ceiling is this value but each poll round-trip is "
                        "~200 s. DuckDB itself emits a warning when this is set above "
                        "~200 s — that warning is informational, not an error."
                    ),
                },
            },
        },
        "keboola": {
            "kind": "object",
            "hint": "Keboola Storage API connection",
            "fields": {
                "stack_url": {
                    "kind": "string",
                    "hint": (
                        "e.g. https://connection.keboola.com (instance-specific stack URL). "
                        "Validated against private-IP allowlist on save (SSRF guard)."
                    ),
                },
                "project_id": {
                    "kind": "string",
                    "hint": "Keboola project ID (numeric, but kept as string in YAML).",
                },
            },
        },
        "databricks": {
            "kind": "object",
            "hint": (
                "Databricks SQL warehouse connection (query_mode='materialized' rows + "
                "Unity Catalog metric-view sync). Token comes from the DATABRICKS_TOKEN "
                "env var / vault secret, never from YAML."
            ),
            "fields": {
                "host": {
                    "kind": "string",
                    "hint": (
                        "Workspace URL, e.g. https://adb-1234567890123456.7.azuredatabricks.net "
                        "or https://dbc-a1b2c3d4-e5f6.cloud.databricks.com. https-only, no path."
                    ),
                },
                "warehouse_id": {
                    "kind": "string",
                    "hint": (
                        "SQL warehouse ID the Statement Execution API runs on (SQL "
                        "Warehouses → your warehouse → Connection details)."
                    ),
                },
                "catalog": {
                    "kind": "string",
                    "hint": (
                        "Default Unity Catalog catalog. Registered rows resolve "
                        "`bucket` as a schema inside it (a dotted bucket "
                        "'catalog.schema' overrides per row); the semantic-layer "
                        "sync enumerates its metric views."
                    ),
                },
                "max_bytes_per_materialize": {
                    "kind": "int",
                    "default": 10737418240,
                    "hint": (
                        "Cost guardrail for query_mode='materialized' Databricks rows. "
                        "Caps the statement RESULT size via the API's byte_limit (no "
                        "dry-run primitive exists, unlike BigQuery) — a truncated "
                        "result is rejected, never written. 0 disables. Default "
                        "10737418240 = 10 GiB."
                    ),
                },
                "statement_timeout_seconds": {
                    "kind": "int",
                    "default": 900,
                    "hint": (
                        "Client-side deadline on a materialize statement; on expiry "
                        "the statement is cancelled on the warehouse and the row "
                        "errors. 0 disables. Default 900."
                    ),
                },
                "max_bytes_per_remote_query": {
                    "kind": "int",
                    "default": 1073741824,
                    "hint": (
                        "Cost guardrail for interactive `agnes query --remote` against "
                        "query_mode='remote' rows. Caps the bytes the warehouse may "
                        "RETURN (the API's byte_limit) — not the bytes it scanned, "
                        "which Databricks does not expose. A capped result is refused, "
                        "never returned short. 0 disables. Default 1073741824 = 1 GiB."
                    ),
                },
                "remote_query_timeout_seconds": {
                    "kind": "int",
                    "default": 120,
                    "hint": (
                        "Deadline on an interactive remote query. Shorter than the "
                        "materialize timeout because a human is waiting. Default 120."
                    ),
                },
                "scan_timeout_seconds": {
                    "kind": "int",
                    "default": 900,
                    "hint": (
                        "Deadline on a SNAPSHOT statement (`agnes snapshot create`, "
                        "/api/v2/scan) against a query_mode='remote' row. Longer than "
                        "the interactive timeout on purpose: a snapshot is a "
                        "materialize, not an answer someone is waiting on. Its SIZE "
                        "bound is api.scan.max_result_bytes, not "
                        "max_bytes_per_remote_query. 0 disables. Default 900."
                    ),
                },
                "semantic_layer_catalogs": {
                    "kind": "array",
                    "item_kind": "string",
                    "hint": (
                        "Extra Unity Catalog catalogs the metric-view sync should "
                        "enumerate, beyond the configured `catalog`. Empty = just "
                        "`catalog`."
                    ),
                },
                "attach_enabled": {
                    "kind": "bool",
                    "default": False,
                    "hint": (
                        "EXPERIMENTAL. Attach Unity Catalog into DuckDB via the "
                        "uc_catalog + delta community extensions, so remote Databricks "
                        "tables can be JOINed against local parquets. Off by default: "
                        "it installs community extensions and sends the workspace PAT "
                        "to the endpoint (pin it with AGNES_REMOTE_ATTACH_HOST_ALLOWLIST). "
                        "Without it, remote rows still work — every statement runs on "
                        "the SQL warehouse instead."
                    ),
                },
            },
        },
        "snowflake": {
            "kind": "object",
            "hint": (
                "Snowflake connection (query_mode='remote' rows resolved locally by the "
                "DuckDB snowflake extension + query_mode='materialized' rows written to "
                "parquet on the scheduler tick). Use auth_type 'password' (default) with "
                "token_env / SNOWFLAKE_PASSWORD, or 'key_pair' with private_key_env / "
                "SNOWFLAKE_PRIVATE_KEY. Credential values are env/vault-backed, never from YAML."
            ),
            "fields": {
                "account": {
                    "kind": "string",
                    "hint": (
                        "Snowflake account identifier, e.g. xy12345 or "
                        "xy12345.eu-central-1. The `.snowflakecomputing.com` suffix is "
                        "appended when absent; give the bare identifier, no scheme or path. "
                        "The resulting host must pass AGNES_REMOTE_ATTACH_HOST_ALLOWLIST — "
                        "otherwise the credential is never sent and the row errors."
                    ),
                },
                "user": {
                    "kind": "string",
                    "hint": (
                        "Snowflake user the ATTACH authenticates as. Its default role "
                        "applies unless `role` overrides it."
                    ),
                },
                "database": {
                    "kind": "string",
                    "hint": (
                        "Default database registered rows resolve `bucket` against (a "
                        "dotted bucket 'database.schema' overrides it per row)."
                    ),
                },
                "warehouse": {
                    "kind": "string",
                    "hint": (
                        "Warehouse that runs materialize statements and live remote "
                        "queries. Sized and billed on the Snowflake side — Agnes does not "
                        "resume or suspend it."
                    ),
                },
                "role": {
                    "kind": "string",
                    "hint": ("Optional Snowflake role to assume. Empty = the user's default role."),
                },
                "auth_type": {
                    "kind": "select",
                    "options": ["password", "key_pair"],
                    "default": "password",
                    "hint": (
                        "Authentication method: 'password' or 'key_pair'. With key_pair, "
                        "set private_key_env / private_key_passphrase_env instead of token_env."
                    ),
                },
                "token_env": {
                    "kind": "string",
                    "default": SF_TOKEN_ENV,
                    "hint": (
                        "Name of the environment variable holding the Snowflake password "
                        "(the name, never the password itself). Used when auth_type is "
                        "'password'. Falls back to the vault secret of the same name when "
                        "the env var is unset. Default SNOWFLAKE_PASSWORD."
                    ),
                },
                "private_key_env": {
                    "kind": "string",
                    "default": SF_PRIVATE_KEY_ENV,
                    "hint": (
                        "Name of the environment variable holding the Snowflake private key "
                        "for key-pair auth (the name, never the key itself). The value may "
                        "be a PEM string or JSON with {private_key, passphrase?}. Used when "
                        "auth_type is 'key_pair'. Falls back to the vault secret of the same "
                        "name. Default SNOWFLAKE_PRIVATE_KEY."
                    ),
                },
                "private_key_passphrase_env": {
                    "kind": "string",
                    "default": SF_PRIVATE_KEY_PASSPHRASE_ENV,
                    "hint": (
                        "Name of the environment variable holding the optional passphrase "
                        "for the Snowflake private key. Used when auth_type is 'key_pair' "
                        "and the private key is encrypted. Falls back to the vault secret "
                        "of the same name. Default SNOWFLAKE_PRIVATE_KEY_PASSPHRASE."
                    ),
                },
                "max_bytes_per_materialize": {
                    "kind": "int",
                    "default": 10737418240,
                    "hint": (
                        "Cost guardrail for query_mode='materialized' Snowflake rows. "
                        "Caps the written parquet size in bytes (no dry-run primitive "
                        "exists, unlike BigQuery) — an oversized result is rejected, never "
                        "published. 0 disables. Default 10737418240 = 10 GiB."
                    ),
                },
            },
        },
    },
    "email": {
        # SMTP fields render via the populated path (always set when email
        # is enabled); no commonly-missing optional knobs at this layer.
    },
    "telegram": {
        # Rarely missing; leave empty.
    },
    "theme": {
        # Cosmetic only; rarely missing.
    },
    "server": {
        # TLS / hostname knobs are mostly env-side; nothing to surface here.
    },
    "auth": {
        "allowed_domain": {
            "kind": "string",
            "hint": (
                "Comma-separated list of allowed sign-in email domains (e.g. "
                "'acme.com,acme-internal.com'). Single domain works too. Empty → no "
                "domain restriction (any verified Google identity can sign in)."
            ),
        },
        # Keboola sign-in + token-header API auth. Declared so the panel
        # renders structured fields with hints — in particular the
        # `allow_token_header` boolean as a toggle rather than a free-text
        # box (Devin Review on PR #1288; same failure shape as `mcp` in
        # #1183). Read by app/auth/providers/keboola_verify.py.
        "keboola": {
            "kind": "object",
            "hint": (
                "Sign in with Keboola (OAuth) + optional X-StorageApi-Token "
                "API auth. Membership in project_id is the trust boundary — "
                "see config/instance.yaml.example for the full notes."
            ),
            "fields": {
                "stack_url": {
                    "kind": "string",
                    "hint": (
                        "Keboola stack URL tokens are verified against, e.g. "
                        "https://connection.keboola.com. https only. Empty → falls "
                        "back to data_source.keboola.stack_url."
                    ),
                },
                "oauth_host": {
                    "kind": "string",
                    "hint": (
                        "Host serving /oauth/authorize + /oauth/token. https only. "
                        "Empty → falls back to stack_url (OAuth lives on the "
                        "connection host)."
                    ),
                },
                "project_id": {
                    "kind": "string",
                    "required": True,
                    "hint": (
                        "Keboola project this instance is bound to — tokens from any "
                        "other project are rejected. Required for both the OAuth "
                        "login and the token-header auth, unless multi_project_mode "
                        "is select/auto — there '*' (or empty) means any project the "
                        "sign-in's introspect lists with an allowed role."
                    ),
                },
                "client_id": {
                    "kind": "string",
                    "hint": (
                        "OAuth client id issued by Keboola for your stack (not "
                        "self-service — ask your Keboola contact). Only the OAuth "
                        "login needs it; token-header auth works without one."
                    ),
                },
                "client_secret": {
                    "kind": "secret",
                    "hint": (
                        "OAuth client secret. Use a ${KEBOOLA_OAUTH_CLIENT_SECRET} "
                        "env-var reference (don't paste the secret directly)."
                    ),
                },
                "allowed_roles": {
                    "kind": "array",
                    "item_kind": "string",
                    "hint": (
                        "Keboola project roles permitted to sign in (e.g. admin, "
                        "share). Empty/unset → any role the project admits, "
                        "guest/readOnly included."
                    ),
                },
                "allow_token_header": {
                    "kind": "bool",
                    "default": _flag_default_path(("auth", "keboola", "allow_token_header"), False),
                    "hint": (
                        "Accept a Keboola Storage API master token in the "
                        "X-StorageApi-Token header as API authentication (existing "
                        "users only, never provisions). Off by default: a plain "
                        "Storage token carries no interactive factor, so this "
                        "bypasses any MFA/SSO enforced on web logins. See "
                        "docs/feature-flags.md."
                    ),
                },
                "multi_project_mode": {
                    "kind": "select",
                    "options": ["disabled", "select", "auto"],
                    "default": _switch_default_path(("auth", "keboola", "multi_project_mode"), "disabled"),
                    "hint": (
                        "What a Keboola sign-in does with the user's OTHER projects. "
                        "disabled = the single-project login only. select = discover "
                        "at login, the user imports chosen projects via "
                        "/api/auth/keboola/projects. auto = every allowed project is "
                        "connected on each login (PAT minted + vaulted, connection + "
                        "chat tools, kbc-<project>-<role> membership sync, semantic "
                        "layer for master tokens). Needs AGNES_VAULT_KEY. See "
                        "docs/feature-flags.md."
                    ),
                },
            },
        },
        # Microsoft Entra ID group sync. Declared so `group_sync_enabled`
        # renders as a toggle rather than a free-text box — same rationale
        # as `keboola.allow_token_header` above (Devin Review on PR #1288).
        # Read by app/auth/microsoft_group_sync.py.
        "microsoft": {
            "kind": "object",
            "hint": (
                "Mirror the signed-in user's Entra ID group memberships into "
                "user_group_members (source='microsoft_sync') on every Microsoft "
                "sign-in. See docs/auth-microsoft-oauth.md before enabling — it "
                "needs its own Entra admin-consent grant."
            ),
            "fields": {
                "group_sync_enabled": {
                    "kind": "bool",
                    "default": _flag_default_path(("auth", "microsoft", "group_sync_enabled"), False),
                    "hint": (
                        "Off by default: enabling it also widens the OAuth consent "
                        "scope requested at /auth/microsoft/login to the delegated "
                        "Graph permission GroupMember.Read.All, which needs admin "
                        "consent in the Entra app registration and a restart to take "
                        "effect. See docs/feature-flags.md."
                    ),
                },
            },
        },
    },
    "ai": {
        "base_url": {
            "kind": "string",
            "hint": (
                "Required for provider='openai_compat' (LiteLLM, OpenRouter, vLLM, etc.). "
                "Ignored when provider='anthropic'. Examples: https://litellm.example.com, "
                "https://openrouter.ai/api/v1."
            ),
        },
        "structured_output": {
            "kind": "select",
            "options": ["strict", "json", "auto"],
            "default": "auto",
            "hint": (
                "JSON-schema enforcement strategy. strict=Layer 1 only "
                "(Anthropic/OpenAI native, fail otherwise). json=Layer 1 + Layer 2 "
                "fallback. auto=all three layers including prompt-based JSON (most "
                "compatible, least strict)."
            ),
        },
    },
    # corporate_memory governance — optional. When the section is missing
    # from instance.yaml the system runs in legacy democratic-wiki mode
    # (no admin review). Schema mirrors config/instance.yaml.example
    # lines 224-317; renderer handles arbitrary depth + arrays + maps.
    "corporate_memory": {
        "distribution_mode": {
            "kind": "select",
            "options": ["mandatory_only", "admin_curated", "hybrid"],
            "default": "hybrid",
            "hint": (
                "How knowledge reaches users. mandatory_only = admin-only; "
                "admin_curated = admin + user voting as feedback; "
                "hybrid = default (mandatory from admin + optional from user "
                "voting). NOT YET ENFORCED (#1573): GET /api/memory/bundle "
                "currently ships every approved item to every user in all "
                "three modes — changing this value has no effect on "
                "distribution yet, only on what this field records."
            ),
        },
        "approval_mode": {
            "kind": "select",
            "options": ["review_queue", "auto_publish", "threshold"],
            "default": "review_queue",
            "hint": (
                "How AI-extracted items enter the system. review_queue = admin "
                "approval required (default); auto_publish = live immediately; "
                "threshold = high-confidence auto, low-confidence to queue."
            ),
        },
        "auto_publish_min_confidence": {
            "kind": "float",
            "default": 0.80,
            "hint": (
                "Only used when approval_mode='threshold'. Items with a "
                "confidence score >= this auto-publish; below it, they go "
                "to the review queue. Compare against "
                "corporate_memory.confidence.base to pick a realistic cutoff "
                "per source type — the default (0.80) is above every "
                "un-tuned base score, so threshold behaves like "
                "review_queue until you tune one or the other."
            ),
        },
        "review_period_months": {
            "kind": "int",
            "default": 6,
            "hint": "How often approved/mandatory items are flagged for re-review (months).",
        },
        "notify_on_new_items": {
            "kind": "bool",
            "default": True,
            "hint": "Notify km_admins when new pending items arrive.",
        },
        "sources": {
            "kind": "object",
            "hint": ("Knowledge-source ingestion. Each source has its own enabled flag + base confidence."),
            "fields": {
                "claude_local_md": {
                    "kind": "object",
                    "fields": {
                        "enabled": {"kind": "bool", "default": True},
                        "confidence_base": {
                            "kind": "float",
                            "default": 0.50,
                            "hint": "Confidence assigned to extractions from CLAUDE.local.md (0-1).",
                        },
                    },
                },
                "session_transcripts": {
                    "kind": "object",
                    "fields": {
                        "enabled": {"kind": "bool", "default": True},
                        "confidence_base": {"kind": "float", "default": 0.60},
                        "max_turns_per_session": {
                            "kind": "int",
                            "default": 100,
                            "hint": "Truncate transcripts longer than this many turns.",
                        },
                        "detection_types": {
                            "kind": "array",
                            "item_kind": "string",
                            "default": [
                                "correction",
                                "confirmation",
                                "unprompted_definition",
                            ],
                            "hint": ("Which extraction patterns to detect. Each entry is a detection-type tag."),
                        },
                    },
                },
            },
        },
        "extraction": {
            "kind": "object",
            "fields": {
                "model": {
                    "kind": "string",
                    "default": "claude-haiku-4-5-20251001",
                    "hint": "LLM used to extract knowledge. Override for cost or quality.",
                },
                "sensitivity_check": {"kind": "bool", "default": True},
                "contradiction_check": {"kind": "bool", "default": True},
            },
        },
        "confidence": {
            "kind": "object",
            "hint": "Confidence scoring + decay rules.",
            "fields": {
                "base": {
                    "kind": "map",
                    "key_kind": "string",
                    "value_kind": "float",
                    "default": {
                        "user_verification.correction": 0.90,
                        "user_verification.unprompted_definition": 0.90,
                        "user_verification.confirmation": 0.60,
                        "admin_mandate": 1.00,
                        "claude_local_md": 0.50,
                        "session_transcript": 0.50,
                    },
                    "hint": (
                        "Base score per source/detection. Keys are 'source_type' "
                        "or 'source_type.detection_type' (the dot is data, not "
                        "nesting)."
                    ),
                },
                "modifiers": {
                    # map<string, map<string, float>>. The renderer's structured
                    # editor for "map of objects with declared subfields" is a
                    # TODO (see admin_server_config.html); for now this falls
                    # back to a JSON textarea — admins editing it see the
                    # schema doc inline via the hint.
                    "kind": "map",
                    "key_kind": "string",
                    "value_kind": "object",
                    "value_fields": {},  # signals the JSON-textarea fallback
                    "hint": (
                        "Per-key modifier step sizes applied to base when "
                        "optional signals are present (3-level dotted paths). "
                        "Edit as a JSON object — outer keys mirror confidence.base "
                        "keys; inner objects map signal name to bonus float."
                    ),
                },
                "decay": {
                    "kind": "object",
                    "fields": {
                        "mode": {
                            "kind": "select",
                            "options": ["linear", "exponential"],
                            "default": "exponential",
                        },
                        "half_life_months": {
                            "kind": "int",
                            "default": 12,
                            "hint": "Used when mode=exponential.",
                        },
                        "decay_rate_monthly": {
                            "kind": "float",
                            "default": 0.02,
                            "hint": "Used when mode=linear.",
                        },
                        "floor": {
                            "kind": "map",
                            "key_kind": "string",
                            "value_kind": "float",
                            "default": {
                                "admin_mandate": 0.50,
                                "user_verification": 0.40,
                                "default": 0.0,
                            },
                            "hint": ("Per-source minimum confidence — items never decay below this floor."),
                        },
                    },
                },
            },
        },
        "contradiction_detection": {
            "kind": "object",
            "fields": {
                "enabled": {"kind": "bool", "default": True},
                "max_candidates": {
                    "kind": "int",
                    "default": 10,
                    "hint": "Max contradiction candidates to evaluate per new item.",
                },
            },
        },
        "entity_resolution": {
            "kind": "object",
            "fields": {
                "enabled": {"kind": "bool", "default": True},
                "entities": {
                    "kind": "map",
                    "key_kind": "string",
                    "value_kind": "array",
                    "value_item_kind": "string",
                    "default": {
                        "metrics": ["churn", "MRR", "ARR", "NPS", "CAC", "LTV"],
                        "products": ["Platform", "API", "Dashboard"],
                    },
                    "hint": ("Domain-entity vocabulary. Key = domain category; value = canonical names list."),
                },
            },
        },
        "domain_owners": {
            "kind": "map",
            "key_kind": "string",
            "value_kind": "array",
            "value_item_kind": "string",
            "hint": ("Per-domain admin emails. Key = domain name; value = email list."),
        },
        "domains": {
            "kind": "array",
            "item_kind": "string",
            "default": [
                "finance",
                "engineering",
                "product",
                "data",
                "operations",
                "infrastructure",
            ],
            "hint": ("Knowledge domains analysts can target. Each must match a key in domain_owners."),
        },
    },
    # materialize — file-lock TTL for the concurrent-materialize safety net.
    # A single field; more knobs may follow as the feature matures.
    "materialize": {
        "lock_ttl_seconds": {
            "kind": "int",
            "default": 86400,
            "hint": (
                "How long (seconds) before a stale materialize lock file is "
                "reclaimed. The lock is a .parquet.lock sibling file; if the "
                "holder process is hard-killed, the next attempt reclaims the "
                "lock once the file's mtime is older than this TTL. "
                "Default 86400 (24 h). Min 60, max 604800 (7 days). "
                "Lower only if you know materializes never exceed the new value "
                "and your host regularly hard-kills processes."
            ),
        },
    },
    "guardrails": {
        "min_description_chars": {
            "kind": "int",
            "default": 60,
            "hint": (
                "Minimum character floor for skill / agent / plugin "
                "descriptions on flea-market uploads (the inline content "
                "guardrail). Real-world Claude skill descriptions cluster "
                "150–220 chars; the default 60 is the bottom of the bar "
                "to catch placeholders. Bump to 100+ to push submitters "
                "closer to the ecosystem norm. Min 1."
            ),
        },
        "min_command_description_chars": {
            "kind": "int",
            "default": 25,
            "hint": (
                "Minimum character floor for slash-command descriptions. "
                "Tighter than skills because commands are one-verb "
                'actions ("run tests", "format code"). Default 25. Min 1.'
            ),
        },
        "min_distinct_words": {
            "kind": "int",
            "default": 5,
            "hint": (
                "Minimum number of DISTINCT words in any description "
                "string. Defends against padding-only descriptions like "
                '"description description description" that hit the '
                "character count but say nothing. Default 5. Min 1."
            ),
        },
        "min_body_chars": {
            "kind": "int",
            "default": 200,
            "hint": (
                "Minimum body-content floor for skill / agent files "
                "(the markdown after the YAML frontmatter). Real skill "
                "bodies run 500–2000 chars; the default 200 is a "
                '"one paragraph" floor that catches stubs. Min 1.'
            ),
        },
        "enabled": {
            "kind": "bool",
            "default": True,
            "hint": (
                "Master kill-switch for the LLM guardrail tier. When "
                "False (or when ANTHROPIC_API_KEY / LLM_API_KEY is "
                "absent), uploads still run the inline mechanical "
                "checks but skip the LLM security + content-quality "
                "review and auto-approve. Default True."
            ),
        },
        "review_model": {
            "kind": "select",
            "default": "haiku",
            "options": ["haiku", "sonnet", "opus"],
            "hint": (
                "Anthropic model tier for the LLM security + content "
                "review. Haiku is the cheapest and fastest; Sonnet / "
                "Opus catch subtler prompt-injection + vague descriptions "
                "at proportionally higher per-upload cost."
            ),
        },
        "blocked_quota_per_day": {
            "kind": "int",
            "default": 50,
            "hint": (
                "Per-submitter cap on `blocked_llm` + `review_error` "
                "rows in the trailing 24h. Bounds the worst case where "
                "a bot loops on bundles that survive inline checks but "
                "trip the async LLM reviewer. Inline failures are "
                "hard-rejected upstream (no row, not counted). 0 "
                "disables the quota. Default 50."
            ),
        },
        "blocked_bundle_ttl_days": {
            "kind": "int",
            "default": 30,
            "hint": (
                "How many days to keep a blocked bundle's bytes on disk. "
                "The submission row + sha256 + size always survive; only "
                "the bytes get removed. 0 disables the purge entirely. "
                "Default 30."
            ),
        },
        "stuck_review_grace_seconds": {
            "kind": "int",
            "default": 1800,
            "hint": (
                "How long a submission may stay at `status='pending_llm'` "
                "before the reaper flips it to `review_error`. Default "
                "1800 (30 min) comfortably exceeds Sonnet / Opus p99 "
                "wall time. 0 disables the reaper."
            ),
        },
    },
    "library": {
        "show_unverified_trust": {
            "kind": "bool",
            "default": True,
            "hint": (
                "Show the 'Community' trust marker beside the name of a "
                "user-authored Store item your organization has not verified. "
                "On by default, so every Library row states its provenance — "
                "Organization, Verified, or Community. Turn it off to restore "
                "the older look, where an unverified item is marked only by the "
                "absence of a marker. Organization and Verified are unaffected."
            ),
        },
        "auto_share_admin_uploads": {
            "kind": "bool",
            "default": False,
            "hint": (
                "Share a collection an admin creates in the Library with the "
                "Everyone group at creation, so admin uploads are visible to "
                "the whole workspace with no manual share step. The grant is "
                "an ordinary Everyone grant — revocable per collection in the "
                "Share dialog. Off by default: turning it on changes only "
                "collections created afterwards. Non-admin uploads and chat "
                "file drops stay private."
            ),
        },
    },
    "marketplace": {
        "curators_url": {
            "kind": "string",
            "hint": (
                "URL the 'See all curators →' link on /marketplace points to "
                "(e.g. an internal wiki page listing curators accountable for "
                "the curated marketplace). Empty → the link is hidden. "
                "Validated against private-IP allowlist on save (SSRF guard)."
            ),
        },
    },
    # Per-tenant connector params served by GET /api/connectors/params and
    # written into every analyst's `.claude/agnes/.env` by `agnes init`.
    # Only `globals` is registry-known — the sibling keys are per-connector
    # slugs (connector-gws, connector-atlassian, …) whose set comes from the
    # seed manifest at runtime, so they can't be enumerated statically here.
    # Operators add them via the section's JSON editor; unknown slugs are
    # dropped (with a server-side warning) by the manifest allowlist in
    # app/api/connectors.py.
    "connectors": {
        "globals": {
            "kind": "object",
            "hint": (
                "Instance-wide params written to every analyst's .env "
                "(e.g. AGNES_INSTANCE_BRAND). Keep user credentials and "
                "server-side secrets out of globals; connector app "
                "identifiers (e.g. the GWS OAuth client secret) belong "
                "under their per-connector section as plain values."
            ),
        },
    },
}

# Keys whose values must be redacted from the audit diff. We match
# substring (case-insensitive) so `client_secret`, `api_token`,
# `webapp_secret_key`, `bot_token`, `password`, `smtp_password`, etc. all
# get masked even when nested.
_SECRET_KEY_PATTERNS: tuple[str, ...] = (
    "secret",
    "token",
    "password",
    "passwd",
    "api_key",
    # #19 hardening: mask credentials stored under keys that the original
    # four-pattern list missed — private keys, generic credential fields, and
    # DB DSNs with embedded userinfo. ("pat" is intentionally omitted: it would
    # substring-match innocuous keys like "path"/"data_path" and over-redact.)
    "private",
    "credential",
    "dsn",
)


def _declared_boolean_fields() -> frozenset[str]:
    """Field names the registry declares as ``kind: "bool"``.

    A boolean cannot be a credential, so these must never be masked — and the
    consequence of masking one is worse than a cosmetic glitch. ``_mask(False)``
    returns ``"***"``, the UI's bool renderer coerces with ``!!value``, and
    ``!!"***"`` is ``true``: an operator who turned a switch OFF sees it ON and
    the next "Save section" posts ``true``, silently undoing what they did.
    That is exactly what happened to ``mcp.allow_query_param_token``, whose
    name contains the substring "token" (Devin Review on #1183).

    Derived from the registry rather than an allowlist so a future boolean is
    covered without anyone remembering this failure mode. Both registries feed
    it: the ``_KNOWN_FIELDS`` panel declarations AND the ``SWITCHES`` registry
    — a bool switch whose leaf name carries a redactor substring (e.g.
    ``auth.keboola.allow_token_header`` / ``mcp.allow_query_param_token``, both
    matching "token") would otherwise render as ``***`` on the settings screen,
    the same failure mode this guards against.
    """
    from_known = {
        name for section in _KNOWN_FIELDS.values() for name, spec in section.items() if spec.get("kind") == "bool"
    }
    from_switches = {s.config_keys[-1] for s in SWITCHES if s.kind == "bool" and s.config_keys}
    return frozenset(from_known | from_switches)


def _is_secret_key(key: str) -> bool:
    """True if a config key holds a credential and should be masked in audit logs."""
    k = key.lower()
    if k in _declared_boolean_fields():
        return False
    return any(pat in k for pat in _SECRET_KEY_PATTERNS)


def _mask(value: Any) -> str:
    """Replacement value used in the audit diff for secret fields.

    We deliberately do NOT preserve length or any hint about the secret —
    the diff is read by other admins, and there's no operator value to
    leaking "the new SMTP password is 16 chars". `***` is enough to show
    that the field changed without exposing it.
    """
    if value in (None, ""):
        return "<empty>"
    return "***"


# Sentinel values produced by `_mask`. Any patch leaf that arrives at a
# secret-keyed slot still bearing one of these strings means the caller
# round-tripped the GET payload (which redacts secret-keyed children inside
# nested objects) without changing the value — `_strip_redacted_sentinels`
# drops the leaf so deep-merge preserves whatever the overlay already had,
# rather than persisting the placeholder on top of the real secret.
_REDACTED_SENTINELS: frozenset = frozenset({"***", "<empty>"})


def _strip_redacted_sentinels(value: Any, key_hint: str = "") -> Any:
    """Recursively drop secret-keyed leaves whose value is a redaction sentinel.

    Symmetric with `_redact`: the GET handler masks secret-keyed children
    inside nested objects so the form never shows cleartext, and this
    function is the write-side counterpart that ensures the placeholder
    doesn't make a round-trip back into the overlay. Defense-in-depth
    alongside the client-side `scrubRedactedSecrets` in
    `admin_server_config.html` — an API caller (CLI / script) that forgets
    to scrub still can't corrupt secrets via this endpoint.
    """
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            if _is_secret_key(k) and isinstance(v, str) and v in _REDACTED_SENTINELS:
                continue
            out[k] = _strip_redacted_sentinels(v, k)
        return out
    if isinstance(value, list):
        return [_strip_redacted_sentinels(item, key_hint) for item in value]
    return value


def _redact(value: Any, key_hint: str = "") -> Any:
    """Recursively mask secret-looking fields in a config subtree.

    `key_hint` is the parent key — used so a string value like
    ``"${KEBOOLA_TOKEN}"`` under ``token_env`` is masked even though the
    value itself isn't a credential, because the key signals it points at
    one.
    """
    if isinstance(value, dict):
        return {k: (_mask(v) if _is_secret_key(k) else _redact(v, k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item, key_hint) for item in value]
    if key_hint and _is_secret_key(key_hint):
        return _mask(value)
    return value


def _diff_dicts(before: dict, after: dict, path: str = "") -> List[Dict[str, Any]]:
    """Flat list of changed fields between two dicts.

    Output: [{"path": "email.smtp_host", "before": "...", "after": "..."}].
    Diff is computed on RAW values, then each row's `before`/`after` is
    masked via `_mask` when the leaf key matches `_is_secret_key` — pre-
    masking the inputs would collapse a secret rotation (e.g. password A
    → password B) into "no diff" because both sides redact to ``"***"``,
    and the audit log would then silently fail to record one of the most
    security-relevant changes. Compare raw, redact when emitting.

    Recurses into a dict on either side (treating the missing side as
    `{}`) so adding a brand-new section reports per-field paths
    (`email.smtp_host`) rather than a single opaque `email` blob — that
    keeps the audit row useful when an admin populates a section for the
    first time.
    """
    changes: List[Dict[str, Any]] = []
    keys = set(before.keys()) | set(after.keys())
    for key in sorted(keys):
        new_path = f"{path}.{key}" if path else key
        b_val = before.get(key)
        a_val = after.get(key)
        b_is_dict = isinstance(b_val, dict)
        a_is_dict = isinstance(a_val, dict)
        # Dict-vs-dict (or dict-vs-None) → recurse for per-field paths.
        if b_is_dict and a_is_dict:
            changes.extend(_diff_dicts(b_val, a_val, new_path))
        elif b_is_dict and a_val is None:
            changes.extend(_diff_dicts(b_val, {}, new_path))
        elif a_is_dict and b_val is None:
            changes.extend(_diff_dicts({}, a_val, new_path))
        # Dict↔scalar shape change is recorded as a single replacement at
        # the parent path. Recursing with `{}` would lose the scalar side
        # entirely (admin sets `keboola: {…}` to `keboola: "disabled"` —
        # auditor would see members removed but never the new value).
        # The dict side may itself contain secret-keyed children (e.g.
        # `keboola: {token_env: ${KEBOOLA_TOKEN}}` resolved to cleartext);
        # `_redact` masks those children even when the parent key isn't
        # secret-named, so the audit log doesn't leak ${ENV_VAR}-resolved
        # values when a section is replaced wholesale.
        elif b_is_dict != a_is_dict:
            if _is_secret_key(key):
                changes.append(
                    {
                        "path": new_path,
                        "before": _mask(b_val),
                        "after": _mask(a_val),
                    }
                )
            else:
                changes.append(
                    {
                        "path": new_path,
                        "before": _redact(b_val, key) if b_is_dict else b_val,
                        "after": _redact(a_val, key) if a_is_dict else a_val,
                    }
                )
        elif b_val != a_val:
            if _is_secret_key(key):
                changes.append(
                    {
                        "path": new_path,
                        "before": _mask(b_val),
                        "after": _mask(a_val),
                    }
                )
            else:
                changes.append({"path": new_path, "before": b_val, "after": a_val})
    return changes


def _deep_merge(base: dict, patch: dict) -> dict:
    """Merge `patch` into `base` recursively, returning a new dict.

    Patch values overwrite base values. Dict-into-dict recurses; everything
    else (lists, scalars, None) is replaced wholesale — admin sets
    ``email: {smtp_port: 465}`` and we don't try to re-merge nested ports.
    """
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_current_instance_yaml() -> dict:
    """Return the editor's view of instance.yaml — deep-merge of static +
    overlay via ``app.instance_config.load_instance_config``.

    Readers (GET /server-config) hit the cache and trust that writers
    invalidate. Writers must call ``reset_cache()`` explicitly *before*
    the read so they see the latest disk state in the read-modify-write
    sequence. The shared helper is the authoritative source so the editor
    never sees a different view than the rest of the running app.
    """
    from app.instance_config import load_instance_config

    return load_instance_config()


def _public_view(config: dict) -> dict:
    """Return a config dict safe to render in the admin UI form.

    Deep-copies and redacts secret-looking fields so an admin can see
    *which* fields are populated without the cleartext leaking into the
    rendered HTML / browser DevTools.
    """
    import copy

    return _redact(copy.deepcopy(config))


# --- Overlay export/apply (agnes admin config export / apply, Track D3) -----
#
# `_public_view` above (used by GET /server-config) masks EVERY secret-shaped
# key to `***`/`<empty>` for on-screen display — including keys like
# `token_env`/`private_key_env` that hold an env-var NAME, not a credential
# (see the docstring on `_is_secret_key`'s callers). That over-redaction is
# fine for a settings form nobody round-trips, but it is wrong for
# `agnes admin config export`: a NAME reference is not a secret and must
# survive so a fresh instance can be onboarded from the exported YAML.
#
# `_export_scrub` instead OMITS a secret-shaped leaf only when its value is
# a literal (neither an env-var-name key nor an unresolved `${VAR}`
# reference) — the one shape that could actually leak a credential into a
# git-reviewed file.
_ENV_REF_RE = re.compile(r"^\$\{[A-Za-z0-9_]+\}$")


def _is_env_name_key(key: str) -> bool:
    """True for the `*_env` naming convention (`token_env`, `private_key_env`,
    …) that stores an env-var NAME, never the credential itself — see
    `config/instance.yaml.example`'s "never YAML" comments on these fields."""
    return key.lower().endswith("_env")


# Env-var identifiers are `[A-Za-z_][A-Za-z0-9_]*` (POSIX shell rules — the
# same charset every `os.environ` lookup in this codebase assumes). A
# `*_env`-suffixed key is only a safe pass-through when its VALUE actually
# has this shape — an admin who mis-suffixes a pasted literal (a plausible
# mistake in the free-form `connectors` section, where field names are
# admin-chosen) must not get free rein over `_is_env_name_key`'s allowlist.
_ENV_NAME_VALUE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _is_valid_env_name_value(value: Any) -> bool:
    """True if `value` looks like an env-var NAME rather than a pasted
    literal — the shape a `*_env`-suffixed key's value must have to be
    trusted as a name reference instead of scrubbed as an ordinary leaf."""
    return isinstance(value, str) and bool(_ENV_NAME_VALUE_RE.match(value))


def _looks_like_env_ref(value: Any) -> bool:
    """True if `value` is an unresolved `${VAR}` placeholder — a pointer to
    an env var, not a cleartext secret."""
    return isinstance(value, str) and bool(_ENV_REF_RE.match(value))


# Sections whose sub-schema is genuinely free-form — admin-typed keys that
# no static registry enumerates. `connectors` is the one instance today: its
# `_KNOWN_FIELDS` entry above documents that the sibling keys of `globals`
# are per-connector slugs sourced from the runtime seed manifest, and its
# own hint text SANCTIONS storing "connector app identifiers... as plain
# values" there — e.g. `connectors."connector-slack".SLACK_WEBHOOK_URL`.
# `_is_secret_key`'s 8 substring patterns cannot police a key name they've
# never seen, so for these sections export can't use a key-name blocklist
# at all — it flips to a POSITIVE allowlist instead: keep ONLY a `${VAR}`
# reference or an `*_env`-named key; every other literal is omitted,
# INCLUDING one that doesn't look secret-shaped (a plain string, a bool, a
# number) — the section's admin-typed nature makes any finer carve-out
# unreliable by construction. The transparency note the endpoint returns
# alongside `sections` (`omitted_keys`) is what keeps this from silently
# discarding a legitimate non-secret global.
_FREE_FORM_EXPORT_SECTIONS: frozenset = frozenset({"connectors"})

# --- Value-shape backstop ----------------------------------------------
#
# Defense-in-depth for a literal secret pasted into a field whose NAME
# doesn't hint at a credential anywhere in the overlay, in ANY section —
# not just the free-form ones above (e.g. an admin naming a custom BigQuery
# field `webhook` instead of `token`). Deliberately conservative: an
# ordinary hostname, email, or short enum must never be caught, so this
# only fires on shapes that are unambiguously a credential — a PEM block, a
# signed JWT, a URL carrying userinfo or a long opaque path segment (the
# shape of a Slack/Discord/Teams incoming-webhook token, a presigned link,
# an OAuth callback code, …), or a single long opaque alphanumeric run
# typical of an API key/hash. This is a backstop, not a claim of
# completeness — it will not catch every secret shape, which is exactly why
# `_FREE_FORM_EXPORT_SECTIONS` and the `omitted_keys` transparency note
# exist alongside it rather than in place of it.
_JWT_RE = re.compile(r"^eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}$")
_URL_USERINFO_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s@]+:[^/\s@]+@")
_ALNUM_ONLY_RE = re.compile(r"^[A-Za-z0-9]+$")
_URL_TOKEN_SEGMENT_MIN_LEN = 20
_OPAQUE_TOKEN_MIN_LEN = 24


def _looks_like_secret_value(value: Any) -> bool:
    """True if `value` is a literal string that is unambiguously
    credential-shaped, independent of what key it is stored under."""
    if not isinstance(value, str) or _looks_like_env_ref(value):
        return False
    v = value.strip()
    if not v:
        return False
    if "-----BEGIN" in v:
        return True
    if _JWT_RE.match(v):
        return True
    if v.lower().startswith(("http://", "https://")):
        if _URL_USERINFO_RE.match(v):
            return True
        path = v.split("?", 1)[0].split("#", 1)[0]
        segments = [s for s in path.split("/") if s]
        last = segments[-1] if segments else ""
        return len(last) >= _URL_TOKEN_SEGMENT_MIN_LEN and bool(_ALNUM_ONLY_RE.match(last))
    return len(v) >= _OPAQUE_TOKEN_MIN_LEN and bool(_ALNUM_ONLY_RE.match(v))


def _export_scrub(
    value: Any,
    *,
    free_form: bool = False,
    path: str = "",
    omitted: Optional[List[str]] = None,
) -> Any:
    """Recursively drop secret-shaped values from an overlay subtree —
    dict VALUES keyed on their key name, list ITEMS on their shape alone
    (a list element has no key to name-match).

    A `${VAR}` reference always wins and is kept verbatim, checked first
    and unconditionally on every leaf regardless of key or position. Next,
    for a dict leaf only, a `*_env`-suffixed key whose value actually looks
    like an env-var NAME (`_is_valid_env_name_value` — not a pasted
    literal) is kept verbatim too. Past those two carve-outs, three
    independent gates, any one omits a leaf:

    1. Key-name gate (`_is_secret_key`) — dict leaves only, as before.
    2. Free-form-section gate (`free_form=True`, set by the caller for every
       section in `_FREE_FORM_EXPORT_SECTIONS`, and propagated to every
       descendant — dict AND list — once set) — a positive allowlist:
       every literal is omitted regardless of key name, list position, or
       shape.
    3. Value-shape gate (`_looks_like_secret_value`) — fires everywhere,
       free-form or not, dict value or list item alike.

    A dict/list value caught by gate 1 or 2 is not blanket-dropped — it is
    recursed into (forcing `free_form=True` for the subtree) so a nested
    `${VAR}` reference or `*_env` key inside it still survives; only an
    actual literal leaf is omitted. `omitted` collects the dotted/indexed
    path (`a.b[2].c`) of every leaf actually dropped, so the caller can
    surface a transparency note instead of a silent drop.
    """
    if omitted is None:
        omitted = []
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            child_path = f"{path}.{k}" if path else k
            if _looks_like_env_ref(v):
                out[k] = v
                continue
            if _is_env_name_key(k) and _is_valid_env_name_value(v):
                out[k] = v
                continue
            if _is_secret_key(k) or free_form or _looks_like_secret_value(v):
                if isinstance(v, (dict, list)):
                    out[k] = _export_scrub(v, free_form=True, path=child_path, omitted=omitted)
                else:
                    omitted.append(child_path)
                continue
            out[k] = _export_scrub(v, free_form=free_form, path=child_path, omitted=omitted)
        return out
    if isinstance(value, list):
        out_list: List[Any] = []
        for i, item in enumerate(value):
            item_path = f"{path}[{i}]"
            if isinstance(item, (dict, list)):
                out_list.append(_export_scrub(item, free_form=free_form, path=item_path, omitted=omitted))
                continue
            if _looks_like_env_ref(item):
                out_list.append(item)
                continue
            if free_form or _looks_like_secret_value(item):
                omitted.append(item_path)
                continue
            out_list.append(item)
        return out_list
    return value


def _load_raw_overlay() -> dict:
    """Read `instance.yaml` straight off disk — no static-config merge, no
    `${VAR}` resolution. This is the literal file `POST /server-config`
    writes and `agnes admin config apply` re-produces; `_load_current_
    instance_yaml()` (used by GET /server-config) is the wrong source here
    because it merges in the static file's defaults and resolves every env
    reference to its runtime value.
    """
    import yaml

    from app.secrets import _state_dir

    overlay_path = _state_dir() / "instance.yaml"
    if not overlay_path.exists():
        return {}
    try:
        data = yaml.safe_load(overlay_path.read_text())
    except Exception as e:
        logger.exception("server-config/overlay: refusing to read corrupt overlay at %s", overlay_path)
        raise HTTPException(
            status_code=500,
            detail=f"cannot read the instance.yaml overlay at {overlay_path}: {e}",
        ) from e
    return data if isinstance(data, dict) else {}


_REPOINT_SAMPLE_SIZE = 5


def _guard_connection_repoint(
    before: dict,
    sections: Dict[str, Dict[str, Any]],
    confirmed: bool,
) -> None:
    """Refuse an unconfirmed change to *which upstream* a data source points at.

    Registrations resolve their upstream against the instance's one connection
    per source (see `app/connection_identity.py`), and the result is baked into
    the extract's `_remote_attach.url` and each remote view's
    ``sf."SCHEMA"."TABLE"``. Repointing the connection therefore invalidates
    every existing row of that source at once — and the failure is silent from
    the operator's seat: reads fail at bind time deep in a query, materialized
    syncs fail at COPY time, and `last_sync_status` keeps showing the last
    successful run. The save path used to apply such a patch without a word.

    Fires only when the source already HAS registrations: first-time setup has
    nothing to break, and nagging there would train operators to click through.
    """
    if confirmed:
        return
    patch = sections.get("data_source")
    if not isinstance(patch, dict):
        return

    before_ds = before.get("data_source")
    before_ds = before_ds if isinstance(before_ds, dict) else {}

    from app.connection_identity import identity_changes
    from src.repositories import table_registry_repo

    for source, source_patch in patch.items():
        if not isinstance(source_patch, dict):
            continue
        before_block = before_ds.get(source)
        changes = identity_changes(source, before_block if isinstance(before_block, dict) else {}, source_patch)
        if not changes:
            continue

        try:
            affected = table_registry_repo().list_by_source(source)
        except Exception:
            # A registry the guard cannot read is not a reason to block a
            # config save — the operator may be fixing exactly that.
            logger.exception("connection-repoint guard: registry lookup failed for %s", source)
            continue
        if not affected:
            continue

        raise HTTPException(
            status_code=409,
            detail={
                "error": "connection_change_affects_registrations",
                "source": source,
                # Same masking rule as the audit diff — the field name carries
                # the operator-relevant signal, so a credential-pointer leaf
                # does not need its value echoed to be understood.
                "changes": [
                    {
                        "field": c["field"],
                        "before": _mask(c["before"]) if _is_secret_key(c["field"]) else c["before"],
                        "after": _mask(c["after"]) if _is_secret_key(c["field"]) else c["after"],
                    }
                    for c in changes
                ],
                "affected_tables": len(affected),
                "sample_tables": [str(r.get("id")) for r in affected[:_REPOINT_SAMPLE_SIZE]],
                "hint": (
                    f"{len(affected)} registered table(s) resolve against the current "
                    f"{source} connection and will stop resolving after this change; "
                    "they need re-registering (or a matching schema on the new "
                    "upstream). Resend with confirm_connection_change=true to apply."
                ),
            },
        )


class ServerConfigUpdateRequest(BaseModel):
    """Patch payload for POST /api/admin/server-config.

    Only the sections listed in `_EDITABLE_SECTIONS` are accepted; anything
    else is rejected with 400. `confirm_danger` must be true if the patch
    touches any danger-zone section (auth.*, server.*), and
    `confirm_connection_change` must be true to repoint a data source that
    already has registrations.
    """

    sections: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        description="Per-section patch dict (e.g. {'instance': {'name': 'X'}})",
    )
    confirm_danger: bool = Field(
        default=False,
        description="Must be true to apply changes touching auth.* or server.*",
    )
    confirm_connection_change: bool = Field(
        default=False,
        description=(
            "Must be true to repoint a data_source connection that registered "
            "tables already resolve against (see 409 "
            "connection_change_affects_registrations)"
        ),
    )


# Optional BQ fields whose runtime defaults are documented but which used to
# be invisible in the editor when YAML omitted them. The data_source.bigquery
# subtree renders as a JSON textarea; a key that's absent from the GET
# payload literally cannot appear in the form for the operator to edit. We
# surface them with their documented defaults so the UI always shows them as
# editable knobs — see Phase J of the admin-tables-cleanup work.
#
#   - billing_project: defaults to data project; explicit value needed when
#     the SA can read the data project but not bill against it.
#   - max_bytes_per_materialize: cost guardrail for `query_mode='materialized'`
#     (default 10 GiB; 0 disables; null falls through to the default).
_BQ_OPTIONAL_FIELD_DEFAULTS: Dict[str, Any] = {
    # `billing_project` intentionally NOT seeded here. The empty-string
    # default would inject `billing_project: ""` into every GET payload,
    # which makes the JS `isUnset = (value === undefined)` check evaluate
    # False — and the `(defaults to <project>)` placeholder feature
    # (#160 §4.7.5) would never render. Leaving it absent keeps the
    # field in the unset rendering path so placeholder_from fires.
    # Devin Review iter #3 on PR #168.
    "max_bytes_per_materialize": 10737418240,
    "bq_max_scan_bytes": 5368709120,
}


def _ensure_bq_optional_fields(sections: Dict[str, Any]) -> None:
    """In-place: add missing BQ optional fields to data_source.bigquery so the
    UI's JSON-textarea renders them as editable keys. Existing values are
    preserved — only absent keys are populated with their documented default.
    """
    ds = sections.get("data_source")
    if not isinstance(ds, dict):
        return
    bq = ds.get("bigquery")
    if not isinstance(bq, dict):
        # No BQ subsection — leave alone. Non-BQ instances don't need these
        # knobs, and creating an empty bigquery dict would be misleading.
        return
    for key, default in _BQ_OPTIONAL_FIELD_DEFAULTS.items():
        bq.setdefault(key, default)


_UNSET = object()


def _known_fields_resolved() -> dict:
    """Per-request view of ``_KNOWN_FIELDS`` with preset-aware defaults.

    The registry's ``default`` literals are what the panel renders for an
    UNSET field — and what "Save section" then persists verbatim
    (``collectSection`` posts every rendered leaf). For the preset-coupled
    knobs a static literal is therefore a footgun: on an
    ``instance.experience: redesign`` instance the switch would render OFF
    (runtime: ON) and a routine section save would silently flip the whole
    instance back to the classic model / blue theme (Devin Review on
    #1199 — the same failure mode the #1190 unset-boolean fix addressed).
    Patch the coupled leaves at request time from the same preset helpers
    the runtime getters resolve through.
    """
    import copy

    from app.instance_config import get_experience, preset_flag_default, preset_knob_default

    fields = copy.deepcopy(_KNOWN_FIELDS)
    fields["features"]["stack_auto_membership"]["default"] = preset_flag_default("stack_auto_membership")
    fields["instance"]["theme"]["default"] = preset_knob_default("theme")
    # The preset ITSELF, not just the leaves it couples. On an instance that
    # sets it by env (`AGNES_INSTANCE_EXPERIENCE=redesign`) the panel rendered
    # the static `classic` for the unset key, so a routine section save wrote
    # `instance.experience: classic` into the overlay — invisible while the
    # env var is present, and a silent revert of the whole preset the day the
    # operator drops it. Same failure mode as the coupled leaves above, one
    # tier up (Devin on #1199).
    fields["instance"]["experience"]["default"] = get_experience()
    # Same one-tier-up failure for the Keboola multi-project mode: the
    # recommended deployment sets it ONLY via env
    # (AGNES_KEBOOLA_MULTI_PROJECT_MODE=auto), so the unset key rendered the
    # registry's static `disabled` and a routine auth-section save persisted
    # that into the overlay — invisible while the env var is present, a
    # silent revert of the whole feature the day the operator drops it
    # (Devin Review on this PR). Render the RESOLVED mode instead, so a save
    # writes what is actually in force.
    from app.switches import switch_value

    fields["auth"]["keboola"]["fields"]["multi_project_mode"]["default"] = switch_value("keboola_multi_project_mode")
    # The project id is required exactly when the single-project gate is in
    # force. Under an active discovery mode (`select`/`auto`) it is optional
    # — unset/`'*'` IS the wildcard — and a static `required: True` rendered
    # a required marker beside a hint telling the operator to leave it
    # blank, nudging them to pin a project and silently disable the
    # wildcard they intended (Devin Review on this PR, sixteenth round).
    fields["auth"]["keboola"]["fields"]["project_id"]["required"] = (
        switch_value("keboola_multi_project_mode") == "disabled"
    )
    return fields


def _feature_flags_inventory() -> List[Dict[str, Any]]:
    """Read-only snapshot of every registered feature flag (#1022).

    ``source`` tells the operator where the effective value came from:
    ``"env"`` when the flag's env var is present in the process environment
    (wins regardless of instance.yaml), ``"config"`` when instance.yaml (the
    static base or the admin server-config overlay, deep-merged — see
    ``load_instance_config``) sets the key explicitly, or ``"default"`` when
    neither is set and the flag's hardcoded default applies. The
    ``config``-vs-``default`` distinction is resolved with a sentinel probe
    through ``get_value`` rather than re-deciding truthiness here, so this
    stays a thin read of ``feature_enabled``'s own resolution.

    ``chat`` is the one flag whose runtime does NOT read the merged config:
    ``app/main.py`` loads it via ``load_chat_config(DATA_DIR/state/
    instance.yaml)`` — the writable overlay file alone, never the static
    ``config/instance.yaml`` base. To keep this panel honest (an operator
    setting ``chat.enabled`` only in the static base would otherwise see
    "on" here while the running app has chat off), the chat row resolves
    from the same overlay-only source the runtime uses.
    """
    from app.instance_config import (
        FEATURE_FLAGS,
        PRESET_COUPLED_FLAGS,
        feature_enabled,
        get_experience,
        get_value,
        preset_flag_default,
    )

    # The experience preset leads the inventory as its own (string-valued)
    # row — it is the one-line adoption switch whose value explains why the
    # preset-coupled rows below may resolve away from their static defaults
    # (spec 2026-08-07-default-chrome-ux-parity).
    if os.environ.get("AGNES_INSTANCE_EXPERIENCE") is not None:
        exp_source = "env"
    else:
        exp_probe = get_value("instance", "experience", default=_UNSET)
        exp_source = "default" if exp_probe is _UNSET else "config"
    out: List[Dict[str, Any]] = [
        {
            "name": "instance.experience",
            # String-valued row: value_label carries the resolved preset for
            # display; effective mirrors it as "is the redesign preset on"
            # so the row satisfies the same schema every switch row carries
            # (tests/test_feature_flags.py::TestInventoryExposesSwitchMetadata).
            "value_label": get_experience(),
            "effective": get_experience() == "redesign",
            "source": exp_source,
            "default": "redesign",
            "env_var": "AGNES_INSTANCE_EXPERIENCE",
            "description": (
                "Experience preset — retired as a choice; `redesign` is the only "
                "option and the default. Changes the DEFAULTS of "
                "instance.theme and features.stack_auto_membership — either can "
                "still be overridden per-knob. instance.ui_layout is NOT one of "
                "those overridable knobs any more: the rail chrome is "
                "hard-wired (Wave 0, 2026-08), so a configured value is ignored "
                "with a startup warning instead of being honored."
            ),
            "effect": "live",
            "editable": True,
            "lock_reason": "",
        }
    ]
    for flag in FEATURE_FLAGS:
        if flag.name == "experience":
            # The preset's registry entry is kind="select" — the leading
            # string-valued row above already renders it (value_label +
            # effective-as-redesign); running it through the boolean
            # ``feature_enabled`` below would coerce "classic" to True.
            continue
        if flag.kind != "bool":
            # Every OTHER select switch resolves its STRING through the
            # switch registry. The boolean path below coerces any option to
            # True ("disabled" included — coerce_flag_value only knows
            # 0/false/no/off/empty), so a three-way mode read as a boolean
            # told the operator it was on while it was off (Devin Review on
            # this PR; the experience skip above was keyed by NAME, so the
            # second select switch fell straight into the trap the comment
            # there warns about). Same row shape as the leading experience
            # row: value_label carries the mode, effective mirrors
            # "resolved away from the default".
            if flag.name in _CHAT_RUNTIME_FLAGS:
                # A select that is ALSO chat-runtime-resolved (chat_provider):
                # switch_value() raises for runtime_view switches by design —
                # the runtime reads the overlay file alone, via
                # load_chat_config — so its string comes from the same view
                # the boolean chat flags use below.
                value_raw, source = _chat_flag_runtime_view(flag)
                value = str(value_raw)
            else:
                from app.switches import switch_value

                value = str(switch_value(flag.name))
                if os.environ.get(flag.env_var) is not None:
                    source = "env"
                else:
                    probe = get_value(*flag.config_keys, default=_UNSET)
                    source = "default" if probe is _UNSET else "config"
            out.append(
                {
                    "name": flag.name,
                    "value_label": value,
                    "effective": value != flag.default,
                    "source": source,
                    "default": flag.default,
                    "env_var": flag.env_var,
                    "description": flag.description,
                    "effect": flag.effect,
                    "editable": flag.editable,
                    "lock_reason": flag.lock_reason,
                }
            )
            continue
        if flag.name in _CHAT_RUNTIME_FLAGS:
            effective, source = _chat_flag_runtime_view(flag)
        else:
            # Preset-coupled flags resolve against the preset-implied default
            # (what the runtime getters actually use), so this panel never
            # reports "off/default" while the running instance has the flag
            # on via ``experience: redesign``.
            default_val = preset_flag_default(flag.name) if flag.name in PRESET_COUPLED_FLAGS else flag.default
            effective = feature_enabled(*flag.config_keys, env_var=flag.env_var, default=default_val)
            if os.environ.get(flag.env_var) is not None:
                source = "env"
            else:
                probe = get_value(*flag.config_keys, default=_UNSET)
                if probe is not _UNSET:
                    source = "config"
                elif flag.name in PRESET_COUPLED_FLAGS and default_val != flag.default:
                    source = "preset"
                else:
                    source = "default"
        out.append(
            {
                "name": flag.name,
                "effective": effective,
                "source": source,
                "default": flag.default,
                "env_var": flag.env_var,
                "description": flag.description,
                "effect": flag.effect,
                "editable": flag.editable,
                "lock_reason": flag.lock_reason,
            }
        )
    return out


#: Registry switches whose runtime value comes from `load_chat_config` rather
#: than the merged config, mapped to the ChatConfig attribute holding it.
#: Derived from `runtime_view` so a third chat-resolved switch cannot be added
#: without the panel following — the previous hand-written dict was the reason
#: this needed a Devin Review note on #1146/#1157.
#:
#: Restricted to `config_keys[0] == "chat"`: `_chat_flag_runtime_view` below
#: reads `raw.get("chat")` and calls `load_chat_config`, so a switch outside
#: the chat section declaring `runtime_view` would silently resolve against
#: the wrong section rather than fail. The assertion turns that into a loud
#: import-time error instead — a non-chat `runtime_view` needs its own
#: resolver, not a bigger map here.
_CHAT_RUNTIME_FLAGS = {
    s.name: s.runtime_view for s in SWITCHES if s.runtime_view and s.config_keys and s.config_keys[0] == "chat"
}
assert len(_CHAT_RUNTIME_FLAGS) == sum(1 for s in SWITCHES if s.runtime_view), (
    "a switch declares runtime_view outside the chat section; _chat_flag_runtime_view "
    "only knows how to resolve chat.* — give it a dedicated resolver instead of widening this map"
)


def _chat_flag_runtime_view(flag) -> tuple:
    """(effective, source) for a flag the chat runtime resolves itself.

    ``load_chat_config`` already applies the flag's env override, so
    ``effective`` matches what a restart would produce; the source label probes
    the overlay file for the explicit key.
    """
    import yaml

    from app.chat.config import load_chat_config
    from app.secrets import _state_dir

    key = _CHAT_RUNTIME_FLAGS[flag.name]
    overlay_path = _state_dir() / "instance.yaml"
    effective = getattr(load_chat_config(overlay_path), key)
    env_raw = os.environ.get(flag.env_var)
    # The "env" label must mirror each flag's own resolver: the boolean chat
    # flags coerce ANY set value (blank included), but the select resolver
    # (`_resolve_chat_provider`) treats a blank env as unset and falls
    # through to yaml/default — labeling that "env" would tell the operator
    # a pin exists where none does.
    env_set = env_raw is not None and (flag.kind != "select" or env_raw.strip() != "")
    if env_set:
        return effective, "env"
    try:
        raw = yaml.safe_load(overlay_path.read_text()) or {}
        has_key = key in ((raw.get("chat") or {}) if isinstance(raw, dict) else {})
    except Exception:
        has_key = False
    return effective, ("config" if has_key else "default")


@router.get("/server-config")
async def get_server_config(
    user: dict = Depends(require_admin),
):
    """Return the current instance.yaml with secrets redacted.

    Used by the /admin/server-config UI to prefill its form. The redacted
    payload mirrors the actual file shape, so the UI doesn't need to know
    the schema — it iterates over the editable sections and renders the
    fields it finds. Empty sections still show in the response so the form
    knows to render their headers.
    """
    config = _load_current_instance_yaml()
    redacted = _public_view(config)
    # Surface every editable section so the UI renders them even when the
    # file omits them — operator can populate from scratch without manual
    # JSON edits.
    sections = {section: redacted.get(section, {}) for section in _EDITABLE_SECTIONS}
    # Always surface the optional BQ knobs so the operator sees them in the
    # UI's JSON editor instead of having to know they exist (Phase J).
    _ensure_bq_optional_fields(sections)
    log_safe(
        user_id=user.get("id"),
        action="server_config.read",
        resource="instance.yaml",
        params={"view": "redacted"},
    )
    return {
        "sections": sections,
        "editable_sections": list(_EDITABLE_SECTIONS),
        "danger_sections": list(_DANGER_SECTIONS),
        "secret_key_patterns": list(_SECRET_KEY_PATTERNS),
        # Known-but-optional fields per section so the UI can render
        # placeholders for fields the operator hasn't set yet (Phase J).
        # Subagents 2-4 populate the bodies; the renderer ships now so the
        # mechanism is wired end-to-end and adding entries is purely a
        # data-edit in `_KNOWN_FIELDS` above.
        "known_fields": _known_fields_resolved(),
        # Read-only feature-flag inventory (#1022 canonicalization) — every
        # flag registered in app.instance_config.FEATURE_FLAGS, its effective
        # value, and where it resolved from. Toggling still happens through
        # the per-section editors above (or an env var); this is display-only.
        "feature_flags": _feature_flags_inventory(),
    }


@router.get("/server-config/overlay")
async def get_server_config_overlay(
    user: dict = Depends(require_admin),
):
    """Return the raw, editable-section-only instance.yaml OVERLAY.

    The export projection behind ``agnes admin config export`` /
    ``agnes admin config apply`` — a small, reviewable slice of the config
    an operator can commit to a PR to onboard a new client from a known-good
    baseline (Track D3). Distinct from ``GET /server-config`` above:

    - This reads the overlay file directly — no merge with the static
      config, no ``${VAR}`` resolution — so an unresolved env reference
      round-trips as the reference, never the cleartext value it resolves
      to at runtime.
    - Only sections that actually exist in the overlay are returned (an
      admin who never touched a section shouldn't see it materialize in
      their exported YAML); every returned section is still one of
      ``_EDITABLE_SECTIONS``, so the output is always valid ``apply`` input.
    - Secret-shaped LITERAL values are omitted, not masked — an env-var
      NAME (``token_env``, itself validated to actually look like a name
      rather than a mis-suffixed pasted literal) or a ``${VAR}`` reference
      passes through unchanged, but a real cleartext credential never
      leaves the server (see ``_export_scrub``). This applies two ways:
      (a) any section in ``_FREE_FORM_EXPORT_SECTIONS`` (``connectors``
      today — an admin-typed dict whose key names no registry enumerates,
      e.g. ``connectors."connector-slack".SLACK_WEBHOOK_URL``) has EVERY
      literal leaf omitted, not just secret-*named* ones, because a
      key-name blocklist cannot police a key it has never seen; (b) a
      value that is unambiguously credential-shaped (a PEM block, a JWT, a
      URL with userinfo or a long opaque path segment, a long opaque
      alphanumeric run) is omitted everywhere else too, regardless of its
      key's name. Both apply equally to a LIST item, which has no key to
      name-match at all — a scalar array entry gets the same free-form and
      value-shape checks as a dict value, indexed in ``omitted_keys`` as
      ``a.b[2]``. There is no DB config table — the overlay IS the
      writable state POST /server-config maintains.
    - ``omitted_keys`` lists the dotted path of every leaf this endpoint
      dropped, so the operator knows what to set via env/``${VAR}`` on the
      target instead of silently losing it — `agnes admin config export`
      surfaces this as a YAML comment header and a stderr note.

    Deliberately REST+CLI only, never MCP-exposed — see CONTRIBUTING.md's
    "operator security-posture diagnostics" standing exemption: a one-call
    dump of the instance's entire editable config surface (which upstream
    it points at, what auth is configured) is reconnaissance in a
    prompt-injected chat session, not an agent affordance.
    """
    raw = _load_raw_overlay()
    omitted: List[str] = []
    sections = {
        section: _export_scrub(
            raw[section],
            free_form=section in _FREE_FORM_EXPORT_SECTIONS,
            path=section,
            omitted=omitted,
        )
        for section in _EDITABLE_SECTIONS
        if isinstance(raw.get(section), dict)
    }
    log_safe(
        user_id=user.get("id"),
        action="server_config.read",
        resource="instance.yaml",
        params={"view": "overlay"},
    )
    return {
        "sections": sections,
        "editable_sections": list(_EDITABLE_SECTIONS),
        "omitted_keys": sorted(omitted),
    }


@router.post("/server-config")
async def update_server_config(
    request: ServerConfigUpdateRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Patch instance.yaml from the /admin/server-config editor.

    Accepts a partial patch keyed by section. Validates sections, refuses
    danger-zone edits without explicit confirmation, deep-merges into the
    current overlay, writes the file, and emits one audit entry per save
    with a sanitized diff. Returns ``restart_required`` computed from the
    sections actually touched (``true`` only when at least one has effect
    ``restart``/``deploy`` — see ``_SECTION_BASELINE_EFFECT``/
    ``_effect_for_section`` above) plus ``sections_effect``, a
    ``{section: "live"|"restart"|"deploy"}`` map so the UI can say which
    section forced it.
    """
    import yaml

    if not request.sections:
        raise HTTPException(status_code=422, detail="sections cannot be empty")

    # Reject unknown sections loudly. Without this, a typo like "thmee"
    # would silently land in the YAML root and the operator wouldn't see
    # their colour change apply.
    unknown = sorted(set(request.sections.keys()) - set(_EDITABLE_SECTIONS))
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"unknown section(s): {', '.join(unknown)}. Editable: {', '.join(_EDITABLE_SECTIONS)}",
        )

    # Danger-zone gate. The UI shows a confirmation dialog before posting
    # with confirm_danger=true; an API caller (CLI/script) has to pass it
    # explicitly so they can't fat-finger a hostname change.
    danger_touched = sorted(set(request.sections.keys()) & set(_DANGER_SECTIONS))
    if danger_touched and not request.confirm_danger:
        raise HTTPException(
            status_code=400,
            detail=f"section(s) {', '.join(danger_touched)} require confirm_danger=true",
        )

    # SSRF protection — same gate the /configure wizard applies to
    # keboola_url, but here it covers any URL-bearing field reachable via
    # the per-section patch (e.g. data_source.keboola.stack_url).
    _validate_urls_in_patch(request.sections)

    # Field-level constraints for sections whose values have documented ranges.
    _validate_materialize_section(request.sections)

    # Defense-in-depth: scrub redaction sentinels (`***` / `<empty>`) out of
    # secret-keyed leaves in the patch before they reach the deep-merge.
    # The client form does the same scrub, but an API caller round-tripping
    # the GET payload could otherwise overwrite real overlay secrets with
    # the placeholder shown in the form.
    scrubbed_sections: Dict[str, Dict[str, Any]] = {
        section: _strip_redacted_sentinels(patch, section) for section, patch in request.sections.items()
    }

    # Runs on the SCRUBBED sections: the auth.providers availability check reads
    # secret leaves (auth.keboola.client_secret), and a masking sentinel (`***`
    # / `<empty>`) round-tripped from the GET payload is truthy — on the raw
    # patch it would report keboola as "available" and let a lockout config
    # through. After the scrub the sentinel key is gone, so the merge with the
    # current overlay sees the real stored value (Devin review on #1288).
    _validate_auth_providers_in_patch(scrubbed_sections)

    # Serialize read-modify-write across concurrent admin saves. Without the
    # lock, two saves would each read the same overlay snapshot, merge their
    # disjoint patches, and the second os.replace would silently drop the
    # first patch. The lock spans the cache-invalidate → load → merge →
    # atomic-write sequence; the audit log sits outside since it operates on
    # local snapshots.
    from app.instance_config import reset_cache
    from app.secrets import _state_dir

    config_path = _state_dir() / "instance.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    with _overlay_write_lock:
        # Drop the in-process cache so we read the latest on-disk state,
        # including any update that landed from a concurrent caller before
        # we acquired the lock.
        reset_cache()
        before = _load_current_instance_yaml()

        # Blast-radius gate, INSIDE the lock and before any write: `before` is
        # the snapshot the merge below is computed from, so the count the
        # operator confirmed against is the one that actually applies.
        _guard_connection_repoint(before, scrubbed_sections, request.confirm_connection_change)

        # Deep merge — section-by-section so we never accidentally delete a
        # sibling section the patch didn't touch. Use the redaction-scrubbed
        # patch so a round-tripped GET payload can't overwrite real secrets
        # with the `***` placeholder.
        after = dict(before)
        for section, patch in scrubbed_sections.items():
            if not isinstance(patch, dict):
                raise HTTPException(
                    status_code=422,
                    detail=f"section '{section}' must be an object, got {type(patch).__name__}",
                )
            if isinstance(after.get(section), dict):
                after[section] = _deep_merge(after[section], patch)
            else:
                after[section] = patch

        # Write only the sections the user actually patched in this request.
        # Two reasons:
        #   1. Persisting the full merged config (or every editable section)
        #      would snapshot non-editable static sections into the overlay,
        #      shadowing later operator updates to those sections in the
        #      static file (`_load_current_instance_yaml` merges static + overlay,
        #      overlay wins per leaf).
        #   2. The merged config has `${ENV_VAR}` placeholders RESOLVED to the
        #      runtime values by config.loader. Writing every editable section
        #      back would persist real cleartext secrets where the static file
        #      had only env-var references — turning `smtp_password:
        #      ${SMTP_PASSWORD}` into `smtp_password: hunter2` in the overlay.
        # By writing only the sections in `request.sections` we keep both the
        # static-evolution and the env-var-placeholder properties intact.
        overlay_payload: Dict[str, Any] = {}
        if config_path.exists():
            try:
                overlay_payload = yaml.safe_load(config_path.read_text()) or {}
            except Exception as e:
                # A corrupt overlay used to be silently replaced — that masked
                # disk corruption / partial writes / hand-edits and dropped
                # every previously-saved section on the next save. Refuse and
                # surface so the operator can investigate.
                logger.exception("server-config: refusing to overwrite corrupt overlay at %s", config_path)
                raise HTTPException(
                    status_code=500,
                    detail=f"refusing to overwrite corrupt overlay at {config_path} ({e}); "
                    "back up and remove the file, or fix it by hand",
                ) from e
        for section, patch in scrubbed_sections.items():
            if section not in _EDITABLE_SECTIONS:
                continue
            # Deep-merge the patch into the existing overlay slot (or static-
            # backed `before` if overlay had nothing for this section). This
            # preserves any unrelated keys the operator didn't touch in this
            # request — e.g. patching `email.smtp_host` doesn't blow away the
            # `email.smtp_password: ${SMTP_PASSWORD}` reference.
            existing = overlay_payload.get(section)
            if not isinstance(existing, dict):
                existing = {}
            overlay_payload[section] = _deep_merge(existing, patch)

        # Atomic via tmp + os.replace so two concurrent admin saves can't
        # interleave bytes and produce corrupt YAML (especially harmful since
        # auth.* is editable here — half-written file → operator lockout).
        tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
        tmp_path.write_text(yaml.dump(overlay_payload, default_flow_style=False, sort_keys=False))
        # 0600 BEFORE the rename, not after: os.replace is atomic, so the
        # file is never observable at the umask default this way. instance.yaml
        # holds the Postgres URL (password inline) and any operator-set
        # connector credentials, yet it was landing world-readable on the data
        # volume — which several non-root containers mount — while the
        # equivalent /opt/agnes/.env is 0600. The app and the state applier
        # both run as uid 999, the file's owner, so nothing legitimate loses
        # access. Mirrors src/db_state_machine.py::write_backend_state and
        # scripts/ops/agnes-state-applier.sh, which already do this.
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, config_path)
        logger.info("server-config: wrote %d section(s) to %s", len(request.sections), config_path)

        # Invalidate cached instance config so subsequent reads pick up the
        # change. Hot-reload of running modules (auth providers, SMTP client)
        # is out of scope — the restart banner tells the operator to bounce.
        reset_cache()

    # Audit entry — diff is computed on RAW values then `_diff_dicts`
    # redacts each row whose leaf key matches `_is_secret_key`. Pre-
    # masking the inputs would collapse a secret rotation into "no
    # diff" because both sides redact to ``***``, hiding the most
    # security-relevant changes from the audit log. We log even if no
    # fields changed so the operator's intent (touched the page, hit
    # save) is auditable.
    diff = _diff_dicts(before, after)
    audit_repo().log(
        user_id=user.get("id"),
        action="instance_config.update",
        resource="instance.yaml",
        params={
            "sections": sorted(request.sections.keys()),
            "danger_sections": danger_touched,
            "diff": diff,
            "diff_count": len(diff),
        },
    )

    # Honest per-section effect (see `_effect_for_section` above) instead of
    # the hardcoded `restart_required=True` this endpoint used to return
    # unconditionally.
    sections_effect = {section: _effect_for_section(section, patch) for section, patch in request.sections.items()}
    restart_required = any(effect != "live" for effect in sections_effect.values())

    return {
        "status": "ok",
        "restart_required": restart_required,
        "sections_effect": sections_effect,
        "sections_updated": sorted(request.sections.keys()),
        "diff_count": len(diff),
    }


# --- End server-config editor -----------------------------------------------


# Source types accepted by /api/admin/register-table. Anything else is
# rejected with 422 — keeps a typo'd source_type from silently landing in
# table_registry (where it would later confuse the orchestrator scan).
_VALID_SOURCE_TYPES: tuple[str, ...] = ("keboola", "bigquery", "jira", "local", "databricks", "snowflake")

# Explicit allowlist of audit-payload keys whose values are credentials and
# must be masked. Substring-scan + ad-hoc whitelist (the previous shape) is
# fragile in two ways:
#   1. False positive: legit fields like `primary_key` get masked because
#      they contain "key" — we then need a whitelist exception, which has
#      to be kept in sync as new fields are added.
#   2. False negative: a future field like `primary_key_hash` *would* be
#      masked (defensible) but `not_actually_a_token` ALSO matches "token"
#      and gets masked unnecessarily; conversely, a brand-new credential
#      field that doesn't contain one of the patterns (`auth_material`,
#      `bearer`) silently leaks.
# Allowlist puts the burden on the developer adding a new secret-bearing
# field: they must add the literal key name here, which forces a code-
# review touch on the audit path. Audit the current Pydantic models
# (RegisterTableRequest / UpdateTableRequest / ConfigureRequest /
# ServerConfigUpdateRequest) when extending — the registry payloads don't
# currently carry credentials, but ConfigureRequest does (`keboola_token`)
# and could be routed through this sanitizer in the future.
_SECRET_FIELDS: frozenset = frozenset(
    {
        # ConfigureRequest — POST /api/admin/configure carries Keboola creds.
        "keboola_token",
        # Generic names that have appeared in earlier iterations of admin
        # request bodies and could resurface — keep them masked defensively.
        "api_token",
        "auth_token",
        "bot_token",
        "client_secret",
        "google_client_secret",
        "google_oauth_client_secret",
        "password",
        "smtp_password",
        "webapp_secret_key",
        "bot_secret",
        # Marketplace PATs (private repos) — see src/marketplace.py.
        "marketplace_token",
        "marketplace_pat",
    }
)


def _sanitize_for_audit(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Mask credential-bearing fields in a request payload before audit_log.

    Uses an explicit `_SECRET_FIELDS` allowlist (case-insensitive) instead
    of substring matching. The trade-off is that adding a new secret field
    requires updating the set — but that's the *point*: the test suite
    asserts `not_actually_a_token` does NOT get masked, so a substring-
    based regression would surface immediately, and a missing entry for a
    real new credential gets caught at code review of the audit path.
    """
    out: Dict[str, Any] = {}
    for k, v in payload.items():
        if k.lower() in _SECRET_FIELDS:
            out[k] = "***" if v not in (None, "") else "<empty>"
        else:
            out[k] = v
    return out


# Both the BigQuery and Keboola materialize paths funnel `source_query`
# through DuckDB (BQ via the bigquery extension's COPY translation, Keboola
# via an ATTACH'd extension and a direct COPY). DuckDB uses double quotes
# for quoted identifiers — backticks are a BigQuery-native syntactic form
# DuckDB's parser does not honor, so a backtick-quoted source_query either
# parse-errors at COPY time or silently scans nothing. Surfaced from the
# field validator on RegisterTableRequest AND the merged-record path in
# `update_table` so neither route can persist a backtick query.
_BACKTICK_REJECTION_MESSAGE = (
    "source_query uses BigQuery-native backtick identifiers (e.g. "
    "`project.dataset.table`), but the materialize path runs the SQL "
    "through DuckDB's BigQuery extension which uses DuckDB-flavor "
    'identifiers. Rewrite to DuckDB syntax: bq."dataset"."table" '
    "(with the attached catalog alias `bq` plus double-quoted dataset/"
    "table). The instance is configured with the data project, so you "
    "don't need to repeat it in the FROM clause."
)


class RegisterTableRequest(BaseModel):
    name: str
    folder: Optional[str] = None
    sync_strategy: str = Field(
        default="full_refresh",
        description=(
            "Per-table extraction strategy. v26+: drives the Keboola "
            "extractor's dispatcher in connectors/keboola/extractor.py. "
            "Allowed values: 'full_refresh' (default; full table dump on "
            "each sync), 'incremental' (Storage API changedSince + "
            "primary-key dedup merge), 'partitioned' (per-partition "
            "parquet files keyed by partition_by column, per-partition "
            "merge for daily updates, chunked initial load). "
            "Pre-v26 this field was inert; existing rows default to "
            "'full_refresh' so behavior is unchanged unless an admin "
            "opts a table in to incremental/partitioned."
        ),
    )
    # Composite primary keys are real (session-grain MSA tables key on
    # `(session_id, event_date)`, browse rows on more). The frontend sends +
    # reads this as a list; backend stores it JSON-serialized in VARCHAR.
    # A bare string is accepted for backward compat — see _normalize_primary_key.
    primary_key: Optional[List[str]] = None
    description: Optional[str] = None
    source_type: Optional[str] = None
    bucket: Optional[str] = None
    source_table: Optional[str] = None
    # Backs query_mode='materialized'. Stored verbatim in
    # table_registry.source_query (schema v20); the trigger pass runs it
    # through the DuckDB BQ extension via BqAccess and writes the result
    # to /data/extracts/bigquery/data/<id>.parquet.
    source_query: Optional[str] = None
    query_mode: str = "local"
    defer_rebuild: bool = Field(
        default=False,
        description=(
            "BigQuery only. When true, skip the synchronous post-insert "
            "rebuild of the extract + master views; the registry row is "
            "created but the table is not yet queryable. Intended for bulk "
            "onboarding: register many tables with defer_rebuild=true (each "
            "skipping the O(registry) per-insert rebuild), then call "
            "POST /api/admin/registry/rebuild ONCE to materialize them all in "
            "a single rebuild. No effect for non-BigQuery or materialized rows."
        ),
    )
    sync_schedule: Optional[str] = None
    profile_after_sync: bool = Field(
        default=True,
        deprecated=True,
        description=(
            "DEPRECATED: not consumed by the runtime (Agent 1 finding "
            "2026-05-01). Profiler runs unconditionally on every synced "
            "table; this flag has no effect. Field stays for back-compat."
        ),
    )
    # v26 — Keboola sync-strategy support fields. All optional; meaningful
    # only when paired with the matching sync_strategy. Per-strategy
    # required-field rules + conflict policy enforced in the model_validator
    # below.
    incremental_window_days: Optional[int] = None
    max_history_days: Optional[int] = None
    incremental_column: Optional[str] = None
    where_filters: Optional[List[Dict[str, Any]]] = None
    partition_by: Optional[str] = None
    partition_granularity: Optional[str] = None
    initial_load_chunk_days: Optional[int] = None
    # v51 — fully-qualified BigQuery path. When set on a BigQuery row,
    # the extractor uses ``project.dataset.table`` from this field instead
    # of constructing the path from ``bucket`` + ``source_table`` against
    # the globally-attached project. Decouples UX/RBAC ``bucket`` label
    # from physical BQ dataset (issue #343). Format ``project.dataset.table``;
    # validated by ``connectors.bigquery.extractor.parse_bq_fqn``.
    bq_fqn: Optional[str] = Field(
        default=None,
        description=(
            "Fully-qualified BigQuery path (``project.dataset.table``). "
            "Only applies to source_type='bigquery'. When set, overrides "
            "the legacy bucket+source_table path construction. Use this "
            "to register a table whose BQ dataset name differs from the "
            "Agnes ``bucket`` label (issue #343)."
        ),
    )
    # v74 (#607) — distribution flag decoupled from query_mode. When true the
    # table is kept server-side & queryable via `agnes query --remote`, but
    # `agnes pull` does NOT download its parquet (the manifest still lists it
    # for catalog discovery + RBAC). Only meaningful for query_mode IN
    # ('local', 'materialized'); the model_validator below rejects it paired
    # with query_mode='remote' (no server-stored parquet to suppress).
    server_only: bool = Field(
        default=False,
        description=(
            "Keep the table server-side & queryable via `agnes query "
            "--remote`, but exclude its parquet from `agnes pull` download. "
            "Only valid for query_mode='local'/'materialized'; rejected with "
            "query_mode='remote'. Default false leaves distribution unchanged."
        ),
    )
    # v79 — nullable FK to source_connections.id. NULL = use the instance-default
    # connection for the row's source_type (spec 2026-06-12). When provided,
    # the register-table handler validates the id exists before persisting.
    connection_id: Optional[str] = Field(
        default=None,
        description=(
            "Pin this table to a named source connection (source_connections.id). "
            "NULL uses the default connection for the row's source_type. "
            "The referenced connection must exist; an unknown id returns 400."
        ),
    )

    @model_validator(mode="after")
    def _check_server_only_query_mode(self):
        """``server_only`` is a *distribution* suppressor — it only makes
        sense when there IS a server-stored parquet to suppress. A
        ``query_mode='remote'`` row has none (every query goes live to the
        upstream source), so ``server_only=true`` there is incoherent.
        Reject it explicitly rather than silently ignore so the admin sees
        the conflict at register/update time (issue #607)."""
        if self.server_only and self.query_mode == "remote":
            raise ValueError(
                "server_only=true is only valid for query_mode='local' or "
                "'materialized' (a 'remote' table has no server-stored parquet "
                "to suppress from agnes pull)"
            )
        return self

    @model_validator(mode="after")
    def _check_mode_query_coherence(self):
        """Enforce query_mode ↔ source_query invariants up front so an admin
        can't persist a remote/local row carrying an orphan source_query.

        For BigQuery materialized rows, an empty source_query is allowed here
        because _validate_bigquery_register_payload generates it from
        bucket+source_table after this validator runs. For all other source
        types (e.g. Keboola), source_query is still required for materialized.
        """
        sq = (self.source_query or "").strip() or None
        if self.query_mode != "materialized" and sq:
            raise ValueError("source_query is only valid when query_mode='materialized'")
        # Databricks supports two modes. 'materialized': the scheduler runs the
        # SQL on the warehouse and distributes the parquet. 'remote': nothing
        # syncs — the analyst's statement ships to the warehouse per query
        # (`agnes query --remote`), which is the only way to reach a Unity
        # Catalog metric view's MEASURE() or a table too large to materialize.
        # 'local' stays rejected: there is no extractor subprocess for it, so a
        # row would land in the registry with a mode nothing will ever sync.
        if self.source_type in ("databricks", "snowflake") and self.query_mode not in ("materialized", "remote"):
            raise ValueError(
                f"source_type='{self.source_type}' supports query_mode='materialized' (scheduled sync to a "
                "parquet) or 'remote' (per-query execution on the SQL warehouse); "
                f"got '{self.query_mode}'"
            )
        # BigQuery materialized auto-generates a full-table-dump SQL from
        # `bucket`+`source_table` when source_query is omitted (see
        # `register_table` BQ branch). Keboola materialized: a NULL
        # source_query means "full-table export via Storage API
        # export-async" — no SQL needed (the API takes a structured
        # filter, see `connectors/keboola/storage_api.py:ExportFilter`).
        # Other source_types (e.g. jira) don't support materialized mode
        # and require an explicit source_query if the operator opts in.
        if (
            self.query_mode == "materialized"
            and not sq
            and self.source_type not in ("bigquery", "keboola", "databricks", "snowflake")
        ):
            raise ValueError(
                f"query_mode='materialized' for source_type='{self.source_type}' requires a non-empty source_query"
            )
        # Backtick guard stays for non-materialized rows (DuckDB-flavor SQL
        # contract); materialized SQL is BigQuery-native and MUST allow
        # backticks for dashed identifiers (e.g. `prj-org.dataset.table`).
        if self.query_mode != "materialized" and sq and "`" in sq:
            raise ValueError(_BACKTICK_REJECTION_MESSAGE)
        # Keboola materialized source_query must be a JSON filter spec, not SQL.
        # The extractor uses the Storage API with structured filters (columns,
        # whereFilters, changedSince) — DuckDB SQL belongs on BigQuery rows.
        if self.query_mode == "materialized" and self.source_type == "keboola" and sq:
            if sq.upper().startswith(("SELECT", "WITH")):
                raise ValueError(
                    "Keboola materialized source_query must be a JSON filter spec "
                    "(columns/whereFilters/changedSince), not SQL. "
                    "Use null for full-table export, or set query_mode='local' "
                    "for DuckDB-based Keboola pulls."
                )
            try:
                json.loads(sq)
            except json.JSONDecodeError as e:
                raise ValueError(f"Keboola materialized source_query must be valid JSON: {e}") from e
        # Normalise: stash the trimmed-or-None form so the persisted column
        # never carries surrounding whitespace or empty-string sentinels.
        self.source_query = sq
        return self

    @field_validator("primary_key", mode="before")
    @classmethod
    def _coerce_primary_key(cls, v):
        return _normalize_primary_key(v)

    @field_validator("description", mode="before")
    @classmethod
    def _normalize_description(cls, v):
        # Defensive normalization for descriptions arriving via shell-quoting
        # tooling that injects literal backslash escapes (e.g. `Don\'t`, `\n`).
        return _unescape_shell_quoting(v)

    @field_validator("source_type", mode="before")
    @classmethod
    def _validate_source_type(cls, v):
        # None is tolerated for backward compat with old CLI scripts that
        # didn't set a source_type; the route resolves it later. Anything
        # else must be in the canonical list.
        if v in (None, ""):
            return v
        if v not in _VALID_SOURCE_TYPES:
            raise ValueError(f"source_type must be one of {sorted(_VALID_SOURCE_TYPES)}, got {v!r}")
        return v

    @field_validator("sync_schedule", mode="before")
    @classmethod
    def _validate_sync_schedule(cls, v):
        # None / "" → no schedule, accepted.
        # Any non-empty string (including pure whitespace) must parse as a
        # valid schedule — otherwise it would be persisted and silently
        # ignored by the runtime evaluator.
        if v in (None, ""):
            return v
        if not is_valid_schedule(v):
            raise ValueError(
                f"sync_schedule must be 'every Nm' / 'every Nh' / "
                f"'daily HH:MM[,HH:MM,...]' / 'cron <min hour dom month dow>' "
                f"(e.g. 'cron 0 5 7 * *'), got {v!r}"
            )
        return v

    @field_validator("sync_strategy", mode="before")
    @classmethod
    def _validate_sync_strategy(cls, v):
        """v26: enforce the strategy enum. NULL/empty → 'full_refresh' default.

        Pre-v26 the column accepted any string (catalog/profiler metadata
        only). Now the extractor dispatches off this value, so unknown
        strings would silently fall through to the default branch and
        confuse operators.
        """
        if v in (None, ""):
            return "full_refresh"
        allowed = {"full_refresh", "incremental", "partitioned"}
        if v not in allowed:
            raise ValueError(f"sync_strategy must be one of {sorted(allowed)}, got {v!r}")
        return v

    @field_validator("partition_granularity", mode="before")
    @classmethod
    def _validate_partition_granularity(cls, v):
        if v in (None, ""):
            return v
        allowed = {"day", "month", "year"}
        if v not in allowed:
            raise ValueError(f"partition_granularity must be one of {sorted(allowed)}, got {v!r}")
        return v

    @field_validator("where_filters", mode="before")
    @classmethod
    def _validate_where_filters(cls, v):
        """Validate filter shape via parse_filters from the keboola module.

        Accepts None / empty list, a JSON string, or a pre-parsed list.
        Returns the canonical list-of-dicts form for storage. Raises
        ValueError(InvalidFilterError message) on malformed shape so
        FastAPI returns 422 with a useful body. Placeholders are NOT
        resolved here — they're resolved at sync time so a misspelled
        token is caught when the next sync runs (admin can register a
        rolling-window filter today and the sync next month uses the
        same filter shape with a fresh date)."""
        if v in (None, "", []):
            return None
        from connectors.keboola.where_filters import InvalidFilterError, parse_filters

        try:
            return parse_filters(v)
        except InvalidFilterError as e:
            raise ValueError(str(e))

    @model_validator(mode="after")
    def _check_strategy_invariants(self):
        """v27 conflict policy + per-strategy required-field rules.

        Reject combinations that are silently broken at the extractor
        layer rather than letting the row land in the registry and
        confuse operators when the next sync misbehaves.

        - partitioned ⇒ partition_by required, query_mode='local' only.
          partition_granularity defaults to 'month' if omitted.
        - incremental + where_filters → 400. changedSince already does
          temporal filtering; layering server-side row filters on top is
          not supported by the extractor (legacy repo silently drops
          filters in this combination — match the rejection here).
        - partitioned + where_filters → 400. extract_partitioned does
          not thread where_filters through to its chunked downloads;
          accepting the pair would persist a filter that gets silently
          ignored at sync time (Devin Review concern). Reject explicitly
          until threading lands.
        - query_mode='remote' + where_filters → 400. _extract_via_extension
          (the remote/extension path) doesn't take a filters argument;
          accepting would silently drop them.
        """
        if self.sync_strategy == "partitioned":
            if not self.partition_by:
                raise ValueError("sync_strategy='partitioned' requires partition_by to be set")
            if self.query_mode == "remote":
                raise ValueError(
                    "sync_strategy='partitioned' is incompatible with query_mode='remote' "
                    "— partitioned writes per-partition parquet files locally"
                )
            if self.where_filters:
                raise ValueError(
                    "sync_strategy='partitioned' is incompatible with where_filters "
                    "in v27 — extract_partitioned does not thread where_filters "
                    "through its chunked downloads; the filter would be silently "
                    "ignored. Use 'full_refresh' for filter+full-overwrite, or "
                    "wait for partitioned + where_filters wiring in a future PR."
                )
            if not self.partition_granularity:
                self.partition_granularity = "month"

        if self.sync_strategy == "incremental" and self.where_filters:
            raise ValueError(
                "sync_strategy='incremental' is incompatible with where_filters "
                "— changedSince already filters temporally; layering whereFilters "
                "on top is silently dropped by the extractor (use 'full_refresh' "
                "for filter+full-overwrite)"
            )

        # query_mode='remote' + where_filters: the DuckDB Keboola extension
        # path does not consume whereFilters. Accepting would silently drop
        # them at sync time. Caller must use query_mode='local' (Direct
        # extract) to apply filters.
        if self.query_mode == "remote" and self.where_filters:
            raise ValueError(
                "query_mode='remote' is incompatible with where_filters "
                "— the DuckDB Keboola extension does not expose whereFilters. "
                "Use query_mode='local' (Direct extract) to apply server-side "
                "row filters."
            )

        return self


def _generate_materialized_source_query(
    bucket: str,
    source_table: str,
    project_id: str,
) -> str:
    """Build the canonical full-table-dump source_query for a materialized
    BQ row when admin only supplies dataset + table. The result is
    BigQuery-native SQL — wrapped at materialize time into
    bigquery_query(...) by connectors.bigquery.extractor.materialize_query."""
    if not _is_safe_quoted_identifier(bucket):
        raise HTTPException(
            status_code=400,
            detail=f"bigquery: dataset {bucket!r} is unsafe",
        )
    if not _is_safe_quoted_identifier(source_table):
        raise HTTPException(
            status_code=400,
            detail=f"bigquery: source_table {source_table!r} is unsafe",
        )
    if not _is_safe_project_id(project_id):
        raise HTTPException(
            status_code=400,
            detail=f"bigquery: data_source.bigquery.project {project_id!r} is malformed",
        )
    return f"SELECT * FROM `{project_id}.{bucket}.{source_table}`"


def _rebuild_databricks_remote_extract() -> str | None:
    """Refresh the Databricks remote extract; return a note for the response.

    Called after a ``query_mode='remote'`` Databricks row is registered or
    updated. Returns ``None`` when there is nothing to say (the common case:
    the instance has not opted into the Unity Catalog ATTACH, so no extract is
    written and the row is served by the warehouse path alone).

    Never raises: the registry row is already committed and correct, and the
    only thing a failure here costs is the *optional* local view. Surfacing it
    as a 500 would tell the admin their registration failed when it did not.
    """
    try:
        from connectors.databricks.extract_init import rebuild_from_registry

        result = rebuild_from_registry()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("databricks remote extract rebuild failed: %s", e)
        return "Registered. The Unity Catalog ATTACH extract could not be rebuilt; see server logs."
    if result.get("skipped"):
        return None
    errors = result.get("errors") or []
    if errors:
        return f"Registered. Unity Catalog extract rebuilt with {len(errors)} error(s); see server logs."
    return None


def _validate_databricks_register_payload(req: "RegisterTableRequest") -> None:
    """Enforce Databricks-specific shape on a register/update request.

    The Pydantic model validator already pinned ``query_mode`` to
    ``'materialized'`` or ``'remote'``; this hook server-generates the
    full-table-dump ``source_query`` from ``bucket``+``source_table`` (+ the
    configured default catalog) when a materialized row omitted custom SQL —
    mirroring the BigQuery register path. Custom SQL passes through untouched:
    that is where semantic-layer queries live (``SELECT dim, MEASURE(m) FROM
    <metric_view> GROUP BY dim``).

    A ``query_mode='remote'`` row carries no ``source_query`` at all — nothing
    is ever scheduled for it, and the statement that runs is the analyst's. It
    still needs ``bucket``+``source_table``, because that pair is what the
    remote path rewrites a bare registered name into.
    """
    if req.query_mode == "remote":
        if not (req.bucket and req.source_table):
            raise HTTPException(
                status_code=422,
                detail=(
                    "databricks remote requires bucket+source_table — they resolve the "
                    "`catalog`.`schema`.`table` an analyst's bare reference to this row is "
                    "rewritten into (bucket is the schema, or 'catalog.schema' to override "
                    "the default catalog)"
                ),
            )
        return
    if req.source_query and req.source_query.strip():
        return
    if not (req.bucket and req.source_table):
        raise HTTPException(
            status_code=422,
            detail=(
                "databricks materialized requires either source_query (custom "
                "Databricks SQL — e.g. SELECT dim, MEASURE(m) FROM a metric view) "
                "or bucket+source_table (server-generates the full-table-dump SQL; "
                "bucket is the schema, or 'catalog.schema' to override the default catalog)"
            ),
        )
    from app.instance_config import get_value
    from connectors.databricks.extractor import full_table_sql, split_bucket

    default_catalog = get_value("data_source", "databricks", "catalog", default="") or ""
    catalog, schema = split_bucket(req.bucket.strip(), default_catalog)
    if not catalog:
        raise HTTPException(
            status_code=422,
            detail=(
                "databricks: no catalog to resolve against — set "
                "data_source.databricks.catalog in instance.yaml / "
                "/admin/server-config, or use a dotted bucket 'catalog.schema'"
            ),
        )
    try:
        req.source_query = full_table_sql(catalog, schema, req.source_table.strip())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"databricks: {e}") from e


def _normalize_bq_source_table(req: "RegisterTableRequest") -> None:
    """Collapse a pasted FQN in ``source_table`` to the bare table name.

    BigQuery table names cannot contain dots, so a dotted ``source_table``
    is always a pasted path (``dataset.table`` or ``project.dataset.table``),
    never a real table name. Stored verbatim, the extractor composes
    ``project.bucket.source_table`` and produces a doubled path that fails to
    register on every sync. Mutates the request in place when the dotted
    value is unambiguous (its dataset component equals ``bucket``); raises
    HTTPException(400) when it contradicts ``bucket`` or points at a foreign
    project (that's what ``bq_fqn`` is for).
    """
    st = req.source_table or ""
    if "." not in st:
        return
    parts = st.split(".")
    bucket = req.bucket or ""
    if len(parts) == 2 and parts[0] == bucket and parts[1]:
        bare = parts[1]
    elif len(parts) == 3 and parts[1] == bucket and parts[2]:
        from app.instance_config import get_value

        project_id = get_value("data_source", "bigquery", "project", default="") or ""
        if project_id and parts[0] != project_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"bigquery: source_table {st!r} points at project "
                    f"{parts[0]!r} but this instance is configured for "
                    f"{project_id!r} — for cross-project tables use the "
                    "bq_fqn field"
                ),
            )
        bare = parts[2]
    else:
        raise HTTPException(
            status_code=400,
            detail=(
                f"bigquery: source_table {st!r} looks like a fully-qualified "
                f"path but does not match bucket {req.bucket!r} — put the BQ "
                "dataset in 'bucket' and the bare table name in 'source_table'"
            ),
        )
    logger.info("normalized dotted BQ source_table %r -> %r", st, bare)
    req.source_table = bare


def _validate_bigquery_register_payload(req: "RegisterTableRequest") -> None:
    """Enforce BQ-specific shape on a register/precheck request.

    Two BQ paths:

    - ``query_mode='materialized'`` — admin-registered SQL writes a parquet on
      schedule. Requires ``source_query``; ``bucket`` / ``source_table`` are
      not used (the SQL inlines the references). Doesn't force any field; the
      Pydantic ``model_validator`` already gated the query/mode coherence.

    - ``query_mode='remote'`` (or default) — remote view over a single BQ
      table. Requires ``bucket`` (BQ dataset) + ``source_table``. Mutates
      the model: forces ``query_mode='remote'`` and ``profile_after_sync=False``
      (per Decision 7 in #108) so a caller can't accidentally enqueue a
      parquet profiling pass for a remote view that has no local file.

    Raises HTTPException(422) for missing required fields and
    HTTPException(400) for unsafe identifiers / bogus project_id.
    """
    _normalize_bq_source_table(req)
    if req.query_mode == "materialized":
        # Materialized BQ rows: the SQL body replaces dataset+table refs.
        # source_query may be empty if admin supplied bucket+source_table —
        # in that case the server generates a full-table-dump SQL below.
        raw_name = req.name or ""
        if raw_name.strip() != raw_name or not _is_safe_identifier(raw_name):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"bigquery: view name {raw_name!r} is unsafe — must match "
                    f"^[a-zA-Z_][a-zA-Z0-9_]{{0,63}}$ (DuckDB identifier rules) "
                    "with no leading/trailing whitespace"
                ),
            )
        from app.instance_config import get_value

        project_id = get_value("data_source", "bigquery", "project", default="") or ""
        if not project_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "bigquery: data_source.bigquery.project is not set in "
                    "instance.yaml; configure it via /admin/server-config or "
                    "/api/admin/configure first"
                ),
            )
        if not _is_safe_project_id(project_id):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"bigquery: data_source.bigquery.project {project_id!r} "
                    "is malformed — must match GCP project_id grammar "
                    "^[a-z][a-z0-9-]{4,28}[a-z0-9]$"
                ),
            )

        if not (req.source_query and req.source_query.strip()):
            # Server-generate from bucket+source_table. Trivial full-table
            # dump path; admin only sets dataset+table and the server
            # builds BQ-native SQL from instance.yaml's configured project.
            if not (req.bucket and req.source_table):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "bigquery materialized requires either source_query "
                        "(custom SQL) or bucket+source_table (server-generates "
                        "the full-table-dump SQL)"
                    ),
                )
            req.source_query = _generate_materialized_source_query(
                req.bucket,
                req.source_table,
                project_id,
            )

        # Phase C: profile_after_sync is now inert (Pydantic field marked
        # deprecated; not read by app/api/sync.py:410-438). The runtime
        # profiles every synced table unconditionally, so we no longer
        # force-set this here as a "signal."
        return

    if not req.bucket or not req.bucket.strip():
        raise HTTPException(
            status_code=422,
            detail="bigquery: 'bucket' (BQ dataset) is required",
        )
    if not req.source_table or not req.source_table.strip():
        raise HTTPException(
            status_code=422,
            detail="bigquery: 'source_table' is required",
        )
    # No wildcard / sharded BQ tables in M1 (Decision 8).
    if "*" in (req.source_table or "") or "*" in (req.bucket or ""):
        raise HTTPException(
            status_code=400,
            detail="bigquery: wildcard / sharded tables are not supported (see #108 M3+)",
        )
    # Strict identifier on the DuckDB view name. CRITICAL: validate the RAW
    # name (the value that ``register_table`` actually persists to
    # ``table_registry.name`` and which the BQ extractor reads back as the
    # DuckDB view name at next rebuild). Earlier revisions normalized first
    # (``strip().lower().replace(" ", "_")``) and then checked, which let
    # names like ``"my table"`` pass here, get stored verbatim, and then
    # blow up inside ``_init_extract`` at view-create time — defeating the
    # whole point of fast-fail-at-register. We do NOT silently rewrite the
    # operator's name; if they typed ``"my table"``, return 400 with a
    # clear message and let them retype with a corrected name.
    raw_name = req.name or ""
    if raw_name.strip() != raw_name or not _is_safe_identifier(raw_name):
        raise HTTPException(
            status_code=400,
            detail=(
                f"bigquery: view name {raw_name!r} is unsafe — must match "
                f"^[a-zA-Z_][a-zA-Z0-9_]{{0,63}}$ (DuckDB identifier rules) "
                "with no leading/trailing whitespace"
            ),
        )
    # Same fast-fail rule as ``raw_name`` above: validate the RAW value the
    # caller sent, not a stripped form. ``register_table`` persists ``bucket``
    # / ``source_table`` verbatim, and the BQ extractor splices them straight
    # into the ``ATTACH … AS bq_<bucket>`` and view DDL at next rebuild — so a
    # value with leading/trailing whitespace passes validation here, gets
    # stored as-is, and explodes inside DuckDB at view-create time. Surface
    # the offending raw value in the 400 detail and let the operator retype.
    raw_bucket = req.bucket
    if raw_bucket.strip() != raw_bucket or not _is_safe_quoted_identifier(raw_bucket):
        raise HTTPException(
            status_code=400,
            detail=(
                f"bigquery: dataset {raw_bucket!r} is unsafe (only [A-Za-z0-9_.-] "
                "allowed, no leading/trailing whitespace)"
            ),
        )
    raw_source_table = req.source_table
    if raw_source_table.strip() != raw_source_table or not _is_safe_quoted_identifier(raw_source_table):
        raise HTTPException(
            status_code=400,
            detail=(
                f"bigquery: source_table {raw_source_table!r} is unsafe (only "
                "[A-Za-z0-9_.-] allowed, no leading/trailing whitespace)"
            ),
        )
    # Pull project from instance.yaml — single-project model in M1
    # (Decision: no per-table project field). Validate the format here so
    # we surface a config issue at registration rather than at first
    # rebuild, where the operator no longer has a request to look at.
    from app.instance_config import get_value

    project_id = get_value("data_source", "bigquery", "project", default="")
    if not project_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "bigquery: data_source.bigquery.project is not set in instance.yaml; "
                "configure it via /admin/server-config or /api/admin/configure first"
            ),
        )
    if not _is_safe_project_id(project_id):
        raise HTTPException(
            status_code=400,
            detail=(
                f"bigquery: data_source.bigquery.project {project_id!r} is malformed — "
                "must match GCP project_id grammar ^[a-z][a-z0-9-]{4,28}[a-z0-9]$"
            ),
        )
    # Force the BQ-required mode (Decision 7). The orchestrator and
    # extractor both assume remote; persisting `local` here would later create
    # a profiling job against a non-existent parquet file.
    # Phase C: profile_after_sync is now inert (deprecated, not read by the
    # runtime); no longer force-set here.
    req.query_mode = "remote"
    # v74 (#607) — re-assert the server_only ↔ query_mode invariant AFTER the
    # coercion above. The Pydantic validator ran against the caller's
    # pre-coercion query_mode (often the 'local' default), so a BQ live
    # registration with server_only=true would otherwise slip past it and
    # persist the exact incoherent state it exists to reject.
    if req.server_only:
        raise HTTPException(
            status_code=422,
            detail=(
                "server_only=true is only valid for query_mode='local' or "
                "'materialized' — a live BigQuery registration is coerced to "
                "query_mode='remote', which has no server-stored parquet to "
                "suppress from agnes pull"
            ),
        )


def _assert_snowflake_custom_sql_targets_sf(sql: str) -> None:
    """Refuse custom Snowflake SQL that reaches outside the attached ``sf`` catalog.

    ``connectors.snowflake.extractor.materialize_query`` opens a scratch DuckDB,
    ATTACHes the configured Snowflake database as ``sf``, and nothing else — so a
    statement naming any other catalog (or naming a table with no catalog at all)
    resolves against an empty database and fails at COPY time, on a scheduler
    tick, hours after registration. That is the "registered but never
    materializes" state the BigQuery and Databricks validators exist to prevent;
    catch it while the operator is still looking at the register form.

    The statement is parsed as DuckDB SQL — which is what actually runs; only the
    table scan is pushed down to Snowflake — so a CTE alias is a local name and
    is not a catalog reference. Table *functions* (``range(10)``, ``VALUES``) are
    not table references either and are left alone.
    """
    import sqlglot
    from sqlglot import exp

    from connectors.snowflake.attach import SF_ALIAS

    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=(
                f"snowflake: source_query could not be parsed as DuckDB SQL ({e}). "
                "The statement runs through DuckDB's Snowflake extension, so it must "
                "be valid DuckDB SQL, not Snowflake-flavour SQL."
            ),
        ) from e

    if not statements:
        raise HTTPException(status_code=422, detail="snowflake: source_query is empty")

    for statement in statements:
        cte_names = {c.alias_or_name.lower() for c in statement.find_all(exp.CTE) if c.alias_or_name}
        for table in statement.find_all(exp.Table):
            if not isinstance(table.this, exp.Identifier):
                # A table function / subquery source has no static catalog to check.
                continue
            catalog = (table.catalog or "").strip('"')
            if not catalog and table.name.lower() in cte_names:
                continue
            if catalog.lower() != SF_ALIAS:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"snowflake: source_query references {table.sql(dialect='duckdb')!r}, "
                        f"which is not in the {SF_ALIAS!r} catalog. The materialize session "
                        f"attaches only the configured Snowflake database as {SF_ALIAS!r}; "
                        f'write every table as {SF_ALIAS}."<schema>"."<table>".'
                    ),
                )


def _validate_snowflake_register_payload(req: "RegisterTableRequest") -> None:
    """Enforce Snowflake-specific shape on a register/update request.

    Snowflake supports ``query_mode='materialized'`` (scheduler runs the SQL
    through the DuckDB Snowflake extension and writes a parquet) and
    ``query_mode='remote'`` (a local view over the attached ``sf`` catalog).
    The server generates ``SELECT * FROM sf."<schema>"."<table>"`` when the
    admin supplies ``bucket`` (schema) + ``source_table`` and omits custom SQL.
    """
    from connectors.snowflake.extractor import full_table_sql, split_bucket
    from connectors.snowflake.settings import resolve_snowflake_settings

    raw_name = req.name or ""
    if raw_name.strip() != raw_name or not _is_safe_identifier(raw_name):
        raise HTTPException(
            status_code=400,
            detail=(
                f"snowflake: view name {raw_name!r} is unsafe — must match "
                f"^[a-zA-Z_][a-zA-Z0-9_]{{0,63}}$ (DuckDB identifier rules) "
                "with no leading/trailing whitespace"
            ),
        )

    settings = resolve_snowflake_settings()
    if not settings:
        raise HTTPException(
            status_code=400,
            detail=(
                "snowflake: data_source.snowflake.* is not configured; "
                "set account, user, database, warehouse and either the SNOWFLAKE_PASSWORD env / vault secret "
                "(password auth) or the SNOWFLAKE_PRIVATE_KEY env / vault secret (key-pair auth) "
                "via instance.yaml or /admin/server-config first"
            ),
        )

    default_database = (settings.get("database") or "").strip()
    if not default_database:
        raise HTTPException(
            status_code=400,
            detail="snowflake: data_source.snowflake.database is required",
        )

    if req.query_mode == "materialized" and req.source_query and req.source_query.strip():
        # Custom SQL is self-contained; bucket/source_table are only required when
        # the server generates the full-table dump itself (mirrors Databricks validator).
        # It still has to obey the one contract the materialize session imposes:
        # the only catalog that exists there is `sf`.
        _assert_snowflake_custom_sql_targets_sf(req.source_query)
        return

    bucket = (req.bucket or "").strip()
    source_table = (req.source_table or "").strip()
    if not bucket or not source_table:
        raise HTTPException(
            status_code=422,
            detail="snowflake: bucket (schema) and source_table are required",
        )

    row_database, schema = split_bucket(bucket, default_database)
    if row_database.lower() != default_database.lower():
        raise HTTPException(
            status_code=400,
            detail=(
                f"snowflake: bucket {bucket!r} resolves to database {row_database!r} "
                f"which does not match the configured database {default_database!r}"
            ),
        )

    if schema.strip() != schema or not _is_safe_quoted_identifier(schema):
        raise HTTPException(
            status_code=400,
            detail=(
                f"snowflake: schema {schema!r} is unsafe (only [A-Za-z0-9_.-] allowed, no leading/trailing whitespace)"
            ),
        )
    if source_table.strip() != source_table or not _is_safe_quoted_identifier(source_table):
        raise HTTPException(
            status_code=400,
            detail=(
                f"snowflake: source_table {source_table!r} is unsafe (only [A-Za-z0-9_.-] "
                "allowed, no leading/trailing whitespace)"
            ),
        )

    if req.query_mode == "remote":
        # Remote rows do not carry source_query; the view is built from bucket/source_table.
        return

    if req.query_mode == "materialized":
        # No custom SQL provided; generate the full-table SELECT for the scheduler.
        try:
            req.source_query = full_table_sql(schema, source_table)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"snowflake: {e}") from e
        return


class _SnowflakeRebuild(NamedTuple):
    """Outcome of one ``extracts/snowflake/extract.duckdb`` rebuild.

    ``ok=False`` is reserved for a *hard* failure (an exception, or per-table
    errors) — a skipped rebuild is a message, so a benign skip never turns a
    successful registration into a 500.

    ``failed_tables`` names the registry rows the rebuild could not build.
    ``rebuild_from_registry`` walks EVERY remote row, not just the one being
    registered, so the aggregate message routinely describes somebody else's
    broken row; a caller that records the failure against a specific row must
    check this set first or it marks a healthy new row as failed. Empty on a
    hard exception, where there is no per-table attribution to be had and the
    rebuild genuinely failed for every row.

    ``rebuilt`` separates "ran and produced views" from "skipped" — both of
    which report ``ok=True``. A caller that CLEARS recorded state (rather than
    recording new state) must gate on this: a skip verified nothing, so wiping
    a row's real failure on the back of one turns a broken table green.
    """

    ok: bool
    message: str
    failed_tables: set[str]
    rebuilt: bool


def _rebuild_snowflake_remote_extract() -> _SnowflakeRebuild:
    """Rebuild ``extracts/snowflake/extract.duckdb`` for remote rows."""
    from connectors.snowflake.extract_init import rebuild_from_registry

    try:
        result = rebuild_from_registry()
    except Exception as exc:
        logger.exception("snowflake remote extract rebuild failed")
        return _SnowflakeRebuild(False, f"snowflake remote extract rebuild failed: {exc}", set(), False)

    if result.get("skipped"):
        # A *skipped* rebuild is a message, never a failed registration —
        # mirroring `_rebuild_databricks_remote_extract`. The registry row is
        # already persisted by the time we get here, so answering 500 would
        # tell the operator the registration failed while the row is in fact
        # live. `no_remote_rows` is a plain no-op (e.g. the update_table
        # background pass); `not_configured` is reachable whenever the password
        # resolved at validation time but not at rebuild time — in both cases
        # the fix is to re-trigger a sync once the instance is configured, not
        # to re-register.
        reason = result.get("reason")
        if reason == "not_configured":
            return _SnowflakeRebuild(
                True,
                "snowflake remote extract skipped: Snowflake is not configured, so the "
                "sf catalog was not attached. Set data_source.snowflake.* + the password "
                "env/vault secret, then POST /api/sync/trigger to build the extract.",
                set(),
                False,
            )
        return _SnowflakeRebuild(True, f"snowflake remote extract skipped: {reason}", set(), False)

    errors = result.get("errors") or []
    if errors:
        failed = {str(e.get("table")) for e in errors if isinstance(e, dict) and e.get("table")}
        return _SnowflakeRebuild(False, f"snowflake remote extract rebuilt with errors: {errors}", failed, True)

    return _SnowflakeRebuild(
        True,
        f"snowflake remote extract rebuilt; {result.get('tables_registered', 0)} table(s) registered",
        set(),
        True,
    )


def _rebuild_snowflake_remote_extract_bg(table_name: Optional[str] = None) -> None:
    """Fire-and-forget wrapper used by ``update_table`` BackgroundTasks.

    ``table_name`` is the edited row's registry ``name``. Correcting a
    schema/table that does not exist upstream is the whole point of this edit
    path, and registration records such a failure on the row (see
    ``register_table``), so a rebuild that now succeeds has to CLEAR it —
    otherwise ``GET /api/admin/registry`` and /admin/sync keep serving the old
    error until the next full orchestrator sweep re-derives ``sync_state`` from
    ``_meta``, and the fix reads as if it did not take.
    """
    outcome = _rebuild_snowflake_remote_extract()
    (logger.info if outcome.ok else logger.error)("%s", outcome.message)

    # Clear on THIS row's outcome, never on the aggregate. Two separate traps
    # live here, and `outcome.ok` alone walks into both:
    #
    #   * `ok` is False as soon as ANY registered remote row errors, and the
    #     rebuild walks every one of them. On the instance this whole change
    #     set came from — which carries pre-existing phantom rows — `ok` is
    #     permanently False, so gating on it means the row the operator just
    #     corrected NEVER gets its error cleared, and the fix reads as if it
    #     did not take. That is the exact symptom being removed here.
    #   * `ok` is True for a benign SKIP (`not_configured` / `no_remote_rows`)
    #     by design, so a skip cannot 500 a registration. But a skip verified
    #     nothing, and the row's recorded failure is still true — clearing it
    #     would flip a table the operator cannot query to a green row.
    #
    # So: the rebuild must have actually RUN, and this row must not be among
    # the ones it could not build. Mirrors the attribution `register_table`
    # uses on the recording side.
    if table_name and outcome.rebuilt and table_name not in outcome.failed_tables:
        try:
            sync_state_repo().clear_error(table_name)
        except Exception as exc:
            logger.warning(
                "rebuild for %s succeeded but its recorded failure could not be "
                "cleared (%s); /admin/sync may show a stale error until the next sweep",
                table_name,
                exc,
            )


# Source types that don't depend on a `data_source.<name>.*` block — they
# get their data through a different ingestion path (e.g. Jira via
# webhooks). Registrations against these types are allowed regardless of
# the configured primary `data_source.type`.
_SOURCE_TYPES_INDEPENDENT_OF_DATA_SOURCE: frozenset[str] = frozenset(
    {
        "jira",
        "local",
    }
)


def _validate_source_type_configured(source_type: Optional[str]) -> None:
    """Refuse register-table requests whose ``source_type`` isn't actually
    configured on this instance.

    Pre-fix the route happily persisted e.g. ``source_type='keboola'`` on a
    BQ-only instance — the row landed in the registry but the scheduler had
    no Keboola URL/token to ATTACH against, so it silently never synced.
    No upfront error, no operator-visible signal until they noticed the
    table was missing from `agnes catalog`.

    A source_type is considered configured when:

    - a named ``source_connections`` row of that type exists — the registry
      is the source of truth (spec 2026-06-12); a connection added via
      /admin/data-sources lets that type register regardless of the legacy
      ``data_source.type``, OR
    - it matches the instance's primary ``data_source.type``, OR
    - a non-empty ``data_source.<source_type>`` block exists in the
      effective `instance.yaml` (legacy first-boot seed), OR
    - it's in the small allowlist of types that don't sit under
      `data_source.*` at all (Jira, local — see
      ``_SOURCE_TYPES_INDEPENDENT_OF_DATA_SOURCE``).

    Special case: when the configured primary is ``'local'`` (or its
    documented alias ``'csv'`` — the default when an instance is freshly
    bootstrapped and no `data_source.type` has been set yet), the validator
    stays permissive — refusing registrations here would block the
    first-time-setup workflow where the operator registers a few tables
    against a not-yet-fully-configured instance. The misconfiguration that
    this validator targets is the *explicit mismatch*: `type=bigquery`
    instance + `source_type=keboola` payload with no keboola connection and
    no `data_source.keboola.*` block. That case still 422s.

    A bare/None source_type is tolerated for backward compat with legacy
    CLI scripts; the route resolves it later against
    ``get_data_source_type()``.
    """
    if not source_type:
        return
    if source_type in _SOURCE_TYPES_INDEPENDENT_OF_DATA_SOURCE:
        return

    from app.instance_config import get_data_source_type, get_value

    configured_primary = get_data_source_type()
    if source_type == configured_primary:
        return

    # Registry is the source of truth (spec 2026-06-12): a configured
    # `source_connections` row of this type means the row can sync, regardless
    # of the legacy `data_source.type`. Checked before the instance.yaml
    # fallbacks below — a keboola connection added via /admin/data-sources must
    # let keboola tables register even on a bigquery-primary instance.
    from src.repositories import source_connections_repo

    if source_connections_repo().list(source_type=source_type):
        return

    # Legacy fallback (instance.yaml is a first-boot seed, not the authority):
    # accept if a non-empty `data_source.<source_type>` block exists. Empty
    # dict / None / "" all count as "not configured".
    secondary_block = get_value("data_source", source_type, default=None)
    if secondary_block:
        # Truthy non-empty dict / mapping / scalar — treat as configured.
        return

    # Bootstrap-friendliness: a primary of 'local' (or its documented alias
    # 'csv') means the instance hasn't been pointed at a real source yet (or
    # has been deliberately set to local-only). Don't gate registrations in
    # that state — the operator is likely in the middle of first-time setup
    # and will fill in the config next. The check still fires when primary is
    # an actual source type (bigquery / keboola) and the requested source_type
    # doesn't match AND has no connection or secondary block.
    if configured_primary in ("local", "csv"):
        return

    raise HTTPException(
        status_code=422,
        detail=(
            f"source_type={source_type!r} is not configured on this instance. "
            f"The configured data source is {configured_primary!r}. To enable "
            f"a secondary source, set data_source.{source_type}.* fields in "
            "instance.yaml or via /admin/server-config."
        ),
    )


class UpdateTableRequest(BaseModel):
    name: Optional[str] = None
    sync_strategy: Optional[str] = Field(
        default=None,
        description=(
            "v26+: drives the Keboola extractor dispatcher. PUT-shape "
            "requires a value if sent. See RegisterTableRequest.sync_strategy."
        ),
    )
    primary_key: Optional[List[str]] = None
    description: Optional[str] = None
    source_type: Optional[str] = None
    bucket: Optional[str] = None
    source_table: Optional[str] = None
    source_query: Optional[str] = None
    query_mode: Optional[str] = None
    sync_schedule: Optional[str] = None
    profile_after_sync: Optional[bool] = Field(
        default=None,
        deprecated=True,
        description=("DEPRECATED: not consumed by the runtime. See RegisterTableRequest.profile_after_sync."),
    )
    # v26 — same fields as RegisterTableRequest, all optional. The PUT
    # handler overlays the body on the existing row and re-runs the
    # synthetic RegisterTableRequest validator on the merged record, so
    # cross-field invariants are checked against the post-update state.
    incremental_window_days: Optional[int] = None
    max_history_days: Optional[int] = None
    incremental_column: Optional[str] = None
    where_filters: Optional[List[Dict[str, Any]]] = None
    partition_by: Optional[str] = None
    partition_granularity: Optional[str] = None
    initial_load_chunk_days: Optional[int] = None
    # v51 — see RegisterTableRequest.bq_fqn. PUT lets an admin add or
    # clear bq_fqn on an existing row (cleared via explicit `null`,
    # per the PUT shape contract documented on the handler below).
    bq_fqn: Optional[str] = None
    # v74 (#607) — distribution flag. PUT lets an admin toggle it on/off.
    # The query_mode='remote' conflict is enforced against the *merged*
    # record in the update_table handler (the PUT body alone may omit
    # query_mode, so it can't be validated here in isolation).
    server_only: Optional[bool] = None
    # v116 (table access policies design doc) — PUT lets an admin attach,
    # replace, or clear (explicit null) the SQL access policy on this row.
    # The feature flag gate, the SQL validator, and the distribution
    # interlock (§3.1/§3.2) all run in update_table against the *merged*
    # record — a PUT body alone can't judge coherence with the row's other
    # fields (query_mode, server_only, physical source). Persisted through
    # table_registry_repo().set_access_policy()/.set_policy_mapping(), not
    # register() — see the strip-tuple comment in update_table below.
    access_policy_sql: Optional[str] = None
    access_policy_note: Optional[str] = None
    policy_mapping: Optional[bool] = None

    @field_validator("access_policy_sql", mode="before")
    @classmethod
    def _normalize_access_policy_sql(cls, v):
        """Mirror source_query's whitespace-only -> None normalization
        below: a blank string is neither a valid policy body nor an
        explicit clear-via-null, so treat it as the latter rather than
        persist an incoherent empty VARCHAR."""
        if v is None:
            return None
        v = str(v).strip()
        return v or None

    @field_validator("sync_strategy", mode="before")
    @classmethod
    def _validate_sync_strategy(cls, v):
        if v in (None, ""):
            return v
        allowed = {"full_refresh", "incremental", "partitioned"}
        if v not in allowed:
            raise ValueError(f"sync_strategy must be one of {sorted(allowed)}, got {v!r}")
        return v

    @field_validator("partition_granularity", mode="before")
    @classmethod
    def _validate_partition_granularity(cls, v):
        if v in (None, ""):
            return v
        allowed = {"day", "month", "year"}
        if v not in allowed:
            raise ValueError(f"partition_granularity must be one of {sorted(allowed)}, got {v!r}")
        return v

    @field_validator("where_filters", mode="before")
    @classmethod
    def _validate_where_filters(cls, v):
        if v in (None, "", []):
            return None
        from connectors.keboola.where_filters import InvalidFilterError, parse_filters

        try:
            return parse_filters(v)
        except InvalidFilterError as e:
            raise ValueError(str(e))

    @model_validator(mode="after")
    def _check_mode_query_coherence(self):
        """PUT semantics — only the fields explicitly in the body are
        validated. The body is overlaid on the existing row at the handler
        level (see ``update_table``), so omitted fields keep their stored
        values and the synthetic ``RegisterTableRequest`` constructed against
        the merged record runs the strict cross-field check before persist.

        The only invariants enforceable from the PUT body alone:

        - explicit ``source_query='SELECT ...'`` paired with ``query_mode``
          that isn't materialized → coherent reject (the SQL would be dead);
        - explicit ``source_query='SELECT ...'`` without any ``query_mode``
          in the body → reject; the operator must commit to materialized;
        - explicit empty/whitespace ``source_query=''`` paired with
          ``query_mode='materialized'`` → reject (operator clearly
          mistyped — they sent the field).

        Pre-fix this validator also rejected ``{"query_mode": "materialized",
        "sync_schedule": "every 12h"}`` because ``source_query`` was None
        — but that's the canonical "edit the schedule on a materialized
        row" use-case from the Edit modal, which always sends
        ``query_mode`` to indicate intent. Devin BUG_0002 on PR #148
        commit 2219255.
        """
        if self.query_mode is None and self.source_query is None:
            return self

        sq_raw = self.source_query
        sq = (sq_raw or "").strip() or None

        # Operator explicitly sent source_query as empty/whitespace while
        # claiming materialized — typo / bad form data, reject.
        if self.query_mode == "materialized" and sq_raw is not None and not sq:
            raise ValueError("query_mode='materialized' requires a non-empty source_query")

        # source_query only makes sense with materialized mode. Allow None
        # (omitted) to flow through; only reject when explicitly set with
        # the wrong mode.
        if self.query_mode is not None and self.query_mode != "materialized" and sq:
            raise ValueError("source_query is only valid when query_mode='materialized'")
        if self.query_mode is None and sq:
            raise ValueError("source_query requires query_mode='materialized' to be set in the same request")

        # Normalise: drop whitespace-only strings to None so the persisted
        # column is clean. Don't touch when source_query was None to begin
        # with — that signals "PUT didn't touch this field, keep existing".
        if sq_raw is not None:
            self.source_query = sq
        return self

    @field_validator("primary_key", mode="before")
    @classmethod
    def _coerce_primary_key(cls, v):
        return _normalize_primary_key(v)

    @field_validator("description", mode="before")
    @classmethod
    def _normalize_description(cls, v):
        # Defensive normalization for descriptions arriving via shell-quoting
        # tooling that injects literal backslash escapes (e.g. `Don\'t`, `\n`).
        return _unescape_shell_quoting(v)

    # Duplicated from RegisterTableRequest — Pydantic v2 validators don't
    # inherit cleanly across unrelated BaseModel classes; a shared mixin
    # would be overkill for two fields.
    @field_validator("sync_schedule", mode="before")
    @classmethod
    def _validate_sync_schedule(cls, v):
        # None / "" → no schedule, accepted.
        # Any non-empty string (including pure whitespace) must parse as a
        # valid schedule — otherwise it would be persisted and silently
        # ignored by the runtime evaluator.
        if v in (None, ""):
            return v
        if not is_valid_schedule(v):
            raise ValueError(
                f"sync_schedule must be 'every Nm' / 'every Nh' / "
                f"'daily HH:MM[,HH:MM,...]' / 'cron <min hour dom month dow>' "
                f"(e.g. 'cron 0 5 7 * *'), got {v!r}"
            )
        return v


class ConfigureRequest(BaseModel):
    data_source: str  # "keboola" | "bigquery" | "local"
    keboola_token: Optional[str] = None
    keboola_url: Optional[str] = None
    bigquery_project: Optional[str] = None
    bigquery_location: Optional[str] = None
    instance_name: Optional[str] = None
    allowed_domain: Optional[str] = None
    confirm_connection_change: bool = False


@router.get("/discover-tables")
async def discover_tables(
    user: dict = Depends(require_admin),
    dataset: Optional[str] = None,
):
    """Discover available tables from the configured data source.

    For ``data_source.type='keboola'`` returns the full Storage API table
    list (single round-trip). For ``data_source.type='bigquery'``:

    - Without ``dataset``: list datasets in the configured project.
    - With ``dataset=name``: list tables (BASE TABLE + VIEW) in that dataset.

    Two-step shape avoids paying the per-dataset list_tables cost up-front
    on projects with hundreds of datasets — the UI populates the dataset
    dropdown first, then fetches tables only for the selected dataset.
    """
    try:
        from app.instance_config import get_data_source_type

        source_type = get_data_source_type()

        if source_type == "keboola":
            from app.instance_config import get_value
            from connectors.keboola.client import KeboolaClient

            from app.datasource_secrets import keboola_instance_token

            url = get_value("data_source", "keboola", "stack_url", default="")
            token_env = get_value("data_source", "keboola", "token_env", default="KEBOOLA_STORAGE_TOKEN")
            token, _provenance = keboola_instance_token(token_env)
            client = KeboolaClient(token=token or "", url=url)
            tables = client.discover_all_tables()
            return {"tables": tables, "count": len(tables), "source": "keboola"}

        if source_type == "bigquery":
            return _discover_bigquery(dataset=dataset)

        return {
            "tables": [],
            "count": 0,
            "source": source_type,
            "error": f"Discovery not implemented for source_type={source_type!r}",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Discovery failed: {e}")


def _discover_bigquery(dataset: Optional[str]) -> Dict[str, Any]:
    """List BQ datasets (when ``dataset`` is None) or tables-in-dataset.

    Routes through ``BqAccess.client()`` so config / auth / error
    translation matches the rest of the BQ surface (#138 facade). Returns
    the same shape as the Keboola path so the UI doesn't have to branch.
    """
    from connectors.bigquery.access import (
        BqAccessError,
        get_bq_access,
        translate_bq_error,
    )

    try:
        bq = get_bq_access()
        client = bq.client()
    except BqAccessError as e:
        raise HTTPException(
            status_code=BqAccessError.HTTP_STATUS.get(e.kind, 500),
            detail={"error": e.message, "kind": e.kind, "details": e.details},
        )

    try:
        if dataset is None:
            datasets = []
            for ds in client.list_datasets():
                datasets.append(
                    {
                        "dataset_id": ds.dataset_id,
                        "full_id": f"{ds.project}.{ds.dataset_id}",
                    }
                )
            return {
                "datasets": sorted(datasets, key=lambda d: d["dataset_id"]),
                "count": len(datasets),
                "source": "bigquery",
            }

        # List tables in the named dataset. `list_tables` returns
        # `TableListItem` with `table_id` + `table_type` ('TABLE', 'VIEW',
        # 'MATERIALIZED_VIEW', 'EXTERNAL', 'SNAPSHOT'). UI maps TABLE → Type
        # selector "table" and VIEW/MATERIALIZED_VIEW → "view"; the rest are
        # passed through with their raw type so the operator can decide.
        tables = []
        for t in client.list_tables(dataset):
            tables.append(
                {
                    "table_id": t.table_id,
                    "table_type": t.table_type,
                    "full_id": f"{t.project}.{t.dataset_id}.{t.table_id}",
                }
            )
        return {
            "tables": sorted(tables, key=lambda t: t["table_id"]),
            "count": len(tables),
            "source": "bigquery",
            "dataset": dataset,
        }
    except Exception as e:
        # `translate_bq_error` re-raises non-Google exceptions unchanged,
        # so wrap in HTTPException to keep the JSON-shape contract.
        try:
            err = translate_bq_error(e, bq.projects, bad_request_status="upstream_error")
        except Exception:
            raise HTTPException(status_code=502, detail=f"BQ discovery failed: {e}")
        raise HTTPException(
            status_code=BqAccessError.HTTP_STATUS.get(err.kind, 502),
            detail={"error": err.message, "kind": err.kind, "details": err.details},
        )


@router.get("/registry")
async def list_registry(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get full table registry.

    Each table row is enriched with its `sync_state` counterpart (#754) so
    the admin dashboard (`admin_sync.html`) and `agnes admin list-tables`
    can explain a "N total, 0 synced" run without trawling scheduler logs:

      - `last_sync_status`: 'ok' | 'error' | 'skipped' | 'pending' (never
        synced). Mirrors `sync_state.status`, defaulted to 'pending' when
        no sync_state row exists yet.
      - `last_sync_error`: the persisted error/skip-reason string when
        `last_sync_status` is 'error' or 'skipped'; None otherwise.
      - `last_sync`: full ISO timestamp of the last successful sync, or
        None if never synced. (`last_sync_display` below is the
        pre-existing truncated "YYYY-MM-DD HH:MM" form, kept for backward
        compatibility with any existing consumer.)
      - `rows` / `file_size_bytes`: the last successful sync's row count /
        parquet size, so the dashboard's "Rows" / "Size" columns aren't
        silently blank for tables that DID sync.
    """
    repo = table_registry_repo()
    tables = repo.list_all()

    # Single batched read of sync_state — avoid N+1 GETs against
    # `sync_state` for large registries. B1: writers resolve `table_id` to
    # the registry `id` when a matching row exists at write time (see
    # `src.sync_state_key.resolve_sync_state_key`), so the join below tries
    # `id` first. A row still keyed by `name` — a legacy row the backfill
    # migration hasn't reached yet, or a fallback write for a table whose
    # `_meta.table_name` had no registry match — is picked up by name so it
    # doesn't silently vanish from this view.
    state_by_key: Dict[str, Dict[str, Any]] = {}
    try:
        rows = sync_state_repo().get_all_states()
        for row in rows:
            tid = row.get("table_id")
            if tid:
                state_by_key[tid] = row
    except Exception:
        # Defensive: if sync_state is unreadable for any reason, the
        # registry response still serializes — operators just lose the
        # enriched columns on this call.
        logger.exception("Failed to read sync_state for registry")

    for t in tables:
        state = state_by_key.get(t.get("id")) or state_by_key.get(t.get("name"))
        status = state.get("status") if state else None
        error = state.get("error") if state else None
        ls = state.get("last_sync") if state else None

        t["last_sync_status"] = status or "pending"
        t["last_sync_error"] = error if (status in ("error", "skipped") and error) else None
        t["last_sync"] = ls.isoformat() if hasattr(ls, "isoformat") else ls
        t["last_sync_display"] = str(ls)[:16] if ls else None  # "YYYY-MM-DD HH:MM"
        t["rows"] = state.get("rows") if state else None
        t["file_size_bytes"] = state.get("file_size_bytes") if state else None

    return {"tables": tables, "count": len(tables)}


# Wall-clock budget for the synchronous BQ materialization that runs after
# a successful BQ register. If the rebuild + view creation exceeds this,
# we hand the rest off to BackgroundTasks and return 202. 5s matches the
# UX contract in #108 ("Queryable as <view> within seconds") — long enough
# to cover a healthy GCE round-trip, short enough that a hung GCE call
# doesn't park the request handler.
_BQ_SYNC_REGISTER_TIMEOUT_S: float = 5.0


def _materialize_bigquery_extract() -> Dict[str, Any]:
    """Re-build the BigQuery extract.duckdb + master views.

    Wrapper used by both the synchronous (in-band) and async (BackgroundTask)
    code paths after a BQ register/update/delete. Imports kept inside the
    function so non-BQ instances don't pay the import cost on app start.

    Opens a FRESH system DB connection rather than reusing the request-scoped
    one. The request handler closes its connection in a `finally` after the
    response, but BackgroundTask + the timeout-fallback daemon thread can
    both outlive that close — they would then operate on a closed handle (or
    one being torn down concurrently). A fresh handle is cheap (DuckDB is an
    embedded engine) and isolates the worker's lifetime from the request's.

    Returns the rebuild result dict (``{"errors": [...], "tables_registered":
    N, ...}``) so the synchronous caller can propagate failures to the
    operator. Background-task callers ignore the return value, but the loud
    log inside ``_run_bigquery_materialize_with_timeout`` covers that path.
    """
    from connectors.bigquery import extractor as _bq_extractor
    from src.db import get_system_db
    from src.orchestrator import SyncOrchestrator
    from src.repositories import use_pg

    # rebuild_from_registry reads the table registry through the repository
    # factory when use_pg() (ignoring ``conn``); on Postgres pass None so the
    # system DuckDB is never opened (forbidden invariant). The analytics
    # extract.duckdb it writes is a separate DuckDB file on both backends.
    fresh_conn = None if use_pg() else get_system_db()
    try:
        result = _bq_extractor.rebuild_from_registry(conn=fresh_conn)
        SyncOrchestrator().rebuild()
        return result or {}
    finally:
        if fresh_conn is not None:
            try:
                fresh_conn.close()
            except Exception:
                pass


def _materialize_bigquery_extract_bg() -> None:
    """BackgroundTask wrapper around `_materialize_bigquery_extract`.

    BackgroundTasks discard return values, but `rebuild_from_registry` can
    surface auth / config / identifier errors via the ``errors`` list. Log
    those at ERROR level so the failure is loud in the operator's logs even
    though the 202 response can't carry the detail (Decision 3 in #108: a
    202 is documented as "accepted, may not be queryable yet" — we don't
    block on it but we shouldn't swallow it either).
    """
    try:
        result = _materialize_bigquery_extract()
    except Exception:
        logger.exception("BQ post-register background materialize crashed")
        return
    errors = (result or {}).get("errors") or []
    if errors:
        logger.error(
            "BQ post-register background materialize completed with %d error(s): %s",
            len(errors),
            errors,
        )


def _schedule_bq_materialize(background: BackgroundTasks) -> bool:
    """Route a fire-and-forget BQ post-register/update rebuild to the right
    executor.

    Worker-role process (single-box ``all``) → FastAPI BackgroundTask, the
    original behavior. Process WITHOUT the worker role (role-split ``api``
    replica) → enqueue the ``analytics-rebuild`` job so the worker plane does
    the write — the api plane must stay analytics-write-free (three-plane
    spec §3.1). Idempotency-keyed so a burst of registers/updates coalesces
    into one queued rebuild (the rebuild is registry-wide anyway).

    Returns ``True`` when the work was enqueued (job path), ``False`` when it
    was scheduled on the BackgroundTask (in-process path).
    """
    from app.roles import Role, role_enabled

    if role_enabled(Role.WORKER):
        background.add_task(_materialize_bigquery_extract_bg)
        return False
    from src.repositories import jobs_repo

    row = jobs_repo().enqueue("analytics-rebuild", payload={}, idempotency_key="analytics-rebuild")
    logger.info(
        "api-role replica: BQ rebuild enqueued as analytics-rebuild job %s (deduped=%s)",
        row.get("id"),
        row.get("deduped"),
    )
    return True


def _run_bigquery_materialize_with_timeout(
    background: BackgroundTasks,
) -> Dict[str, Any]:
    """Try to materialize synchronously within the wall-clock budget.

    Returns a dict with:
      - ``status`` ∈ {"ok", "errors", "timeout", "enqueued"} — caller maps
        to HTTP code
      - ``errors``: list of {table, error} surfaced by ``rebuild_from_registry``
        (only present on ``status="errors"``)

    Mapping by caller (`register_table`):
      - "ok"       → 200 (synchronous success)
      - "errors"   → 500 (rebuild ran but reported errors — propagate so
                     the operator knows the registry row exists but the
                     view wasn't created)
      - "timeout"  → 202 (rebuild still running on a BackgroundTask)
      - "enqueued" → 202 (process lacks the worker role — the rebuild rides
                     the ``analytics-rebuild`` job on the worker plane; the
                     api plane is analytics-write-free per three-plane §3.1)

    The synchronous worker runs on a daemon thread (so a hung GCE call
    can't park the request) that opens its OWN system DB connection (see
    `_materialize_bigquery_extract`). Even though FastAPI now invokes the
    sync route in a threadpool — and `done.wait()` no longer blocks the
    event loop — we still off-load to a daemon so the wait is bounded
    even if `rebuild_from_registry` ignores its own timeouts.
    """
    import threading

    from app.roles import Role, role_enabled

    if not role_enabled(Role.WORKER):
        # Role-split api replica: never run (or thread off) the rebuild in
        # this process — hand it to the worker plane via the job queue.
        _schedule_bq_materialize(background)
        return {"status": "enqueued"}

    done = threading.Event()
    err_holder: Dict[str, Any] = {}
    result_holder: Dict[str, Any] = {}

    def _worker():
        try:
            result_holder["result"] = _materialize_bigquery_extract()
        except Exception as e:  # pragma: no cover — logged below
            err_holder["error"] = e
        finally:
            done.set()

    t = threading.Thread(target=_worker, daemon=True, name="bq-register-rebuild")
    t.start()
    finished = done.wait(_BQ_SYNC_REGISTER_TIMEOUT_S)

    if finished:
        if "error" in err_holder:
            # Worker finished within the wall-clock budget but raised. This
            # is a HARD ERROR, not a timeout — surface it as such so the
            # operator gets the actual exception in the 500 body instead
            # of a misleading 202 + "still working in the background".
            # Earlier revisions returned ``{"status": "timeout"}`` here,
            # which the register handler then mapped to 202 + a retry
            # BackgroundTask; that hid the real failure for `_BQ_SYNC_
            # REGISTER_TIMEOUT_S` seconds before the BG retry surfaced
            # the same exception in the logs.
            exc = err_holder["error"]
            logger.error(
                "BQ post-register rebuild raised within budget: %r",
                exc,
            )
            return {
                "status": "errors",
                "errors": [{"error": f"{type(exc).__name__}: {exc}"}],
            }
        # Synchronous worker finished cleanly — but check whether
        # `rebuild_from_registry` itself surfaced any errors (auth fail,
        # missing project from the overlay, unsafe identifier slipping the
        # validator, etc.). Without this, those errors got silently logged
        # and the API claimed success.
        result = result_holder.get("result") or {}
        errors = result.get("errors") or []
        if errors:
            logger.error(
                "BQ post-register rebuild reported %d error(s): %s",
                len(errors),
                errors,
            )
            return {"status": "errors", "errors": errors}
        return {"status": "ok"}

    # Timed out — let the worker keep running on its thread (already daemon)
    # and also schedule a BackgroundTask so the orchestrator gets called via
    # the supported FastAPI path. `_INIT_EXTRACT_LOCK` in the BQ extractor
    # serializes the two file-swap calls so the slow daemon thread and the
    # background task can't tear `extract.duckdb`; the orchestrator's own
    # `_rebuild_lock` protects the master-view rebuild step downstream.
    logger.info(
        "BQ post-register rebuild exceeded %ss budget — handing off to BackgroundTask",
        _BQ_SYNC_REGISTER_TIMEOUT_S,
    )
    background.add_task(_materialize_bigquery_extract_bg)
    return {"status": "timeout"}


@router.post(
    "/register-table",
    responses={
        200: {"description": "BigQuery row registered + materialized synchronously"},
        201: {"description": "Non-BigQuery row registered (no post-insert materialize)"},
        202: {"description": "BigQuery row registered; materialize continues in background"},
        409: {"description": "Table id or view name already in use"},
        500: {"description": "BigQuery row registered but post-insert rebuild failed"},
    },
)
def register_table(
    request: RegisterTableRequest,
    background: BackgroundTasks,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Register a new table in the system.

    Behavior by source_type:
    - **bigquery**: validates BQ-specific shape (dataset / source_table /
      identifier safety / project_id format), forces query_mode='remote' and
      profile_after_sync=False, then synchronously rebuilds extract.duckdb +
      master views with a wall-clock budget. Returns 200 with the view name
      on success, 202 on budget overrun (rebuild continues in a
      BackgroundTask), or 500 if the synchronous rebuild ran but reported
      an error (e.g. auth failure, missing project, unsafe identifier).
    - other source types: insert-only, no post-register hook. Returns 201.

    Defined as a plain ``def`` (not ``async def``) so FastAPI runs it in a
    threadpool — the synchronous-materialize path waits on
    ``threading.Event.wait()``, which would otherwise block the asyncio
    event loop and stall every other request for up to ``_BQ_SYNC_REGISTER_
    TIMEOUT_S``. ``Depends(...)``, ``BackgroundTasks``, and
    ``JSONResponse`` all work the same in sync handlers; the rest of the
    admin module mixes both styles already.

    The route does NOT carry a default ``status_code`` — each branch returns
    its own JSONResponse with the right code. A blanket ``status_code=201``
    on the decorator would mislead OpenAPI consumers about the BQ branch.

    Always: 409 on view-name collision against the existing registry, audit
    log entry on success.
    """
    from fastapi.responses import JSONResponse

    if not request.name or not request.name.strip():
        raise HTTPException(status_code=422, detail="Table name cannot be empty")
    import re as _re

    repo = table_registry_repo()
    table_id = request.name.strip().lower().replace(" ", "_")

    if not _re.fullmatch(r"[a-z_][a-z0-9_]*", table_id):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Table name produces unsafe identifier '{table_id}'. "
                "Use only letters, digits, and underscores — no hyphens or special characters."
            ),
        )

    if repo.get(table_id):
        raise HTTPException(status_code=409, detail=f"Table '{table_id}' already registered")

    # View-name collision pre-check — distinct from id collision above.
    # `id` is derived from `name`, but two callers could legally pick
    # different display names that lower-case + slugify to the same view
    # (e.g. "Orders v2" + "orders_v2"); the strict view-name uniqueness
    # check stops that here, before the orchestrator surfaces it as a
    # silent overwrite at next rebuild.
    existing_by_name = next(
        (r for r in repo.list_all() if (r.get("name") or "") == request.name),
        None,
    )
    if existing_by_name is not None:
        raise HTTPException(
            status_code=409,
            detail=f"View name '{request.name}' is already in use by table id '{existing_by_name.get('id')}'",
        )

    # Refuse rows whose source_type isn't actually configured — pre-fix the
    # row landed in the registry but never synced because there was no
    # Keboola URL/token (or BQ project) to ATTACH against. Surfaces the
    # misconfig at registration time so the operator sees the gap before
    # they wonder why `agnes catalog` is missing the table.
    _validate_source_type_configured(request.source_type)

    # BQ rows go through the extra validation + post-insert materialization
    # contract from issue #108. Other source types keep the legacy insert-only
    # flow — Keboola materialization happens via the scheduled sync, Jira via
    # webhook, local via a manual extractor run.
    is_bigquery = request.source_type == "bigquery"
    if is_bigquery:
        _validate_bigquery_register_payload(request)
    if request.source_type == "databricks":
        # Materialized-only (model validator enforced); server-generate the
        # full-table SQL when the admin supplied bucket+source_table only.
        _validate_databricks_register_payload(request)
    if request.source_type == "snowflake":
        # Materialized or remote; server-generate the full-table SQL when the
        # admin supplied bucket (schema) + source_table and omitted custom SQL.
        _validate_snowflake_register_payload(request)

    # Phase C: profile_after_sync is no longer passed — the field is
    # deprecated and inert at the runtime layer. The DB column keeps its
    # schema default; the registry response no longer reflects request
    # values for this flag.
    # v51 — validate bq_fqn upfront. The extractor would catch a malformed
    # value at next rebuild and skip the row, but failing at register time
    # gives the admin a clean 422 with the specific complaint instead of
    # a silent "table registered but never materialized" state.
    if request.bq_fqn is not None and request.source_type != "bigquery":
        raise HTTPException(
            status_code=422,
            detail="bq_fqn only applies to source_type='bigquery'",
        )
    if request.bq_fqn is not None:
        from connectors.bigquery.extractor import parse_bq_fqn

        try:
            parse_bq_fqn(request.bq_fqn)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

    # v79 — validate connection_id FK before persisting.
    if request.connection_id is not None:
        from src.repositories import source_connections_repo

        if source_connections_repo().get(request.connection_id) is None:
            raise HTTPException(
                status_code=400,
                detail=f"connection_id '{request.connection_id}' not found in source_connections",
            )

    # §3.2 (table access policies design doc) — the physical-source twin.
    # Must run against the fully-normalized request (after the BQ coercion
    # above forced query_mode='remote' for a non-materialized BQ row, or
    # left 'materialized' alone) and BEFORE repo.register() persists
    # anything — a row this check rejects must never reach the registry,
    # let alone the BQ branch below that can materialize it. Shared with
    # update_table via _check_access_policy_physical_source_conflict.
    _check_access_policy_physical_source_conflict(
        source_type=request.source_type,
        connection_id=request.connection_id,
        bucket=request.bucket,
        source_table=request.source_table,
        bq_fqn=request.bq_fqn,
        source_query=request.source_query,
        query_mode=request.query_mode,
        server_only=bool(request.server_only),
        has_access_policy=False,
        exclude_id=None,
    )

    repo.register(
        id=table_id,
        name=request.name,
        folder=request.folder,
        sync_strategy=request.sync_strategy,
        primary_key=request.primary_key,
        description=request.description,
        registered_by=user.get("email"),
        source_type=request.source_type,
        bucket=request.bucket,
        source_table=request.source_table,
        source_query=request.source_query,
        query_mode=request.query_mode,
        sync_schedule=request.sync_schedule,
        # v26 sync-strategy support fields. None for non-Keboola or
        # full_refresh tables; persisted as NULL.
        incremental_window_days=request.incremental_window_days,
        max_history_days=request.max_history_days,
        incremental_column=request.incremental_column,
        where_filters=request.where_filters,
        partition_by=request.partition_by,
        partition_granularity=request.partition_granularity,
        initial_load_chunk_days=request.initial_load_chunk_days,
        bq_fqn=request.bq_fqn,
        server_only=request.server_only,
        connection_id=request.connection_id,
    )

    # Audit entry — masked params; description kept raw (it's documentation).
    audit_repo().log(
        user_id=user.get("id"),
        action="register_table",
        resource=table_id,
        params=_sanitize_for_audit(request.model_dump()),
    )

    from app.api.v2_catalog import invalidate_for_table

    invalidate_for_table(table_id)

    if not is_bigquery:
        message = None
        if request.source_type == "databricks" and request.query_mode == "remote":
            # A remote Databricks row is queryable the moment it is registered
            # — `agnes query --remote` reads the registry directly and ships
            # the statement to the warehouse. The extract rebuild below is only
            # for the optional Unity Catalog ATTACH (which gives DuckDB a local
            # view so the row can be joined against local data); it no-ops on
            # every instance that has not opted in.
            message = _rebuild_databricks_remote_extract()
        if request.source_type == "snowflake" and request.query_mode == "remote":
            # Snowflake remote rows need a local extract.duckdb with the
            # _remote_attach row and per-table views so the orchestrator can
            # ATTACH the sf catalog and create master views.
            ok, message, failed_tables, _rebuilt = _rebuild_snowflake_remote_extract()
            if not ok:
                # The row stays registered on purpose — the usual cause is a
                # mistyped schema/table, and editing the existing row beats
                # re-entering everything. But a bare row would then read
                # `pending` ("never synced") in /admin/sync and
                # `GET /api/admin/registry` forever: nothing retries a remote
                # rebuild except a re-save, so the operator has no way to tell
                # "this name does not exist upstream" from "waiting for the
                # first tick". Record the failure against the row so both
                # surfaces say so.
                # …but ONLY when this row is the one that failed. The rebuild
                # walks every registered remote row, so a single pre-existing
                # broken row (a schema dropped upstream, say) otherwise stamps
                # its error onto every healthy table registered afterwards —
                # the operator reads "error" plus somebody else's table name on
                # a row that is in fact fine, until the next orchestrator sweep
                # re-derives state from _meta. An empty `failed_tables` means a
                # hard exception with no per-table attribution, where the
                # rebuild did fail for this row too.
                if not failed_tables or request.name in failed_tables:
                    try:
                        sync_state_repo().set_error(request.name, message)
                    except Exception as exc:
                        logger.warning(
                            "could not record rebuild failure for %s in sync_state (%s); the 500 "
                            "response still carries the reason",
                            table_id,
                            exc,
                        )
                return JSONResponse(
                    status_code=500,
                    content={
                        "id": table_id,
                        "name": request.name,
                        "status": "rebuild_failed",
                        "view_name": table_id,
                        # `detail` is the key every client renders (FastAPI's own
                        # error shape, and what the admin UI reads); `message` is
                        # kept for existing consumers. Same content — pre-fix only
                        # `message` was set, so the UI fell through to a bare
                        # "✗ failed" and threw the real reason away.
                        "detail": message,
                        "message": message,
                    },
                )
        # Keboola / Jira / local rows are insert-only here. 201 Created — the
        # decorator no longer carries a default status, so each branch is
        # explicit about its code (BQ branch overrides via JSONResponse).
        content = {"id": table_id, "name": request.name, "status": "registered"}
        if message:
            content["message"] = message
        return JSONResponse(status_code=201, content=content)

    if request.query_mode == "materialized":
        # Materialized BQ rows are picked up by the trigger pass on the next
        # scheduled tick (or via POST /api/sync/trigger). No synchronous
        # rebuild — the COPY can scan multi-GB and would block the request.
        return JSONResponse(
            status_code=201,
            content={
                "id": table_id,
                "name": request.name,
                "status": "registered",
                "view_name": table_id,
                "message": (
                    "Materialized — parquet will be written on the next sync "
                    "tick. Trigger now via POST /api/sync/trigger."
                ),
            },
        )

    if request.defer_rebuild:
        # Bulk-onboarding path: the registry row is created but the
        # (O(registry)) extract + master-view rebuild is skipped. The caller
        # registers a batch this way and then triggers a single rebuild via
        # POST /api/admin/registry/rebuild — turning N per-insert rebuilds into
        # one. The table is NOT queryable until that rebuild runs.
        return JSONResponse(
            status_code=202,
            content={
                "id": table_id,
                "name": request.name,
                "status": "registered",
                "view_name": table_id,
                "message": ("Registered; rebuild deferred. Trigger once via POST /api/admin/registry/rebuild."),
            },
        )

    # BQ post-register: rebuild extract + master views, with timeout fallback.
    # Decision 1: 200 on synchronous success, 202 on timeout, 500 if the
    # synchronous rebuild surfaced errors. Distinct from the 201 Keboola
    # path above, so the BQ branch builds its own response.
    outcome = _run_bigquery_materialize_with_timeout(background)
    status = outcome.get("status")
    if status == "ok":
        return JSONResponse(
            status_code=200,
            content={
                "id": table_id,
                "name": request.name,
                "status": "ok",
                "view_name": table_id,
            },
        )
    if status == "errors":
        # Registry insert succeeded but the post-insert rebuild reported
        # errors — the row is in the registry but the master view was NOT
        # created. Surface the failure verbatim so the operator can fix
        # the underlying config (typically a missing
        # `data_source.bigquery.project` in the overlay or auth that lacks
        # bigquery.metadata.get on the dataset). The row stays in the
        # registry; a re-run after fixing the config picks up the existing
        # row and creates the view on the next register/update or
        # scheduler tick.
        return JSONResponse(
            status_code=500,
            content={
                "id": table_id,
                "name": request.name,
                "status": "rebuild_failed",
                "view_name": table_id,
                "errors": outcome.get("errors") or [],
                "message": (
                    "Registry row created but post-insert rebuild failed; "
                    "view is not queryable. See `errors` for details."
                ),
            },
        )
    # Default: "timeout" (rebuild continues on a BackgroundTask) or
    # "enqueued" (api-role replica — rebuild rides the analytics-rebuild
    # job on the worker plane). Both are the same 202 contract: the row is
    # registered, the view materializes asynchronously.
    return JSONResponse(
        status_code=202,
        content={
            "id": table_id,
            "name": request.name,
            "status": "accepted",
            "view_name": table_id,
            "message": "Registration accepted; materializing in background",
        },
    )


@router.post(
    "/registry/rebuild",
    responses={
        200: {"description": "Extract + master views rebuilt synchronously"},
        202: {"description": "Rebuild exceeded the wall-clock budget; continues in background"},
        500: {"description": "Rebuild surfaced errors; master views may be incomplete"},
    },
)
def rebuild_registry(
    background: BackgroundTasks,
    user: dict = Depends(require_admin),
):
    """Rebuild the BigQuery extract + master views once, across the whole registry.

    Companion to ``register-table``'s ``defer_rebuild``: a bulk-onboarding flow
    registers many tables with ``defer_rebuild=true`` (each skipping the
    O(registry) per-insert rebuild) and then calls this endpoint ONCE to
    materialize them all in a single rebuild — turning N registry-wide rebuilds
    into one. Same timeout/background semantics as a synchronous register: 200
    on synchronous success, 202 if the rebuild exceeds the wall-clock budget and
    continues on a BackgroundTask, 500 if it surfaced errors.
    """
    from fastapi.responses import JSONResponse

    from app.instance_config import get_data_source_type

    # The rebuild only makes sense on a BigQuery instance (it rebuilds the BQ
    # extract). On a non-BQ instance rebuild_from_registry would fail with a
    # "project missing" 500 — pre-check for a clean 422 instead.
    if get_data_source_type() != "bigquery":
        raise HTTPException(
            status_code=422,
            detail="registry rebuild applies only to BigQuery instances",
        )

    outcome = _run_bigquery_materialize_with_timeout(background)
    status = outcome.get("status")
    audit_repo().log(
        user_id=user.get("id"),
        action="rebuild_registry",
        resource="registry",
        params={"status": status},
    )
    if status == "ok":
        # Views are materialized — drop every per-table catalog cache so reads
        # taken during the deferred-register window (which would have cached a
        # no-view schema) don't serve stale results. (The 202 background path
        # mirrors register-table: caches were invalidated per table at register.)
        from app.api.v2_catalog import invalidate_all

        invalidate_all()
        return JSONResponse(status_code=200, content={"status": "ok"})
    if status == "errors":
        return JSONResponse(
            status_code=500,
            content={"status": "rebuild_failed", "errors": outcome.get("errors") or []},
        )
    return JSONResponse(
        status_code=202,
        content={"status": "accepted", "message": "Rebuild continues in background"},
    )


class PrecheckResponse(BaseModel):
    """Response model for /api/admin/register-table/precheck.

    Documented here so OpenAPI consumers know what to expect; the route
    returns a plain dict for backwards compatibility with the rest of the
    admin API which doesn't use response_model.
    """

    ok: bool
    table: Dict[str, Any]


@router.post("/register-table/precheck")
def register_table_precheck(
    request: RegisterTableRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Validate a register-table payload + (BQ only) confirm the source table exists.

    No DB write. Used by the UI to surface row count + size + column count
    in the modal before the operator clicks Register, and by the CLI's
    ``--dry-run`` to print what *would* be registered without touching
    state. Identical Pydantic validation to register-table; for BQ rows we
    additionally make a ``bigquery.Client(project).get_table(...)`` call
    and surface the GCP error verbatim.

    Defined as a plain ``def`` (not ``async def``) so FastAPI runs it in a
    threadpool — the BQ branch makes synchronous ``bigquery.Client(...)``
    /``client.get_table(...)`` calls, which would otherwise block the
    asyncio event loop and stall every other request for the duration of
    the GCE round-trip. Mirrors the same conversion done for
    ``register_table`` (see comment on that route). ``Depends(...)`` works
    identically in sync handlers.
    """
    if not request.name or not request.name.strip():
        raise HTTPException(status_code=422, detail="Table name cannot be empty")

    if request.source_type == "snowflake":
        # Snowflake precheck is validation-only (no round-trip to the
        # warehouse in M1), but we still run the shape validator so the UI
        # surfaces the same errors as the real register call.
        _validate_snowflake_register_payload(request)
        return {
            "ok": True,
            "table": {
                "name": request.name,
                "source_type": "snowflake",
                "query_mode": request.query_mode,
                "bucket": request.bucket,
                "source_table": request.source_table,
                "source_query": request.source_query,
                "rows": None,
                "size_bytes": None,
                "columns": [],
                "note": "snowflake precheck is validation-only",
            },
        }

    if request.source_type != "bigquery":
        # M1 only adds BQ-specific precheck. Other source types get a
        # validation-only response so the CLI / UI can rely on the same
        # endpoint shape across types.
        return {
            "ok": True,
            "table": {
                "name": request.name,
                "source_type": request.source_type,
                "bucket": request.bucket,
                "source_table": request.source_table,
                "rows": None,
                "size_bytes": None,
                "columns": [],
                "note": "precheck for non-bigquery sources is validation-only in M1",
            },
        }

    # BQ-specific shape validation (forces query_mode/profile_after_sync,
    # checks identifier safety, validates project_id from instance.yaml).
    _validate_bigquery_register_payload(request)

    # Materialized BQ rows have no `dataset.source_table` to round-trip —
    # the SQL body is the contract. Skip the BQ-jobs-API call and return a
    # validation-only precheck so the CLI's `--dry-run --query-mode
    # materialized` path doesn't crash on an empty fully-qualified name.
    if request.query_mode == "materialized":
        return {
            "ok": True,
            "table": {
                "name": request.name,
                "source_type": "bigquery",
                "query_mode": "materialized",
                "source_query": request.source_query,
                "rows": None,
                "size_bytes": None,
                "columns": [],
                "note": (
                    "materialized precheck is validation-only — the SQL is "
                    "evaluated for cost on each scheduled materialize tick"
                ),
            },
        }

    # Round-trip the BQ jobs API to confirm the table exists and the SA can
    # see it. Imports kept local to avoid pulling google-cloud-bigquery into
    # the import chain on non-BQ instances.
    try:
        from google.api_core import exceptions as google_exc  # noqa: PLC0415
        from google.cloud import bigquery  # noqa: PLC0415
    except ImportError as e:
        raise HTTPException(
            status_code=500,
            detail=(f"google-cloud-bigquery not installed; install the bigquery extras to use BQ precheck ({e})"),
        ) from e

    from app.instance_config import get_value

    project_id = get_value("data_source", "bigquery", "project", default="")
    dataset = (request.bucket or "").strip()
    source_table = (request.source_table or "").strip()
    fq = f"{project_id}.{dataset}.{source_table}"

    try:
        client = bigquery.Client(project=project_id)
        bq_table = client.get_table(fq)
    except google_exc.NotFound as e:
        raise HTTPException(status_code=404, detail=f"BigQuery table not found: {fq} ({e})") from e
    except google_exc.Forbidden as e:
        raise HTTPException(
            status_code=403,
            detail=(
                f"BigQuery access denied for {fq}: {e}. Service account needs bigquery.metadata.get on the dataset."
            ),
        ) from e
    except Exception as e:
        # Auth errors, transient 5xx, malformed table refs — surface as 400
        # so the operator gets the GCP error verbatim and can fix their
        # config without us guessing the right HTTP code.
        raise HTTPException(status_code=400, detail=f"BigQuery precheck failed for {fq}: {e}") from e

    columns = [{"name": f.name, "type": f.field_type} for f in (bq_table.schema or [])]
    return {
        "ok": True,
        "table": {
            "name": request.name,
            "source_type": "bigquery",
            "bucket": dataset,
            "source_table": source_table,
            "project_id": project_id,
            "rows": int(bq_table.num_rows or 0),
            "size_bytes": int(bq_table.num_bytes or 0),
            "columns": columns,
            "column_count": len(columns),
        },
    }


def _policy_physical_source_signals(row: Dict[str, Any]) -> set:
    """Every physical-source signal ``row`` (a ``table_registry`` record)
    carries — the ways a DIFFERENT registry row could resolve to the exact
    same underlying data (table access policies design doc §3.2, "the
    physical-source twin"). A row may carry more than one signal at once
    (e.g. a BigQuery row with both ``bq_fqn`` and ``bucket``/``source_table``
    set); two rows collide when their signal sets intersect at all.
    """
    signals: set = set()
    bq_fqn = (row.get("bq_fqn") or "").strip().lower()
    if bq_fqn:
        signals.add(("bq_fqn", bq_fqn))
    bucket = (row.get("bucket") or "").strip()
    source_table = (row.get("source_table") or "").strip()
    if bucket and source_table:
        # Namespaced by source_type + connection_id too: two DIFFERENT
        # source systems (or two Keboola projects behind different
        # connections) can coincidentally reuse the same bucket/table label
        # without pointing at the same physical data.
        signals.add(
            (
                "bucket_table",
                (row.get("source_type") or "").strip().lower(),
                row.get("connection_id") or "",
                bucket.lower(),
                source_table.lower(),
            )
        )
    # Keboola's source_query is a JSON *filter* spec layered atop
    # bucket/source_table (see the materialized-mode coherence check in
    # update_table below), not an independent physical pointer — two
    # unrelated Keboola rows can carry byte-identical filter JSON (starting
    # with both blank/null). Every other source type's source_query IS the
    # physical pointer — the design doc's own §3.2 example is a materialized
    # SELECT against a fully-qualified remote table.
    source_query = (row.get("source_query") or "").strip()
    if source_query and (row.get("source_type") or "") != "keboola":
        signals.add(("source_query", " ".join(source_query.split()).lower()))
    return signals


def _is_distributable_registry_row(row: Dict[str, Any]) -> bool:
    """Whether ``row`` is the shape ``agnes pull`` downloads —
    ``query_mode in ('local', 'materialized')`` and not ``server_only``.
    The single definition both §3.2 directions below are keyed on, so the
    twin check and the attach check can never drift apart on what
    "distributable" means.
    """
    if (str(row.get("query_mode") or "local")).strip().lower() not in ("local", "materialized"):
        return False
    return not row.get("server_only")


def _find_policied_physical_source_twin(
    my_signals: set,
    *,
    exclude_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    """The first existing registry row that carries ``access_policy_sql``
    and whose physical-source signals intersect ``my_signals`` — i.e. the
    policied table a row with these signals would be a distributable twin
    of. ``None`` when there is no such row.
    """
    if not my_signals:
        return None
    for other in table_registry_repo().list_all():
        if other.get("id") == exclude_id or not other.get("access_policy_sql"):
            continue
        if my_signals & _policy_physical_source_signals(other):
            return other
    return None


def _find_unpolicied_physical_source_twin(
    my_signals: set,
    *,
    exclude_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    """The first existing registry row that carries NO policy and whose
    physical-source signals intersect ``my_signals`` — the row a caller
    granted it reads the raw data through, no matter what policy protects
    the other name.

    Supersedes the distributable-only scan this file shipped first. That
    one keyed on ``agnes pull``: a twin that never leaves the server was
    treated as harmless, and two undistributed rows over one source were
    explicitly allowed to coexist. Verified against a live instance, that
    is false — ``/api/query`` resolves the twin's own name server-side and
    returns the unfiltered, unmasked rows to anyone granted it, which is
    the same disclosure the parquet would have made, minus the file. The
    scan is therefore on physical-source overlap plus "has no policy of
    its own", not on distributability. Two POLICIED rows over one source
    stay legal: each read goes through a policy, and which one an admin
    wants where is their call.
    """
    if not my_signals:
        return None
    for other in table_registry_repo().list_all():
        if other.get("id") == exclude_id or other.get("access_policy_sql"):
            continue
        if my_signals & _policy_physical_source_signals(other):
            return other
    return None


def _check_access_policy_physical_source_conflict(
    *,
    source_type: Optional[str],
    connection_id: Optional[str],
    bucket: Optional[str],
    source_table: Optional[str],
    bq_fqn: Optional[str],
    source_query: Optional[str],
    query_mode: Optional[str],
    server_only: bool,
    has_access_policy: bool = False,
    clearing_policy: bool = False,
    exclude_id: Optional[str] = None,
) -> None:
    """§3.2 (table access policies design doc) — the physical-source twin.

    Rejects a table-registry row-in-progress (about to be inserted by
    ``register_table``, or the merged shape ``update_table`` is about to
    persist) when BOTH:

    - it carries no policy of its own (``has_access_policy=False``), AND
    - its physical source (``bq_fqn`` / ``(source_type, connection_id,
      bucket, source_table)`` / non-Keboola ``source_query`` — see
      ``_policy_physical_source_signals``) matches that of ANY existing
      registry row carrying ``access_policy_sql``.

    The first draft of this check also required the row to be
    DISTRIBUTABLE, on the reasoning that a row which never leaves the
    server hands nothing to an analyst. That is wrong, and was wrong in
    production: ``/api/query`` resolves an undistributed row by name
    server-side and returns its raw rows to anyone granted it, so an
    unpolicied ``server_only`` / ``remote`` twin discloses exactly what
    the policy withholds. Distributability now only changes the wording of
    the rejection, never whether it fires.

    Shared between ``register_table`` (a brand-new row, not yet persisted —
    call with ``exclude_id=None``) and ``update_table`` (an existing row
    being edited — call with ``exclude_id=table_id`` so the row doesn't
    collide with its own already-persisted signals). Mirrors how
    ``_validate_bigquery_register_payload`` is already shared between the
    two handlers. The caller MUST run this before persisting the row and
    (for ``register_table``) before any materialization can run — a row
    rejected here must never reach disk.

    The ATTACH direction — a policy going onto a row that already has an
    unpolicied twin — is the mirror check
    ``_check_policied_row_has_no_unpolicied_twin`` below; this one
    structurally cannot cover it (the row being written IS the policied
    one there, so the guard above returns early).

    ``clearing_policy`` changes only the WORDING, never the verdict. Two
    policied rows over one source are legal, and clearing either one's policy
    leaves an unpolicied name over a source the other still policies — the
    disclosure this check exists to refuse, so it must still fire. But the
    default wording ("attach a policy to this row too") is nonsense addressed
    to an admin who is *removing* one, and it names neither escape that
    actually works: repoint this row at a different physical source first (its
    policy travels with it, so the clear then succeeds), or unregister one of
    the pair. Set by ``update_table`` when the pre-write row carried a policy
    and the merged one does not.

    Raises ``HTTPException(422, "access_policy_physical_source_conflict")``.
    """
    if has_access_policy:
        return
    my_signals = _policy_physical_source_signals(
        {
            "source_type": source_type,
            "connection_id": connection_id,
            "bucket": bucket,
            "source_table": source_table,
            "bq_fqn": bq_fqn,
            "source_query": source_query,
        }
    )
    other = _find_policied_physical_source_twin(my_signals, exclude_id=exclude_id)
    if other is not None:
        if clearing_policy:
            raise HTTPException(
                status_code=422,
                detail=(
                    "access_policy_physical_source_conflict: clearing this "
                    "table's access policy would leave it an unpolicied name "
                    "over the same physical source as table "
                    f"{other.get('id')!r} ({other.get('name')!r}), which still "
                    "carries one -- and an unpolicied name returns the "
                    "unfiltered rows to anyone granted it. Point this row at a "
                    "different physical source first (its policy travels with "
                    "it, so the clear then succeeds), or unregister one of the "
                    "two rows"
                ),
            )
        raise HTTPException(
            status_code=422,
            detail=(
                "access_policy_physical_source_conflict: this table's "
                f"physical source matches table {other.get('id')!r} "
                f"({other.get('name')!r}), which has an access policy "
                "attached -- a second, unpolicied name over the same "
                "source returns the unfiltered rows to anyone granted it, "
                "so attach a policy to this row too, point it at a "
                "different physical source, unregister one of the two rows, "
                "or read the policied table by its own name"
            ),
        )


def _check_policied_row_has_no_unpolicied_twin(merged: Dict[str, Any], *, table_id: str) -> None:
    """§3.2, the ATTACH direction — refuse to leave a policy on a row that
    an EXISTING unpolicied row resolves to the same physical source as.

    ``_check_access_policy_physical_source_conflict`` above asks "is THIS
    row an unpolicied twin of a policied one", so on the attach path it
    always short-circuits: the row being written is the policied one.
    Registering ``twin`` first and only THEN attaching the policy to
    ``orig`` would otherwise be accepted with no scan at all — and since
    nothing ever PUTs ``twin`` again, the twin-side interlock never runs
    for it. This is the symmetric scan that closes it.

    A distributable twin leaks through ``agnes pull``; an undistributed
    one leaks through ``/api/query`` resolving its name server-side for
    anyone granted it. Both are refused here — the first draft exempted
    the second, which a live instance disproved.

    Evaluated against the MERGED record on every write that leaves a
    policy attached — not only the PUT that attaches one — exactly like
    the §3.1 interlock, so the incoherent shape can't be reached in two
    steps either. Clearing ``access_policy_sql`` short-circuits HERE (no
    policy on the merged record, nothing to protect) — but that is not by
    itself a safety valve, and the earlier version of this docstring claiming
    "an admin can always undo the policy" was wrong. The twin check on the
    other side then sees an unpolicied row over a still-policied source and
    refuses, which is correct: two policied rows over one source are legal,
    an unpolicied one beside a policied one is the disclosure. What unwinds
    such a pair is repointing one row (its policy travels with it, so the
    clear then succeeds) or unregistering one — both named in that
    rejection.

    Raises ``HTTPException(422, "access_policy_physical_source_conflict")``.
    """
    if not merged.get("access_policy_sql"):
        return
    my_signals = _policy_physical_source_signals(merged)
    other = _find_unpolicied_physical_source_twin(my_signals, exclude_id=table_id)
    if other is not None:
        distributable = _is_distributable_registry_row(other)
        reach = (
            "agnes pull would hand out the unfiltered rows this policy exists to withhold"
            if distributable
            else "any caller granted it reads the unfiltered rows "
            "server-side under that name, which is the same disclosure "
            "without the parquet"
        )
        raise HTTPException(
            status_code=422,
            detail=(
                "access_policy_physical_source_conflict: table "
                f"{other.get('id')!r} ({other.get('name')!r}) points at this "
                "table's physical source and carries no policy of its own "
                f"(query_mode={str(other.get('query_mode') or 'local')!r}, "
                f"server_only={bool(other.get('server_only'))}), so {reach} "
                "-- attach a policy to that row, unregister it, or point it "
                "at a different physical source, then attach this policy"
            ),
        )


@router.put("/registry/{table_id}")
async def update_table(
    table_id: str,
    request: UpdateTableRequest,
    background: BackgroundTasks,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Update a registered table's configuration.

    For BQ rows, schedules a background rebuild so the master view picks
    up changes (e.g. a renamed dataset) without waiting for the next
    scheduled sync.
    """
    repo = table_registry_repo()
    existing = repo.get(table_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Table not found")

    # `exclude_unset=True` honors the PUT-shape distinction between
    # "field omitted from body" (keep existing) vs "field sent as null"
    # (clear to NULL). Pre-v26 the handler used `model_dump()` filtered by
    # `if v is not None`, which collapsed both cases to "omitted" — meaning
    # an admin couldn't clear a field via PUT. v26 needs the clear path so
    # the Edit modal can switch a partitioned row back to full_refresh and
    # have the stale partition_by / partition_granularity / max_history_days
    # actually go away (without this fix, those fields linger and either
    # confuse the dispatcher or trip the v26 conflict-policy validator on
    # the next edit).
    #
    # Contract change (Devin Review finding 0001): callers that previously
    # sent explicit `null` to mean "no-op, keep existing" will now have the
    # field cleared. In practice this is fine — the only known caller is
    # the Edit modal, which pre-populates form fields from the existing row
    # and JSON-encodes the populated (non-null) value back. CLI register-table
    # only POSTs new rows, never PUTs nulls. If a future client needs the
    # old "null = no-op" semantics for some field, it should omit the field
    # from the body instead of sending null — that's the canonical PUT shape.
    updates = request.model_dump(exclude_unset=True)
    # View-name / id collision guard, mirrored from register_table's
    # `existing_by_name` check. `table_registry.name` has no DB-level
    # uniqueness constraint and register_table only pre-checks it against
    # OTHER names on the way in (a duplicate matching another row's ID is
    # already caught there, indirectly, by the derived-id collision check —
    # PUT never re-derives an id, so that protection doesn't carry over
    # here). Left unchecked, a rename could collide with another table's
    # `name` (the original register_table concern: a silent view overwrite
    # at next rebuild) OR — since B1 — with another table's `id`: every
    # id-first sync_state/manifest resolver (`app/api/sync.py::_reg_for`,
    # the distribution mirror job in `app/worker/kinds.py`, this module's
    # own `list_registry`) tries a raw key against the registry BY ID
    # before falling back to name, so a legacy name-keyed sync_state row
    # sharing that string would resolve to the WRONG registry entry.
    if "name" in updates and updates["name"] != existing.get("name"):
        new_name = updates["name"]
        collision = next(
            (
                r
                for r in repo.list_all()
                if r.get("id") != table_id and ((r.get("name") or "") == new_name or r.get("id") == new_name)
            ),
            None,
        )
        if collision is not None:
            raise HTTPException(
                status_code=409,
                detail=f"View name '{new_name}' is already in use by table id '{collision.get('id')}'",
            )
    # Run BQ-shape validation BEFORE persisting whenever the merged record
    # would be a bigquery row (existing was BQ, or the patch flips it to BQ,
    # or the patch touches BQ-relevant fields on an already-BQ row). Without
    # this gate, an admin could PUT `bucket="evil\"; DROP --"` onto a BQ
    # row and the next rebuild would silently fail at view-create time —
    # surface the bad shape at PUT time instead.
    if updates:
        # Preserve the original `registered_at` across PUTs — `repo.register`
        # now accepts it as an optional kwarg; without this the upsert would
        # stamp a fresh `now()` on every edit (issue #130).
        merged = dict(existing)
        merged.update(updates)
        merged.pop("id", None)  # avoid duplicate id kwarg

        # v52 + v56: per-table docs fields (sample_questions /
        # things_to_know / pairs_well_with + grain / platforms /
        # partition_col / history / gotchas) live on table_registry
        # but have their own PATCH /registry/{id}/docs endpoint.
        # ``repo.register()`` doesn't know them; stripping here keeps
        # the read-modify-write loop the PUT handler relies on
        # (existing → merged → register) from blowing up with
        # TypeError when the docs columns are populated.
        for _docs_key in (
            "sample_questions",
            "things_to_know",
            "pairs_well_with",
            "grain",
            "platforms",
            "partition_col",
            "history",
            "gotchas",
        ):
            merged.pop(_docs_key, None)

        # v116 — table access policies (access_policy_sql/_note/_updated_at/
        # _updated_by + policy_mapping) live on table_registry but, like the
        # docs fields above, are written through their own dedicated setters
        # (``table_registry_repo().set_access_policy`` / ``.set_policy_mapping``),
        # not ``register()``. Unlike the docs fields, the interlock below needs
        # to read these values off ``merged`` first (a PUT that only touches
        # server_only/query_mode must still be judged against a policy that's
        # already persisted and simply carried over from ``existing`` here) —
        # so the strip that keeps ``register()`` from TypeErroring on them runs
        # much later, immediately before the ``register()`` call itself.

        # v74 (#607) — validate the server_only ↔ query_mode invariant
        # against the *merged* record (the PUT body may toggle either field
        # independently). server_only=true is only coherent for a row with a
        # server-stored parquet (local / materialized); a 'remote' row has
        # none. Mirror the RegisterTableRequest validator at PUT time.
        if merged.get("server_only") and merged.get("query_mode") == "remote":
            raise HTTPException(
                status_code=422,
                detail=(
                    "server_only=true is only valid for query_mode='local' or "
                    "'materialized' (a 'remote' table has no server-stored "
                    "parquet to suppress from agnes pull)"
                ),
            )

        # When switching the merged record away from materialized mode, drop
        # the stale source_query — the request validator can't clear it via
        # the `if v is not None` filter above. Without this, a remote/local
        # row would carry an orphan source_query in the registry.
        if merged.get("query_mode") != "materialized":
            merged["source_query"] = None

        # Cross-source coherence: query_mode='materialized' + source_query rules:
        # - bigquery: null OK — server-generates source_query from bucket+source_table.
        # - keboola:  null OK — null means full-table export (valid at registration too;
        #             see RegisterTableRequest validator which guards only non-empty sq).
        # - all others: require an explicit non-empty source_query.
        if merged.get("query_mode") == "materialized":
            sq = merged.get("source_query")
            if not sq or not str(sq).strip():
                # BQ, Keboola and Snowflake all allow null/empty source_query —
                # the server generates it from bucket+source_table. All other
                # source types require an explicit source_query; raise 422.
                if merged.get("source_type") not in ("bigquery", "keboola", "snowflake"):
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            "query_mode='materialized' requires a non-empty "
                            "source_query. To revert to a non-materialized mode, "
                            "PATCH query_mode='local' (Keboola) or 'remote' "
                            "(BigQuery) and the stale source_query is cleared "
                            "automatically."
                        ),
                    )
            # Backtick guard removed for materialized rows: the Task 2 wrapping
            # path (connectors.bigquery.extractor.materialize_query) now runs
            # admin SQL through the BQ jobs API using BQ-native syntax, which
            # requires backticks for dashed project/dataset identifiers.
            # Non-materialized rows still reject backticks in the model validator.

            # Keboola materialized: source_query must be a JSON filter spec,
            # not SQL. Validate after the non-empty check above so we know sq
            # is a non-empty string here.
            if merged.get("source_type") == "keboola":
                _sq = str(merged.get("source_query", "") or "").strip()
                if _sq.upper().startswith(("SELECT", "WITH")):
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            "Keboola materialized source_query must be a JSON "
                            "filter spec (columns/whereFilters/changedSince), "
                            "not SQL. Use null for full-table export, or set "
                            "query_mode='local' for DuckDB-based Keboola pulls."
                        ),
                    )
                if _sq:
                    try:
                        json.loads(_sq)
                    except json.JSONDecodeError as _e:
                        raise HTTPException(
                            status_code=422,
                            detail=f"Keboola materialized source_query must be valid JSON: {_e}",
                        ) from _e

        if merged.get("source_type") == "databricks":
            # Reuse the register-time contract on updates too: the synthetic
            # runs the model validator (materialized-only gate) and the
            # payload validator (server-generated source_query), so a PUT
            # can neither flip a Databricks row out of 'materialized' nor
            # strand it without runnable SQL.
            try:
                synthetic = RegisterTableRequest(
                    name=merged.get("name") or table_id,
                    bucket=merged.get("bucket"),
                    source_table=merged.get("source_table"),
                    source_query=merged.get("source_query"),
                    source_type="databricks",
                    query_mode=merged.get("query_mode") or "materialized",
                    primary_key=merged.get("primary_key"),
                    description=merged.get("description"),
                    folder=merged.get("folder"),
                    sync_strategy=merged.get("sync_strategy") or "full_refresh",
                    sync_schedule=merged.get("sync_schedule"),
                    server_only=bool(merged.get("server_only") or False),
                )
            except ValidationError as e:
                raise HTTPException(status_code=422, detail=str(e)) from e
            _validate_databricks_register_payload(synthetic)
            merged["query_mode"] = synthetic.query_mode
            merged["source_query"] = synthetic.source_query

        if merged.get("source_type") == "snowflake":
            # Reuse the register-time contract on updates too: validate the
            # Snowflake shape and server-generate source_query when omitted.
            try:
                synthetic = RegisterTableRequest(
                    name=merged.get("name") or table_id,
                    bucket=merged.get("bucket"),
                    source_table=merged.get("source_table"),
                    source_query=merged.get("source_query"),
                    source_type="snowflake",
                    query_mode=merged.get("query_mode") or "materialized",
                    primary_key=merged.get("primary_key"),
                    description=merged.get("description"),
                    folder=merged.get("folder"),
                    sync_strategy=merged.get("sync_strategy") or "full_refresh",
                    sync_schedule=merged.get("sync_schedule"),
                    server_only=bool(merged.get("server_only") or False),
                )
            except ValidationError as e:
                raise HTTPException(status_code=422, detail=str(e)) from e
            _validate_snowflake_register_payload(synthetic)
            merged["query_mode"] = synthetic.query_mode
            merged["source_query"] = synthetic.source_query

        if merged.get("source_type") == "bigquery":
            # Reuse the register-time validator. It mutates the request to
            # force query_mode='remote' / profile_after_sync=False (or to
            # leave a materialized row alone) — apply the same coercion to
            # `merged` so the persisted row matches.
            synthetic = RegisterTableRequest(
                name=merged.get("name") or table_id,
                bucket=merged.get("bucket"),
                source_table=merged.get("source_table"),
                source_query=merged.get("source_query"),
                source_type="bigquery",
                query_mode=merged.get("query_mode") or "remote",
                profile_after_sync=bool(merged.get("profile_after_sync") or False),
                primary_key=merged.get("primary_key"),
                description=merged.get("description"),
                folder=merged.get("folder"),
                sync_strategy=merged.get("sync_strategy") or "full_refresh",
                sync_schedule=merged.get("sync_schedule"),
                # v74 (#607) — carry server_only into the synthetic so the
                # validator's post-coercion check fires when this PUT lands
                # the row in 'remote' mode; the merged-record check above
                # only saw the pre-coercion query_mode.
                server_only=bool(merged.get("server_only") or False),
            )
            _validate_bigquery_register_payload(synthetic)
            merged["query_mode"] = synthetic.query_mode
            merged["profile_after_sync"] = synthetic.profile_after_sync
            merged["source_query"] = synthetic.source_query
            # FQN normalization mutates source_table the same way the
            # validator coerces query_mode — copy it back so the persisted
            # row carries the bare table name, not the pasted path.
            merged["source_table"] = synthetic.source_table

            # v51 — same bq_fqn validation as register-table. PUT can both
            # add a fresh bq_fqn or update an existing one; in either case
            # malformed values should reject at PUT time, not silently
            # land in the DB and break the next rebuild.
            if merged.get("bq_fqn"):
                from connectors.bigquery.extractor import parse_bq_fqn

                try:
                    parse_bq_fqn(merged["bq_fqn"])
                except ValueError as e:
                    raise HTTPException(status_code=422, detail=str(e))
        else:
            # Non-BQ row carrying bq_fqn is nonsensical — reject the same
            # way register-table does.
            if merged.get("bq_fqn"):
                raise HTTPException(
                    status_code=422,
                    detail="bq_fqn only applies to source_type='bigquery'",
                )

        # v116 (table access policies design doc §3.1/§3.2) — evaluated
        # against the FINAL, fully-normalized ``merged`` record, i.e. AFTER
        # the BQ coercion above: a PUT that flips query_mode via BQ
        # coercion must be judged on the post-coercion value, the same
        # reason the server_only re-check on the BQ synthetic exists
        # (Devin Review, #630).
        if "access_policy_sql" in updates and updates["access_policy_sql"] is not None:
            # Attaching or replacing a policy in THIS request. Clearing
            # (explicit null, handled by the interlock below via
            # ``merged``) always stays possible regardless of the flag —
            # it is a safety valve, not a new grant — so only an actual
            # non-null SQL body is flag-gated and SQL-validated here.
            from app.instance_config import feature_enabled

            if not feature_enabled(
                "access_policies", "enabled", env_var="AGNES_ACCESS_POLICIES_ENABLED", default=False
            ):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "access_policies_disabled: table access policies are not "
                        "enabled on this instance -- set access_policies.enabled=true "
                        "(or AGNES_ACCESS_POLICIES_ENABLED=1) before attaching one"
                    ),
                )

            from src.access_policy_validate import PolicyValidationError, validate_policy_sql

            _mapping_table_names = {
                r["name"] for r in table_registry_repo().list_all() if r.get("policy_mapping") and r.get("name")
            }
            try:
                validate_policy_sql(
                    updates["access_policy_sql"],
                    table_id=table_id,
                    table_name=merged.get("name") or table_id,
                    mapping_table_names=_mapping_table_names,
                    for_remote=(merged.get("query_mode") == "remote"),
                )
            except PolicyValidationError as e:
                raise HTTPException(status_code=422, detail=f"{e.reason}: {e.detail}") from e

        # §4 (Task 14) — access_policy_note is MANDATORY whenever a non-null
        # access_policy_sql is attached or replaced. Tasks 2/4 deliberately
        # left this to the API layer: the repository setter accepts sql and
        # note independently (a future non-HTTP caller may have its own
        # reason to write without one), but every write through THIS
        # endpoint must explain why the policy exists — the inheriting
        # admin who finds forty lines of SQL joining `user_access` otherwise
        # has no way to tell "legal requirement" from "hunch", and the safe
        # move is always "leave it alone", so an unexplained policy
        # calcifies (§4's own reasoning).
        #
        # Evaluated against the MERGED/final record, like the §3.1/§3.2
        # interlocks below — not merely "did THIS PUT's body include a
        # note" — so a SEPARATE PUT that blanks only access_policy_note
        # while access_policy_sql stays attached is caught too (a naive
        # "only check when this PUT touches sql" rule would miss exactly
        # that "one toggle away" shape). Clearing the policy itself
        # (access_policy_sql explicit null) short-circuits this — merged
        # carries no sql, so nothing to explain — the same safety-valve
        # carve-out the flag gate above already gives clearing.
        if merged.get("access_policy_sql") and not (merged.get("access_policy_note") or "").strip():
            raise HTTPException(
                status_code=422,
                detail=(
                    "policy_note_required: access_policy_note is required whenever "
                    "access_policy_sql is set -- explain why this policy exists so the "
                    "next admin to find it knows whether it's safe to change"
                ),
            )

        # §3.1 — the interlock itself: a policy (attached by this PUT, or
        # already persisted and simply left untouched by it) may only
        # survive on a merged record that is 'remote' or server_only=true.
        # One check catches both directions the design doc names —
        # attaching a policy to a distributed table, AND clearing
        # server_only / moving query_mode to 'local' on an already-policied
        # one — because both leave the SAME incoherent shape: a policy on a
        # row `agnes pull` would otherwise download unfiltered.
        if merged.get("access_policy_sql") and merged.get("query_mode") != "remote" and not merged.get("server_only"):
            raise HTTPException(
                status_code=422,
                detail=(
                    "access_policy_requires_undistributed: a table carrying an access "
                    "policy must stay undistributed (query_mode='remote' or "
                    "server_only=true), so the policy can't be routed around via agnes "
                    "pull -- set server_only=true first, attach the policy to a "
                    "query_mode='remote' table instead, or clear access_policy_sql "
                    "before making this table distributable"
                ),
            )

        # §3.2 — the physical-source twin: a DIFFERENT row with no policy of
        # its own, pointing at the exact same physical source as an existing
        # policied table, hands every granted analyst the raw rows the policy
        # exists to withhold — through `agnes pull` when it is distributable,
        # and through `/api/query` resolving its name server-side when it is
        # not. Runs on every write that leaves the merged row UNPOLICIED (the
        # earlier draft keyed on distributability, which a live instance
        # disproved), independent of which fields this particular PUT changed —
        # the danger is the merged row's current shape, not the delta. Shared
        # with register_table's own call to the same helper, so a brand-new twin
        # is caught at registration too, not only here.
        _check_access_policy_physical_source_conflict(
            source_type=merged.get("source_type"),
            connection_id=merged.get("connection_id"),
            bucket=merged.get("bucket"),
            source_table=merged.get("source_table"),
            bq_fqn=merged.get("bq_fqn"),
            source_query=merged.get("source_query"),
            query_mode=merged.get("query_mode"),
            server_only=bool(merged.get("server_only")),
            has_access_policy=bool(merged.get("access_policy_sql")),
            # Wording only — see the helper. A PUT that REMOVES a policy is
            # still refused while another policied row covers the same source
            # (that is the disclosure), but the default message tells the admin
            # to attach a policy they are in the middle of removing.
            clearing_policy=bool(existing.get("access_policy_sql")) and not merged.get("access_policy_sql"),
            exclude_id=table_id,
        )

        # §3.2, the OTHER direction — the check just above is structurally
        # blind to it. It returns early whenever the row it is called for
        # carries a policy of its own, which on the attach path is always the
        # case, so it can only ever reject the TWIN's own write. A twin
        # registered BEFORE the policy existed is never PUT again, so nothing
        # would ever run that check for it: scan for one here instead.
        _check_policied_row_has_no_unpolicied_twin(merged, table_id=table_id)

        # §14.6 — the live LIMIT 0 execution probe. Runs LAST among the
        # policy-write checks: after static validation (rule 1-5, above)
        # AND after the §3.1/§3.2 interlocks, so a table this PUT would be
        # rejected for on distribution grounds gets that specific, cheaper
        # rejection instead of a probe failure — and never pays for a live
        # DuckDB execution it was always going to reject anyway. Static
        # analysis alone cannot catch a policy that references a column
        # the underlying table has since dropped (or never had) — this
        # turns that failure into a rejected write here, instead of the
        # first analyst's request.
        if "access_policy_sql" in updates and updates["access_policy_sql"] is not None:
            from src.access_policy_validate import PolicyValidationError, probe_policy
            from src.db import get_analytics_db_readonly

            probe_conn = get_analytics_db_readonly()
            try:
                probe_policy(updates["access_policy_sql"], table_id, probe_conn)
            except PolicyValidationError as e:
                raise HTTPException(status_code=422, detail=f"{e.reason}: {e.detail}") from e
            finally:
                probe_conn.close()

        # Capture the fully-validated policy fields before stripping them
        # out of ``merged`` (register() doesn't accept them — see the v116
        # comment above) so the setter calls after register() below persist
        # exactly what was just validated.
        _final_access_policy_sql = merged.get("access_policy_sql")
        _final_access_policy_note = merged.get("access_policy_note")
        for _policy_key in (
            "access_policy_sql",
            "access_policy_note",
            "access_policy_updated_at",
            "access_policy_updated_by",
            "policy_mapping",
        ):
            merged.pop(_policy_key, None)

        repo.register(id=table_id, **merged)

        # Persist the access-policy fields through their dedicated setters
        # (Task 2's set_access_policy/set_policy_mapping) — only called when
        # this PUT actually touched one of them, so an unrelated edit never
        # re-stamps access_policy_updated_at.
        if "access_policy_sql" in updates or "access_policy_note" in updates:
            repo.set_access_policy(
                table_id,
                sql=_final_access_policy_sql,
                note=_final_access_policy_note,
                updated_by=user.get("email"),
            )
        if "policy_mapping" in updates:
            repo.set_policy_mapping(table_id, bool(updates["policy_mapping"]))

    audit_repo().log(
        user_id=user.get("id"),
        action="update_table",
        resource=table_id,
        params=_sanitize_for_audit({"updated_fields": sorted(updates.keys()), **updates}),
    )

    # If we updated a BQ row (or one that's now BQ), refresh the extract in
    # the background so the view picks up renames / column-list changes.
    # Use the BG wrapper so any rebuild errors are logged at ERROR level
    # instead of being silently dropped by BackgroundTasks (which discards
    # return values).
    after = repo.get(table_id) or {}
    if after.get("source_type") == "bigquery":
        _schedule_bq_materialize(background)
    if after.get("source_type") == "snowflake" and after.get("query_mode") == "remote":
        # The POST-update name, not `existing`'s. `sync_state.table_id ==
        # table_registry.name` by convention, and the rebuild attributes its
        # per-table errors from the CURRENT registry rows — so on a PUT that
        # renames the row, the old name is absent from `failed_tables` no
        # matter what happened, the "not in failed_tables" guard passes, and a
        # rename that left the table still broken would clear the only record
        # of the failure. Renames are an anticipated case here (see above).
        background.add_task(_rebuild_snowflake_remote_extract_bg, after.get("name") or table_id)

    from app.api.v2_catalog import invalidate_for_table

    invalidate_for_table(table_id)

    return {"id": table_id, "updated": list(updates.keys())}


class PolicyPreviewRequest(BaseModel):
    """Body for ``POST /registry/{table_id}/policy/preview`` (design doc
    §13.1). ``sql`` is optional — omitted previews the table's currently
    stored ``access_policy_sql``; given, it previews a CANDIDATE body
    before it is ever saved. Exactly one of ``as_user`` / ``as_groups``
    selects the persona to run the policy as: ``as_user`` binds a real,
    existing user's identity (id/email) AND their LIVE group membership;
    ``as_groups`` is an ad-hoc group set with no real user behind it — the
    shape Task 15's persona matrix repeatedly calls this endpoint with, one
    distinct group-set at a time, per §13.1's "enumerates the distinct
    group-sets" (an ad-hoc set never needs a real account to exist).
    """

    sql: Optional[str] = None
    as_user: Optional[str] = None
    as_groups: Optional[List[str]] = None


# Mirrors ``src.access_policy._PATTERN_METACHARACTERS`` (§6.3) — group names
# are not validated against any character class elsewhere in the system, so
# a wildcard-named ad-hoc group here would silently widen a LIKE-adjacent
# policy the same way a Workspace-synced one would at live-enforcement time.
# Duplicated rather than imported: that constant is private to the resolver
# module, and this is the one OTHER place a caller-supplied string is bound
# as a ``$user_groups`` value instead of being read live from the DB.
_POLICY_PREVIEW_PATTERN_METACHARACTERS = ("%", "_")

# The only three identity values a policy may reference (§6.2) — mirrors the
# same closed set ``src/access_policy.py`` and
# ``src/access_policy_validate.py`` each already check against.
_POLICY_PREVIEW_KNOWN_VARIABLES = frozenset({"user_email", "user_id", "user_groups"})

_POLICY_PREVIEW_SAMPLE_LIMIT = 20


def _policy_preview_sample_is_redirectable(policy_sql: str, table_name: str) -> bool:
    """True when a ``WITH "<table_name>" AS (<bounded sample>)`` prelude
    provably redirects EVERY read the policy makes of its own table.

    The before/after preview is only honest if both lists cover the same
    source rows (see ``_policy_preview_samples``), and the prelude is how
    that is arranged -- but a CTE only shadows a BARE identifier. Two
    shapes escape it, and both are refused here rather than silently
    diffed against unrelated rows:

    * a qualified reference (``main.t``, ``db.main.t``) binds to the real
      table, not the CTE -- the policy validator matches on the last name
      part only, so this passes validation today;
    * a policy-defined CTE of the same name shadows OUR prelude, so the
      policy reads that instead.

    A body we cannot parse is refused too (conservative by construction).
    Note what this never does: rewrite, substitute, or otherwise edit the
    policy SQL. The body is handed to DuckDB verbatim either way.
    """
    import sqlglot
    from sqlglot import exp

    try:
        statement = sqlglot.parse_one(policy_sql, read="duckdb")
    except Exception:
        return False
    if statement is None:
        return False

    lname = table_name.lower()
    for cte in statement.find_all(exp.CTE):
        if (cte.alias_or_name or "").lower() == lname:
            return False

    saw_bare_reference = False
    for table in statement.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            continue
        if table.name.lower() != lname:
            continue
        if table.args.get("db") or table.args.get("catalog"):
            return False
        saw_bare_reference = True
    return saw_bare_reference


def _policy_preview_samples(analytics_conn, table_name: str, policy_sql: str, params: dict):
    """Both halves of the before/after preview over the SAME bounded rows.

    Returns ``(policied_sample, base_sample, comparable)``.

    The naive shape -- an unbounded ``SELECT * FROM (policy) LIMIT n``
    beside an independent ``SELECT * FROM t LIMIT n`` -- reads two
    different, unordered windows: whenever the rows a persona can see sit
    past the raw sample's window, the policied list carries rows the raw
    list never contains, and the UI pairs unrelated rows into false
    "dropped" rows and false masked-cell diffs.

    So the raw window is materialized ONCE into a per-request temp table
    (one bounded read, replacing the previous two -- strictly cheaper than
    before, which matters on a remote/BQ-backed table) and the policy is
    then run against a CTE that reads it back. Nothing rewrites the policy
    body; DuckDB's own name resolution prefers the CTE over the base
    table, which is exactly why ``_policy_preview_sample_is_redirectable``
    has to certify that every reference is shadowable first.

    When it is not, both samples fall back to independent bounded reads
    and ``comparable`` is True only when the raw sample is provably the
    WHOLE table (fewer rows came back than the limit) -- in which case the
    policied sample is the whole policied output and the two are exactly
    comparable anyway. Otherwise the caller is told not to diff them.
    """
    import uuid

    from src.sql_ident import quote_ident

    limit = _POLICY_PREVIEW_SAMPLE_LIMIT
    quoted_table = quote_ident(table_name)

    def _rows(cursor):
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, r)) for r in cursor.fetchall()]

    if _policy_preview_sample_is_redirectable(policy_sql, table_name):
        # Unique per request: a temp table is connection-scoped, but the
        # DuckLake backend hands out cursors off one long-lived connection,
        # so a fixed name could collide between concurrent previews.
        tmp = quote_ident(f"__agnes_policy_base_{uuid.uuid4().hex}")
        created = False
        try:
            analytics_conn.execute(f"CREATE TEMP TABLE {tmp} AS SELECT * FROM {quoted_table} LIMIT {limit}")
            created = True
        except Exception:
            created = False
        if created:
            try:
                base_sample = _rows(analytics_conn.execute(f"SELECT * FROM {tmp}"))
                policied_sample = _rows(
                    analytics_conn.execute(
                        f"WITH {quoted_table} AS (SELECT * FROM {tmp}) "
                        f"SELECT * FROM ({policy_sql}) AS __agnes_policy_preview__ LIMIT {limit}",
                        params,
                    )
                )
                return policied_sample, base_sample, True
            finally:
                try:
                    analytics_conn.execute(f"DROP TABLE IF EXISTS {tmp}")
                except Exception:
                    pass

    policied_sample = _rows(
        analytics_conn.execute(
            f"SELECT * FROM ({policy_sql}) AS __agnes_policy_preview__ LIMIT {limit}",
            params,
        )
    )
    base_sample = _rows(analytics_conn.execute(f"SELECT * FROM {quoted_table} LIMIT {limit}"))
    return policied_sample, base_sample, len(base_sample) < limit


def _sanitize_for_json(obj):
    """Recursively replace NaN / ±inf floats with None so a preview's sample
    rows survive JSON serialization -- FastAPI's default encoder rejects
    these even though Python's stdlib ``json`` accepts them by default, and
    NaNs show up routinely in DuckDB scans (NULL through certain casts).
    Same fix as ``app/api/v2_sample.py::_sanitize_for_json`` (duplicated
    rather than imported -- that one is private to its own module and this
    is the only other endpoint that hands back raw policy-query row values)."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, list):
        return [_sanitize_for_json(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    return obj


def _policy_preview_referenced_variables(sql: str) -> set:
    """Which of the three known ``$name`` variables ``sql`` actually
    references, so the bind dict only ever carries the keys the policy text
    uses — DuckDB rejects a named parameter bound but never referenced
    (§7.1 documents this exact failure mode for the BigQuery push-down; the
    same strictness applies to a plain parameterized DuckDB query). Mirrors
    the identical walk already duplicated in ``probe_policy``
    (``src/access_policy_validate.py``) and ``_referenced_variables``
    (``src/access_policy.py``) — each module computes its own because the
    VALUES bound differ per caller (probe: throwaway sentinels; the live
    resolver: the real caller's identity; here: the admin-chosen preview
    persona).
    """
    import sqlglot
    from sqlglot import exp

    statement = sqlglot.parse_one(sql, read="duckdb")
    return {p.name for p in statement.find_all(exp.Placeholder) if p.name in _POLICY_PREVIEW_KNOWN_VARIABLES}


@router.post("/registry/{table_id}/policy/preview")
async def preview_table_policy(
    table_id: str,
    request: PolicyPreviewRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Run a stored or candidate access policy as a chosen persona and
    report what it does (design doc §13.1) — the single-persona primitive
    admins use to check a policy before trusting it, and that Task 15's
    admin-UI matrix (multiple personas, union/overlap) is built from
    repeated calls to.

    Deliberately does NOT route through ``policied_relation``'s admin-bypass
    path (§12): that bypass exists for LIVE reads, where "is this caller
    exempt from filtering" is a real authorization decision keyed off the
    CALLING admin's own credential surface. A preview's entire point is
    "what would THIS CHOSEN persona see", independent of who is asking for
    it — so identity/groups are resolved directly from the request (or, for
    ``as_user``, from that user's own live membership), never from the
    calling admin's principal.

    Every preview is audited (§13.1: "it shows one person another person's
    slice, and 'who looked at whose data, when' is the first question asked
    after an incident").
    """
    from src.sql_ident import quote_ident

    row = table_registry_repo().get(table_id)
    if not row:
        raise HTTPException(status_code=404, detail="Table not found")

    if request.as_user and request.as_groups is not None:
        raise HTTPException(
            status_code=422,
            detail=(
                "policy_preview_persona_conflict: choose one persona selector -- "
                "as_user (an existing user) OR as_groups (an ad-hoc group set), not both"
            ),
        )
    if not request.as_user and request.as_groups is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "policy_preview_persona_required: identify a persona to preview as -- "
                "as_user (an existing user) or as_groups (an ad-hoc group set)"
            ),
        )

    is_candidate = request.sql is not None
    policy_sql = request.sql if is_candidate else row.get("access_policy_sql")
    if not policy_sql:
        raise HTTPException(
            status_code=422,
            detail=(
                "policy_preview_no_policy: this table has no stored access policy, and "
                "no candidate `sql` was given to preview"
            ),
        )

    if is_candidate:
        from src.access_policy_validate import PolicyValidationError, validate_policy_sql

        mapping_table_names = {
            r["name"] for r in table_registry_repo().list_all() if r.get("policy_mapping") and r.get("name")
        }
        try:
            validate_policy_sql(
                policy_sql,
                table_id=table_id,
                table_name=row.get("name") or table_id,
                mapping_table_names=mapping_table_names,
                for_remote=(row.get("query_mode") == "remote"),
            )
        except PolicyValidationError as e:
            raise HTTPException(status_code=422, detail=f"{e.reason}: {e.detail}") from e

    # Persona resolution -- exactly one of as_user / as_groups was required
    # above, so exactly one branch below runs.
    if request.as_user:
        from src.repositories import user_group_members_repo, users_repo

        target = users_repo().get_by_id(request.as_user) or users_repo().get_by_email(request.as_user)
        if not target:
            raise HTTPException(status_code=404, detail=f"user_not_found: no such user {request.as_user!r}")
        persona_user_id, persona_user_email = target["id"], target["email"]
        persona_groups = user_group_members_repo().list_group_names_for_user(persona_user_id)
    else:
        for group_name in request.as_groups:
            if any(ch in group_name for ch in _POLICY_PREVIEW_PATTERN_METACHARACTERS):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"policy_preview_unsafe_group_name: {group_name!r} contains a "
                        "pattern metacharacter (%, _) and cannot be bound as a group name"
                    ),
                )
        persona_user_id, persona_user_email = None, None
        persona_groups = list(request.as_groups)

    from src.access_policy_validate import PolicyValidationError, probe_policy
    from src.db import get_analytics_db_readonly

    analytics_conn = get_analytics_db_readonly()
    try:
        try:
            probed_columns = probe_policy(policy_sql, table_id, analytics_conn)
        except PolicyValidationError as e:
            raise HTTPException(status_code=422, detail=f"{e.reason}: {e.detail}") from e

        try:
            base_rows = analytics_conn.execute(f"DESCRIBE {quote_ident(row['name'])}").fetchall()
        except Exception:
            base_rows = []
        base_names = [r[0] for r in base_rows]
        probed_names = {c["name"] for c in probed_columns}
        columns = [{"name": name, "hidden": name not in probed_names} for name in base_names]
        for probed_col in probed_columns:
            if probed_col["name"] not in base_names:
                columns.append({"name": probed_col["name"], "hidden": False})

        try:
            referenced = _policy_preview_referenced_variables(policy_sql)
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail=f"policy_preview_failed: could not parse policy SQL: {exc}",
            ) from exc

        params: Dict[str, Any] = {}
        if "user_email" in referenced:
            params["user_email"] = persona_user_email
        if "user_id" in referenced:
            params["user_id"] = persona_user_id
        if "user_groups" in referenced:
            # An `as_groups` persona was already screened for pattern
            # metacharacters up in the persona branch. An `as_user`
            # persona's groups come from the DB, so nothing screened them
            # — yet the LIVE resolver (`src/access_policy.py::
            # policied_relation`) raises `PolicyError` for ANY bound group
            # name carrying one. Without this check, a preview of a user
            # in a group named e.g. `R&D%` renders a slice the product can
            # never serve that user: the preview succeeds, every real read
            # by them fails. Mirrored here exactly as the resolver does it
            # — only when the policy actually binds `$user_groups`, since
            # a policy that never references them serves that user fine.
            for group_name in persona_groups:
                if any(ch in group_name for ch in _POLICY_PREVIEW_PATTERN_METACHARACTERS):
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"policy_preview_unsafe_live_group_name: {group_name!r} is a live "
                            "group of this user and contains a pattern metacharacter (%, _), "
                            "which the policy resolver refuses to bind -- this policy can "
                            "never be served to this user as things stand; rename the group "
                            "before relying on the preview"
                        ),
                    )
            params["user_groups"] = persona_groups

        try:
            rows_total = analytics_conn.execute(f"SELECT COUNT(*) FROM {quote_ident(row['name'])}").fetchone()[0]
            rows_visible = analytics_conn.execute(
                f"SELECT COUNT(*) FROM ({policy_sql}) AS __agnes_policy_preview__",
                params,
            ).fetchone()[0]
            # Slice 2 (§13.1 before/after): the policied slice AND the RAW
            # sample the authoring admin (god-mode) may see, so the UI can
            # diff them — struck-through dropped rows, real->masked cells.
            # Both must cover the SAME bounded rows or the diff pairs
            # unrelated rows; `_policy_preview_samples` arranges that (and
            # says so via `comparable`) on ONE bounded read, so it never adds
            # to the two full COUNT(*) scans above, which are the pre-existing
            # per-call cost on a remote/BQ-backed table.
            sample_rows, base_sample_rows, base_sample_comparable = _policy_preview_samples(
                analytics_conn, row["name"], policy_sql, params
            )
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"policy_preview_failed: {exc}") from exc
    finally:
        analytics_conn.close()

    sample_rows = _sanitize_for_json(sample_rows)
    base_sample_rows = _sanitize_for_json(base_sample_rows)

    audit_repo().log(
        user_id=user.get("id"),
        action="access_policy.preview",
        resource=table_id,
        params=_sanitize_for_audit(
            {
                "as_user": request.as_user,
                "as_groups": request.as_groups,
                "candidate_sql": request.sql,
                "rows_visible": int(rows_visible),
                "rows_total": int(rows_total),
            }
        ),
    )

    return {
        "columns": columns,
        "sample_rows": sample_rows,
        "base_sample_rows": base_sample_rows,
        # False = the two samples are NOT guaranteed to cover the same rows,
        # so the UI must render the policied slice on its own rather than a
        # row-by-row before/after diff.
        "base_sample_comparable": bool(base_sample_comparable),
        "rows_visible": int(rows_visible),
        "rows_total": int(rows_total),
    }


def _policy_builder_describe(name: str) -> Optional[list]:
    """``DESCRIBE {name}`` on the read-only analytics connection -- the
    same query ``preview_table_policy`` already runs (line ~5054),
    factored out here because both new builder endpoints below need it:
    Task 2's columns list wants types too, Task 3's compile just wants the
    names. Never trusts a caller-supplied name -- every caller resolves
    ``name`` from the registry row first, never from the URL/body.

    ``None`` when the DESCRIBE itself failed, which is NOT the same as a table
    with no columns: this runs on a fresh read-only analytics connection where a
    ``query_mode='remote'`` view's external catalog is not re-ATTACHed, so a
    failure here is the ordinary outcome for exactly the remote rows a policy is
    most often written for. Callers that report "no columns" to a human must be
    able to tell the two apart.
    """
    from src.db import get_analytics_db_readonly
    from src.sql_ident import quote_ident

    analytics_conn = get_analytics_db_readonly()
    try:
        try:
            return analytics_conn.execute(f"DESCRIBE {quote_ident(name)}").fetchall()
        except Exception:
            logger.info("policy builder: DESCRIBE %s failed; schema unavailable", name, exc_info=True)
            return None
    finally:
        analytics_conn.close()


# Best-effort name hints for the builder's `pii` flag (plan Task 2) -- never
# authoritative, just a nudge toward masking a column an admin might not
# think to check.
_POLICY_BUILDER_PII_NAME_HINTS = (
    "email",
    "phone",
    "ssn",
    "national_id",
    "passport",
    "address",
    "birth",
    "dob",
    "credit_card",
    "iban",
    "ip_address",
    "first_name",
    "last_name",
    "full_name",
    "tax_id",
)


def _policy_builder_looks_like_pii(col_name: str, profile_col: dict) -> bool:
    """A name-substring match against common PII field names, OR the
    profiler's own ``"unique"`` alert on a non-numeric column (uniquely
    identifies every row -- a plausible identifier even when the name
    itself gives no hint)."""
    lname = col_name.lower()
    if any(hint in lname for hint in _POLICY_BUILDER_PII_NAME_HINTS):
        return True
    return (
        bool(profile_col)
        and "unique" in (profile_col.get("alerts") or [])
        and profile_col.get("type_category") != "NUMERIC"
    )


@router.get("/registry/{table_id}/policy/columns")
async def policy_builder_columns(
    table_id: str,
    user: dict = Depends(require_admin),
):
    """Real schema + sample values for the no-SQL policy builder (plan
    Task 2, access-policy-builder-ux) -- the column list a
    ``policy/compile`` spec (Task 3) is built from, so the builder UI
    never has to know a table's structure up front.

    Read-only and never gated on ``access_policies.enabled``: like
    ``preview_table_policy`` right above, this is an admin-authoring
    read/compute surface, not the ATTACH action that flag actually gates
    (``PUT /registry/{id}`` writing a non-null ``access_policy_sql``, per
    that flag's own hint text in ``_flag_default("access_policies", ...)``
    above).
    """
    row = table_registry_repo().get(table_id)
    if not row:
        raise HTTPException(status_code=404, detail="Table not found")

    name = row.get("name") or table_id
    eligible = row.get("query_mode") == "remote" or bool(row.get("server_only"))

    # `None` = the DESCRIBE failed (see the helper): the builder needs that
    # distinction to explain an empty list, instead of showing "No columns
    # found" for a table whose schema simply cannot be read from here and only
    # surfacing the real reason once a compile is attempted.
    described = _policy_builder_describe(name)
    base_rows = described or []

    # A profile may be keyed by the registry id or the table name depending
    # on when/how it was saved (mirrors `catalog.py::get_table_profile`'s own
    # `repo.get(table_name)` lookup -- profiles are keyed by whichever the
    # caller passed at save time, historically the name).
    profile = profile_repo().get(table_id) or profile_repo().get(name)
    profile_by_col = {c["name"]: c for c in (profile or {}).get("columns", []) if isinstance(c, dict)}

    columns = []
    for col_row in base_rows:
        col_name, col_type = col_row[0], col_row[1]
        prof_col = profile_by_col.get(col_name, {})
        columns.append(
            {
                "name": col_name,
                "type": col_type,
                "samples": [str(v) for v in (prof_col.get("sample_values") or [])],
                "distinct": prof_col.get("unique_count"),
                "pii": _policy_builder_looks_like_pii(col_name, prof_col),
            }
        )

    mapping_tables = [r["name"] for r in table_registry_repo().list_all() if r.get("policy_mapping") and r.get("name")]

    return {
        "columns": columns,
        "mapping_tables": mapping_tables,
        "eligible": eligible,
        "schema_available": described is not None,
    }


class PolicyCompileRequest(BaseModel):
    """Body for ``POST /registry/{table_id}/policy/compile`` (plan Task 3)
    -- the structured spec the builder UI assembles from Task 2's column
    list plus its own mask/row-rule pickers. Deliberately has NO ``table``
    field: the compiled SQL always names the REGISTRY row's own ``name``,
    resolved server-side, so a stale or tampered client can never smuggle
    a different table into the generated SQL.
    """

    row_rules: List[Dict[str, Any]] = Field(default_factory=list)
    row_combine: str = "and"
    column_masks: Dict[str, Any] = Field(default_factory=dict)


@router.post("/registry/{table_id}/policy/compile")
async def policy_builder_compile(
    table_id: str,
    request: PolicyCompileRequest,
    user: dict = Depends(require_admin),
):
    """Turn a structured builder spec into the canonical policy SQL (plan
    Task 3) via ``src.access_policy_compile.compile_policy`` -- the ONLY
    place that generator's anti-leak EXCLUDE-before-derive invariant is
    exercised over HTTP. Returns SQL only; nothing is persisted here -- an
    admin who likes the result still saves it through the existing ``PUT
    /registry/{id}`` (``access_policy_sql``), unchanged, so the stored
    artifact stays SQL, never a structured spec.

    Read-only and, like ``policy_builder_columns`` above, deliberately not
    gated on ``access_policies.enabled``: it neither reads nor writes a
    stored policy. The flag gates ATTACHING one (``PUT /registry/{id}``
    with a non-null ``access_policy_sql``).
    """
    row = table_registry_repo().get(table_id)
    if not row:
        raise HTTPException(status_code=404, detail="Table not found")

    name = row.get("name") or table_id
    describe_rows = _policy_builder_describe(name)
    if not describe_rows:
        raise HTTPException(
            status_code=422,
            detail="policy_builder_schema_unavailable: the table schema could not be read; "
            "ensure the table is materialized or remote before building a policy.",
        )
    columns = [{"name": c[0], "type": c[1]} for c in describe_rows]

    from src.access_policy_compile import compile_policy

    spec = {
        "table": name,
        "row_rules": request.row_rules,
        "row_combine": request.row_combine,
        "column_masks": request.column_masks,
    }
    try:
        compiled = compile_policy(spec, columns)
    except ValueError as e:
        # `compile_policy` raises a bare ValueError for a spec it cannot
        # understand -- an unknown row `op`, an unknown mask `choice`. That
        # is a malformed CLIENT payload (a stale builder, a hand-rolled
        # caller), not a server fault: surface it as a 4xx the builder can
        # render inline rather than letting it escape as a 500.
        raise HTTPException(status_code=422, detail=f"policy_compile_invalid_spec: {e}") from e
    return {"sql": compiled.sql, "warnings": compiled.warnings}


class _GotchaItem(BaseModel):
    """v56: a single gotcha entry. ``key=True`` marks the first one as
    the "Key gotcha" rendered distinctly by the package detail page."""

    key: bool = False
    body: str


class TableDocsRequest(BaseModel):
    """Per-table docs surface — v52 (sample_questions / things_to_know /
    pairs_well_with) extended in v56 with structured fields (grain /
    platforms / partition_col / history / gotchas) for the
    /catalog/p/<slug> package detail page rewrite.

    All fields optional. Sending `[]` for a list clears it; sending
    `""` for a scalar clears it; omitting leaves it untouched
    (Optional-is-no-op contract).
    """

    # v52 fields.
    sample_questions: Optional[List[str]] = None
    things_to_know: Optional[str] = None
    pairs_well_with: Optional[List[str]] = None
    # v56 fields.
    grain: Optional[str] = None
    platforms: Optional[List[str]] = None
    partition_col: Optional[str] = None
    history: Optional[str] = None
    gotchas: Optional[List[_GotchaItem]] = None

    @field_validator("platforms")
    @classmethod
    def _check_platforms(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return None
        if len(v) > 8:
            raise ValueError("platforms: max 8 entries")
        return v

    @field_validator("gotchas")
    @classmethod
    def _check_gotchas(cls, v):
        if v is None:
            return None
        if len(v) > 8:
            raise ValueError("gotchas: max 8 entries")
        return v


@router.patch("/registry/{table_id}/docs")
async def update_table_docs(
    table_id: str,
    payload: TableDocsRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Write the admin-authored per-table docs read by /catalog/t/<id>
    and (for the v56 structured fields) by the per-table extended
    section on /catalog/p/<slug>. Separated from PUT /registry/{id} so
    admins can flip these fields without re-submitting the whole big
    registration payload."""
    repo = table_registry_repo()
    if not repo.get(table_id):
        raise HTTPException(status_code=404, detail="table_not_found")
    # Empty-string ``things_to_know`` clears; explicit `[]` clears lists.
    clear_things = payload.things_to_know == ""
    clear_questions = payload.sample_questions == []
    clear_pairs = payload.pairs_well_with == []
    # v56 ``gotchas`` Pydantic models → list of dicts for the repo (JSON
    # serializer handles plain dicts; we'd lose the validator if we
    # passed _GotchaItem instances through).
    gotchas_payload = [g.model_dump() for g in payload.gotchas] if payload.gotchas is not None else None
    repo.update_docs(
        table_id,
        sample_questions=(None if clear_questions else payload.sample_questions),
        things_to_know=(None if clear_things else payload.things_to_know),
        pairs_well_with=(None if clear_pairs else payload.pairs_well_with),
        clear_sample_questions=clear_questions,
        clear_things_to_know=clear_things,
        clear_pairs_well_with=clear_pairs,
        # v56 — same Optional-is-no-op contract.
        grain=payload.grain,
        platforms=payload.platforms,
        partition_col=payload.partition_col,
        history=payload.history,
        gotchas=gotchas_payload,
    )
    # Echo the fresh state so the admin client can re-render without a
    # second GET. Lets the test suite (and the eventual admin UI) inspect
    # what landed in DB.
    fresh = repo.get(table_id) or {}
    return {
        "id": table_id,
        "sample_questions": fresh.get("sample_questions") or [],
        "things_to_know": fresh.get("things_to_know"),
        "pairs_well_with": fresh.get("pairs_well_with") or [],
        "grain": fresh.get("grain"),
        "platforms": fresh.get("platforms") or [],
        "partition_col": fresh.get("partition_col"),
        "history": fresh.get("history"),
        "gotchas": fresh.get("gotchas") or [],
    }


@router.delete("/registry/{table_id}", status_code=204)
async def unregister_table(
    table_id: str,
    background: BackgroundTasks,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Unregister a table from the system.

    For BQ rows, schedules a background rebuild so the dropped row's
    master view is removed from analytics.duckdb (rather than hanging
    around until the next scheduled sync).

    For materialized rows, also removes the canonical parquet at
    `${DATA_DIR}/extracts/<source_type>/data/<id>.parquet` and clears
    the matching `sync_state` row. Without these two cleanups, the
    manifest endpoint kept advertising the dropped table to `agnes pull`
    (sync_state-driven) and the orchestrator's next rebuild could
    resurrect a master view from the leftover parquet (E2E sub-agent
    finding 2026-05-01).
    """
    repo = table_registry_repo()
    existing = repo.get(table_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Table not found")

    was_bigquery = existing.get("source_type") == "bigquery"
    was_materialized = existing.get("query_mode") == "materialized"
    source_type = existing.get("source_type") or ""
    name = existing.get("name") or table_id

    repo.unregister(table_id)

    # Drop the canonical parquet for materialized rows. Path layout:
    # `${DATA_DIR}/extracts/<source_type>/data/<name>.parquet` — the
    # filename is keyed by `table_registry.name` (matches sync_state
    # bookkeeping convention; see _run_materialized_pass + the manifest
    # builder for the same name-keyed lookup). Defensively remove the
    # `.parquet.tmp` sibling too in case a prior materialize crashed
    # mid-COPY. Failure to remove (file missing, permission error) is
    # logged but doesn't fail the DELETE — the registry row is already
    # gone, and the orphan parquet will not produce a master view at
    # next rebuild because the orchestrator's _meta-driven scan never
    # picks up bare parquet files.
    if was_materialized and source_type in ("bigquery", "keboola"):
        try:
            data_dir = Path(os.environ.get("DATA_DIR", "./data"))
            base = data_dir / "extracts" / source_type / "data"
            # The publish temp is per-process since #1359
            # (`<name>.parquet.<pid>.tmp`, see src/parquet_publish.py), so a
            # single fixed `.parquet.tmp` no longer names anything a writer
            # produces. Both spellings are swept: the glob for temps this
            # build leaves behind, and the legacy fixed name for ones already
            # sitting on deployed volumes from before that change. Escaped
            # because the glob is built from a registry-supplied name.
            candidates = [
                base / f"{name}.parquet",
                base / f"{name}.parquet.tmp",
                *sorted(base.glob(f"{glob.escape(name)}.parquet.*.tmp")),
            ]
            for candidate in candidates:
                if candidate.exists():
                    candidate.unlink()
                    logger.info(
                        "Removed materialized parquet for unregistered table %s: %s",
                        table_id,
                        candidate,
                    )
        except Exception as e:
            logger.warning(
                "Failed to remove materialized parquet for %s: %s — registry row is "
                "still dropped; clean up the file manually if it lingers",
                table_id,
                e,
            )

    # Clear sync_state for any source/mode (a row that was synced at any
    # point — local/materialized — has a sync_state entry that the manifest
    # serves regardless of registry state). Pre-fix, the manifest still
    # advertised the dropped table to `agnes pull` because sync_state was
    # never cleaned up, and analysts kept getting it through the manifest.
    try:
        sync_state_repo().clear_for_table(name)
    except Exception as e:
        logger.warning(
            "Failed to clear sync_state for unregistered table %s: %s — "
            "manifest may still advertise the dropped row to agnes pull",
            table_id,
            e,
        )

    audit_repo().log(
        user_id=user.get("id"),
        action="unregister_table",
        resource=table_id,
        params=_sanitize_for_audit(
            {
                "name": existing.get("name"),
                "source_type": existing.get("source_type"),
                "bucket": existing.get("bucket"),
                "source_table": existing.get("source_table"),
            }
        ),
    )

    from app.api.v2_catalog import invalidate_for_table

    invalidate_for_table(table_id)

    if was_bigquery:
        _schedule_bq_materialize(background)


@router.post("/configure")
async def configure_instance(
    request: ConfigureRequest,
    user: dict = Depends(require_admin),
):
    """Configure data source and instance settings via API.

    Writes config to instance.yaml and persists secrets to .env_overlay.
    AI agents and the /setup wizard use this instead of manual file editing.
    """
    import yaml

    if request.data_source not in ("keboola", "bigquery", "local"):
        raise HTTPException(status_code=400, detail="data_source must be 'keboola', 'bigquery', or 'local'")

    # Validate credentials if provided
    if request.data_source == "keboola":
        if not request.keboola_token or not request.keboola_url:
            raise HTTPException(
                status_code=400, detail="keboola_token and keboola_url are required for Keboola data source"
            )
        _validate_url_not_private(request.keboola_url, field_name="keboola_url")
        try:
            from connectors.keboola.client import KeboolaClient

            client = KeboolaClient(token=request.keboola_token, url=request.keboola_url)
            client.test_connection()
        except Exception as e:
            logger.error("Keboola connection validation failed: %s", e)
            raise HTTPException(status_code=400, detail="Keboola connection failed. Check your token and URL.")

    elif request.data_source == "bigquery":
        if not request.bigquery_project:
            raise HTTPException(status_code=400, detail="bigquery_project is required for BigQuery data source")

    # Write instance.yaml to DATA_DIR/state/ (writable Docker volume),
    # NOT to CONFIG_DIR which is mounted read-only in Docker.
    #
    # Narrow-overlay write strategy — must match `/api/admin/server-config`:
    # 1. Read overlay verbatim (do NOT fall back to static). Falling back
    #    would copy env-resolved cleartext secrets from the merged static
    #    file back into the overlay (e.g. `smtp_password: ${SMTP_PASSWORD}`
    #    → `smtp_password: hunter2`). The wizard only ever sets
    #    `instance`, `auth`, `data_source` here, so other sections must
    #    flow from the static file via `load_instance_config`'s deep-merge
    #    — they don't belong in the overlay at all.
    # 2. Patch only the sections this endpoint touches.
    # 3. Write the narrow overlay back atomically (tmp + os.replace).
    from app.secrets import _state_dir

    config_path = _state_dir() / "instance.yaml"

    # Changed top-level overlay KEY NAMES only (never values) — the audit
    # row `instance.configure` reports at the end of this handler.
    changed_keys: set = set()

    # Same serialization + corrupt-overlay handling as POST /server-config.
    with _overlay_write_lock:
        overlay: dict = {}
        if config_path.exists():
            try:
                overlay = yaml.safe_load(config_path.read_text()) or {}
            except Exception as e:
                logger.exception("configure: refusing to overwrite corrupt overlay at %s", config_path)
                raise HTTPException(
                    status_code=500,
                    detail=f"refusing to overwrite corrupt overlay at {config_path} ({e}); "
                    "back up and remove the file, or fix it by hand",
                ) from e

        # Same repoint gate as POST /server-config. This endpoint writes the
        # very same `data_source.<source>` coordinates, so skipping it here
        # would leave a second, unguarded door to the same silent breakage
        # (the wizard is admin-callable long after first boot). Compared
        # against the EFFECTIVE config, not the overlay: a connection that
        # lives in the static instance.yaml is what registrations resolved
        # against, and reading only the overlay would score it as unset and
        # report a change where there is none.
        repoint_patch: Dict[str, Dict[str, Any]] = {}
        if request.data_source == "keboola":
            repoint_patch = {"keboola": {"stack_url": request.keboola_url, "token_env": "KEBOOLA_STORAGE_TOKEN"}}
        elif request.data_source == "bigquery":
            repoint_patch = {
                "bigquery": {
                    "project": request.bigquery_project,
                    "location": request.bigquery_location or "us",
                }
            }
        if repoint_patch:
            from app.instance_config import reset_cache as _reset_config_cache

            _reset_config_cache()
            _guard_connection_repoint(
                _load_current_instance_yaml(),
                {"data_source": repoint_patch},
                request.confirm_connection_change,
            )

        # Merge instance settings into the overlay only — never seed from the
        # env-resolved merged config.
        if request.instance_name:
            overlay.setdefault("instance", {})["name"] = request.instance_name
            changed_keys.add("instance")

        if request.allowed_domain:
            overlay.setdefault("auth", {})["allowed_domain"] = request.allowed_domain
            changed_keys.add("auth")

        # data_source.type is fully owned by this endpoint, but the REST of
        # the data_source block is not — an instance can already carry
        # sibling connection coordinates (data_source.snowflake/databricks/
        # ...) saved through /admin/server-config, and re-running first-time
        # setup must not silently drop them. Merge `type` into whatever is
        # already there instead of replacing the block wholesale.
        existing_data_source = overlay.get("data_source")
        if not isinstance(existing_data_source, dict):
            existing_data_source = {}
        existing_data_source["type"] = request.data_source
        overlay["data_source"] = existing_data_source
        changed_keys.add("data_source")
        if request.data_source == "keboola":
            overlay["data_source"]["keboola"] = {
                "stack_url": request.keboola_url,
                "token_env": "KEBOOLA_STORAGE_TOKEN",
            }
        elif request.data_source == "bigquery":
            overlay["data_source"]["bigquery"] = {
                "project": request.bigquery_project,
                "location": request.bigquery_location or "us",
            }

        # Seed an ai: block on first-time setup so LLM-driven services
        # (corporate_memory, verification_detector) can boot without manual
        # YAML editing. Only inserts when the overlay has no ai: yet AND an
        # appropriate env var is present — never overwrites operator config,
        # never writes a placeholder block (#176).
        if "ai" not in overlay:
            anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
            llm_key = os.environ.get("LLM_API_KEY", "").strip()
            if anthropic_key:
                overlay["ai"] = {
                    "provider": "anthropic",
                    "api_key": "${ANTHROPIC_API_KEY}",
                    "model": "claude-haiku-4-5-20251001",
                    "structured_output": "auto",
                }
                changed_keys.add("ai")
            elif llm_key:
                overlay["ai"] = {
                    "provider": "anthropic",
                    "api_key": "${LLM_API_KEY}",
                    "model": "claude-haiku-4-5-20251001",
                    "structured_output": "auto",
                }
                changed_keys.add("ai")

        # Atomic write to writable data volume — same tmp + os.replace pattern
        # as the server-config editor so a concurrent save can't tear the file.
        config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
        tmp_path.write_text(yaml.dump(overlay, default_flow_style=False, sort_keys=False))
        # 0600 before the rename — see the server-config editor above.
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, config_path)
        logger.info("Wrote instance config to %s", config_path)

    # Persist secrets to .env_overlay (in data volume, never in git)
    secrets_to_persist = {}
    if request.keboola_token:
        secrets_to_persist["KEBOOLA_STORAGE_TOKEN"] = request.keboola_token
    if request.keboola_url:
        secrets_to_persist["KEBOOLA_STACK_URL"] = request.keboola_url

    if secrets_to_persist:
        changed_keys.add("secrets")
        # SECURITY (#12): this path writes KEBOOLA_STORAGE_TOKEN to the plaintext
        # .env_overlay even when the Fernet vault is configured, bypassing
        # encryption-at-rest. Warn so it's visible; full fix (route datasource
        # tokens through system_secrets_repo()/persist_overlay_token, which needs
        # the token read-path audited so resolution doesn't break) is a tracked
        # follow-up. chmod 0o600 below limits exposure to same-uid readers only.
        from app.secrets_vault import vault_key_configured

        if request.keboola_token and vault_key_configured():
            logger.warning(
                "Persisting KEBOOLA_STORAGE_TOKEN to plaintext .env_overlay while "
                "AGNES_VAULT_KEY is configured — this bypasses the encrypted vault "
                "(tracked follow-up: route datasource tokens through the vault)."
            )

        # Resolve via _state_dir() so the path matches app/main.py's
        # startup-time read of the same overlay. Without this, an operator
        # on the flat-mount layout (STATE_DIR=/data-state) would write
        # secrets to /data/state/.env_overlay here while the app reads
        # from /data-state/.env_overlay — silent loss on next restart.
        from app.secrets import _state_dir

        overlay_path = _state_dir() / ".env_overlay"
        overlay_path.parent.mkdir(parents=True, exist_ok=True)

        # Merge with existing overlay
        existing_overlay = {}
        if overlay_path.exists():
            for line in overlay_path.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    existing_overlay[k.strip()] = v.strip()
        existing_overlay.update(secrets_to_persist)

        overlay_path.write_text("\n".join(f"{k}={v}" for k, v in existing_overlay.items()) + "\n")
        try:
            overlay_path.chmod(0o600)
        except OSError:
            pass
        logger.info("Persisted %d secrets to .env_overlay", len(secrets_to_persist))

        # Inject into current process environment
        for k, v in secrets_to_persist.items():
            os.environ[k] = v

    # Invalidate cached instance config so next read picks up changes.
    # Use the public helper (matches `/api/admin/server-config`); reaching
    # into the private global silently breaks if the cache layout changes.
    from app.instance_config import reset_cache

    reset_cache()

    # Changed key NAMES only — never values (secret-bearing keys/config
    # content never enters an audit record).
    log_safe(
        user_id=user.get("id"),
        action="instance.configure",
        resource="instance.yaml",
        params={"keys": sorted(changed_keys)},
    )

    return {
        "status": "ok",
        "data_source": request.data_source,
        "connection": "verified" if request.data_source != "local" else "local",
    }


def _split_keboola_table_id(full_id: str, fallback_name: str = "") -> tuple[str, str]:
    """Split a Keboola table id into ``(bucket, source_table)``.

    Keboola convention: ``<stage>.<bucket-id>.<table>`` where stage ∈
    ``{in, out, sys}`` and bucket-id typically starts with ``c-``
    (e.g. ``in.c-finance.orders``). Storage API export-async needs the
    FULL ``<stage>.<bucket-id>`` as the bucket arg — a stripped
    ``c-finance`` 404s. The 2-segment fallback covers id strings
    without the stage prefix; the 0/1-segment path returns empty
    bucket and uses ``fallback_name`` as the table name so the row
    fails loud at sync time rather than silently registering with
    no source coordinates.
    """
    parts = (full_id or "").strip().split(".")
    if len(parts) >= 3:
        return ".".join(parts[:-1]), parts[-1]
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", fallback_name or full_id


def _build_keboola_discovery_plan(
    conn: duckdb.DuckDBPyConnection,
    discovered: list[dict],
) -> dict:
    """Inspect ``discovered`` (output of ``KeboolaClient.discover_all_tables``)
    against the live registry and bucket every entry into one of:

      - ``new``: not in registry, will be inserted.
      - ``existing_match``: row already in registry under the same id
        AND its ``(bucket, source_table)`` matches what discovery would
        write — no-op, nothing to do.
      - ``existing_drift``: a row in the registry conflicts with what
        discovery would write. Two flavours, both surfaced for operator
        visibility but **never overwritten**:

          1. Same registry id, different ``(bucket, source_table)`` —
             admin corrected the coordinates inline (rarer).
          2. Different registry id but the discovered ``name`` clashes
             with an existing row's ``name`` (case-insensitive). Real
             example: registry has ``id='kbc_job', name='kbc_job',
             bucket='in.c-kbc_telemetry'``; Keboola exposes the same
             logical table at id ``in.c-keboola-storage.job`` (which
             slugs to a different ``table_id``). Without this
             check, auto-discovery would insert a duplicate ``kbc_job``
             whose Storage API export-async 404s.

      - ``invalid``: id couldn't produce a usable ``table_id`` slug.

    Each bucket carries the exact rows; the API endpoint composes a
    summary + (optionally) executes. Pre-fix, this logic was inlined
    in ``_discover_and_register_tables`` and there was no way to see
    what would change without writing.
    """
    repo = table_registry_repo()
    # Pre-load all keboola rows once so the name-collision lookup
    # below is O(1) per discovered entry. Falls back to per-id
    # `repo.get(...)` calls when list_all isn't available — keeps
    # the single-row test stubs working without forcing them to
    # implement list_all.
    try:
        registry_rows = repo.list_all()
    except AttributeError:
        registry_rows = []
    all_rows = [r for r in registry_rows if r.get("source_type") == "keboola"]
    by_name: dict[str, dict] = {(r.get("name") or "").strip().lower(): r for r in all_rows}
    # §3.2 — one pass over the policied rows, so the physical-source-twin
    # check below is an O(1) dict hit per discovered entry rather than a
    # fresh `list_all()` scan each time (discovery routinely walks
    # hundreds of tables). Same signal vocabulary as the register/update
    # interlocks, via `_policy_physical_source_signals`.
    policied_by_signal: dict = {}
    for row in registry_rows:
        if not row.get("access_policy_sql"):
            continue
        for signal in _policy_physical_source_signals(row):
            policied_by_signal.setdefault(signal, row)

    plan = {"new": [], "existing_match": [], "existing_drift": [], "invalid": []}
    for table in discovered:
        full_id = (table.get("id") or "").strip()
        # Slug used as the registry primary key. Lowercase, dots/spaces
        # → underscores. Stable across discovery runs.
        table_id = full_id.lower().replace(".", "_").replace(" ", "_")
        if not table_id:
            plan["invalid"].append(
                {
                    "table_id": "",
                    "full_id": full_id,
                    "reason": "empty id from discovery payload",
                }
            )
            continue

        # Prefer Keboola's authoritative `bucket_id` (separate field in
        # the API response, normalised by `discover_all_tables`) over
        # parsing the full id string. Fall back to the parser when
        # the API didn't return bucket_id (older fallback path inside
        # discover_all_tables).
        bucket = (table.get("bucket_id") or "").strip()
        name = (table.get("name") or "").strip()
        source_table = name
        if not bucket or not source_table:
            bucket, source_table = _split_keboola_table_id(full_id, source_table)

        entry = {
            "table_id": table_id,
            "name": table.get("name", table_id),
            "full_id": full_id,
            "bucket": bucket,
            "source_table": source_table,
        }

        existing = repo.get(table_id)
        if existing is not None:
            ex_bucket = existing.get("bucket") or ""
            ex_source_table = existing.get("source_table") or ""
            if ex_bucket == bucket and ex_source_table == source_table:
                plan["existing_match"].append(entry)
            else:
                plan["existing_drift"].append(
                    {
                        **entry,
                        "registry_bucket": ex_bucket,
                        "registry_source_table": ex_source_table,
                        "registry_id": existing.get("id"),
                        "drift_kind": "same_id_diff_coords",
                    }
                )
            continue

        # No row at this id. Look for a name collision (admin
        # registered the same logical table under a different id).
        name_match = by_name.get(name.lower()) if name else None
        if name_match is not None:
            plan["existing_drift"].append(
                {
                    **entry,
                    "registry_bucket": name_match.get("bucket") or "",
                    "registry_source_table": name_match.get("source_table") or "",
                    "registry_id": name_match.get("id"),
                    "drift_kind": "name_collision",
                }
            )
            continue

        # §3.2 — the physical-source twin. Everything the writer takes out
        # of `new` is registered as `query_mode='materialized'` with no
        # `server_only`: the distributable shape `agnes pull` downloads,
        # and one whose next sync tick writes the raw rows to parquet with
        # no further admin action. The id/name checks above do NOT cover
        # this: a policied row that was renamed, or registered under a
        # hand-picked id, matches neither — so discovery would insert a
        # twin of it and route the policy around in bulk. Classify it
        # `invalid` (rather than raising) so one governed table never
        # aborts a whole discovery run, and the operator sees in the
        # dry-run exactly which row blocked it and why.
        my_signals = _policy_physical_source_signals(
            {
                "source_type": "keboola",
                "bucket": bucket,
                "source_table": source_table,
            }
        )
        policied_twin = next(
            (policied_by_signal[s] for s in my_signals if s in policied_by_signal),
            None,
        )
        if policied_twin is not None:
            plan["invalid"].append(
                {
                    **entry,
                    "reason": (
                        "access_policy_physical_source_conflict: this source is already "
                        f"registered as {policied_twin.get('id')!r} "
                        f"({policied_twin.get('name')!r}) with an access policy attached -- "
                        "auto-discovery would register a copy with no policy of its own, "
                        "which routes the policy around; if you need a second row, register "
                        "it by hand and attach a policy to it in the same breath, or point "
                        "it at a different physical source"
                    ),
                }
            )
            continue

        plan["new"].append(entry)
    return plan


def _discover_and_register_tables(
    conn: duckdb.DuckDBPyConnection,
    user_email: str,
    *,
    dry_run: bool = False,
) -> dict:
    """Discover tables from configured source and register them.

    Behavior:
      - Only the configured source type ``keboola`` is supported here
        (BigQuery uses a different discovery endpoint).
      - Already-registered rows are NEVER overwritten. The plan
        classifies them as ``existing_match`` (no-op, registry agrees
        with discovery) or ``existing_drift`` (admin edited the
        coordinates; left alone, surfaced in the response so the
        operator sees the divergence).
      - ``dry_run=True`` returns the plan without writing anything —
        useful for auditing before a re-discovery on a registry that
        already has admin overrides.
    """
    from app.instance_config import get_data_source_type, get_value

    source_type = get_data_source_type()
    if source_type != "keboola":
        return {
            "registered": 0,
            "skipped": 0,
            "errors": 0,
            "drifted": 0,
            "tables": [],
            "source": source_type,
            "dry_run": dry_run,
        }

    from connectors.keboola.client import KeboolaClient
    from app.datasource_secrets import keboola_instance_token

    # Read from data_source.keboola (matches what /api/admin/configure writes)
    url = get_value("data_source", "keboola", "stack_url", default="")
    token_env = get_value("data_source", "keboola", "token_env", default="KEBOOLA_STORAGE_TOKEN")
    token, _provenance = keboola_instance_token(token_env)

    client = KeboolaClient(token=token or "", url=url)
    discovered = client.discover_all_tables()

    plan = _build_keboola_discovery_plan(conn, discovered)
    drift_summary = [
        {
            "table_id": e["table_id"],
            "discovery": {"bucket": e["bucket"], "source_table": e["source_table"]},
            "registry": {"bucket": e["registry_bucket"], "source_table": e["registry_source_table"]},
        }
        for e in plan["existing_drift"]
    ]

    if dry_run:
        return {
            "registered": 0,
            "skipped": len(plan["existing_match"]),
            "errors": len(plan["invalid"]),
            "drifted": len(plan["existing_drift"]),
            "tables": [e["table_id"] for e in plan["new"]],
            "would_register": [e["table_id"] for e in plan["new"]],
            "drift": drift_summary,
            "invalid": plan["invalid"],
            "source": "keboola",
            "dry_run": True,
        }

    repo = table_registry_repo()
    registered = 0
    errors = 0
    table_names = []

    for entry in plan["new"]:
        try:
            repo.register(
                id=entry["table_id"],
                name=entry["name"],
                source_type="keboola",
                bucket=entry["bucket"],
                source_table=entry["source_table"],
                # Keboola goes through Storage API export-async via the
                # materialized path (NULL source_query = full table). The
                # legacy `local` mode for Keboola was retired in v26 and
                # would no-op here anyway.
                query_mode="materialized",
                registered_by=user_email,
                description=f"Auto-discovered from Keboola: {entry['full_id']}",
            )
            registered += 1
            table_names.append(entry["table_id"])
        except Exception as e:
            logger.warning("Failed to register %s: %s", entry["table_id"], e)
            errors += 1

    if plan["existing_drift"]:
        logger.warning(
            "Auto-discover skipped %d row(s) where the admin-edited "
            "bucket/source_table differs from discovery — preserving "
            "the admin values. Run with dry_run=True to see the deltas.",
            len(plan["existing_drift"]),
        )

    return {
        "registered": registered,
        "skipped": len(plan["existing_match"]),
        "errors": errors + len(plan["invalid"]),
        "drifted": len(plan["existing_drift"]),
        "tables": table_names,
        "drift": drift_summary,
        "invalid": plan["invalid"],
        "source": "keboola",
        "dry_run": False,
    }


@router.post("/discover-and-register")
async def discover_and_register(
    dry_run: bool = False,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Discover tables from configured source and auto-register them.

    Combines discover-tables + register-table into one call. Already-
    registered rows are NEVER overwritten — admin edits to bucket /
    source_table win. The response surfaces a ``drift`` array listing
    any rows where discovery would have written different coordinates
    than what's in the registry, so operators can audit divergence
    after a Keboola-side bucket rename / table move.

    Query params:
      - ``dry_run=true`` returns the plan without writing anything.
        Lists ``would_register``, ``drift``, and ``invalid`` so an
        operator can decide whether to proceed (or, in the drift case,
        which side they want to fix).

    Used by /setup wizard and AI agents.
    """
    try:
        result = _discover_and_register_tables(
            conn,
            user.get("email", "admin"),
            dry_run=dry_run,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Discovery and registration failed: {e}")


# ---------------------------------------------------------------------------
# Scheduler-driven LLM pipeline endpoints (#176)
#
# The scheduler container drives these via HTTP rather than running them
# in-process — same reasoning as the existing /api/marketplaces/sync-all
# job: DuckDB allows only one writer per file across processes, and the
# app keeps a long-lived handle on system.duckdb. Routing through the app
# inherits the existing connection without contention.
#
# Each endpoint is `def` (sync), so FastAPI runs it in a thread pool —
# the underlying jobs do blocking I/O (LLM calls, DuckDB writes,
# filesystem scans). Running on the asyncio thread would block health
# checks for the duration of a job.
# ---------------------------------------------------------------------------


@router.post("/run-session-collector")
def run_session_collector(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Trigger the session-collector job from the scheduler.

    Walks /home/*/user/sessions/*.jsonl and copies new files into
    /data/user_sessions/<user>/. Idempotent — already-collected files
    are skipped.
    """
    from services.session_collector import collector

    # Call run() not main(): main() does argparse.parse_args() which would
    # try to parse uvicorn's sys.argv and SystemExit(2) the worker.
    rc: int = 1
    stats: dict = {}
    job_error: Optional[Exception] = None
    try:
        rc, stats = collector.run(dry_run=False, verbose=False)
    except Exception as e:
        # Mirror run_verification_detector / run_corporate_memory
        # (#179 review): capture any unhandled error so audit_log +
        # /admin/scheduler-runs reflect the failure. Re-raised below
        # after audit. Filesystem permission, OSError on /home walking,
        # etc. are realistic failure modes worth surfacing.
        job_error = e

    audit_params: dict = {"rc": rc, **stats}
    if job_error is not None:
        audit_params["unhandled_error"] = f"{type(job_error).__name__}: {job_error}"

    audit_repo().log(
        user_id=user.get("id"),
        client_kind=client_kind_from_user(user),
        action="run_session_collector",
        resource="job:session-collector",
        params=audit_params,
    )

    if job_error is not None:
        raise HTTPException(status_code=500, detail=audit_params["unhandled_error"])

    return {"ok": rc == 0, "details": {"rc": rc, **stats}}


@router.post("/run-session-processor")
def run_session_processor(
    processor: str = Query(..., description="Processor name (e.g. 'verification', 'usage')"),
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Trigger one session-pipeline processor against /data/user_sessions/*.

    Replaces the per-processor /run-* endpoints with a single parametrized
    entry. The scheduler invokes this once per registered processor on its
    own cadence; processors are independent (one slow / failing processor
    can't block any other).

    Returns 400 if `processor` is unknown. The verification processor
    requires an LLM extractor — if the instance has no ai: config and no
    ANTHROPIC_API_KEY / LLM_API_KEY, it won't appear in the registry and
    the call returns 400 the same as a misspelled name.
    """
    from services.session_pipeline.runner import run_processor as _run_processor
    from services.session_processors import get_processor, list_processor_names
    from src.db import get_system_db
    from src.repositories import use_pg

    proc = get_processor(processor)
    if proc is None:
        raise HTTPException(
            status_code=400,
            detail=(f"Unknown processor '{processor}'. Known: {', '.join(list_processor_names())}"),
        )

    # Reject overlapping invocations of the same processor (PR #232 review).
    # See `_get_processor_run_lock` docstring for why this matters
    # (verification_evidence row duplication on race).
    proc_lock = _get_processor_run_lock(processor)
    if not proc_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail=f"Processor '{processor}' is already running",
        )

    # run_processor / the processors resolve their state through the repository
    # factory and ignore ``conn``; on Postgres pass None so the system DuckDB is
    # never opened (forbidden invariant).
    job_conn = None if use_pg() else get_system_db()
    # This attempt-count cap exists to bound wall-clock/CPU from processors
    # that make synchronous, blocking LLM calls per session (verification).
    # "usage" is pure local jsonl parsing + repository writes — no network
    # I/O — so it stays exempt from THIS cap (Devin Review, PR #894: capping
    # it too would just throttle telemetry-extraction throughput, draining a
    # bulk backfill/onboarding wave at cap-size-per-tick instead of clearing
    # it in one run). Default-capped for any other/future processor
    # (safe-by-default).
    #
    # CORRECTION (incident 2026-07-20): the #894 claim that exempting usage
    # from a per-tick cap has "no safety benefit" was wrong — an uncapped
    # usage run against a large onboarding-wave backlog held the
    # request-serving process for ~6 minutes (app-wide 503s), because an
    # attempt-count cap was never the only lever. run_processor() now also
    # enforces a per-tick WALL-CLOCK time budget
    # (services/session_pipeline/runner.py, time_budget_seconds, default
    # 150s) that applies to every processor regardless of this exemption —
    # that budget is the load-bearing guard against a repeat of this
    # incident, so usage stays safely exempt from the attempt-count cap
    # without being unbounded in wall-clock terms. Do not re-exempt usage
    # from the time budget too.
    session_processor_cap = None if processor == "usage" else _session_processor_max_per_run()
    stats: dict = {}
    job_error: Optional[Exception] = None
    try:
        stats = _run_processor(job_conn, proc, max_sessions_per_run=session_processor_cap)
        # Rebuild daily rollups after a successful usage run so the
        # marketplace / admin dashboards see fresh aggregates. Backend-aware
        # (#728 — the free-function DuckDB-only producer left rollups
        # permanently empty on Postgres); incremental (last-7-days) so it's
        # cheap. Kept here (not in runner.py) to stay processor-agnostic at
        # the framework level.
        if processor == "usage" and stats.get("errors", 0) == 0:
            from datetime import datetime, timedelta, timezone

            try:
                since_day = (datetime.now(timezone.utc) - timedelta(days=7)).date()
                usage_repo().rebuild_rollups(since_day=since_day)
            except Exception as rollup_exc:
                logger.warning("usage rollup rebuild failed: %s", rollup_exc)
    except Exception as e:
        # Capture and re-raise after audit so an unhandled runner error
        # (DuckDB lock, network blip, unexpected SDK type) still leaves a
        # row in audit_log — the /admin/scheduler-runs page is the
        # operator's only signal beyond docker logs.
        job_error = e
    finally:
        if job_conn is not None:
            try:
                job_conn.close()
            except Exception:
                pass
        # Always release, even if the runner raised. A leaked lock would
        # wedge the processor permanently until process restart.
        proc_lock.release()

    audit_params: dict = {
        "processor": processor,
        "scanned": stats.get("scanned", 0),
        "processed": stats.get("processed", 0),
        "skipped": stats.get("skipped", 0),
        "capped": stats.get("capped", 0),
        "errors": stats.get("errors", 0),
        "items_extracted": stats.get("items_extracted", 0),
    }
    if job_error is not None:
        audit_params["unhandled_error"] = f"{type(job_error).__name__}: {job_error}"

    audit_repo().log(
        user_id=user.get("id"),
        client_kind=client_kind_from_user(user),
        action=f"run_session_processor:{processor}",
        resource=f"job:session-processor:{processor}",
        params=audit_params,
    )

    if job_error is not None:
        raise HTTPException(status_code=500, detail=audit_params["unhandled_error"])

    return {"ok": stats.get("errors", 0) == 0, "details": stats}


@router.post("/run-corporate-memory")
def run_corporate_memory(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Trigger the corporate-memory catalog refresh from the scheduler.

    Reads all CLAUDE.local.md files, sends them through the LLM with the
    existing catalog, and writes an updated catalog to knowledge.json.
    """
    from services.corporate_memory.collector import collect_all

    # Fail-fast (#176): collect_all raises ValueError when no ai: block AND
    # no env keys are present. Surface the actionable factory message in a
    # 500 instead of letting it crash the request anonymously.
    stats: dict = {}
    job_error: Optional[Exception] = None
    try:
        stats = collect_all(dry_run=False)
    except ValueError as e:
        # Already-translated misconfiguration → 500 with actionable message
        # but no audit row (the request never reached the LLM stage).
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        # Mirror run_verification_detector (#179 review): capture any other
        # unhandled error so audit_log + /admin/scheduler-runs reflect the
        # failure. Re-raised below after audit.
        job_error = e

    audit_params: dict = {
        "items_new": stats.get("items_new", 0),
        "items_filtered": stats.get("items_filtered", 0),
        "items_duplicate_skipped": stats.get("items_duplicate_skipped", 0),
        "items_db_inserted": stats.get("items_db_inserted", 0),
        "items_db_updated": stats.get("items_db_updated", 0),
        "items_db_errors": stats.get("items_db_errors", 0),
        "errors": len(stats.get("errors", [])),
        "skipped": stats.get("skipped", False),
    }
    if job_error is not None:
        audit_params["unhandled_error"] = f"{type(job_error).__name__}: {job_error}"

    audit_repo().log(
        user_id=user.get("id"),
        client_kind=client_kind_from_user(user),
        action="run_corporate_memory",
        resource="job:corporate-memory",
        params=audit_params,
    )

    if job_error is not None:
        raise HTTPException(status_code=500, detail=audit_params["unhandled_error"])

    return {
        "ok": not stats.get("errors") and stats.get("items_db_errors", 0) == 0,
        "details": stats,
    }


@router.post("/run-knowledge-packaging")
def run_knowledge_packaging(
    user: dict = Depends(require_admin),
):
    """Rebuild per-collection knowledge.duckdb artifacts whose content changed.

    Scheduler-driven (K3, #798): fingerprints each corpus's chunks, rebuilds
    stale artifacts, prunes artifacts for deleted corpora. Idempotent and
    cheap when nothing changed (fingerprint check only). Mirrors
    run_corporate_memory's audit + error posture.
    """
    from src.knowledge_packaging import run_packaging_pass

    job_error: Optional[Exception] = None
    summary: dict = {}
    try:
        summary = run_packaging_pass()
    except Exception as e:
        # Mirror run_corporate_memory / run_verification_detector: capture
        # any unhandled error so audit_log + /admin/scheduler-runs reflect
        # the failure. Re-raised below after audit.
        job_error = e

    audit_params: dict = {
        "built": len(summary.get("built", [])),
        "skipped": len(summary.get("skipped", [])),
        "pruned": len(summary.get("pruned", [])),
        "errors": len(summary.get("errors", [])),
    }
    if job_error is not None:
        audit_params["unhandled_error"] = f"{type(job_error).__name__}: {job_error}"

    audit_repo().log(
        user_id=user.get("id"),
        client_kind=client_kind_from_user(user),
        action="run_knowledge_packaging",
        resource="job:knowledge-packaging",
        params=audit_params,
    )

    if job_error is not None:
        raise HTTPException(status_code=500, detail=audit_params["unhandled_error"])

    return {"ok": not summary.get("errors"), "details": summary}


@router.post("/run-knowledge-digests")
def run_knowledge_digests(
    user: dict = Depends(require_admin),
):
    """Regenerate maintained digests whose source fingerprint changed.

    Scheduler-driven (K4, #799): fingerprints each digest's instructions +
    source corpora, regenerates via LLM only when the fingerprint changed
    (concurrency 1, budget/timeout-capped). Failures keep the previous
    markdown and mark the digest visibly stale. Mirrors
    run_knowledge_packaging's audit + error posture.
    """
    from src.knowledge_digests import run_digest_pass

    job_error: Optional[Exception] = None
    summary: dict = {}
    try:
        summary = run_digest_pass()
    except Exception as e:
        # Mirror run_knowledge_packaging / run_corporate_memory: capture any
        # unhandled error so audit_log + /admin/scheduler-runs reflect the
        # failure. Re-raised below after audit.
        job_error = e

    audit_params: dict = {
        "generated": len(summary.get("generated", [])),
        "skipped": len(summary.get("skipped", [])),
        "stale": len(summary.get("stale", [])),
        "errors": len(summary.get("errors", [])),
    }
    if job_error is not None:
        audit_params["unhandled_error"] = f"{type(job_error).__name__}: {job_error}"

    audit_repo().log(
        user_id=user.get("id"),
        client_kind=client_kind_from_user(user),
        action="run_knowledge_digests",
        resource="job:knowledge-digests",
        params=audit_params,
    )

    if job_error is not None:
        raise HTTPException(status_code=500, detail=audit_params["unhandled_error"])

    return {"ok": not summary.get("errors"), "details": summary}


@router.post("/run-knowledge-migration")
def run_knowledge_migration(
    user: dict = Depends(require_admin),
):
    """Retroactively import knowledge items from knowledge.json into the DB.

    One-time migration for instances where collect_all() ran before v0.71.60
    (which added the DB sync step). Idempotent — items already in the DB are
    skipped. Remove this endpoint after all instances have been migrated.
    """
    data_dir = Path(os.environ.get("DATA_DIR", "./data"))
    knowledge_file = data_dir / "corporate-memory" / "knowledge.json"

    try:
        knowledge_data = json.loads(knowledge_file.read_text())
    except FileNotFoundError:
        knowledge_data = []
    except (json.JSONDecodeError, OSError) as e:
        raise HTTPException(status_code=500, detail=f"Failed to read knowledge.json: {e}")

    if isinstance(knowledge_data, dict) and "items" in knowledge_data:
        knowledge_data = list(knowledge_data["items"].values())
    elif not isinstance(knowledge_data, list):
        raise HTTPException(status_code=500, detail="knowledge.json has unexpected format")

    count = 0
    repo = knowledge_repo()
    for item in knowledge_data:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id", "")
        if not item_id:
            continue
        if repo.get_by_id(item_id):
            continue
        # Validate domain slug before INSERT to avoid a partial commit: create()
        # does the INSERT first, then resolves the slug; if the slug is unknown
        # it raises ValueError after the row is already committed, leaving an
        # orphaned item that can never be re-migrated. Resolve upfront instead.
        domain_slug = item.get("domain")
        if domain_slug and not repo._resolve_domain_slug(domain_slug):
            domain_slug = None
        repo.create(
            id=item_id,
            title=item.get("title", ""),
            content=item.get("content", ""),
            category=item.get("category", ""),
            source_user=item.get("source_user"),
            tags=item.get("tags"),
            status=item.get("status", "pending"),
            confidence=item.get("confidence"),
            domain=domain_slug,
            entities=item.get("entities"),
            source_type=item.get("source_type", "claude_local_md"),
            source_ref=item.get("source_ref"),
            sensitivity=item.get("sensitivity", "internal"),
            is_personal=item.get("is_personal", False),
        )
        count += 1

    audit_repo().log(
        user_id=user.get("id"),
        client_kind=client_kind_from_user(user),
        action="run_knowledge_migration",
        resource="job:knowledge-migration",
        params={"knowledge_imported": count},
    )

    return {"ok": True, "knowledge_imported": count}


# ---------------------------------------------------------------------------
# Jira self-healing endpoints — driven by the scheduler
#
# Parity with the legacy Data Broker ``jira-sla-poll.timer`` /
# ``jira-consistency.timer`` systemd units, but invoked from the in-cluster
# scheduler container instead of host systemd. Both endpoints
# short-circuit with ``{"status": "skipped", "reason":
# "jira_not_configured"}`` when the ``JIRA_*`` env vars are unset — a
# customer without Jira ingest pays nothing for the default scheduler
# entries.
# ---------------------------------------------------------------------------


@router.post("/run-jira-sla-poll")
def run_jira_sla_poll(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Refresh SLA + status fields for open Jira tickets.

    Webhooks update tickets on activity. Tickets that sit idle for hours
    don't fire webhooks; their ``elapsed_millis`` ages out, and SLA
    breaches go undetected. This job polls every open ticket with SLA
    data and re-fetches the live values from the Jira API. Self-heals
    stale ``status`` / ``resolution`` fields on the same pass — tickets
    closed during a webhook outage get corrected.

    Cadence: every 45 min by default (SCHEDULER_JIRA_SLA_POLL_INTERVAL) —
    long enough that a serial pass over every open ticket finishes before
    the next one is due; see the DEFAULTS comment in services/scheduler.
    Skipped gracefully when JIRA_SLA_* env vars aren't set.
    """
    from connectors.jira.scripts.poll_sla import run as _run_poll_sla

    stats: dict = {}
    job_error: Optional[Exception] = None
    try:
        stats = _run_poll_sla(dry_run=False)
    except ValueError as e:
        # Raised by load_config() when JIRA_SLA_* env vars are missing.
        # Scheduler-driven endpoints prefer a 200 skip over a 500 — the
        # operator sees the no-op in audit_log without alert noise.
        audit_repo().log(
            user_id=user.get("id"),
            client_kind=client_kind_from_user(user),
            action="run_jira_sla_poll",
            resource="job:jira-sla-poll",
            params={"status": "skipped", "reason": str(e)[:200]},
        )
        return {"status": "skipped", "reason": "jira_not_configured", "detail": str(e)}
    except Exception as e:
        # Mirror run_corporate_memory: capture any other unhandled error so
        # audit_log + /admin/scheduler-runs reflect the failure. Re-raised
        # below after the audit row is written. Without this branch, a
        # network timeout / DuckDB lock / JSON-parse failure here would
        # 500 silently — operator wouldn't see the failure in the
        # scheduler-runs surface.
        job_error = e

    audit_params: dict = {
        "open_issues": stats.get("open_issues", 0),
        "updated": stats.get("updated", 0),
        "healed": stats.get("healed", 0),
        "skipped": stats.get("skipped", 0),
        "failed": stats.get("failed", 0),
        "elapsed_sec": round(float(stats.get("elapsed_sec", 0.0)), 2),
    }
    if job_error is not None:
        audit_params["unhandled_error"] = f"{type(job_error).__name__}: {job_error}"

    audit_repo().log(
        user_id=user.get("id"),
        client_kind=client_kind_from_user(user),
        action="run_jira_sla_poll",
        resource="job:jira-sla-poll",
        params=audit_params,
    )

    if job_error is not None:
        raise HTTPException(status_code=500, detail=audit_params["unhandled_error"])

    return {"ok": stats.get("failed", 0) == 0, "details": stats}


@router.post("/run-jira-consistency-check")
def run_jira_consistency_check(
    max_age_days: int = 30,
    auto_fix: bool = True,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Validate Jira parquet against the Jira API and backfill small gaps.

    Compares three sources: Jira API (ground truth), raw JSON, and
    parquet. Auto-fixes small webhook-loss gaps (≤10 issues); larger
    gaps are reported and require manual review.

    Cadence: every 30 min by default (SCHEDULER_JIRA_CONSISTENCY_INTERVAL).
    Default ``max_age_days=30`` keeps the routine cost bounded; an
    operator triggering a deeper validation passes a larger value.
    """
    from connectors.jira.scripts.consistency_check import (
        Config,
        JiraConsistencyChecker,
    )

    try:
        config = Config.from_env()
    except (ValueError, KeyError) as e:
        audit_repo().log(
            user_id=user.get("id"),
            client_kind=client_kind_from_user(user),
            action="run_jira_consistency_check",
            resource="job:jira-consistency-check",
            params={"status": "skipped", "reason": str(e)[:200]},
        )
        return {"status": "skipped", "reason": "jira_not_configured", "detail": str(e)}

    report: dict = {}
    job_error: Optional[Exception] = None
    try:
        checker = JiraConsistencyChecker(config)
        report = checker.run_check(
            max_age_days=max_age_days,
            auto_fix=auto_fix,
            dry_run=False,
        )
    except Exception as e:
        # Mirror run_corporate_memory: capture any unhandled error so
        # audit_log + /admin/scheduler-runs reflect the failure. Re-raised
        # below after the audit row is written. Without this branch a Jira
        # API outage or DuckDB lock during the parquet compare would 500
        # silently — operator wouldn't see the failure in scheduler-runs.
        job_error = e

    audit_params: dict = {
        "max_age_days": max_age_days,
        "auto_fix": auto_fix,
        "status": report.get("status"),
        "alert_level": report.get("alert_level"),
    }
    if job_error is not None:
        audit_params["unhandled_error"] = f"{type(job_error).__name__}: {job_error}"

    audit_repo().log(
        user_id=user.get("id"),
        client_kind=client_kind_from_user(user),
        action="run_jira_consistency_check",
        resource="job:jira-consistency-check",
        params=audit_params,
    )

    if job_error is not None:
        raise HTTPException(status_code=500, detail=audit_params["unhandled_error"])

    ok = report.get("status") == "success" and report.get("alert_level") != "ERROR"
    return {"ok": ok, "details": report}


# ---------------------------------------------------------------------------
# Flea-market guardrails — admin endpoints
#
# Backs /admin/store/submissions (the human triage page) and the override /
# retry / delete-submission action buttons. Every action here writes an
# audit_log row so the trail of "who force-published what, and why" is
# permanent — same governance posture as the corporate-memory + scheduler
# runs surfaces.
# ---------------------------------------------------------------------------

import shutil as _shutil  # noqa: E402


@router.get("/store/submissions")
async def admin_list_store_submissions(
    status: Optional[str] = None,
    submitter: Optional[str] = None,
    type: Optional[str] = None,  # noqa: A002 — FastAPI query-param name
    name: Optional[str] = None,
    version: Optional[str] = None,
    sort: Optional[str] = None,
    order: Optional[str] = None,
    limit: int = 100,
    skip: int = 0,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List flea-market guardrail submissions newest-first.

    All filters AND together. ``status`` is comma-separated
    (e.g. ``blocked_inline,blocked_llm``). ``submitter`` matches
    ``submitter_id`` exactly. ``type`` is one of ``skill`` / ``agent`` /
    ``plugin``. ``name`` and ``version`` are case-insensitive substrings.
    ``limit`` clamped to [1, 500].
    """

    statuses = None
    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
    if type and type not in {"skill", "agent", "plugin"}:
        raise HTTPException(status_code=400, detail="invalid_type")
    limit = max(1, min(int(limit), 500))
    skip = max(0, int(skip))

    # v36+ chip routing: 'archived' / 'deleted' tokens in ?status=
    # are LIFECYCLE filters, not verdict filters. The repo handles the
    # JOIN-on-entity logic for archived; submission terminal marker
    # for deleted. Verdict tokens (approved, blocked_*, pending_*,
    # overridden, review_error) pass through unchanged.
    lifecycle = None
    if statuses == ["archived"]:
        lifecycle = "archived"
        statuses = None
    elif statuses == ["deleted"]:
        lifecycle = "deleted"
        statuses = None

    try:
        items, total = store_submissions_repo().list_for_admin(
            status=statuses,
            submitter_id=submitter or None,
            type_=type or None,
            name_substr=name or None,
            version_substr=version or None,
            sort_by=sort or None,
            sort_order=order or None,
            lifecycle=lifecycle,
            limit=limit,
            skip=skip,
        )
    except ValueError as e:
        # Sort key whitelist rejection (#23) — surface as 400 so the UI
        # can show the operator a meaningful message instead of 500.
        msg = str(e)
        if msg.startswith("invalid_sort_key"):
            raise HTTPException(status_code=400, detail="invalid_sort_key")
        raise
    return {"items": items, "total": total, "limit": limit, "skip": skip}


@router.get("/store/submissions/{submission_id}")
async def admin_get_store_submission(
    submission_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    sub = store_submissions_repo().get(submission_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="submission_not_found")
    return sub


class _OverrideRequest(BaseModel):
    reason: str = Field(..., min_length=4, max_length=2000)


@router.post("/store/submissions/{submission_id}/override")
async def admin_override_store_submission(
    submission_id: str,
    body: _OverrideRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Force-publish a previously-blocked submission.

    Flips the submission to ``status='overridden'`` and the linked
    store_entities row to ``visibility_status='approved'``. Audit row
    captures who, why, and the verdict that was overridden so the next
    time this submission shows up, the trail is intact.
    """

    subs = store_submissions_repo()
    sub = subs.get(submission_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="submission_not_found")
    if sub["status"] not in {"blocked_inline", "blocked_llm", "review_error", "pending_llm"}:
        raise HTTPException(
            status_code=409,
            detail=f"cannot_override_status:{sub['status']}",
        )

    entity_id = sub.get("entity_id")
    if not entity_id:
        # v30+ ought to always carry entity_id. Legacy rows from the
        # pre-v30 inline-rollback design land here — refuse with a
        # message that points at the only path forward (Delete +
        # ask submitter to re-upload).
        raise HTTPException(
            status_code=409,
            detail="cannot_override_legacy_without_entity",
        )

    subs.set_override(submission_id, admin_user_id=user["id"], reason=body.reason)
    ents_repo = store_entities_repo()
    ents_repo.set_visibility(entity_id, "approved")

    # Mirror the runner's deferred-promotion path. An override on a
    # v2+ edit/restore must promote the overridden version + swap the
    # on-disk live bundle, otherwise the entity stays at the prior
    # approved version and installers keep receiving stale bytes the
    # admin just told us to replace. For an initial v1 submission
    # (no prior approved) the version_no already matches — the loop
    # just no-ops and we skip promotion harmlessly.
    entity_row = ents_repo.get(entity_id) or {}
    promoted_to: Optional[int] = None
    # Look up THIS submission's version entry by submission_id, NOT
    # by hash. Hash-based lookup breaks when the user re-uploads
    # byte-identical bundles (e.g. v2 same content as v1): the loop
    # picks the FIRST history entry with that hash (always v1, n=1),
    # so target_version_no lands at 1 instead of the actual new
    # entry's n. The forward-only `target > current` guard then
    # skips the promote, leaving the entity stuck at v1. Surfaced
    # live on a development deployment.
    from app.api.store import _version_no_for_submission

    target_version_no: Optional[int] = _version_no_for_submission(
        entity_row,
        submission_id,
    )
    # Forward-only: refuse to promote backwards. An admin overriding a
    # stale v2 submission when v3 is already approved + live must NOT
    # demote the live bundle back to v2's bytes. Override flips the
    # row's status + visibility regardless; only the version-promote
    # is gated. Forward (target > current) is the only motion the
    # publish-gate model is designed to express.
    if target_version_no is not None and target_version_no > int(entity_row.get("version_no") or 0):
        # Atomic helper: swap live bundle first, then update the DB.
        # Eliminates the "DB promoted but live still on prior bytes"
        # window. If the helper returns None (source missing / swap
        # failed) the row's status + visibility are still flipped
        # above — admin can re-trigger via /rescan once the bundle
        # is recovered.
        from app.api.store import promote_to_version

        promoted_to = promote_to_version(
            entity_id,
            target_version_no,
            ents_repo,
        )
        if promoted_to is not None:
            # Re-read after promotion so attribution picks up the
            # new version's name/type if a rename was bundled in.
            entity_row = ents_repo.get(entity_id) or entity_row

    # v46: attribution lookup is live — the next UsageProcessor tick
    # preloads the newly-approved entity by name.

    audit_repo().log(
        user_id=user["id"],
        action="store.submission.overridden",
        resource=f"store_submission:{submission_id}",
        params={
            "entity_id": entity_id,
            "reason": body.reason,
            "prior_status": sub["status"],
            "prior_findings": sub.get("llm_findings"),
            "prior_inline": sub.get("inline_checks"),
            "promoted_to_version_no": promoted_to,
        },
        result="success",
    )

    from app.api.store import _notify_submitter

    _notify_submitter(sub, decision="overridden", note=body.reason)
    return {"ok": True, "submission_id": submission_id, "entity_id": entity_id}


@router.post("/store/submissions/{submission_id}/rescan")
async def admin_rescan_store_submission(
    submission_id: str,
    background: BackgroundTasks,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Re-run **all** guardrail checks (inline + LLM) against the current
    bundle.

    Different from ``/retry``: rescan starts from scratch (re-runs the
    deterministic inline checks too) and is allowed regardless of
    current status. Use when check rules have changed and a previously-
    approved entity might now fail (or vice versa).

    Effects:
      * inline checks run sync; verdict written to ``inline_checks``
      * on inline fail → ``status='blocked_inline'``, entity hidden
      * on inline pass → ``status='pending_llm'``, LLM call scheduled,
        entity visibility flipped to ``pending`` until verdict lands
      * audit_log entry recorded for both outcomes — admin sees the
        rescan in the detail-page activity timeline
      * audit row recorded

    Requires the bundle to still be on disk. Inline-blocked submissions
    whose bundle was rolled back (no ``entity_id``) cannot be rescanned —
    nothing to scan.
    """
    from app.api.store import (
        _plugin_dir,
        _submission_plugin_dir,
        _version_no_for_submission,
    )
    from app.instance_config import (
        get_guardrails_enabled,
        get_guardrails_llm_provider_ready,
    )
    from src.db import get_system_db
    from src.store_guardrails import run_inline_checks
    from src.store_guardrails.runner import (
        default_api_key_loader,
        default_model_loader,
        run_llm_review,
    )

    subs = store_submissions_repo()
    sub = subs.get(submission_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="submission_not_found")
    entity_id = sub.get("entity_id")
    if not entity_id:
        raise HTTPException(status_code=409, detail="cannot_rescan_without_entity")

    ents = store_entities_repo()
    entity = ents.get(entity_id)
    # Rescan the bundle this submission represents — not live. See the
    # equivalent fix in /retry for the full reasoning. Same fall-back
    # to live for legacy rows that never seeded a versions/v<N>/plugin/.
    target_n = _version_no_for_submission(entity or {}, submission_id)
    if target_n is not None:
        plugin_dir = _submission_plugin_dir(entity_id, target_n)
        if not plugin_dir.exists():
            plugin_dir = _plugin_dir(entity_id)
    else:
        plugin_dir = _plugin_dir(entity_id)
    if not plugin_dir.exists():
        raise HTTPException(status_code=410, detail="bundle_missing")

    description = (entity or {}).get("description")

    inline = run_inline_checks(
        plugin_dir,
        type_=sub["type"],
        description=description,
    )

    if not inline.passed:
        # Re-failed inline. Hide the entity (was approved or pending);
        # admin can either fix the bundle (PUT to recreate) or override.
        subs.set_inline_result(
            submission_id,
            inline_checks=inline.to_response_dict(),
            status="blocked_inline",
        )
        ents.set_visibility(entity_id, "hidden")
        audit_repo().log(
            user_id=user["id"],
            action="store.submission.rescan",
            resource=f"store_submission:{submission_id}",
            params={"entity_id": entity_id, "outcome": "blocked_inline"},
        )
        return {"ok": True, "submission_id": submission_id, "status": "blocked_inline"}

    # Inline passes. Three-state matrix:
    #   - intent False           → auto-approve (operator opt-out)
    #   - intent True + ready    → pending_llm, schedule LLM
    #   - intent True + not-ready → pending_llm, DO NOT schedule (admin
    #     retries from the same endpoint after providing credentials)
    guardrails_enabled = get_guardrails_enabled()
    provider_ready = get_guardrails_llm_provider_ready()
    hold_for_review = guardrails_enabled
    schedule_async_llm = guardrails_enabled and provider_ready
    guardrails_on = hold_for_review  # retained for audit-log compat
    new_status = "pending_llm" if hold_for_review else "approved"
    subs.set_inline_result(
        submission_id,
        inline_checks=inline.to_response_dict(),
        status=new_status,
    )
    if hold_for_review:
        ents.set_visibility(entity_id, "pending")
    else:
        ents.set_visibility(entity_id, "approved")
        # Guardrails explicitly disabled — immediately live. Promote
        # the rescanned submission's version forward (same atomic
        # helper the create / update / restore inline-promote paths
        # use). Pre-fix this branch flipped visibility but never
        # called promote_to_version, so a rescan that re-approved a
        # non-current v2+ left the entity stuck at the prior version.
        # Surfaced by adversarial review of PR #330.
        from app.api.store import promote_to_version

        entity_row = ents.get(entity_id) or {}
        if target_n is not None and target_n > int(entity_row.get("version_no") or 0):
            promote_to_version(entity_id, target_n, ents)
        # v46: attribution lookup is live — no explicit refresh needed.
    audit_repo().log(
        user_id=user["id"],
        action="store.submission.rescan",
        resource=f"store_submission:{submission_id}",
        params={
            "entity_id": entity_id,
            "outcome": new_status,
            "guardrails_enabled": guardrails_on,
            "provider_ready": provider_ready,
        },
    )
    if schedule_async_llm:
        background.add_task(
            run_llm_review,
            submission_id,
            plugin_dir=plugin_dir,
            conn_factory=get_system_db,
            api_key_loader=default_api_key_loader,
            model_loader=default_model_loader,
        )
    return {"ok": True, "submission_id": submission_id, "status": new_status}


@router.post("/store/submissions/{submission_id}/retry")
async def admin_retry_store_submission(
    submission_id: str,
    background: BackgroundTasks,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Re-queue the LLM review for a submission.

    Eligible statuses:
      * ``review_error`` — LLM call failed, admin retrying after the
        underlying issue (rate limit, timeout, transient outage) clears.
      * ``blocked_llm`` — admin disagrees with the prior verdict; rerun
        from a clean slate (review rules may have shifted since).
      * ``pending_llm`` — submission was held when the LLM provider had
        no credentials in env (fail-CLOSED matrix: intent True + not
        ready). Admin sets the key and re-fires from here.

    Only valid when the original submission's plugin tree is still on
    disk — for inline-blocked rows the bundle was deleted at POST time.
    """
    from app.api.store import (
        _plugin_dir,
        _submission_plugin_dir,
        _version_no_for_submission,
    )
    from src.db import get_system_db
    from src.store_guardrails.runner import (
        default_api_key_loader,
        default_model_loader,
        run_llm_review,
    )

    subs = store_submissions_repo()
    sub = subs.get(submission_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="submission_not_found")
    if sub["status"] not in {"review_error", "blocked_llm", "pending_llm"}:
        raise HTTPException(
            status_code=409,
            detail=f"cannot_retry_status:{sub['status']}",
        )
    entity_id = sub.get("entity_id")
    if not entity_id:
        raise HTTPException(
            status_code=409,
            detail="cannot_retry_without_entity",
        )

    # Review the STAGED version's bytes — not live. For a v2+ edit
    # held at pending_llm or blocked_llm, live `plugin/` still holds
    # the prior approved version. Reviewing live would produce a
    # verdict against the wrong bytes; the runner's hash-match
    # promotion would then advance the entity to staged bytes that
    # were never actually reviewed.
    ent = store_entities_repo().get(entity_id) or {}
    target_n = _version_no_for_submission(ent, submission_id)
    if target_n is not None:
        plugin_dir = _submission_plugin_dir(entity_id, target_n)
        # Fall back to live for legacy pre-v37 rows where the version
        # dir was never seeded.
        if not plugin_dir.exists():
            plugin_dir = _plugin_dir(entity_id)
    else:
        plugin_dir = _plugin_dir(entity_id)
    if not plugin_dir.exists():
        raise HTTPException(status_code=410, detail="bundle_missing")

    subs.update_status(submission_id, status="pending_llm")
    audit_repo().log(
        user_id=user["id"],
        action="store.submission.retry",
        resource=f"store_submission:{submission_id}",
        params={"entity_id": entity_id},
    )
    background.add_task(
        run_llm_review,
        submission_id,
        plugin_dir=plugin_dir,
        conn_factory=get_system_db,
        api_key_loader=default_api_key_loader,
        model_loader=default_model_loader,
    )
    return {"ok": True, "submission_id": submission_id, "status": "pending_llm"}


@router.delete("/store/submissions/{submission_id}", status_code=204)
async def admin_delete_store_submission(
    submission_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Hard-delete a submission record + its linked bundle (if any).

    Use this for spam / accidental uploads after override-publish is the
    wrong call. The audit_log row preserves what was deleted in case
    triage needs the evidence trail later.
    """
    from app.api.store import _entity_dir, _notify_submitter

    subs = store_submissions_repo()
    sub = subs.get(submission_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="submission_not_found")

    entity_id = sub.get("entity_id")
    if entity_id:
        user_store_installs_repo().delete_all_for_entity(entity_id)
        store_entities_repo().delete(entity_id)
        _shutil.rmtree(_entity_dir(entity_id), ignore_errors=True)
    subs.delete(submission_id)

    audit_repo().log(
        user_id=user["id"],
        action="store.submission.deleted",
        resource=f"store_submission:{submission_id}",
        params={
            "entity_id": entity_id,
            "submitter_id": sub.get("submitter_id"),
            "name": sub.get("name"),
            "status": sub.get("status"),
        },
    )

    _notify_submitter(sub, decision="deleted")


# ---------------------------------------------------------------------------
# v30: download blocked bundle for forensic inspection
# ---------------------------------------------------------------------------

from fastapi.responses import StreamingResponse  # noqa: E402


@router.get("/store/submissions/{submission_id}/bundle.zip")
async def admin_download_store_submission_bundle(
    submission_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Stream the on-disk bundle as a fresh ZIP for admin inspection.

    Required by the forensic use case: admin needs to inspect what a
    submitter actually tried to upload (not just the verdict). Bundle
    must still be on disk — TTL purge nulls ``entity_id`` and removes
    the directory, in which case this returns 410.
    """
    import io as _io
    import zipfile as _zipfile
    from pathlib import Path as _P

    from app.api.store import (
        _plugin_dir as _sp_plugin_dir,
    )
    from app.api.store import (
        _submission_plugin_dir,
        _version_no_for_submission,
    )

    sub = store_submissions_repo().get(submission_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="submission_not_found")
    entity_id = sub.get("entity_id")
    if not entity_id:
        raise HTTPException(status_code=410, detail="bundle_purged_or_missing")

    # Resolve the STAGED bundle this submission represents, not live.
    # Under deferred promotion, live `plugin/` holds the prior approved
    # version — so for a blocked v2 row, live shows v1's safe bytes
    # while the staged v2 bytes (the actual risky upload the admin is
    # reviewing) sit in `versions/v2/plugin/`. Falls back to live for
    # legacy rows that never seeded a versions/ dir.
    ent = store_entities_repo().get(entity_id) or {}
    target_n = _version_no_for_submission(ent, submission_id)
    if target_n is not None:
        plugin_dir = _submission_plugin_dir(entity_id, target_n)
        if not plugin_dir.exists():
            plugin_dir = _sp_plugin_dir(entity_id)
    else:
        plugin_dir = _sp_plugin_dir(entity_id)
    if not plugin_dir.exists():
        raise HTTPException(status_code=410, detail="bundle_missing")

    audit_repo().log(
        user_id=user["id"],
        action="store.submission.bundle_downloaded",
        resource=f"store_submission:{submission_id}",
        params={"entity_id": entity_id, "name": sub.get("name")},
    )

    buf = _io.BytesIO()
    with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(_P(plugin_dir).rglob("*")):
            if not f.is_file():
                continue
            arcname = f.relative_to(plugin_dir).as_posix()
            zf.write(f, arcname)
    buf.seek(0)

    safe_name = (sub.get("name") or "bundle").replace("/", "_")
    filename = f"{safe_name}-{submission_id[:8]}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# v30: scheduled TTL purge of blocked bundle bytes
# ---------------------------------------------------------------------------


@router.post("/run-blocked-purge")
async def run_blocked_purge(
    user: dict = Depends(require_admin),
):
    """Trigger the TTL purge of blocked bundle bytes.

    Wraps :func:`src.store_guardrails.purge.purge_blocked_bundles`. The
    scheduler service hits this endpoint daily (under
    ``SCHEDULER_API_TOKEN`` like the corporate-memory + verification
    jobs); admins can also run it on demand from the UI.

    ``purge_blocked_bundles`` resolves its repositories from the
    ``src.repositories`` factory (backend-agnostic), so no DuckDB
    connection is threaded through this handler anymore.
    """
    from app.instance_config import get_guardrails_blocked_bundle_ttl_days
    from src.store_guardrails.purge import purge_blocked_bundles

    ttl = get_guardrails_blocked_bundle_ttl_days()
    result = purge_blocked_bundles(ttl_days=ttl)

    audit_repo().log(
        user_id=user.get("id"),
        client_kind=client_kind_from_user(user),
        action="run_blocked_purge",
        resource="job:store-blocked-purge",
        params={"ttl_days": ttl, "purged": result.get("purged", 0), "skipped": result.get("skipped", False)},
    )
    return {"ok": True, "details": result}


# ---------------------------------------------------------------------------
# B8: scheduled retention pruning of audit_log
# ---------------------------------------------------------------------------


@router.post("/run-audit-prune")
async def run_audit_prune(
    user: dict = Depends(require_admin),
):
    """Trigger the retention-based ``audit_log`` prune.

    Wraps :func:`src.audit_retention.prune_audit_log`. The scheduler service
    hits this endpoint daily (under ``SCHEDULER_API_TOKEN`` like the
    corporate-memory + blocked-purge jobs); admins can also run it on demand.

    ``retention_days`` comes from ``audit.retention_days`` (default 365, 0
    keeps rows forever). Only ``audit_log`` has a retention policy — see
    docs/observability.md for the other audit/observability trails.
    """
    from app.instance_config import get_audit_retention_days
    from src.audit_retention import prune_audit_log

    retention_days = get_audit_retention_days()
    result = prune_audit_log(retention_days=retention_days)

    audit_repo().log(
        user_id=user.get("id"),
        client_kind=client_kind_from_user(user),
        action="run_audit_prune",
        resource="job:audit-prune",
        params={"retention_days": retention_days, **result},
    )
    return {"ok": True, "details": result}


# ---------------------------------------------------------------------------
# Track E3 Slice 1: scheduled retention pruning of the other unbounded
# audit/activity trails (sync_history, llm_usage, agent_scope_snapshots)
# ---------------------------------------------------------------------------


@router.post("/run-retention-prune")
async def run_retention_prune(
    user: dict = Depends(require_admin),
):
    """Trigger the opt-in retention sweep for ``sync_history``,
    ``llm_usage``, and ``agent_scope_snapshots``.

    Wraps :func:`src.audit_retention.run_retention_sweep`. The scheduler
    service hits this endpoint daily (under ``SCHEDULER_API_TOKEN``, same as
    ``run-audit-prune``); admins can also run it on demand.

    Every trail's window comes from ``retention.<trail>_days`` in
    ``instance.yaml`` and defaults to 0 (keep forever) — nothing is pruned
    until an admin explicitly sets a window. ``audit_log`` has its own
    standalone job (``run-audit-prune`` above, unchanged); ``usage_events``
    keeps its own standalone job (``POST /api/admin/usage/prune``) — see
    ``src/audit_retention.py``'s module docstring for why those two aren't
    folded into this sweep.
    """
    from app.instance_config import (
        get_agent_scope_snapshots_retention_days,
        get_llm_usage_retention_days,
        get_sync_history_retention_days,
    )
    from src.audit_retention import run_retention_sweep

    windows = {
        "sync_history": get_sync_history_retention_days(),
        "llm_usage": get_llm_usage_retention_days(),
        "agent_scope_snapshots": get_agent_scope_snapshots_retention_days(),
    }
    result = run_retention_sweep(windows)

    audit_repo().log(
        user_id=user.get("id"),
        client_kind=client_kind_from_user(user),
        action="run_retention_prune",
        resource="job:retention-prune",
        params={"windows": windows, **result},
    )
    return {"ok": True, "details": result}


@router.post("/run-reap-stuck-reviews")
async def run_reap_stuck_reviews(
    user: dict = Depends(require_admin),
):
    """Trigger the stuck-review reaper.

    Wraps :func:`src.store_guardrails.reaper.reap_stuck_llm_reviews`.
    The scheduler hits this every 15 minutes; admins can run it on
    demand if a worker crash is suspected. Flips any
    ``status='pending_llm'`` row older than the configured grace to
    ``review_error`` so the queue stops growing indefinitely.

    No DuckDB ``conn`` dependency: the reaper resolves the submissions
    repo from the factory so it flips rows on whichever backend holds
    them. Injecting a DuckDB ``conn`` here was the bug that made the
    reaper a silent no-op on Postgres-backed instances — the rows live
    in PG, the conn pointed at an empty local DuckDB.
    """
    from app.instance_config import get_guardrails_stuck_review_grace_seconds
    from src.store_guardrails.reaper import reap_stuck_llm_reviews

    grace = get_guardrails_stuck_review_grace_seconds()
    result = reap_stuck_llm_reviews(grace_seconds=grace)

    audit_repo().log(
        user_id=user.get("id"),
        client_kind=client_kind_from_user(user),
        action="run_reap_stuck_reviews",
        resource="job:store-reap-stuck-reviews",
        params={"grace_seconds": grace, "reaped": result.get("reaped", 0), "skipped": result.get("skipped", False)},
    )
    return {"ok": True, "details": result}
