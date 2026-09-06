"""Regression: PG advisory-lock leases must not pin the database vacuum horizon.

All three session-scoped leases in ``src/db_pg.py`` (``seed_lease``,
``rebuild_lease``, ``knowledge_packaging_lease``) acquire their advisory
lock via ``engine.connect()`` and never commit before ``yield``-ing the
guarded operation to the caller. Under SQLAlchemy 2.x "commit as you go",
the first ``execute()`` on a fresh connection auto-begins a transaction,
so the connection sits ``idle in transaction`` for the ENTIRE guarded
operation — for ``knowledge_packaging_lease`` that can be hours. An
idle-in-transaction backend's snapshot pins
``pg_stat_activity.backend_xmin``, which stops autovacuum ANYWHERE in the
database from removing any row version created after the lease was taken —
no per-table tuning can undo this, because the constraint is the horizon,
not vacuum throughput.

Measured on a live instance: a ``knowledge-packaging`` job held its lease
for 24 211 s (6.7 h) idle in transaction (last statement
``SELECT pg_try_advisory_lock($1)``); over that window one heavily-updated
table's TOAST accumulated 8.9M dead tuples / 52 GB against 2 MB of live
heap data, and autovacuum running every ~5 min reclaimed nothing.

The fix commits the connection right after acquiring the (session-scoped,
not transaction-scoped) advisory lock: ``pg_advisory_lock``/
``pg_try_advisory_lock`` are tied to the *session* (the backend/connection),
not the transaction, so ending the transaction releases the snapshot while
the lock itself is held for as long as the connection stays open — verified
directly against a real Postgres 16 instance (via the ``pg_engine`` fixture):
a lock acquired then ``commit()``-ed remains listed in ``pg_locks``, a
concurrent ``pg_try_advisory_lock`` on the same key still returns ``False``,
and the connection's ``pg_stat_activity`` row flips from
``idle in transaction`` / non-null ``xact_start`` + ``backend_xmin`` to
``idle`` / both ``NULL``.
"""

from __future__ import annotations

import threading

import sqlalchemy as sa

import src.db_pg as db_pg
from src.db_pg import knowledge_packaging_lease, rebuild_lease, seed_lease

# pg_locks stores a session-level advisory lock taken via the single-bigint
# functions (pg_advisory_lock(key bigint)) as classid=high 32 bits,
# objid=low 32 bits, objsubid=1 — see the Postgres docs' note on
# reconstructing the original key from pg_locks for exactly this join.
_PROBE_SQL = sa.text(
    """
    SELECT a.pid, a.state, a.xact_start, a.backend_xmin
    FROM pg_locks l
    JOIN pg_stat_activity a ON a.pid = l.pid
    WHERE l.locktype = 'advisory'
      AND l.granted
      AND ((l.classid::bigint << 32) | l.objid::bigint) = :key
    """
)


def _assert_holder_pins_no_horizon(pg_engine: sa.Engine, key: int) -> None:
    with pg_engine.connect() as probe:
        rows = probe.execute(_PROBE_SQL, {"key": key}).fetchall()
    assert rows, "expected the lease holder to show up in pg_stat_activity via pg_locks"
    for pid, state, xact_start, backend_xmin in rows:
        assert state != "idle in transaction", (
            f"lease holder pid={pid} is idle in transaction (state={state!r}) — its "
            "connection was never committed, so a transaction stays open for the "
            "whole guarded operation"
        )
        assert xact_start is None, f"lease holder pid={pid} has an open transaction (xact_start={xact_start!r})"
        assert backend_xmin is None, (
            f"lease holder pid={pid} pins a vacuum horizon (backend_xmin={backend_xmin!r}) — "
            "no autovacuum in this database can reclaim rows created after the lease was taken"
        )


