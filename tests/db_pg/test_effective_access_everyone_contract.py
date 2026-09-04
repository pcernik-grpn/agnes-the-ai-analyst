"""Cross-engine contract for the effective-access read (#2254).

`src.grant_scopes.grants_reaching_user` is the one resolver behind both
effective-access surfaces, and the two backends spell an everyone-grant
differently: Postgres carries `scope='everyone'` on the row and reaches every
account with it, while the frozen DuckDB ladder has no such column and the
same statement is a grant held by the seeded carrier group, reaching its
members. The resolver takes REACH from the repository for exactly that reason
and only ATTRIBUTES on top of it — so what is pinned here is both halves: the
attribution is identical on the two backends, and the reach is each backend's
own, stated rather than papered over.

Driven through `seeded_app_both` rather than the repos directly because the
defect was in the reader above them: the repositories already reached an
account in no group on Postgres (`tests/db_pg/test_rbac_contract.py::
test_grant_scope_accepted_by_both_backends_and_surfaced_by_pg` pins
`has_grant([], …) is True`), and the endpoint answered `items: []` for that
same account anyway.
"""

from __future__ import annotations

import pytest

from src.grant_scopes import EVERYONE_TARGET_ID, EVERYONE_TARGET_LABEL


@pytest.fixture
def instance(seeded_app_both):
    """One everyone-scoped grant, one grant on a group nobody here is in, and
    two accounts: `analyst1` in the carrier (what every real account is), and
    `loner` in no group at all (a service account, or anyone an admin has
    taken out of it)."""
    from app.auth.group_sync import ensure_everyone_membership
    from src.repositories import resource_grants_repo, user_groups_repo, users_repo

    carrier = user_groups_repo().get_by_name("Everyone")
    assert carrier, "the seeded carrier group must exist on both backends"
    others = user_groups_repo().create(name="Not-Theirs", created_by="admin@test.com")

    users_repo().create(id="loner", email="loner@test.com", name="Loner")
    ensure_everyone_membership("analyst1", added_by="test")

    # `marketplace_plugin` rather than a data package: Postgres puts a real FK
    # on the five typed grant columns (migration 0013), so a package id has to
    # exist before a grant may name it — and what is under test here is the
    # audience, not the referent. It is also the type the scope was introduced
    # for, since a plugin reaching every account is what `is_system` used to
    # mean before 0098 turned it into an everyone-scoped grant.
    grants = resource_grants_repo()
    grants.create(
        group_id=carrier["id"],
        resource_type="marketplace_plugin",
        resource_id="acme/for-everyone",
        assigned_by="admin@test.com",
        scope="everyone",
    )
    grants.create(
        group_id=others["id"],
        resource_type="marketplace_plugin",
        resource_id="acme/for-others",
        assigned_by="admin@test.com",
    )
    return {"carrier_id": carrier["id"], "other_group_id": others["id"]}


def _items(seeded_app_both, path: str, token_key: str) -> list:
    r = seeded_app_both["client"].get(path, headers={"Authorization": f"Bearer {seeded_app_both[token_key]}"})
    assert r.status_code == 200, r.text
    return r.json()["items"]


def test_the_audience_is_the_scope_and_never_the_carrier_on_both_backends(seeded_app_both, instance):
    """The attribution half — identical on both backends, which is the whole
    point of resolving it in one place."""
    items = _items(seeded_app_both, "/api/admin/users/analyst1/effective-access", "admin_token")
    item = next(i for i in items if i["resource_id"] == "acme/for-everyone")

    assert item["via_groups"] == [
        {"group_id": EVERYONE_TARGET_ID, "group_name": EVERYONE_TARGET_LABEL, "kind": "scope"}
    ]
    # A grant on a group they are not in stays out of the answer either way.
    assert "acme/for-others" not in {i["resource_id"] for i in items}


def test_an_account_in_no_group_gets_its_own_backends_answer(seeded_app_both, instance):
    """The reach half — and the one question the two backends answer
    differently, because only one of them can express it.

    Postgres: an everyone-scoped grant reaches the account unconditionally,
    which is the case the group model could not state and the case #2254 was
    filed on. DuckDB: the same grant is a row on the carrier group, so an
    account outside it is not reached — and the read-out must say so, since a
    page claiming access `can_access` denies is the same defect the other way
    round. Both are asserted against the repository's own answer, so the read
    and the enforcement cannot drift apart on either engine.
    """
    from src.repositories import resource_grants_repo

    reached = resource_grants_repo().has_grant([], "marketplace_plugin", "acme/for-everyone")
    assert reached is (seeded_app_both["backend"] == "pg")

    items = _items(seeded_app_both, "/api/admin/users/loner/effective-access", "admin_token")
    got = {i["resource_id"] for i in items}
    assert ("acme/for-everyone" in got) is reached

    if reached:
        item = next(i for i in items if i["resource_id"] == "acme/for-everyone")
        assert item["via_groups"] == [
            {"group_id": EVERYONE_TARGET_ID, "group_name": EVERYONE_TARGET_LABEL, "kind": "scope"}
        ]


def test_the_self_read_agrees_with_the_admin_read_on_both_backends(seeded_app_both, instance):
    mine = _items(seeded_app_both, "/api/me/effective-access", "analyst_token")
    theirs = _items(seeded_app_both, "/api/admin/users/analyst1/effective-access", "admin_token")

    def shape(items):
        return {(i["resource_type"], i["resource_id"]): i["via_groups"] for i in items}

    assert shape(mine) == shape(theirs)
