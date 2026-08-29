"""Personal access token endpoints (#12)."""

import hashlib
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, List

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth.access import require_admin
from app.auth.dependencies import require_session_token, get_current_user, _get_db
from app.auth.jwt import create_access_token

from src.audit_helpers import log_safe
from src.repositories import (
    access_token_repo,
    audit_repo,
)

router = APIRouter(prefix="/auth/tokens", tags=["tokens"])
admin_router = APIRouter(prefix="/auth/admin/tokens", tags=["tokens-admin"])


class CreateTokenRequest(BaseModel):
    name: str
    expires_in_days: Optional[int] = 90  # null = no expiry
    # Informational tag carried in the JWT (`scope` claim) and the audit log.
    # The special value "bootstrap-analyst" force-clamps the resolved TTL to
    # at most 1 hour regardless of the requested lifetime, so the bootstrap
    # PAT can't be repurposed as a long-lived credential.
    scope: str = "general"
    # If set, wins over expires_in_days. Mirrors the same 10-year cap as
    # expires_in_days (3650 days * 86400 = 315_360_000 seconds).
    ttl_seconds: Optional[int] = None
    # v106: credential data-read surface — 'all' (default here: self-service
    # PATs back CI/automation, so no silent narrowing on upgrade) or 'stack'
    # (catalog/query follow the owner's stack even for admins; the value is
    # inert for non-admins, whose read surface is stack-scoped regardless).
    surface: str = "all"


class CreateTokenResponse(BaseModel):
    id: str
    name: str
    prefix: str
    token: str  # raw token — returned exactly once
    expires_at: Optional[str]
    created_at: str


class TokenListItem(BaseModel):
    id: str
    name: str
    prefix: str
    created_at: str
    expires_at: Optional[str]
    last_used_at: Optional[str]
    revoked_at: Optional[str]
    # v106: 'all' | 'stack' — lets owners/admins audit which tokens still
    # carry the full (god-mode-capable) read surface.
    surface: Optional[str] = "all"


class AdminTokenItem(TokenListItem):
    """Admin list row: adds owner identity + last IP for incident response."""

    user_id: str
    user_email: Optional[str] = None
    last_used_ip: Optional[str] = None


def _audit(conn, actor: str, action: str, target: str, params=None):
    try:
        audit_repo().log(user_id=actor, action=action, resource=f"token:{target}", params=params)
    except Exception:
        pass


def _row_to_item(row: dict) -> TokenListItem:
    return TokenListItem(
        id=row["id"],
        name=row["name"],
        prefix=row["prefix"],
        created_at=str(row.get("created_at") or ""),
        expires_at=str(row["expires_at"]) if row.get("expires_at") else None,
        last_used_at=str(row["last_used_at"]) if row.get("last_used_at") else None,
        revoked_at=str(row["revoked_at"]) if row.get("revoked_at") else None,
        surface=row.get("surface") or "all",
    )


def _row_to_admin_item(row: dict) -> AdminTokenItem:
    return AdminTokenItem(
        id=row["id"],
        name=row["name"],
        prefix=row["prefix"],
        created_at=str(row.get("created_at") or ""),
        expires_at=str(row["expires_at"]) if row.get("expires_at") else None,
        last_used_at=str(row["last_used_at"]) if row.get("last_used_at") else None,
        revoked_at=str(row["revoked_at"]) if row.get("revoked_at") else None,
        user_id=row.get("user_id") or "",
        user_email=row.get("user_email"),
        last_used_ip=row.get("last_used_ip"),
        surface=row.get("surface") or "all",
    )


