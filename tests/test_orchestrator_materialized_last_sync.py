"""Rebuild bookkeeping must not mask materialized-row failures.

`SyncOrchestrator._update_sync_state` runs after every rebuild (~each
scheduler tick) and upserts sync_state for every table in the source's
`_meta` — bumping `last_sync = now()`. For `query_mode='materialized'`
rows that timestamp is the *schedule gate*: `_run_materialized_pass`
compares it against `sync_schedule` (e.g. ``daily 06:00``) to decide
whether to run.

When the rebuild bump owns that column, a failed or killed daily
materialization is never retried the same day: the next rebuild
refreshes `last_sync` minutes later, the due-check sees a "fresh" sync,
and the row is skipped until the next day (observed in production as a
week-long gap for a `daily 06:00` table whose 06:00 runs kept getting
killed).

Ownership contract: for materialized rows, sync_state is written by the
materialized pass alone — `update_sync` on success (which bumps
last_sync), `set_error` on failure (which deliberately does not).
"""

from datetime import datetime
from unittest.mock import patch

import duckdb
import pytest

from src.db import _ensure_schema
from src.orchestrator import SyncOrchestrator
from src.repositories.sync_state import SyncStateRepository
from src.repositories.table_registry import TableRegistryRepository


@pytest.fixture
def system_db_path(tmp_path):
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    try:
        _ensure_schema(conn)
        registry = TableRegistryRepository(conn)
        registry.register(
            id="orders_daily_econ",
            name="orders_daily_econ",
            source_type="bigquery",
            bucket="",
            source_table="orders_daily_econ",
            query_mode="materialized",
            description="",
            sync_schedule="daily 06:00",
        )
        registry.register(
            id="orders",
            name="orders",
            source_type="keboola",
            bucket="in.c-crm",
            source_table="orders",
            query_mode="local",
            description="",
        )
        # Yesterday's successful materialize — the timestamp the schedule
        # gate must keep seeing after a failed run today.
        SyncStateRepository(conn).update_sync(
            table_id="orders_daily_econ", rows=100, file_size_bytes=1000, hash="a" * 32
        )
    finally:
        conn.close()
    return db_path


def _run_update(system_db_path, meta_rows, data_dir):
    def fake_get_system_db():
        return duckdb.connect(str(system_db_path))

    with (
        patch("src.db.get_system_db", side_effect=fake_get_system_db),
        patch("src.repositories.get_system_db", side_effect=fake_get_system_db),
        patch("src.orchestrator._get_extracts_dir", return_value=data_dir / "extracts"),
    ):
        orch = SyncOrchestrator.__new__(SyncOrchestrator)
        orch._update_sync_state(meta_rows=meta_rows, source_name="bigquery")


def _get_state(system_db_path, table_id):
    conn = duckdb.connect(str(system_db_path))
    try:
        return SyncStateRepository(conn).get_table_state(table_id)
    finally:
        conn.close()


def test_rebuild_does_not_bump_materialized_last_sync(system_db_path, tmp_path):
    """A rebuild pass over a materialized row must leave its sync_state
    untouched — otherwise the daily due-check never fires again after a
    failed run (retry starves until the next day)."""
    (tmp_path / "extracts" / "bigquery" / "data").mkdir(parents=True)
    before = _get_state(system_db_path, "orders_daily_econ")
    assert before is not None and before["last_sync"] is not None

    _run_update(
        system_db_path,
        meta_rows=[("orders_daily_econ", 100, 1000, "materialized")],
        data_dir=tmp_path,
    )

    after = _get_state(system_db_path, "orders_daily_econ")
    assert after is not None
    assert after["last_sync"] == before["last_sync"], (
        "rebuild bookkeeping bumped last_sync for a materialized row; "
        "this masks failed daily runs from the due-check (no same-day retry)"
    )
    assert after["hash"] == before["hash"], "materialized hash is owned by the materialize pass"


