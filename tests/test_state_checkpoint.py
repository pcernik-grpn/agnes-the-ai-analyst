"""Periodic CHECKPOINT of system.duckdb (#710).

The app holds a long-lived singleton connection to the state DB, which
makes DuckDB defer its automatic checkpoint indefinitely — the WAL grows
unbounded between graceful restarts. `checkpoint_system_db()` is the
periodic remedy: a best-effort explicit CHECKPOINT the lifespan task
fires every few minutes.
"""

from __future__ import annotations

import logging

import pytest


@pytest.fixture
def system_db(tmp_path, monkeypatch):
    """Fresh system.duckdb under a tmp DATA_DIR; closed after the test."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from src.db import close_system_db, get_system_db

    conn = get_system_db()
    yield conn, tmp_path
    close_system_db()


def _wal_size(tmp_path) -> int:
    wal = tmp_path / "state" / "system.duckdb.wal"
    return wal.stat().st_size if wal.exists() else 0


def test_checkpoint_flushes_wal(system_db):
    conn, tmp_path = system_db
    from src.db import checkpoint_system_db

    # Produce WAL content: uncheckpointed writes on the singleton conn.
    conn.execute(
        "INSERT INTO audit_log (id, timestamp, user_id, action) VALUES ('wal-fill-1', now(), 'test', 'wal-fill')"
    )
    assert _wal_size(tmp_path) > 0, "expected a non-empty WAL before checkpoint"

    assert checkpoint_system_db() is True
    assert _wal_size(tmp_path) == 0, "CHECKPOINT should fold the WAL into system.duckdb"


def test_checkpoint_noop_when_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.db import checkpoint_system_db, close_system_db

    close_system_db()
    # No open singleton — must be a quiet no-op, never an implicit open.
    assert checkpoint_system_db() is False
    assert not (tmp_path / "state" / "system.duckdb").exists()


def test_checkpoint_failure_is_nonfatal(system_db, monkeypatch, caplog):
    conn, _ = system_db

    class _RaisingConn:
        """The CHECKPOINT executes on a cursor, outside `_system_db_lock` (#2352)."""

        def cursor(self):
            return self

        def execute(self, *_a, **_k):
            raise RuntimeError("Cannot CHECKPOINT: there are other transactions active")

        def close(self):
            return None

    import src.db as db_mod

    monkeypatch.setattr(db_mod, "_system_db_conn", _RaisingConn())
    with caplog.at_level(logging.WARNING):
        assert db_mod.checkpoint_system_db() is False
    assert any("CHECKPOINT" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 300.0),  # default
        ("120", 120.0),
        ("0", 0.0),  # disabled
        ("-5", 0.0),  # negative → disabled
        ("garbage", 300.0),  # unparsable → default
        ("nan", 300.0),  # non-finite would silently disable → default
        ("inf", 300.0),  # would sleep forever → default
    ],
)
def test_checkpoint_interval_parsing(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("AGNES_STATE_CHECKPOINT_INTERVAL_S", raising=False)
    else:
        monkeypatch.setenv("AGNES_STATE_CHECKPOINT_INTERVAL_S", raw)
    from app.main import _state_checkpoint_interval_s

    assert _state_checkpoint_interval_s() == expected
