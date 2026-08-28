"""#1364, first shape: `connectors/jira/extract_init.py::_rebuild_view_and_stats`
used to swallow a failed view build/count into ``(0, 0)`` — indistinguishable
from a genuinely empty table, even though DuckDB's own exception names the
offending file. This pins the fix: a failed count returns ``rows=None``
("could not count"), never the plain ``0`` a real empty table reports, and
that distinction survives into `_meta.rows` (nullable ``BIGINT``, no schema
change) for `src.orchestrator._update_sync_state` to flag on `sync_state`
(see `tests/test_orchestrator_sync_state_hash.py`'s
`test_update_sync_state_count_unavailable_is_flagged_not_silently_zeroed`).
"""

import duckdb
import pytest

from connectors.jira.extract_init import _rebuild_view_and_stats, init_extract, update_meta

# Same fixture used across the #1364 orchestrator tests: good leading PAR1
# magic, no trailing footer magic — the #1354 truncated-write shape. DuckDB
# raises building/counting a view over it; the exception names the file.
CORRUPT_PARQUET_BYTES = b"PAR1" + b"\x00" * 64


def test_rebuild_view_and_stats_no_files_is_genuinely_empty(tmp_path):
    """No parquet on disk at all: a real empty table, not a failure."""
    conn = duckdb.connect()
    try:
        table_dir = tmp_path / "issues"
        table_dir.mkdir()
        rows, size_bytes = _rebuild_view_and_stats(conn, "issues", table_dir)
    finally:
        conn.close()
    assert rows == 0
    assert size_bytes == 0


def test_rebuild_view_and_stats_corrupt_file_returns_none_not_zero(tmp_path, caplog):
    """A corrupt part makes the view build/count raise. The old behavior
    collapsed that into `(0, 0)` — identical to the empty-table case above.
    `rows` must come back `None` instead, and the WARNING must name the
    exact file DuckDB refused."""
    table_dir = tmp_path / "issues"
    (table_dir / "month=2026-01").mkdir(parents=True)
    bad_file = table_dir / "month=2026-01" / "data.parquet"
    bad_file.write_bytes(CORRUPT_PARQUET_BYTES)

    conn = duckdb.connect()
    try:
        with caplog.at_level("WARNING", logger="connectors.jira.extract_init"):
            rows, size_bytes = _rebuild_view_and_stats(conn, "issues", table_dir)
    finally:
        conn.close()

    assert rows is None, "count failed — must not silently look like a real 0"
    assert size_bytes == 0
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any(str(bad_file) in w for w in warnings), (
        f"expected a WARNING naming the corrupt file {bad_file}; got {warnings!r}"
    )


def test_rebuild_view_and_stats_one_bad_month_does_not_hide_behind_a_healthy_one(tmp_path):
    """A single corrupt month is enough to fail the WHOLE table's count —
    DuckDB's glob-based view build fails on the first unreadable file it
    touches, even with healthy siblings present. `rows` must still be
    `None`, not the healthy sibling's count alone and not `0`."""
    pa = pytest.importorskip("pyarrow")
    pq_mod = pytest.importorskip("pyarrow.parquet")

    table_dir = tmp_path / "issues"
    (table_dir / "month=2026-01").mkdir(parents=True)
    (table_dir / "month=2026-01" / "data.parquet").write_bytes(CORRUPT_PARQUET_BYTES)
    (table_dir / "month=2026-02").mkdir(parents=True)
    pq_mod.write_table(pa.table({"issue_key": ["PROJ-1"]}), table_dir / "month=2026-02" / "data.parquet")

    conn = duckdb.connect()
    try:
        rows, _ = _rebuild_view_and_stats(conn, "issues", table_dir)
    finally:
        conn.close()
    assert rows is None


def test_init_extract_meta_rows_null_when_count_fails(tmp_path):
    """End-to-end through `init_extract`: `_meta.rows` is SQL NULL for the
    damaged table, `0` (not NULL) for a genuinely empty sibling — the exact
    distinction `_update_sync_state` reads to decide whether to flag a
    table's `sync_state` row."""
    output_dir = tmp_path / "jira"
    output_dir.mkdir()
    data_dir = output_dir / "data"

    issues_dir = data_dir / "issues" / "month=2026-01"
    issues_dir.mkdir(parents=True)
    (issues_dir / "data.parquet").write_bytes(CORRUPT_PARQUET_BYTES)
    # "comments" is left with no parquet at all — genuinely empty sibling.

    init_extract(output_dir)

    from src.duckdb_conn import _open_duckdb

    conn = _open_duckdb(str(output_dir / "extract.duckdb"))
    try:
        issues_rows = conn.execute("SELECT rows FROM _meta WHERE table_name = 'issues'").fetchone()[0]
        comments_rows = conn.execute("SELECT rows FROM _meta WHERE table_name = 'comments'").fetchone()[0]
    finally:
        conn.close()
    assert issues_rows is None, "damaged table must report NULL (could not count), not 0"
    assert comments_rows == 0, "a genuinely empty table must still report a plain 0"


def test_update_meta_meta_rows_null_when_count_fails(tmp_path):
    """Same distinction through the incremental `update_meta` path (the one
    a Jira webhook rebuild actually calls) — not just the bulk `init_extract`
    path."""
    output_dir = tmp_path / "jira"
    output_dir.mkdir()
    data_dir = output_dir / "data"
    init_extract(output_dir)  # lay down the initial (empty) _meta + views

    issues_dir = data_dir / "issues" / "month=2026-01"
    issues_dir.mkdir(parents=True)
    (issues_dir / "data.parquet").write_bytes(CORRUPT_PARQUET_BYTES)

    update_meta(output_dir, "issues")

    from src.duckdb_conn import _open_duckdb

    conn = _open_duckdb(str(output_dir / "extract.duckdb"))
    try:
        rows = conn.execute("SELECT rows FROM _meta WHERE table_name = 'issues'").fetchone()[0]
    finally:
        conn.close()
    assert rows is None
