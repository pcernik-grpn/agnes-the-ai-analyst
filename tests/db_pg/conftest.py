"""Postgres test fixtures.

Three backends, selected at fixture-resolution time via the
``AGNES_TEST_PG_BACKEND`` environment variable:

  - ``pgserver`` (default) — uses the ``pgserver`` package's bundled
    Postgres 16 binary. No system install, no Docker. Works on any dev
    box out of the box.
  - ``container`` — testcontainers boots ``postgres:16-alpine`` once per
    pytest session. Opt-in; requires a working Docker socket.
  - ``embedded`` — pytest-postgresql boots a system ``postgres`` binary
    (initdb on tmpfs). Opt-in; requires the binary on PATH.

Per-test isolation: the session-scoped engine boots PG once; each test
function gets a freshly DROP/CREATE'd ``public`` schema so a previous
test's tables can't leak. ~100x faster than recreating the container/
process per test, with equivalent observable behavior.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from typing import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

# Ensure every SQLAlchemy model is registered on ``src.db_pg.Base.metadata``
# before any test runs. Tests that call ``Base.metadata.create_all(pg_engine)``
# (e.g. ``test_db_state_migrator.py``) need the full model set; without this
# pre-import they fail on a pytest-xdist worker whose test slice doesn't
# transitively import ``src.models`` before the first such test runs
# (`relation "users" does not exist` from a half-populated metadata).
import src.models  # noqa: F401


_VALID_BACKENDS = {"container", "embedded", "pgserver"}


def _resolve_backend() -> str:
    """Return ``"pgserver"`` by default; honor ``AGNES_TEST_PG_BACKEND`` override.

    pgserver ships a Postgres 16 binary in its wheel — works on any dev box
    without Docker or system PG. container/embedded backends remain available
    as explicit opt-ins for fidelity testing or CI matrix runs.
    """
    explicit = os.environ.get("AGNES_TEST_PG_BACKEND")
    if explicit:
        if explicit not in _VALID_BACKENDS:
            raise ValueError(f"AGNES_TEST_PG_BACKEND={explicit!r} not in {_VALID_BACKENDS}")
        return explicit
    return "pgserver"


def _start_container() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer("postgres:16-alpine", driver="psycopg")
    try:
        container.start()
    except Exception as exc:
        pytest.skip(f"docker unavailable for testcontainers: {exc}")
        return
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


def _start_embedded() -> Iterator[str]:
    import tempfile
    from pytest_postgresql.executor import PostgreSQLExecutor

    postgres_bin = shutil.which("postgres")
    if not postgres_bin:
        pytest.skip("AGNES_TEST_PG_BACKEND=embedded but no `postgres` on PATH")
        return

    tmpdir = tempfile.mkdtemp(prefix="agnes-pg-")
    try:
        executor = PostgreSQLExecutor(
            executable=postgres_bin,
            host="127.0.0.1",
            port=None,
            user="postgres",
            password="",
            dbname="postgres",
            options="",
            startparams="",
            datadir=tmpdir,
            unixsocketdir="/tmp",
            logfile=os.path.join(tmpdir, "pg.log"),
            postgres_options="",
        )
        executor.start()
        try:
            url = f"postgresql+psycopg://postgres@{executor.host}:{executor.port}/postgres"
            yield url
        finally:
            executor.stop()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _start_dedicated_pgserver() -> Iterator[str]:
    """A private, throwaway Postgres for one fixture — the pre-sharing behavior.

    ``_start_pgserver`` below now shares ONE server across all xdist workers of
    a run, which is right for the ~2.7k contract tests but wrong for the few
    tests that need a server nobody else has touched: naming a database that
    provably does not exist yet, or counting connections against a dsn that a
    peer's warm DuckLake attach could otherwise be pooling. Those keep this
    dedicated path, and pay the initdb, deliberately.
    """
    import tempfile
    from pathlib import Path

    import pixeltable_pgserver as pgserver

    from tests.db_pg.pgserver_reaper import OWNER_SENTINEL

    tmpdir = tempfile.mkdtemp(prefix="agnes-pgserver-")
    server = None
    try:
        server = pgserver.get_server(tmpdir, cleanup_mode="stop")
        (Path(tmpdir) / OWNER_SENTINEL).write_text(str(os.getpid()), encoding="utf-8")
        yield server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    finally:
        if server is not None:
            try:
                server.cleanup()
            except Exception:
                pass
        shutil.rmtree(tmpdir, ignore_errors=True)


def _start_pgserver(testrun_uid: str, worker_id: str) -> Iterator[str]:
    """Boot (or attach to) ONE Postgres shared by every xdist worker in this run.

    ``_pg_url`` is session-scoped, but under pytest-xdist each worker process
    runs its OWN session — so the previous ``mkdtemp()`` per worker meant
    ``-n auto`` booted one full Postgres PER CORE. Measured on a 14-core
    laptop: 8+ concurrent postmasters, each with a 300-640 MB data dir, one
    initdb apiece, and a matching pile of orphans whenever a run was killed
    (10 dirs / 4.1 GB observed).

    pgserver already supports exactly what is wanted here: ``get_server()``
    takes an interprocess lock, calls an idempotent ``ensure_postgres_running``,
    and refcounts holders in ``.handle_pids.json``, stopping the postmaster
    when the LAST handle closes. It was never given the chance, because every
    worker passed a different path. Keying the data dir on xdist's
    ``testrun_uid`` (identical across all workers of one run, fresh for the
    next) turns N servers into one, and ``cleanup_mode='stop'`` still shuts it
    down once the final worker exits.

    Isolation is preserved by giving each worker its OWN DATABASE on that
    shared server, because the per-test ``_drop_user_schema`` drops ``public``
    and workers would otherwise drop it out from under each other. A separate
    database is a stronger boundary than the separate schema they had before.

    The checkout token keeps two git worktrees from ever selecting the same
    data dir, matching the DATA_DIR scheme in the root conftest.
    """
    import tempfile
    from pathlib import Path

    import pixeltable_pgserver as pgserver
    import sqlalchemy as _sa

    from tests.db_pg.pgserver_reaper import OWNER_SENTINEL

    checkout_token = hashlib.sha256(str(Path(__file__).resolve().parents[2]).encode()).hexdigest()[:8]
    pgdata = Path(tempfile.gettempdir()) / f"agnes-pgserver-{checkout_token}-{testrun_uid}"
    pgdata.mkdir(parents=True, exist_ok=True)

    server = None
    try:
        # Idempotent + interprocess-locked: the first worker to arrive runs
        # initdb and starts the postmaster, the rest attach to it.
        server = pgserver.get_server(pgdata, cleanup_mode="stop")
        # Owner sentinel (#1362) for the reaper. Under a shared server the
        # sentinel must name a process that outlives any single worker, so it
        # records the xdist CONTROLLER (our parent) when we are a worker.
        # Writing our own short-lived worker PID would make the dir look
        # orphaned the moment that worker exited.
        owner_pid = os.getppid() if worker_id != "master" else os.getpid()
        try:
            (pgdata / OWNER_SENTINEL).write_text(str(owner_pid), encoding="utf-8")
        except OSError:
            pass  # a peer worker wrote it microseconds ago; either value is fine

        raw_uri = server.get_uri()
        admin_url = raw_uri.replace("postgresql://", "postgresql+psycopg://", 1)

        # Per-worker database on the shared server. `master` is the no-xdist
        # case and keeps the default database.
        if worker_id == "master":
            yield admin_url
        else:
            dbname = f"agnes_{worker_id}"
            admin = _sa.create_engine(admin_url, future=True, isolation_level="AUTOCOMMIT")
            try:
                with admin.connect() as conn:
                    # CREATE DATABASE has no IF NOT EXISTS; a leftover from a
                    # reused uid would otherwise abort the worker.
                    exists = conn.execute(
                        _sa.text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": dbname}
                    ).scalar()
                    if not exists:
                        conn.execute(_sa.text(f'CREATE DATABASE "{dbname}"'))
            finally:
                admin.dispose()
            yield server.get_uri(database=dbname).replace("postgresql://", "postgresql+psycopg://", 1)
    finally:
        if server is not None:
            try:
                # Releases THIS process's handle. pgserver stops the postmaster
                # only when the last worker's handle goes away.
                server.cleanup()
            except Exception:
                pass
        # Deliberately NOT rmtree'd here: a peer worker may still be using the
        # shared dir. The last handle's cleanup stops the server, and the
        # sessionstart reaper in the root conftest removes the directory once
        # its owner is gone.


# The orphaned-pgserver sweep used to live here as a session autouse fixture,
# which meant it only ever ran for developers who invoked `tests/db_pg`. It now
# runs from the root conftest's `pytest_sessionstart` for EVERY invocation of
# the suite (see `_sweep_leaked_scratch` there) — the leak this guards against
# is created by this package but suffered by every later run on the machine.


@pytest.fixture(scope="session")
def pg_backend() -> str:
    """Expose the resolved backend name to tests that want to assert it."""
    return _resolve_backend()


@pytest.fixture(scope="session")
def _pg_url(pg_backend, testrun_uid, worker_id) -> Iterator[str]:
    """Boot a Postgres (once per RUN, not per worker) and yield its URL.

    ``testrun_uid`` and ``worker_id`` come from pytest-xdist and are defined
    even without ``-n`` (uid random per run, worker_id ``"master"``).
    """
    if pg_backend == "container":
        yield from _start_container()
    elif pg_backend == "embedded":
        yield from _start_embedded()
    elif pg_backend == "pgserver":
        yield from _start_pgserver(testrun_uid, worker_id)
    else:
        raise ValueError(f"unknown backend {pg_backend!r}")


@pytest.fixture(scope="session")
def _pg_engine_session(_pg_url) -> Iterator[Engine]:
    engine = sa.create_engine(_pg_url, future=True)
    try:
        yield engine
    finally:
        engine.dispose()


def _drop_user_schema(engine: Engine) -> None:
    """Reset ``public`` so the next test sees a clean DB."""
    with engine.connect() as conn:
        conn.execute(sa.text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(sa.text("CREATE SCHEMA public"))
        conn.execute(sa.text("GRANT ALL ON SCHEMA public TO public"))
        conn.commit()


@pytest.fixture
def pg_engine(_pg_engine_session) -> Iterator[Engine]:
    """Per-test engine; empty ``public`` schema on entry."""
    _drop_user_schema(_pg_engine_session)
    yield _pg_engine_session


# ---------------------------------------------------------------------------
# Module-scoped alembic fixture (Phase 7.13)
#
# Running alembic upgrade head once per module (rather than once per test)
# saves ~3-5 s per test.  Tests that need a clean slate TRUNCATE individual
# tables (handled by the autouse _truncate_pg_user_tables fixture below)
# rather than DROP/recreate the whole schema.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pg_engine_with_schema(_pg_engine_session) -> Engine:
    """Module-scoped: fresh schema + alembic head applied once per module.

    The ``public`` schema is dropped and recreated once at the start of each
    test module that requests this fixture — so two modules that both use it
    get independent schemas without re-running the PG process.  Individual
    tests rely on the autouse ``_truncate_pg_user_tables`` fixture to clear
    data rows between runs.
    """
    from pathlib import Path
    from alembic import command
    from alembic.config import Config

    REPO_ROOT = Path(__file__).resolve().parents[2]
    _drop_user_schema(_pg_engine_session)
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(_pg_engine_session.url)
    command.upgrade(cfg, "head")
    return _pg_engine_session


@pytest.fixture(autouse=True)
def _truncate_pg_user_tables(request) -> Iterator[None]:
    """Auto-applied per-test cleanup for tests that use
    ``pg_engine_with_schema``.

    Yields immediately (no setup cost) and on teardown TRUNCATEs all
    ``public`` tables except ``alembic_version``, leaving the schema intact
    so the module-scoped alembic fixture can be reused by the next test.

    Tests that do NOT use ``pg_engine_with_schema`` are unaffected — the
    early-return guard skips the teardown entirely.
    """
    yield
    if "pg_engine_with_schema" not in request.fixturenames:
        return
    engine: Engine = request.getfixturevalue("pg_engine_with_schema")
    with engine.begin() as conn:
        rows = conn.execute(
            sa.text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename != 'alembic_version'")
        ).fetchall()
        for (table,) in rows:
            conn.execute(sa.text(f'TRUNCATE TABLE "{table}" CASCADE'))


@pytest.fixture
def pg_session(pg_engine) -> Iterator[Session]:
    """Per-test SQLAlchemy session over the per-test engine."""
    with Session(pg_engine, future=True) as session:
        yield session


# ---------------------------------------------------------------------------
# parametrized backend harness — runs the same endpoint test twice, once
# against DuckDB and once against Postgres.
# ---------------------------------------------------------------------------


@pytest.fixture(params=["duckdb", "pg"], ids=["duck", "pg"])
def state_backend(request, monkeypatch, tmp_path, _pg_url, pg_engine):
    """Configure the app-state backend.

    Tests that consume ``seeded_app_both`` indirectly consume this and
    therefore run twice: once with ``AGNES_DB_URL`` unset (DuckDB path)
    and once with it set to the per-test pgserver instance + alembic
    upgraded to head.

    Tests that should ONLY run against one backend skip the other inside the
    body (do NOT re-``@parametrize`` ``state_backend`` — re-parametrizing a name
    already supplied by this parametrized fixture is a duplicate-parametrization
    collection error under newer pytest)::

        def test_pg_only_thing(state_backend, seeded_app_both):
            if state_backend != "pg":
                pytest.skip("PG-only")
            ...
    """
    if request.param == "pg":
        # pg_engine already created the engine and bumped schema cleanly.
        # Run alembic upgrade head so the chain is materialised.
        from pathlib import Path
        from alembic import command
        from alembic.config import Config

        REPO_ROOT = Path(__file__).resolve().parents[2]
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
        command.upgrade(cfg, "head")

        # Seed Admin + Everyone groups (DuckDB does this in _seed_system_groups
        # on every connect; PG needs an explicit seed). Idempotent.
        with pg_engine.begin() as conn_:
            import uuid as _uuid

            for name, description in (
                ("Admin", "System: full access to all data and admin actions"),
                ("Everyone", "System: default group every user is implicitly a member of"),
            ):
                conn_.execute(
                    sa.text(
                        "INSERT INTO user_groups (id, name, description, is_system, created_by) "
                        "VALUES (:id, :name, :desc, TRUE, 'system:seed') "
                        "ON CONFLICT (name) DO UPDATE SET is_system = TRUE"
                    ),
                    {"id": _uuid.uuid4().hex, "name": name, "desc": description},
                )

        monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))

        # Force a fresh PG engine inside the app process
        import src.db_pg as db_pg

        db_pg.dispose()
    else:
        monkeypatch.delenv("AGNES_DB_URL", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)

    # Reset the factory module to pick up the env change on next import
    import importlib
    import src.repositories

    importlib.reload(src.repositories)

    yield request.param


@pytest.fixture
def seeded_app_both(state_backend, tmp_path, monkeypatch):
    """Backend-parametrized TestClient with seeded admin + analyst users.

    Drop-in for tests that want to verify endpoint behaviour identically
    against DuckDB and Postgres. Returns the same dict shape as the
    legacy ``seeded_app`` fixture (client + token strings + env), with
    one extra key ``backend`` for diagnostic assertions.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
    for sub in ("extracts", "analytics", "state", "notifications"):
        (tmp_path / sub).mkdir(exist_ok=True)

    from app.auth.jwt import create_access_token
    from app.main import create_app
    from fastapi.testclient import TestClient
    from src.repositories import users_repo, user_group_members_repo

    if state_backend == "duckdb":
        # DuckDB side: ensure system DB is created + system groups seeded
        from src.db import close_system_db, get_system_db

        close_system_db()
        get_system_db()  # triggers _ensure_schema + _seed_system_groups

    u = users_repo()
    u.create(id="admin1", email="admin@test.com", name="Admin")
    u.create(id="analyst1", email="analyst@test.com", name="Analyst")

    # Find Admin group id (seeded by either DuckDB _ensure_schema or the
    # PG fixture above)
    if state_backend == "duckdb":
        from src.db import get_system_db

        admin_gid = get_system_db().execute("SELECT id FROM user_groups WHERE name = 'Admin'").fetchone()[0]
    else:
        import sqlalchemy as sa
        from src.db_pg import get_engine

        with get_engine().connect() as conn_:
            admin_gid = conn_.execute(sa.text("SELECT id FROM user_groups WHERE name = 'Admin'")).scalar()

    user_group_members_repo().add_member("admin1", admin_gid, source="system_seed")

    app = create_app()
    client = TestClient(app)

    return {
        "client": client,
        "admin_token": create_access_token("admin1", "admin@test.com"),
        "analyst_token": create_access_token("analyst1", "analyst@test.com"),
        "backend": state_backend,
        "data_dir": tmp_path,
    }


