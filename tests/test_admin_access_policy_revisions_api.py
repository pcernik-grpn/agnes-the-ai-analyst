"""Access-policy revision history — the route contract on the DEFAULT
(DuckDB) test backend (#1979 K1-sweep finding 1).

``access_policy_revisions`` is a post-A3 PG-only table, so this file owns
the half of the contract a DuckDB instance can prove:

* admin gating and the 404-before-any-repo-work,
* the typed ``501 requires_postgres_backend`` the list route owes,
* and — the property that actually matters — that a policy SAVE still
  succeeds on an instance with nowhere to record its revision. Recording
  history must never be able to block an admin from narrowing access to a
  table.

The Postgres happy path (revisions actually written on save/clear, listed in
order, and the restore round-trip) lives in
``tests/db_pg/test_access_policy_revisions_api_pg.py``.
"""

from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(c, token, **kwargs) -> str:
    kwargs.setdefault("source_type", "keboola")
    kwargs.setdefault("query_mode", "local")
    resp = c.post("/api/admin/register-table", json=kwargs, headers=_auth(token))
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _url(table_id: str) -> str:
    return f"/api/admin/registry/{table_id}/policy/revisions"


class TestAuthGating:
    def test_requires_authentication(self, seeded_app):
        r = seeded_app["client"].get(_url("whatever"))
        assert r.status_code == 401

    def test_requires_admin(self, seeded_app):
        """A non-admin analyst must not read policy bodies — the SQL names
        the columns and identity predicates the policy exists to enforce,
        which is a map of what it is hiding."""
        r = seeded_app["client"].get(_url("whatever"), headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 403


class TestUnknownTable:
    def test_unknown_table_is_404_not_501(self, seeded_app):
        """The registry lookup runs BEFORE the PG-only repo, so a typo'd id
        is a 404 on every backend — a 501 would tell an admin to migrate
        their database over a misspelling."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        r = c.get(_url("does-not-exist"), headers=_auth(token))
        assert r.status_code == 404, r.text
        assert r.json()["detail"] == "Table not found"


class TestDuckDbDegradesCleanly:
    def test_list_is_typed_501(self, seeded_app):
        """A3 ratchet: the revision store is PG-only, so a DuckDB instance
        gets a TYPED 501 the modal can recognize (and fall back to the
        audit-derived history on) — never a raw 500, and never an
        empty-but-healthy-looking "no revisions" answer that would read as
        "this policy was never edited"."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        table_id = _register(c, token, name="rev_duckdb_tbl", server_only=True)

        r = c.get(_url(table_id), headers=_auth(token))
        assert r.status_code == 501, r.text
        body = r.json()
        assert body["error"] == "requires_postgres_backend"
        assert body["feature"] == "access_policy_revisions"

    @pytest.mark.journey
    def test_saving_a_policy_still_works_with_no_revision_store(self, seeded_app, monkeypatch):
        """The write hook is observability, never load-bearing: an admin
        must be able to attach (and clear) a policy on a DuckDB instance,
        where there is nowhere to record the revision."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c, token = seeded_app["client"], seeded_app["admin_token"]
        table_id = _register(c, token, name="rev_duckdb_save", server_only=True)

        r = c.put(
            f"/api/admin/registry/{table_id}",
            json={
                "access_policy_sql": f"SELECT * FROM {table_id}",
                "access_policy_note": "no revision store here",
            },
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text

        from src.repositories import table_registry_repo

        assert table_registry_repo().get(table_id)["access_policy_sql"] is not None

        cleared = c.put(
            f"/api/admin/registry/{table_id}",
            json={"access_policy_sql": None},
            headers=_auth(token),
        )
        assert cleared.status_code == 200, cleared.text
        assert table_registry_repo().get(table_id)["access_policy_sql"] is None

    def test_unregistering_a_table_still_works_with_no_revision_store(self, seeded_app):
        """The revision purge on DELETE degrades the same way the write
        does — a DuckDB instance must still be able to unregister a table."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        table_id = _register(c, token, name="rev_duckdb_delete")

        r = c.delete(f"/api/admin/registry/{table_id}", headers=_auth(token))
        assert r.status_code == 204, r.text

    def test_registering_a_table_still_works_with_no_revision_store_to_purge(self, seeded_app):
        """finding 3 (PR #2023 review): `register_table`'s defense-in-depth
        orphan purge degrades the same way the write hook and the
        unregister-time purge do — a DuckDB instance (no revision store at
        all) must still be able to register a brand-new table."""
        c, token = seeded_app["client"], seeded_app["admin_token"]

        r = c.post(
            "/api/admin/register-table",
            json={"name": "rev_duckdb_register", "source_type": "keboola", "query_mode": "local"},
            headers=_auth(token),
        )
        assert r.status_code == 201, r.text


class TestUnregisterPurgeFailure:
    def test_unregister_aborts_if_purge_fails_for_a_reason_other_than_missing_backend(self, seeded_app, monkeypatch):
        """finding 3 (PR #2023 review): a genuine failure while purging a
        table's access-policy revisions must abort the whole unregistration
        cleanly -- never silently drop the registry row and leave the (now
        unreachable-to-purge) old revisions sitting under an id a
        re-registration of the same name would reuse."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        table_id = _register(c, token, name="rev_purge_fails")

        class _BoomRepo:
            def delete_for_table(self, table_id):
                raise RuntimeError("boom")

        monkeypatch.setattr("app.api.admin.access_policy_revisions_repo", lambda: _BoomRepo())

        r = c.delete(f"/api/admin/registry/{table_id}", headers=_auth(token))
        assert r.status_code != 204, r.text
        assert r.status_code >= 400, r.text

        from src.repositories import table_registry_repo

        assert table_registry_repo().get(table_id) is not None, "registry row must survive a failed purge"
