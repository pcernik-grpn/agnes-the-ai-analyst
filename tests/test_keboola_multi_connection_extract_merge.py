"""B2 regression: per-connection extractor groups must not clobber each
other's extract.duckdb.

`app.api.sync._run_sync` dispatches ONE `connectors.keboola.extractor.run()`
per `connection_id` credential group, and every group writes the SAME
`extracts/keboola/extract.duckdb`. `run()`'s default mode builds a fresh
`extract.duckdb.tmp` containing only that call's tables and atomically moves
it over the previous file — so with 2+ groups, only the LAST group's `_meta`
rows and views survived, and every earlier group's tables (local parquet rows
AND view-only remote rows) vanished from analytics at the next orchestrator
rebuild.

The fix: the first group of a pass still runs fresh (keeping the whole-pass
implicit prune for deleted/renamed registry rows), and every later group runs
`merge=True` — `run()` seeds its temp build from the current extract and
replaces only its own tables.

These tests exercise `run()` for real — real temp-DB build, real `_meta`
writes, real parquet COPY, real atomic swap — with only the network edge
faked: `_try_attach_extension` ATTACHes an in-memory `kbc` catalog seeded
with the group's source tables, exactly the surface the DuckDB Keboola
extension would provide (pattern from tests/test_keboola_extractor_dispatch.py).
"""

from pathlib import Path

import duckdb
import pytest


def _fake_attach(monkeypatch, tables_by_bucket):
    """Stub `_try_attach_extension` with an in-memory `kbc` catalog holding
    `tables_by_bucket` ({bucket: [table, ...]}), each with 2 rows."""
    from connectors.keboola import extractor

    def fake(conn, url, token):
        conn.execute("ATTACH ':memory:' AS kbc")
        for bucket, tables in tables_by_bucket.items():
            conn.execute(f'CREATE SCHEMA kbc."{bucket}"')
            for table in tables:
                conn.execute(f'CREATE TABLE kbc."{bucket}"."{table}" (id INTEGER)')
                conn.execute(f'INSERT INTO kbc."{bucket}"."{table}" VALUES (1), (2)')
        return True

    monkeypatch.setattr(extractor, "_try_attach_extension", fake)


def _local_cfg(name, bucket):
    return {
        "id": name,
        "name": name,
        "bucket": bucket,
        "source_table": name,
        "query_mode": "local",
        "sync_strategy": "full_refresh",
    }


def _remote_cfg(name, bucket, source_table):
    return {
        "id": name,
        "name": name,
        "bucket": bucket,
        "source_table": source_table,
        "query_mode": "remote",
    }


def _meta_names(extract_db):
    conn = duckdb.connect(str(extract_db), read_only=True)
    try:
        return {r[0] for r in conn.execute("SELECT table_name FROM _meta").fetchall()}
    finally:
        conn.close()


@pytest.fixture
def extract_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
    (tmp_path / "extracts").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "state").mkdir()
    return {
        "output": str(tmp_path / "extracts" / "keboola"),
        "extract_db": tmp_path / "extracts" / "keboola" / "extract.duckdb",
        "analytics_db": str(tmp_path / "analytics" / "server.duckdb"),
    }


def test_second_connection_group_merges_instead_of_clobbering(extract_env, monkeypatch):
    """Two credential groups in one pass → BOTH groups' tables must survive
    in the final extract.duckdb and in the orchestrator's master views."""
    from connectors.keboola.extractor import run

    # Group 1 — the global (connection_id IS NULL) project: fresh rebuild.
    _fake_attach(monkeypatch, {"in.c-a": ["orders_a"]})
    r1 = run(extract_env["output"], [_local_cfg("orders_a", "in.c-a")], "https://a.example", "tok-a")
    assert r1 == {"tables_extracted": 1, "tables_failed": 0, "errors": []}
    assert _meta_names(extract_env["extract_db"]) == {"orders_a"}

    # Group 2 — a named connection with its OWN credentials: merge.
    _fake_attach(monkeypatch, {"in.c-b": ["orders_b"]})
    r2 = run(
        extract_env["output"],
        [_local_cfg("orders_b", "in.c-b")],
        "https://b.example",
        "tok-b",
        merge=True,
    )
    assert r2 == {"tables_extracted": 1, "tables_failed": 0, "errors": []}

    # Both groups' tables live in the final extract, with queryable views.
    assert _meta_names(extract_env["extract_db"]) == {"orders_a", "orders_b"}
    conn = duckdb.connect(str(extract_env["extract_db"]), read_only=True)
    try:
        assert conn.execute("SELECT count(*) FROM orders_a").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM orders_b").fetchone()[0] == 2
    finally:
        conn.close()

    # And both make it into the master analytics views.
    from src.orchestrator import SyncOrchestrator

    result = SyncOrchestrator(analytics_db_path=extract_env["analytics_db"]).rebuild()
    assert set(result.get("keboola", [])) >= {"orders_a", "orders_b"}


