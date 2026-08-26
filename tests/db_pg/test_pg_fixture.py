"""Failing tests for the pg_engine fixture itself.

Drives the fixture into existence (TDD step 1 in the parent plan). These
tests are the load-bearing check that *all* downstream PG tests have a
working Postgres to talk to.

Backend selection is via AGNES_TEST_PG_BACKEND env var:
  - "container" (default in CI) → testcontainers spins postgres:16-alpine
  - "embedded" (default locally if a system PG binary is present)
                → pytest-postgresql boots a local PG process

If neither backend is available, fixture tests skip cleanly with a clear
message — no silent green pass.
"""

import os

import pytest
import sqlalchemy as sa


def test_pg_engine_is_sqlalchemy_engine(pg_engine):
    """Fixture yields a SQLAlchemy Engine, not a connection or session."""
    assert isinstance(pg_engine, sa.Engine)


def test_pg_engine_select_one_works(pg_engine):
    """Connection actually reaches a live Postgres."""
    with pg_engine.connect() as conn:
        result = conn.execute(sa.text("SELECT 1")).scalar()
        assert result == 1


def test_pg_engine_reports_postgres_dialect(pg_engine):
    """We did not accidentally hand back a SQLite or DuckDB engine."""
    assert pg_engine.dialect.name == "postgresql"


def test_pg_engine_starts_with_empty_user_schema(pg_engine):
    """Fresh DB: no user tables before any migration runs.

    Catches a class of test pollution where a previous test left tables
    behind in a session-scoped DB. We rely on this invariant for
    round-trip / drift tests downstream.
    """
    inspector = sa.inspect(pg_engine)
    user_tables = [t for t in inspector.get_table_names(schema="public") if not t.startswith("pg_")]
    assert user_tables == [], f"expected empty public schema, found: {user_tables}"


def test_pg_session_factory_yields_session(pg_session):
    """`pg_session` fixture wraps the engine in a transaction-scoped session
    that auto-rollbacks at test end (per-test isolation)."""
    from sqlalchemy.orm import Session

    assert isinstance(pg_session, Session)
    result = pg_session.execute(sa.text("SELECT 2")).scalar()
    assert result == 2


def test_backend_env_var_is_respected():
    """The fixture honors AGNES_TEST_PG_BACKEND — if neither env value nor
    autodetection succeeds, downstream tests skip with a clear message
    rather than failing in an opaque way."""
    backend = os.environ.get("AGNES_TEST_PG_BACKEND")
    if backend is not None:
        assert backend in {"container", "embedded", "pgserver"}, (
            f"AGNES_TEST_PG_BACKEND must be one of container|embedded|pgserver (got {backend!r})"
        )


def test_default_backend_is_pgserver(monkeypatch):
    """When AGNES_TEST_PG_BACKEND is unset, pgserver is the default — not autodetect."""
    monkeypatch.delenv("AGNES_TEST_PG_BACKEND", raising=False)
    from tests.db_pg.conftest import _resolve_backend

    assert _resolve_backend() == "pgserver"


@pytest.mark.slow
def test_start_pgserver_stops_postmaster_on_cleanup(monkeypatch):
    """Regression for #1362: closing the fixture must actually stop the
    postmaster (``cleanup_mode='stop'`` → ``pg_ctl -w stop``), not leave it
    running until PostgreSQL PANICs ~15 s after the pidfile disappears —
    and must write the owner sentinel the reaper needs for hard-kill leaks.

    Boots a real pgserver, so marked ``slow``.
    """
    import tempfile
    import time
    from pathlib import Path

    from tests.db_pg.conftest import _resolve_backend, _start_dedicated_pgserver
    from tests.db_pg.pgserver_reaper import OWNER_SENTINEL, _pid_alive

    if _resolve_backend() != "pgserver":
        pytest.skip("only meaningful on the pgserver backend")

    captured = {}
    real_mkdtemp = tempfile.mkdtemp

    def spying_mkdtemp(*args, **kwargs):
        d = real_mkdtemp(*args, **kwargs)
        # pgserver may mkdtemp internally (e.g. a short socket dir); pin the
        # capture to the fixture's own data dir by its prefix.
        if os.path.basename(d).startswith("agnes-pgserver-"):
            captured["dir"] = d
        return d

    monkeypatch.setattr(tempfile, "mkdtemp", spying_mkdtemp)

    gen = _start_dedicated_pgserver()
    try:
        url = next(gen)
        assert url.startswith("postgresql+psycopg://")
        pgdata = Path(captured["dir"])
        assert (pgdata / OWNER_SENTINEL).read_text().strip() == str(os.getpid())
        postmaster_pid = int((pgdata / "postmaster.pid").read_text().splitlines()[0])
        assert _pid_alive(postmaster_pid)
    finally:
        with pytest.raises(StopIteration):
            next(gen)  # drives the fixture's finally: cleanup() + rmtree

    # `pg_ctl -w stop` waits, so the postmaster should already be gone; the
    # short grace poll only absorbs scheduler noise. The old behavior took
    # ~15 s (PANIC on the deleted pidfile), which this deadline rejects.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _pid_alive(postmaster_pid):
        time.sleep(0.1)
    assert not _pid_alive(postmaster_pid)
    assert not Path(captured["dir"]).exists()


