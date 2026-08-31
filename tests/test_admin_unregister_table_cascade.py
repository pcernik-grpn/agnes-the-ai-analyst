"""``DELETE /api/admin/registry/{table_id}`` — dependent rows go with it.

Two writers hold a reference to a ``table_registry`` row:

- ``data_package_tables`` (the package↔table junction), which the DuckDB
  DDL declares ``REFERENCES table_registry(id)`` with no ``ON DELETE``
  clause — so deleting a packaged table raised a constraint violation and
  the endpoint answered a raw 500. Postgres declares no FK there at all,
  so the same call succeeded and left an orphan junction row behind: two
  backends, two different wrong answers.
- ``resource_grants`` (the per-table grant), whose Postgres FK
  (``migrations 0013`` → ``resource_id_table``) IS ``ON DELETE CASCADE``
  while DuckDB has no FK enforcement at all — so a grant outlived the
  table it named on DuckDB and could silently re-apply if the same id were
  registered again.

Both are now cleared by ``TableRegistryRepository.unregister`` itself (and
its Postgres sibling), which is what makes the observable behavior the same
on either backend. Repo-level parity lives in
``tests/db_pg/test_table_registry_contract.py``; this file pins the
endpoint's own answer.
"""

from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def packaged_table(seeded_app):
    """A registered table that belongs to a data package."""
    from src.repositories import data_packages_repo, table_registry_repo

    table_registry_repo().register(
        id="orders",
        name="orders",
        source_type="keboola",
        bucket="in.c-main",
        source_table="orders",
        query_mode="local",
    )
    pkg_id = data_packages_repo().create(
        name="Sales",
        slug="sales",
        description=None,
        icon=None,
        color=None,
        created_by="admin@example.com",
    )
    data_packages_repo().add_table(pkg_id, "orders", added_by="admin@example.com")
    return pkg_id


class TestPackagedTable:
    def test_unregistering_a_packaged_table_succeeds(self, seeded_app, packaged_table):
        c = seeded_app["client"]
        r = c.delete("/api/admin/registry/orders", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 204, r.text

    def test_the_junction_row_goes_with_it(self, seeded_app, packaged_table):
        from src.repositories import data_packages_repo, table_registry_repo

        c = seeded_app["client"]
        r = c.delete("/api/admin/registry/orders", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 204, r.text

        assert table_registry_repo().get("orders") is None
        assert data_packages_repo().list_tables(packaged_table) == []


class TestGrantedTable:
    def test_the_tables_grants_go_with_it(self, seeded_app):
        """Postgres's ``resource_id_table`` FK is ``ON DELETE CASCADE``;
        DuckDB enforces no FK, so without this the grant outlived the table
        and would silently re-apply to a table re-registered under the same
        id."""
        from src.repositories import resource_grants_repo, table_registry_repo, user_groups_repo

        table_registry_repo().register(id="orders", name="orders", source_type="keboola", query_mode="local")
        group_id = user_groups_repo().create(name="Analysts", created_by="admin@example.com")["id"]
        resource_grants_repo().create(
            group_id=group_id,
            resource_type="table",
            resource_id="orders",
            assigned_by="admin@example.com",
        )
        assert resource_grants_repo().list_all(resource_type="table") != []

        c = seeded_app["client"]
        r = c.delete("/api/admin/registry/orders", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 204, r.text

        assert resource_grants_repo().list_all(resource_type="table") == []


class TestAuditTrail:
    """The cascade revokes ``resource_grants`` — an access-control change.

    An audit row for ``unregister_table`` that records only the table's
    identity says nothing about the grants that were revoked with it, so
    "who lost access to what, and when" is unanswerable from the audit log
    for exactly the event that caused it. Precedent for the shape:
    ``app/api/marketplaces.py``'s plugin-disable logs ``revoked_grants``
    next to the action.
    """

    def _params_for(self, action: str, resource: str) -> dict:
        import json

        from src.repositories import audit_repo

        rows, _ = audit_repo().query(action=action, resource=resource, limit=50)
        assert rows, f"no {action} audit row for {resource}"
        params = rows[0]["params"]
        return json.loads(params) if isinstance(params, str) else (params or {})

    def test_it_records_what_the_cascade_removed(self, seeded_app, packaged_table):
        from src.repositories import resource_grants_repo, user_groups_repo

        group_id = user_groups_repo().create(name="Analysts", created_by="admin@example.com")["id"]
        resource_grants_repo().create(
            group_id=group_id,
            resource_type="table",
            resource_id="orders",
            assigned_by="admin@example.com",
        )

        c = seeded_app["client"]
        r = c.delete("/api/admin/registry/orders", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 204, r.text

        params = self._params_for("unregister_table", "orders")
        assert params["package_memberships_removed"] == 1
        assert params["grants_revoked"] == 1

    def test_both_counts_are_logged_even_when_zero(self, seeded_app):
        """A delete that revoked nothing and a delete nobody counted must not
        produce identical audit rows — same reasoning as the marketplace
        precedent's always-logged pair."""
        from src.repositories import table_registry_repo

        table_registry_repo().register(id="orders", name="orders", source_type="keboola", query_mode="local")

        c = seeded_app["client"]
        r = c.delete("/api/admin/registry/orders", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 204, r.text

        params = self._params_for("unregister_table", "orders")
        assert params["package_memberships_removed"] == 0
        assert params["grants_revoked"] == 0

    def test_it_still_records_the_tables_identity(self, seeded_app):
        """The pre-existing fields must survive the addition."""
        from src.repositories import table_registry_repo

        table_registry_repo().register(
            id="orders",
            name="orders",
            source_type="keboola",
            bucket="in.c-main",
            source_table="orders",
            query_mode="local",
        )

        c = seeded_app["client"]
        r = c.delete("/api/admin/registry/orders", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 204, r.text

        params = self._params_for("unregister_table", "orders")
        assert params["name"] == "orders"
        assert params["source_type"] == "keboola"
        assert params["bucket"] == "in.c-main"