# ---------------------------------------------------------------------------
# CLI fixture — CliRunner wired through the same in-process FastAPI app
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_client_both(seeded_app_both, monkeypatch):
    """CliRunner whose HTTP calls go to the in-process TestClient app.

    Patches cli.client.get_client and cli.client._get_shared_client so
    every CLI command hits the same FastAPI app as the web tests —
    no real ports, full API surface, both backends.
    """
    import contextlib
    from typer.testing import CliRunner

    tc = seeded_app_both["client"]
    admin_token = seeded_app_both["admin_token"]

    def _make_client(timeout=30.0):
        from starlette.testclient import TestClient as _TC

        return _TC(
            app=tc.app,
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    @contextlib.contextmanager
    def _patched_get_client(timeout=30.0):
        client = _make_client(timeout)
        try:
            yield client
        finally:
            client.close()

    import cli.client as _cli_client

    monkeypatch.setattr(_cli_client, "get_client", _patched_get_client)
    monkeypatch.setattr(_cli_client, "_get_shared_client", lambda: _make_client())
    _cli_client._SHARED_CLIENT = None
    monkeypatch.setenv("AGNES_SERVER_URL", "http://testserver")

    # v2_client uses httpx.get/post/etc. directly — patch each helper to
    # route through the in-process TestClient so CLI commands that call
    # v2_client (catalog, schema, my-stack, …) don't try a real TCP connection.
    import cli.v2_client as _v2_client
    from cli.v2_client import V2ClientError, _parse_error_body as _v2_parse_error

    def _v2_get(path, **params):
        c = _make_client()
        r = c.get(path, params=params or None)
        if r.status_code >= 400:
            raise V2ClientError(status_code=r.status_code, body=_v2_parse_error(r))
        return r.json()

    def _v2_post(path, payload=None):
        c = _make_client()
        r = c.post(path, json=payload)
        if r.status_code >= 400:
            raise V2ClientError(status_code=r.status_code, body=_v2_parse_error(r))
        return r.json()

    def _v2_delete(path):
        c = _make_client()
        r = c.delete(path)
        if r.status_code >= 400:
            raise V2ClientError(status_code=r.status_code, body=_v2_parse_error(r))
        return r.json() if r.content else {}

    def _v2_put(path, payload=None):
        c = _make_client()
        r = c.put(path, json=payload)
        if r.status_code >= 400:
            raise V2ClientError(status_code=r.status_code, body=_v2_parse_error(r))
        return r.json()

    monkeypatch.setattr(_v2_client, "api_get_json", _v2_get)
    monkeypatch.setattr(_v2_client, "api_post_json", _v2_post)
    monkeypatch.setattr(_v2_client, "api_delete", _v2_delete)
    monkeypatch.setattr(_v2_client, "api_put_json", _v2_put)

    # cli.commands.* modules do `from cli.v2_client import api_get_json` which
    # creates a local binding that is NOT updated by setattr on _v2_client above.
    # Under xdist, command modules are imported early in the process (before this
    # fixture runs), so we must also patch their local references directly.
    #
    # IDENTITY-checked, not name-checked: `api_delete` exists in BOTH cli.client
    # (returns an httpx.Response) and cli.v2_client (returns parsed JSON, raises
    # V2ClientError). Fourteen command modules import the former; a blanket
    # patch-by-name handed them the latter, so any CLI DELETE driven through this
    # fixture died on `.status_code`. cli.client's own helpers already route
    # through the patched `get_client` above, so leaving them alone is correct.
    import sys as _sys

    _cmd_patches = {
        "api_get_json": _v2_get,
        "api_post_json": _v2_post,
        "api_delete": _v2_delete,
        "api_put_json": _v2_put,
    }
    for _mod_name, _mod in list(_sys.modules.items()):
        if _mod_name.startswith("cli.commands.") and _mod is not None:
            for _attr, _replacement in _cmd_patches.items():
                if getattr(_mod, _attr, None) is getattr(_v2_client, _attr, None):
                    monkeypatch.setattr(_mod, _attr, _replacement)

    runner = CliRunner()

    def invoke(args):
        from cli.main import app as cli_app

        return runner.invoke(cli_app, args, catch_exceptions=False)

    yield {
        "runner": runner,
        "invoke": invoke,
        "backend": seeded_app_both["backend"],
        "admin_token": admin_token,
        "analyst_token": seeded_app_both["analyst_token"],
        "client": tc,
        "data_dir": seeded_app_both["data_dir"],
    }


# ---------------------------------------------------------------------------
# registered_table_both — a queryable table registered in the active backend
# ---------------------------------------------------------------------------


@pytest.fixture
def registered_table_both(seeded_app_both):
    """Register a table via the API, write parquet + sync_state, yield table info.

    Returns {"table_id": str, "source_name": str, "data_dir": Path}.

    - ``table_id`` is the UUID from table_registry (used by download handler rglob
      and RBAC grants).
    - ``source_name`` is the human name ("smoke_orders") which is the key used in
      sync_state and the manifest ``tables`` dict.
    """
    import pandas as pd
    from src.repositories import sync_state_repo

    client = seeded_app_both["client"]
    admin_token = seeded_app_both["admin_token"]
    data_dir = seeded_app_both["data_dir"]
    headers = {"Authorization": f"Bearer {admin_token}"}

    source_name = "smoke_orders"
    bucket = "smoke_src"

    # Register first to get the table_id (UUID) the download handler looks up
    r = client.post(
        "/api/admin/register-table",
        json={
            "name": source_name,
            "source_type": "keboola",
            "bucket": bucket,
            "source_table": source_name,
            "query_mode": "local",
        },
        headers=headers,
    )
    assert r.status_code == 201, f"register-table failed: {r.text}"
    table_id = r.json()["id"]

    # Write parquet at extracts/{bucket}/data/{table_id}.parquet so the download
    # handler (which rglob-searches "data/{table_id}.parquet") can stream it.
    parquet_dir = data_dir / "extracts" / bucket / "data"
    parquet_dir.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame({"id": [1, 2, 3], "amount": [10.0, 20.0, 30.0]})
    parquet_path = parquet_dir / f"{table_id}.parquet"
    df.to_parquet(str(parquet_path))

    # Populate sync_state directly so the manifest returns this table.
    # sync_state.table_id mirrors table_registry.name ("smoke_orders"), which is
    # the key the manifest uses in its ``tables`` dict.
    sync_state_repo().update_sync(
        table_id=source_name,
        rows=3,
        file_size_bytes=parquet_path.stat().st_size,
        hash="",
    )

    yield {"table_id": table_id, "source_name": source_name, "data_dir": data_dir}