def _shared_pgdata_for(testrun_uid: str):
    """Mirror of the data-dir path ``_start_pgserver`` derives, for assertions."""
    import hashlib
    import tempfile
    from pathlib import Path

    checkout_token = hashlib.sha256(str(Path(__file__).resolve().parents[2]).encode()).hexdigest()[:8]
    return Path(tempfile.gettempdir()) / f"agnes-pgserver-{checkout_token}-{testrun_uid}"


# Stands in for a second xdist worker; see the test below for why it cannot
# simply be a second in-process handle.
_SECOND_WORKER = """
import sys
sys.path.insert(0, {repo!r})
from tests.db_pg.conftest import _start_pgserver
gen = _start_pgserver({uid!r}, "gw1")
print(next(gen), flush=True)
try:
    next(gen)
except StopIteration:
    pass
"""


class TestWorkerDatabaseName:
    """``_start_pgserver`` interpolates this straight into ``CREATE DATABASE``.

    xdist owns the value, so this is not an untrusted-input path — but
    ``CREATE DATABASE`` accepts no bind parameters, and the repo's security
    playbook asks that an identifier reaching an f-string be one that has
    been checked rather than one that merely happens to be safe.
    """

    def test_master_keeps_the_servers_default_database(self):
        from tests.db_pg.conftest import _worker_database_name

        assert _worker_database_name("master") is None

    def test_each_xdist_worker_gets_a_distinct_database(self):
        from tests.db_pg.conftest import _worker_database_name

        assert _worker_database_name("gw0") == "agnes_gw0"
        assert _worker_database_name("gw0") != _worker_database_name("gw1")

    @pytest.mark.parametrize(
        "hostile",
        ['gw0"; DROP DATABASE postgres; --', "gw0 gw1", "", "gw-0", "GW0"],
        ids=["sql-injection", "space", "empty", "dash", "uppercase"],
    )
    def test_rejects_anything_that_is_not_a_plain_worker_id(self, hostile):
        from tests.db_pg.conftest import _worker_database_name

        with pytest.raises(ValueError):
            _worker_database_name(hostile)


@pytest.mark.slow
def test_shared_pgserver_serves_every_worker_from_one_postmaster(tmp_path):
    """The contract that makes the shared server safe, across real processes.

    ``_start_pgserver`` turns N workers into one postmaster, which is what
    took a ``-n auto`` run from 11 postmasters (91-100 postgres processes,
    load average 22 on an 11-core box) down to one. Nothing asserted the two
    properties that make the sharing safe rather than merely cheap:

      * a worker leaving must NOT stop the server its siblings still use;
      * the last worker out must stop it, so a run leaves nothing behind.

    The second worker has to be a real subprocess. pgserver refcounts holders
    BY PID in ``<pgdata>/.handle_pids.json``, and ``get_server`` additionally
    returns the same object from ``_instances`` for a repeated path in one
    interpreter — so two in-process handles would be a single holder and the
    first close would stop the server, modelling the fan-out backwards.

    Boots a real pgserver, so marked ``slow``.
    """
    import subprocess
    import sys
    import time
    from pathlib import Path

    import sqlalchemy as sa

    from tests.db_pg.conftest import _resolve_backend, _start_pgserver, _worker_database_name
    from tests.db_pg.pgserver_reaper import OWNER_SENTINEL, _pid_alive

    if _resolve_backend() != "pgserver":
        pytest.skip("only meaningful on the pgserver backend")

    repo_root = Path(__file__).resolve().parents[2]
    testrun_uid = f"pgshare{os.getpid()}"

    gw0 = _start_pgserver(testrun_uid, "gw0")
    try:
        url0 = next(gw0)
        pgdata = _shared_pgdata_for(testrun_uid)
        assert (pgdata / "PG_VERSION").exists(), "initdb ran in the shared dir"
        postmaster_pid = int((pgdata / "postmaster.pid").read_text().splitlines()[0])
        assert _pid_alive(postmaster_pid)
        assert sa.make_url(url0).database == _worker_database_name("gw0")

        second = subprocess.run(
            [sys.executable, "-c", _SECOND_WORKER.format(repo=str(repo_root), uid=testrun_uid)],
            capture_output=True,
            text=True,
            check=False,  # the assertion below reports stderr on failure
            timeout=180,
            cwd=str(repo_root),
        )
        assert second.returncode == 0, second.stderr
        url1 = second.stdout.strip().splitlines()[-1]

        # Same postmaster, different databases — where isolation now lives.
        assert sa.make_url(url1).database == _worker_database_name("gw1")
        assert int((pgdata / "postmaster.pid").read_text().splitlines()[0]) == postmaster_pid

        # gw1's PROCESS has exited by now. Its handle going away must not have
        # taken the server with it.
        assert _pid_alive(postmaster_pid), "a worker leaving stopped the shared postmaster"

        # The sentinel must name something that outlives a single worker,
        # otherwise a concurrent session's reaper reads the live server as an
        # orphan and stops it mid-run.
        assert _pid_alive(int((pgdata / OWNER_SENTINEL).read_text().strip()))
    finally:
        with pytest.raises(StopIteration):
            next(gw0)  # last handle out

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _pid_alive(postmaster_pid):
        time.sleep(0.1)
    assert not _pid_alive(postmaster_pid), "last worker out must stop the postmaster"
