"""Where a `resource_grants` row came from — the closed set, once.

A grant records WHO wrote it (``assigned_by``). It did not record WHAT wrote
it, and those are different questions with different answers: marking a plugin
Required on /admin/marketplaces fans a grant out to every group and stamps each
one with the admin who clicked, so N machine-written rows are indistinguishable
from N an admin typed on /admin/access. The page then offers a Revoke that
either fails (a Required plugin) or succeeds and is undone by the next sync.

This module is the vocabulary for the missing half. A CLOSED set rather than
free text, for three reasons: it is greppable, a display label can be attached
to it without parsing prose, and a writer that forgets to declare itself shows
up as ``None`` — visibly unknown — rather than as a plausible sentence nobody
wrote.

``revocable`` is the part that keeps this honest. Not every non-page writer
owns its grants: several SEED a default an admin is expected to override
(``mcp_source_default``, ``chat_seed``), and refusing a revoke there would be
worse than saying nothing. Only sources that genuinely re-assert their grants,
or that another surface controls, are marked non-revocable.

Postgres-only, like the column it describes — see the note on
``resource_grants.source``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class GrantSource:
    """One writer, as the Access page needs to talk about it."""

    #: Stored in `resource_grants.source`. Never rendered.
    key: str
    #: What the reader is told this grant IS ("Required plugin").
    label: str
    #: The surface that owns it, named as the admin nav names it.
    surface: str
    #: Where to go to change it. Empty when this page is the owner.
    href: str
    #: False when revoking here cannot stick — the control is elsewhere, or
    #: the writer re-asserts on its next run.
    revocable: bool = True
    #: One sentence explaining the above, shown on hover.
    reason: str = ""


#: The Access page itself. The default for anything an admin does by hand.
ACCESS_PAGE = "access_page"

GRANT_SOURCES: Dict[str, GrantSource] = {
    ACCESS_PAGE: GrantSource(
        key=ACCESS_PAGE,
        label="Granted here",
        surface="Access",
        href="",
        revocable=True,
    ),
    "marketplace_required": GrantSource(
        key="marketplace_required",
        # The same words the switch on /admin/marketplaces wears. An admin who
        # met "Automatic" on this page and "system" on that one was learning
        # two names for one idea, which is the defect (#1956 items 13 + 14);
        # a third name here would have re-introduced it in the fix.
        label="Automatic for everyone",
        surface="Marketplaces",
        href="/admin/marketplaces",
        revocable=False,
        reason=(
            "Every user gets this plugin without asking. It is set by one "
            "switch on Marketplaces, not by a grant here."
        ),
    ),
    "marketplace_sync": GrantSource(
        key="marketplace_sync",
        label="From a marketplace sync",
        surface="Marketplaces",
        href="/admin/marketplaces",
        revocable=False,
        reason="Written by the nightly marketplace sync, which re-asserts it on its next run.",
    ),
    "sharepoint_wizard": GrantSource(
        key="sharepoint_wizard",
        label="From the SharePoint wizard",
        surface="Data sources",
        href="/admin/data-sources",
        revocable=False,
        reason="The connect wizard owns this scope's collection grants and rewrites them on the next run.",
    ),
    "share_request": GrantSource(
        key="share_request",
        label="Approved share request",
        surface="Submissions",
        href="/admin/store-submissions",
        revocable=True,
        reason="Created when an admin approved a share request. Revoking here is final.",
    ),
    "library_share": GrantSource(
        key="library_share",
        label="Shared by its owner",
        surface="Library",
        href="/library",
        revocable=True,
        reason="The item's owner shared it from the Library. Revoking here overrides that.",
    ),
    "collection_create": GrantSource(
        key="collection_create",
        label="Set when created",
        surface="Library",
        href="/library",
        revocable=True,
        reason="Written when the collection was created. Yours to change.",
    ),
    "mcp_source_default": GrantSource(
        key="mcp_source_default",
        label="Default visibility",
        surface="MCP sources",
        href="/admin/mcp-sources",
        revocable=True,
        reason="A default written when the source was registered — an admin is expected to narrow it.",
    ),
    "skill_contribution": GrantSource(
        key="skill_contribution",
        label="Published skill",
        surface="Library",
        href="/library",
        revocable=True,
        reason="Written when a contributed skill was published.",
    ),
    "chat_seed": GrantSource(
        key="chat_seed",
        label="Seeded default",
        surface="Access",
        href="",
        revocable=True,
        reason="A one-time default so Everyone can use chat. Yours to change.",
    ),
}


def describe(source: Optional[str]) -> Optional[dict]:
    """The row's provenance as the API sends it, or ``None``.

    ``None`` for a grant with no source (every row written before the column
    existed) and for one written on the Access page itself: neither needs the
    page to explain where it came from, and a badge on every row would be
    noise on the common case.

    An UNKNOWN key also returns ``None`` rather than raising. A stale value —
    a writer removed in a later release, a hand-edited row — must not take the
    Access overview down; the grant simply renders as an ordinary one.
    """
    if not source or source == ACCESS_PAGE:
        return None
    spec = GRANT_SOURCES.get(source)
    if spec is None:
        return None
    return {
        "key": spec.key,
        "label": spec.label,
        "surface": spec.surface,
        "href": spec.href,
        "revocable": spec.revocable,
        "reason": spec.reason,
    }
