"""Cross-engine contract tests for the sync_state repository.

Targets: sync_state_repo (DuckDB + Postgres). Parametrises over both
backends; identical inputs must produce identical outputs.

Follows the fixture pattern in test_rbac_contract.py: DuckDB via
_ensure_schema, Postgres via alembic upgrade -> head.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.sync_state import SyncStateRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return SyncStateRepository(conn), conn


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
    engine = db_pg.get_engine()

    from src.repositories.sync_state_pg import SyncStatePgRepository

    return SyncStatePgRepository(engine), None


@pytest.fixture(params=["duckdb", "pg"])
def sync_repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields ``(repo, raw_conn_or_None, backend)`` for both backends."""
    backend = request.param
    if backend == "duckdb":
        repo, conn = _make_duckdb_repo(tmp_path)
        yield repo, conn, backend
        if conn is not None:
            conn.close()
    else:
        repo, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield repo, None, backend


# ---------------------------------------------------------------------------
# contract tests
# ---------------------------------------------------------------------------


def test_clear_for_table_removes_state_and_history(sync_repo):
    repo, _, _ = sync_repo

    # Seed via the existing update_sync(): writes both sync_state and
    # sync_history in one call.
    repo.update_sync(
        table_id="bucket.orders",
        rows=42,
        file_size_bytes=1024,
        hash="abc123",
        duration_ms=99,
    )

    assert repo.get_table_state("bucket.orders") is not None
    assert repo.get_sync_history("bucket.orders") != []

    removed = repo.clear_for_table("bucket.orders")
    assert removed == 1

    assert repo.get_table_state("bucket.orders") is None
    assert repo.get_sync_history("bucket.orders") == []


def test_clear_for_table_no_rows_returns_zero(sync_repo):
    repo, _, _ = sync_repo
    assert repo.clear_for_table("never.synced") == 0


def test_clear_for_table_only_targets_named_table(sync_repo):
    repo, _, _ = sync_repo

    repo.update_sync(table_id="t.keep", rows=1, file_size_bytes=10, hash="h1")
    repo.update_sync(table_id="t.drop", rows=2, file_size_bytes=20, hash="h2")

    removed = repo.clear_for_table("t.drop")
    assert removed == 1

    assert repo.get_table_state("t.drop") is None
    assert repo.get_sync_history("t.drop") == []
    # Untouched sibling survives.
    assert repo.get_table_state("t.keep") is not None
    assert repo.get_sync_history("t.keep") != []


# ---------------------------------------------------------------------------
# set_skipped (#754) — per-table skip reason, mirrors set_error's shape.
# ---------------------------------------------------------------------------


def test_set_skipped_creates_row_with_reason(sync_repo):
    repo, _, _ = sync_repo

    repo.set_skipped("bucket.orders", "in_flight")

    state = repo.get_table_state("bucket.orders")
    assert state["status"] == "skipped"
    assert state["error"] == "in_flight"
    # First-ever skip (no prior sync) must not claim a sync happened.
    assert state["last_sync"] is None


def test_set_skipped_preserves_prior_sync_fields(sync_repo):
    repo, _, _ = sync_repo

    repo.update_sync(table_id="bucket.orders", rows=42, file_size_bytes=1024, hash="abc123")
    repo.set_skipped("bucket.orders", "source_filter")

    state = repo.get_table_state("bucket.orders")
    assert state["status"] == "skipped"
    assert state["error"] == "source_filter"
    # Untouched — the last successful sync's data stays visible to the
    # manifest / `agnes pull` while this run's skip reason is recorded.
    assert state["rows"] == 42
    assert state["hash"] == "abc123"
    assert state["last_sync"] is not None


