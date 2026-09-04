"""GET /catalog/t/<table_id> — per-table drill-down (FAI-132 t6).

Closes a coverage gap: prior to this test the route had no behavioral
test, despite being the worst N+1 offender in ``app/web/router.py``
(a per-package ``list_tables`` call inside a ``for p in pkg_repo.list()``
loop). Pins the refactor to ``pkg_repo.list_member_ids_bulk()`` +
``get_accessible_ids(user, DATA_PACKAGE, conn)``: admin god-mode, grant
on a parent package, and 403 for an ungranted analyst.
"""

from __future__ import annotations

import uuid


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_table(table_id: str, name: str) -> None:
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        TableRegistryRepository(conn).register(
            id=table_id,
            name=name,
            source_type="keboola",
            bucket="in.c-test",
            source_table=table_id,
            query_mode="local",
        )
    finally:
        conn.close()


def _make_pkg(slug: str, name: str) -> str:
    from src.db import get_system_db
    from src.repositories.data_packages import DataPackagesRepository

    conn = get_system_db()
    try:
        return DataPackagesRepository(conn).create(
            name=name,
            slug=slug,
            description=f"{name} desc",
            icon="📦",
            color="#fce7f3",
            created_by="test",
        )
    finally:
        conn.close()


def _add_table_to_pkg(pkg_id: str, table_id: str) -> None:
    from src.db import get_system_db
    from src.repositories.data_packages import DataPackagesRepository

    conn = get_system_db()
    try:
        DataPackagesRepository(conn).add_table(pkg_id, table_id, added_by="test")
    finally:
        conn.close()


def _grant_pkg(group_name: str, resource_id: str, requirement: str = "available", users: list[str] | None = None):
    from src.db import get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    try:
        gid_row = conn.execute("SELECT id FROM user_groups WHERE name = ?", [group_name]).fetchone()
        if not gid_row:
            return
        group_id = gid_row[0]
        if users:
            for u in users:
                try:
                    UserGroupMembersRepository(conn).add_member(u, group_id, source="test")
                except Exception:
                    pass
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
            "requirement, assigned_at, assigned_by) "
            "VALUES (?, ?, 'data_package', ?, ?, CURRENT_TIMESTAMP, 'test')",
            [str(uuid.uuid4()), group_id, resource_id, requirement],
        )
    finally:
        conn.close()


