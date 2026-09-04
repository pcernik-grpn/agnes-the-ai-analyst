"""Shared-secret auth path for the in-cluster scheduler service.

The scheduler container ships every cron tick to the FastAPI app over HTTP
(see ``services.scheduler.__main__``). It needs a long-lived credential to
authenticate itself, but minting a real PAT for it requires a logged-in
session — chicken-and-egg at first boot.

The pragmatic solution: both the ``app`` and ``scheduler`` containers source
the same ``.env`` (via Docker Compose ``env_file: .env``). The
``infra/modules/customer-instance/startup-script.sh.tpl`` generates a random
``SCHEDULER_API_TOKEN`` once at VM provisioning and writes it there. When a
caller presents that exact secret as ``Authorization: Bearer <secret>``, the
app loads (or seeds on demand) a synthetic ``scheduler@system.local`` user
that is a member of the ``Admin`` system group — so existing RBAC paths
continue to work without special-casing.

Constraints on the secret (enforced here, not parsed):

- Empty / unset → this auth path is **disabled**. Production deploys should
  set it; dev / LOCAL_DEV_MODE typically doesn't, since the scheduler
  rides the dev-bypass instead.
- Length < 32 → treated as misconfiguration and disabled. Prevents an
  operator typo that sets ``SCHEDULER_API_TOKEN=todo`` from accidentally
  granting admin to a 4-character bearer.
- Comparison uses :func:`hmac.compare_digest` — constant-time so a remote
  caller cannot mount a length-discrimination timing attack.

Audit: every action by this user is attributed to ``scheduler@system.local``,
visible in ``audit_log`` as a normal admin actor. Rotating the secret is
``edit .env → docker compose restart app scheduler``; no DB write needed.
"""

from __future__ import annotations

import hmac
import logging
import os
import uuid
from typing import Optional

import duckdb

logger = logging.getLogger(__name__)

# Identity of the synthetic user that backs the shared-secret auth path.
# Kept stable so audit-log entries from the scheduler are easy to filter.
SCHEDULER_USER_EMAIL = "scheduler@system.local"
SCHEDULER_USER_NAME = "Scheduler"

# Floor on the secret length. 32 bytes ≈ 256 bits of entropy if generated
# from /dev/urandom; well above the brute-force frontier and well above any
# typo a human is plausibly going to make.
SCHEDULER_TOKEN_MIN_LENGTH = 32


def get_scheduler_secret() -> str:
    """Return the configured shared secret, stripped. Empty when disabled."""
    return os.environ.get("SCHEDULER_API_TOKEN", "").strip()


def is_scheduler_token(token: str) -> bool:
    """True iff ``token`` exactly matches the configured shared secret.

    Returns False when the env var is empty or shorter than the minimum
    length (auth path disabled). Uses constant-time comparison.
    """
    if not token:
        return False
    secret = get_scheduler_secret()
    if not secret or len(secret) < SCHEDULER_TOKEN_MIN_LENGTH:
        return False
    return hmac.compare_digest(token, secret)


def ensure_scheduler_user(conn: Optional[duckdb.DuckDBPyConnection] = None) -> dict:
    """Idempotently provision the scheduler user + Admin group membership.

    Called both from the app's startup hook (so the user exists from the
    very first boot) and lazily from :func:`get_scheduler_user` so a token
    presented before the next restart of the app still resolves.

    ``conn`` retained for signature stability — actual repo lookups go
    through the factory in ``src.repositories``.
    """
    from src.db import SYSTEM_ADMIN_GROUP
    from src.repositories import (
        users_repo,
        user_groups_repo,
        user_group_members_repo,
    )

    users = users_repo()
    user = users.get_by_email(SCHEDULER_USER_EMAIL)
    if not user:
        user_id = str(uuid.uuid4())
        users.create(
            id=user_id,
            email=SCHEDULER_USER_EMAIL,
            name=SCHEDULER_USER_NAME,
            password_hash=None,
        )
        users.mark_system_identity(user_id)
        user = users.get_by_email(SCHEDULER_USER_EMAIL)
        logger.info("Seeded scheduler service user: %s", SCHEDULER_USER_EMAIL)

    admin_group = user_groups_repo().get_by_name(SYSTEM_ADMIN_GROUP)
    if admin_group:
        user_group_members_repo().add_member(
            user_id=user["id"],
            group_id=admin_group["id"],
            source="system_seed",
            added_by="app.auth.scheduler_token:ensure_scheduler_user",
        )

    return user


def get_scheduler_user(conn: Optional[duckdb.DuckDBPyConnection] = None) -> Optional[dict]:
    """Look up the scheduler user, seeding it on demand if absent.

    Returns None only when seeding fails — typically a malformed schema or
    an out-of-band DB error. The caller (``get_current_user``) maps None
    to a normal 401 so the failure is observable but does not crash.

    ``conn`` retained for signature stability; actual lookup is through
    the factory in ``src.repositories``.
    """
    from src.repositories import users_repo

    user = users_repo().get_by_email(SCHEDULER_USER_EMAIL)
    if user:
        return user
    try:
        return ensure_scheduler_user()
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to provision scheduler user on demand: %s", e)
        return None
