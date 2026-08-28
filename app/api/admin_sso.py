"""Admin REST API for the external SSO login config (design 2026-08-28).

  - GET    /api/admin/sso/config               — config status (never the secret)
  - PUT    /api/admin/sso/config               — upsert (validated; enable-guarded)
  - PUT    /api/admin/sso/client-secret        — set / rotate (write-only)
  - DELETE /api/admin/sso/client-secret        — clear (last-login-door guarded)
  - DELETE /api/admin/sso/config               — delete config, KEEP identities
  - POST   /api/admin/sso/test-config          — server-side discovery probe
  - GET    /api/admin/sso/identities           — paginated linked identities
  - DELETE /api/admin/sso/identities/{user_id} — admin unlink (hard delete)

All gated by ``require_admin``. The client secret lives only in the request
body -> Fernet-encrypted at rest on the ``sso_config`` row. It is never
returned by any endpoint and never placed in an audit record (audit params
are empty, cloning the ``admin_slack_secrets`` contract).

The backing repos are PG-only (A3 ratchet): on a DuckDB-backed instance the
``*_repo()`` factories raise ``RequiresPostgresBackend``, which the app-wide
handler translates to a typed 501 — these routes are listed in the parity
sweeps' ``_PG_ONLY_ROUTE_EXEMPTIONS``.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth.access import require_admin
from app.secrets_vault import VaultKeyNotConfiguredError, vault_key_configured
from src.repositories import audit_repo, sso_config_repo, user_external_identities_repo

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin/sso", tags=["admin-sso"])

_IDENTITIES_PAGE_DEFAULT = 50
_IDENTITIES_PAGE_MAX = 200
_DISCOVERY_TIMEOUT_SECONDS = 10.0


class SsoConfigBody(BaseModel):
    tenant_id: str
    client_id: str
    display_name: str
    allowed_email_domains: list[str]
    enabled: bool = False


class SsoSecretBody(BaseModel):
    value: str


def _audit(actor_id: str, action: str, resource: str) -> None:
    """Best-effort audit row. Params are intentionally empty — the secret
    value never enters the audit record (mirrors ``admin_slack_secrets``)."""
    try:
        audit_repo().log(user_id=actor_id, action=action, resource=resource, params={})
    except Exception:
        logger.warning("audit log failed for %s/%s", action, resource)


def _tenant_problem(tenant_id: str) -> str | None:
    """The imported single-tenant validator, with its env-var-era wording
    re-pointed at this API's field name."""
    from app.auth.providers.microsoft import tenant_id_error

    problem = tenant_id_error(tenant_id)
    return problem.replace("MICROSOFT_TENANT_ID", "tenant_id") if problem else None


def _refuse_if_last_login_door(actor_hint: str) -> None:
    """422 when this operation would turn off the ONLY usable login door.

    Fires only when ``sso`` currently IS a usable door (allowed + available)
    — removing an already-unusable config changes nothing and is never
    refused. The break-glass path is documented, not built: the
    ``AGNES_AUTH_PROVIDERS`` env override plus the registry's password/email
    rescue let an operator with server access reopen a door.
    """
    from app.auth import provider_registry
    from app.auth.providers import sso as sso_provider

    if not (provider_registry.provider_allowed("sso") and sso_provider.is_available()):
        return
    if provider_registry.any_login_door_usable_besides("sso"):
        return
    raise HTTPException(
        status_code=422,
        detail=(
            f"last_login_door: {actor_hint} would leave this instance with no usable "
            "sign-in method — SSO is currently the only login door (no other provider "
            "is allowed and configured, and no user holds a password). Open another "
            "door first (e.g. set a password for an admin, or widen auth.providers)."
        ),
    )


