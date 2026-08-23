"""API ↔ CLI parity tests for the v49 unified stack (Phase 9 / Task 9.1).

For each (HTTP endpoint, ``agnes …`` CLI command) pair listed in Section 6
of the unified-stack design doc, fire BOTH paths against the same in-memory
DB and verify they produce the **identical** DB state delta. Catches drift
where a CLI command and the corresponding endpoint quietly start writing
different rows, dropping audit lines, or fanning out one but not the other.

Bridging the CLI to the FastAPI ``TestClient``
----------------------------------------------
The CLI's ``api_get/post/put/delete`` helpers do real HTTP via ``httpx``.
For parity we patch them inside each command module (where Typer captured
them at import time) to redirect through the shared ``TestClient`` with an
admin Bearer token injected. The CLI code path is otherwise untouched —
argument parsing, error handling, slug→id resolution, audit emission all
exercise their real implementations.

Delta shape
-----------
Each test seeds the DB to a known starting state, snapshots the relevant
tables, fires the API path, snapshots again → ``delta_api``. Reset to the
same starting state, fire the CLI path, snapshot → ``delta_cli``. Assert
``delta_api == delta_cli``.

We deliberately exclude ``audit_log.created_at`` / ``data_packages.created_at``
from comparison — wall-clock columns flicker between two adjacent writes. We
DO compare row counts, schema, and content columns. Audit rows are matched
by ``(action, target)`` pairs since exact wall-clock differs.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from typing import Dict, List

import pytest
from typer.testing import CliRunner

from cli.main import app as cli_app
from src.db import get_system_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _patch_cli_to_testclient(monkeypatch, modules: List[str], client, token: str):
    """Redirect CLI's ``api_*`` helpers to the in-memory TestClient.

    Each CLI command module imported its api helpers at module import time
    (``from cli.client import api_get, api_post, …``). Typer captures those
    bound names — so we patch them on each module rather than on
    ``cli.client`` itself.

    Auth header is injected automatically using the admin token so admin
    endpoints accept the request.
    """
    auth = _auth(token)

    def _normalize(headers):
        out = dict(auth)
        if headers:
            out.update(headers)
        return out

    def _get(path: str, *, timeout: float = 30.0, **kwargs):
        kwargs.setdefault("headers", {})
        kwargs["headers"] = _normalize(kwargs["headers"])
        # TestClient accepts ``params`` directly like httpx.
        return client.get(path, **kwargs)

    def _post(path: str, *, timeout: float = 30.0, **kwargs):
        kwargs.setdefault("headers", {})
        kwargs["headers"] = _normalize(kwargs["headers"])
        return client.post(path, **kwargs)

    def _put(path: str, *, timeout: float = 30.0, **kwargs):
        kwargs.setdefault("headers", {})
        kwargs["headers"] = _normalize(kwargs["headers"])
        return client.put(path, **kwargs)

    def _delete(path: str, *, timeout: float = 30.0, **kwargs):
        kwargs.setdefault("headers", {})
        kwargs["headers"] = _normalize(kwargs["headers"])
        return client.delete(path, **kwargs)

    for mod_name in modules:
        for name, repl in (
            ("api_get", _get),
            ("api_post", _post),
            ("api_put", _put),
            ("api_delete", _delete),
        ):
            try:
                monkeypatch.setattr(f"{mod_name}.{name}", repl)
            except AttributeError:
                # Module didn't import this helper — skip silently.
                pass


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------


def _snapshot_table(conn, sql: str, params=None) -> List[tuple]:
    rows = conn.execute(sql, params or []).fetchall()
    # Sort to make comparison order-independent.
    return sorted(tuple(r) for r in rows)


def _snapshot_audit_actions(conn, prefix: str = "") -> List[str]:
    """Audit actions are compared by (action, resource-shape) without
    wall-clock or random uuid suffixes so parity holds across two adjacent
    writes against different random ids.

    Newly-created rows pick up a fresh uuid in ``resource`` (e.g.
    ``data_package:pkg_0f7a4af03713``) so we mask the id to ``<id>`` and
    keep only the resource type prefix. The remaining shape is what we
    actually care about: same action verb + same resource type.
    """

    if prefix:
        rows = conn.execute(
            "SELECT action, resource FROM audit_log WHERE action LIKE ? ORDER BY id",
            [f"{prefix}%"],
        ).fetchall()
    else:
        rows = conn.execute("SELECT action, resource FROM audit_log ORDER BY id").fetchall()
    # ``resource`` is canonically ``<type>:<id>``; normalize the id half so
    # uuid-based ids don't break equality on two adjacent runs.
    masked = []
    for action, resource in rows:
        if resource and ":" in resource:
            rtype, _ = resource.split(":", 1)
            masked.append(f"{action}|{rtype}:<id>")
        else:
            masked.append(f"{action}|{resource}")
    return masked


def _reset_audit_log(conn) -> None:
    conn.execute("DELETE FROM audit_log")


# ---------------------------------------------------------------------------
# DB seeding helpers
# ---------------------------------------------------------------------------


def _seed_group_with_user(conn, *, name: str, user_id: str) -> str:
    """Create a fresh group + add user_id to it. Returns the new group id."""
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository

    g = UserGroupsRepository(conn).create(name=name, description="", created_by="test")
    gid = g["id"] if isinstance(g, dict) else g
    UserGroupMembersRepository(conn).add_member(user_id, gid, source="test")
    return gid


def _seed_data_package(conn, *, slug: str, name: str = "P") -> str:
    from src.repositories.data_packages import DataPackagesRepository

    return DataPackagesRepository(conn).create(
        name=name,
        slug=slug,
        description=None,
        icon=None,
        color=None,
        created_by="test",
    )


def _seed_memory_domain(conn, *, slug: str, name: str = "D") -> str:
    from src.repositories.memory_domains import MemoryDomainsRepository

    return MemoryDomainsRepository(conn).create(
        name=name,
        slug=slug,
        description=None,
        icon=None,
        color=None,
        created_by="test",
    )


def _seed_grant_for(
    conn,
    group_id: str,
    resource_type: str,
    resource_id: str,
    requirement: str = "available",
) -> str:
    grant_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
        "requirement, assigned_at, assigned_by) "
        "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 'test')",
        [grant_id, group_id, resource_type, resource_id, requirement],
    )
    return grant_id


def _seed_table_registry(conn, *, name: str) -> str:
    """Insert a minimal table_registry row and return its id.

    The repo's ``register()`` requires an explicit id and returns None; we
    just pick a stable id mirroring the name so subsequent lookups don't
    need a second roundtrip.
    """
    from src.repositories.table_registry import TableRegistryRepository

    tid = f"tbl_{name}"
    TableRegistryRepository(conn).register(
        id=tid,
        name=name,
        source_type="keboola",
        bucket="b",
        source_table=name,
        query_mode="local",
    )
    return tid


def _seed_knowledge_item(conn, *, item_id: str, title: str = "T", status: str = "approved") -> None:
    conn.execute(
        "INSERT INTO knowledge_items(id, title, status) VALUES (?, ?, ?)",
        [item_id, title, status],
    )


def _purge_user_state(conn) -> None:
    """Wipe everything that the parity tests dirty. Called between API/CLI runs.

    Order matters — junction tables first to avoid FK / orphan complaints.
    """
    for sql in (
        "DELETE FROM data_package_tables",
        "DELETE FROM data_packages",
        "DELETE FROM knowledge_item_domains",
        "DELETE FROM memory_domains",
        "DELETE FROM user_stack_subscriptions",
        "DELETE FROM resource_grants",
        "DELETE FROM user_group_members WHERE source = 'test'",
        "DELETE FROM user_groups WHERE name LIKE 'parity_%'",
        "DELETE FROM knowledge_items",
        "DELETE FROM table_registry",
        "DELETE FROM audit_log",
    ):
        try:
            conn.execute(sql)
        except Exception:
            # DuckDB raises on unknown tables in some legacy schemas. The
            # parity suite only runs against v49+; the empty try keeps
            # cleanup robust if a table was renamed in a parallel branch.
            pass


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def parity_env(seeded_app, monkeypatch):
    """Patch CLI api_* helpers to the seeded TestClient + return runners.

    Returns a dict with the test client, admin token, the Typer CliRunner,
    and ready-made ``run_cli`` / ``run_api`` callables.
    """
    # Suppress the auto-update probe — it hits /cli/latest, prints a stderr
    # warning that CliRunner merges into ``result.output``, and breaks
    # ``json.loads(result.output)`` parsing.
    monkeypatch.setenv("AGNES_NO_UPDATE_CHECK", "1")

    client = seeded_app["client"]
    admin_token = seeded_app["admin_token"]
    analyst_token = seeded_app["analyst_token"]

    _patch_cli_to_testclient(
        monkeypatch,
        modules=[
            "cli.commands.admin_data_package",
            "cli.commands.admin_memory_domain",
            "cli.commands.memory_admin",
            "cli.commands.stack",
            "cli.commands.admin",
            "cli.commands.admin_jobs",
            "cli.commands.admin_marketplace",
            "cli.commands.admin_mcp",
            "cli.commands.admin_connection",
            "cli.commands.mcp",
        ],
        client=client,
        token=admin_token,
    )
    runner = CliRunner()

    def run_cli(args: List[str], *, expect_success: bool = True, input: str = ""):
        result = runner.invoke(cli_app, args, input=input)
        if expect_success:
            assert result.exit_code == 0, (
                f"CLI {' '.join(args)} failed exit={result.exit_code}\n"
                f"stdout={result.output}\n"
                f"stderr={getattr(result, 'stderr', '<merged>')}"
            )
        return result

    return {
        "client": client,
        "admin_token": admin_token,
        "analyst_token": analyst_token,
        "run_cli": run_cli,
    }


# ---------------------------------------------------------------------------
# Stack subscribe / unsubscribe / list
# ---------------------------------------------------------------------------


class TestStackSubscribeParity:
    """``POST /api/stack/subscribe`` ↔ ``agnes stack add <type> <id>``."""

    def _setup(self):
        conn = get_system_db()
        _purge_user_state(conn)
        gid = _seed_group_with_user(conn, name="parity_subs", user_id="analyst1")
        pkg_id = _seed_data_package(conn, slug="parity-sub-pkg")
        _seed_grant_for(conn, gid, "data_package", pkg_id, "available")
        conn.close()
        return pkg_id

    def test_subscribe_parity(self, parity_env):
        pkg_id = self._setup()

        # API path
        r = parity_env["client"].post(
            "/api/stack/subscribe",
            json={"resource_type": "data_package", "resource_id": pkg_id},
            headers=_auth(parity_env["analyst_token"]),
        )
        assert r.status_code == 200
        conn = get_system_db()
        delta_api = _snapshot_table(
            conn,
            "SELECT user_id, resource_type, resource_id FROM user_stack_subscriptions WHERE resource_id = ?",
            [pkg_id],
        )
        conn.close()

        # Reset subscriptions and re-fire via CLI. CLI uses admin token by
        # default (the global patch wires admin). To target analyst, we
        # re-patch with the analyst token for this test only.
        conn = get_system_db()
        conn.execute("DELETE FROM user_stack_subscriptions")
        conn.close()

        # Re-issue CLI under analyst auth.
        with _admin_auth_swap(parity_env, parity_env["analyst_token"]):
            parity_env["run_cli"](["stack", "add", "data_package", pkg_id])

        conn = get_system_db()
        delta_cli = _snapshot_table(
            conn,
            "SELECT user_id, resource_type, resource_id FROM user_stack_subscriptions WHERE resource_id = ?",
            [pkg_id],
        )
        conn.close()

        assert delta_api == delta_cli
        # Both paths produced exactly one (analyst1, data_package, pkg) row.
        assert delta_api == [("analyst1", "data_package", pkg_id)]


class TestStackUnsubscribeParity:
    """``DELETE /api/stack/subscription/{type}/{id}`` ↔ ``agnes stack remove …``."""

    def _setup_with_sub(self):
        conn = get_system_db()
        _purge_user_state(conn)
        gid = _seed_group_with_user(conn, name="parity_unsubs", user_id="analyst1")
        pkg_id = _seed_data_package(conn, slug="parity-unsub-pkg")
        _seed_grant_for(conn, gid, "data_package", pkg_id, "available")
        conn.execute(
            "INSERT INTO user_stack_subscriptions(user_id, resource_type, resource_id) "
            "VALUES ('analyst1', 'data_package', ?)",
            [pkg_id],
        )
        conn.close()
        return pkg_id

    def test_unsubscribe_parity(self, parity_env):
        pkg_id = self._setup_with_sub()

        # API
        r = parity_env["client"].delete(
            f"/api/stack/subscription/data_package/{pkg_id}",
            headers=_auth(parity_env["analyst_token"]),
        )
        # 0.54.26 design-rules pass moved this endpoint to 204.
        assert r.status_code == 204
        conn = get_system_db()
        delta_api = _snapshot_table(
            conn,
            "SELECT user_id, resource_type, resource_id FROM user_stack_subscriptions",
        )
        conn.close()

        # Reinstate the same subscription against the same package id (don't
        # rebuild the whole setup; pkg_id is random and would drift).
        conn = get_system_db()
        conn.execute(
            "INSERT INTO user_stack_subscriptions(user_id, resource_type, resource_id) "
            "VALUES ('analyst1', 'data_package', ?)",
            [pkg_id],
        )
        conn.close()
        with _admin_auth_swap(parity_env, parity_env["analyst_token"]):
            parity_env["run_cli"](["stack", "remove", "data_package", pkg_id])

        conn = get_system_db()
        delta_cli = _snapshot_table(
            conn,
            "SELECT user_id, resource_type, resource_id FROM user_stack_subscriptions",
        )
        conn.close()

        assert delta_api == delta_cli == []


class TestStackListParity:
    """``GET /api/stack?type=`` ↔ ``agnes stack list --type ...``."""

    def test_list_parity(self, parity_env):
        conn = get_system_db()
        _purge_user_state(conn)
        gid = _seed_group_with_user(conn, name="parity_list", user_id="analyst1")
        pkg_id = _seed_data_package(conn, slug="parity-list-pkg", name="ListPkg")
        _seed_grant_for(conn, gid, "data_package", pkg_id, "required")
        conn.close()

        # API
        r_api = parity_env["client"].get(
            "/api/stack?type=data_package",
            headers=_auth(parity_env["analyst_token"]),
        )
        assert r_api.status_code == 200
        api_ids = sorted(it["id"] for it in r_api.json()["items"])

        # CLI — JSON output for stable comparison
        with _admin_auth_swap(parity_env, parity_env["analyst_token"]):
            result = parity_env["run_cli"](["stack", "list", "--type", "data_package", "--json"])
        import json

        cli_ids = sorted(it["id"] for it in json.loads(result.output))

        assert api_ids == cli_ids
        assert pkg_id in api_ids


class TestStackArtefactAddRemoveParity:
    """``POST/DELETE /api/stack/artefacts/{id}`` ↔ ``agnes stack artefacts add/remove``.

    Artefacts are NOT resource_grants-gated like data_package/memory_domain
    above — permission is ownership (``file_corpora.created_by``), so the
    setup seeds a plain collection owned by the analyst instead of a grant.
    """

    def _seed_collection(self, conn, *, name: str = "Parity Artefact") -> str:
        from src.repositories.file_corpora import FileCorporaRepository

        return FileCorporaRepository(conn).create(
            name=name,
            slug=f"parity-artefact-{uuid.uuid4().hex[:8]}",
            description=None,
            created_by="analyst1",
        )

    def test_add_parity(self, parity_env):
        conn = get_system_db()
        _purge_user_state(conn)
        corpus_id = self._seed_collection(conn)
        conn.close()

        r = parity_env["client"].post(
            f"/api/stack/artefacts/{corpus_id}",
            headers=_auth(parity_env["analyst_token"]),
        )
        assert r.status_code == 200, r.text
        conn = get_system_db()
        delta_api = _snapshot_table(
            conn,
            "SELECT user_id, resource_type, resource_id FROM user_stack_subscriptions WHERE resource_id = ?",
            [corpus_id],
        )
        conn.execute("DELETE FROM user_stack_subscriptions WHERE resource_id = ?", [corpus_id])
        conn.close()

        with _admin_auth_swap(parity_env, parity_env["analyst_token"]):
            parity_env["run_cli"](["stack", "artefacts", "add", corpus_id])

        conn = get_system_db()
        delta_cli = _snapshot_table(
            conn,
            "SELECT user_id, resource_type, resource_id FROM user_stack_subscriptions WHERE resource_id = ?",
            [corpus_id],
        )
        conn.close()

        assert delta_api == delta_cli == [("analyst1", "collection", corpus_id)]

    def test_remove_parity(self, parity_env):
        conn = get_system_db()
        _purge_user_state(conn)
        corpus_id = self._seed_collection(conn, name="Parity Artefact Remove")
        conn.execute(
            "INSERT INTO user_stack_subscriptions(user_id, resource_type, resource_id) "
            "VALUES ('analyst1', 'collection', ?)",
            [corpus_id],
        )
        conn.close()

        r = parity_env["client"].delete(
            f"/api/stack/artefacts/{corpus_id}",
            headers=_auth(parity_env["analyst_token"]),
        )
        assert r.status_code == 204
        conn = get_system_db()
        delta_api = _snapshot_table(
            conn,
            "SELECT user_id, resource_type, resource_id FROM user_stack_subscriptions WHERE resource_id = ?",
            [corpus_id],
        )
        conn.execute(
            "INSERT INTO user_stack_subscriptions(user_id, resource_type, resource_id) "
            "VALUES ('analyst1', 'collection', ?)",
            [corpus_id],
        )
        conn.close()

        with _admin_auth_swap(parity_env, parity_env["analyst_token"]):
            parity_env["run_cli"](["stack", "artefacts", "remove", corpus_id])

        conn = get_system_db()
        delta_cli = _snapshot_table(
            conn,
            "SELECT user_id, resource_type, resource_id FROM user_stack_subscriptions WHERE resource_id = ?",
            [corpus_id],
        )
        conn.close()

        assert delta_api == delta_cli == []


# ---------------------------------------------------------------------------
# Data Package admin CRUD
# ---------------------------------------------------------------------------


class TestDataPackageCreateParity:
    """``POST /api/admin/data-packages`` ↔ ``agnes admin data-package create``."""

    def _snapshot(self, conn) -> tuple:
        pkg_rows = _snapshot_table(conn, "SELECT slug, name, description, icon, color FROM data_packages")
        audit_rows = _snapshot_audit_actions(conn, prefix="data_package.create")
        return (pkg_rows, audit_rows)

    def test_create_parity(self, parity_env):
        conn = get_system_db()
        _purge_user_state(conn)
        conn.close()

        # API
        r = parity_env["client"].post(
            "/api/admin/data-packages",
            json={"name": "API Pkg", "slug": "api-pkg", "description": "via api", "icon": None, "color": None},
            headers=_auth(parity_env["admin_token"]),
        )
        assert r.status_code == 201
        conn = get_system_db()
        delta_api = self._snapshot(conn)
        conn.close()

        # Reset
        conn = get_system_db()
        _purge_user_state(conn)
        conn.close()

        # CLI
        parity_env["run_cli"](
            ["admin", "data-package", "create", "--name", "API Pkg", "--slug", "api-pkg", "--description", "via api"]
        )
        conn = get_system_db()
        delta_cli = self._snapshot(conn)
        conn.close()

        assert delta_api == delta_cli
        # Both wrote one package + one audit row.
        assert len(delta_api[0]) == 1
        assert len(delta_api[1]) == 1


class TestDataPackageEditParity:
    """``PUT /api/admin/data-packages/{id}`` ↔ ``agnes admin data-package edit``."""

    def _setup(self):
        conn = get_system_db()
        _purge_user_state(conn)
        pkg_id = _seed_data_package(conn, slug="parity-edit", name="OldName")
        conn.close()
        return pkg_id

    def test_edit_parity(self, parity_env):
        pkg_id = self._setup()

        r = parity_env["client"].put(
            f"/api/admin/data-packages/{pkg_id}",
            json={"name": "NewName"},
            headers=_auth(parity_env["admin_token"]),
        )
        assert r.status_code == 200
        conn = get_system_db()
        name_api = conn.execute("SELECT name FROM data_packages WHERE id = ?", [pkg_id]).fetchone()[0]
        audit_api = _snapshot_audit_actions(conn, prefix="data_package.update")
        conn.close()

        # Reset to OldName + audit
        conn = get_system_db()
        conn.execute("UPDATE data_packages SET name = 'OldName' WHERE id = ?", [pkg_id])
        _reset_audit_log(conn)
        conn.close()

        parity_env["run_cli"](["admin", "data-package", "edit", pkg_id, "--name", "NewName"])
        conn = get_system_db()
        name_cli = conn.execute("SELECT name FROM data_packages WHERE id = ?", [pkg_id]).fetchone()[0]
        audit_cli = _snapshot_audit_actions(conn, prefix="data_package.update")
        conn.close()

        assert name_api == name_cli == "NewName"
        assert audit_api == audit_cli
        assert len(audit_api) == 1


class TestDataPackageDeleteParity:
    """``DELETE /api/admin/data-packages/{id}`` ↔ ``agnes admin data-package delete``."""

    def _setup(self):
        conn = get_system_db()
        _purge_user_state(conn)
        pkg_id = _seed_data_package(conn, slug="parity-del")
        conn.close()
        return pkg_id

    def test_delete_parity(self, parity_env):
        pkg_id = self._setup()

        r = parity_env["client"].delete(
            f"/api/admin/data-packages/{pkg_id}",
            headers=_auth(parity_env["admin_token"]),
        )
        assert r.status_code == 204
        # v54: delete() is a soft delete — the row still exists but
        # carries ``deleted_at IS NOT NULL``. Parity asserts both
        # paths leave the row in the same "live=0, soft-deleted=1"
        # shape.
        conn = get_system_db()
        api_live = conn.execute(
            "SELECT COUNT(*) FROM data_packages WHERE id = ? AND deleted_at IS NULL", [pkg_id]
        ).fetchone()[0]
        api_soft = conn.execute(
            "SELECT COUNT(*) FROM data_packages WHERE id = ? AND deleted_at IS NOT NULL", [pkg_id]
        ).fetchone()[0]
        api_audit = _snapshot_audit_actions(conn, prefix="data_package.delete")
        conn.close()

        pkg_id_2 = self._setup()
        parity_env["run_cli"](["admin", "data-package", "delete", pkg_id_2, "--yes"])
        conn = get_system_db()
        cli_live = conn.execute(
            "SELECT COUNT(*) FROM data_packages WHERE id = ? AND deleted_at IS NULL", [pkg_id_2]
        ).fetchone()[0]
        cli_soft = conn.execute(
            "SELECT COUNT(*) FROM data_packages WHERE id = ? AND deleted_at IS NOT NULL", [pkg_id_2]
        ).fetchone()[0]
        cli_audit = _snapshot_audit_actions(conn, prefix="data_package.delete")
        conn.close()

        assert api_live == cli_live == 0
        assert api_soft == cli_soft == 1
        # Both paths emit exactly one delete audit row
        assert len(api_audit) == len(cli_audit) == 1


class TestDataPackageAddRemoveTableParity:
    """``POST /api/admin/data-packages/{id}/tables`` ↔ ``agnes admin data-package add-table``;
    plus the matching remove-table pair."""

    def _setup(self):
        conn = get_system_db()
        _purge_user_state(conn)
        pkg_id = _seed_data_package(conn, slug="parity-tbl-pkg")
        tbl_id = _seed_table_registry(conn, name="parity_tbl")
        conn.close()
        return pkg_id, tbl_id

    def test_add_table_parity(self, parity_env):
        pkg_id, tbl_id = self._setup()

        r = parity_env["client"].post(
            f"/api/admin/data-packages/{pkg_id}/tables",
            json={"table_id": tbl_id},
            headers=_auth(parity_env["admin_token"]),
        )
        assert r.status_code == 200
        conn = get_system_db()
        api_state = _snapshot_table(conn, "SELECT package_id, table_id FROM data_package_tables")
        api_audit = _snapshot_audit_actions(conn, prefix="data_package.add_table")
        conn.close()

        # Reset junction + audit
        conn = get_system_db()
        conn.execute("DELETE FROM data_package_tables")
        _reset_audit_log(conn)
        conn.close()

        parity_env["run_cli"](["admin", "data-package", "add-table", pkg_id, tbl_id])
        conn = get_system_db()
        cli_state = _snapshot_table(conn, "SELECT package_id, table_id FROM data_package_tables")
        cli_audit = _snapshot_audit_actions(conn, prefix="data_package.add_table")
        conn.close()

        assert api_state == cli_state
        assert api_audit == cli_audit
        assert api_state == [(pkg_id, tbl_id)]

    def test_remove_table_parity(self, parity_env):
        pkg_id, tbl_id = self._setup()
        # Pre-link
        conn = get_system_db()
        conn.execute(
            "INSERT INTO data_package_tables(package_id, table_id, added_by) VALUES (?, ?, 'test')",
            [pkg_id, tbl_id],
        )
        conn.close()

        r = parity_env["client"].delete(
            f"/api/admin/data-packages/{pkg_id}/tables/{tbl_id}",
            headers=_auth(parity_env["admin_token"]),
        )
        assert r.status_code == 204
        conn = get_system_db()
        api_state = _snapshot_table(conn, "SELECT package_id, table_id FROM data_package_tables")
        api_audit = _snapshot_audit_actions(conn, prefix="data_package.remove_table")
        conn.close()

        # Re-link
        conn = get_system_db()
        conn.execute(
            "INSERT INTO data_package_tables(package_id, table_id, added_by) VALUES (?, ?, 'test')",
            [pkg_id, tbl_id],
        )
        _reset_audit_log(conn)
        conn.close()

        parity_env["run_cli"](["admin", "data-package", "remove-table", pkg_id, tbl_id, "--yes"])
        conn = get_system_db()
        cli_state = _snapshot_table(conn, "SELECT package_id, table_id FROM data_package_tables")
        cli_audit = _snapshot_audit_actions(conn, prefix="data_package.remove_table")
        conn.close()

        assert api_state == cli_state == []
        assert api_audit == cli_audit


# ---------------------------------------------------------------------------
# Memory Domain admin CRUD
# ---------------------------------------------------------------------------


class TestMemoryDomainCreateParity:
    def _snapshot(self, conn) -> tuple:
        return (
            _snapshot_table(conn, "SELECT slug, name, description FROM memory_domains"),
            _snapshot_audit_actions(conn, prefix="memory_domain.create"),
        )

    def test_create_parity(self, parity_env):
        conn = get_system_db()
        _purge_user_state(conn)
        conn.close()

        r = parity_env["client"].post(
            "/api/admin/memory-domains",
            json={"name": "Finance", "slug": "parity-finance", "description": "$$$"},
            headers=_auth(parity_env["admin_token"]),
        )
        assert r.status_code == 201
        conn = get_system_db()
        api_delta = self._snapshot(conn)
        conn.close()

        conn = get_system_db()
        _purge_user_state(conn)
        conn.close()

        parity_env["run_cli"](
            [
                "admin",
                "memory-domain",
                "create",
                "--name",
                "Finance",
                "--slug",
                "parity-finance",
                "--description",
                "$$$",
            ]
        )
        conn = get_system_db()
        cli_delta = self._snapshot(conn)
        conn.close()

        assert api_delta == cli_delta
        assert len(api_delta[0]) == 1


class TestMemoryDomainEditParity:
    def _setup(self):
        conn = get_system_db()
        _purge_user_state(conn)
        dom_id = _seed_memory_domain(conn, slug="parity-edit-dom", name="Old")
        conn.close()
        return dom_id

    def test_edit_parity(self, parity_env):
        dom_id = self._setup()

        r = parity_env["client"].put(
            f"/api/admin/memory-domains/{dom_id}",
            json={"name": "New"},
            headers=_auth(parity_env["admin_token"]),
        )
        assert r.status_code == 200
        conn = get_system_db()
        api_name = conn.execute("SELECT name FROM memory_domains WHERE id = ?", [dom_id]).fetchone()[0]
        api_audit = _snapshot_audit_actions(conn, prefix="memory_domain.update")
        conn.close()

        conn = get_system_db()
        conn.execute("UPDATE memory_domains SET name = 'Old' WHERE id = ?", [dom_id])
        _reset_audit_log(conn)
        conn.close()

        parity_env["run_cli"](["admin", "memory-domain", "edit", dom_id, "--name", "New"])
        conn = get_system_db()
        cli_name = conn.execute("SELECT name FROM memory_domains WHERE id = ?", [dom_id]).fetchone()[0]
        cli_audit = _snapshot_audit_actions(conn, prefix="memory_domain.update")
        conn.close()

        assert api_name == cli_name == "New"
        assert api_audit == cli_audit


class TestMemoryDomainDeleteParity:
    def _setup(self):
        conn = get_system_db()
        _purge_user_state(conn)
        dom_id = _seed_memory_domain(conn, slug="parity-del-dom")
        conn.close()
        return dom_id

    def test_delete_parity(self, parity_env):
        dom_id = self._setup()

        r = parity_env["client"].delete(
            f"/api/admin/memory-domains/{dom_id}",
            headers=_auth(parity_env["admin_token"]),
        )
        assert r.status_code == 204
        # v54: soft delete (see TestDataPackageDeleteParity above).
        conn = get_system_db()
        api_live = conn.execute(
            "SELECT COUNT(*) FROM memory_domains WHERE id = ? AND deleted_at IS NULL", [dom_id]
        ).fetchone()[0]
        api_soft = conn.execute(
            "SELECT COUNT(*) FROM memory_domains WHERE id = ? AND deleted_at IS NOT NULL", [dom_id]
        ).fetchone()[0]
        api_audit = _snapshot_audit_actions(conn, prefix="memory_domain.delete")
        conn.close()

        dom_id_2 = self._setup()
        parity_env["run_cli"](["admin", "memory-domain", "delete", dom_id_2, "--yes"])
        conn = get_system_db()
        cli_live = conn.execute(
            "SELECT COUNT(*) FROM memory_domains WHERE id = ? AND deleted_at IS NULL", [dom_id_2]
        ).fetchone()[0]
        cli_soft = conn.execute(
            "SELECT COUNT(*) FROM memory_domains WHERE id = ? AND deleted_at IS NOT NULL", [dom_id_2]
        ).fetchone()[0]
        cli_audit = _snapshot_audit_actions(conn, prefix="memory_domain.delete")
        conn.close()

        assert api_live == cli_live == 0
        assert api_soft == cli_soft == 1
        assert len(api_audit) == len(cli_audit) == 1


class TestMemoryDomainAddRemoveItemParity:
    def _setup(self):
        conn = get_system_db()
        _purge_user_state(conn)
        dom_id = _seed_memory_domain(conn, slug="parity-item-dom")
        _seed_knowledge_item(conn, item_id="parity_item_1", title="T")
        conn.close()
        return dom_id, "parity_item_1"

    def test_add_item_parity(self, parity_env):
        dom_id, item_id = self._setup()

        r = parity_env["client"].post(
            f"/api/admin/memory-domains/{dom_id}/items",
            json={"item_id": item_id},
            headers=_auth(parity_env["admin_token"]),
        )
        assert r.status_code == 200
        conn = get_system_db()
        api_state = _snapshot_table(
            conn,
            "SELECT domain_id, item_id FROM knowledge_item_domains",
        )
        api_audit = _snapshot_audit_actions(conn, prefix="memory_domain.add_item")
        conn.close()

        conn = get_system_db()
        conn.execute("DELETE FROM knowledge_item_domains")
        _reset_audit_log(conn)
        conn.close()

        parity_env["run_cli"](["admin", "memory-domain", "add-item", dom_id, item_id])
        conn = get_system_db()
        cli_state = _snapshot_table(
            conn,
            "SELECT domain_id, item_id FROM knowledge_item_domains",
        )
        cli_audit = _snapshot_audit_actions(conn, prefix="memory_domain.add_item")
        conn.close()

        assert api_state == cli_state
        assert api_audit == cli_audit
        assert api_state == [(dom_id, item_id)]

    def test_remove_item_parity(self, parity_env):
        dom_id, item_id = self._setup()
        conn = get_system_db()
        conn.execute(
            "INSERT INTO knowledge_item_domains(item_id, domain_id, added_by) VALUES (?, ?, 'test')",
            [item_id, dom_id],
        )
        conn.close()

        r = parity_env["client"].delete(
            f"/api/admin/memory-domains/{dom_id}/items/{item_id}",
            headers=_auth(parity_env["admin_token"]),
        )
        assert r.status_code == 204
        conn = get_system_db()
        api_state = _snapshot_table(
            conn,
            "SELECT domain_id, item_id FROM knowledge_item_domains",
        )
        api_audit = _snapshot_audit_actions(conn, prefix="memory_domain.remove_item")
        conn.close()

        # Re-link
        conn = get_system_db()
        conn.execute(
            "INSERT INTO knowledge_item_domains(item_id, domain_id, added_by) VALUES (?, ?, 'test')",
            [item_id, dom_id],
        )
        _reset_audit_log(conn)
        conn.close()

        parity_env["run_cli"](["admin", "memory-domain", "remove-item", dom_id, item_id, "--yes"])
        conn = get_system_db()
        cli_state = _snapshot_table(
            conn,
            "SELECT domain_id, item_id FROM knowledge_item_domains",
        )
        cli_audit = _snapshot_audit_actions(conn, prefix="memory_domain.remove_item")
        conn.close()

        assert api_state == cli_state == []
        assert api_audit == cli_audit


# ---------------------------------------------------------------------------
# Corporate-memory lifecycle moderation (approve / require)
# ---------------------------------------------------------------------------


class TestMemoryModerationParity:
    """CLI ``agnes admin memory approve|require`` vs the governance batch
    endpoint they wrap (``POST /api/memory/admin/batch``)."""

    def _setup(self, status: str = "pending") -> str:
        conn = get_system_db()
        _purge_user_state(conn)
        _seed_knowledge_item(conn, item_id="parity_mod_1", title="T", status=status)
        conn.close()
        return "parity_mod_1"

    def test_approve_parity(self, parity_env):
        item_id = self._setup(status="pending")

        r = parity_env["client"].post(
            "/api/memory/admin/batch",
            json={"item_ids": [item_id], "action": "approve"},
            headers=_auth(parity_env["admin_token"]),
        )
        assert r.status_code == 200
        conn = get_system_db()
        api_status = conn.execute("SELECT status FROM knowledge_items WHERE id = ?", [item_id]).fetchone()[0]
        api_audit = _snapshot_audit_actions(conn, prefix="corporate_memory.approve")
        conn.close()

        conn = get_system_db()
        conn.execute("UPDATE knowledge_items SET status = 'pending' WHERE id = ?", [item_id])
        _reset_audit_log(conn)
        conn.close()

        parity_env["run_cli"](["admin", "memory", "approve", item_id])
        conn = get_system_db()
        cli_status = conn.execute("SELECT status FROM knowledge_items WHERE id = ?", [item_id]).fetchone()[0]
        cli_audit = _snapshot_audit_actions(conn, prefix="corporate_memory.approve")
        conn.close()

        assert api_status == cli_status == "approved"
        assert api_audit == cli_audit
        assert len(api_audit) == 1

    def test_require_parity(self, parity_env):
        item_id = self._setup(status="approved")

        r = parity_env["client"].post(
            "/api/memory/admin/batch",
            json={"item_ids": [item_id], "action": "mandate"},
            headers=_auth(parity_env["admin_token"]),
        )
        assert r.status_code == 200
        conn = get_system_db()
        api_required = conn.execute("SELECT is_required FROM knowledge_items WHERE id = ?", [item_id]).fetchone()[0]
        api_audit = _snapshot_audit_actions(conn, prefix="corporate_memory.mandate")
        conn.close()

        conn = get_system_db()
        conn.execute("UPDATE knowledge_items SET is_required = FALSE WHERE id = ?", [item_id])
        _reset_audit_log(conn)
        conn.close()

        parity_env["run_cli"](["admin", "memory", "require", item_id])
        conn = get_system_db()
        cli_required = conn.execute("SELECT is_required FROM knowledge_items WHERE id = ?", [item_id]).fetchone()[0]
        cli_audit = _snapshot_audit_actions(conn, prefix="corporate_memory.mandate")
        conn.close()

        assert bool(api_required) is bool(cli_required) is True
        assert api_audit == cli_audit
        assert len(api_audit) == 1


# ---------------------------------------------------------------------------
# Grants (requirement) — POST + PUT pair
# ---------------------------------------------------------------------------


class TestGrantCreateRequirementParity:
    """``POST /api/admin/grants`` + PUT requirement ↔
    ``agnes admin grant create … --requirement required``.

    CLI route: POST → if 201, PUT requirement when caller asked for
    required. We assert that both code paths end with the same single
    grant row at the requested requirement.
    """

    def _setup_group_and_package(self):
        conn = get_system_db()
        _purge_user_state(conn)
        from src.repositories.user_groups import UserGroupsRepository
        from src.repositories.user_group_members import UserGroupMembersRepository

        g = UserGroupsRepository(conn).create(
            name="parity_grant_g",
            description="",
            created_by="test",
        )
        gid = g["id"] if isinstance(g, dict) else g
        UserGroupMembersRepository(conn).add_member("analyst1", gid, source="test")
        pkg_id = _seed_data_package(conn, slug="parity-grant-pkg")
        conn.close()
        return gid, pkg_id

    def _snapshot_grant(self, conn, gid, pkg_id):
        return _snapshot_table(
            conn,
            "SELECT group_id, resource_type, resource_id, requirement "
            "FROM resource_grants WHERE group_id = ? AND resource_id = ?",
            [gid, pkg_id],
        )

    def test_grant_required_parity(self, parity_env):
        gid, pkg_id = self._setup_group_and_package()

        # API path: POST (creates available) + PUT (flips to required).
        r1 = parity_env["client"].post(
            "/api/admin/grants",
            json={"group_id": gid, "resource_type": "data_package", "resource_id": pkg_id},
            headers=_auth(parity_env["admin_token"]),
        )
        assert r1.status_code == 201, r1.text
        grant_id = r1.json()["id"]
        r2 = parity_env["client"].put(
            f"/api/admin/grants/{grant_id}",
            json={"requirement": "required"},
            headers=_auth(parity_env["admin_token"]),
        )
        assert r2.status_code == 200, r2.text

        conn = get_system_db()
        api_state = self._snapshot_grant(conn, gid, pkg_id)
        conn.close()

        # Reset grants, fire CLI which internally does the same POST+PUT.
        conn = get_system_db()
        conn.execute(
            "DELETE FROM resource_grants WHERE group_id = ? AND resource_id = ?",
            [gid, pkg_id],
        )
        conn.close()

        parity_env["run_cli"](
            ["admin", "grant", "create", "parity_grant_g", "data_package", pkg_id, "--requirement", "required"]
        )
        conn = get_system_db()
        cli_state = self._snapshot_grant(conn, gid, pkg_id)
        conn.close()

        assert api_state == cli_state
        # Single required grant row.
        assert len(api_state) == 1
        assert api_state[0][3] == "required"


class TestGrantUpdateRequirementParity:
    """``PUT /api/admin/grants/{id}`` (requirement update) ↔ CLI grant create
    when an existing grant is detected (409 → list → PUT).
    """

    def _setup_with_grant(self, requirement: str):
        conn = get_system_db()
        _purge_user_state(conn)
        from src.repositories.user_groups import UserGroupsRepository
        from src.repositories.user_group_members import UserGroupMembersRepository

        g = UserGroupsRepository(conn).create(
            name="parity_put_g",
            description="",
            created_by="test",
        )
        gid = g["id"] if isinstance(g, dict) else g
        UserGroupMembersRepository(conn).add_member("analyst1", gid, source="test")
        pkg_id = _seed_data_package(conn, slug="parity-put-pkg")
        grant_id = _seed_grant_for(conn, gid, "data_package", pkg_id, requirement)
        conn.close()
        return gid, pkg_id, grant_id

    def test_downgrade_parity_no_subscription_fan_out(self, parity_env, monkeypatch):
        """Auto-membership (opt-in): a required → available downgrade
        writes NO user_stack_subscriptions rows on EITHER path — available
        is already automatically in every granted user's stack. The classic
        default fans out on both paths identically (sibling below)."""
        monkeypatch.setenv("AGNES_STACK_AUTO_MEMBERSHIP", "1")
        gid, pkg_id, grant_id = self._setup_with_grant("required")

        # API path
        r = parity_env["client"].put(
            f"/api/admin/grants/{grant_id}",
            json={"requirement": "available"},
            headers=_auth(parity_env["admin_token"]),
        )
        assert r.status_code == 200, r.text
        conn = get_system_db()
        api_grants = _snapshot_table(
            conn,
            "SELECT group_id, resource_type, resource_id, requirement FROM resource_grants WHERE id = ?",
            [grant_id],
        )
        api_subs = _snapshot_table(
            conn,
            "SELECT user_id, resource_type, resource_id FROM user_stack_subscriptions WHERE resource_id = ?",
            [pkg_id],
        )
        conn.close()

        # Reset to required, re-run CLI
        conn = get_system_db()
        conn.execute(
            "UPDATE resource_grants SET requirement = 'required' WHERE id = ?",
            [grant_id],
        )
        conn.close()

        # CLI: same group/resource → POST returns 409 → CLI lists → finds
        # existing grant → PUTs requirement update.
        parity_env["run_cli"](
            ["admin", "grant", "create", "parity_put_g", "data_package", pkg_id, "--requirement", "available"]
        )
        conn = get_system_db()
        cli_grants = _snapshot_table(
            conn,
            "SELECT group_id, resource_type, resource_id, requirement FROM resource_grants WHERE id = ?",
            [grant_id],
        )
        cli_subs = _snapshot_table(
            conn,
            "SELECT user_id, resource_type, resource_id FROM user_stack_subscriptions WHERE resource_id = ?",
            [pkg_id],
        )
        conn.close()

        assert api_grants == cli_grants
        # Neither path writes a subscription row anymore.
        assert api_subs == cli_subs == []

    def test_downgrade_parity_classic_fan_out(self, parity_env, monkeypatch):
        """Classic: the required → available downgrade fans out a
        subscription row per group member — the pre-redesign v49 behavior
        (spec 2026-08-07-default-chrome-ux-parity) — and BOTH paths land on
        the identical row set (the fan-out is idempotent, so the CLI re-run
        over the API-created rows changes nothing).

        Classic is no longer the presetless default (Wave 0, 2026-08, coupled
        the sole remaining `redesign` experience to auto-membership) — forced
        here via the still-fully-supported explicit per-knob override, which
        wins over any preset."""
        monkeypatch.setenv("AGNES_STACK_AUTO_MEMBERSHIP", "0")
        gid, pkg_id, grant_id = self._setup_with_grant("required")

        # API path
        r = parity_env["client"].put(
            f"/api/admin/grants/{grant_id}",
            json={"requirement": "available"},
            headers=_auth(parity_env["admin_token"]),
        )
        assert r.status_code == 200, r.text
        conn = get_system_db()
        api_subs = _snapshot_table(
            conn,
            "SELECT user_id, resource_type, resource_id FROM user_stack_subscriptions WHERE resource_id = ?",
            [pkg_id],
        )
        # Reset the grant; keep the subscriptions (idempotent fan-out).
        conn.execute(
            "UPDATE resource_grants SET requirement = 'required' WHERE id = ?",
            [grant_id],
        )
        conn.close()
        assert api_subs == [("analyst1", "data_package", pkg_id)]

        # CLI path: 409 → list → PUT requirement update.
        parity_env["run_cli"](
            ["admin", "grant", "create", "parity_put_g", "data_package", pkg_id, "--requirement", "available"]
        )
        conn = get_system_db()
        cli_subs = _snapshot_table(
            conn,
            "SELECT user_id, resource_type, resource_id FROM user_stack_subscriptions WHERE resource_id = ?",
            [pkg_id],
        )
        conn.close()
        assert api_subs == cli_subs


# ---------------------------------------------------------------------------
# Jobs (wave-2B worker queue) — enqueue
# ---------------------------------------------------------------------------


class TestJobEnqueueParity:
    """``POST /api/jobs`` ↔ ``agnes admin jobs enqueue <kind>``."""

    def _snapshot(self, conn, kind: str) -> tuple:
        job_rows = _snapshot_table(
            conn,
            "SELECT kind, status, payload_json FROM jobs WHERE kind = ?",
            [kind],
        )
        # `_enqueued_by_request` (app/job_correlation.py, wave-2D Task 4)
        # stamps the *live* HTTP request-id onto the payload at enqueue
        # time — both the API call and the CLI's own `POST /api/jobs`
        # call go through the same endpoint, but each is a distinct HTTP
        # request, so they mint distinct request-ids even for an
        # otherwise byte-identical payload. Mask the value to a
        # placeholder before comparing (same convention
        # `_snapshot_audit_actions` already uses for uuid-suffixed audit
        # resource ids), after first asserting the key actually landed as
        # a non-empty string — the correlation stamp itself is part of
        # what parity should verify, just not its exact value.
        masked_job_rows = []
        for row_kind, status, payload_json in job_rows:
            payload = json.loads(payload_json)
            rid = payload.get("_enqueued_by_request")
            assert isinstance(rid, str) and rid, f"expected a non-empty _enqueued_by_request, got {rid!r}"
            payload["_enqueued_by_request"] = "<rid>"
            masked_job_rows.append((row_kind, status, json.dumps(payload, sort_keys=True)))
        audit_rows = _snapshot_audit_actions(conn, prefix="job.enqueue")
        return (sorted(masked_job_rows), audit_rows)

    def test_enqueue_parity(self, parity_env):
        from app.worker.registry import LIGHT_LANE, JOB_KINDS, JobKind, register_kind

        JOB_KINDS.clear()
        register_kind(JobKind(name="parity-kind", handler=lambda payload: None, lane=LIGHT_LANE))
        try:
            conn = get_system_db()
            conn.execute("DELETE FROM jobs WHERE kind = 'parity-kind'")
            _reset_audit_log(conn)
            conn.close()

            # API
            r = parity_env["client"].post(
                "/api/jobs",
                json={"kind": "parity-kind", "payload": {"x": 1}},
                headers=_auth(parity_env["admin_token"]),
            )
            assert r.status_code == 202, r.text
            conn = get_system_db()
            api_delta = self._snapshot(conn, "parity-kind")
            conn.close()

            # Reset jobs + audit, re-fire via CLI.
            conn = get_system_db()
            conn.execute("DELETE FROM jobs WHERE kind = 'parity-kind'")
            _reset_audit_log(conn)
            conn.close()

            parity_env["run_cli"](["admin", "jobs", "enqueue", "parity-kind", "--payload", '{"x": 1}'])
            conn = get_system_db()
            cli_delta = self._snapshot(conn, "parity-kind")
            conn.close()

            assert api_delta == cli_delta
            # Both paths queued exactly one job + one audit row.
            assert len(api_delta[0]) == 1
            assert len(api_delta[1]) == 1
        finally:
            JOB_KINDS.clear()


# ---------------------------------------------------------------------------
# Per-test auth swap helper (re-patches the CLI helpers to a non-admin token)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _admin_auth_swap(parity_env, token: str):
    """Temporarily re-patch ``cli.commands.stack`` to use a different token.

    The fixture-level patch hard-wires the admin token. Tests that need
    analyst auth (subscribe/unsubscribe/list) flip it just for the CLI
    invocation, then restore.
    """
    from cli.commands import stack as _stack_mod

    client = parity_env["client"]

    def _normalize(headers, t):
        out = {"Authorization": f"Bearer {t}"}
        if headers:
            out.update(headers)
        return out

    orig = {
        "api_get": _stack_mod.api_get,
        "api_post": _stack_mod.api_post,
        "api_delete": _stack_mod.api_delete,
    }

    def _get(path: str, *, timeout: float = 30.0, **kwargs):
        kwargs["headers"] = _normalize(kwargs.get("headers"), token)
        return client.get(path, **kwargs)

    def _post(path: str, *, timeout: float = 30.0, **kwargs):
        kwargs["headers"] = _normalize(kwargs.get("headers"), token)
        return client.post(path, **kwargs)

    def _delete(path: str, *, timeout: float = 30.0, **kwargs):
        kwargs["headers"] = _normalize(kwargs.get("headers"), token)
        return client.delete(path, **kwargs)

    _stack_mod.api_get = _get
    _stack_mod.api_post = _post
    _stack_mod.api_delete = _delete
    try:
        yield
    finally:
        for k, v in orig.items():
            setattr(_stack_mod, k, v)


# ---------------------------------------------------------------------------
# MCP source OAuth client provisioning (register + manual config)
# ---------------------------------------------------------------------------


def _oauth_parity_env(monkeypatch):
    """Vault key + no-network DNS stub shared by the OAuth parity cases."""
    import ipaddress
    import socket as socket_mod

    from cryptography.fernet import Fernet

    from app.secrets_vault import _reset_ephemeral_key_for_tests

    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    _reset_ephemeral_key_for_tests()

    real_getaddrinfo = socket_mod.getaddrinfo

    def _fake(host, *args, **kwargs):
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return [(socket_mod.AF_INET, socket_mod.SOCK_STREAM, 0, "", ("93.184.216.34", 0))]
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr("src.net.ssrf_safe_client.socket.getaddrinfo", _fake)


def _seed_oauth_parity_source(source_id):
    from src.repositories.mcp_sources import MCPSourceRepository

    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id=source_id,
        name=source_id,
        transport="http",
        url="https://mcp.example.com/mcp",
        auth_method="oauth",
        scope="per_user",
    )
    conn.close()


def _snapshot_oauth_client_row(source_id):
    conn = get_system_db()
    rows = _snapshot_table(
        conn,
        "SELECT source_id, client_id, issuer, authorization_endpoint, token_endpoint"
        " FROM mcp_source_oauth_clients WHERE source_id = ?",
        [source_id],
    )
    conn.close()
    return rows


def _drop_oauth_client_row(source_id):
    from src.repositories import mcp_source_oauth_clients_repo

    mcp_source_oauth_clients_repo().delete(source_id)


class TestMcpSourceOAuthManualClientParity:
    """``PUT /api/admin/mcp-sources/{id}/oauth/client`` ↔
    ``agnes admin mcp source oauth-client``."""

    def test_manual_client_parity(self, parity_env, monkeypatch):
        _oauth_parity_env(monkeypatch)
        sid = "src_par_oauth_manual"
        _seed_oauth_parity_source(sid)
        body = {
            "client_id": "manual-cid",
            "authorization_endpoint": "https://as.example.com/authorize",
            "token_endpoint": "https://as.example.com/token",
        }

        r = parity_env["client"].put(
            f"/api/admin/mcp-sources/{sid}/oauth/client",
            json=body,
            headers=_auth(parity_env["admin_token"]),
        )
        assert r.status_code == 200, r.text
        delta_api = _snapshot_oauth_client_row(sid)

        _drop_oauth_client_row(sid)

        parity_env["run_cli"](
            [
                "admin",
                "mcp",
                "source",
                "oauth-client",
                sid,
                "--client-id",
                "manual-cid",
                "--authorization-endpoint",
                "https://as.example.com/authorize",
                "--token-endpoint",
                "https://as.example.com/token",
                "--public-client",
            ]
        )
        delta_cli = _snapshot_oauth_client_row(sid)

        assert delta_api == delta_cli
        assert len(delta_cli) == 1 and delta_cli[0][1] == "manual-cid"


class TestMcpSourceOAuthRegisterParity:
    """``POST /api/admin/mcp-sources/{id}/oauth/register`` ↔
    ``agnes admin mcp source oauth-register`` (discovery/DCR mocked at the
    ``connectors.mcp.oauth_client`` seam, same as the endpoint tests)."""

    def _patch_discovery(self, monkeypatch):
        import connectors.mcp.oauth_client as oc

        as_metadata = {
            "issuer": "https://as.example.com",
            "authorization_endpoint": "https://as.example.com/authorize",
            "token_endpoint": "https://as.example.com/token",
            "registration_endpoint": "https://as.example.com/register",
            "code_challenge_methods_supported": ["S256"],
        }

        async def _fake_discover_pr(source_url, *, client):
            return {"authorization_servers": ["https://as.example.com"]}

        async def _fake_discover_as(issuer, *, client):
            return as_metadata

        async def _fake_register(meta, *, redirect_uri, client, scopes=None, client_name="Agnes"):
            return oc.RegisteredOAuthClient(
                issuer=meta["issuer"],
                client_id="parity-client-id",
                client_secret="parity-secret",
                registration_access_token="parity-rat",
                authorization_endpoint=meta["authorization_endpoint"],
                token_endpoint=meta["token_endpoint"],
                registration_endpoint=meta["registration_endpoint"],
                scopes=scopes,
            )

        async def _fake_revoke(*, registration_endpoint, client_id, registration_access_token, client):
            return None

        monkeypatch.setattr(oc, "discover_protected_resource_metadata", _fake_discover_pr)
        monkeypatch.setattr(oc, "discover_as_metadata", _fake_discover_as)
        monkeypatch.setattr(oc, "register_dynamic_client", _fake_register)
        monkeypatch.setattr(oc, "best_effort_revoke_registration", _fake_revoke)

    def test_register_parity(self, parity_env, monkeypatch):
        _oauth_parity_env(monkeypatch)
        monkeypatch.setenv("PUBLIC_URL", "https://agnes.example.com")
        self._patch_discovery(monkeypatch)
        sid = "src_par_oauth_reg"
        _seed_oauth_parity_source(sid)

        r = parity_env["client"].post(
            f"/api/admin/mcp-sources/{sid}/oauth/register",
            headers=_auth(parity_env["admin_token"]),
        )
        assert r.status_code == 200, r.text
        delta_api = _snapshot_oauth_client_row(sid)

        _drop_oauth_client_row(sid)

        parity_env["run_cli"](["admin", "mcp", "source", "oauth-register", sid])
        delta_cli = _snapshot_oauth_client_row(sid)

        assert delta_api == delta_cli
        assert len(delta_cli) == 1 and delta_cli[0][1] == "parity-client-id"


class TestMcpOAuthDisconnectParity:
    """``DELETE /api/mcp/sources/{id}/oauth/connection`` ↔
    ``agnes mcp disconnect`` (2026-07-30 outbound MCP OAuth sources spec §3,
    PR 2). Admin short-circuits the grant gate, so no tool/grant seeding is
    needed — only the source row and the token row under test."""

    def test_disconnect_parity(self, parity_env, monkeypatch):
        from src.repositories import mcp_user_oauth_tokens_repo

        _oauth_parity_env(monkeypatch)
        sid = "src_par_oauth_disc"
        _seed_oauth_parity_source(sid)
        tokens = mcp_user_oauth_tokens_repo()

        tokens.upsert(sid, "admin1", "atok", refresh_token="rtok")
        r = parity_env["client"].delete(
            f"/api/mcp/sources/{sid}/oauth/connection",
            headers=_auth(parity_env["admin_token"]),
        )
        assert r.status_code == 204, r.text
        delta_api = tokens.has(sid, "admin1")
        assert delta_api is False

        tokens.upsert(sid, "admin1", "atok2", refresh_token="rtok2")
        parity_env["run_cli"](["mcp", "disconnect", sid, "--yes"])
        delta_cli = tokens.has(sid, "admin1")

        assert delta_api == delta_cli is False


class TestKeboolaChatToolsParity:
    """``POST/DELETE /api/admin/source-connections/{id}/chat-tools`` ↔
    ``agnes admin connection chat-tools [--disable]``.

    Both paths must land the same derived ``mcp_sources`` row AND the same
    vault copy of the connection's token — a CLI that created the source but
    skipped the secret would produce a source that starts and then fails every
    tool call at the far end.
    """

    @pytest.fixture(autouse=True)
    def _stable_vault_key(self, monkeypatch):
        from cryptography.fernet import Fernet

        from app.secrets_vault import _reset_ephemeral_key_for_tests

        monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
        _reset_ephemeral_key_for_tests()
        yield
        _reset_ephemeral_key_for_tests()

    @pytest.fixture(autouse=True)
    def _storage_api_is_reachable(self):
        """Storing a token runs a live `verify_token` preflight (#1242) and
        re-validates the stack URL, DNS included. These two tests are about
        REST/CLI parity on the chat-tools pair, not about the Storage API, so
        one stable project answers for both surfaces."""
        from unittest.mock import patch

        with (
            patch("app.api.admin_source_connections.KeboolaStorageClient.verify_token") as verify,
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            verify.return_value = {"isMasterToken": False, "owner": {"id": 4242, "name": "Test Project"}}
            yield verify

    @pytest.fixture(autouse=True)
    def _fake_upstream(self, monkeypatch):
        """Enabling dials the upstream to register its tools; stand in for it
        so parity neither spawns `uv` nor reaches the network."""
        from connectors.mcp.client import ToolInfo

        async def _fake_list_tools(source, *, caller_user_id=None):
            return [ToolInfo(name="query_data", description="sql", input_schema=None, read_only=True)]

        monkeypatch.setattr("connectors.mcp.client.list_tools_async", _fake_list_tools)

    def _seed_connection(self, parity_env, name: str) -> str:
        c, token = parity_env["client"], parity_env["admin_token"]
        r = c.post(
            "/api/admin/source-connections",
            json={
                "name": name,
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert r.status_code == 201, r.text
        conn_id = r.json()["id"]
        assert (
            c.put(
                f"/api/admin/source-connections/{conn_id}/secret",
                json={"value": "parity-token"},
                headers=_auth(token),
            ).status_code
            == 204
        )
        return conn_id

    def _snapshot(self, conn_id: str) -> tuple:
        from src.keboola_chat_tools import derived_source_id
        from src.repositories import mcp_sources_repo, shared_secrets_repo, tool_registry_repo

        source_id = derived_source_id(conn_id)
        row = mcp_sources_repo().get(source_id)
        projected = None
        if row is not None:
            projected = (row["transport"], row["command"], json.dumps(row.get("args")), row.get("auth_secret_env"))
        tools = sorted(
            (t["original_name"], t["exposed_name"], t["mode"], bool(t["mutating"]))
            for t in tool_registry_repo().list_for_source(source_id)
        )
        return (projected, shared_secrets_repo().get(source_id), tools)

    def test_enable_parity(self, parity_env):
        conn_id = self._seed_connection(parity_env, "parity-chat-tools")

        r = parity_env["client"].post(
            f"/api/admin/source-connections/{conn_id}/chat-tools",
            headers=_auth(parity_env["admin_token"]),
        )
        assert r.status_code == 201, r.text
        delta_api = self._snapshot(conn_id)

        parity_env["client"].delete(
            f"/api/admin/source-connections/{conn_id}/chat-tools",
            headers=_auth(parity_env["admin_token"]),
        )
        assert self._snapshot(conn_id) == (None, None, [])

        parity_env["run_cli"](["admin", "connection", "chat-tools", conn_id])
        delta_cli = self._snapshot(conn_id)

        assert delta_api == delta_cli
        assert delta_api[1] == "parity-token"

    def test_disable_parity(self, parity_env):
        conn_id = self._seed_connection(parity_env, "parity-chat-tools-off")
        client, token = parity_env["client"], parity_env["admin_token"]

        client.post(f"/api/admin/source-connections/{conn_id}/chat-tools", headers=_auth(token))
        r = client.delete(f"/api/admin/source-connections/{conn_id}/chat-tools", headers=_auth(token))
        assert r.status_code == 204
        delta_api = self._snapshot(conn_id)

        client.post(f"/api/admin/source-connections/{conn_id}/chat-tools", headers=_auth(token))
        parity_env["run_cli"](["admin", "connection", "chat-tools", conn_id, "--disable"])
        delta_cli = self._snapshot(conn_id)

        assert delta_api == delta_cli == (None, None, [])


class TestMCPSourceBulkGrantParity:
    """``POST/DELETE /api/admin/mcp-sources/{id}/grants`` ↔
    ``agnes admin mcp source grant [--revoke]``.

    Both paths must land the same `tool_grants` rows AND the same audit line.
    A CLI that granted without auditing, or that resolved the source
    differently, would widen access with a thinner trail than the API.
    """

    @pytest.fixture(autouse=True)
    def _stable_vault_key(self, monkeypatch):
        from cryptography.fernet import Fernet

        from app.secrets_vault import _reset_ephemeral_key_for_tests

        monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
        _reset_ephemeral_key_for_tests()
        yield
        _reset_ephemeral_key_for_tests()

    def _seed(self, parity_env):
        """A source with two registered tools, and a group to grant them to."""
        from src.repositories import mcp_sources_repo, tool_registry_repo, user_groups_repo

        source_id = "src_parity_bulk_grant"
        mcp_sources_repo().upsert(
            id=source_id,
            name="Parity bulk grant",
            transport="stdio",
            command="true",
            args=[],
            scope="shared",
        )
        repo = tool_registry_repo()
        for name in ("alpha", "beta"):
            repo.upsert(
                tool_id=f"{source_id}__{name}",
                source_id=source_id,
                original_name=name,
                exposed_name=f"parity_{name}",
                mode="passthrough",
            )
        gid = user_groups_repo().get_by_name("Everyone")["id"]
        return source_id, gid

    def _snapshot(self, source_id: str, gid: str):
        from src.repositories import tool_registry_repo

        repo = tool_registry_repo()
        return sorted(
            t["tool_id"] for t in repo.list_for_source(source_id) if gid in repo.grants_for_tool(t["tool_id"])
        )

    def test_grant_parity(self, parity_env):
        source_id, gid = self._seed(parity_env)

        r = parity_env["client"].post(
            f"/api/admin/mcp-sources/{source_id}/grants",
            json={"group_id": gid},
            headers=_auth(parity_env["admin_token"]),
        )
        assert r.status_code == 200, r.text
        conn = get_system_db()
        api_state = self._snapshot(source_id, gid)
        api_audit = _snapshot_audit_actions(conn, prefix="mcp_source.grant")
        conn.close()

        parity_env["client"].delete(
            f"/api/admin/mcp-sources/{source_id}/grants/{gid}",
            headers=_auth(parity_env["admin_token"]),
        )
        conn = get_system_db()
        _reset_audit_log(conn)
        conn.close()
        assert self._snapshot(source_id, gid) == []

        parity_env["run_cli"](["admin", "mcp", "source", "grant", source_id, "--group", gid])
        conn = get_system_db()
        cli_state = self._snapshot(source_id, gid)
        cli_audit = _snapshot_audit_actions(conn, prefix="mcp_source.grant")
        conn.close()

        assert api_state == cli_state
        assert api_audit == cli_audit
        assert len(api_state) == 2

    def test_revoke_parity(self, parity_env):
        source_id, gid = self._seed(parity_env)
        client, token = parity_env["client"], parity_env["admin_token"]

        client.post(f"/api/admin/mcp-sources/{source_id}/grants", json={"group_id": gid}, headers=_auth(token))
        assert (
            client.delete(f"/api/admin/mcp-sources/{source_id}/grants/{gid}", headers=_auth(token)).status_code == 204
        )
        api_state = self._snapshot(source_id, gid)

        client.post(f"/api/admin/mcp-sources/{source_id}/grants", json={"group_id": gid}, headers=_auth(token))
        parity_env["run_cli"](["admin", "mcp", "source", "grant", source_id, "--group", gid, "--revoke"])
        cli_state = self._snapshot(source_id, gid)

        assert api_state == cli_state == []


# ---------------------------------------------------------------------------
# MCP per-tool grant (allow_mutating opt-in, v120)
# ---------------------------------------------------------------------------


class TestMcpToolGrantParity:
    """``POST /api/admin/mcp-tools/{id}/grants`` ↔ ``agnes admin mcp tool grant``."""

    def _seed_tool_and_group(self, conn, *, tool_id="tg.parity", gname="parity_mut"):
        from src.repositories.mcp_sources import MCPSourceRepository
        from src.repositories.tool_registry import PASSTHROUGH, ToolRegistryRepository

        gid = _seed_group_with_user(conn, name=gname, user_id="analyst1")
        MCPSourceRepository(conn).upsert(id="src_parity", name="parity-src", transport="stdio", command="/bin/true")
        ToolRegistryRepository(conn).upsert(
            tool_id=tool_id,
            source_id="src_parity",
            original_name="write_thing",
            exposed_name="write_thing",
            mode=PASSTHROUGH,
            mutating=True,
        )
        return gid

    def _grants_snapshot(self, conn, tool_id="tg.parity"):
        return _snapshot_table(
            conn,
            "SELECT tool_id, group_id, COALESCE(allow_mutating, FALSE) FROM tool_grants WHERE tool_id = ?",
            [tool_id],
        )

    def test_grant_with_allow_mutating_parity(self, parity_env):
        conn = get_system_db()
        _purge_user_state(conn)
        gid = self._seed_tool_and_group(conn)
        conn.close()

        r = parity_env["client"].post(
            "/api/admin/mcp-tools/tg.parity/grants",
            json={"group_id": gid, "allow_mutating": True},
            headers=_auth(parity_env["admin_token"]),
        )
        assert r.status_code == 200, r.text
        assert r.json()["allow_mutating"] is True
        conn = get_system_db()
        delta_api = self._grants_snapshot(conn)
        conn.execute("DELETE FROM tool_grants WHERE tool_id = 'tg.parity'")
        conn.close()

        parity_env["run_cli"](["admin", "mcp", "tool", "grant", "tg.parity", "--group", gid, "--allow-mutating"])

        conn = get_system_db()
        delta_cli = self._grants_snapshot(conn)
        conn.close()

        assert delta_api == delta_cli == [("tg.parity", gid, True)]

    def test_regrant_downgrades_to_read_only_parity(self, parity_env):
        conn = get_system_db()
        _purge_user_state(conn)
        gid = self._seed_tool_and_group(conn, tool_id="tg.parity2", gname="parity_mut2")
        conn.close()

        for path in ("api", "cli"):
            # start each path from the same opted-in state
            conn = get_system_db()
            conn.execute("DELETE FROM tool_grants WHERE tool_id = 'tg.parity2'")
            conn.execute(
                "INSERT INTO tool_grants(tool_id, group_id, allow_mutating) VALUES ('tg.parity2', ?, TRUE)",
                [gid],
            )
            conn.close()
            if path == "api":
                r = parity_env["client"].post(
                    "/api/admin/mcp-tools/tg.parity2/grants",
                    json={"group_id": gid, "allow_mutating": False},
                    headers=_auth(parity_env["admin_token"]),
                )
                assert r.status_code == 200, r.text
            else:
                parity_env["run_cli"](
                    ["admin", "mcp", "tool", "grant", "tg.parity2", "--group", gid, "--no-allow-mutating"]
                )
            conn = get_system_db()
            snap = self._grants_snapshot(conn, "tg.parity2")
            conn.close()
            assert snap == [("tg.parity2", gid, False)], f"path={path}"


# ---------------------------------------------------------------------------
# Marketplace plugin disable / enable (agnes admin marketplace)
# ---------------------------------------------------------------------------


class TestMarketplacePluginDisableParity:
    """``POST /api/marketplaces/{id}/plugins/{name}/disable`` (with
    ``revoke_grants``) ↔ ``agnes admin marketplace disable-plugin <ref>
    --revoke-grants`` — and the enable direction. Same admin_disabled flip,
    same grant deletion, same audit rows."""

    MKT = "parity-mkt"
    PLUGIN = "p1"

    def _seed(self):
        from datetime import datetime, timezone

        conn = get_system_db()
        _reset_audit_log(conn)
        conn.execute("DELETE FROM resource_grants WHERE resource_id = ?", [f"{self.MKT}/{self.PLUGIN}"])
        conn.execute(
            "INSERT INTO marketplace_registry (id, name, url, registered_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
            [self.MKT, "Parity MKT", "https://example.test/parity.git", datetime.now(timezone.utc)],
        )
        conn.execute(
            "INSERT INTO marketplace_plugins (marketplace_id, name, raw, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (marketplace_id, name) "
            "DO UPDATE SET admin_disabled = FALSE",
            [self.MKT, self.PLUGIN, json.dumps({"name": self.PLUGIN}), datetime.now(timezone.utc)],
        )
        gid = _seed_group_with_user(conn, name=f"parity_mkt_{uuid.uuid4().hex[:6]}", user_id="analyst1")
        _seed_grant_for(conn, gid, "marketplace_plugin", f"{self.MKT}/{self.PLUGIN}")
        conn.close()
        return gid

    def _snapshot(self, conn):
        state = _snapshot_table(
            conn,
            "SELECT admin_disabled FROM marketplace_plugins WHERE marketplace_id = ? AND name = ?",
            [self.MKT, self.PLUGIN],
        )
        grants = _snapshot_table(
            conn,
            "SELECT resource_type, resource_id FROM resource_grants WHERE resource_id = ?",
            [f"{self.MKT}/{self.PLUGIN}"],
        )
        audit = _snapshot_table(
            conn,
            "SELECT action, resource, params FROM audit_log "
            "WHERE action LIKE 'marketplace.plugin.%' ORDER BY id",
        )
        return (state, grants, audit)

    def test_disable_with_revoke_parity(self, parity_env):
        ref = f"{self.MKT}/{self.PLUGIN}"

        # API path
        self._seed()
        r = parity_env["client"].post(
            f"/api/marketplaces/{self.MKT}/plugins/{self.PLUGIN}/disable",
            json={"revoke_grants": True},
            headers=_auth(parity_env["admin_token"]),
        )
        assert r.status_code == 200, r.text
        assert r.json()["revoked_grants"] == 1
        conn = get_system_db()
        delta_api = self._snapshot(conn)
        conn.close()

        # CLI path (fresh seed resets flag + grant + audit)
        self._seed()
        parity_env["run_cli"](["admin", "marketplace", "disable-plugin", ref, "--revoke-grants"])
        conn = get_system_db()
        delta_cli = self._snapshot(conn)
        conn.close()

        assert delta_api == delta_cli
        state, grants, audit = delta_api
        assert state == [(True,)]
        assert grants == []
        assert len(audit) == 1

    def test_enable_parity(self, parity_env):
        ref = f"{self.MKT}/{self.PLUGIN}"

        def _disable_first():
            self._seed()
            conn = get_system_db()
            conn.execute(
                "UPDATE marketplace_plugins SET admin_disabled = TRUE "
                "WHERE marketplace_id = ? AND name = ?",
                [self.MKT, self.PLUGIN],
            )
            _reset_audit_log(conn)
            conn.close()

        # API path
        _disable_first()
        r = parity_env["client"].post(
            f"/api/marketplaces/{self.MKT}/plugins/{self.PLUGIN}/enable",
            headers=_auth(parity_env["admin_token"]),
        )
        assert r.status_code == 200, r.text
        conn = get_system_db()
        delta_api = self._snapshot(conn)
        conn.close()

        # CLI path
        _disable_first()
        parity_env["run_cli"](["admin", "marketplace", "enable-plugin", ref])
        conn = get_system_db()
        delta_cli = self._snapshot(conn)
        conn.close()

        assert delta_api == delta_cli
        state, grants, _ = delta_api
        assert state == [(False,)]
        assert grants != [], "enable must NOT touch grants"
