"""The grant audit trail names the group the row actually landed on.

An everyone-scoped grant does not live on the group the caller named. The
repository redirects it onto the CARRIER (``_carrier_or``) because the scope
needs somewhere to sit and ``group_id`` is ignored on read for those rows.
``POST /api/admin/grants`` used to audit the caller's ``group_id`` anyway, so
``resource_grant.created`` named a group that never held the grant — while
``resource_grant.deleted``, which reads the stored row, named the carrier. The
two halves of one grant's trail disagreed about the same grant.

Parametrized over both backends on purpose. The scope column is Postgres-only,
so on DuckDB the scope is dropped and the caller's group IS the stored group —
which is exactly why the assertion is written against the STORED row rather
than against a hard-coded carrier: it is the same invariant on both, and it
cannot drift if the carrier rule changes.
"""

from __future__ import annotations

import json


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _created_audit_params(grant_id: str) -> dict:
    from src.repositories import audit_repo

    rows = audit_repo().query_for_resources([f"grant:{grant_id}"], limit=50)
    created = [r for r in rows if r["action"] == "resource_grant.created"]
    assert created, f"no resource_grant.created audit row for grant:{grant_id}"
    params = created[0].get("params")
    return json.loads(params) if isinstance(params, str) else (params or {})


def _make_group(client, headers, name: str) -> str:
    resp = client.post(
        "/api/admin/groups",
        json={"name": name, "description": "audit trail test"},
        headers=headers,
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def test_everyone_scoped_grant_audits_the_stored_group(seeded_app_both):
    """The audited group_id is the one the repository wrote, not the payload's."""
    c = seeded_app_both["client"]
    h = _auth(seeded_app_both["admin_token"])
    gid = _make_group(c, h, "Audit Trail Everyone")

    created = c.post(
        "/api/admin/grants",
        json={
            "group_id": gid,
            "resource_type": "marketplace_plugin",
            "resource_id": "audit-mp/everyone-plugin",
            "requirement": "available",
            "scope": "everyone",
        },
        headers=h,
    )
    assert created.status_code in (200, 201), created.text
    grant_id = created.json()["id"]

    listing = c.get("/api/admin/grants", headers=h).json()
    rows_all = listing["grants"] if isinstance(listing, dict) else listing
    stored = next(r for r in rows_all if r["id"] == grant_id)
    params = _created_audit_params(grant_id)

    # The invariant: the trail names the row, on either backend.
    assert params["group_id"] == stored["group_id"], (
        f"audit named {params['group_id']!r} but the row landed on "
        f"{stored['group_id']!r} (backend={seeded_app_both['backend']})"
    )
    assert params["scope"] == "everyone"

    # And the delete half agrees with the create half, which is the symptom
    # an operator actually hits when reading one grant's history.
    assert c.delete(f"/api/admin/grants/{grant_id}", headers=h).status_code in (200, 204)
    from src.repositories import audit_repo

    rows = audit_repo().query_for_resources([f"grant:{grant_id}"], limit=50)
    deleted = [r for r in rows if r["action"] == "resource_grant.deleted"]
    if deleted:  # not every backend/path writes params on delete
        d = deleted[0].get("params")
        d = json.loads(d) if isinstance(d, str) else (d or {})
        if "group_id" in d:
            assert d["group_id"] == params["group_id"]


def test_plain_group_grant_still_audits_its_own_group(seeded_app_both):
    """The unscoped case is untouched: the caller's group IS the stored one."""
    c = seeded_app_both["client"]
    h = _auth(seeded_app_both["admin_token"])
    gid = _make_group(c, h, "Audit Trail Plain")

    created = c.post(
        "/api/admin/grants",
        json={
            "group_id": gid,
            "resource_type": "marketplace_plugin",
            "resource_id": "audit-mp/plain-plugin",
            "requirement": "available",
        },
        headers=h,
    )
    assert created.status_code in (200, 201), created.text
    grant_id = created.json()["id"]

    params = _created_audit_params(grant_id)
    assert params["group_id"] == gid
    assert params["scope"] is None
