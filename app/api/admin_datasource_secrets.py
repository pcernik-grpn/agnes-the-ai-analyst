"""Admin REST API for datasource credentials (vault-backed).

  - GET    /api/admin/datasource-secrets             — presence/source status (no values)
  - PUT    /api/admin/datasource-secrets/{name}      — set / rotate (write-only)
  - DELETE /api/admin/datasource-secrets/{name}      — clear vault row
  - POST   /api/admin/validate-gws-credentials       — format-check GWS client_id (no network)

All gated by ``require_admin``. Secret values are never returned by any
endpoint and never placed in audit records (params are empty, mirroring
the Slack secrets endpoints).
"""

from __future__ import annotations

import json
import logging
import os
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.access import require_admin
from app.datasource_secrets import DATA_SOURCE_SECRET_NAMES
from app.secrets_vault import VaultKeyNotConfiguredError
from src.audit_helpers import log_safe
from src.repositories import audit_repo, system_secrets_repo

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin-datasource-secrets"])

_BQ_JSON_MAX_BYTES = 64 * 1024  # 64 KiB cap
_GWS_CLIENT_ID_RE = re.compile(r"^\d+-\w+\.apps\.googleusercontent\.com$")


class DatasourceSecretBody(BaseModel):
    value: str


class ValidateGwsBody(BaseModel):
    client_id: str


def _audit(actor_id: str, action: str, resource: str) -> None:
    try:
        audit_repo().log(user_id=actor_id, action=action, resource=resource, params={})
    except Exception:
        logger.warning("audit log failed for %s/%s", action, resource)


def _validate_gws_credential(name: str, value: str) -> None:
    if name == "AGNES_GWS_CLIENT_ID" and not _GWS_CLIENT_ID_RE.match(value):
        raise HTTPException(status_code=400, detail="invalid_gws_client_id")


def _validate_bq_json(value: str) -> None:
    """Validate BigQuery service account JSON shape.

    Raises ``HTTPException(400, detail='invalid_service_account_json')`` if the
    value is not a well-formed service account credential.
    """
    if len(value.encode()) > _BQ_JSON_MAX_BYTES:
        raise HTTPException(status_code=400, detail="invalid_service_account_json")
    try:
        info = json.loads(value)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid_service_account_json")
    required = {"type", "private_key", "client_email"}
    if not isinstance(info, dict) or info.get("type") != "service_account" or not required.issubset(info.keys()):
        raise HTTPException(status_code=400, detail="invalid_service_account_json")


@router.get("/datasource-secrets")
async def list_datasource_secrets(user: dict = Depends(require_admin)):
    """Presence/source status for known datasource secrets. Never leaks values."""
    repo = system_secrets_repo()
    out = []
    for name in DATA_SOURCE_SECRET_NAMES:
        if os.environ.get(name):
            source, has_value = "env", True
        elif repo.has(name):
            source, has_value = "vault", True
        else:
            source, has_value = "unset", False
        out.append({"name": name, "source": source, "has_value": has_value})
    # NEVER a value — this endpoint only ever reports presence/source.
    log_safe(user_id=user.get("id"), action="datasource.secret.read", resource="datasource_secrets")
    return {"secrets": out}


@router.put("/datasource-secrets/{name}", status_code=204)
async def set_datasource_secret(
    name: str,
    body: DatasourceSecretBody,
    user: dict = Depends(require_admin),
):
    """Store (or rotate) the vault secret for ``name``. Write-only."""
    if name not in DATA_SOURCE_SECRET_NAMES:
        raise HTTPException(status_code=400, detail="unknown_datasource_secret")
    if not body.value.strip():
        raise HTTPException(status_code=400, detail="secret value required")
    if name == "BIGQUERY_SERVICE_ACCOUNT_JSON":
        _validate_bq_json(body.value.strip())
    if name in ("AGNES_GWS_CLIENT_ID", "AGNES_GWS_CLIENT_SECRET"):
        _validate_gws_credential(name, body.value.strip())
    try:
        system_secrets_repo().upsert(name, body.value.strip())
    except VaultKeyNotConfiguredError as exc:
        raise HTTPException(
            status_code=409,
            detail="vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets",
        ) from exc
    if name == "BIGQUERY_SERVICE_ACCOUNT_JSON":
        try:
            from connectors.bigquery.auth import clear_token_cache  # noqa: PLC0415

            clear_token_cache()
        except Exception:
            pass
    _audit(user["id"], "datasource.secret.set", f"datasource_secret:{name}")


@router.post("/validate-gws-credentials")
async def validate_gws_credentials(
    body: ValidateGwsBody,
    user: dict = Depends(require_admin),
):
    """Format-check a GWS ``client_id`` without saving it or making a network call.

    Returns ``{"valid": bool}`` (200 always) so the UI "Test" button can show a
    ✓/✗ before the admin commits the value via PUT. Mirrors the server-side regex
    applied during ``set_datasource_secret``.
    """
    return {"valid": bool(_GWS_CLIENT_ID_RE.match(body.client_id.strip()))}


@router.delete("/datasource-secrets/{name}", status_code=204)
async def delete_datasource_secret(
    name: str,
    user: dict = Depends(require_admin),
):
    """Drop the vault row for ``name``. Resolution falls back to env / unset."""
    if name not in DATA_SOURCE_SECRET_NAMES:
        raise HTTPException(status_code=400, detail="unknown_datasource_secret")
    system_secrets_repo().delete(name)
    _audit(user["id"], "datasource.secret.clear", f"datasource_secret:{name}")