def _config_response() -> dict:
    cfg = sso_config_repo().get_config()
    if cfg is None:
        return {
            "configured": False,
            "enabled": False,
            "provider_type": None,
            "tenant_id": None,
            "client_id": None,
            "display_name": None,
            "allowed_email_domains": [],
            "has_client_secret": False,
            "vault_key_configured": vault_key_configured(),
            "updated_at": None,
            "updated_by": None,
        }
    return {
        "configured": True,
        "enabled": cfg["enabled"],
        "provider_type": cfg["provider_type"],
        "tenant_id": cfg["tenant_id"],
        "client_id": cfg["client_id"],
        "display_name": cfg["display_name"],
        "allowed_email_domains": cfg["allowed_email_domains"],
        "has_client_secret": cfg["has_client_secret"],
        "vault_key_configured": vault_key_configured(),
        "updated_at": cfg["updated_at"],
        "updated_by": cfg["updated_by"],
    }


@router.get("/config")
async def get_sso_config(user: dict = Depends(require_admin)):
    """Config status for the admin panel. Never returns the secret."""
    return _config_response()


@router.put("/config")
async def put_sso_config(body: SsoConfigBody, user: dict = Depends(require_admin)):
    """Upsert the singleton config. Validates the tenant via the imported
    single-tenant validator; ``enabled=true`` additionally requires a stored,
    decryptable secret (and the always-required non-empty domain list)."""
    from src.repositories.sso_config_pg import normalize_domains

    problem = _tenant_problem(body.tenant_id)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    if not body.client_id.strip():
        raise HTTPException(status_code=422, detail="client_id must not be empty")
    if not body.display_name.strip():
        raise HTTPException(status_code=422, detail="display_name must not be empty")
    domains = normalize_domains(body.allowed_email_domains)
    if not domains:
        raise HTTPException(
            status_code=422,
            detail=(
                "allowed_email_domains must name at least one domain — the allowlist is "
                "the boundary on whose identities the external tenant may assert, and it "
                "is deliberately NOT inherited from auth.allowed_domain"
            ),
        )

    repo = sso_config_repo()
    prev = repo.get_config()
    if body.enabled and repo.get_client_secret() is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "enable_requires_secret: store a decryptable client secret "
                "(PUT /api/admin/sso/client-secret) before enabling the provider"
            ),
        )
    was_enabled = bool(prev and prev["enabled"])
    if was_enabled and not body.enabled:
        _refuse_if_last_login_door("disabling SSO sign-in")

    repo.upsert_config(
        tenant_id=body.tenant_id.strip(),
        client_id=body.client_id.strip(),
        display_name=body.display_name.strip(),
        allowed_email_domains=domains,
        enabled=body.enabled,
        updated_by=user["id"],
    )
    _audit(user["id"], "sso.config.set", "sso_config:default")
    if body.enabled and not was_enabled:
        _audit(user["id"], "sso.config.enable", "sso_config:default")
    elif was_enabled and not body.enabled:
        _audit(user["id"], "sso.config.disable", "sso_config:default")
    return _config_response()


@router.put("/client-secret", status_code=204)
async def set_sso_client_secret(body: SsoSecretBody, user: dict = Depends(require_admin)):
    """Store (or rotate) the client secret. Write-only."""
    if not body.value:
        raise HTTPException(status_code=400, detail="secret value required")
    try:
        stored = sso_config_repo().set_client_secret(body.value)
    except VaultKeyNotConfiguredError as exc:
        raise HTTPException(
            status_code=409,
            detail="vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets",
        ) from exc
    if not stored:
        raise HTTPException(status_code=404, detail="sso_not_configured: save the SSO config first")
    _audit(user["id"], "sso.secret.set", "sso_config:default")


@router.delete("/client-secret", status_code=204)
async def clear_sso_client_secret(user: dict = Depends(require_admin)):
    """Clear the stored secret (the provider becomes unavailable)."""
    _refuse_if_last_login_door("clearing the SSO client secret")
    if not sso_config_repo().clear_client_secret():
        raise HTTPException(status_code=404, detail="sso_not_configured")
    _audit(user["id"], "sso.secret.delete", "sso_config:default")


