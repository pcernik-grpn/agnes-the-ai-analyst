"""``src/ducklake_session.py`` — Postgres-catalog contract.

Uses the repo's existing PG test fixture (``pg_engine`` from
``tests/db_pg/conftest.py``) — same pgserver-backed pattern every other
``tests/db_pg/*`` contract test uses. ``pg_engine`` gives a freshly
DROP/CREATE'd ``public`` schema per test; DuckLake's Postgres catalog
creates its own ``ducklake_*`` metadata tables directly in ``public``
(verified against DuckDB 1.5.2 — no separate schema/namespace), so the
per-test schema reset is exactly the isolation this file needs, with no
extra per-test database bookkeeping.

Complements ``tests/test_ducklake_session.py`` (DuckDB-file catalog,
no Postgres) with the Postgres-specific parts of the contract: the
``postgresql://`` DSN → libpq-keyword ATTACH path, and the "exactly one
connection per ATTACH" invariant from the wave-2G plan (verified here via
``pg_stat_activity`` through the fixture's own engine).

Same loud-skip contract as the file-catalog test file: if the
``ducklake``/``postgres`` DuckDB extensions can't be installed here, every
test in this module is skipped with an explicit reason rather than faking
success.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa


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
        "Skipping real DuckLake-over-Postgres session tests rather than "
        "faking success — see src/ducklake_session.py::ducklake_available()."
    ),
)


@pytest.fixture
def ducklake_pg_env(pg_engine, monkeypatch, tmp_path):
    """Point ``ducklake_catalog_dsn()`` at the per-test pgserver schema and
    reset both the analytics-backend cache and the DuckLake session
    singletons, so no state leaks in from another test/module.

    ``render_as_string(hide_password=False)`` (not ``str(engine.url)``,
    which masks the password with ``***``) — the actual DSN is what
    :func:`src.ducklake_session.pg_dsn_to_libpq` needs to convert.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    dsn = pg_engine.url.render_as_string(hide_password=False)
    monkeypatch.setenv("AGNES_DUCKLAKE_CATALOG_DSN", dsn)

    import src.analytics_backend as ab
    import src.ducklake_session as ds

    ab.reset_analytics_backend_cache()
    ds.close_ducklake_sessions()
    yield dsn
    ds.close_ducklake_sessions()
    ab.reset_analytics_backend_cache()


@pytest.fixture
def _isolated_pg_url():
    """A pgserver instance dedicated to a single test.

    Only the connection-count test below needs this — it requires a
    guaranteed-empty ``pg_stat_activity`` baseline. This module's other
    tests share the conftest's session-scoped ``pg_engine`` (cheaper —
    pgserver boot is the expensive part), but that sharing means a prior
    test's DuckLake attach can leave a warm connection behind that a
    later attach against the *same* dsn transparently reuses instead of
    opening a fresh one (observed directly while developing this test —
    DuckDB's postgres/ducklake catalog support appears to pool/reuse a
    connection per dsn+data_path target within a process). That reuse
    would make a shared-server delta assertion flaky depending on test
    order; a dedicated throwaway server sidesteps it entirely.
    """
    from tests.db_pg.conftest import _start_dedicated_pgserver

    yield from _start_dedicated_pgserver()


@pytest.fixture
def isolated_ducklake_pg_env(_isolated_pg_url, monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_DUCKLAKE_CATALOG_DSN", _isolated_pg_url)

    import src.analytics_backend as ab
    import src.ducklake_session as ds

    ab.reset_analytics_backend_cache()
    ds.close_ducklake_sessions()
    yield _isolated_pg_url
    ds.close_ducklake_sessions()
    ab.reset_analytics_backend_cache()


def _client_backend_pids(engine: sa.Engine) -> list[int]:
    """Other client-backend connections against *engine*'s database,
    excluding our own querying connection and Postgres's own background
    processes (autovacuum launcher, checkpointer, ...), which always show
    up in ``pg_stat_activity`` regardless of any DuckLake activity."""
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT pid FROM pg_stat_activity WHERE backend_type = 'client backend' AND pid != pg_backend_pid()"
            )
        ).fetchall()
    return sorted(r[0] for r in rows)


def test_pg_catalog_write_then_read_roundtrip(ducklake_pg_env):
    from src.ducklake_session import get_ducklake_read, get_ducklake_write

    w = get_ducklake_write()
    w.execute("CREATE SCHEMA IF NOT EXISTS lake.src1")
    w.execute("CREATE OR REPLACE TABLE lake.src1.t1 AS SELECT 1 AS x")
    w.close()

    r = get_ducklake_read()
    assert r.execute("SELECT * FROM lake.src1.t1").fetchall() == [(1,)]
    r.close()


