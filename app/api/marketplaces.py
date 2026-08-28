"""Admin endpoints for marketplace git repositories.

CRUD + on-demand "Sync now" mirroring the /api/users shape. Tokens supplied
through the admin UI are persisted to data/state/.env_overlay (same pattern
as /api/admin/configure for Keboola/BigQuery) — never stored in the DB.
"""

import logging
import os
import re
from datetime import datetime
from typing import Any, List, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from app.resource_types import ResourceType
from app.secrets import persist_overlay_token
from src.marketplace import (
    MarketplaceNotFound,
    MarketplaceNotSyncable,
    delete_marketplace_dir,
    is_valid_ref,
    is_valid_slug,
    sync_marketplaces,
    sync_one,
)

from src.repositories import (
    audit_repo,
    marketplace_plugins_repo,
    marketplace_registry_repo,
    resource_grants_repo,
    user_curated_subscriptions_repo,
    user_groups_repo,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/marketplaces", tags=["marketplaces"])


# ---------------------------------------------------------------------------
# Audit helper — same shape as app/api/users.py::_audit
# ---------------------------------------------------------------------------


def _audit(
    conn: duckdb.DuckDBPyConnection,
    actor_id: str,
    action: str,
    target_id: str,
    params: Optional[dict] = None,
) -> None:
    try:
        safe_params = None
        if params:
            safe_params = {}
            for k, v in params.items():
                if isinstance(v, datetime):
                    safe_params[k] = v.isoformat()
                else:
                    safe_params[k] = v
        audit_repo().log(
            user_id=actor_id,
            action=action,
            resource=f"marketplace:{target_id}",
            params=safe_params,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class CreateMarketplaceRequest(BaseModel):
    name: str
    slug: str
    url: str
    branch: Optional[str] = None
    # v87: pin to a fixed tag name or full 40-char commit SHA (#781).
    # Mutually exclusive with `branch` — validated in create_marketplace.
    ref: Optional[str] = None
    description: Optional[str] = None
    token: Optional[str] = None
    # v32: required at create time. Surfaced on /marketplace cards + plugin
    # detail in place of the historical owner_todo placeholder. The plan
    # decision was to validate at the application layer (no DB NOT NULL)
    # so existing rows survive the schema migration without forcing all
    # admins to refill before the next request lands.
    curator_name: Optional[str] = None
    curator_email: Optional[str] = None


class UpdateMarketplaceRequest(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    branch: Optional[str] = None
    # None = leave untouched; empty string = clear the pin; non-empty = set.
    # Mutually exclusive with `branch` — validated in update_marketplace.
    ref: Optional[str] = None
    description: Optional[str] = None
    # None = leave untouched; empty string = clear token; non-empty = rotate
    token: Optional[str] = None
    # Either field None = leave untouched; non-empty string = update.
    # Empty string is treated the same as None on update so admins can't
    # accidentally null out a curator by submitting an empty form input.
    curator_name: Optional[str] = None
    curator_email: Optional[str] = None


class MarketplaceResponse(BaseModel):
    id: str
    name: str
    url: str
    branch: Optional[str] = None
    # v87: currently-pinned tag/commit ref, if any (#781).
    ref: Optional[str] = None
    description: Optional[str] = None
    registered_by: Optional[str] = None
    registered_at: Optional[str] = None
    last_synced_at: Optional[str] = None
    last_commit_sha: Optional[str] = None
    last_error: Optional[str] = None
    has_token: bool = False
    plugin_count: int = 0
    curator_name: Optional[str] = None
    curator_email: Optional[str] = None
    # Built-in rows (agnes-builtin, agnes-contributed) have no git remote:
    # surfaced so the admin table can drop the "Sync now" button instead of
    # offering an action the API refuses with 409.
    is_builtin: bool = False


# Liberal email regex — RFC 5322 is too permissive to be useful at the
# admin-form layer. Anchored, requires `local@domain.tld`, no whitespace,
# 1-254 chars total. Matches what /admin UI expects an admin to type.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _validate_branch_ref_pair(branch: Optional[str], ref: Optional[str]) -> None:
    """400 when `branch` and `ref` are both set, or `ref` is malformed.

    `ref` (tag name or full 40-char commit SHA) pins a marketplace to a
    fixed point in history; `branch` floats. The two are mutually
    exclusive (#781) — an admin who wants to switch from one to the other
    must explicitly clear the field they're leaving via an empty string.
    """
    if branch and ref:
        raise HTTPException(
            status_code=400,
            detail="branch and ref are mutually exclusive — clear one before setting the other",
        )
    if ref and not is_valid_ref(ref):
        raise HTTPException(
            status_code=400,
            detail="ref must be a valid tag name or a 40-character commit SHA",
        )


def _to_response(row: dict, plugin_count: int = 0) -> MarketplaceResponse:
    token_env = row.get("token_env") or ""
    has_token = bool(token_env) and bool(os.environ.get(token_env, ""))
    return MarketplaceResponse(
        id=row["id"],
        name=row["name"],
        url=row["url"],
        branch=row.get("branch"),
        ref=row.get("ref"),
        description=row.get("description"),
        registered_by=row.get("registered_by"),
        registered_at=str(row["registered_at"]) if row.get("registered_at") else None,
        last_synced_at=str(row["last_synced_at"]) if row.get("last_synced_at") else None,
        last_commit_sha=row.get("last_commit_sha"),
        last_error=row.get("last_error"),
        has_token=has_token,
        plugin_count=plugin_count,
        curator_name=row.get("curator_name"),
        curator_email=row.get("curator_email"),
        is_builtin=bool(row.get("is_builtin")),
    )


class PluginResponse(BaseModel):
    name: str
    description: Optional[str] = None
    version: Optional[str] = None
    author_name: Optional[str] = None
    homepage: Optional[str] = None
    category: Optional[str] = None
    source_type: Optional[str] = None
    source_spec: Optional[Any] = None
    # v39: surfaced so the admin Details modal renders the SYSTEM pill
    # + flips the "Mark as system" / "Unmark system" toggle button.
    is_system: bool = False
    # v78: surfaced so the admin Details modal renders the "Disable plugin"
    # toggle + greys out the system button for admin-disabled plugins.
    admin_disabled: bool = False
    # Curator-side lifecycle, read on demand from the cloned repo's
    # marketplace-metadata.json (not persisted): the Details modal renders a
    # DEPRECATED pill + the curator's note. The sync pipeline is what acts on
    # the flag (auto-disable); this is display only.
    deprecated: bool = False
    deprecation_note: Optional[str] = None
    replacement: Optional[str] = None


class SystemFlagResponse(BaseModel):
    """Return shape of the mark/unmark_system endpoints."""

    marketplace_id: str
    plugin_name: str
    is_system: bool
    affected_groups: int = 0
    affected_users: int = 0


# ---------------------------------------------------------------------------
# Token env-var naming
# ---------------------------------------------------------------------------
#
# Read-modify-write of `.env_overlay` lives in `app.secrets.persist_overlay_token`
# (single shared helper with a process-wide lock). Multiple admins clicking
# Save in /admin/marketplaces + /admin/server-config concurrently must not
# corrupt the overlay file.


def _token_env_name(slug: str) -> str:
    """Derive a conventional env-var name from a slug.

    "foundry-ai" -> "AGNES_MARKETPLACE_FOUNDRY_AI_TOKEN"
    """
    normalized = slug.upper().replace("-", "_")
    return f"AGNES_MARKETPLACE_{normalized}_TOKEN"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=List[MarketplaceResponse])
async def list_marketplaces(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    counts = marketplace_plugins_repo().count_by_marketplace()
    return [_to_response(row, counts.get(row["id"], 0)) for row in marketplace_registry_repo().list_all()]


@router.get("/{marketplace_id}/plugins", response_model=List[PluginResponse])
async def list_plugins(
    marketplace_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Return the cached plugin list for a marketplace.

    Rows come from `marketplace_plugins`, which is refreshed from
    `.claude-plugin/marketplace.json` on every successful sync. An
    unsynced marketplace will return an empty list.
    """
    if not marketplace_registry_repo().get(marketplace_id):
        raise HTTPException(status_code=404, detail="marketplace not found")
    rows = marketplace_plugins_repo().list_for_marketplace(marketplace_id)

    # Curator-side deprecation is read from the cloned working tree at request
    # time (same read-on-demand contract as the rich detail-page fields) so a
    # curator commit shows up on the next modal open, not the next sync.
    #
    # `marketplace_id` is a Starlette `[^/]+` path param used verbatim to build
    # a filesystem root, so it gets the at-use containment check the security
    # playbook mandates alongside the at-ingest one — the registry lookup above
    # already implies a valid slug today, but this call site must not silently
    # become traversable if a future registry writer skips slug validation.
    from app.utils import get_marketplaces_dir
    from src.marketplace import is_safe_plugin_name
    from src.marketplace_metadata import plugin_deprecation, read_marketplace_metadata

    if not is_safe_plugin_name(marketplace_id or ""):
        raise HTTPException(status_code=404, detail="marketplace not found")
    metadata = read_marketplace_metadata(get_marketplaces_dir() / marketplace_id)
    return [
        PluginResponse(
            name=r["name"],
            description=r.get("description"),
            version=r.get("version"),
            author_name=r.get("author_name"),
            homepage=r.get("homepage"),
            category=r.get("category"),
            source_type=r.get("source_type"),
            source_spec=r.get("source_spec"),
            is_system=bool(r.get("is_system")),
            admin_disabled=bool(r.get("admin_disabled")),
            **plugin_deprecation(metadata, r["name"]),
        )
        for r in rows
    ]


@router.post("", response_model=MarketplaceResponse, status_code=201)
async def create_marketplace(
    payload: CreateMarketplaceRequest,
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    slug = (payload.slug or "").strip().lower()
    if not is_valid_slug(slug):
        raise HTTPException(
            status_code=400,
            detail="slug must match [a-z0-9][a-z0-9_-]{0,63} (1-64 chars, start with alnum)",
        )
    if not (payload.url or "").strip().lower().startswith("https://"):
        raise HTTPException(status_code=400, detail="url must start with https://")
    # SSRF guard (audit L2): this URL is git-cloned server-side on the nightly
    # sync, so reject hosts that resolve to a private/reserved network — the
    # same check the other URL-bearing admin fields already run.
    from app.api.admin import _validate_url_not_private

    _validate_url_not_private(payload.url.strip(), field_name="url")
    if not (payload.name or "").strip():
        raise HTTPException(status_code=400, detail="name is required")

    branch = (payload.branch or "").strip() or None
    ref = (payload.ref or "").strip() or None
    _validate_branch_ref_pair(branch, ref)

    # v32: curator is mandatory at create time. Validation lives here (not in
    # DB schema) so legacy rows that pre-date the column survive — admins
    # patch them via the edit modal at their leisure.
    curator_name = (payload.curator_name or "").strip()
    curator_email = (payload.curator_email or "").strip()
    if not curator_name:
        raise HTTPException(
            status_code=400,
            detail="curator_name is required",
        )
    if not curator_email:
        raise HTTPException(
            status_code=400,
            detail="curator_email is required",
        )
    if not _EMAIL_RE.match(curator_email):
        raise HTTPException(
            status_code=400,
            detail="curator_email is not a valid email address",
        )

    repo = marketplace_registry_repo()
    if repo.get(slug):
        raise HTTPException(status_code=409, detail=f"marketplace '{slug}' already exists")

    token_env: Optional[str] = None
    if payload.token:
        token_env = _token_env_name(slug)
        persist_overlay_token(token_env, payload.token)

    repo.register(
        id=slug,
        name=payload.name.strip(),
        url=payload.url.strip(),
        branch=branch,
        ref=ref,
        token_env=token_env,
        description=payload.description,
        registered_by=user.get("email"),
        curator_name=curator_name,
        curator_email=curator_email,
    )
    _audit(
        conn,
        user["id"],
        "marketplace.create",
        slug,
        {"url": payload.url, "has_token": bool(payload.token)},
    )
    return _to_response(repo.get(slug))


@router.patch("/{marketplace_id}", response_model=MarketplaceResponse)
async def update_marketplace(
    marketplace_id: str,
    payload: UpdateMarketplaceRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = marketplace_registry_repo()
    existing = repo.get(marketplace_id)
    if not existing:
        raise HTTPException(status_code=404, detail="marketplace not found")

    # Start with the existing row; override fields the caller provided.
    updated = {
        "id": existing["id"],
        "name": existing["name"],
        "url": existing["url"],
        "branch": existing.get("branch"),
        "ref": existing.get("ref"),
        "token_env": existing.get("token_env"),
        "description": existing.get("description"),
        "registered_by": existing.get("registered_by"),
        "curator_name": existing.get("curator_name"),
        "curator_email": existing.get("curator_email"),
    }
    changed: dict = {}
    if payload.name is not None:
        if not payload.name.strip():
            raise HTTPException(status_code=400, detail="name cannot be empty")
        updated["name"] = payload.name.strip()
        changed["name"] = updated["name"]
    if payload.url is not None:
        url = payload.url.strip()
        if not url.lower().startswith("https://"):
            raise HTTPException(status_code=400, detail="url must start with https://")
        from app.api.admin import _validate_url_not_private

        _validate_url_not_private(url, field_name="url")  # SSRF guard (audit L2)
        updated["url"] = url
        changed["url"] = url
    if payload.branch is not None:
        updated["branch"] = payload.branch.strip() or None
        changed["branch"] = updated["branch"]
    if payload.ref is not None:
        updated["ref"] = payload.ref.strip() or None
        changed["ref"] = updated["ref"]
    # Re-checked after merging both fields (not just when one of them was
    # just touched) — an admin who only PATCHes `branch` while a `ref` pin
    # from an earlier request is still on the row must also be caught here.
    _validate_branch_ref_pair(updated["branch"], updated["ref"])
    if payload.description is not None:
        updated["description"] = payload.description
        changed["description"] = payload.description

    # Curator fields: empty-string treated as "no change" so an admin
    # editing only the URL doesn't accidentally null out curator metadata.
    if payload.curator_name is not None and payload.curator_name.strip():
        updated["curator_name"] = payload.curator_name.strip()
        changed["curator_name"] = updated["curator_name"]
    if payload.curator_email is not None and payload.curator_email.strip():
        if not _EMAIL_RE.match(payload.curator_email.strip()):
            raise HTTPException(
                status_code=400,
                detail="curator_email is not a valid email address",
            )
        updated["curator_email"] = payload.curator_email.strip()
        changed["curator_email"] = updated["curator_email"]

    if payload.token is not None:
        # None = untouched; "" = clear token_env binding; non-empty = rotate.
        if payload.token == "":
            if updated["token_env"]:
                persist_overlay_token(updated["token_env"], "")
            updated["token_env"] = None
            changed["token"] = "cleared"
        else:
            env_name = _token_env_name(marketplace_id)
            persist_overlay_token(env_name, payload.token)
            updated["token_env"] = env_name
            changed["token"] = "rotated"

    # Mandatory curator on UPDATE too — legacy rows that pre-date v32 have
    # NULL curator and survive the migration, but the moment an admin opens
    # the edit modal they must fill the gap. The previous PATCH flow let
    # URL/description tweaks persist indefinitely with OWNER_TODO_PLACEHOLDER
    # showing on every /marketplace card. The DB column itself stays nullable
    # so untouched legacy rows are not broken.
    if not (updated.get("curator_name") or "").strip():
        raise HTTPException(
            status_code=400,
            detail="curator_name is required",
        )
    if not (updated.get("curator_email") or "").strip():
        raise HTTPException(
            status_code=400,
            detail="curator_email is required",
        )

    repo.register(
        id=updated["id"],
        name=updated["name"],
        url=updated["url"],
        branch=updated["branch"],
        ref=updated["ref"],
        token_env=updated["token_env"],
        description=updated["description"],
        registered_by=updated["registered_by"],
        curator_name=updated["curator_name"],
        curator_email=updated["curator_email"],
    )
    _audit(conn, user["id"], "marketplace.update", marketplace_id, changed)
    counts = marketplace_plugins_repo().count_by_marketplace()
    return _to_response(repo.get(marketplace_id), counts.get(marketplace_id, 0))


@router.delete("/{marketplace_id}", status_code=204)
async def delete_marketplace(
    marketplace_id: str,
    purge: bool = False,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = marketplace_registry_repo()
    existing = repo.get(marketplace_id)
    if not existing:
        raise HTTPException(status_code=404, detail="marketplace not found")

    # A built-in row is not an admin-registered pointer that can be dropped and
    # re-added: `agnes-builtin` is re-seeded from the wheel on every boot (so the
    # delete is a no-op the next restart undoes), and the contributed
    # marketplace has NO re-seed at all — with `purge=true` its locally written
    # skills are gone for good, which is the same content-destroying shape as
    # the sync bug this release fixes. Retiring built-in content is what the
    # per-plugin disable is for; it drops a plugin from every served surface
    # without touching the row or the disk.
    if existing.get("is_builtin"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"marketplace {marketplace_id!r} is built-in and cannot be deleted — its content "
                "ships with the instance (or is written locally) and has no registered remote. "
                "To retire its content, disable the individual plugins instead "
                "(POST /api/marketplaces/{marketplace_id}/plugins/{plugin_name}/disable)."
            ),
        )

    # Also clear any overlay token binding so a re-created marketplace of the
    # same slug doesn't accidentally inherit the old PAT.
    if existing.get("token_env"):
        persist_overlay_token(existing["token_env"], "")

    repo.unregister(marketplace_id)
    # Drop cached plugin rows and any resource grants that reference plugins
    # from this marketplace. Routed through the repo factory so the deletes hit
    # the ACTIVE backend (PG / DuckDB) — a raw conn.execute on the
    # Depends(_get_db) connection runs against the always-DuckDB system file and
    # silently no-ops on a Postgres instance, orphaning the plugins + grants in
    # PG (#518 backend-split). The repos match the marketplace slug via
    # split_part(resource_id, '/', 1) rather than a LIKE prefix, so a slug
    # containing '_' can't drop a sibling marketplace's grants.
    try:
        marketplace_plugins_repo().clear_for_marketplace(marketplace_id)
        resource_grants_repo().delete_for_marketplace_plugins(marketplace_id)
        # Drop user subscriptions to plugins from this marketplace so a
        # re-registered slug doesn't inherit stale subscribe state.
        user_curated_subscriptions_repo().delete_for_marketplace(marketplace_id)
    except Exception as e:
        logger.warning("cleanup for marketplace %s failed: %s", marketplace_id, e)
    purged = False
    if purge:
        try:
            purged = delete_marketplace_dir(marketplace_id)
        except Exception as e:
            logger.warning("delete_marketplace_dir(%s) failed: %s", marketplace_id, e)

    _audit(
        conn,
        user["id"],
        "marketplace.delete",
        marketplace_id,
        {"purged_disk": purged},
    )


@router.post("/{marketplace_id}/sync")
def trigger_sync(
    marketplace_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Sync one marketplace on demand ("Sync now" in the admin UI).

    Declared ``def`` (not ``async def``) for the same reason as
    :func:`trigger_sync_all` below — ``sync_one`` does blocking I/O
    (subprocess git clone/fetch, DuckDB writes, the module-wide sync lock)
    and would freeze the event loop for the duration of the sync if it ran
    on the asyncio thread.
    """
    try:
        result = sync_one(marketplace_id)
    except MarketplaceNotFound:
        raise HTTPException(status_code=404, detail="marketplace not found")
    except MarketplaceNotSyncable as e:
        # 409, not 500: the row exists and is healthy — the action simply does
        # not apply to it. Nothing was touched, so no sync_failed audit row and
        # no `last_error` stamp (which the nightly sync would never clear,
        # leaving the built-in row permanently red in the admin table and
        # "error" in the marketplace-health report).
        raise HTTPException(status_code=409, detail=str(e))
    except (RuntimeError, ValueError) as e:
        _audit(conn, user["id"], "marketplace.sync_failed", marketplace_id, {"error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))
    _audit(
        conn,
        user["id"],
        "marketplace.sync",
        marketplace_id,
        {"commit": result["commit"], "action": result["action"]},
    )
    return result


@router.post("/sync-all")
def trigger_sync_all(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Sync every registered marketplace.

    Wired up so the scheduler service can drive the nightly refresh over
    HTTP. The previous implementation called ``src.marketplace.sync_marketplaces``
    in-process from the scheduler container, which conflicted with the app's
    long-lived ``system.duckdb`` handle (DuckDB allows only one writer per
    file across processes). Routing through the app inherits the existing
    connection without contention.

    Declared ``def`` (not ``async def``) so FastAPI runs it in a thread
    pool — :func:`sync_marketplaces` does blocking I/O (subprocess git
    clones with ``GIT_TIMEOUT_SEC=300`` per repo, DuckDB writes, a
    process-wide threading.Lock) and would freeze the event loop for the
    duration of a bulk sync if it ran on the asyncio thread. Health
    checks, login redirects, and every other concurrent request keep
    serving while the bulk sync churns through the registry.

    One audit row per call summarises the outcome — per-marketplace details
    live in ``marketplace_registry`` and the per-call result payload below.
    """
    result = sync_marketplaces()
    # _audit appends "marketplace:" to the target id when writing the
    # resource column. "_all" produces "marketplace:_all" — a stable,
    # greppable sentinel for bulk-sync rows; the real per-marketplace
    # commit/error breakdown is in the params payload.
    _audit(
        conn,
        user["id"],
        "marketplace.sync_all",
        "_all",
        {
            "synced": [r.get("id") for r in result.get("synced", [])],
            "errors": [{"id": e.get("id"), "error": e.get("error")} for e in result.get("errors", [])],
        },
    )
    return result


# ---------------------------------------------------------------------------
# v39: system plugin mark / unmark
#
# A plugin marked as "system" is materialized into:
#   * resource_grants — one row per existing user_groups row
#   * user_plugin_optouts — one row per existing users row
# so the resolver's existing (rbac ∩ subscriptions) computation naturally
# includes it for every user. The UI then locks the corresponding controls
# (admin can't revoke per-group, user can't unsubscribe). Unmark flips the
# flag only — materialized rows survive so unmark cannot accidentally rip
# the plugin out of every user's stack mid-day; admin curates the cleanup
# afterwards via the standard resource_grants UI.
# ---------------------------------------------------------------------------


def _invalidate_marketplace_etag() -> None:
    """Drop the served-marketplace ETag cache so the next ZIP / git fetch
    rehashes against the post-mark/post-unmark RBAC view. Best-effort —
    the cache layer survives an import error during early startup."""
    try:
        from app.marketplace_server import packager

        packager.invalidate_etag_cache()
    except Exception:
        logger.exception("failed to invalidate marketplace etag cache")


@router.post(
    "/{marketplace_id}/plugins/{plugin_name}/system",
    response_model=SystemFlagResponse,
)
def mark_plugin_system(
    marketplace_id: str,
    plugin_name: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Mark a plugin as system (mandatory for every user).

    Idempotent — re-running a mark on an already-system plugin still
    runs the fanout (cheap; ON CONFLICT DO NOTHING) so any user/group
    that slipped past the creation hooks gets caught up.
    """

    # Existence check + is_system flip routed through the factory so they hit
    # the ACTIVE backend (PG / DuckDB). A raw conn.execute on the
    # Depends(_get_db) connection runs against the always-DuckDB system file and
    # silently no-ops on a Postgres instance (#518 backend-split): the SELECT
    # would 404 a plugin that only exists in PG, and the UPDATE would never
    # persist is_system.
    plugins_repo = marketplace_plugins_repo()
    plugin = plugins_repo.get(marketplace_id, plugin_name)
    if not plugin:
        raise HTTPException(status_code=404, detail="plugin not found")
    # A disabled plugin must not regain system status. Disabling clears
    # is_system and re-enabling does NOT restore it; marking a disabled plugin
    # system here would resurrect it as a mandatory default on re-enable,
    # breaking that contract. The UI greys the button out, but this endpoint is
    # the real state boundary (direct API call / stale-modal race).
    if plugin.get("admin_disabled"):
        raise HTTPException(status_code=409, detail="plugin is disabled")
    # set_system returns False only on a concurrent delete between the fetch
    # above and the flip — surface it as a 404 rather than fanning out a ghost.
    if not plugins_repo.set_system(marketplace_id, plugin_name, True):
        raise HTTPException(status_code=404, detail="plugin not found")

    resource_id = f"{marketplace_id}/{plugin_name}"
    affected_groups = 0
    affected_users = 0

    # Pivot fanout: this plugin × every group / every user, through the repo
    # factory. user_groups lives in the active backend, so the raw
    # "SELECT id FROM user_groups" on the DuckDB conn read the wrong (frozen)
    # group set on Postgres. ensure_grant is INSERT-OR-IGNORE / ON-CONFLICT-DO-
    # NOTHING on both engines, so an idempotent re-run is cheap and never raises
    # on a duplicate (the old code caught DuckDB's ConstraintException, which a
    # Postgres IntegrityError would have slipped past). has_grant before the
    # ensure keeps affected_groups meaning "newly granted"; user_groups is small.
    grants_repo = resource_grants_repo()
    actor_email = user.get("email") or user.get("id")
    for group in user_groups_repo().list_all():
        group_id = group["id"]
        already = grants_repo.has_grant([group_id], ResourceType.MARKETPLACE_PLUGIN.value, resource_id)
        grants_repo.ensure_grant(
            group_id,
            ResourceType.MARKETPLACE_PLUGIN.value,
            resource_id,
            actor_email,
        )
        if not already:
            affected_groups += 1

    affected_users = user_curated_subscriptions_repo().fanout_system_for_plugin(
        marketplace_id,
        plugin_name,
    )

    _audit(
        conn,
        user["id"],
        "marketplace.plugin.mark_system",
        f"{marketplace_id}/{plugin_name}",
        {"affected_groups": affected_groups, "affected_users": affected_users},
    )
    _invalidate_marketplace_etag()

    return SystemFlagResponse(
        marketplace_id=marketplace_id,
        plugin_name=plugin_name,
        is_system=True,
        affected_groups=affected_groups,
        affected_users=affected_users,
    )


@router.delete(
    "/{marketplace_id}/plugins/{plugin_name}/system",
    status_code=204,
)
def unmark_plugin_system(
    marketplace_id: str,
    plugin_name: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Flip ``is_system`` to FALSE. Materialized grants/subscriptions
    survive — admin curates cleanup via the standard /admin/access UI
    (which immediately unlocks the checkboxes for this plugin) and
    users can unsubscribe normally on /marketplace?tab=my.
    """
    # Routed through the factory so the flip lands on the active backend — a
    # raw UPDATE on the Depends(_get_db) DuckDB conn no-ops on Postgres
    # (#518 backend-split). set_system returns False when no row matched.
    if not marketplace_plugins_repo().set_system(marketplace_id, plugin_name, False):
        raise HTTPException(status_code=404, detail="plugin not found")
    _audit(
        conn,
        user["id"],
        "marketplace.plugin.unmark_system",
        f"{marketplace_id}/{plugin_name}",
        None,
    )
    _invalidate_marketplace_etag()


# ---------------------------------------------------------------------------
# Per-plugin admin disable (v77 built-in marketplace)
# ---------------------------------------------------------------------------


class DisablePluginRequest(BaseModel):
    """Optional body of the disable endpoint.

    ``revoke_grants=True`` turns the disable into a one-action retirement:
    every ``marketplace_plugin`` grant on the plugin is deleted in the same
    call. Without it (or with no body at all — the pre-existing call shape)
    grants are left intact, so a later re-enable restores the plugin for the
    same groups.
    """

    revoke_grants: bool = False


class AdminDisableResponse(BaseModel):
    """Return shape of the enable/disable plugin endpoints."""

    marketplace_id: str
    plugin_name: str
    admin_disabled: bool
    # Number of resource_grants rows deleted by ``revoke_grants=True``;
    # always 0 on enable and on a plain disable.
    revoked_grants: int = 0


@router.post(
    "/{marketplace_id}/plugins/{plugin_name}/disable",
    response_model=AdminDisableResponse,
)
async def disable_plugin(
    marketplace_id: str,
    plugin_name: str,
    payload: Optional[DisablePluginRequest] = None,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Admin-disable a plugin instance-wide.

    Disabled plugins are filtered from the served feed for all callers
    regardless of their RBAC grants. Distinct from per-user opt-outs.
    Primarily intended for built-in plugins (is_builtin=TRUE registry row),
    but works on any registered plugin.

    Optional JSON body ``{"revoke_grants": true}`` additionally deletes every
    group grant on the plugin (one-action retirement) — without it the grants
    stay, so re-enabling restores the plugin for the same groups. The response
    reports the number of grants removed as ``revoked_grants``.
    """
    if not marketplace_registry_repo().get(marketplace_id):
        raise HTTPException(status_code=404, detail="marketplace not found")
    # ORDER MATTERS: set_admin_disabled clears is_system in the same UPDATE.
    # `DELETE /api/access/grants/{id}` refuses to revoke a grant on a
    # still-system plugin (409 cannot_revoke_system_grant) so nobody punches a
    # hole in a mandatory-tier plugin; the bulk delete below does not consult
    # that guard, and is only safe because is_system is already FALSE by the
    # time it runs — the same state a manual unmark-system → revoke sequence
    # would reach. Keep the disable flip strictly before the grant deletion.
    found = marketplace_plugins_repo().set_admin_disabled(marketplace_id, plugin_name, True)
    if not found:
        raise HTTPException(status_code=404, detail="plugin not found")
    requested = payload is not None and payload.revoke_grants
    revoked = 0
    if requested:
        # Exact-equality delete on the composed resource_id — no prefix/LIKE
        # hazard (unlike delete_for_marketplace_plugins), and neither half can
        # contain '/': slugs are charset-validated at registration and plugin
        # names at sync ingestion (is_safe_plugin_name).
        revoked = resource_grants_repo().delete_by_resource(
            ResourceType.MARKETPLACE_PLUGIN.value,
            f"{marketplace_id}/{plugin_name}",
        )
    # Both keys always logged: a retirement that found zero grants and a plain
    # disable are different admin intents and must not produce identical audit
    # rows (`revoked_grants` alone cannot tell them apart).
    _audit(
        conn,
        user["id"],
        "marketplace.plugin.disable",
        f"{marketplace_id}/{plugin_name}",
        {"revoke_grants_requested": requested, "revoked_grants": revoked},
    )
    _invalidate_marketplace_etag()
    return AdminDisableResponse(
        marketplace_id=marketplace_id,
        plugin_name=plugin_name,
        admin_disabled=True,
        revoked_grants=revoked,
    )


@router.post(
    "/{marketplace_id}/plugins/{plugin_name}/enable",
    response_model=AdminDisableResponse,
)
async def enable_plugin(
    marketplace_id: str,
    plugin_name: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Re-enable a previously admin-disabled plugin.

    Restores the plugin to the served feed for callers who hold a resource_grant.
    """
    if not marketplace_registry_repo().get(marketplace_id):
        raise HTTPException(status_code=404, detail="marketplace not found")
    found = marketplace_plugins_repo().set_admin_disabled(marketplace_id, plugin_name, False)
    if not found:
        raise HTTPException(status_code=404, detail="plugin not found")
    _audit(conn, user["id"], "marketplace.plugin.enable", f"{marketplace_id}/{plugin_name}", None)
    _invalidate_marketplace_etag()
    return AdminDisableResponse(marketplace_id=marketplace_id, plugin_name=plugin_name, admin_disabled=False)
