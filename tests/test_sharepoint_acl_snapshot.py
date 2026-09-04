"""Unit tests for the SharePoint ACL-snapshot pure functions (TCRD-296 gap
#79 — permissions captured as metadata, independent of ``access_mode``):
``snapshot_principals`` (raw Graph permission -> informational principal
rows), ``summarize_snapshot_principals`` and ``aggregate_acl_snapshot``.

Deliberately disjoint from ``test_sharepoint_acl_classify.py``: this feature
shows EVERY grantee SharePoint reports, including ones
``classify_permissions`` would mark ``unhonored`` — the two must never
disagree about how many principals a permission list carries, but they answer
different questions (see the module docstring's "Permissions snapshot"
section).
"""

from __future__ import annotations

from connectors.sharepoint.acl_sync import (
    _has_any_scopes,
    _mirrored_scopes,
    _snapshot_only_scopes,
    aggregate_acl_snapshot,
    snapshot_principals,
    summarize_snapshot_principals,
)


def _perm(**grantee):
    return {"id": "p", "roles": ["read"], **grantee}


def test_entra_group_and_direct_user_are_direct():
    rows = snapshot_principals(
        [
            _perm(grantedToV2={"group": {"id": "g-1", "displayName": "Finance"}}),
            _perm(grantedToV2={"user": {"id": "u-1", "email": "a@example.com", "displayName": "Alice"}}),
        ]
    )
    assert rows[0] == {
        "principal_kind": "entra_group",
        "principal_id": "g-1",
        "display_name": "Finance",
        "roles": ["read"],
        "via": "direct",
    }
    assert rows[1] == {
        "principal_kind": "user",
        "principal_id": "a@example.com",
        "display_name": "Alice",
        "roles": ["read"],
        "via": "direct",
    }


def test_unmapped_site_group_still_appears_in_snapshot():
    """`classify_permissions` would mark this `unhonored: site_group` — the
    snapshot shows it anyway (informational, independent of honoring)."""
    rows = snapshot_principals([_perm(grantedToV2={"siteGroup": {"id": "3", "displayName": "Members"}})])
    assert rows == [
        {
            "principal_kind": "site_group",
            "principal_id": "3",
            "display_name": "Members",
            "roles": ["read"],
            "via": "direct",
        }
    ]


def test_site_user_resolves_email_from_claims_login_name():
    rows = snapshot_principals(
        [_perm(grantedToV2={"siteUser": {"id": "su-1", "loginName": "i:0#.f|membership|bob@example.com"}})]
    )
    assert rows[0]["principal_kind"] == "site_user"
    assert rows[0]["principal_id"] == "bob@example.com"


def test_anonymous_and_organization_links():
    rows = snapshot_principals(
        [
            _perm(link={"scope": "anonymous"}),
            _perm(link={"scope": "organization"}),
        ]
    )
    assert [r["principal_kind"] for r in rows] == ["link_anonymous", "link_organization"]
    assert all(r["via"] == "link" for r in rows)


def test_specific_people_link_expands_each_identity():
    rows = snapshot_principals(
        [
            _perm(
                link={"scope": "users"},
                grantedToIdentitiesV2=[
                    {"user": {"id": "u9", "email": "b@example.com", "displayName": "Bob"}},
                    {"group": {"id": "g-9", "displayName": "Contractors"}},
                ],
            )
        ]
    )
    assert len(rows) == 2
    assert rows[0] == {
        "principal_kind": "user",
        "principal_id": "b@example.com",
        "display_name": "Bob",
        "roles": ["read"],
        "via": "link",
    }
    assert rows[1]["principal_kind"] == "entra_group"


def test_specific_people_link_with_no_identities_falls_back_to_link_people():
    rows = snapshot_principals([_perm(link={"scope": "users"}, grantedToIdentitiesV2=[])])
    assert rows == [
        {
            "principal_kind": "link_people",
            "principal_id": "p",
            "display_name": "Specific people (link)",
            "roles": ["read"],
            "via": "link",
        }
    ]


