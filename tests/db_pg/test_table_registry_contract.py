"""Cross-engine contract test for the table_registry repository.

Pins ``count_non_internal`` on both backends — the single repo method that
backs the dashboard headline "total tables" counter (app/web/router.py
dashboard) and the /catalog empty-state hint (app/web/router.py catalog).
Both handlers previously did a raw ``conn.execute`` on the DuckDB-typed
connection, so on Postgres-backed deployments the count silently read off an
empty DuckDB table. Parametrising over both backends through the repo
factory makes that drift impossible.

The contract: COUNT(*) FROM table_registry WHERE
COALESCE(source_type, '') != 'internal' — i.e. exclude ``source_type='internal``
rows, but count NULL / empty-string source_type as non-internal.
"""

from __future__ import annotations

import pytest


def _make_duckdb_repo(tmp_path):
    # Route through `_open_duckdb` (rather than bare `duckdb.connect`) so the
    # session timezone is pinned to UTC — `tests/test_duckdb_session_tz.py`
    # regression guard catches any new bare connect under `tests/db_pg/`.
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.table_registry import TableRegistryRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return {
        "registry": TableRegistryRepository(conn),
        "conn": conn,
        "backend": "duckdb",
    }


def _make_pg_repo(pg_engine, monkeypatch):
    from pathlib import Path
    from alembic import command
    from alembic.config import Config

    REPO_ROOT = Path(__file__).resolve().parents[2]
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.table_registry_pg import TableRegistryPgRepository

    eng = db_pg.get_engine()
    return {
        "registry": TableRegistryPgRepository(eng),
        "engine": eng,
        "backend": "pg",
    }


@pytest.fixture(params=["duckdb", "pg"], ids=["duck", "pg"])
def repos(request, tmp_path, pg_engine, monkeypatch):
    if request.param == "duckdb":
        bundle = _make_duckdb_repo(tmp_path)
        yield bundle
        bundle["conn"].close()
    else:
        bundle = _make_pg_repo(pg_engine, monkeypatch)
        yield bundle


def _seed(repos: dict, id: str, name: str, source_type=None) -> None:
    """Seed a table_registry row via the repo's own register() upsert so the
    test exercises real write semantics on both backends."""
    repos["registry"].register(id=id, name=name, source_type=source_type)


class TestGetByName:
    def test_returns_row_when_name_matches(self, repos):
        _seed(repos, "mat_001", "Orders Daily", source_type="bigquery")
        row = repos["registry"].get_by_name("Orders Daily")
        assert row is not None
        assert row["id"] == "mat_001"
        assert row["name"] == "Orders Daily"

    def test_returns_none_when_name_absent(self, repos):
        _seed(repos, "mat_002", "Sales Weekly", source_type="bigquery")
        assert repos["registry"].get_by_name("nonexistent") is None

    def test_name_differs_from_id(self, repos):
        """id != name: get_by_name must hit WHERE name = ?, not WHERE id = ?."""
        _seed(repos, "tbl_id", "human_readable_name", source_type="keboola")
        assert repos["registry"].get_by_name("human_readable_name") is not None
        assert repos["registry"].get_by_name("tbl_id") is None


class TestCountNonInternal:
    def test_empty_registry_is_zero(self, repos):
        assert repos["registry"].count_non_internal() == 0

    def test_excludes_internal_rows(self, repos):
        _seed(repos, "t-keboola", "Keboola Table", source_type="keboola")
        _seed(repos, "t-bq", "BigQuery Table", source_type="bigquery")
        _seed(repos, "t-internal", "Agnes Internal", source_type="internal")

        # 3 rows registered, one is internal → counter is 2.
        assert repos["registry"].count_non_internal() == 2

    def test_null_source_type_counts_as_non_internal(self, repos):
        _seed(repos, "t-null", "No Source Type", source_type=None)
        _seed(repos, "t-internal", "Agnes Internal", source_type="internal")

        # NULL source_type is COALESCE'd to '' which != 'internal' → counted.
        assert repos["registry"].count_non_internal() == 1

    def test_returns_int(self, repos):
        _seed(repos, "t-x", "X", source_type="jira")
        result = repos["registry"].count_non_internal()
        assert isinstance(result, int)
        assert result == 1


class TestDeleteInternalExcept:
    """Pins ``delete_internal_except`` — backs
    ``connectors.internal.registry.ensure_internal_tables_registered``'s
    stale-row eviction (a renamed internal table's old id must not linger
    in /catalog forever). Previously a raw ``DELETE ... WHERE source_type =
    'internal' AND id NOT IN (...)`` on the DuckDB-typed conn, silently
    no-op on Postgres-backed deployments."""

    def test_deletes_internal_rows_not_in_keep_list(self, repos):
        _seed(repos, "agnes_sessions", "Sessions", source_type="internal")
        _seed(repos, "agnes_old_name", "Old Telemetry", source_type="internal")

        removed = repos["registry"].delete_internal_except(["agnes_sessions"])
        assert removed == 1
        assert repos["registry"].get("agnes_old_name") is None
        assert repos["registry"].get("agnes_sessions") is not None

    def test_leaves_non_internal_rows_alone(self, repos):
        _seed(repos, "t-keboola", "Keboola Table", source_type="keboola")
        _seed(repos, "agnes_old_name", "Old Telemetry", source_type="internal")

        removed = repos["registry"].delete_internal_except([])
        assert removed == 1
        assert repos["registry"].get("t-keboola") is not None
        assert repos["registry"].get("agnes_old_name") is None

    def test_noop_when_all_internal_rows_are_kept(self, repos):
        _seed(repos, "agnes_sessions", "Sessions", source_type="internal")
        _seed(repos, "agnes_telemetry", "Telemetry", source_type="internal")

        removed = repos["registry"].delete_internal_except(["agnes_sessions", "agnes_telemetry"])
        assert removed == 0
        assert repos["registry"].get("agnes_sessions") is not None
        assert repos["registry"].get("agnes_telemetry") is not None