def test_fallback_publish_preserves_materialized_schedule_gate(system_db_path, tmp_path):
    """The filesystem-fallback publish path fires exactly when `_meta` is
    missing — e.g. a materialize killed between the parquet swap and the
    `_meta` update. It must clear the stale error (the table IS serving
    data) but must NOT bump last_sync: the gate has to stay open so the
    next tick re-runs the materialize and heals `_meta`."""
    conn = duckdb.connect(str(system_db_path))
    try:
        SyncStateRepository(conn).set_error("orders_daily_econ", "killed mid-run")
        before = SyncStateRepository(conn).get_table_state("orders_daily_econ")
    finally:
        conn.close()
    assert before["status"] == "error" and before["last_sync"] is not None

    pq = tmp_path / "orders_daily_econ.parquet"
    pq.write_bytes(b"PAR1" + b"x" * 64 + b"PAR1")

    def fake_get_system_db():
        return duckdb.connect(str(system_db_path))

    view_conn = duckdb.connect()
    view_conn.execute('CREATE TABLE "orders_daily_econ" AS SELECT 1 AS x')
    with (
        patch("src.db.get_system_db", side_effect=fake_get_system_db),
        patch("src.repositories.get_system_db", side_effect=fake_get_system_db),
    ):
        orch = SyncOrchestrator.__new__(SyncOrchestrator)
        orch._record_fallback_sync_state(view_conn, "orders_daily_econ", pq)
    view_conn.close()

    after = _get_state(system_db_path, "orders_daily_econ")
    assert after["last_sync"] == before["last_sync"], (
        "fallback publish bumped last_sync for a materialized row; the "
        "due-check gate must stay open so the next tick re-materializes"
    )
    assert after["status"] == "ok", "stale error must be cleared — the table is serving data"
    assert after["rows"] == 1, "the publish itself must still be recorded (rows from the view)"
    assert len(after["hash"] or "") == 32, "content MD5 recorded so `agnes pull` verifies"


def test_rebuild_still_bumps_local_rows(system_db_path, tmp_path):
    """Regression guard: local-mode rows keep the existing bookkeeping."""
    (tmp_path / "extracts" / "bigquery" / "data").mkdir(parents=True)
    _run_update(
        system_db_path,
        meta_rows=[
            ("orders_daily_econ", 100, 1000, "materialized"),
            ("orders", 50, 512, "local"),
        ],
        data_dir=tmp_path,
    )

    local_state = _get_state(system_db_path, "orders")
    assert local_state is not None, "local rows must still get sync_state bookkeeping"
    assert isinstance(local_state["last_sync"], datetime)


def test_fallback_publish_preserves_gate_when_id_differs_from_name(tmp_path):
    """Regression: _record_fallback_sync_state previously called
    table_registry_repo().get(table_id) which queries WHERE id = ?. When
    registry id != name (parquet filename stem), the lookup returned None,
    is_materialized was False, and last_sync was bumped — starving retries.

    B1: sync_state.table_id is now the registry id (resolved via
    get_by_name, same lookup this test already exercised) rather than the
    name/filename-stem parameter, so the pre-seeded "before" row — and the
    read after the fallback publish — use the registry id `mat_001`, not
    the name `orders_daily_econ`."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    try:
        _ensure_schema(conn)
        registry = TableRegistryRepository(conn)
        registry.register(
            id="mat_001",
            name="orders_daily_econ",
            source_type="bigquery",
            bucket="",
            source_table="orders_daily_econ",
            query_mode="materialized",
            description="",
            sync_schedule="daily 06:00",
        )
        from src.repositories.sync_state import SyncStateRepository

        SyncStateRepository(conn).update_sync(table_id="mat_001", rows=100, file_size_bytes=1000, hash="a" * 32)
        SyncStateRepository(conn).set_error("mat_001", "killed mid-run")
        before = SyncStateRepository(conn).get_table_state("mat_001")
    finally:
        conn.close()
    assert before["last_sync"] is not None

    pq = tmp_path / "orders_daily_econ.parquet"
    pq.write_bytes(b"PAR1" + b"x" * 64 + b"PAR1")

    def fake_get_system_db():
        return duckdb.connect(str(db_path))

    view_conn = duckdb.connect()
    view_conn.execute('CREATE TABLE "orders_daily_econ" AS SELECT 1 AS x')
    with (
        patch("src.db.get_system_db", side_effect=fake_get_system_db),
        patch("src.repositories.get_system_db", side_effect=fake_get_system_db),
    ):
        orch = SyncOrchestrator.__new__(SyncOrchestrator)
        orch._record_fallback_sync_state(view_conn, "orders_daily_econ", pq)
    view_conn.close()

    after = _get_state(db_path, "mat_001")
    assert after["last_sync"] == before["last_sync"], (
        "fallback publish bumped last_sync for a materialized row with id != name; "
        "get_by_name must be used so the schedule gate stays open"
    )
    assert after["status"] == "ok"
