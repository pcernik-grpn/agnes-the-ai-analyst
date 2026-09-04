"""WHO a ``resource_grants`` row reaches — the closed set, once.

``Everyone`` used to be a group. It normally held every account, but when
``AGNES_GROUP_EVERYONE_EMAIL`` was set it was mirrored from a Workspace group
instead — so the word named two different things, and on a mirrored instance
two different *sets of people*. ``marketplace_plugins.is_system`` was a third
spelling of the same idea, and unconditionally every account, so on a mirrored
instance the two disagreed about who "everyone" was.

A grant now says which it means, in one column:

- ``None`` — the members of ``group_id``. Every grant written before 0097.
- ``EVERYONE`` — every account on the instance, unconditionally, including
  accounts that belong to no group at all.

An ``EVERYONE`` row keeps its ``group_id`` pointed at the carrier group (see
:func:`carrier_group_id`) and that column is ignored on read; 0097's docstring
has the reasoning. Postgres-only, like the column it describes.

``EVERYONE_TARGET_ID`` is the other half. The sharing surfaces already
special-cased Everyone — they carried an ``is_everyone`` boolean beside a
group id through the sharing API into four templates. They keep the boolean;
what changes is that the id beside it is this sentinel rather than a real
group's uuid, so "everyone" stops being a row in a list of audiences and
becomes a choice a caller makes.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional


#: Stored in ``resource_grants.scope``. The only non-NULL value.
EVERYONE = "everyone"

#: What a grant with no scope means. Never stored — NULL is.
THIS_GROUP = None

#: Every accepted value of the column, NULL included.
VALID_SCOPES: tuple[Optional[str], ...] = (THIS_GROUP, EVERYONE)

#: The id a sharing/API surface sends in place of a group id when the target
#: is everyone. Not a group id, and deliberately not shaped like one — a
#: caller that treats it as one gets a lookup miss rather than a silent hit
#: on some real group.
EVERYONE_TARGET_ID = "everyone"

#: The label the sharing surfaces print for it. One place, so the Library
#: dialog, the package detail and the skills picker cannot drift.
EVERYONE_TARGET_LABEL = "Everyone (workspace)"


#: Resource types for which "give this to everyone" is NOT offered, and for
#: which migration 0098 therefore does not write a scope. Each is withheld
#: for its own reason (the effort's ticket 07), and none of them is a
#: disabled control — the choice is simply absent:
#:
#: - ``slack_channel`` — the grantee is a CHANNEL, not an audience. A row on
#:   the seeded ``Everyone`` group is how a channel is marked open at all
#:   (``services/slack_bot/binding.is_channel_allowlisted``), so "everyone"
#:   would be answering a question nobody asked.
#: - ``table`` — a grant here sets a ceiling for agent scoping and grants no
#:   analyst visibility at all; analyst access runs through data packages.
#: - ``memory_domain`` — a grant is ADDITIVE (it reveals, it does not
#:   restrict), so revealing to everyone is the same as no restriction.
#: - ``memory_item`` — a per-group override of an item's Required flag,
#:   which has no meaning without a group to override for.
SCOPE_WITHHELD_TYPES: frozenset[str] = frozenset(
    {"slack_channel", "table", "memory_domain", "memory_item"}
)


def takes_everyone_scope(resource_type: str) -> bool:
    """Whether "give this to everyone" is a coherent choice for this type.

    Allow-by-default: a type means "this audience gets the thing" unless
    :data:`SCOPE_WITHHELD_TYPES` says otherwise. A new resource type is far
    more likely to be an ordinary grantable thing than one of the four
    special cases, and a wrong default here is visible (an offered choice
    that reads oddly) rather than silent (a missing one nobody notices).
    """
    return resource_type not in SCOPE_WITHHELD_TYPES


def normalize(scope: Optional[str]) -> Optional[str]:
    """Coerce a caller's value to a storable one, or raise.

    Empty string and whitespace normalize to ``None`` (this group) rather
    than raising: they arrive from form posts and JSON nulls round-tripped
    through a query string, and both plainly mean "no scope given".
    """
    if scope is None:
        return None
    cleaned = scope.strip().lower()
    if not cleaned:
        return None
    if cleaned not in (EVERYONE,):
        raise ValueError(f"scope must be None or {EVERYONE!r}, got {scope!r}")
    return cleaned


def is_everyone(row: Mapping[str, Any]) -> bool:
    """Whether a grant row reaches every account.

    Takes the row rather than the value so a DuckDB row — which has no
    ``scope`` key at all, because that ladder is frozen (A3) — answers
    False instead of raising.
    """
    return (row.get("scope") or None) == EVERYONE


def reaches_everyone(row: Mapping[str, Any], carrier_id: Optional[str] = None) -> bool:
    """Whether a grant row reaches every account, on EITHER backend.

    :func:`is_everyone` reads the column, which only Postgres has. On the
    frozen DuckDB ladder an everyone-grant is stored against the carrier
    group and is indistinguishable from an ordinary grant on it — so a
    grant held by the carrier is the same statement, spelled the only way
    that backend can spell it.

    Pass ``carrier_id`` when checking many rows; it costs a group lookup
    each time otherwise.
    """
    if is_everyone(row):
        return True
    if carrier_id is None:
        carrier_id = carrier_group_id()
    return carrier_id is not None and row.get("group_id") == carrier_id


def carrier_group_id() -> Optional[str]:
    """The ``group_id`` an everyone-scoped grant is stored against.

    The seeded ``Everyone`` group. Not because the group decides anything any
    more — it does not, the scope does — but because ``group_id`` is a NOT
    NULL FK and every everyone-grant using the *same* carrier is what keeps
    ``UNIQUE (group_id, resource_type, resource_id)`` doing useful work: one
    everyone-grant per resource, enforced by the index rather than by a
    read-modify-write.

    ``None`` when the group is missing, which a caller must treat as "cannot
    write an everyone-grant" rather than substituting a group of its own.
    """
    from src.db import SYSTEM_EVERYONE_GROUP
    from src.repositories import user_groups_repo

    try:
        row = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
    except Exception:  # noqa: BLE001 - a missing carrier is not a crash
        return None
    return row["id"] if row else None