# NOTE: mark_semantic_draft_pending / clear_semantic_draft_pending
# (semantic-phase5 wave 2's auto-draft sweep dedup flag) are PG-only (A3
# PG-first ratchet — table_registry.semantic_draft_pending_at is a
# Postgres-only column with no DuckDB sibling), so they are not part of
# this dual-backend contract. See tests/db_pg/test_table_registry_pg.py
# for their PG-only-shaped coverage.


def _seed_dependants(repos: dict, table_id: str) -> None:
    """Give ``table_id`` one ``data_package_tables`` row and one
    ``resource_grants`` row, written straight to the tables so the contract
    below is about ``unregister`` alone.

    Both parents are seeded too: DuckDB enforces
    ``data_package_tables.package_id -> data_packages(id)`` and
    ``resource_grants.group_id -> user_groups(id)``.
    """
    if repos["backend"] == "duckdb":
        conn = repos["conn"]
        conn.execute("INSERT INTO data_packages (id, slug, name) VALUES (?, ?, ?)", ["pkg-1", "pkg-1", "Pkg"])
        conn.execute("INSERT INTO data_package_tables (package_id, table_id) VALUES (?, ?)", ["pkg-1", table_id])
        conn.execute("INSERT INTO user_groups (id, name) VALUES (?, ?)", ["grp-1", "Analysts"])
        conn.execute(
            "INSERT INTO resource_grants (id, group_id, resource_type, resource_id, resource_id_table) "
            "VALUES (?, ?, 'table', ?, ?)",
            ["grant-1", "grp-1", table_id, table_id],
        )
        return

    import sqlalchemy as sa

    with repos["engine"].begin() as conn:
        conn.execute(
            sa.text("INSERT INTO data_packages (id, slug, name) VALUES ('pkg-1', 'pkg-1', 'Pkg')"),
        )
        conn.execute(
            sa.text("INSERT INTO data_package_tables (package_id, table_id) VALUES ('pkg-1', :t)"),
            {"t": table_id},
        )
        conn.execute(sa.text("INSERT INTO user_groups (id, name) VALUES ('grp-1', 'Analysts')"))
        conn.execute(
            sa.text(
                "INSERT INTO resource_grants (id, group_id, resource_type, resource_id, resource_id_table) "
                "VALUES ('grant-1', 'grp-1', 'table', :t, :t)"
            ),
            {"t": table_id},
        )


def _count(repos: dict, sql: str, table_id: str) -> int:
    if repos["backend"] == "duckdb":
        return int(repos["conn"].execute(sql.replace(":t", "?"), [table_id]).fetchone()[0])

    import sqlalchemy as sa

    with repos["engine"].connect() as conn:
        return int(conn.execute(sa.text(sql), {"t": table_id}).scalar_one())


class TestUnregisterCascade:
    """Pins ``unregister``'s dependant cleanup on both backends.

    The two engines disagreed in opposite directions before this: DuckDB
    declares ``data_package_tables.table_id REFERENCES table_registry(id)``
    with no ``ON DELETE``, so deleting a packaged table raised a constraint
    violation (a raw 500 out of ``DELETE /api/admin/registry/{id}``);
    Postgres declares no FK on that column and quietly kept the orphan
    junction row. ``resource_grants`` was the mirror image — a real
    ``ON DELETE CASCADE`` on PG, no enforcement at all on DuckDB.
    """

    def test_removes_the_registry_row(self, repos):
        _seed(repos, "orders", "Orders", source_type="keboola")
        _seed_dependants(repos, "orders")

        repos["registry"].unregister("orders")

        assert repos["registry"].get("orders") is None

    def test_removes_the_data_package_membership(self, repos):
        _seed(repos, "orders", "Orders", source_type="keboola")
        _seed_dependants(repos, "orders")

        repos["registry"].unregister("orders")

        assert _count(repos, "SELECT COUNT(*) FROM data_package_tables WHERE table_id = :t", "orders") == 0

    def test_removes_the_tables_grants(self, repos):
        _seed(repos, "orders", "Orders", source_type="keboola")
        _seed_dependants(repos, "orders")

        repos["registry"].unregister("orders")

        assert (
            _count(
                repos,
                "SELECT COUNT(*) FROM resource_grants WHERE resource_type = 'table' AND resource_id = :t",
                "orders",
            )
            == 0
        )

    def test_leaves_another_tables_dependants_alone(self, repos):
        _seed(repos, "orders", "Orders", source_type="keboola")
        _seed(repos, "customers", "Customers", source_type="keboola")
        _seed_dependants(repos, "customers")

        repos["registry"].unregister("orders")

        assert repos["registry"].get("customers") is not None
        assert _count(repos, "SELECT COUNT(*) FROM data_package_tables WHERE table_id = :t", "customers") == 1
        assert (
            _count(
                repos,
                "SELECT COUNT(*) FROM resource_grants WHERE resource_type = 'table' AND resource_id = :t",
                "customers",
            )
            == 1
        )

    def test_unregistering_an_unknown_id_is_a_no_op(self, repos):
        _seed(repos, "orders", "Orders", source_type="keboola")
        _seed_dependants(repos, "orders")

        repos["registry"].unregister("nope")

        assert repos["registry"].get("orders") is not None
        assert _count(repos, "SELECT COUNT(*) FROM data_package_tables WHERE table_id = :t", "orders") == 1