@router.post("", response_model=CreateTokenResponse, status_code=201)
async def create_token(
    payload: CreateTokenRequest,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    if payload.surface not in ("all", "stack"):
        raise HTTPException(status_code=400, detail="surface must be 'all' or 'stack'")
    if payload.expires_in_days is not None and payload.expires_in_days <= 0:
        raise HTTPException(status_code=400, detail="expires_in_days must be a positive integer")
    # Cap at 10 years — larger values overflow datetime.max during the
    # `datetime.now(utc) + timedelta(days=...)` addition and surface as an
    # unhandled OverflowError → 500. 10y is well past any legitimate PAT
    # lifetime (the no-expiry path below uses ~100y and doesn't compute
    # expires_at on the datetime object).
    if payload.expires_in_days is not None and payload.expires_in_days > 3650:
        raise HTTPException(status_code=400, detail="expires_in_days must not exceed 3650 (10 years)")
    if payload.ttl_seconds is not None and payload.ttl_seconds <= 0:
        raise HTTPException(status_code=400, detail="ttl_seconds must be a positive integer")
    # Mirror the 10-year cap: 3650 days * 86400 s/day = 315_360_000 seconds.
    if payload.ttl_seconds is not None and payload.ttl_seconds > 315_360_000:
        raise HTTPException(status_code=400, detail="ttl_seconds must not exceed 315360000 (10 years)")

    # Resolve TTL: ttl_seconds wins; fall back to expires_in_days; else "no expiry".
    expires_delta: Optional[timedelta] = None
    omit_exp = False
    if payload.ttl_seconds is not None:
        expires_delta = timedelta(seconds=payload.ttl_seconds)
    elif payload.expires_in_days is not None:
        expires_delta = timedelta(days=payload.expires_in_days)
    else:
        omit_exp = True  # "no expiry" — DB stores expires_at=NULL and the JWT
        # carries no `exp` claim. The authoritative expiry check lives in
        # app/auth/dependencies.py (via the DB row).

    # Force-clamp bootstrap-analyst PATs to <= 1 h regardless of request, so
    # an init-time PAT can't be repurposed as a long-lived credential.
    if payload.scope == "bootstrap-analyst":
        ONE_HOUR = timedelta(hours=1)
        if expires_delta is None or expires_delta > ONE_HOUR:
            expires_delta = ONE_HOUR
        omit_exp = False

    expires_at: Optional[datetime] = None
    if expires_delta is not None:
        expires_at = datetime.now(timezone.utc) + expires_delta

    repo = access_token_repo()
    token_id = str(uuid.uuid4())
    # Build the JWT that embeds jti=token_id and typ=pat
    jwt_token = create_access_token(
        user_id=user["id"],
        email=user["email"],
        token_id=token_id,
        typ="pat",
        expires_delta=expires_delta,
        omit_exp=omit_exp,
        extra_claims={"scope": payload.scope},
    )
    # Prefix: first 8 chars of the jti (UUID) — uniquely identifies the token in UI
    # without exposing JWT headers (which all start with "eyJhbGci…" and are useless
    # for identification). The JWT itself is returned ONCE in the response body.
    prefix = token_id.replace("-", "")[:8]
    # token_hash = sha256(raw JWT). Used in verify_token as defense-in-depth.
    token_hash = hashlib.sha256(jwt_token.encode()).hexdigest()
    repo.create(
        id=token_id,
        user_id=user["id"],
        name=payload.name.strip(),
        token_hash=token_hash,
        prefix=prefix,
        expires_at=expires_at,
        surface=payload.surface,
    )
    _audit(
        conn,
        user["id"],
        "token.create",
        token_id,
        {"name": payload.name, "scope": payload.scope, "surface": payload.surface},
    )
    return CreateTokenResponse(
        id=token_id,
        name=payload.name.strip(),
        prefix=prefix,
        token=jwt_token,  # returned EXACTLY ONCE; never retrievable again
        expires_at=str(expires_at) if expires_at else None,
        created_at=str(datetime.now(timezone.utc)),
    )


@router.get("", response_model=List[TokenListItem])
async def list_tokens(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    # PATs may list their owner's own tokens — required by the documented
    # `agnes auth token list` CLI flow (HEADLESS_USAGE.md). Only `create_token`
    # is session-only (to block PAT-spawning-PAT chains).
    rows = access_token_repo().list_for_user(user["id"])
    log_safe(user_id=user["id"], action="token.list", resource="token:own", params={"scope": "own"})
    return [_row_to_item(r) for r in rows]


@router.get("/{token_id}", response_model=TokenListItem)
async def get_token(
    token_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    row = access_token_repo().get_by_id(token_id)
    if not row or row["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Token not found")
    log_safe(user_id=user["id"], action="token.list", resource=f"token:{token_id}", params={"scope": "one"})
    return _row_to_item(row)


@router.delete("/{token_id}", status_code=204)
async def revoke_token(
    token_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = access_token_repo()
    row = repo.get_by_id(token_id)
    if not row or row["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Token not found")
    repo.revoke(token_id)
    _audit(conn, user["id"], "token.revoke", token_id)


# Admin — list & revoke tokens across users (for incident response)


@admin_router.get("", response_model=List[AdminTokenItem])
async def admin_list_tokens(
    limit: int = Query(default=1000, ge=1, le=10000),
    offset: int = Query(default=0, ge=0),
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    rows = access_token_repo().list_all_with_user(limit=limit, offset=offset)
    log_safe(user_id=user["id"], action="token.list", resource="token:admin_all", params={"scope": "admin_all"})
    return [_row_to_admin_item(r) for r in rows]


@admin_router.delete("/{token_id}", status_code=204)
async def admin_revoke_token(
    token_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = access_token_repo()
    row = repo.get_by_id(token_id)
    if not row:
        raise HTTPException(status_code=404, detail="Token not found")
    repo.revoke(token_id)
    _audit(conn, user["id"], "token.admin_revoke", token_id, {"owner_id": row["user_id"]})
