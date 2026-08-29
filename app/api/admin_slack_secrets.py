"""Admin REST API for server-wide Slack bot secrets (vault-backed).

  - GET    /api/admin/slack-secrets          — presence/source status (no values)
  - PUT    /api/admin/slack-secrets/{name}    — set / rotate (write-only)
  - DELETE /api/admin/slack-secrets/{name}    — clear

All gated by ``require_admin``. The secret value lives only in the request
body -> Fernet-encrypted at rest in ``system_secrets``. It is never returned
by any endpoint and never placed in an audit record (audit params are empty,
mirroring the MCP secret endpoints).
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.access import require_admin
from app.secrets_vault import VaultKeyNotConfiguredError
from services.slack_bot.secrets import SLACK_SECRET_NAMES
from src.audit_helpers import log_safe
from src.repositories import audit_repo, system_secrets_repo

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin-slack-secrets"])


class SlackSecretBody(BaseModel):
    value: str


def _audit(actor_id: str, action: str, resource: str) -> None:
    """Best-effort audit row. Params are intentionally empty — the secret
    value never enters the audit record (mirrors the MCP secret endpoints).
    Routed through the ``audit_repo()`` factory so it works on either backend."""
    try:
        audit_repo().log(user_id=actor_id, action=action, resource=resource, params={})
    except Exception:
        logger.warning("audit log failed for %s/%s", action, resource)


@router.get("/slack-secrets")
async def list_slack_secrets(user: dict = Depends(require_admin)):
    """Presence/source status for the three Slack tokens. Never leaks values."""
    repo = system_secrets_repo()
    out = []
    for name in SLACK_SECRET_NAMES:
        if os.environ.get(name):
            source, has_value = "env", True
        elif repo.has(name):
            source, has_value = "vault", True
        else:
            source, has_value = "unset", False
        out.append({"name": name, "source": source, "has_value": has_value})
    # NEVER a value — this endpoint only ever reports presence/source.
    log_safe(user_id=user.get("id"), action="slack.secret.read", resource="slack_secrets")
    return {"secrets": out}


@router.put("/slack-secrets/{name}", status_code=204)
async def set_slack_secret(
    name: str,
    body: SlackSecretBody,
    user: dict = Depends(require_admin),
):
    """Store (or rotate) the vault secret for ``name``. Write-only."""
    if name not in SLACK_SECRET_NAMES:
        raise HTTPException(status_code=400, detail="unknown_slack_secret")
    if not body.value:
        raise HTTPException(status_code=400, detail="secret value required")
    try:
        system_secrets_repo().upsert(name, body.value)
    except VaultKeyNotConfiguredError as exc:
        raise HTTPException(
            status_code=409,
            detail="vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets",
        ) from exc
    _audit(user["id"], "slack.secret.set", f"slack_secret:{name}")


@router.delete("/slack-secrets/{name}", status_code=204)
async def delete_slack_secret(
    name: str,
    user: dict = Depends(require_admin),
):
    """Drop the vault row for ``name``. Resolution falls back to env / disabled."""
    if name not in SLACK_SECRET_NAMES:
        raise HTTPException(status_code=400, detail="unknown_slack_secret")
    system_secrets_repo().delete(name)
    _audit(user["id"], "slack.secret.clear", f"slack_secret:{name}")
