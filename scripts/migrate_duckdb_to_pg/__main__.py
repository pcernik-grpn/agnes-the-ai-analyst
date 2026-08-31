"""CLI entry point: ``python -m scripts.migrate_duckdb_to_pg``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="One-shot DuckDB → Postgres data migration",
    )
    parser.add_argument(
        "--duckdb-path",
        default=None,
        help="Path to system.duckdb (default: ${DATA_DIR}/state/system.duckdb)",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="Only run the named task (target_table). Repeatable.",
    )
    parser.add_argument("--dry-run", action="store_true", help="No PG writes")
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip post-copy validation (row counts + checksums)",
    )
    parser.add_argument(
        "--reset-target",
        action="store_true",
        help=(
            "Truncate the target state tables before copying so a RETRY "
            "rebuilds a fresh mirror of the DuckDB source. Required for a "
            "one-time cutover (e.g. infra pg-cutover.sh): without it the bare "
            "ON CONFLICT DO NOTHING copy keeps stale rows from a prior failed "
            "attempt. NEVER pass this on the docker-compose data-migrate "
            "one-shot — it re-runs every boot and would wipe live data."
        ),
    )
    parser.add_argument(
        "--missing-source-ok",
        action="store_true",
        help=(
            "Exit 0 when the source DuckDB file does not exist (fresh "
            "deployment — nothing to migrate). Passed by the docker-compose "
            "data-migrate one-shot so a brand-new data volume can boot: "
            "app/scheduler gate on this service exiting 0. Default is exit 2 "
            "so operator-driven runs fail loudly on a missing or mis-mounted "
            "source. Incompatible with --reset-target."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Run the copy even though a completion marker records a "
            "finished migration. After completion the DuckDB snapshot is "
            "no longer a source of truth — re-copying it re-inserts rows "
            "deleted from Postgres since. Only force when you know the "
            "marker is wrong (e.g. the target database was replaced)."
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="DEBUG-level logging")
    args = parser.parse_args()

    if args.missing_source_ok and args.reset_target:
        # --reset-target asserts a one-time cutover, which requires an
        # existing source; tolerating a missing one would let a mis-mounted
        # volume "succeed" as an empty copy after truncating the target.
        parser.error("--missing-source-ok cannot be combined with --reset-target")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.duckdb_path:
        duckdb_path = Path(args.duckdb_path)
    else:
        data_dir = os.environ.get("DATA_DIR")
        if not data_dir:
            print("DATA_DIR not set and --duckdb-path not provided", file=sys.stderr)
            return 2
        duckdb_path = Path(data_dir) / "state" / "system.duckdb"
    if not duckdb_path.is_file():
        if args.missing_source_ok:
            # Fresh deployment: alembic has already created the PG schema
            # (the compose `migrate` one-shot runs first) and there is no
            # DuckDB state to copy. Exit 0 so app/scheduler can boot — but
            # loudly, so an operator who EXPECTED an existing source spots a
            # mis-mounted data volume in the logs.
            print(
                f"source DuckDB not found: {duckdb_path} — nothing to migrate "
                "(fresh deployment). If you expected an existing "
                "system.duckdb, check the /data volume mount."
            )
            return 0
        print(f"DuckDB file not found: {duckdb_path}", file=sys.stderr)
        return 2

    from scripts.migrate_duckdb_to_pg.marker import (
        marker_path,
        read_completion_marker,
        target_has_app_state,
        write_completion_marker,
    )

    import src.db_pg as db_pg
    from src.duckdb_conn import _open_duckdb
    from scripts.migrate_duckdb_to_pg import run_all

    pg_engine = db_pg.get_engine()

    # Completion gate: once a run has migrated EVERYTHING successfully, the
    # source file is a frozen snapshot and Postgres is the source of truth.
    # Re-running the ON CONFLICT DO NOTHING copy from that snapshot would
    # re-insert any row deleted from Postgres since the migration (original
    # values, no audit trail) — the compose data-migrate one-shot re-runs on
    # every `compose up`, so without this gate every post-cutover delete of
    # a pre-cutover row silently reverts on the next container recreate.
    # The target-side probe keeps the marker honest against a replaced or
    # restored database (see target_has_app_state). --force overrides;
    # --reset-target already asserts a deliberate re-cutover; --dry-run
    # writes nothing and stays available as a diagnostic.
    if not (args.force or args.reset_target or args.dry_run):
        marker = read_completion_marker(duckdb_path)
        if marker is not None:
            if target_has_app_state(pg_engine):
                print(
                    f"migration already completed at {marker.get('completed_at')} "
                    f"({marker_path(duckdb_path)}) — skipping copy. The DuckDB "
                    "snapshot is no longer a source of truth; re-copying it would "
                    "resurrect rows deleted from Postgres since the migration. "
                    "Pass --force to copy anyway."
                )
                return 0
            print(
                "completion marker present but the target holds no app state — "
                "assuming the target database was replaced or restored; "
                "running the copy despite the marker."
            )

    duck_conn = _open_duckdb(str(duckdb_path), read_only=True)

    reports = run_all(
        duck_conn,
        pg_engine,
        only=args.only or None,
        dry_run=args.dry_run,
        validate=not args.no_validate,
        reset_target=args.reset_target,
    )
    for r in reports:
        print(r)
    # Per-task errors land as {"table": ..., "error": ...} without a
    # checksum_match key; the default-True on .get() previously masked
    # them. Treat the explicit error key as the authoritative failure
    # signal. Both predicates must hold for exit 0.
    ok = all("error" not in r and r.get("checksum_match", True) for r in reports)
    # Record completion only for a run that could have completed the whole
    # migration: every task selected (no --only), rows actually written
    # (no --dry-run), and nothing failed or was halt-skipped. A partial or
    # diagnostic run must not claim the snapshot is fully mirrored.
    if ok and not args.dry_run and not args.only and not any(r.get("skipped") for r in reports):
        write_completion_marker(
            duckdb_path,
            tables_migrated=len(reports),
            source="migrate_duckdb_to_pg",
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
