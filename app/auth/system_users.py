"""Non-human system identities that back a headless internal caller.

Sibling to ``app/auth/scheduler_token.py``'s ``ensure_scheduler_user`` — the
same idempotent-seed pattern (a real ``users`` row, provisioned on demand,
never a bare string the broker can't mint a JWT for: ``app/api/broker.py``'s
``_mint_identity_jwt`` does an unconditional ``users_repo().get_by_email(...)``
and 401s ``ticket_user_not_found`` on a miss). Kept in its own module rather
than folded into ``scheduler_token.py`` because it carries no shared-secret /
token-verification concerns — only the identity-provisioning half applies.

``semantic-drafter@system.local`` is the identity a later, headless
auto-drafting chat session (semantic-layer V0 wave 2) authenticates as when
proposing semantic-model edits. Critically UNLIKE the scheduler user, it is
never added to the Admin group: ``POST /api/semantic-models/apply``
(``Depends(get_current_user)``, not ``require_admin`` — see
``app/api/semantic_models.py``) routes a non-admin caller's document to the
``submitted_for_review`` moderation queue instead of writing it directly, and
that moderation gate is the entire safety property this identity exists to
preserve. Admin-promoting it would let a compromised or malfunctioning
drafting session write semantic models straight to the flat query tables
projection reads, unreviewed.

``memory-curator@system.local`` is a different shape of identity: it owns no
sandbox and runs no chat turn. It exists purely so the ``memory-curator``
agent profile (issue #1971 — corporate-memory detection as an editable,
observable "agent") has an OWNER, because ``agents.owner_user_id`` is
NOT NULL and the profile is config/identity for the EXISTING direct LLM
calls the verification detector already makes, never a spawned agent
runtime. See ``app/services/memory_curator_profile.py`` for the profile
itself and the policy-text accessor.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

# Identity of the synthetic user that backs the headless semantic-model
# auto-drafting session. Kept stable so audit-log entries and moderation
# queue submissions attributed to it are easy to filter.
SEMANTIC_DRAFTER_USER_EMAIL = "semantic-drafter@system.local"
SEMANTIC_DRAFTER_USER_NAME = "Semantic Drafter"


def ensure_semantic_drafter_user(conn: Optional[object] = None) -> dict:
    """Idempotently provision the semantic-drafter user — no group
    membership beyond whatever ``Everyone``/default RBAC a plain user gets.

    ``conn`` retained for signature stability, mirroring
    ``ensure_scheduler_user`` — actual repo lookups go through the factory
    in ``src.repositories``, so this works unchanged on either the DuckDB or
    Postgres backend.
    """
    from src.repositories import users_repo

    users = users_repo()
    user = users.get_by_email(SEMANTIC_DRAFTER_USER_EMAIL)
    if not user:
        user_id = str(uuid.uuid4())
        users.create(
            id=user_id,
            email=SEMANTIC_DRAFTER_USER_EMAIL,
            name=SEMANTIC_DRAFTER_USER_NAME,
            password_hash=None,
        )
        user = users.get_by_email(SEMANTIC_DRAFTER_USER_EMAIL)
        logger.info("Seeded semantic-drafter service user: %s", SEMANTIC_DRAFTER_USER_EMAIL)

    return user


# Identity of the synthetic user that owns the seeded ``memory-curator``
# agent profile (see ``app/services/memory_curator_profile.py``). Never
# added to the Admin group — it needs no authority of its own, since nothing
# ever authenticates AS it; ``agents.owner_user_id`` is simply NOT NULL and
# this row is what satisfies that constraint for a profile nobody personally
# owns.
MEMORY_CURATOR_USER_EMAIL = "memory-curator@system.local"
MEMORY_CURATOR_USER_NAME = "Memory Curator"


def ensure_memory_curator_user(conn: Optional[object] = None) -> dict:
    """Idempotently provision the memory-curator service user.

    Same shape as :func:`ensure_semantic_drafter_user` — ``conn`` is accepted
    for signature stability but unused, since every read/write goes through
    the ``src.repositories`` factory and therefore works unchanged on either
    the DuckDB or Postgres backend.
    """
    from src.repositories import users_repo

    users = users_repo()
    user = users.get_by_email(MEMORY_CURATOR_USER_EMAIL)
    if not user:
        user_id = str(uuid.uuid4())
        users.create(
            id=user_id,
            email=MEMORY_CURATOR_USER_EMAIL,
            name=MEMORY_CURATOR_USER_NAME,
            password_hash=None,
        )
        user = users.get_by_email(MEMORY_CURATOR_USER_EMAIL)
        logger.info("Seeded memory-curator service user: %s", MEMORY_CURATOR_USER_EMAIL)

    return user
