"""Tests for /api/admin/data-packages (Task 6.1).

Covers: admin CRUD happy path, RBAC (403 for non-admin), slug-collision
(409), and audit_log writes for each mutation.
"""

from __future__ import annotations

import json

import pytest

from src.db import get_system_db


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _register_table(name: str = "t_pkg_test") -> str:
    """Insert a row into ``table_registry`` and return its id. Reused across
    junction tests so we don't depend on a real connector pipeline."""
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    repo = TableRegistryRepository(conn)
    table_id = f"tbl_{name}"
    repo.register(
        id=table_id,
        name=name,
        source_type="keboola",
        source_table=f"in.c-test.{name}",
        bucket="in.c-test",
        query_mode="local",
    )
    conn.close()
    return table_id


def _audit_actions_for_resource(resource: str) -> list[dict]:
    conn = get_system_db()
    rows = conn.execute(
        "SELECT action, params FROM audit_log WHERE resource = ? ORDER BY timestamp",
        [resource],
    ).fetchall()
    conn.close()
    out = []
    for action, params in rows:
        out.append(
            {
                "action": action,
                "params": json.loads(params) if params else None,
            }
        )
    return out


class TestDataPackagesList:
    def test_admin_list_empty(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(
            "/api/admin/data-packages",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_non_admin_gets_403(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(
            "/api/admin/data-packages",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403


class TestDataPackagesCreate:
    def test_create_returns_id_and_audits(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/data-packages",
            json={
                "name": "Sales bundle",
                "slug": "sales-bundle",
                "description": "All sales tables",
                "icon": "📦",
                "color": "#fce7f3",
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 201
        pkg_id = resp.json()["id"]
        assert pkg_id.startswith("pkg_")

        rows = _audit_actions_for_resource(f"data_package:{pkg_id}")
        assert any(r["action"] == "data_package.create" for r in rows)
        create_row = next(r for r in rows if r["action"] == "data_package.create")
        assert create_row["params"]["slug"] == "sales-bundle"
        assert create_row["params"]["name"] == "Sales bundle"

    def test_duplicate_slug_returns_409(self, seeded_app):
        c = seeded_app["client"]
        c.post(
            "/api/admin/data-packages",
            json={"name": "X", "slug": "x"},
            headers=_auth(seeded_app["admin_token"]),
        )
        resp = c.post(
            "/api/admin/data-packages",
            json={"name": "X2", "slug": "x"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 409
        assert resp.json()["detail"] == "slug_exists"

    def test_blank_name_returns_400(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/data-packages",
            json={"name": "  ", "slug": "blank"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 400

    def test_non_admin_cannot_create(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/data-packages",
            json={"name": "X", "slug": "x"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403


class TestDataPackagesDetail:
    def test_get_includes_tables(self, seeded_app):
        c = seeded_app["client"]
        headers = _auth(seeded_app["admin_token"])
        pkg_id = c.post(
            "/api/admin/data-packages",
            json={"name": "P", "slug": "p"},
            headers=headers,
        ).json()["id"]
        table_id = _register_table("orders_detail")
        c.post(
            f"/api/admin/data-packages/{pkg_id}/tables",
            json={"table_id": table_id},
            headers=headers,
        )
        resp = c.get(f"/api/admin/data-packages/{pkg_id}", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["slug"] == "p"
        assert any(t["id"] == table_id for t in body["tables"])

    def test_unknown_id_returns_404(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(
            "/api/admin/data-packages/pkg_does_not_exist",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 404


class TestDataPackagesUpdate:
    def test_update_writes_audit_with_diff(self, seeded_app):
        c = seeded_app["client"]
        headers = _auth(seeded_app["admin_token"])
        pkg_id = c.post(
            "/api/admin/data-packages",
            json={"name": "Old", "slug": "u"},
            headers=headers,
        ).json()["id"]
        resp = c.put(
            f"/api/admin/data-packages/{pkg_id}",
            json={"name": "New"},
            headers=headers,
        )
        assert resp.status_code == 200
        rows = _audit_actions_for_resource(f"data_package:{pkg_id}")
        upd = next(r for r in rows if r["action"] == "data_package.update")
        assert upd["params"]["after"]["name"] == "New"


class TestDataPackagesDelete:
    def test_delete_audits_tables_count(self, seeded_app):
        c = seeded_app["client"]
        headers = _auth(seeded_app["admin_token"])
        pkg_id = c.post(
            "/api/admin/data-packages",
            json={"name": "D", "slug": "d"},
            headers=headers,
        ).json()["id"]
        table_id = _register_table("orders_delete")
        c.post(
            f"/api/admin/data-packages/{pkg_id}/tables",
            json={"table_id": table_id},
            headers=headers,
        )
        resp = c.delete(
            f"/api/admin/data-packages/{pkg_id}",
            headers=headers,
        )
        assert resp.status_code == 204
        rows = _audit_actions_for_resource(f"data_package:{pkg_id}")
        deleted = next(r for r in rows if r["action"] == "data_package.delete")
        assert deleted["params"]["tables_count"] == 1
        assert deleted["params"]["slug"] == "d"


class TestDataPackagesJunction:
    def test_add_and_remove_table_audited(self, seeded_app):
        c = seeded_app["client"]
        headers = _auth(seeded_app["admin_token"])
        pkg_id = c.post(
            "/api/admin/data-packages",
            json={"name": "J", "slug": "j"},
            headers=headers,
        ).json()["id"]
        table_id = _register_table("orders_junction")

        add = c.post(
            f"/api/admin/data-packages/{pkg_id}/tables",
            json={"table_id": table_id},
            headers=headers,
        )
        assert add.status_code == 200
        assert add.json()["added"] is True

        # idempotent on duplicate
        again = c.post(
            f"/api/admin/data-packages/{pkg_id}/tables",
            json={"table_id": table_id},
            headers=headers,
        )
        assert again.json()["added"] is False

        rem = c.delete(
            f"/api/admin/data-packages/{pkg_id}/tables/{table_id}",
            headers=headers,
        )
        assert rem.status_code == 204

        rows = _audit_actions_for_resource(f"data_package:{pkg_id}")
        actions = [r["action"] for r in rows]
        assert "data_package.add_table" in actions
        assert "data_package.remove_table" in actions

    def test_add_unknown_table_404(self, seeded_app):
        c = seeded_app["client"]
        headers = _auth(seeded_app["admin_token"])
        pkg_id = c.post(
            "/api/admin/data-packages",
            json={"name": "U", "slug": "u-unknown"},
            headers=headers,
        ).json()["id"]
        resp = c.post(
            f"/api/admin/data-packages/{pkg_id}/tables",
            json={"table_id": "does-not-exist"},
            headers=headers,
        )
        assert resp.status_code == 404


class TestDataPackagesListLimit:
    """``?limit=`` on the list — the repository always had one (default 200)
    but the endpoint never exposed it, so a composed reader (the
    ``admin_access_picture`` MCP tool) could not ask for a complete inventory
    and silently lost every package past the 200th, misclassifying their
    tables as unpackaged."""

    def _create(self, seeded_app, n: int) -> None:
        c = seeded_app["client"]
        for i in range(n):
            resp = c.post(
                "/api/admin/data-packages",
                json={"name": f"Limit {i}", "slug": f"limit-{i}"},
                headers=_auth(seeded_app["admin_token"]),
            )
            assert resp.status_code == 201, resp.text

    def test_limit_bounds_the_page(self, seeded_app):
        self._create(seeded_app, 3)
        resp = seeded_app["client"].get(
            "/api/admin/data-packages",
            params={"limit": 2},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_default_page_is_unchanged(self, seeded_app):
        self._create(seeded_app, 3)
        resp = seeded_app["client"].get(
            "/api/admin/data-packages",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    def test_limit_out_of_range_is_422(self, seeded_app):
        for bad in (0, 5001):
            resp = seeded_app["client"].get(
                "/api/admin/data-packages",
                params={"limit": bad},
                headers=_auth(seeded_app["admin_token"]),
            )
            assert resp.status_code == 422, (bad, resp.text)
