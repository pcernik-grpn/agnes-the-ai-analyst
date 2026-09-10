"""Periodic CHECKPOINT + graceful close of operational.duckdb (#710).

``operational.duckdb`` is a second long-lived DuckDB singleton (CLI-auth /
Slack-binding codes) with no salvage-reopen recovery path. It gets the same
checkpoint/close lifecycle as ``system.duckdb`` so a non-graceful exit can't
leave a dirty WAL — and on a Postgres-state instance it is the ONLY written
DuckDB file, so the system-DB checkpoint loop never touches it.
"""

from __future__ import annotations

import logging

import pytest


@pytest.fixture
def operational_db(tmp_path, monkeypatch):
    """Fresh operational.duckdb under a tmp DATA_DIR; closed after the test."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from src.db import close_operational_db, get_operational_db

    conn = get_operational_db()
    yield conn, tmp_path
    close_operational_db()


def _wal_size(tmp_path) -> int:
    wal = tmp_path / "state" / "operational.duckdb.wal"
    return wal.stat().st_size if wal.exists() else 0


def _insert_code(conn, code_hash: str) -> None:
    conn.execute(
        "INSERT INTO cli_auth_codes (code_hash, user_id, email, expires_at) "
        "VALUES (?, 'u_test', 'test@example.com', now())",
        [code_hash],
    )


def test_checkpoint_flushes_wal(operational_db):
    conn, tmp_path = operational_db
    from src.db import checkpoint_operational_db

    # Produce WAL content: uncheckpointed writes on the singleton conn.
    _insert_code(conn, "wal-fill-1")
    assert _wal_size(tmp_path) > 0, "expected a non-empty WAL before checkpoint"

    assert checkpoint_operational_db() is True
    assert _wal_size(tmp_path) == 0, "CHECKPOINT should fold the WAL into operational.duckdb"


def test_checkpoint_noop_when_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.db import checkpoint_operational_db, close_operational_db

    close_operational_db()
    # No open singleton — must be a quiet no-op, never an implicit open.
    assert checkpoint_operational_db() is False
    assert not (tmp_path / "state" / "operational.duckdb").exists()


def test_close_flushes_and_resets(operational_db):
    conn, tmp_path = operational_db
    from src.db import close_operational_db

    _insert_code(conn, "wal-fill-2")
    assert _wal_size(tmp_path) > 0

    close_operational_db()
    assert _wal_size(tmp_path) == 0, "close should CHECKPOINT the WAL into the file"

    import src.db as db_mod

    assert db_mod._operational_db_conn is None
    assert db_mod._operational_db_path is None


def test_checkpoint_failure_is_nonfatal(operational_db, monkeypatch, caplog):
    class _RaisingConn:
        """The CHECKPOINT executes on a cursor, outside `_system_db_lock` (#2352)."""

        def cursor(self):
            return self

        def execute(self, *_a, **_k):
            raise RuntimeError("Cannot CHECKPOINT: there are other transactions active")

        def close(self):
            return None

    import src.db as db_mod

    monkeypatch.setattr(db_mod, "_operational_db_conn", _RaisingConn())
    with caplog.at_level(logging.WARNING):
        assert db_mod.checkpoint_operational_db() is False
    assert any("CHECKPOINT" in r.message for r in caplog.records)


def test_close_singleton_connections_releases_operational(operational_db):
    """The migrator-spawn lock-release must also drop the operational singleton.

    ``close_singleton_connections()`` runs right before a migrator subprocess is
    launched to release every exclusive DuckDB file lock. The operational
    singleton is a third long-lived DuckDB file, so it must be closed too —
    otherwise its lock is held across the spawn.
    """
    conn, _tmp_path = operational_db
    import src.db as db_mod
    from src.db import close_singleton_connections

    _insert_code(conn, "singleton-close")
    assert db_mod._operational_db_conn is not None

    close_singleton_connections()

    assert db_mod._operational_db_conn is None
