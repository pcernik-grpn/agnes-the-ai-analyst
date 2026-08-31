"""The repair for `data_app` grants written with an app's row id.

A ``data_app`` grant is keyed by SLUG — ``ResourceTypeSpec`` declares
``id_format="<slug>"``, ``_can_view`` calls ``can_access(…, row["slug"])``, and
the Library's apps band tests ``da["slug"] in granted_ids``. The apps builder's
publish sent ``app_<hex>`` instead, so it wrote grant rows nothing could read:
publishing reported success and the granted group still saw no apps. The write
side is fixed in ``linked_apps_panel.js``; this covers the rows already written.

Asserted through ``_can_view`` rather than by counting rows, because "the grant
row exists" was exactly the thing that looked fine while being broken — the row
was always there, on the wrong key.
"""

from __future__ import annotations

import uuid

import pytest


@pytest.fixture
def grant_env(e2e_env, monkeypatch):
    """Real app/user/group/grant rows on a per-test DATA_DIR."""
    from src.repositories import (
        data_apps_repo,
        resource_grants_repo,
        user_group_members_repo,
        user_groups_repo,
        users_repo,
    )

    users, groups, members = users_repo(), user_groups_repo(), user_group_members_repo()
    apps, grants = data_apps_repo(), resource_grants_repo()

    uid = "u_" + uuid.uuid4().hex[:10]
    users.create(id=uid, email="analyst@example.com", name="Analyst")
    user = users.get_by_id(uid)

    groups.create("Finance")
    group = groups.get_by_name("Finance")
    members.add_member(user_id=uid, group_id=group["id"], source="admin")

    made = {}
    for slug, name in (("sales-dash", "Sales Dash"), ("ops-dash", "Ops Dash"), ("hr-dash", "HR Dash")):
        # owner_user_id='system' — a linked app is owned by nobody, so a grant
        # is the ONLY thing that can make it visible. An owned app would pass
        # `_can_view` on ownership and hide the bug.
        apps.create_linked(
            slug=slug,
            name=name,
            external_url=f"https://apps.example.com/{slug}",
            source_ref=f"src:{slug}",
            owner_user_id="system",
        )
        made[slug] = apps.get_by_slug(slug)

    return {"user": user, "group": group, "apps": made, "grants": grants, "apps_repo": apps}


def _sees(user, row) -> bool:
    from app.api.data_apps import _can_view

    return _can_view(user, row)


def test_an_id_keyed_grant_makes_nothing_visible(grant_env):
    """The bug itself, so the repair below is measured against a real failure
    rather than an assumed one."""
    g, app = grant_env["grants"], grant_env["apps"]["sales-dash"]
    g.ensure_grant(grant_env["group"]["id"], "data_app", app["id"])
    assert g.list_all(resource_type="data_app"), "the grant row exists…"
    assert _sees(grant_env["user"], app) is False, "…and grants nothing"


def test_repair_rekeys_to_the_slug_and_restores_visibility(grant_env):
    from scripts.repair_data_app_grant_keys import repair

    g, apps = grant_env["grants"], grant_env["apps"]
    gid = grant_env["group"]["id"]
    g.ensure_grant(gid, "data_app", apps["sales-dash"]["id"])
    g.ensure_grant(gid, "data_app", apps["ops-dash"]["id"])

    assert repair(dry_run=False) == 2
    for slug in ("sales-dash", "ops-dash"):
        assert _sees(grant_env["user"], apps[slug]) is True, f"{slug} still invisible"
    # An app nobody granted stays invisible — the repair widens nothing.
    assert _sees(grant_env["user"], apps["hr-dash"]) is False


def test_dry_run_changes_nothing(grant_env):
    from scripts.repair_data_app_grant_keys import repair

    g, app = grant_env["grants"], grant_env["apps"]["sales-dash"]
    g.ensure_grant(grant_env["group"]["id"], "data_app", app["id"])

    assert repair(dry_run=True) == 1, "dry run should still report what it found"
    assert _sees(grant_env["user"], app) is False, "dry run must not repair anything"
    assert [r["resource_id"] for r in g.list_all(resource_type="data_app")] == [app["id"]]


def test_a_correct_grant_alongside_the_broken_one_is_not_duplicated(grant_env):
    """Both surfaces published, or someone granted by hand after the fact: the
    dead row is dropped rather than becoming a second grant on the same pair."""
    from scripts.repair_data_app_grant_keys import repair

    g, app = grant_env["grants"], grant_env["apps"]["sales-dash"]
    gid = grant_env["group"]["id"]
    g.ensure_grant(gid, "data_app", app["id"])
    g.ensure_grant(gid, "data_app", app["slug"])

    assert repair(dry_run=False) == 1
    rows = g.list_all(resource_type="data_app")
    assert [r["resource_id"] for r in rows] == [app["slug"]]
    assert len({(r["group_id"], r["resource_id"]) for r in rows}) == len(rows)
    assert _sees(grant_env["user"], app) is True


def test_a_grant_naming_no_known_app_is_left_alone(grant_env):
    """It cannot be re-keyed — there is no slug to re-key it to — and deleting
    it would destroy the only record of intent if the app is re-linked."""
    from scripts.repair_data_app_grant_keys import repair

    g = grant_env["grants"]
    g.ensure_grant(grant_env["group"]["id"], "data_app", "app_gone0000")

    assert repair(dry_run=False) == 0
    assert [r["resource_id"] for r in g.list_all(resource_type="data_app")] == ["app_gone0000"]


def test_a_slug_keyed_grant_is_never_touched(grant_env):
    from scripts.repair_data_app_grant_keys import repair

    g, app = grant_env["grants"], grant_env["apps"]["sales-dash"]
    g.ensure_grant(grant_env["group"]["id"], "data_app", app["slug"])

    assert repair(dry_run=False) == 0
    assert [r["resource_id"] for r in g.list_all(resource_type="data_app")] == [app["slug"]]
    assert _sees(grant_env["user"], app) is True


def test_repair_is_idempotent(grant_env):
    from scripts.repair_data_app_grant_keys import repair

    g, app = grant_env["grants"], grant_env["apps"]["sales-dash"]
    g.ensure_grant(grant_env["group"]["id"], "data_app", app["id"])

    assert repair(dry_run=False) == 1
    assert repair(dry_run=False) == 0, "a second run must find nothing to do"
    assert _sees(grant_env["user"], app) is True