def test_application_and_unknown_grantees():
    rows = snapshot_principals(
        [
            _perm(grantedToV2={"application": {"id": "app1", "displayName": "Some App"}}),
            _perm(grantedToV2={}),
        ]
    )
    assert rows[0]["principal_kind"] == "application"
    assert rows[1]["principal_kind"] == "unknown"


def test_inherited_from_marks_via_inherited():
    rows = snapshot_principals(
        [_perm(grantedToV2={"user": {"id": "u", "email": "a@example.com"}}, inheritedFrom={"id": "parent"})]
    )
    assert rows[0]["via"] == "inherited"


def test_summarize_counts_by_kind():
    rows = snapshot_principals(
        [
            _perm(grantedToV2={"group": {"id": "g-1"}}),
            _perm(grantedToV2={"group": {"id": "g-2"}}),
            _perm(grantedToV2={"user": {"id": "u", "email": "a@example.com"}}),
        ]
    )
    assert summarize_snapshot_principals(rows) == {"entra_group": 2, "user": 1}


def test_aggregate_dedupes_entra_and_site_groups_across_scopes():
    """The SAME Entra group shared on two scopes counts once — but each
    scope with an individual user still counts toward
    folders_with_individual_users (a per-SCOPE tally, not deduped)."""
    scope_a = {
        "source_scope_id": "a",
        "captured_at": "2026-09-01T00:00:00+00:00",
        "principals": [
            {"principal_kind": "entra_group", "principal_id": "g-1", "display_name": "G", "roles": [], "via": "direct"},
            {
                "principal_kind": "user",
                "principal_id": "x@example.com",
                "display_name": "X",
                "roles": [],
                "via": "direct",
            },
        ],
    }
    scope_b = {
        "source_scope_id": "b",
        "captured_at": "2026-09-02T00:00:00+00:00",
        "principals": [
            {"principal_kind": "entra_group", "principal_id": "g-1", "display_name": "G", "roles": [], "via": "direct"},
            {
                "principal_kind": "link_organization",
                "principal_id": "p2",
                "display_name": "People in the organization",
                "roles": [],
                "via": "link",
            },
        ],
    }
    agg = aggregate_acl_snapshot([scope_a, scope_b])
    assert agg == {
        "entra_groups": 1,
        "site_groups": 0,
        "folders_with_org_links": 1,
        "folders_with_individual_users": 1,
        "scopes_captured": 2,
        "captured_at": "2026-09-02T00:00:00+00:00",
    }


def test_aggregate_of_empty_snapshots_is_all_zero():
    assert aggregate_acl_snapshot([]) == {
        "entra_groups": 0,
        "site_groups": 0,
        "folders_with_org_links": 0,
        "folders_with_individual_users": 0,
        "scopes_captured": 0,
        "captured_at": None,
    }


# ---------------------------------------------------------------------------
# Scope-selection helpers — pure dict logic, no DB fixture needed.
# ---------------------------------------------------------------------------


def _connection(scopes):
    return {"config": {"scopes": scopes}}


def test_snapshot_only_scopes_is_the_complement_of_mirrored_scopes():
    conn = _connection(
        [
            {"source_scope_id": "m", "access_mode": "mirrored"},
            {"source_scope_id": "man", "access_mode": "manual"},
            {"source_scope_id": "no-mode-set"},  # default is 'manual'
        ]
    )
    assert [s["source_scope_id"] for s in _mirrored_scopes(conn)] == ["m"]
    assert [s["source_scope_id"] for s in _snapshot_only_scopes(conn)] == ["man", "no-mode-set"]


def test_has_any_scopes_true_for_manual_only_connection():
    """TCRD-296 gap #79: a manual-only connection has nothing to MIRROR but
    still needs its scopes' ACL snapshots captured every run — the nightly
    sweep's inclusion filter must not exclude it."""
    manual_only = _connection([{"source_scope_id": "man", "access_mode": "manual"}])
    assert _has_any_scopes(manual_only) is True
    assert _mirrored_scopes(manual_only) == []

    empty = _connection([])
    assert _has_any_scopes(empty) is False

    no_scopes_key = {"config": {}}
    assert _has_any_scopes(no_scopes_key) is False
