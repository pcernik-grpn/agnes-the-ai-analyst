"""Unit tests for the ACL-mirroring classification engine (spec §8.2).

One test per §8.2 table row: honored grantees (direct user email, Entra
group) vs. out-and-counted grantees (site group, sharing link, org link,
anonymous link, external/guest, application, email-less user).
"""

from connectors.sharepoint.acl_sync import classify_permissions


def _perm(**grantee):
    return {"id": "p", "roles": ["read"], **grantee}


def test_user_and_group_honored():
    c = classify_permissions(
        [
            _perm(grantedToV2={"user": {"id": "u", "email": "a@example.com"}}),
            _perm(grantedToV2={"group": {"id": "g-123"}}),
        ]
    )
    assert c.direct_user_emails == ["a@example.com"]
    assert c.entra_group_oids == ["g-123"]
    assert c.unhonored == []


def test_out_rows_fail_closed_and_counted():
    c = classify_permissions(
        [
            _perm(grantedToV2={"siteGroup": {"id": "3", "displayName": "Members"}}),
            _perm(link={"scope": "organization"}, grantedToIdentitiesV2=[]),
            _perm(link={"scope": "anonymous"}),
            _perm(grantedToV2={"user": {"id": "x", "userPrincipalName": "guest_ext#EXT#@t.example"}}),
            _perm(grantedToV2={"application": {"id": "app1"}}),
            _perm(grantedToV2={"user": {"id": "no-mail"}}),  # email-less principal
        ]
    )
    assert c.entra_group_oids == [] and c.direct_user_emails == []
    assert len(c.unhonored) == 6
    kinds = {u["kind"] for u in c.unhonored}
    assert kinds == {
        "site_group",
        "org_link",
        "anonymous_link",
        "external_guest",
        "application",
        "no_email",
    }


def test_specific_people_link_grantees_are_out():
    c = classify_permissions(
        [
            _perm(
                link={"scope": "users"},
                grantedToIdentitiesV2=[{"user": {"id": "u9", "email": "b@example.com"}}],
            ),
        ]
    )
    assert c.direct_user_emails == []
    assert c.unhonored[0]["kind"] == "sharing_link"


def test_email_preference_order_prefers_email_then_mail_then_upn():
    c = classify_permissions(
        [
            _perm(
                grantedToV2={
                    "user": {
                        "id": "u1",
                        "email": "primary@example.com",
                        "mail": "secondary@example.com",
                        "userPrincipalName": "upn@example.com",
                    }
                }
            ),
            _perm(
                grantedToV2={
                    "user": {
                        "id": "u2",
                        "mail": "mailfield@example.com",
                        "userPrincipalName": "upn2@example.com",
                    }
                }
            ),
            _perm(grantedToV2={"user": {"id": "u3", "userPrincipalName": "upnonly@example.com"}}),
        ]
    )
    assert c.direct_user_emails == [
        "primary@example.com",
        "mailfield@example.com",
        "upnonly@example.com",
    ]


def test_dedupe_preserves_order():
    c = classify_permissions(
        [
            _perm(grantedToV2={"group": {"id": "g-1"}}),
            _perm(grantedToV2={"group": {"id": "g-2"}}),
            _perm(grantedToV2={"group": {"id": "g-1"}}),
            _perm(grantedToV2={"user": {"id": "u1", "email": "a@example.com"}}),
            _perm(grantedToV2={"user": {"id": "u2", "email": "a@example.com"}}),
        ]
    )
    assert c.entra_group_oids == ["g-1", "g-2"]
    assert c.direct_user_emails == ["a@example.com"]


def test_group_naming_and_sentinel_constants():
    from connectors.sharepoint.acl_sync import (
        ACL_SYNC_SENTINEL,
        ACL_SYNC_SOURCE,
        direct_group_name,
        entra_group_name,
    )

    assert entra_group_name("g-123") == "entra:g-123"
    assert direct_group_name("scope-1") == "sp-direct:scope-1"
    assert ACL_SYNC_SENTINEL == "system:sharepoint-acl-sync"
    assert ACL_SYNC_SOURCE == "sharepoint_sync"