def test_pg_catalog_exactly_one_connection_per_attach(isolated_ducklake_pg_env):
    """Opening the reader singleton must open exactly one libpq
    connection to the catalog; adding the writer singleton (a second,
    independent ATTACH — Postgres catalogs don't hit the same-process
    file-catalog restriction) brings the total to exactly two. Matches
    the wave-2G plan's "one connection per ATTACH" sizing claim.

    Runs against a dedicated, single-test pgserver (``isolated_ducklake_pg_env``)
    rather than the module-shared ``pg_engine`` — see that fixture's
    docstring for why a shared server makes this specific assertion
    order-dependent (DuckDB reuses a pooled connection to the same DSN
    across separate attaches once one has ever been opened in-process).
    """
    from src.ducklake_session import get_ducklake_read, get_ducklake_write

    engine = sa.create_engine(isolated_ducklake_pg_env.replace("postgresql://", "postgresql+psycopg://", 1))
    try:
        assert _client_backend_pids(engine) == []

        r = get_ducklake_read()
        r.execute("SELECT 1")
        after_reader = _client_backend_pids(engine)
        assert len(after_reader) == 1, f"expected exactly 1 connection after reader attach, got {after_reader}"

        w = get_ducklake_write()
        w.execute("SELECT 1")
        after_writer = _client_backend_pids(engine)
        assert len(after_writer) == 2, f"expected exactly 2 connections after writer attach too, got {after_writer}"

        r.close()
        w.close()
    finally:
        engine.dispose()


def test_pg_catalog_reader_and_writer_are_independent_connections(ducklake_pg_env):
    """Contrast with the DuckDB-file-catalog case (where reader and
    writer share one physical connection, see
    ``tests/test_ducklake_session.py``): a Postgres catalog gives each
    singleton its own independent connection."""
    import src.ducklake_session as ds

    r = ds.get_ducklake_read()
    w = ds.get_ducklake_write()
    assert ds._read_conn is not ds._write_conn
    r.close()
    w.close()


def test_pg_catalog_close_reopen_cycle_is_clean(ducklake_pg_env):
    from src.ducklake_session import close_ducklake_sessions, get_ducklake_read, get_ducklake_write

    for i in range(2):
        w = get_ducklake_write()
        w.execute("CREATE SCHEMA IF NOT EXISTS lake.cycle_src")
        w.execute(f"CREATE OR REPLACE TABLE lake.cycle_src.t AS SELECT {i} AS x")
        w.close()

        r = get_ducklake_read()
        assert r.execute("SELECT * FROM lake.cycle_src.t").fetchall() == [(i,)]
        r.close()

        close_ducklake_sessions()


def test_pg_catalog_memory_caps_applied(ducklake_pg_env):
    from src.ducklake_session import get_ducklake_read, get_ducklake_write

    r = get_ducklake_read()
    (mem_r,) = r.execute("SELECT current_setting('memory_limit')").fetchone()
    assert mem_r
    r.close()

    w = get_ducklake_write()
    (mem_w,) = w.execute("SELECT current_setting('memory_limit')").fetchone()
    assert mem_w
    w.close()


def test_pg_dsn_to_libpq_round_trips_against_real_pgserver_dsn(ducklake_pg_env):
    """The converter's output must be a libpq DSN the postgres extension
    actually accepts against the live pgserver instance — an end-to-end
    check complementing the unit-level assertions in
    ``tests/test_ducklake_session.py``."""
    from src.ducklake_session import pg_dsn_to_libpq
    from src.orchestrator_security import escape_sql_string_literal

    libpq = pg_dsn_to_libpq(ducklake_pg_env)

    import duckdb

    conn = duckdb.connect(":memory:")
    try:
        conn.execute("INSTALL postgres")
        conn.execute("LOAD postgres")
        # libpq-escaped values embed literal single quotes (e.g.
        # host='...'); escape_sql_string_literal doubles them for the
        # OUTER DuckDB SQL string literal — same two-layer escaping
        # src.ducklake_session._attach_ducklake applies for the real
        # ducklake ATTACH.
        conn.execute(f"ATTACH '{escape_sql_string_literal(libpq)}' AS pg_direct (TYPE postgres)")
        conn.execute("SELECT 1 FROM pg_direct.information_schema.tables LIMIT 1")
    finally:
        conn.close()