def _use_pg(pg_engine: sa.Engine, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", str(pg_engine.url))
    monkeypatch.setattr(db_pg, "_lease_use_pg", lambda: True)
    db_pg.dispose()


def test_knowledge_packaging_lease_holder_is_not_idle_in_transaction(pg_engine, monkeypatch):
    _use_pg(pg_engine, monkeypatch)

    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with knowledge_packaging_lease():
            holding.set()
            release.wait(timeout=5)

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert holding.wait(timeout=5)
        _assert_holder_pins_no_horizon(pg_engine, db_pg._KNOWLEDGE_PACKAGING_LEASE_ID)
    finally:
        release.set()
        t.join(timeout=5)
        db_pg.dispose()


def test_rebuild_lease_holder_is_not_idle_in_transaction(pg_engine, monkeypatch):
    _use_pg(pg_engine, monkeypatch)

    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with rebuild_lease():
            holding.set()
            release.wait(timeout=5)

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert holding.wait(timeout=5)
        _assert_holder_pins_no_horizon(pg_engine, db_pg._REBUILD_LEASE_ID)
    finally:
        release.set()
        t.join(timeout=5)
        db_pg.dispose()


def test_seed_lease_holder_is_not_idle_in_transaction(pg_engine, monkeypatch):
    _use_pg(pg_engine, monkeypatch)

    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with seed_lease():
            holding.set()
            release.wait(timeout=5)

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert holding.wait(timeout=5)
        _assert_holder_pins_no_horizon(pg_engine, db_pg._SEED_LEASE_ID)
    finally:
        release.set()
        t.join(timeout=5)
        db_pg.dispose()


def test_knowledge_packaging_lease_second_caller_still_denied_after_commit(pg_engine, monkeypatch):
    """Mutual exclusion must survive the fix — the guard must not go away
    just because the lease's connection now commits."""
    _use_pg(pg_engine, monkeypatch)

    holding = threading.Event()
    release = threading.Event()
    results: dict[str, bool] = {}

    def hold() -> None:
        with knowledge_packaging_lease() as acquired:
            results["first"] = acquired
            holding.set()
            release.wait(timeout=5)

    def contend() -> None:
        holding.wait(timeout=5)
        with knowledge_packaging_lease() as acquired:
            results["second"] = acquired
        release.set()

    t1, t2 = threading.Thread(target=hold), threading.Thread(target=contend)
    try:
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
    finally:
        release.set()
        db_pg.dispose()

    assert results["first"] is True
    assert results["second"] is False


def test_knowledge_packaging_lease_released_on_exception(pg_engine, monkeypatch):
    """The advisory lock must release even when the guarded block raises."""
    _use_pg(pg_engine, monkeypatch)

    try:
        with pg_engine.connect() as probe:
            held = probe.execute(
                sa.text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted"),
            ).scalar()
        assert held == 0

        with pg_engine.connect() as probe:
            before = probe.execute(
                sa.text(
                    "SELECT pg_try_advisory_lock(:key)",
                ),
                {"key": db_pg._KNOWLEDGE_PACKAGING_LEASE_ID},
            ).scalar()
            assert before is True
            probe.execute(sa.text("SELECT pg_advisory_unlock(:key)"), {"key": db_pg._KNOWLEDGE_PACKAGING_LEASE_ID})
            probe.commit()

        class _Boom(Exception):
            pass

        with pg_engine.connect() as probe:
            # sanity: lock is free before the raising attempt
            free_before = probe.execute(
                sa.text("SELECT pg_try_advisory_lock(:key)"),
                {"key": db_pg._KNOWLEDGE_PACKAGING_LEASE_ID},
            ).scalar()
            assert free_before is True
            probe.execute(sa.text("SELECT pg_advisory_unlock(:key)"), {"key": db_pg._KNOWLEDGE_PACKAGING_LEASE_ID})
            probe.commit()

        try:
            with knowledge_packaging_lease():
                raise _Boom("guarded operation failed")
        except _Boom:
            pass

        with pg_engine.connect() as probe:
            free_after = probe.execute(
                sa.text("SELECT pg_try_advisory_lock(:key)"),
                {"key": db_pg._KNOWLEDGE_PACKAGING_LEASE_ID},
            ).scalar()
            probe.execute(sa.text("SELECT pg_advisory_unlock(:key)"), {"key": db_pg._KNOWLEDGE_PACKAGING_LEASE_ID})
            probe.commit()
        assert free_after is True, "advisory lock was not released after the guarded block raised"
    finally:
        db_pg.dispose()


def test_rebuild_lease_released_on_exception(pg_engine, monkeypatch):
    _use_pg(pg_engine, monkeypatch)

    class _Boom(Exception):
        pass

    try:
        try:
            with rebuild_lease():
                raise _Boom("guarded operation failed")
        except _Boom:
            pass

        with pg_engine.connect() as probe:
            free_after = probe.execute(
                sa.text("SELECT pg_try_advisory_lock(:key)"),
                {"key": db_pg._REBUILD_LEASE_ID},
            ).scalar()
            probe.execute(sa.text("SELECT pg_advisory_unlock(:key)"), {"key": db_pg._REBUILD_LEASE_ID})
            probe.commit()
        assert free_after is True, "advisory lock was not released after the guarded block raised"
    finally:
        db_pg.dispose()


def test_knowledge_packaging_lease_does_not_pin_vacuum_horizon(pg_engine, monkeypatch):
    """End-to-end: a dead row created by a concurrent writer WHILE the lease
    is held must still be reclaimable by VACUUM once the lease's snapshot
    is not pinning the horizon."""
    _use_pg(pg_engine, monkeypatch)

    with pg_engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE IF EXISTS lease_vacuum_probe"))
        conn.execute(sa.text("CREATE TABLE lease_vacuum_probe (id int)"))
        conn.execute(sa.text("INSERT INTO lease_vacuum_probe SELECT generate_series(1, 50)"))

    try:
        n_dead = None
        with knowledge_packaging_lease() as acquired:
            assert acquired is True
            # A concurrent writer deletes rows *after* the lease was
            # acquired — ordinary traffic during a long-running guarded job.
            with pg_engine.begin() as conn:
                conn.execute(sa.text("DELETE FROM lease_vacuum_probe"))

            with pg_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(sa.text("VACUUM lease_vacuum_probe"))
                n_dead = conn.execute(
                    sa.text("SELECT n_dead_tup FROM pg_stat_user_tables WHERE relname = 'lease_vacuum_probe'"),
                ).scalar()

        assert n_dead == 0, (
            f"n_dead_tup={n_dead} after VACUUM — dead rows created while the lease was held "
            "were not reclaimable, meaning the lease pinned the vacuum horizon"
        )
    finally:
        with pg_engine.begin() as conn:
            conn.execute(sa.text("DROP TABLE IF EXISTS lease_vacuum_probe"))
        db_pg.dispose()