def test_remote_rows_and_remote_attach_survive_merge(extract_env, monkeypatch):
    """A view-only remote row from group 1 must survive group 2's merge, and
    the merge must NOT clobber `_remote_attach` — the `kbc` alias keeps the
    FIRST (global) group's stack URL, which is the one the orchestrator's
    token_env resolution matches at re-ATTACH time."""
    from connectors.keboola.extractor import run

    # Group 1: one remote (live) row + one local row against stack A.
    _fake_attach(monkeypatch, {"in.c-a": ["orders", "events"]})
    r1 = run(
        extract_env["output"],
        [_remote_cfg("orders_live", "in.c-a", "orders"), _local_cfg("events", "in.c-a")],
        "https://a.example",
        "tok-a",
    )
    assert r1["tables_failed"] == 0

    # Group 2: a remote row against stack B, merged on top.
    _fake_attach(monkeypatch, {"in.c-b": ["clicks"]})
    r2 = run(
        extract_env["output"],
        [_remote_cfg("clicks_live", "in.c-b", "clicks")],
        "https://b.example",
        "tok-b",
        merge=True,
    )
    assert r2["tables_failed"] == 0

    assert _meta_names(extract_env["extract_db"]) == {"orders_live", "events", "clicks_live"}

    conn = duckdb.connect(str(extract_env["extract_db"]), read_only=True)
    try:
        modes = dict(conn.execute("SELECT table_name, query_mode FROM _meta").fetchall())
        assert modes["orders_live"] == "remote"
        assert modes["clicks_live"] == "remote"
        # First writer keeps the kbc alias — group 2's merge must not have
        # DROP-and-recreated the table with stack B's URL.
        attach_rows = conn.execute("SELECT alias, extension, url, token_env FROM _remote_attach").fetchall()
        assert attach_rows == [("kbc", "keboola", "https://a.example", "KEBOOLA_STORAGE_TOKEN")]
    finally:
        conn.close()


def test_re_extracted_table_is_replaced_not_duplicated_in_merge(extract_env, monkeypatch):
    """Merge mode prunes its OWN tables' previous `_meta` rows before
    re-inserting, so re-running a group never duplicates rows."""
    from connectors.keboola.extractor import run

    _fake_attach(monkeypatch, {"in.c-b": ["orders_b"]})
    run(extract_env["output"], [_local_cfg("orders_b", "in.c-b")], "https://b.example", "tok-b")
    run(
        extract_env["output"],
        [_local_cfg("orders_b", "in.c-b")],
        "https://b.example",
        "tok-b",
        merge=True,
    )

    conn = duckdb.connect(str(extract_env["extract_db"]), read_only=True)
    try:
        count = conn.execute("SELECT count(*) FROM _meta WHERE table_name = 'orders_b'").fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_default_fresh_run_still_rebuilds_from_scratch(extract_env, monkeypatch):
    """merge=False (the pass's first group) keeps the historical semantics:
    the extract is rebuilt with ONLY this call's tables — the implicit prune
    that makes deleted/renamed registry rows disappear."""
    from connectors.keboola.extractor import run

    _fake_attach(monkeypatch, {"in.c-a": ["orders_a"]})
    run(extract_env["output"], [_local_cfg("orders_a", "in.c-a")], "https://a.example", "tok-a")

    _fake_attach(monkeypatch, {"in.c-b": ["orders_b"]})
    run(extract_env["output"], [_local_cfg("orders_b", "in.c-b")], "https://b.example", "tok-b")

    assert _meta_names(extract_env["extract_db"]) == {"orders_b"}


def test_merge_with_no_existing_extract_behaves_like_fresh(extract_env, monkeypatch):
    """merge=True with no prior extract.duckdb (first-ever pass whose first
    group failed, say) must still produce a complete extract."""
    from connectors.keboola.extractor import run

    _fake_attach(monkeypatch, {"in.c-b": ["orders_b"]})
    r = run(
        extract_env["output"],
        [_local_cfg("orders_b", "in.c-b")],
        "https://b.example",
        "tok-b",
        merge=True,
    )
    assert r == {"tables_extracted": 1, "tables_failed": 0, "errors": []}
    assert _meta_names(extract_env["extract_db"]) == {"orders_b"}


def test_no_stale_tmp_left_behind(extract_env, monkeypatch):
    """The merge seed copy must not leave extract.duckdb.tmp lying around."""
    from connectors.keboola.extractor import run

    _fake_attach(monkeypatch, {"in.c-a": ["orders_a"]})
    run(extract_env["output"], [_local_cfg("orders_a", "in.c-a")], "https://a.example", "tok-a")
    _fake_attach(monkeypatch, {"in.c-b": ["orders_b"]})
    run(
        extract_env["output"],
        [_local_cfg("orders_b", "in.c-b")],
        "https://b.example",
        "tok-b",
        merge=True,
    )
    assert not Path(extract_env["output"], "extract.duckdb.tmp").exists()
