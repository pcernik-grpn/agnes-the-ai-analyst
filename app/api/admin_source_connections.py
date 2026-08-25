"""Admin REST API for source connections (named multi-project data-source connections).

Surface (all gated by ``Depends(require_admin)``):

  GET    /api/admin/source-connections              — list (?source_type=)
  POST   /api/admin/source-connections              — create; 409 on duplicate name.
                                                       Keboola + ``seed_from_instance_credentials=true``:
                                                       copy the instance-level Keboola token into this
                                                       new connection's own vault slot, unless that token
                                                       resolves under the EXACT ``token_env`` name this
                                                       new row inherited (only then does the row's own
                                                       resolver already find it unaided) — see
                                                       ``app.datasource_secrets.keboola_instance_token``.
                                                       Response gains ``token_seeded``/``token_seed_error``
                                                       when this ran and something was actually seeded.
  GET    /api/admin/source-connections/{id}         — detail; 404 if missing
  PUT    /api/admin/source-connections/{id}         — update config / token_env; 404 if missing
  DELETE /api/admin/source-connections/{id}         — delete; 404 if missing
  PUT    /api/admin/source-connections/{id}/secret  — store vault secret (kind=storage|master
                                                       in body); 409 if AGNES_VAULT_KEY missing;
                                                       kind=master is keboola-only and validated
                                                       live via a verify_token preflight
  DELETE /api/admin/source-connections/{id}/secret  — clear vault secret (?kind=storage|master)
  POST   /api/admin/source-connections/{id}/test    — verify connectivity; timeout 10s
  GET    /api/admin/source-connections/{id}/tables  — list buckets/tables for the "add data
                                                       source" wizard; keboola only, REST-only
                                                       admin-UI helper (see _EXEMPT classification
                                                       in tests/test_documentation_api_triple_surface.py)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import requests
from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.auth.access import require_admin
from app.keboola_identity import project_identity
from app.secrets_vault import VaultKeyNotConfiguredError, can_store_secrets
from connectors.keboola.semantic_layer import MasterTokenRequiredError, require_master_token
from connectors.keboola.storage_api import KeboolaStorageClient, StorageApiError, is_upstream_client_error
from src.keboola_chat_tools import (
    build_stdio_spec,
    merge_env,
    derived_source_id,
    derived_tool_id,
    exposed_tool_name,
)
from src.repositories import (
    connection_secrets_repo,
    mcp_sources_repo,
    per_user_secrets_repo,
    shared_secrets_repo,
    source_connections_repo,
    table_registry_repo,
    tool_registry_repo,
)
from src.repositories.tool_registry import PASSTHROUGH

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/source-connections", tags=["admin"])

# Ceiling on the chat-tools introspection dial-out. Generous because the first
# run downloads the upstream server (~157 MB through `uv`), but finite so a
# stalled download turns into the endpoint's 502-with-retry-hint instead of an
# admin request that never returns.
CHAT_TOOLS_INTROSPECT_TIMEOUT_S = 300.0

# The exact keyword surface of the repos' `upsert`s. A rollback replays rows it
# read with `get`/`list_for_source`, and those carry `created_at`/`updated_at`
# too, which the upserts do not accept.
_MCP_SOURCE_UPSERT_FIELDS = (
    "id",
    "name",
    "transport",
    "command",
    "args",
    "env",
    "url",
    "auth_method",
    "auth_secret_env",
    "enabled",
    "scope",
    "connect_hint",
)
_TOOL_UPSERT_FIELDS = (
    "tool_id",
    "source_id",
    "original_name",
    "exposed_name",
    "mode",
    "table_id",
    "input_schema",
    "description",
    "mutating",
    "pii_fields",
    "rate_limit_pm",
    "schedule",
    "enabled",
)


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


class CreateConnectionBody(BaseModel):
    name: str
    source_type: str
    config: Dict[str, Any]
    token_env: Optional[str] = None
    is_default: bool = False
    # Keboola-only, opt-in: seed the new connection's own vault slot from
    # the instance-level credential unless that credential is resolvable
    # under the EXACT token_env name this new row inherited (see
    # `_seed_keboola_instance_credential`). Set by the derived Keboola card's
    # "Import as managed connection" button — every other caller (the "Add
    # data source" wizard, the CLI, third-party API clients) leaves this
    # False and gets exactly today's behavior.
    seed_from_instance_credentials: bool = False


class UpdateConnectionBody(BaseModel):
    # `name` supports the "Add data source" wizard's rename-after-test step
    # (#755): the project name is only known once `POST .../test` succeeds,
    # which requires the row to already exist.
    name: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    token_env: Optional[str] = None
    is_default: Optional[bool] = None


class SecretBody(BaseModel):
    value: str
    kind: str = "storage"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def master_secret_key(connection_id: str) -> str:
    """Vault key for a connection's Keboola master (owner) Storage API token —
    a separate slot from the plain storage token (see ``SecretBody.kind``).
    Shared with the semantic-layer sync (which requires a master token)."""
    return f"{connection_id}:master"


def _log_host(stack_url: str) -> str:
    """Hostname of a connection's stack_url for log lines (never the token)."""
    return urlsplit(stack_url).hostname or stack_url


