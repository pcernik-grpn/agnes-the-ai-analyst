"""#2254 — effective-access answers for the AUDIENCE, not for the carrier.

The endpoint used to answer from group membership alone: it read the person's
groups, returned early when there were none, and then labelled every row with
the name of the group whose id it is STORED against. Under the scope model
(`src/grant_scopes.py`) both halves are wrong for an everyone-scoped grant:

* it reaches every account *unconditionally*, including one that belongs to no
  group at all — so the early return answered `items: []` on an instance where
  several grants reached that account;
* it is stored against the seeded carrier group, which the person is typically
  not a member of — so the name lookup missed and the payload printed the
  carrier's raw uuid as the "group" they got the thing through.

`GET /api/admin/access-overview` already answers the second question correctly,
per row, via `src.grant_scopes.reaches_everyone`. Both effective-access
surfaces now go through that same rule, in `grants_reaching_user`.

DuckDB here — the frozen app-state ladder, which has no `scope` column at all,
so an everyone-grant is spelled as a grant on the seeded carrier and reaches
its members and nobody else. That backend's half of this fix is the
ATTRIBUTION; it must not gain REACH the backend cannot express, because a
read-out claiming access `can_access` denies is the same defect as #2254
pointing the other way. The reach half is asserted where the model exists:
`tests/db_pg/test_effective_access_everyone_contract.py` drives both backends
and states what each one answers.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.grant_scopes import EVERYONE_TARGET_ID, EVERYONE_TARGET_LABEL


@pytest.fixture
def client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    for sub in ("state", "analytics", "extracts"):
        (tmp_path / sub).mkdir(exist_ok=True)
    from src.db import close_system_db

    close_system_db()
    yield TestClient(shared_app)
    close_system_db()


def _token(user_id: str, email: str) -> dict:
    from app.auth.jwt import create_access_token

    return {"Authorization": f"Bearer {create_access_token(user_id, email)}"}


@pytest.fixture
def instance(client):
    """An admin, an ordinary member, an account in no group, and one grant
    held by the carrier.

    `member` joins `Everyone` the way every real account does
    (`app.auth.group_sync.ensure_everyone_membership`, called from OAuth first
    sign-in, bootstrap and admin create). `loner` deliberately does not — the
    shape a service account is in, and anyone an admin has taken out of the
    group.
    """
    from app.auth.group_sync import ensure_everyone_membership
    from src.db import get_system_db
    from src.repositories import resource_grants_repo, user_groups_repo, users_repo
    from tests.helpers.auth import grant_admin

    users_repo().create(id="admin1", email="admin@example.com", name="Admin")
    users_repo().create(id="member", email="member@example.com", name="Member")
    users_repo().create(id="loner", email="loner@example.com", name="Loner")
    conn = get_system_db()
    grant_admin(conn, "admin1")

    carrier = user_groups_repo().get_by_name("Everyone")
    assert carrier, "the seeded Everyone carrier must exist"

    resource_grants_repo().create(
        group_id=carrier["id"],
        resource_type="data_package",
        resource_id="pkg_for_everyone",
        assigned_by="admin@example.com",
        scope="everyone",
    )
    ensure_everyone_membership("member", added_by="test")
    return {"carrier_id": carrier["id"]}


class TestTheCarrierIsNeverWhatAPersonIsToldTheyGotItThrough:
    """Symptom 2 of #2254, and the half that is real on this backend. The row
    is stored against the seeded carrier, so the read named that group — a raw
    uuid for anyone outside it, and "Everyone" for anyone in it, which is the
    group the Access page draws as a scope precisely because it is not one
    anybody should be told they are getting things through."""

    def test_the_audience_is_the_scope_with_the_shared_label(self, client, instance):
        r = client.get(
            "/api/admin/users/member/effective-access",
            headers=_token("admin1", "admin@example.com"),
        )
        assert r.status_code == 200
        item = next(i for i in r.json()["items"] if i["resource_id"] == "pkg_for_everyone")
        assert item["via_groups"] == [
            {
                "group_id": EVERYONE_TARGET_ID,
                "group_name": EVERYONE_TARGET_LABEL,
                "kind": "scope",
            }
        ]

    def test_the_carrier_id_is_nowhere_in_the_payload(self, client, instance):
        r = client.get(
            "/api/admin/users/member/effective-access",
            headers=_token("admin1", "admin@example.com"),
        )
        assert instance["carrier_id"] not in r.text

    def test_the_self_read_says_the_same_thing(self, client, instance):
        r = client.get("/api/me/effective-access", headers=_token("member", "member@example.com"))
        item = next(i for i in r.json()["items"] if i["resource_id"] == "pkg_for_everyone")
        assert item["via_groups"][0]["group_name"] == EVERYONE_TARGET_LABEL
        assert instance["carrier_id"] not in r.text


class TestTheReadOutNeverClaimsMoreThanEnforcementGives:
    """The other direction, and the reason the resolver takes REACH from the
    repository instead of deriving its own.

    On this frozen backend an everyone-grant is a grant on the carrier group,
    so an account outside that group does not receive it — `has_grant`, which
    is what `can_access` calls, says so. Teaching the read to add the carrier
    would make the page an admin opens to answer "what can this person reach"
    list something the runtime denies them.
    """

    def test_an_account_outside_the_carrier_is_told_the_truth(self, client, instance):
        from src.repositories import resource_grants_repo

        assert resource_grants_repo().has_grant([], "data_package", "pkg_for_everyone") is False, (
            "this backend's enforcement — the read-out below has to agree with it"
        )

        r = client.get(
            "/api/admin/users/loner/effective-access",
            headers=_token("admin1", "admin@example.com"),
        )
        assert r.status_code == 200
        assert [i["resource_id"] for i in r.json()["items"]] == []


class TestAGroupGrantIsStillAttributedToItsGroup:
    """The other half of the same rule: naming the audience must not blur the
    two kinds of audience together. A group grant keeps the group's id and
    name, and says which kind it is."""

    def test_a_group_row_names_the_group(self, client, instance):
        from src.repositories import resource_grants_repo, user_group_members_repo, user_groups_repo

        finance = user_groups_repo().create(name="Finance", created_by="admin@example.com")
        user_group_members_repo().add_member("member", finance["id"], source="admin")
        resource_grants_repo().create(
            group_id=finance["id"],
            resource_type="data_package",
            resource_id="pkg_for_finance",
            assigned_by="admin@example.com",
        )

        r = client.get(
            "/api/admin/users/member/effective-access",
            headers=_token("admin1", "admin@example.com"),
        )
        item = next(i for i in r.json()["items"] if i["resource_id"] == "pkg_for_finance")
        assert item["via_groups"] == [{"group_id": finance["id"], "group_name": "Finance", "kind": "group"}]

    def test_a_resource_reached_both_ways_names_both_audiences(self, client, instance):
        """The shape #2255 was filed on: one package, an everyone-scoped grant
        and a group-scoped one. Two audiences on one row — never two rows, and
        never one of them dropped."""
        from src.repositories import resource_grants_repo, user_group_members_repo, user_groups_repo

        team = user_groups_repo().create(name="Team", created_by="admin@example.com")
        user_group_members_repo().add_member("member", team["id"], source="admin")
        resource_grants_repo().create(
            group_id=team["id"],
            resource_type="data_package",
            resource_id="pkg_for_everyone",
            assigned_by="admin@example.com",
        )

        r = client.get(
            "/api/admin/users/member/effective-access",
            headers=_token("admin1", "admin@example.com"),
        )
        rows = [i for i in r.json()["items"] if i["resource_id"] == "pkg_for_everyone"]
        assert len(rows) == 1
        assert {v["group_name"] for v in rows[0]["via_groups"]} == {EVERYONE_TARGET_LABEL, "Team"}


class TestThePersonPageDoesNotSayTheOppositeOfTheReadout:
    """The endpoint's answer is rendered on `/admin/users/{id}`, and two
    strings on that page contradicted it once the scope model landed (#2254).

    Source guards rather than a render: both live in the page's script, which
    no test can execute — and both are wrong the same way, by treating group
    membership as the whole of what an account can reach.
    """

    def _page(self) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_user_detail.html").read_text(
            encoding="utf-8"
        )

    def test_no_group_does_not_mean_nothing_reaches_them(self):
        """ "Not in any group — Which means no data, no plugins and no memory
        reach this account yet" is false under the scope model, on the page an
        admin opens to answer "what can this person reach", directly above a
        read-out that now lists what they reach."""
        page = self._page()
        assert "no data, no plugins and no memory reach this account yet" not in page
        assert "Not in any group" in page  # still the heading; only the claim changed

    def test_the_scope_row_does_not_link_to_a_group_that_is_not_one(self):
        """`?group=` selects a GROUP. The everyone audience's id is a sentinel
        (`EVERYONE_TARGET_ID`), so carrying it there selects nothing at all."""
        page = self._page()
        assert 'g.kind === "scope"' in page