@router.delete("/config", status_code=204)
async def delete_sso_config(user: dict = Depends(require_admin)):
    """Delete the config row + secret. KEEPS ``user_external_identities``
    rows — they are historically true links, inert unless the same tenant is
    configured again; purgeable per-user via the identities endpoint."""
    _refuse_if_last_login_door("deleting the SSO config")
    if not sso_config_repo().delete_config():
        raise HTTPException(status_code=404, detail="sso_not_configured")
    _audit(user["id"], "sso.config.delete", "sso_config:default")


def _fetch_discovery_document(url: str) -> dict:
    """GET the OIDC discovery document. Isolated for tests to monkeypatch."""
    import httpx

    resp = httpx.get(url, timeout=_DISCOVERY_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


@router.post("/test-config")
async def test_sso_config(user: dict = Depends(require_admin)):
    """Server-side reachability probe: tenant validation, secret presence +
    decryptability, then the tenant's OIDC discovery document. No SSRF
    surface: fixed host, tenant validated and quoted into a single path
    segment (the ``microsoft.py`` discovery-URL pattern)."""
    repo = sso_config_repo()
    cfg = repo.get_config()
    if cfg is None:
        raise HTTPException(status_code=404, detail="sso_not_configured")

    problem = _tenant_problem(cfg["tenant_id"])
    if problem:
        return {"ok": False, "error": f"tenant_id invalid: {problem}"}
    if not cfg["allowed_email_domains"]:
        return {"ok": False, "error": "allowed_email_domains is empty — the callback would fail closed"}
    if repo.get_client_secret() is None:
        return {"ok": False, "error": "client secret is missing or no longer decrypts — (re-)set it first"}

    url = (
        f"https://login.microsoftonline.com/{quote(cfg['tenant_id'].strip(), safe='')}"
        "/v2.0/.well-known/openid-configuration"
    )
    try:
        doc = _fetch_discovery_document(url)
    except Exception as exc:
        # %r + slice: the response body/exception text originates from the
        # network — same logging discipline as the OAuth callbacks.
        logger.warning("SSO test-config discovery fetch failed: %r", str(exc)[:500])
        return {"ok": False, "error": f"discovery fetch failed: {str(exc)[:200]}"}

    return {
        "ok": True,
        "issuer": doc.get("issuer"),
        "authorization_endpoint": doc.get("authorization_endpoint"),
        "token_endpoint": doc.get("token_endpoint"),
    }


@router.get("/identities")
async def list_sso_identities(
    limit: int = Query(default=_IDENTITIES_PAGE_DEFAULT, ge=1),
    offset: int = Query(default=0, ge=0),
    user: dict = Depends(require_admin),
):
    """Paginated list of linked external identities, newest link first."""
    limit = min(limit, _IDENTITIES_PAGE_MAX)
    repo = user_external_identities_repo()
    rows = repo.list_page(limit=limit, offset=offset)
    return {
        "identities": [
            {
                "user_id": r["user_id"],
                "email": r["email"],
                "email_at_link": r["email_at_link"],
                "provider_type": r["provider_type"],
                "tenant_id": r["tenant_id"],
                "subject": r["subject"],
                "linked_at": r["linked_at"],
                "last_login_at": r["last_login_at"],
            }
            for r in rows
        ],
        "total": repo.count(),
        "limit": limit,
        "offset": offset,
    }


@router.delete("/identities/{user_id}", status_code=204)
async def unlink_sso_identity(user_id: str, user: dict = Depends(require_admin)):
    """Admin unlink — hard delete of the identity row; the recovery tool for
    a mis-attach. Does not touch the user row itself."""
    if not user_external_identities_repo().unlink(user_id):
        raise HTTPException(status_code=404, detail="identity_not_found")
    _audit(user["id"], "sso.identity.unlinked", f"user:{user_id}")
