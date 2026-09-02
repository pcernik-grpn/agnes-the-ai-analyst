"""Plugin visibility on `/api/knowledge/search`, asked through the endpoint.

`#2019` taught global search to find an installed plugin by name. That opened
an authorization question the search-layer tests cannot answer: `unified_search`
takes the caller's plugin list as an ARGUMENT, so a unit test that passes
`plugins=[]` proves the function returns nothing when handed nothing — never
that the ENDPOINT withholds a plugin the caller was not granted. The gate lives
in `app.api.knowledge_search._accessible_plugins`, and only a request exercises
it.

Written through a NON-ADMIN caller on purpose. The Admin group short-circuits
every authorization check in this codebase (`app.auth.access.is_user_admin`),
so the same assertions written with `admin_token` pass no matter what the gate
does — the failure mode the repository's visibility-testing rule exists to
catch. The admin case appears here only as the control that proves the
ungranted plugin is findable at all, so the analyst's not seeing it is
authorization rather than a search miss.
"""

from __future__ import annotations

GRANTED = "quarterly-revenue-briefing"
UNGRANTED = "quarterly-revenue-forecasting"
MARKETPLACE = "vis-mp"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_two_plugins_grant_one() -> None:
    """Register both plugins in one marketplace, grant only ``GRANTED``.

    Both carry the same distinctive words so a single query reaches both — the
    point is that one is filtered by the grant, not by relevance.

    ``seeded_app`` seeds only ``admin1`` into a group, so ``analyst1`` needs an
    explicit membership row (there is no implicit "everyone is in Everyone"),
    mirroring ``tests/test_chat_capability_snapshot.py::_grant_plugin_to_analyst``.
    """
    from src.db import get_system_db
    from src.repositories import (
        marketplace_plugins_repo,
        marketplace_registry_repo,
        resource_grants_repo,
        user_groups_repo,
    )
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    try:
        marketplace_registry_repo().register(
            id=MARKETPLACE, name=MARKETPLACE, url=f"https://example.test/{MARKETPLACE}.git"
        )
        marketplace_plugins_repo().replace_for_marketplace(
            MARKETPLACE,
            [
                {"name": GRANTED, "version": "1.0", "description": "quarterly revenue briefing pack"},
                {"name": UNGRANTED, "version": "1.0", "description": "quarterly revenue forecasting pack"},
            ],
        )
        everyone = user_groups_repo().get_by_name("Everyone")
        assert everyone is not None, "system groups are seeded by _ensure_schema"
        try:
            UserGroupMembersRepository(conn).add_member("analyst1", everyone["id"], source="test")
        except Exception:
            pass  # already a member
        resource_grants_repo().create(
            group_id=everyone["id"],
            resource_type="marketplace_plugin",
            resource_id=f"{MARKETPLACE}/{GRANTED}",
            requirement="available",
        )
    finally:
        conn.close()


def _plugin_names(seeded_app, token: str) -> set[str]:
    r = seeded_app["client"].get(
        "/api/knowledge/search", params={"q": "quarterly revenue", "k": 50}, headers=_auth(token)
    )
    assert r.status_code == 200, r.text
    return {h.get("name") or h.get("id") for h in r.json().get("results", []) if h.get("type") == "plugin"}


def test_a_non_admin_sees_a_granted_plugin_and_not_an_ungranted_one(seeded_app, monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _seed_two_plugins_grant_one()

    names = _plugin_names(seeded_app, seeded_app["analyst_token"])
    assert GRANTED in names, f"a granted plugin must be findable by name; got {names}"
    assert UNGRANTED not in names, (
        f"an ungranted plugin leaked to a non-admin caller; got {names} — "
        "the endpoint's grant filter is what must withhold it, not relevance"
    )


def test_the_ungranted_plugin_is_findable_for_someone(seeded_app, monkeypatch, tmp_path):
    """The control. Without it, the assertion above passes just as well when
    the query matches nothing at all, and would keep passing if plugin search
    broke entirely."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _seed_two_plugins_grant_one()

    names = _plugin_names(seeded_app, seeded_app["admin_token"])
    assert {GRANTED, UNGRANTED} <= names, (
        f"admin (god-mode) must see both, proving the query reaches them; got {names}"
    )
