"""The ``memory-curator`` agent profile — config + identity for the
EXISTING corporate-memory LLM extraction paths (issue #1971, Part 1).

Runtime shape: **profile-as-config**, not a spawned agent runtime. This
module never runs a sandbox, never streams a chat turn, and never sends a
session transcript anywhere but the existing direct
``StructuredExtractor.extract_json`` call in
``services/verification_detector/detector.py``. The agent profile
(``agents`` row) is reused purely as an already-built, already-admin-gated
place to store and edit a policy string — the ``/agents`` builder's
"Instructions" field — because building a bespoke config surface for one
string would duplicate CRUD, auth, and UI that already exists.

The full-runtime upgrade path (spawning ``memory-curator`` as an actual
sandboxed agent that reasons over multiple tool calls) stays open — nothing
here forecloses it — but is deliberately NOT built: transcripts must not
enter a sandbox, and the 15-minute detection cadence must stay cheap. See
``docs/corporate-memory-governance.md`` -> "Detection as an editable agent".

Ownership: the profile is owned by the synthetic ``memory-curator@system.local``
user (:mod:`app.auth.system_users`), the same idempotent-seed pattern as
``semantic-drafter``. Admin editing is carved out narrowly in
``app/api/agents_admin.py::_load_agent`` — see that module for the exact
authorization reasoning; ownership rules alone would 404 EVERY caller
(including admins) on a system-owned row, which would make "admin-gated
editing" unreachable.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

# Re-exported so callers (and the seed-idempotency test) have one place to
# import the default policy text from, whether they want the profile module
# or the prompt-assembly module.
from services.verification_detector.prompts import DEFAULT_DETECTION_POLICY

logger = logging.getLogger(__name__)

#: Reserved for this seed only — never assigned by the normal create-agent
#: slug picker because it is always taken (``_unique_slug`` skips taken
#: slugs), so there is no separate reserved-slug list entry to maintain.
MEMORY_CURATOR_AGENT_SLUG = "memory-curator"
MEMORY_CURATOR_AGENT_NAME = "Memory Curator"
MEMORY_CURATOR_AGENT_DESCRIPTION = (
    "Editable detection policy for corporate-memory extraction (session "
    "transcripts). Config + identity only — no chat runtime spawns from "
    "this profile."
)

__all__ = [
    "MEMORY_CURATOR_AGENT_SLUG",
    "MEMORY_CURATOR_AGENT_NAME",
    "MEMORY_CURATOR_AGENT_DESCRIPTION",
    "DEFAULT_DETECTION_POLICY",
    "ensure_memory_curator_agent_profile",
    "get_memory_curator_policy_text",
]


def ensure_memory_curator_agent_profile() -> Dict[str, Any]:
    """Idempotently seed the ``memory-curator`` agent profile.

    Insert-if-absent, exactly like ``memory_domains_repo().ensure_seed`` and
    ``ensure_semantic_drafter_user``: a second call (every app boot) must
    never reset an admin's edited policy text back to the default. Provisions
    the owning system user first (also idempotent) so ``owner_user_id`` is
    always resolvable.
    """
    import uuid

    from app.auth.system_users import ensure_memory_curator_user
    from src.repositories import agents_repo

    owner = ensure_memory_curator_user()
    repo = agents_repo()
    existing = repo.get_by_slug(owner["id"], MEMORY_CURATOR_AGENT_SLUG)
    if existing is not None:
        return existing

    agent_id = str(uuid.uuid4())
    repo.create(
        id=agent_id,
        owner_user_id=owner["id"],
        name=MEMORY_CURATOR_AGENT_NAME,
        slug=MEMORY_CURATOR_AGENT_SLUG,
        description=MEMORY_CURATOR_AGENT_DESCRIPTION,
        system_prompt=DEFAULT_DETECTION_POLICY,
        # This profile is never RUN (no chat/runtime spawn), so the scope
        # axes are immaterial — 'selected' with an empty scope mirrors the
        # API-created-agent default (app/api/agents_admin.py::create_agent)
        # rather than 'all', so it can never accidentally inherit the
        # owner's full stack if the runtime path is ever wired later.
        plugins_mode="selected",
        connections_mode="selected",
        tables_mode="selected",
        memory_mode="selected",
        memory_write_mode="off",
        status="ready",
    )
    logger.info("Seeded memory-curator agent profile: %s", agent_id)
    return repo.get_by_id(agent_id)  # type: ignore[return-value]


def get_memory_curator_policy_text() -> str:
    """The LIVE detection policy text — the profile's current
    ``instructions``/``system_prompt``, or :data:`DEFAULT_DETECTION_POLICY`
    when the profile is missing, un-seeded, or its policy text is blank.

    Never raises: this is the fallback boundary the detector relies on to
    guarantee "never a crash, never an empty policy" — a repo/backend
    hiccup here must degrade to the built-in policy, not take detection
    down with it.
    """
    try:
        from app.auth.system_users import MEMORY_CURATOR_USER_EMAIL
        from src.repositories import agents_repo, users_repo

        owner = users_repo().get_by_email(MEMORY_CURATOR_USER_EMAIL)
        if owner:
            agent: Optional[Dict[str, Any]] = agents_repo().get_by_slug(owner["id"], MEMORY_CURATOR_AGENT_SLUG)
            if agent:
                text = (agent.get("system_prompt") or "").strip()
                if text:
                    return text
    except Exception:
        logger.warning("memory-curator policy lookup failed; using built-in default", exc_info=True)
    return DEFAULT_DETECTION_POLICY