def test_update_sync_can_preserve_last_sync(sync_repo):
    """`bump_last_sync=False` records fresh rows/hash and clears a prior
    error WITHOUT touching last_sync — the filesystem-fallback publish path
    for materialized rows needs exactly this so the daily schedule gate
    stays open (a bumped last_sync would starve same-day retries)."""
    repo, _, _ = sync_repo

    repo.update_sync(table_id="mat.orders", rows=1, file_size_bytes=10, hash="a" * 32)
    before = repo.get_table_state("mat.orders")["last_sync"]
    assert before is not None
    repo.set_error("mat.orders", "killed mid-run")

    repo.update_sync(table_id="mat.orders", rows=7, file_size_bytes=70, hash="b" * 32, bump_last_sync=False)

    state = repo.get_table_state("mat.orders")
    assert state["last_sync"] == before, "bump_last_sync=False must preserve last_sync"
    assert state["rows"] == 7
    assert state["hash"] == "b" * 32
    assert state["status"] == "ok"
    assert state["error"] in (None, "")


def test_update_sync_preserve_on_fresh_row_leaves_last_sync_null(sync_repo):
    """First-ever write with bump_last_sync=False must not fabricate a
    last_sync — NULL keeps the row 'due' and the manifest honest."""
    repo, _, _ = sync_repo

    repo.update_sync(table_id="mat.fresh", rows=3, file_size_bytes=30, hash="c" * 32, bump_last_sync=False)

    state = repo.get_table_state("mat.fresh")
    assert state is not None
    assert state["last_sync"] is None
    assert state["rows"] == 3
    assert state["status"] == "ok"


def test_update_sync_clears_a_prior_skip(sync_repo):
    """A table that gets skipped one run and materializes successfully the
    next must flip back to status='ok' — mirrors `update_sync` already
    clearing a prior `set_error`."""
    repo, _, _ = sync_repo

    repo.set_skipped("bucket.orders", "not_in_target")
    repo.update_sync(table_id="bucket.orders", rows=1, file_size_bytes=10, hash="h1")

    state = repo.get_table_state("bucket.orders")
    assert state["status"] == "ok"
    assert state["error"] in (None, "")


# ---------------------------------------------------------------------------
# status_counts_since — Activity Center health-pulse "sync_24h" field
# ---------------------------------------------------------------------------


def test_status_counts_since_groups_by_status(sync_repo):
    repo, _, _ = sync_repo

    repo.update_sync(table_id="t.ok1", rows=1, file_size_bytes=10, hash="h1", status="ok")
    repo.update_sync(table_id="t.ok2", rows=1, file_size_bytes=10, hash="h2", status="ok")
    repo.update_sync(table_id="t.err", rows=0, file_size_bytes=0, hash="h3", status="error", error="boom")

    since = datetime.now(timezone.utc) - timedelta(hours=1)
    counts = repo.status_counts_since(since)
    assert counts == {"ok": 2, "error": 1}


def test_status_counts_since_empty_window_returns_empty_dict(sync_repo):
    repo, _, _ = sync_repo
    repo.update_sync(table_id="t.ok1", rows=1, file_size_bytes=10, hash="h1")

    since = datetime.now(timezone.utc) + timedelta(hours=1)
    assert repo.status_counts_since(since) == {}


# ---------------------------------------------------------------------------
# parts[] — per-partition manifest for partitioned tables (partitioned
# distribution). A partitioned table stores its per-part {path,hash,size}
# list; single-file tables carry parts=None. Must round-trip type-identical
# on both backends (DuckDB JSON string vs PG JSONB object).
# ---------------------------------------------------------------------------


def test_update_sync_round_trips_parts(sync_repo):
    repo, _, _ = sync_repo

    parts = [
        {"path": "month=2026-06/data.parquet", "hash": "aa11", "size_bytes": 100},
        {"path": "month=2026-07/data.parquet", "hash": "bb22", "size_bytes": 250},
    ]
    repo.update_sync(
        table_id="jira.issues",
        rows=5,
        file_size_bytes=350,
        hash="rollup",
        parts=parts,
    )

    state = repo.get_table_state("jira.issues")
    assert state["parts"] == parts


def test_update_sync_without_parts_is_none(sync_repo):
    """Single-file tables never carry parts — the column stays NULL so the
    manifest treats them as single-file (backward compatible)."""
    repo, _, _ = sync_repo

    repo.update_sync(table_id="kbc.account", rows=9, file_size_bytes=90, hash="h")

    state = repo.get_table_state("kbc.account")
    assert state["parts"] is None


