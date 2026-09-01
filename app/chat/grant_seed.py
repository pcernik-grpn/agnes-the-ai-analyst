"""One-time seed of the chat resource grant for the Everyone group.

Chat visibility is gated on an EXPLICIT resource grant
(``app.web.router._compute_can_chat`` reads ``has_explicit_grant``,
deliberately NOT ``can_access`` — admin god-mode does not reveal chat, by
design). A fresh instance deployed with ``chat.enabled: true`` and no
``resource_grants`` row ``(group, chat, chat)`` therefore has a fully
working chat backend that is INVISIBLE to everyone, including admins —
only a hand-typed ``/chat`` URL works. This module seeds that grant once
so a fresh deploy is usable out of the box.

Seeding exactly once is the whole difficulty: ``resource_grants`` has no
provenance column, so "never seeded" and "an admin deliberately revoked
it" look identical in the table. A marker file in the state directory
records that we have seeded, which makes the two distinguishable without
a schema change — the same approach ``app.secrets`` uses for the
generated ``.session_secret`` next to it, and it lives on the persistent
data disk, so it survives container recreates and auto-upgrades.

Deliberately keyed on the marker rather than on "is this the instance's
first boot": an instance that boots with chat disabled and turns it on
later still gets the grant the first time it boots WITH chat enabled,
because nothing was seeded (and no marker written) before that.

The marker alone is not a sufficient gate, though, because it is younger
than the instances it has to reason about: on the FIRST boot after an
upgrade to the build that introduced it, a long-running instance has no
marker either, and is indistinguishable from a fresh deploy by that test
alone. Seeding there would hand chat to every user of an instance whose
admin had deliberately granted it to one group only. So a pre-existing
``(chat, chat)`` grant — for ANY group — is treated as proof that chat
access has already been decided by a human: nothing is seeded, and the
marker is written so the question is settled from then on.

Multi-replica caveat: replicas that do not share the state directory
would each seed once. The insert is idempotent, so the only visible
effect is that a revoked grant could come back on a replica that never
seeded it — acceptable, and impossible in the single-mount deployments
the module provisions.
"""

from __future__ import annotations

import logging
from pathlib import Path

#: What this module is, to `resource_grants.source` (src/grant_sources.py).
GRANT_SOURCE = "chat_seed"

logger = logging.getLogger(__name__)

#: Written next to `.session_secret` on the persistent state volume.
MARKER_NAME = ".chat_grant_seeded"


def _write_marker(marker: Path, why: str) -> None:
    """Record that the seed question is settled, and why.

    Both terminal outcomes write it — "we seeded it" and "an admin had
    already configured chat access" — because both mean the same thing to
    every later boot: do not touch the grant again.
    """
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{why}\n", encoding="utf-8")


def seed_everyone_chat_grant(*, chat_enabled: bool) -> bool:
    """Seed the ``(Everyone, chat, chat)`` grant unless it was seeded before.

    Args:
        chat_enabled: the resolved ``chat.enabled`` value for this instance.
            When false nothing is seeded and no marker is written, so the
            grant is still seeded the first time the instance boots with
            chat turned on.

    Returns:
        ``True`` iff this call seeded the grant, ``False`` otherwise (chat
        disabled, already seeded once, chat access already configured by an
        admin, or the ``Everyone`` group could not be resolved).
    """
    if not chat_enabled:
        return False

    from app.secrets import _state_dir

    marker = _state_dir() / MARKER_NAME
    if marker.exists():
        return False

    from src.db import SYSTEM_EVERYONE_GROUP
    from src.repositories import resource_grants_repo, user_groups_repo

    everyone_group = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
    if not everyone_group:
        # Too early, or a backend where the group seed has not run yet.
        # Leave the marker unwritten so the next boot retries.
        return False

    from app.resource_types import ResourceType

    # An instance that already carries ANY (chat, chat) grant has had its
    # chat access decided by a human — possibly a deliberately narrow
    # rollout to a single group. This is the only signal that separates a
    # genuinely fresh deploy from a long-running instance booting for the
    # first time on the build that introduced the marker, where the marker
    # is absent for both. Record that we are done and never widen it to
    # Everyone.
    if resource_grants_repo().list_all(resource_type=ResourceType.CHAT.value):
        logger.info(
            "Chat grant already configured (%s exists) — not seeding the Everyone grant",
            ResourceType.CHAT.value,
        )
        _write_marker(marker, "pre-existing chat grant found; nothing seeded")
        return False

    resource_grants_repo().ensure_grant(
        group_id=everyone_group["id"],
        resource_type=ResourceType.CHAT.value,
        resource_id="chat",
        assigned_by="app.main:seed_chat_grant",
        source=GRANT_SOURCE,
    )

    _write_marker(marker, "seeded by app.main:seed_chat_grant")
    return True