def _with_secret_status(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Annotate a connection row with ``has_secret``/``has_master_secret``
    (whether a vault secret is stored under each slot).

    The token's storage location isn't derivable from ``token_env`` alone —
    vault secrets live in the separate ``connection_secrets`` store. The UI
    badge needs this to distinguish "vault" from "env"/"unset".
    """
    if row is None:
        return None
    try:
        row["has_secret"] = bool(connection_secrets_repo().has(row["id"]))
    except Exception:
        row["has_secret"] = False
    try:
        row["has_master_secret"] = bool(connection_secrets_repo().has(master_secret_key(row["id"])))
    except Exception:
        row["has_master_secret"] = False
    try:
        # The switch must read what the source DOES, not merely that a row
        # exists. Both `enable_chat_tools` and `_resync_derived_chat_tools`
        # deliberately carry a previously-set `enabled=False` over, so a
        # disabled source kept showing "on" — and turning the switch off then
        # on again is exactly how an admin would try to fix that, which does
        # nothing because the row was there all along. (Devin Review.)
        derived = mcp_sources_repo().get(derived_source_id(row["id"]))
        row["has_chat_tools"] = bool(derived) and derived.get("enabled", True) is not False
        # The page needs the source id to grant the whole set in one call.
        # Served rather than recomputed in JS: `derived_source_id` is server
        # logic, and a second copy of it in the template is a rename away from
        # pointing the grant at a source that does not exist.
        row["chat_tools_source_id"] = derived["id"] if derived else None
    except Exception:
        row["has_chat_tools"] = False
        row["chat_tools_source_id"] = None
    return row


def _workspace_schema_of(config: Optional[Dict[str, Any]]) -> Optional[str]:
    """The connection's ``workspace_schema``, or None when unset.

    ``config`` is admin-supplied free-form JSON, so the value can be any
    type; only a non-empty string counts. Anything else is treated as unset
    rather than letting enable/re-sync crash on it. (Devin Review on this PR.)
    """
    raw = (config or {}).get("workspace_schema")
    if not isinstance(raw, str):
        return None
    return raw.strip() or None


def _resolve_token(connection_id: str, row: Dict[str, Any]) -> Optional[str]:
    """Resolve the storage token for a connection: vault secret first, then
    the ``token_env`` environment-variable fallback. Shared by ``/test`` and
    ``/tables`` so both endpoints treat "how do I get the token" identically.
    """
    token: Optional[str] = None
    try:
        secrets = connection_secrets_repo()
        if secrets.has(connection_id):
            token = secrets.get(connection_id)
    except Exception as exc:
        logger.debug("vault lookup failed for %s: %s", connection_id, exc)

    if not token:
        token_env = row.get("token_env") or ""
        if token_env:
            # SECURITY: only read env vars on the remote-attach allowlist. Without
            # this, an admin could set token_env=JWT_SECRET_KEY (or DATABASE_URL,
            # ANTHROPIC_API_KEY, …) and exfiltrate that server-process secret via
            # the outbound X-StorageApi-Token header in /test and /tables. Enforced
            # here (validate-at-use) as well as at create/update, so a row written
            # before this guard existed still cannot leak an off-allowlist env var.
            from src.orchestrator_security import is_token_env_allowed

            if is_token_env_allowed(token_env):
                token = os.environ.get(token_env, "")
            else:
                logger.warning(
                    "connection %s: token_env %r is not on the remote-attach "
                    "allowlist; refusing to read it (add it to "
                    "AGNES_REMOTE_ATTACH_TOKEN_ENVS or use a vault secret)",
                    connection_id,
                    token_env,
                )
    return token or None


def _reject_disallowed_token_env(token_env: Optional[str]) -> None:
    """Reject a token_env that isn't on the remote-attach allowlist (409-style
    400). None/empty is allowed — vault-secret connections don't use token_env.
    Called on create/update so a bad name never lands in the row."""
    if not token_env:
        return
    from src.orchestrator_security import is_token_env_allowed

    if not is_token_env_allowed(token_env):
        raise HTTPException(
            status_code=400,
            detail=(
                f"token_env {token_env!r} is not allowlisted. Use a Keboola storage-"
                "token env var (or add the name to AGNES_REMOTE_ATTACH_TOKEN_ENVS), "
                "or store the token in the vault via PUT .../secret instead."
            ),
        )


def _record_project_identity(connection_id: str, row: Dict[str, Any], payload: Dict[str, Any]) -> None:
    """Persist the upstream project's id + name onto the connection config.

    A connection is one Keboola project, but nothing used to record WHICH —
    so an instance with several projects showed several identical-looking
    connections and a "master token: SET" badge that said nothing about
    which project the token actually opened. Recording the identity at every
    point a token verifies is what makes :func:`_reject_project_mismatch`
    possible at all.
    """
    project_id, project_name = project_identity(payload)
    if project_id is None:
        return
    config = dict(row.get("config") or {})
    known_id = config.get("project_id")
    if known_id is not None and str(known_id) != str(project_id):
        # NEVER silently re-bind. Callers are expected to detect the
        # disagreement and report it, but this is the backstop: a caller that
        # forgets would rewrite the binding under a stored master token that
        # still matches the ORIGINAL project, and that token then starts
        # failing a mismatch check nobody triggered (Devin Review on #1242).
        logger.warning(
            "connection %s is bound to Keboola project %s but a token for project %s verified; "
            "leaving the binding alone",
            connection_id,
            known_id,
            project_id,
        )
        return
    if known_id == project_id and config.get("project_name") == project_name:
        return
    config["project_id"] = project_id
    config["project_name"] = project_name
    source_connections_repo().update(connection_id, config=config)


def project_mismatch_message(row: Dict[str, Any], payload: Dict[str, Any], *, what: str) -> Optional[str]:
    """Why this token disagrees with the connection's recorded project, or
    ``None`` when there is no disagreement.

    The failure this closes: a master token pasted onto the wrong connection
    was stored happily and badged "SET", and the semantic layer then synced
    a *different* project's metrics under this connection's name — with no
    surface anywhere showing the two tokens disagreed. Silent, and only
    visible as metrics that make no sense for the project you thought you
    were looking at.

    Returns ``None`` when the connection has no recorded identity yet
    (nothing to contradict — the caller records it instead).

    Separate from the raising wrapper because ``/test`` reports failures as
    ``{"ok": false, "error": …}`` rather than an HTTP error, and it must be
    able to say the same thing in its own shape.
    """
    config = row.get("config") or {}
    known_id = config.get("project_id")
    if known_id is None:
        return None
    token_id, token_name = project_identity(payload)
    # Compared as strings: the id round-trips through a JSON config column on
    # two different backends (DuckDB JSON, PG JSONB), and a 12345 that comes
    # back as "12345" would otherwise read as a permanent, unfixable mismatch
    # on a correctly-configured connection. Devin Review on #1242 raised the
    # same risk across endpoints.
    if token_id is None or str(token_id) == str(known_id):
        return None
    known_name = config.get("project_name") or "unnamed"
    return (
        f"project_mismatch: this {what} belongs to Keboola project "
        f"{token_id} ({token_name or 'unnamed'}), but the connection is bound to project "
        f"{known_id} ({known_name}). Use a token from that project, or create a separate "
        f"connection for project {token_id}."
    )


def _reject_project_mismatch(row: Dict[str, Any], payload: Dict[str, Any], *, what: str) -> None:
    """400 if this token opens a different Keboola project than the one the
    connection is already bound to. See :func:`project_mismatch_message`."""
    message = project_mismatch_message(row, payload, what=what)
    if message is not None:
        raise HTTPException(status_code=400, detail=message)


class _VerifiedTokenInfo:
    """Adapts an already-fetched ``verify_token()`` response so it can be
    passed to ``connectors.keboola.semantic_layer.require_master_token``
    (which expects an object exposing ``verify_token() -> dict``) without a
    second Storage API round-trip just to reuse its exact error message."""

    def __init__(self, info: Dict[str, Any]) -> None:
        self._info = info

    def verify_token(self) -> Dict[str, Any]:
        return self._info


def _validate_stack_url(config: Optional[Dict[str, Any]], *, required: bool, resolve: bool = True) -> None:
    """SSRF guard for a connection's stack_url. Rejects non-https and
    private/reserved/link-local hosts (e.g. the cloud metadata endpoint).

    ``required=False`` (create/update): validate only if a stack_url is present,
    so partial configs from the "add data source" wizard still save.
    ``required=True`` (test/tables): a stack_url must be present AND is
    re-validated immediately before the outbound request — validate-at-use
    closes the DNS-rebind window between store-time and fetch-time.

    ``resolve=False`` (create/update): check the SCHEME only, no DNS. The
    private-range half needs DNS, and at store time DNS is the wrong
    dependency — a stack that does not resolve from the Agnes host yet (a
    fresh deployment, split-horizon DNS, a momentary outage) is a legitimate
    thing to save, and refusing it would make configuring a connection fail
    for a reason that has nothing to do with the connection. The resolving
    half stays where it belongs: at use, on every outbound call, which is
    also the only placement that closes DNS rebinding. Before this split the
    create/update branch had **no caller at all**, so a stored ``stack_url``
    was never checked even for its scheme. (Devin Review on this PR.)
    """
    stack_url = ((config or {}).get("stack_url") or "").rstrip("/")
    if not stack_url:
        if required:
            raise HTTPException(status_code=400, detail="no stack_url in connection config")
        return
    if not stack_url.lower().startswith("https://"):
        raise HTTPException(status_code=400, detail="stack_url must be an https:// URL")
    if not resolve:
        return
    # Reuse the shared SSRF validator (honors the operator SSRF-allowed-hosts
    # opt-out) rather than duplicating the private-range checks.
    from app.api.admin import _validate_url_not_private

    _validate_url_not_private(stack_url, "stack_url")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("")
async def list_connections(
    source_type: Optional[str] = None,
    _user: dict = Depends(require_admin),
):
    """List all named source connections, optionally filtered by source_type."""
    return [_with_secret_status(r) for r in source_connections_repo().list(source_type=source_type)]


@router.post("", status_code=201)
async def create_connection(
    body: CreateConnectionBody,
    _user: dict = Depends(require_admin),
):
    """Create a named source connection. 409 if the name is already taken."""
    repo = source_connections_repo()
    if repo.get_by_name(body.name) is not None:
        raise HTTPException(status_code=409, detail="connection_name_exists")
    _reject_disallowed_token_env(body.token_env)
    # The `required=False` branch of the SSRF guard exists for exactly this
    # call and its sibling in `update_connection`, and neither was wired — so
    # the branch had no caller at all and an admin-supplied `stack_url` was
    # stored unvalidated. `/test`, `/tables` and `/secret` re-validate at use,
    # which is what closes the DNS-rebind window and is NOT replaced by this;
    # but nothing checked the value on the way IN, and the chat-tools enable
    # path skips validation on the stated premise that the stored URL was
    # already checked here. Now it is. (Devin Review on this PR.)
    _validate_stack_url(body.config, required=False, resolve=False)
    conn_id = str(uuid4())
    repo.create(
        id=conn_id,
        name=body.name,
        source_type=body.source_type,
        config=body.config,
        token_env=body.token_env,
        is_default=body.is_default,
        created_by=_user.get("id"),
    )
    row = _with_secret_status(repo.get(conn_id))
    if body.source_type == "keboola" and body.seed_from_instance_credentials and row is not None:
        row = await _seed_keboola_instance_credential(conn_id, row)
    return row


@router.get("/{connection_id}")
async def get_connection(
    connection_id: str,
    _user: dict = Depends(require_admin),
):
    """Return a single source connection. 404 if not found."""
    row = source_connections_repo().get(connection_id)
    if row is None:
        raise HTTPException(status_code=404, detail="connection_not_found")
    return _with_secret_status(row)


@router.put("/{connection_id}")
async def update_connection(
    connection_id: str,
    body: UpdateConnectionBody,
    _user: dict = Depends(require_admin),
):
    """Update name/config/token_env/is_default of an existing connection.

    404 if missing; 409 if renaming to a name already taken by a different
    connection.

    Moving the connection to a different ``stack_url`` drops any recorded
    project identity: a project id is only meaningful on the stack it came
    from, and keeping the old binding would make every token from the new
    stack fail ``project_mismatch`` with no way to clear it from the UI. This
    is also the supported way to re-point a bound connection — see the note
    on :func:`project_mismatch_message`.
    """
    repo = source_connections_repo()
    existing_row = repo.get(connection_id)
    if existing_row is None:
        raise HTTPException(status_code=404, detail="connection_not_found")
    if body.name is not None:
        existing = repo.get_by_name(body.name)
        if existing is not None and existing["id"] != connection_id:
            raise HTTPException(status_code=409, detail="connection_name_exists")
    _reject_disallowed_token_env(body.token_env)
    _validate_stack_url(body.config, required=False, resolve=False)  # see create_connection
    config = body.config
    if config is not None:
        old_config = existing_row.get("config") or {}
        old_stack = (old_config.get("stack_url") or "").rstrip("/")
        new_stack = (config.get("stack_url") or "").rstrip("/")
        if old_stack and new_stack and old_stack != new_stack:
            config = {k: v for k, v in config.items() if k not in ("project_id", "project_name")}
            logger.info(
                "connection %s moved to a different stack; clearing its recorded project identity",
                connection_id,
            )
        else:
            # `config` REPLACES the stored dict, and the admin form posts only
            # the fields it renders — `project_id`/`project_name` are not among
            # them, because they are recorded by the connection itself rather
            # than typed. So every ordinary edit (a rename, a token_env change,
            # re-saving the same form) silently dropped the binding, and the
            # next token from any project was accepted again: the safeguard
            # this change set exists to add, removed by using the UI. Carried
            # forward unless the caller explicitly supplies the key — an
            # explicit `null` still clears it, which is how a mis-recorded
            # binding is reset without moving the stack. The branch above stays
            # the one deliberate clear: a project id means nothing on a
            # different stack. (Devin Review on this PR.)
            # Keboola only. `project_id`/`project_name` are *recorded* there —
            # written by the connection from its own token's owner block, never
            # typed — which is why an absent key must mean "unchanged". On a
            # BigQuery connection `project_id` is an ordinary configuration
            # field the admin DOES type, so carrying it forward would make it
            # unclearable: an admin who empties the field would see it come
            # straight back. (Devin Review on this PR.)
            if existing_row.get("source_type") == "keboola":
                config = {
                    **{k: v for k, v in old_config.items() if k in ("project_id", "project_name")},
                    **config,
                }
    repo.update(
        connection_id,
        name=body.name,
        config=config,
        token_env=body.token_env,
        is_default=body.is_default,
    )
    _resync_derived_chat_tools(connection_id)
    return _with_secret_status(repo.get(connection_id))


def _resync_derived_chat_tools(connection_id: str) -> None:
    """Rebuild the derived MCP source's spec from the connection's row.

    The spec embeds the connection's NAME and `stack_url` (see
    ``src.keboola_chat_tools.build_stdio_spec``), so an edit that moved the
    project to a new stack left the agent talking to the old one — and, since
    storing a token now propagates to the derived copy, a freshly rotated
    credential was being copied to a source still pointed at the previous
    address. A rename left the agent's source under the old label for the same
    reason. Only an EXISTING derived source is touched, so this never enables
    chat tools; the token slot is not written here at all. Best-effort: the
    connection update itself already succeeded, and this must not turn a good
    edit into a `500`. (Devin Review on this PR.)
    """
    source_id = derived_source_id(connection_id)
    try:
        if mcp_sources_repo().get(source_id) is None:
            return
        row = source_connections_repo().get(connection_id)
        if row is None or row.get("source_type") != "keboola":
            return
        stack_url = ((row.get("config") or {}).get("stack_url") or "").rstrip("/")
        if not stack_url:
            return
        existing = mcp_sources_repo().get(source_id)
        spec = build_stdio_spec(
            connection_id=connection_id,
            connection_name=row.get("name") or connection_id,
            stack_url=stack_url,
            # Connection-derived exactly like the stack URL: added later it has
            # to arrive, removed it has to go. Without it here, setting a
            # workspace schema on an already-enabled connection did nothing
            # until the admin toggled chat tools off and on.
            workspace_schema=_workspace_schema_of(row.get("config")),
        )
        # Only what the CONNECTION determines is re-derived — its name and its
        # stack URL, the two things that actually go stale when it is edited.
        # `build_stdio_spec` describes a freshly enabled source, so upserting
        # it wholesale on an unrelated save (flipping "set as default", a
        # rename) discarded everything an admin had adjusted on that server
        # entry: the enabled flag, extra environment values, launch arguments,
        # the credential scope, the connect hint. None of those are derived
        # from the connection, so they are carried over.
        # (Devin Review on this PR — the `enabled` half first, then the rest.)
        if existing is not None:
            for field in ("enabled", "scope", "connect_hint", "command", "args", "auth_method", "auth_secret_env"):
                if field in existing:
                    spec[field] = existing[field]
            # `env` is merged rather than replaced: the keys this module
            # derives are ours (and absent means absent — see `merge_env`),
            # any other key the admin added is theirs.
            spec["env"] = merge_env(existing.get("env"), spec.get("env") or {})
        # A rename onto a name another MCP source already holds cannot be
        # upserted — `mcp_sources.name` is unique. Skipping loudly beats
        # letting the upsert raise into the broad handler below, which would
        # log the same outcome as an unexpected failure and tell the admin
        # nothing about the clash.
        clash = mcp_sources_repo().get_by_name(spec["name"])
        if clash is not None and clash["id"] != spec["id"]:
            logger.warning(
                "connection %s renamed, but an MCP source named %r already exists (id %s) — "
                "its chat-tools source keeps the previous name and address",
                connection_id,
                spec["name"],
                clash["id"],
            )
            return
        mcp_sources_repo().upsert(**spec)
    except Exception:  # noqa: BLE001 — the connection edit already landed
        logger.warning(
            "updated connection %s but could not re-sync its chat-tools source",
            connection_id,
            exc_info=True,
        )


@router.delete("/{connection_id}", status_code=204)
async def delete_connection(
    connection_id: str,
    _user: dict = Depends(require_admin),
):
    """Delete a source connection. 404 if not found; 409 if tables still reference it."""
    repo = source_connections_repo()
    if repo.get(connection_id) is None:
        raise HTTPException(status_code=404, detail="connection_not_found")
    # Refuse to orphan tables: a registry row pinned to this connection would
    # start failing its sync with "connection_not_found" once the row is gone.
    referencing = [t["id"] for t in table_registry_repo().list_all() if t.get("connection_id") == connection_id]
    if referencing:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "connection_in_use",
                "message": "Repoint or unregister these tables before deleting the connection.",
                "tables": referencing,
            },
        )
    # BEFORE the row goes, not after. A derived chat-tools source outlives its
    # connection otherwise, keeping a live Keboola credential in the vault and
    # still offering the project's tools to the agent — and since this step now
    # raises on a genuine failure rather than swallowing it, doing it after the
    # delete would answer "delete failed" for a connection that is already
    # gone: the admin cannot retry (the retry 404s), the leftover tools have no
    # obvious route to removal, and the list still shows the row until a
    # reload. Running first makes the failure honest and the operation
    # repeatable — nothing has been removed yet, so "retry" is the right
    # advice. (Devin Review on this PR.)
    _remove_chat_tools(connection_id)
    repo.delete(connection_id)
    # Best-effort: clear any vault secret — ignore if none exists.
    try:
        connection_secrets_repo().delete(connection_id)
    except Exception:
        logger.debug("no vault secret for connection %s (expected)", connection_id)
    try:
        connection_secrets_repo().delete(master_secret_key(connection_id))
    except Exception:
        logger.debug("no master vault secret for connection %s (expected)", connection_id)


async def _store_connection_secret(connection_id: str, row: Dict[str, Any], value: str, kind: str) -> None:
    """Preflight (Keboola: ``verify_token`` + project-mismatch guard), persist
    to the vault, propagate to any chat-tools copy, and record the verified
    project identity. Raises ``HTTPException`` on any failure — a doomed
    write, a vault that cannot store secrets, or a token the Storage API
    rejects.

    The single implementation behind ``PUT .../secret`` and the Keboola
    "Import as managed connection" vault-seeding step
    (``_seed_keboola_instance_credential``), so a future fix to the
    preflight/identity-recording behavior lands in exactly one place instead
    of two copies drifting apart.
    """
    if not value:
        raise HTTPException(status_code=400, detail="secret value required")
    if kind not in ("storage", "master"):
        raise HTTPException(status_code=400, detail="invalid_kind")

    # Refuse a doomed write before asking Keboola anything. Both branches below
    # now preflight the token upstream, and a round-trip to validate a secret
    # this instance cannot store is both wasted and actively misleading — the
    # admin would get a token error for what is really an unconfigured vault.
    if not can_store_secrets():
        raise HTTPException(
            status_code=409,
            detail="vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets",
        )

    # Stays None for a non-Keboola connection (no Storage API to ask), which
    # is what the identity-recording step below keys off.
    info: Optional[Dict[str, Any]] = None

    if kind == "master":
        if row.get("source_type") != "keboola":
            raise HTTPException(status_code=400, detail="master_token_only_for_keboola")
        config = row.get("config") or {}
        # Validate-at-use SSRF guard, re-checked immediately before the
        # outbound preflight call (same rationale as /test and /tables).
        _validate_stack_url(config, required=True)
        stack_url = (config.get("stack_url") or "").rstrip("/")
        client = KeboolaStorageClient(url=stack_url, token=value)
        try:
            info = await run_in_threadpool(client.verify_token)
        except (StorageApiError, requests.RequestException) as exc:
            # A freshly typed token is in flight — never surface bare str(exc);
            # route through the client's own token-aware redaction. Only these
            # two named types map to an upstream error at all; an unrelated
            # programming error should surface as a 500, not be mistaken for
            # an upstream outage.
            #
            # A 4xx means the Storage API understood us and said no — the
            # pasted token is invalid, expired, or belongs to another stack.
            # That is the admin's to fix, so it must not come back as 502:
            # a Bad Gateway reads as "Agnes is broken" and sends people
            # hunting infrastructure instead of re-reading the error.
            redacted = client._redact(exc)
            logger.warning(
                "master-token preflight failed for connection %s (%s): %s",
                connection_id,
                _log_host(stack_url),
                redacted,
            )
            status = 400 if is_upstream_client_error(exc) else 502
            raise HTTPException(status_code=status, detail=f"storage_api_error: {redacted}") from exc
        if not info.get("isMasterToken"):
            # Reuse require_master_token's exact message rather than duplicating
            # it — it already fetched isMasterToken, so hand it the cached
            # response instead of a second Storage API round-trip.
            try:
                require_master_token(_VerifiedTokenInfo(info))
            except MasterTokenRequiredError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        # A master token is only "the" master token for THIS connection if it
        # opens the same project the connection is bound to. Without this, a
        # token pasted onto the wrong row stored fine, badged "SET", and the
        # semantic layer synced another project's metrics under this name.
        _reject_project_mismatch(row, info, what="master token")
        key = master_secret_key(connection_id)
    else:
        if row.get("source_type") == "keboola":
            config = row.get("config") or {}
            # Keboola storage tokens get the same preflight as master tokens.
            # It is what makes the project identity knowable at all — the
            # storage token is the one the wizard fills, so it is where a
            # connection's project comes from. The cost is real and worth
            # naming: a Storage API outage now blocks storing a storage token
            # instead of accepting one nobody has checked.
            #
            # No stack_url yet → nothing to preflight AGAINST, so the token is
            # stored unverified and unbound rather than refused. The wizard
            # creates the row before the config is complete (create validates
            # stack_url with required=False for exactly that reason), so
            # demanding one here would have broken storing a token on a
            # half-built connection — a regression this change introduced and
            # Devin Review on #1242 caught. Identity gets recorded later, at
            # /test or when a master token is stored.
            if (config.get("stack_url") or "").strip():
                _validate_stack_url(config, required=True)
                stack_url = (config.get("stack_url") or "").rstrip("/")
                client = KeboolaStorageClient(url=stack_url, token=value)
                try:
                    info = await run_in_threadpool(client.verify_token)
                except (StorageApiError, requests.RequestException) as exc:
                    redacted = client._redact(exc)
                    logger.warning(
                        "storage-token preflight failed for connection %s (%s): %s",
                        connection_id,
                        _log_host(stack_url),
                        redacted,
                    )
                    status = 400 if is_upstream_client_error(exc) else 502
                    raise HTTPException(status_code=status, detail=f"storage_api_error: {redacted}") from exc
                _reject_project_mismatch(row, info, what="storage token")
        key = connection_id

    try:
        connection_secrets_repo().upsert(key, value)
    except VaultKeyNotConfiguredError as exc:
        raise HTTPException(
            status_code=409,
            detail="vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets",
        ) from exc
    if kind != "master":
        # Rotation propagates to the agent's copy, symmetrically with the
        # clear above. Copy-not-reference is a deliberate design, but it made
        # the two halves of the same admin intent behave differently: clearing
        # cut the agent off while rotating left the OLD value live in the MCP
        # vault — so rotating a leaked token left the leak working, and the
        # agent kept authenticating with a credential that may already have
        # been revoked upstream. Only an EXISTING copy is updated; this does
        # not enable chat tools for a connection that never had them.
        # (Devin Review on this PR.)
        derived = derived_source_id(connection_id)
        try:
            # Keyed on the derived SOURCE, not on whether a copy happens to
            # exist. Keying on the copy made clear-then-store a dead end: the
            # clear deletes the copy, so the re-store found nothing to update
            # and left the agent with no credential at all while the switch
            # still read "on" — the two fixes in this PR cancelling each other
            # out. The source is what says chat tools are on.
            # (Devin Review on this PR.)
            if mcp_sources_repo().get(derived) is not None:
                shared_secrets_repo().upsert(derived, value)
        except Exception:  # noqa: BLE001 — the primary store already succeeded
            logger.warning(
                "stored a new token for connection %s but could not re-sync the chat-tools copy",
                connection_id,
                exc_info=True,
            )

    # Only after the secret is safely stored: a recorded identity whose token
    # failed to persist would bind the connection to a project it cannot open.
    if row.get("source_type") == "keboola" and info is not None:
        # Bookkeeping, same as on `/test`: the token is already safely
        # stored by this point, so a vault or DB fault here must not turn a
        # successful save into an error response. (Devin Review.)
        try:
            _record_project_identity(connection_id, row, info)
        except Exception:  # noqa: BLE001 — the secret itself landed
            logger.warning(
                "stored the token for connection %s but could not record its project identity",
                connection_id,
                exc_info=True,
            )


async def _seed_keboola_instance_credential(connection_id: str, row: Dict[str, Any]) -> Dict[str, Any]:
    """For the derived Keboola card's "Import as managed connection" button:
    a freshly-created connection has no vault entry of its own, so unless the
    instance-level credential is resolvable under the EXACT ``token_env`` name
    this new row inherited, its own ``_resolve_token`` finds nothing and the
    "import" produced a connection that cannot actually reach Keboola.

    Resolves via ``app.datasource_secrets.keboola_instance_token`` — the exact
    3-step fallback ``_keboola_credentialed()`` (``app/web/router.py``) checks,
    kept in one place so the two can never drift apart. Only
    ``provenance == "env_token_env"`` needs no seeding: that is the one case
    where the value was found under the row's own configured ``token_env``
    name, which is process-global and so already resolvable by the new row on
    its own. ``"env_generic"`` (found via the generic ``KEBOOLA_STORAGE_TOKEN``
    fallback name, while the row's own ``token_env`` is some OTHER, custom
    name) and ``"vault"`` both need seeding — in neither case will the new
    row's ``_resolve_token`` independently rediscover the value.

    Never raises — a failed or skipped seed must not turn a successful
    ``POST`` into an error response; instead annotates
    ``row["token_seeded"]`` / ``row["token_seed_error"]`` so the caller can
    report the truth instead of a plain success.
    """
    from app.datasource_secrets import keboola_instance_token

    value, provenance = keboola_instance_token(row.get("token_env") or "")
    if provenance in (None, "env_token_env"):
        # Either nothing is credentialed anywhere (`provenance is None` — the
        # button that reaches this path is gated on `_keboola_credentialed()`
        # so this should not happen in practice, but a direct API call could
        # still hit it), or the credential is already resolvable under the
        # exact env var name the new row inherited. Either way, nothing to
        # seed.
        return row

    try:
        await _store_connection_secret(connection_id, row, value, "storage")
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        logger.warning(
            "Keboola import for connection %s: could not seed the instance-vault token: %s",
            connection_id,
            detail,
        )
        row["token_seeded"] = False
        row["token_seed_error"] = detail
        return row
    except Exception as exc:  # noqa: BLE001 — the connection row already landed; never fail the POST
        logger.warning(
            "Keboola import for connection %s: could not seed the instance-vault token",
            connection_id,
            exc_info=True,
        )
        row["token_seeded"] = False
        row["token_seed_error"] = str(exc)
        return row

    refreshed = _with_secret_status(source_connections_repo().get(connection_id)) or row
    refreshed["token_seeded"] = True
    return refreshed


@router.put("/{connection_id}/secret", status_code=204)
async def set_connection_secret(
    connection_id: str,
    body: SecretBody,
    _user: dict = Depends(require_admin),
):
    """Store (or rotate) the vault secret for a connection token.

    ``kind="storage"`` (default): the plain Storage API token used for pulls.
    For a Keboola connection it is preflighted like the master token, and the
    project it opens (``owner.id``/``owner.name``) is recorded on the
    connection — that identity is what later tokens are checked against.
    400 ``project_mismatch`` if the token opens a different project than the
    one this connection is already bound to; a connection is one project.
    ``kind="master"``: a *separate* slot for a Keboola master (owner) Storage
    API token, required by the semantic-layer sync (Metastore API rejects
    non-master tokens). 400 if the connection isn't ``source_type="keboola"``,
    if the token fails a live ``verify_token`` preflight (not a master token),
    or if the Storage API refuses the token outright (4xx — an invalid or
    expired token is the admin's to fix, not a gateway failure). 502 only when
    the Storage API is unreachable or answers 5xx.

    409 if AGNES_VAULT_KEY is not configured on the server — checked FIRST, so
    an instance that cannot store secrets says so instead of spending an
    upstream round-trip and reporting a token problem it never had.
    """
    row = source_connections_repo().get(connection_id)
    if row is None:
        raise HTTPException(status_code=404, detail="connection_not_found")
    await _store_connection_secret(connection_id, row, body.value, body.kind)


@router.delete("/{connection_id}/secret", status_code=204)
async def delete_connection_secret(
    connection_id: str,
    kind: str = "storage",
    _user: dict = Depends(require_admin),
):
    """Clear the vault secret for a connection (idempotent).

    ``kind`` is a query param: ``storage`` (default) or ``master``.
    """
    if source_connections_repo().get(connection_id) is None:
        raise HTTPException(status_code=404, detail="connection_not_found")
    if kind not in ("storage", "master"):
        raise HTTPException(status_code=400, detail="invalid_kind")
    key = master_secret_key(connection_id) if kind == "master" else connection_id
    connection_secrets_repo().delete(key)
    if kind != "master":
        # The chat-tools source holds a COPY of the storage token, taken at
        # enable time. Clearing the connection's token is how an admin cuts a
        # project off, and leaving the copy behind meant the agent kept
        # querying that project with a credential the admin believed they had
        # removed — until they separately noticed the chat-tools switch. The
        # derived source and its tools stay (so the admin still sees the
        # switch is on and can turn it off deliberately); what goes is the
        # credential, which is what "cleared" has to mean.
        # (Devin Review on this PR.)
        try:
            derived = derived_source_id(connection_id)
            shared_secrets_repo().delete(derived)
            # …and switch the derived source OFF with it. Deleting the vault
            # copy alone is not enough: `connectors/mcp/client.py` falls back
            # to `os.environ[auth_secret_env]`, and the derived source names
            # `KBC_STORAGE_TOKEN` — a variable a Keboola deployment plausibly
            # has set — so the agent kept working from the host environment.
            # Clearing `auth_secret_env` instead was WRONG: for a stdio source
            # that name is also how the vault value is injected into the
            # subprocess, so it broke the working path rather than the
            # fallback. Disabling is the honest expression of the admin's
            # intent, and the switch then reads "off" because it is. Turning
            # it back on is explicit, which is what `enable_chat_tools` is
            # for. (Devin Review on this PR, three rounds.)
            source = mcp_sources_repo().get(derived)
            if source is not None and source.get("enabled") is not False:
                mcp_sources_repo().upsert(
                    **{k: v for k, v in {**source, "enabled": False}.items() if k not in ("created_at", "updated_at")}
                )
        except Exception:  # noqa: BLE001 — best-effort; the primary delete already succeeded
            logger.warning(
                "cleared the token for connection %s but could not clear the chat-tools copy",
                connection_id,
                exc_info=True,
            )


def _remove_chat_tools(connection_id: str) -> None:
    """Drop a connection's derived MCP source, its tools, grants and secrets.

    Shared by the explicit disable and by ``delete_connection`` — a deleted
    connection that left its derived source behind would keep a live Keboola
    credential in the vault and keep offering tools for a project the admin
    believes they disconnected.

    Deliberately mirrors ``app.api.admin_mcp.delete_mcp_source`` rather than
    deleting the ``mcp_sources`` row alone. The discovered tools live in
    ``tool_registry`` keyed by source id, and the per-group permissions live
    in ``tool_grants`` keyed by tool — neither is reached by deleting the
    source. Because ``derived_source_id`` is a pure function of the connection
    id, re-enabling later lands on the *same* source id, so orphaned tools and
    their grants would be adopted by the new source: an admin who turned chat
    tools off to revoke access, then turned them back on, would silently
    restore the exact grants they revoked. ``delete_for_source`` drops the
    grants per tool before the registry rows, which is the ordering that
    leaves nothing addressable behind. Per-user secrets get the same treatment
    for the same "no orphaned credential material" reason the canonical path
    cites; the OAuth trio does not apply — the derived source is stdio.
    (Devin Review on this PR.)

    A step that FAILS is reported, not swallowed. Every removal used to sit
    under `except Exception: logger.debug(…)`, which cannot tell "there was
    nothing to delete" from "the delete did not work" — so an admin cutting
    access could be answered `204` while the tools, the grants and the copied
    credential were all still live, which is the one outcome this endpoint
    exists to prevent. Idempotency does not need the broad catch anyway: each
    repository's `delete` is a `DELETE … WHERE id = ?`, a no-op on a row that
    is not there. (Devin Review on this PR.)
    """
    source_id = derived_source_id(connection_id)
    failed: list[str] = []

    def _step(what: str, fn) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 — every step runs; the caller is told which failed
            logger.warning("could not remove %s for connection %s", what, connection_id, exc_info=True)
            failed.append(what)

    def _drop_per_user_secrets() -> None:
        pu_secrets = per_user_secrets_repo()
        for uid in pu_secrets.list_for_source(source_id):
            pu_secrets.delete(source_id, uid)

    # Tools and grants FIRST, and a failure there stops the teardown. The
    # remaining steps are attempted after any *other* failure, because a
    # partial teardown that removes three of four things beats stopping at the
    # first — but that reasoning inverts here: `list_passthrough_for_groups`
    # joins `tool_registry` to `tool_grants` and never to `mcp_sources`, so a
    # tool whose parent source is gone is STILL served to any granted group.
    # Deleting the source after failing to remove the tools would therefore
    # leave the access live while removing the row an admin would use to clean
    # it up from /admin/mcp — strictly worse than stopping.
    # (Devin Review on this PR.)
    _step("tools and their grants", lambda: tool_registry_repo().delete_for_source(source_id))
    if failed:
        # ONLY this step is fatal. The tools ARE the access, they outlive their
        # source row, and nothing has been removed yet — so the caller can
        # simply retry, and the message can honestly name the source to clean
        # up by hand because it still exists.
        raise HTTPException(
            status_code=500,
            detail={
                "error": "chat_tools_not_fully_removed",
                "still_present": failed,
                "message": (
                    "Chat tools were not fully removed — the tools and their grants are "
                    "still live, so analyst access has NOT been revoked. Retry, or remove "
                    f"the derived source {source_id} from /admin/mcp."
                ),
            },
        )

    _step("the derived MCP source", lambda: mcp_sources_repo().delete(source_id))
    _step("the copied credential", lambda: shared_secrets_repo().delete(source_id))
    _step("per-user credentials", _drop_per_user_secrets)
    if failed:
        # Past the tools step, a failure leaves ORPHANED MATERIAL, not access:
        # the grants are gone, so nothing reaches an analyst either way. Raising
        # here made `delete_connection` — which runs this before dropping the
        # row — permanently unfinishable on a persistent vault fault: the retry
        # re-ran the same failing step forever and the connection could never
        # be deleted, while the pre-PR behaviour simply logged it. Logged, with
        # exactly what to clean up. (Devin Review on this PR.)
        logger.warning(
            "chat tools for connection %s were revoked, but these could not be removed and are "
            "left orphaned: %s (source %s)",
            connection_id,
            ", ".join(failed),
            source_id,
        )


async def _register_derived_tools(source: Dict[str, Any], connection_id: str, connection_name: str) -> int:
    """Introspect the derived source and register its tools as passthrough.

    Without this the switch would create a source and nothing else: the
    passthrough surface the agent sees is built from ``tool_registry``, and
    those rows are otherwise only written by an admin curating each tool by
    hand under ``/admin/mcp``. Thirty-odd hand-registrations is not the "one
    switch" this endpoint promises.

    Grants are deliberately NOT created here — registering a tool makes it
    curatable, granting it makes it reachable, and only the second is an
    access-control decision.
    """
    from connectors.mcp.client import list_tools_async

    # Bounded: the introspection spawns `uv tool run …`, whose first run
    # downloads the server, and neither the stdio session nor the subprocess
    # sets a read timeout — an upstream that hangs would otherwise hold the
    # admin request open forever instead of failing into the 502 path this
    # handler was written for. (Devin Review on this PR.)
    tools = await asyncio.wait_for(list_tools_async(source), timeout=CHAT_TOOLS_INTROSPECT_TIMEOUT_S)
    registry = tool_registry_repo()
    existing = {r["tool_id"]: r for r in registry.list_for_source(source["id"])}
    # An upstream that starts but lists nothing (expired token, a runner that
    # resolved an older package) must not be read as "the server dropped all
    # its tools" — reconciling on it would retire every registered tool AND
    # its grants, unrecoverably, behind a 201. Refuse into the 502 path
    # instead; a first enable has nothing to destroy and stays permissive.
    # (Devin Review on this PR, sixth round.)
    if not tools and existing:
        raise RuntimeError("the upstream answered with an empty tool list — refusing to retire the registered tools")
    mutating_count = 0
    for tool in tools:
        row = existing.get(derived_tool_id(connection_id, tool.name))
        if row is None:
            # `read_only is True` — not `not read_only`. An upstream that
            # annotates nothing yields None, and treating that as read-only
            # would mark every tool of such a server safe to call unattended.
            mutating = tool.read_only is not True
            curated: Dict[str, Any] = {}
        else:
            # A re-run re-derives what is THIS release's (names, schema,
            # description) and keeps what is the ADMIN's: PII masking, rate
            # limit and the off-switch are per-tool curation that a token
            # re-sync must not silently reset. `mutating` is the admin's too
            # once the row exists — the CLI explicitly invites correcting it —
            # but an upstream that EXPLICITLY declares a tool not-read-only
            # re-tightens: trusting an explicit claim to restrict is safe,
            # relaxing stays a human decision.
            # (Devin Review on this PR, sixth round.)
            mutating = bool(row["mutating"]) or tool.read_only is False
            curated = {
                "pii_fields": row["pii_fields"],
                "rate_limit_pm": row["rate_limit_pm"],
                "enabled": row["enabled"],
            }
        mutating_count += int(mutating)
        registry.upsert(
            tool_id=derived_tool_id(connection_id, tool.name),
            source_id=source["id"],
            original_name=tool.name,
            exposed_name=exposed_tool_name(connection_id, connection_name, tool.name),
            mode=PASSTHROUGH,
            input_schema=tool.input_schema,
            description=tool.description,
            mutating=mutating,
            **curated,
        )
    # Reconcile: a tool the upstream no longer offers must not stay callable.
    # `derived_tool_id` is deterministic per name, so the loop above updates
    # matching rows in place but never removes one whose name vanished (e.g.
    # after a `KEBOOLA_MCP_VERSION` bump drops a tool) — and the passthrough
    # surface joins `tool_registry` to grants only, so a stale row with a
    # grant would keep being served and fail at the far end. Matching on
    # `original_name` also retires hand-registered rows under this source
    # whose upstream tool is gone — those would fail identically.
    # (Devin Review on this PR.)
    fresh_names = {tool.name for tool in tools}
    for row in registry.list_for_source(source["id"]):
        if row["original_name"] not in fresh_names:
            registry.delete(row["tool_id"])  # cascades the tool's grants
    return len(tools), mutating_count


@router.post("/{connection_id}/chat-tools", status_code=201)
async def enable_chat_tools(
    connection_id: str,
    _user: dict = Depends(require_admin),
):
    """Expose this Keboola project's own MCP tools to the chat agent.

    Derives an ``mcp_sources`` stdio row from the connection (see
    ``src/keboola_chat_tools.py``) and copies the connection's storage token
    into the MCP shared vault, so the admin registers the project once rather
    than a second time under ``/admin/mcp``.

    Idempotent, and it always leaves the source ENABLED — the page's switch
    calls this to turn chat tools on, so a stored "off" must not survive it.
    A rotated token no longer needs a re-run here: ``set_connection_secret``
    copies a new token to the derived source on its own. The derived source
    lands with **no** ``tool_grants``, so enabling exposes nothing until an
    admin grants the tools to a group.

    400 if the connection isn't ``source_type='keboola'`` or has no resolvable
    token; 404 if the connection doesn't exist; 409 if the vault key is unset.
    """
    # Local import: connectors.mcp.client (and the ``mcp`` SDK it pulls in)
    # is ~180ms at import time — only worth paying on the error path below.
    from connectors.mcp.client import exc_summary

    row = source_connections_repo().get(connection_id)
    if row is None:
        raise HTTPException(status_code=404, detail="connection_not_found")
    if row.get("source_type") != "keboola":
        raise HTTPException(status_code=400, detail="chat_tools_only_for_keboola")

    config = row.get("config") or {}
    stack_url = (config.get("stack_url") or "").rstrip("/")
    # Deliberately NOT the DNS-resolving `_validate_stack_url(required=True)`
    # used by /test and /tables: those validate-at-use because they are about
    # to make the outbound call themselves, and DNS is the rebind window they
    # are closing. This endpoint makes no request — it stores a config row —
    # and the URL it stores is the connection's own, whose SCHEME was checked
    # on create/update (`resolve=False`) — the private-range half needs DNS and
    # lives at use, on `/test`, `/tables` and `/secret`, which is the only
    # placement that closes rebinding anyway. Resolving here would only make
    # enabling fail whenever DNS is unavailable, without narrowing any window.
    if not stack_url:
        raise HTTPException(status_code=400, detail="no stack_url in connection config")
    if not stack_url.lower().startswith("https://"):
        raise HTTPException(status_code=400, detail="stack_url must be an https:// URL")

    token = _resolve_token(connection_id, row)
    if not token:
        raise HTTPException(
            status_code=400,
            detail=(
                "connection has no storage token — store one via "
                f"PUT {router.prefix}/{connection_id}/secret first. Without it the "
                "MCP server would start and fail every tool call at the far end."
            ),
        )

    spec = build_stdio_spec(
        connection_id=connection_id,
        connection_name=row.get("name") or connection_id,
        stack_url=stack_url,
        # Passed through whenever the connection sets `config.workspace_schema`
        # — no token-kind check, the code only refuses to invent a value. A
        # master-token setup normally leaves it unset (Keboola creates the
        # workspace itself); setting it anyway pins query_data to that
        # workspace deliberately. Read from the connection so the admin sets it
        # in one place rather than editing the derived MCP source by hand.
        workspace_schema=_workspace_schema_of(config),
    )
    # What the vault held before this call decides how a failed write is undone.
    # This endpoint is idempotent by design — re-running is how a rotated token
    # is propagated — so on a re-sync the slot we are about to overwrite is the
    # credential the existing, still-live setup authenticates with. Deleting it
    # unconditionally on failure turned a working project's every tool call
    # into an auth error, which is strictly worse than the failed re-sync the
    # admin came to fix. (Devin Review on this PR.)
    previous_secret = shared_secrets_repo().get(spec["id"])
    try:
        shared_secrets_repo().upsert(spec["id"], token)
    except VaultKeyNotConfiguredError as exc:
        raise HTTPException(
            status_code=409,
            detail="vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets",
        ) from exc

    # A derived source is named after the connection, and `mcp_sources.name` is
    # unique — so a hand-registered source that already owns that name made the
    # upsert die with an opaque 500 and no hint at what clashed.
    # (Devin Review on this PR.)
    clashing = mcp_sources_repo().get_by_name(spec["name"])
    if clashing is not None and clashing["id"] != spec["id"]:
        if previous_secret is None:
            shared_secrets_repo().delete(spec["id"])
        raise HTTPException(
            status_code=409,
            detail={
                "error": "mcp_source_name_taken",
                "name": spec["name"],
                "message": (
                    f"An MCP source named {spec['name']!r} already exists (id {clashing['id']}). "
                    "Rename this connection, or remove that source under /admin/mcp, then try again."
                ),
            },
        )
    # This endpoint ENABLES — that is what it is for, and the page's switch
    # calls it to turn chat tools on. An earlier revision carried a stored
    # `enabled=False` over, so that a re-run to propagate a rotated token
    # could not silently re-enable a server the admin had switched off; the
    # cost was the opposite bug, that the switch could no longer turn it back
    # ON. Both are gone now that a rotation propagates on its own path — see
    # `set_connection_secret`, which copies a new token to the derived source
    # directly, so re-running enable is no longer the way to refresh a
    # credential. The unrelated-edit path (`_resync_derived_chat_tools`) still
    # preserves the flag, because an edit is not a request to enable.
    # (Devin Review on this PR, twice — once from each side.)
    #
    # Everything ELSE the admin adjusted on that server entry is carried over,
    # the same way the edit path does it: this endpoint also backs the page's
    # "Re-sync token" button, and rebuilding the row wholesale threw away
    # extra environment values, launch arguments, the credential scope and the
    # connect hint. Only `enabled` is forced, because turning it on is what
    # this endpoint is for. (Devin Review on this PR, third round.)
    existing_row = mcp_sources_repo().get(spec["id"])
    if existing_row is not None:
        # Only the fields that are the ADMIN's. `command`, `args` (the pinned
        # runner version) and `auth_*` are derived from this release, and this
        # endpoint is the "make it current" action — carrying them over meant
        # an upgraded runner never reached a project that already had chat
        # tools on. `scope` is derived too, and for a harder reason: this
        # endpoint writes the token into the SHARED vault, so a preserved
        # `per_user` scope would leave that credential unreachable. The
        # unrelated-edit path preserves all of them, because an edit is not a
        # request to re-derive anything. (Devin Review on this PR, both sides.)
        if "connect_hint" in existing_row:
            spec["connect_hint"] = existing_row["connect_hint"]
        spec["env"] = merge_env(existing_row.get("env"), spec.get("env") or {})
    was_enabled = existing_row is not None
    # What the registry held before this call. The registration step both
    # writes fresh rows and reconciles stale ones away, so a failed re-run has
    # real tool-set damage to put back, not just a credential slot. Grants are
    # captured too: `delete` cascades them, and a rollback that resurrects the
    # tool but not its permissions would silently revoke analyst access.
    registry = tool_registry_repo()
    previous_tools = registry.list_for_source(spec["id"]) if was_enabled else []
    # Snapshot full grant rows (group_id + allow_mutating), not bare group
    # ids: a rollback that restored grants flagless would silently reset an
    # admin's v120 mutating opt-in to read-only.
    previous_grants = {t["tool_id"]: registry.grant_rows_for_tool(t["tool_id"]) for t in previous_tools}

    def _undo() -> bool:
        """Put back what was here. Never raises — it runs inside an exception
        handler, and masking the original failure with a cleanup error would
        cost the admin the only useful diagnostic. Returns whether the restore
        actually succeeded, so the 502 can say "restored" only when it is true
        — asserting it unconditionally was the same class of unverified claim
        the restore itself was built to retire.
        (Devin Review on this PR, seventh round.)"""
        try:
            if not was_enabled:
                # First enable: never leave a source, its tools or the token
                # behind under an id the admin does not know exists.
                _remove_chat_tools(connection_id)
                return True
            # Re-run: by the time registration fails the source row has been
            # rewritten and forced on, the vault slot rewritten, and the
            # registration loop may have written rows before dying. The 502's
            # "nothing was changed" is made true here, not just claimed —
            # earlier this branch put back only the credential, so an admin
            # re-enabling a deliberately disabled source was told the project
            # was untouched while it sat enabled with a fresh token.
            # (Devin Review on this PR, fourth round.)
            mcp_sources_repo().upsert(**{k: existing_row[k] for k in _MCP_SOURCE_UPSERT_FIELDS if k in existing_row})
            if previous_secret is not None:
                shared_secrets_repo().upsert(spec["id"], previous_secret)
            else:
                # The pre-call state held no credential (e.g. auto-cleared when
                # the connection's token was removed) — so neither may this one.
                shared_secrets_repo().delete(spec["id"])
            undo_registry = tool_registry_repo()
            keep = {t["tool_id"] for t in previous_tools}
            for r in undo_registry.list_for_source(spec["id"]):
                if r["tool_id"] not in keep:
                    undo_registry.delete(r["tool_id"])
            for t in previous_tools:
                undo_registry.upsert(**{k: t[k] for k in _TOOL_UPSERT_FIELDS if k in t})
                for grant in previous_grants.get(t["tool_id"], []):
                    undo_registry.add_grant(t["tool_id"], grant["group_id"], allow_mutating=grant["allow_mutating"])
            return True
        except Exception:
            logger.warning("could not roll back a failed enable for %s", connection_id, exc_info=True)
            return False

    try:
        mcp_sources_repo().upsert(**spec)
    except Exception:
        # First enable: never leave the token behind under a source id that
        # does not exist. Re-sync: put back what was working. The error itself
        # propagates — a failed local config write is not something the admin
        # can fix by retrying an upstream, so it must not be dressed up as one.
        if previous_secret is None:
            shared_secrets_repo().delete(spec["id"])
        else:
            shared_secrets_repo().upsert(spec["id"], previous_secret)
        raise

    # Registering is a separate failure domain: this one really is "the
    # upstream did not answer", and it is the step most likely to fail on a
    # cold cache, so it gets the 502 and the retry hint.
    try:
        tool_count, mutating_count = await _register_derived_tools(
            spec, connection_id, row.get("name") or connection_id
        )
    except Exception as exc:
        restored = _undo()
        logger.warning("chat-tools tool registration failed for connection %s", connection_id, exc_info=True)
        raise HTTPException(
            status_code=502,
            detail=(
                f"could not reach the Keboola MCP server: {exc_summary(exc)}. "
                "The first run downloads it, so a retry is usually quick; "
                + (
                    "the previous chat-tools state was restored."
                    if restored
                    else "the previous chat-tools state could not be fully restored — review "
                    "the derived source under /admin/mcp before retrying."
                )
            ),
        ) from exc

    logger.info(
        "chat tools enabled for connection %s (source %s, %d tools, %d admin-only)",
        connection_id,
        spec["id"],
        tool_count,
        mutating_count,
    )
    return {
        "source_id": spec["id"],
        "name": spec["name"],
        "tools_registered": tool_count,
        # `mutating=True` rows are refused for every non-admin by the
        # passthrough policy gate, so a grant alone does not make them
        # reachable. On an upstream that annotates nothing this is ALL of
        # them — the caller must be able to see that, or "grant and go"
        # is a false promise. (Devin Review on this PR, fifth round.)
        "tools_admin_only": mutating_count,
        "granted_to_groups": 0,
    }


@router.delete("/{connection_id}/chat-tools", status_code=204)
async def disable_chat_tools(
    connection_id: str,
    _user: dict = Depends(require_admin),
):
    """Remove the derived MCP source and its copy of the token (idempotent)."""
    if source_connections_repo().get(connection_id) is None:
        raise HTTPException(status_code=404, detail="connection_not_found")
    _remove_chat_tools(connection_id)


@router.post("/{connection_id}/test")
async def test_connection(
    connection_id: str,
    _user: dict = Depends(require_admin),
):
    """Verify connectivity for the connection.

    Resolves the stack URL and token from the connection row (token_env →
    environment lookup, or vault secret), then calls
    ``GET {stack_url}/v2/storage/tokens/verify`` with a 10-second timeout.

    Returns ``{ok: true, project_name: "…"}`` on success or
    ``{ok: false, error: "…"}`` on failure.

    It used to probe ``/v2/storage?exclude=components``, which measured
    verified live (2026-08-10): that endpoint is the unauthenticated stack
    index — it answers **200 with no token at all** and carries no ``owner``
    block. So "Test" reported OK for any token, including a garbage one, and
    the ``project_name`` it returned was always the empty string. Verifying
    the token is the only probe that answers the question the button asks,
    and it is what makes the project identity below readable at all.
    """
    row = source_connections_repo().get(connection_id)
    if row is None:
        raise HTTPException(status_code=404, detail="connection_not_found")

    config = row.get("config") or {}
    try:
        # Re-validate immediately before the outbound call (validate-at-use)
        # so a stored-but-now-rebound host is caught, not just at store time.
        _validate_stack_url(config, required=True)
    except HTTPException as exc:
        err = exc.detail if isinstance(exc.detail, str) else "invalid stack_url"
        logger.info("connection test for %s: failed — %s", connection_id, err)
        return {"ok": False, "error": err}
    stack_url = (config.get("stack_url") or "").rstrip("/")

    token = _resolve_token(connection_id, row)
    if not token:
        logger.info(
            "connection test for %s (%s): failed — no token available",
            connection_id,
            _log_host(stack_url),
        )
        return {"ok": False, "error": "no token available (vault empty, token_env unset)"}

    url = f"{stack_url}/v2/storage/tokens/verify"
    # Outcome log lines carry the status/reason but never the response body —
    # a proxy-echoed token must not land in server logs (the body still goes
    # to the admin client, same as before).
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers={"X-StorageApi-Token": token})
        if resp.status_code == 200:
            data = resp.json()
            project_name = (data.get("owner") or {}).get("name") or ""
            # This is the wizard's own last step, so verifying here is what
            # binds a connection to its project the moment it is created.
            #
            # A disagreement is a FAILED test, not a re-binding: the token this
            # probe resolves is not necessarily the one that established the
            # binding (`_resolve_token` falls back to `token_env`), so
            # overwriting here would quietly re-point the connection and leave
            # the stored master token failing a mismatch nobody caused
            # (Devin Review on #1242).
            if row.get("source_type") == "keboola":
                mismatch = project_mismatch_message(row, data, what="connection's token")
                if mismatch:
                    logger.warning(
                        "connection test for %s (%s): project mismatch",
                        connection_id,
                        _log_host(stack_url),
                    )
                    return {"ok": False, "error": mismatch}
                # Recording the identity is BOOKKEEPING — it must not be able
                # to turn a passing connectivity check into a failure report.
                # It sat inside the same try/except that catches the outbound
                # call's network errors, so a vault or DB fault surfaced to the
                # admin as "connection test failed" with a database message,
                # for a project that is in fact correctly configured.
                # (Devin Review on this PR.)
                try:
                    _record_project_identity(connection_id, row, data)
                except Exception:  # noqa: BLE001 — the check itself succeeded
                    logger.warning(
                        "connection test for %s (%s) passed but its project identity could not be recorded",
                        connection_id,
                        _log_host(stack_url),
                        exc_info=True,
                    )
            # project_name is response-body content — it goes to the caller
            # but deliberately NOT into the log line (see comment above).
            logger.info(
                "connection test for %s (%s): ok",
                connection_id,
                _log_host(stack_url),
            )
            return {"ok": True, "project_name": project_name}
        logger.info(
            "connection test for %s (%s): failed — HTTP %d",
            connection_id,
            _log_host(stack_url),
            resp.status_code,
        )
        return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as exc:
        # Scrub the resolved token before logging (replace, then cap, so a
        # token straddling the cap can't survive). The client-visible error
        # keeps its pre-existing shape — the caller is the admin who
        # configured this token, the aggregated server log is not.
        logger.info(
            "connection test for %s (%s): failed — %s",
            connection_id,
            _log_host(stack_url),
            str(exc).replace(token, "<redacted-storage-token>")[:300],
        )
        return {"ok": False, "error": str(exc)[:300]}


def _scoped_listing(
    client: KeboolaStorageClient, connection_id: str
) -> tuple[Optional[List[dict]], Optional[List[dict]]]:
    """Per-bucket listing driven by the token's own ``bucketPermissions``.

    Bucket-scoped (custom access) tokens can be refused the project-wide
    ``/buckets`` + ``/tables`` listings while remaining able to read the
    buckets they are scoped to. ``/tokens/verify`` works for every token and
    names those buckets, so enumerate exactly what the token can reach.

    Returns ``(buckets, tables)`` in the same shapes the project-wide
    listings produce (per-bucket table rows are stamped with a nested
    ``bucket`` object when the upstream omits it). Returns ``(None, None)``
    when the token carries no ``bucketPermissions`` — nothing to enumerate
    from, the caller re-raises the original listing error. When permissions
    exist but every single bucket fails to list, the last per-bucket error
    is raised so the caller surfaces a real 502 instead of a silently empty
    picker.
    """
    info = client.verify_token()
    perms = info.get("bucketPermissions") or {}
    if not perms:
        return None, None

    buckets: List[dict] = []
    tables: List[dict] = []
    last_exc: Optional[Exception] = None
    for bucket_id in sorted(perms):
        try:
            bucket_tables = client.list_tables(bucket_id)
        except (StorageApiError, requests.RequestException) as exc:
            logger.warning(
                "connection %s: listing tables of bucket %s failed: %s",
                connection_id,
                bucket_id,
                exc,
            )
            last_exc = exc
            continue
        try:
            bucket = client.get_bucket(bucket_id)
        except (StorageApiError, requests.RequestException) as exc:
            # Table listing worked, so keep going — render the bucket from
            # its id alone rather than dropping its tables.
            logger.warning(
                "connection %s: bucket detail for %s failed (%s); rendering from id",
                connection_id,
                bucket_id,
                exc,
            )
            stage, _, rest = bucket_id.partition(".")
            bucket = {"id": bucket_id, "name": rest or bucket_id, "stage": stage, "description": ""}
        buckets.append(bucket)
        for t in bucket_tables:
            if not isinstance(t, dict):
                continue
            if not t.get("bucket"):
                t["bucket"] = {"id": bucket_id}
            tables.append(t)
    if not buckets and last_exc is not None:
        raise last_exc
    logger.info(
        "connection %s: bucket-scoped token fallback listed %d bucket(s), %d table(s)",
        connection_id,
        len(buckets),
        len(tables),
    )
    return buckets, tables


@router.get("/{connection_id}/tables")
async def list_connection_tables(
    connection_id: str,
    _user: dict = Depends(require_admin),
):
    """List Keboola buckets + tables reachable via this connection's token.

    Powers the admin "Add data source" wizard's table picker (#755): after a
    connection tests OK, the UI calls this to render a bucket-grouped
    checkbox list, then registers the selected tables one-by-one via
    ``POST /api/admin/register-table`` with this connection's ``id``.

    Tokens with project-wide read use the project-wide ``/buckets`` +
    ``/tables`` listings. When those are refused — typically a bucket-scoped
    (custom access) token — the endpoint falls back to enumerating the
    token's own ``bucketPermissions`` per bucket, so the picker shows
    exactly what the token can read (``scope: "token_buckets"`` in the
    response marks the fallback; the default is ``scope: "project"``).

    REST-only — admin-UI helper with no analyst-facing CLI/MCP analogue (see
    ``_EXEMPT`` in ``tests/test_documentation_api_triple_surface.py``).

    404 if the connection doesn't exist. 400 if the connection isn't
    ``source_type='keboola'`` (the only source type supported today), if no
    ``stack_url`` is configured, or if no token is resolvable (vault empty,
    ``token_env`` unset). 502 if the upstream Storage API calls fail (both
    the project-wide listing and the per-bucket fallback).

    Returns ``{"buckets": [{"id", "name", "stage", "description", "tables": [
    {"id", "name", "rows", "size_bytes"}, ...]}, ...], "scope": "project" |
    "token_buckets"}``.
    """
    row = source_connections_repo().get(connection_id)
    if row is None:
        raise HTTPException(status_code=404, detail="connection_not_found")
    if row.get("source_type") != "keboola":
        raise HTTPException(status_code=400, detail="tables_listing_only_supported_for_keboola")

    config = row.get("config") or {}
    # Validate-at-use SSRF guard (also defeats DNS rebinding since store time).
    _validate_stack_url(config, required=True)
    stack_url = (config.get("stack_url") or "").rstrip("/")

    token = _resolve_token(connection_id, row)
    if not token:
        raise HTTPException(
            status_code=400,
            detail="no token available (vault empty, token_env unset)",
        )

    client = KeboolaStorageClient(url=stack_url, token=token)

    def _project_listing() -> tuple[List[dict], List[dict]]:
        return client.list_buckets(), client.list_tables()

    scope = "project"
    started = time.monotonic()
    try:
        buckets, tables = await run_in_threadpool(_project_listing)
    except (StorageApiError, requests.RequestException) as exc:
        # Only a PERMISSION failure justifies the per-bucket fallback. That loop
        # makes two upstream calls per bucket the token can see, so on a large
        # project a transient blip would otherwise stall the admin's "Browse &
        # register tables" for minutes and then mislabel a full-access token as
        # `token_buckets`. A 5xx or a connection error is not a scope problem, so
        # it surfaces as itself (Devin Review on #1189).
        # Require an EXPLICIT refusal. The first version of this gate read
        # `status is not None and status not in (401, 403)`, which let a
        # status-less StorageApiError fall THROUGH to the fallback — the very
        # case the gate exists to prevent. `_parse_list` always stamps a status
        # today, so it was unreachable, but the condition said the opposite of
        # its intent and would have started lying the moment some other path
        # raised without one (Devin Review on #1189).
        if getattr(exc, "status", None) not in (401, 403):
            # An HTTPException leaves no server-side trace, unlike the catch-all
            # 500 this path replaced — so a transport failure (DNS, refused
            # connection, read timeout, TLS) was visible only to the admin who
            # happened to click. Redacted: the message can echo a proxy's reply,
            # and `_redact` is what keeps a token out of the log line.
            redacted = client._redact(exc)
            logger.warning(
                "tables listing failed for connection %s (%s): %s",
                connection_id,
                _log_host(stack_url),
                redacted,
            )
            raise HTTPException(status_code=502, detail=f"keboola_storage_api_error: {redacted}") from exc
        # The client's messages already redact response bodies; the token
        # itself travels in a header and never appears in the exception.
        logger.warning(
            "connection %s: project-wide bucket/table listing was refused (%s); "
            "retrying per-bucket via the token's bucketPermissions",
            connection_id,
            exc,
        )
        try:
            buckets, tables = await run_in_threadpool(_scoped_listing, client, connection_id)
        except (StorageApiError, requests.RequestException) as scoped_exc:
            raise HTTPException(status_code=502, detail=f"keboola_storage_api_error: {scoped_exc}") from scoped_exc
        if buckets is None or tables is None:
            # Token has no bucketPermissions to enumerate — the original
            # project-wide failure is the real story.
            raise HTTPException(status_code=502, detail=f"keboola_storage_api_error: {exc}") from exc
        scope = "token_buckets"
    else:
        # A refusal is not the only way a scoped token hides the project: some
        # token shapes get a 200 with an EMPTY array from /v2/storage/buckets
        # instead of a 403. That looked identical to an empty project, so the
        # picker said "no buckets visible to this token" and the fallback never
        # ran — the exact case this endpoint's fallback exists for
        # (Devin Review on #1189).
        #
        # Safe for a genuinely empty project: `_scoped_listing` returns
        # (None, None) when the token carries no bucketPermissions, and we keep
        # the empty project-wide answer. Cost there is one extra verify_token.
        if not buckets:
            try:
                scoped_buckets, scoped_tables = await run_in_threadpool(_scoped_listing, client, connection_id)
            except (StorageApiError, requests.RequestException) as scoped_exc:
                # The project-wide call succeeded, so an empty answer is still a
                # valid one — don't turn a working (if empty) picker into a 502.
                logger.warning(
                    "connection %s: empty project-wide listing, and the per-bucket "
                    "retry failed too (%s); reporting the empty project view",
                    connection_id,
                    scoped_exc,
                )
                scoped_buckets = scoped_tables = None
            if scoped_buckets:
                buckets, tables = scoped_buckets, scoped_tables or []
                scope = "token_buckets"

    # Outcome trail for a probe that is otherwise invisible server-side.
    # Host, not stack_url, and counts, not content: the token travels in a
    # header and a proxy-echoed body must never reach the logs.
    logger.info(
        "tables listing for connection %s (%s): %d buckets, %d tables in %.1fs (scope=%s)",
        connection_id,
        _log_host(stack_url),
        len(buckets),
        len(tables),
        time.monotonic() - started,
        scope,
    )

    tables_by_bucket: Dict[str, List[Dict[str, Any]]] = {}
    for t in tables:
        bucket_id = (t.get("bucket") or {}).get("id", "")
        tables_by_bucket.setdefault(bucket_id, []).append(
            {
                "id": t.get("id"),
                "name": t.get("name"),
                "rows": t.get("rowsCount"),
                "size_bytes": t.get("dataSizeBytes"),
            }
        )

    result = []
    for b in buckets:
        bucket_id = b.get("id")
        result.append(
            {
                "id": bucket_id,
                "name": b.get("name"),
                "stage": b.get("stage"),
                "description": b.get("description"),
                "tables": tables_by_bucket.pop(bucket_id, []),
            }
        )
    # Defensive: a table whose bucket wasn't in the buckets listing (stale
    # permissions edge case) still surfaces, grouped under its bucket id.
    for bucket_id, tbls in tables_by_bucket.items():
        result.append({"id": bucket_id, "name": bucket_id, "stage": None, "description": None, "tables": tbls})

    return {"buckets": result, "scope": scope}