def test_set_skipped_preserves_parts(sync_repo):
    """A skip must not wipe a partitioned table's parts — analysts keep
    serving the last-good part set while this run's skip reason is recorded."""
    repo, _, _ = sync_repo

    parts = [{"path": "month=2026-06/data.parquet", "hash": "aa11", "size_bytes": 100}]
    repo.update_sync(table_id="jira.comments", rows=1, file_size_bytes=100, hash="r", parts=parts)
    repo.set_skipped("jira.comments", "in_flight")

    state = repo.get_table_state("jira.comments")
    assert state["status"] == "skipped"
    assert state["parts"] == parts


def test_get_all_states_deserializes_parts(sync_repo):
    """get_all_states must deserialize parts identically to get_table_state."""
    repo, _, _ = sync_repo

    parts = [{"path": "2025_11.parquet", "hash": "cc33", "size_bytes": 42}]
    repo.update_sync(table_id="kbc.partitioned", rows=1, file_size_bytes=42, hash="r", parts=parts)

    all_states = {s["table_id"]: s for s in repo.get_all_states()}
    assert all_states["kbc.partitioned"]["parts"] == parts


# ---------------------------------------------------------------------------
# Track E3 Slice 1: prune_history_older_than — opt-in sync_history retention
# ---------------------------------------------------------------------------


def _backdate_sync_history(sync_repo_tuple, history_id: str, ts: datetime) -> None:
    """Rewrite one sync_history row's `synced_at` directly — `update_sync`
    always stamps `now()`, so the prune tests need an implementation-specific
    path to plant an old row (same reasoning as test_audit_contract.py's
    `_backdate` helper)."""
    repo, conn, backend = sync_repo_tuple
    if backend == "duckdb":
        conn.execute("UPDATE sync_history SET synced_at = ? WHERE id = ?", [ts, history_id])
    else:
        import sqlalchemy as sa

        with repo._engine.begin() as c:
            c.execute(
                sa.text("UPDATE sync_history SET synced_at = :ts WHERE id = :id"),
                {"ts": ts, "id": history_id},
            )


def test_prune_history_older_than_deletes_only_old_rows(sync_repo):
    repo, _, _ = sync_repo
    repo.update_sync(table_id="t.old", rows=1, file_size_bytes=10, hash="h1")
    repo.update_sync(table_id="t.new", rows=1, file_size_bytes=10, hash="h2")

    old_hist = repo.get_sync_history("t.old")[0]
    _backdate_sync_history(sync_repo, old_hist["id"], datetime.now(timezone.utc) - timedelta(days=400))

    pruned = repo.prune_history_older_than(365)

    assert pruned == 1
    assert repo.get_sync_history("t.old") == []
    assert repo.get_sync_history("t.new") != []


def test_prune_history_older_than_returns_zero_when_nothing_qualifies(sync_repo):
    repo, _, _ = sync_repo
    repo.update_sync(table_id="t.recent", rows=1, file_size_bytes=10, hash="h1")

    pruned = repo.prune_history_older_than(365)

    assert pruned == 0
    assert repo.get_sync_history("t.recent") != []


def test_prune_history_older_than_never_touches_sync_state(sync_repo):
    """The current-state row (sync_state) must survive even when its entire
    sync_history is pruned — a table that hasn't synced in over a year
    should still report its last-known state to the manifest / registry UI,
    just with no history rows to browse."""
    repo, _, _ = sync_repo
    repo.update_sync(table_id="t.old", rows=42, file_size_bytes=1024, hash="abc123")
    old_hist = repo.get_sync_history("t.old")[0]
    _backdate_sync_history(sync_repo, old_hist["id"], datetime.now(timezone.utc) - timedelta(days=400))

    pruned = repo.prune_history_older_than(365)

    assert pruned == 1
    assert repo.get_sync_history("t.old") == []
    state = repo.get_table_state("t.old")
    assert state is not None
    assert state["rows"] == 42
    assert state["hash"] == "abc123"