class TestCatalogTableDetail:
    def test_unknown_table_returns_404(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/catalog/t/does-not-exist", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 404

    def test_analyst_without_grant_blocked(self, seeded_app):
        table_id = f"t-{uuid.uuid4().hex[:8]}"
        _make_table(table_id, "Locked Table")
        pkg_id = _make_pkg(f"locked-pkg-{uuid.uuid4().hex[:8]}", "Locked Pkg")
        _add_table_to_pkg(pkg_id, table_id)
        c = seeded_app["client"]
        resp = c.get(f"/catalog/t/{table_id}", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403

    def test_analyst_with_grant_on_parent_package_sees_table(self, seeded_app):
        table_id = f"t-{uuid.uuid4().hex[:8]}"
        _make_table(table_id, "Granted Table")
        pkg_id = _make_pkg(f"granted-pkg-{uuid.uuid4().hex[:8]}", "Granted Pkg")
        _add_table_to_pkg(pkg_id, table_id)
        _grant_pkg("Everyone", pkg_id, requirement="available", users=["analyst1"])
        c = seeded_app["client"]
        resp = c.get(f"/catalog/t/{table_id}", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        assert "Granted Table" in resp.text
        assert "Granted Pkg" in resp.text

    def test_admin_sees_table_even_without_parent_package(self, seeded_app):
        table_id = f"t-{uuid.uuid4().hex[:8]}"
        _make_table(table_id, "Orphan Table")
        c = seeded_app["client"]
        resp = c.get(f"/catalog/t/{table_id}", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert "Orphan Table" in resp.text

    def test_admin_sees_correct_parent_packages(self, seeded_app):
        table_id = f"t-{uuid.uuid4().hex[:8]}"
        _make_table(table_id, "Multi Pkg Table")
        pkg1 = _make_pkg(f"pkg-a-{uuid.uuid4().hex[:8]}", "Pkg Alpha")
        pkg2 = _make_pkg(f"pkg-b-{uuid.uuid4().hex[:8]}", "Pkg Beta")
        _add_table_to_pkg(pkg1, table_id)
        _add_table_to_pkg(pkg2, table_id)
        c = seeded_app["client"]
        resp = c.get(f"/catalog/t/{table_id}", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert "Pkg Alpha" in resp.text
        assert "Pkg Beta" in resp.text

    def test_bulk_member_lookup_failure_degrades_not_500(self, seeded_app, monkeypatch):
        """F2 (review): if the bulk membership query raises, the route must
        degrade via the existing logged fail-closed path — 403 for a
        non-admin, 200 for admin — never a 500. The bulk call was moved
        back inside the parent-package try/except so a query failure can't
        bypass the final authorization check."""
        from src.repositories.data_packages import DataPackagesRepository

        def _boom(self, *args, **kwargs):
            raise RuntimeError("bulk membership query exploded")

        monkeypatch.setattr(DataPackagesRepository, "list_member_ids_bulk", _boom)

        table_id = f"t-{uuid.uuid4().hex[:8]}"
        _make_table(table_id, "Degrade Table")
        pkg_id = _make_pkg(f"degrade-pkg-{uuid.uuid4().hex[:8]}", "Degrade Pkg")
        _add_table_to_pkg(pkg_id, table_id)
        _grant_pkg("Everyone", pkg_id, requirement="available", users=["analyst1"])
        c = seeded_app["client"]

        # Non-admin: bulk failure → fail-closed 403 (not 500).
        resp = c.get(f"/catalog/t/{table_id}", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403

        # Admin: god-mode is resolved before the try, so a bulk failure
        # still renders (degraded, empty parent_packages) → 200 (not 500).
        resp = c.get(f"/catalog/t/{table_id}", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200


# ── table access policies (design doc §11; plan Task 13) ───────────────────
#
# Pre-fix, this route leaked in two ways: (a) the profile-derived column
# list (`profile_repo().get(table_id)["columns"]`) is UNFILTERED raw schema
# — computed once at sync time, independent of any policy attached later —
# so a non-admin saw an EXCLUDE'd column's NAME in "What's inside"; (b) the
# schema FALLBACK (when no profile row exists) called `build_schema_uncached`
# directly, which by its own docstring skips both RBAC and the Task 9
# effective-schema override. Both are closed by one `effective_schema(...)`
# filter applied after either source populates `columns`.


def _seed_profile(table_id: str, columns: list[dict]) -> None:
    from src.db import get_system_db
    from src.repositories.profiles import ProfileRepository

    conn = get_system_db()
    try:
        ProfileRepository(conn).save(table_id, {"columns": columns, "row_count": 999})
    finally:
        conn.close()


# Reused fixture: a `server_only` "invoices" table carrying the design doc's
# canonical `SELECT * EXCLUDE (national_id), md5(email) AS email FROM invoices
# WHERE list_contains($user_groups, cost_center)` policy, plus a non-policied
# "products" sibling, `finance_user`/`finance_token` granted both via a data
# package (satisfying this route's OWN package-grant RBAC gate, not just the
# stack check the resolver uses).
from tests.test_access_policy_effective_schema import policied_invoices  # noqa: F401,E402


class TestCatalogTableDetailAccessPolicy:
    """The "What's inside" columns section only renders in the redesign
    template (``catalog_table_detail.html``, rail layout / paper theme) —
    the frozen legacy template dropped that section entirely (see its own
    comment, "PATCH endpoint + columns query still exist for tools that
    read direct API callers"). ``AGNES_UI_LAYOUT=rail`` forces the redesign
    template so these tests exercise real rendered markup rather than
    asserting against a section the default chrome never shows at all.
    """

    def test_non_admin_hides_excluded_column_from_profile_derived_list(self, policied_invoices, monkeypatch):  # noqa: F811
        """(a) — the common real-world case: a stored `table_profiles` row
        exists (every synced table gets one), so the profile-derived branch
        runs, not the schema fallback. Pre-fix this rendered `national_id`
        unconditionally; the sample values ("N-1"/"N-2") were never rendered
        even pre-fix (this route only ever extracted name/type/nullable),
        so their absence is a defensive regression guard, not a pre/post-fix
        delta by itself — the column NAME's absence is the real assertion.
        """
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        _seed_profile(
            "invoices",
            [
                {"name": "id", "type": "VARCHAR", "nullable": True, "sample_values": ["1", "2"]},
                {"name": "national_id", "type": "VARCHAR", "nullable": True, "sample_values": ["N-1", "N-2"]},
                {"name": "cost_center", "type": "VARCHAR", "nullable": True, "sample_values": ["Finance"]},
                {"name": "amount", "type": "VARCHAR", "nullable": True, "sample_values": ["100"]},
            ],
        )
        c = policied_invoices["client"]
        resp = c.get("/catalog/t/invoices", headers=_auth(policied_invoices["finance_token"]))
        assert resp.status_code == 200, resp.text
        assert "national_id" not in resp.text
        assert "N-1" not in resp.text
        # A confirmed-visible column must still render — proves the whole
        # section wasn't just blanked out.
        assert "cost_center" in resp.text

    def test_admin_still_sees_the_excluded_column(self, policied_invoices, monkeypatch):  # noqa: F811
        """Admin/no-policy unchanged (§12) — the raw column list stays
        authoritative for an admin caller."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        _seed_profile(
            "invoices",
            [
                {"name": "id", "type": "VARCHAR", "nullable": True},
                {"name": "national_id", "type": "VARCHAR", "nullable": True},
                {"name": "cost_center", "type": "VARCHAR", "nullable": True},
            ],
        )
        c = policied_invoices["client"]
        resp = c.get("/catalog/t/invoices", headers=_auth(policied_invoices["admin_token"]))
        assert resp.status_code == 200, resp.text
        assert "national_id" in resp.text

    def test_masked_column_is_badged(self, policied_invoices, monkeypatch):  # noqa: F811
        """The canonical ``md5(email) AS email`` column stays visible (it's
        masked, not hidden) but is badged so a reader can tell it apart from
        an untouched pass-through column."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        _seed_profile(
            "invoices",
            [
                {"name": "id", "type": "VARCHAR", "nullable": True},
                {"name": "email", "type": "VARCHAR", "nullable": True},
                {"name": "cost_center", "type": "VARCHAR", "nullable": True},
            ],
        )
        c = policied_invoices["client"]
        resp = c.get("/catalog/t/invoices", headers=_auth(policied_invoices["finance_token"]))
        assert resp.status_code == 200, resp.text
        assert "email" in resp.text
        assert 'class="td-masked-badge"' in resp.text

    def test_non_policied_sibling_is_unaffected(self, policied_invoices, monkeypatch):  # noqa: F811
        """The inert case: a table with no access_policy_sql renders exactly
        as before — no effective_schema call, no filtering."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        _seed_profile("products", [{"name": "id", "type": "VARCHAR"}, {"name": "sku", "type": "VARCHAR"}])
        c = policied_invoices["client"]
        resp = c.get("/catalog/t/products", headers=_auth(policied_invoices["finance_token"]))
        assert resp.status_code == 200, resp.text
        assert "sku" in resp.text
        assert 'class="td-masked-badge"' not in resp.text
