"""``src.ducklake_session.validate_ducklake_migration_prerequisites`` —
Postgres-catalog reachability + the "missing catalog database" auto-repair
path, against a REAL pgserver instance (wave-2G Task 6).

The pure-logic branches (extension unavailable, multi-process without a PG
DSN, and the mocked missing-database/create-fails/create-succeeds shapes)
are covered in ``tests/test_ducklake_session.py`` without needing a real
Postgres. This file proves the one thing that can't be faked: a real
``CREATE DATABASE`` issued against a live server, followed by a real
DuckLake ATTACH that only succeeds once that database exists — the exact
scenario wave-2G Task 5 flagged as unhandled (an operator adding
``analytics.backend: ducklake`` to an EXISTING Postgres volume, where
``deploy/postgres/init-ducklake-db.sql`` never runs because it only fires
on a brand-new empty volume).

Same loud-skip contract as the other DuckLake test files.
"""

from __future__ import annotations

import pytest


def _extensions_available() -> bool:
    import duckdb

    try:
        probe = duckdb.connect(":memory:")
        try:
            probe.execute("INSTALL ducklake")
            probe.execute("LOAD ducklake")
            probe.execute("INSTALL postgres")
            probe.execute("LOAD postgres")
        finally:
            probe.close()
        return True
    except Exception:
        return False


_EXTENSIONS_AVAILABLE = _extensions_available()


pytestmark = pytest.mark.skipif(
    not _EXTENSIONS_AVAILABLE,
    reason=(
        "DuckDB 'ducklake'/'postgres' extensions could not be INSTALL/LOAD'ed "
        "in this environment (offline, or DuckDB build predates them). "
        "Skipping real DuckLake-over-Postgres prerequisite tests rather than "
        "faking success — see src/ducklake_session.py::ducklake_available()."
    ),
)


@pytest.fixture
def _fresh_pgserver_url():
    """A dedicated pgserver instance (not the shared session-scoped
    ``pg_engine``) — this file needs to name a database that does NOT
    exist yet on the server, which a shared/reused server can't
    guarantee across test order."""
    from tests.db_pg.conftest import _start_dedicated_pgserver

    yield from _start_dedicated_pgserver()


@pytest.fixture
def ducklake_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    import src.analytics_backend as ab
    import src.ducklake_session as ds

    ab.reset_analytics_backend_cache()
    ds.close_ducklake_sessions()
    yield
    ds.close_ducklake_sessions()
    ab.reset_analytics_backend_cache()


def _dsn_with_dbname(url: str, dbname: str) -> str:
    from src.ducklake_session import _dsn_with_database

    return _dsn_with_database(url, dbname)


def test_prerequisites_ok_when_target_database_already_exists(_fresh_pgserver_url, ducklake_env, monkeypatch):
    """pgserver's default connection targets an already-existing
    ``postgres`` database — no repair needed, no problems reported."""
    import app.startup_guards as guards
    from src.ducklake_session import validate_ducklake_migration_prerequisites

    monkeypatch.setenv("AGNES_DUCKLAKE_CATALOG_DSN", _fresh_pgserver_url)
    monkeypatch.setattr(guards, "is_multi_process", lambda: True)  # PG DSN present -> no complaint either way

    assert validate_ducklake_migration_prerequisites() == []


def test_prerequisites_auto_creates_missing_database_then_succeeds(_fresh_pgserver_url, ducklake_env, monkeypatch):
    """Point the catalog DSN at a database name that does not exist on
    this fresh pgserver instance — the validator must issue a real
    ``CREATE DATABASE`` against the server's administrative ``postgres``
    database, then a real ATTACH against the now-existing target
    database, and report zero problems."""
    import sqlalchemy as sa

    from src.ducklake_session import validate_ducklake_migration_prerequisites

    missing_db_dsn = _dsn_with_dbname(_fresh_pgserver_url, "agnes_ducklake_missing_test")
    monkeypatch.setenv("AGNES_DUCKLAKE_CATALOG_DSN", missing_db_dsn)

    # Sanity: the target database genuinely does not exist yet.
    admin_engine = sa.create_engine(_dsn_with_dbname(_fresh_pgserver_url, "postgres"), future=True)
    try:
        with admin_engine.connect() as conn:
            exists_before = conn.execute(
                sa.text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": "agnes_ducklake_missing_test"},
            ).fetchone()
        assert exists_before is None

        problems = validate_ducklake_migration_prerequisites()
        assert problems == []

        with admin_engine.connect() as conn:
            exists_after = conn.execute(
                sa.text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": "agnes_ducklake_missing_test"},
            ).fetchone()
        assert exists_after is not None
    finally:
        admin_engine.dispose()
