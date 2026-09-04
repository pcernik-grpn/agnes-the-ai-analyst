"""Shared Microsoft Entra ID group-naming rule.

Two independent writers turn an Entra security/M365 group into an Agnes
``user_groups`` row: the login-time sync
(:mod:`app.auth.microsoft_group_sync`, gated by
``auth.microsoft.group_sync_enabled``) and the SharePoint ACL mirror
(:mod:`connectors.sharepoint.acl_sync`, gated by the ``sharepoint`` switch).
Both must key that row IDENTICALLY — on the group's stable Entra object id,
never on its (mutable, sometimes absent) ``mail``/``displayName`` — or the
same Entra group ends up as two different ``user_groups`` rows that never
converge, silently splitting its membership and grants across both.

:func:`entra_group_name` is that single naming rule, kept in a module
neither writer's domain depends on so importing it never creates a layering
dependency between auth and a specific connector.
"""

from __future__ import annotations


def entra_group_name(object_id: str) -> str:
    """Canonical ``user_groups.name`` for an Entra security/M365 group,
    keyed on its Entra object id (stable, never absent) rather than its
    ``mail``/``displayName`` (mutable, and not every group is mail-enabled).
    """
    return f"entra:{object_id}"
