"""Service-account identities — admin CRUD + PAT issuance (issue #1534).

A service account is a `users` row flagged `kind='service'` (PG-only —
`users.kind`, A3 ratchet, `migrations/versions/0096_users_kind.py`): a
headless caller (CI, an integration, a bot) that holds its OWN group grants
and mints its OWN independently-revocable PATs, and can never sign in
interactively or land in the Admin group (see `app/auth/jwt.py` and
`src/repositories/user_group_members(_pg).py` for those two guards).

Every route here requires the Postgres app-state backend — declared ONCE as
a router-level dependency rather than repeated per handler, so a DuckDB-
backed instance answers a clean, typed 501 on all five routes uniformly
(`RequiresPostgresBackend`), not just the two the dynamic parity sweep would
otherwise catch on its own (path-param routes are out of scope for that
sweep by construction).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.tokens import CreateTokenRequest, CreateTokenResponse, mint_pat
from app.auth.access import require_admin
from app.auth.dependencies import require_session_token
from src.audit_helpers import log_safe
from src.repositories import RequiresPostgresBackend, use_pg, users_repo
from src.service_accounts import is_service_account

# Same shape as agents_admin.py's _SLUG_RE — lowercase, digits, hyphens.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# The synthetic-address domain for service accounts — deliberately distinct
# from `@system.local` (app/auth/system_users.py, app/auth/scheduler_token.py),
# which names identities INTERNAL to Agnes itself (the scheduler, the
# semantic-drafter, the memory-curator). A service account is admin-
# provisioned, user-visible, and independently manageable, so it gets its
# own unroutable-by-construction domain rather than sharing the internal one.
SERVICE_ACCOUNT_EMAIL_DOMAIN = "service.local"


def _require_pg_backend() -> None:
    if not use_pg():
        raise RequiresPostgresBackend("service_accounts")


router = APIRouter(
    prefix="/api/admin/service-accounts",
    tags=["service-accounts"],
    dependencies=[Depends(_require_pg_backend)],
)


class CreateServiceAccountRequest(BaseModel):
    name: str
    slug: str


class ServiceAccountResponse(BaseModel):
    id: str
    name: Optional[str] = None
    email: str
    active: bool
    created_at: str
    deactivated_at: Optional[str] = None
    token_count: int = 0
    last_used_at: Optional[str] = None
    soonest_expiry: Optional[str] = None


def _to_response(row: dict) -> ServiceAccountResponse:
    return ServiceAccountResponse(
        id=row["id"],
        name=row.get("name"),
        email=row["email"],
        active=bool(row.get("active", True)),
        created_at=str(row.get("created_at") or ""),
        deactivated_at=str(row["deactivated_at"]) if row.get("deactivated_at") else None,
        token_count=int(row.get("token_count") or 0),
        last_used_at=str(row["last_used_at"]) if row.get("last_used_at") else None,
        soonest_expiry=str(row["soonest_expiry"]) if row.get("soonest_expiry") else None,
    )


def _get_service_account_or_404(service_account_id: str) -> dict:
    row = users_repo().get_by_id(service_account_id)
    if not row or not is_service_account(row):
        raise HTTPException(status_code=404, detail="Service account not found")
    return row


@router.post("", response_model=ServiceAccountResponse, status_code=201)
async def create_service_account(
    payload: CreateServiceAccountRequest,
    admin: dict = Depends(require_admin),
):
    """Create a headless identity: `kind='service'`, `active=true`, a
    synthetic `<slug>@service.local` address, NEVER in the Admin group (its
    own guard lives in `user_group_members(_pg).add_member`, not here)."""
    name = payload.name.strip()
    slug = payload.slug.strip().lower()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if not _SLUG_RE.match(slug):
        raise HTTPException(
            status_code=400,
            detail="slug must be lowercase letters, digits and hyphens (e.g. 'ci-bot')",
        )
    email = f"{slug}@{SERVICE_ACCOUNT_EMAIL_DOMAIN}"

    repo = users_repo()
    if repo.get_by_email(email):
        raise HTTPException(status_code=409, detail=f"slug '{slug}' is already in use")

    account_id = str(uuid.uuid4())
    repo.create_service_account(id=account_id, email=email, name=name)

    log_safe(
        user_id=admin["id"],
        action="user.service_account_create",
        resource=f"user:{account_id}",
        params={"slug": slug, "name": name},
    )
    return _to_response(repo.get_by_id(account_id))


@router.get("", response_model=List[ServiceAccountResponse])
async def list_service_accounts(
    admin: dict = Depends(require_admin),
):
    """Every service account with a per-account PAT summary (count,
    `last_used_at`, soonest `expires_at`) — what an operator needs to see
    what breaks before deactivating one."""
    rows = users_repo().list_service_accounts()
    return [_to_response(r) for r in rows]


@router.post("/{service_account_id}/tokens", response_model=CreateTokenResponse, status_code=201)
async def mint_service_account_token(
    service_account_id: str,
    payload: CreateTokenRequest,
    admin: dict = Depends(require_admin),
    _session_only: dict = Depends(require_session_token),
):
    """Mint a PAT FOR the service account.

    CRITICAL GATE: admin AND session-token-only (mirrors `POST /auth/tokens`
    — a PAT-authenticated admin gets a typed 403 here too via
    `require_session_token`, preserving the #1292 boundary: durable
    credentials are only ever minted from an interactive session). Reuses
    the exact PAT pipeline `POST /auth/tokens` uses (`app.api.tokens.mint_pat`)
    — the minted row's `user_id` is the SERVICE ACCOUNT's id, `typ` stays
    `"pat"` (nothing here needs a new token kind: pat_resolver's whole
    validity chain — revoked/expired/hash-mismatch/active-check — already
    treats any `users` row as a first-class PAT holder regardless of `kind`).
    """
    account = _get_service_account_or_404(service_account_id)
    result = mint_pat(account["id"], account["email"], payload)
    log_safe(
        user_id=admin["id"],
        action="token.create",
        resource=f"token:{result.id}",
        params={"name": payload.name, "surface": payload.surface, "for_user_id": account["id"]},
    )
    return result


class UpdateServiceAccountRequest(BaseModel):
    """Activation is a state change on the resource (`{"active": bool}`),
    not a verb segment — tests/test_api_design_rules.py, same rework the
    share-request lifecycle went through."""

    active: bool


@router.patch("/{service_account_id}", response_model=ServiceAccountResponse)
async def update_service_account(
    service_account_id: str,
    payload: UpdateServiceAccountRequest,
    admin: dict = Depends(require_admin),
):
    """Flip `users.active` — the SAME primitive
    `POST /api/users/{id}/deactivate` uses. `pat_resolver`'s existing
    `active` check then kills every one of the account's PATs with zero new
    code; re-activation clears `deactivated_at`/`deactivated_by`."""
    account = _get_service_account_or_404(service_account_id)
    repo = users_repo()
    if payload.active:
        repo.update(id=account["id"], active=True, deactivated_at=None, deactivated_by=None)
    else:
        repo.update(
            id=account["id"],
            active=False,
            deactivated_at=datetime.now(timezone.utc),
            deactivated_by=admin["id"],
        )
    log_safe(
        user_id=admin["id"],
        action="user.service_account_activate" if payload.active else "user.service_account_deactivate",
        resource=f"user:{account['id']}",
        params={"email": account["email"]},
    )
    return _to_response(repo.get_by_id(account["id"]))
