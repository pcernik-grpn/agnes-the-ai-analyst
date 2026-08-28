"""Initialize Jira extract.duckdb with _meta table and views for all entity types.

Called once on first webhook or manually via CLI. Creates the extract.duckdb
contract structure for the Jira connector.
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from src.duckdb_conn import _open_duckdb
from src.sql_ident import quote_ident

logger = logging.getLogger(__name__)

JIRA_TABLES = ["issues", "comments", "attachments", "changelog", "issuelinks", "remote_links"]

# Current-state dimension tables: a single `data.parquet` directly under the table
# directory, with no `month=` partitioning. The tables above are append-mostly event
# streams partitioned by the month of the parent issue; an organization has one
# current name and one current set of detail values, so partitioning it by month
# would either invent a history it does not have or scatter one row per month.
# Views over these are built without `hive_partitioning` (there is no partition key
# to project) but keep `union_by_name` so adding a detail column stays non-breaking.
JIRA_FLAT_TABLES = ["organizations"]


def _is_flat_table(table_name: str) -> bool:
    """Is this table a current-state dimension rather than a month-partitioned stream?"""
    return table_name in JIRA_FLAT_TABLES


def _table_parquets(table_name: str, table_dir: Path) -> tuple[str, list[Path]]:
    """``(glob_path, existing_files)`` for a table, whichever layout it uses."""
    if _is_flat_table(table_name):
        return str(table_dir / "*.parquet"), list(table_dir.glob("*.parquet"))
    return str(table_dir / "month=*" / "*.parquet"), list(table_dir.glob("month=*/data.parquet"))


def _view_sql(table_name: str, glob_path: str) -> str:
    """``CREATE OR REPLACE VIEW`` over a table's parquet glob.

    ``union_by_name`` on both layouts, so adding a column stays non-breaking;
    ``hive_partitioning`` only where there is a partition key to project.
    """
    hive = "" if _is_flat_table(table_name) else ", hive_partitioning=true"
    return (
        f"CREATE OR REPLACE VIEW {quote_ident(table_name)} AS "
        f"SELECT * FROM read_parquet('{glob_path}', union_by_name=true{hive})"
    )


def _rebuild_view_and_stats(conn, table_name: str, table_dir: Path) -> tuple[int | None, int]:
    """Recreate a table's view and return its ``(rows, size_bytes)``.

    ``(0, 0)`` when the table has no parquet yet — DuckDB's glob fails on an empty
    directory, so the view is left uncreated rather than pointing at nothing. This is
    a genuinely empty table, not a failure.

    ``(None, 0)`` when parquet files exist but the view build/count raised — #1364.
    DuckDB's own exception already names the offending file (see the WARNING below);
    the bug this closes was collapsing that into the SAME ``(0, 0)`` an honestly empty
    table reports, making a corrupt table invisible on the one column an operator
    would look at. ``None`` is a real "row count unavailable", never silently coerced
    to ``0`` downstream — ``rows BIGINT`` in ``_meta`` is nullable, and
    ``src.orchestrator._update_sync_state`` treats a NULL here as "could not count"
    and flags it via the existing ``sync_state.status``/``error`` columns (the same
    ones ``GET /api/admin/registry`` / ``agnes admin list-tables`` already surface as
    ``last_sync_status``/``last_sync_error``) instead of publishing a plain, unflagged
    zero.

    A failure to build the view is warned about and NOT raised. That is deliberate,
    and it makes ``init_extract`` more forgiving than it used to be: it previously
    created views outside its try, so one unreadable parquet aborted the whole init.
    ``update_meta`` already swallowed the same failure, and it runs after every
    webhook transform — so raising here would turn a single corrupt partition into a
    failing ingest path. The table simply reports an unavailable count until the
    parquet is readable again.
    """
    glob_path, files = _table_parquets(table_name, table_dir)
    if not files:
        return 0, 0
    try:
        conn.execute(_view_sql(table_name, glob_path))
        rows = conn.execute(f"SELECT count(*) FROM {quote_ident(table_name)}").fetchone()[0]
        return rows, sum(f.stat().st_size for f in files)
    except Exception as e:
        logger.warning(
            "Could not count rows for %s: %s — reporting row count as unavailable, NOT as zero (see #1364).",
            table_name,
            e,
        )
        return None, 0


def _upsert_meta(conn, table_name: str, rows: int | None, size_bytes: int, now: datetime) -> None:
    """Write a table's catalog row, inserting it when it is not there yet.

    An UPDATE alone is not enough: on an instance whose extract.duckdb predates a
    table, `_meta` has no row for it and a bare UPDATE matches nothing, leaving the
    table absent from the catalog (and so unlisted for users) until someone happens
    to re-run ``init_extract``.

    ``rows`` may be ``None`` (#1364) — "the extractor could not count this table's
    rows this pass", distinct from a genuine ``0``. `_meta.rows` is nullable
    specifically to carry that distinction through to `_update_sync_state`.
    """
    if conn.execute("SELECT count(*) FROM _meta WHERE table_name = ?", [table_name]).fetchone()[0]:
        conn.execute(
            "UPDATE _meta SET rows = ?, size_bytes = ?, extracted_at = ? WHERE table_name = ?",
            [rows, size_bytes, now, table_name],
        )
    else:
        conn.execute(
            "INSERT INTO _meta VALUES (?, ?, ?, ?, ?, 'local')",
            [table_name, f"Jira {table_name}", rows, size_bytes, now],
        )


def init_extract(output_dir: str | Path) -> None:
    """Create /data/extracts/jira/extract.duckdb with _meta and views.

    Views point to monthly parquet partitions in data/{table}/*.parquet.
    Safe to call multiple times — recreates _meta and views.
    """
    output_path = Path(output_dir)
    data_dir = output_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    db_path = output_path / "extract.duckdb"
    conn = _open_duckdb(str(db_path))

    try:
        # Create _meta table
        conn.execute("DROP TABLE IF EXISTS _meta")
        conn.execute("""CREATE TABLE _meta (
            table_name VARCHAR NOT NULL,
            description VARCHAR,
            rows BIGINT,
            size_bytes BIGINT,
            extracted_at TIMESTAMP,
            query_mode VARCHAR DEFAULT 'local'
        )""")

        now = datetime.now(timezone.utc)
        for table_name in JIRA_TABLES + JIRA_FLAT_TABLES:
            table_dir = data_dir / table_name
            table_dir.mkdir(exist_ok=True)

            if not _is_flat_table(table_name):
                # Migrate any remaining flat YYYY-MM.parquet files to hive layout
                # before building the view so the glob always points at hive dirs.
                # Skipped for dimension tables, whose single data.parquet is the
                # intended layout rather than an unmigrated legacy one.
                try:
                    from .incremental_transform import migrate_flat_to_hive

                    migrated = migrate_flat_to_hive(table_dir)
                    if migrated:
                        logger.info(
                            "Migrated %d flat parquet(s) to hive layout for %s: %s",
                            len(migrated),
                            table_name,
                            migrated,
                        )
                except Exception as mig_err:
                    logger.warning("Could not migrate flat parquets for %s: %s", table_name, mig_err)

            rows, size_bytes = _rebuild_view_and_stats(conn, table_name, table_dir)
            _upsert_meta(conn, table_name, rows, size_bytes, now)

        logger.info(
            "Initialized Jira extract.duckdb at %s with %d tables",
            db_path,
            len(JIRA_TABLES) + len(JIRA_FLAT_TABLES),
        )
    finally:
        conn.close()


def update_meta(output_dir: str | Path, table_name: str) -> None:
    """Update _meta entry for a table after parquet write.

    Called after incremental_transform writes/updates a parquet file.
    """
    output_path = Path(output_dir)
    db_path = output_path / "extract.duckdb"

    if not db_path.exists():
        init_extract(output_dir)
        return

    conn = _open_duckdb(str(db_path))
    try:
        table_dir = output_path / "data" / table_name
        # The view is recreated (not just counted) so it picks up new partition dirs,
        # and on a dimension table so it picks up a newly added detail column.
        rows, size_bytes = _rebuild_view_and_stats(conn, table_name, table_dir)
        _upsert_meta(conn, table_name, rows, size_bytes, datetime.now(timezone.utc))
        conn.execute("CHECKPOINT")
    finally:
        conn.close()


def get_default_output_dir() -> Path:
    """Get the default Jira extract output directory."""
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    return data_dir / "extracts" / "jira"


if __name__ == "__main__":
    from app.logging_config import setup_logging

    setup_logging(__name__)
    init_extract(get_default_output_dir())
