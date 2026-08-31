"""PG-backed tests for db_state_migrator (alembic upgrade step).

JobWriter unit tests live in ``tests/test_db_state_migrator.py`` because
they don't need a Postgres instance. This module covers steps that
require a real PG target — currently just ``alembic_upgrade_head``.
"""
from __future__ import annotations


def test_alembic_upgrade_head_runs(tmp_path, pg_engine):
    """alembic_upgrade_head brings target to current head revision."""
    from scripts.db_state_migrator import alembic_upgrade_head

    alembic_upgrade_head(str(pg_engine.url))

    # Verify alembic_version row exists with head revision
    import sqlalchemy as sa
    with pg_engine.connect() as conn:
        row = conn.execute(sa.text("SELECT version_num FROM alembic_version")).fetchone()
    assert row is not None
    assert len(row[0]) > 0


def test_copy_duckdb_to_pg_full_cycle(tmp_path, pg_engine):
    """Seed DuckDB → copy to PG → verify rows present."""
    import duckdb
    from src.db import _ensure_schema

    duck_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.execute(
        "INSERT INTO users (id, email, name) VALUES ('u1', 'alice@example.com', 'Alice')"
    )
    conn.close()

    from scripts.db_state_migrator import alembic_upgrade_head, copy_duckdb_to_pg
    alembic_upgrade_head(str(pg_engine.url))

    summary = copy_duckdb_to_pg(duck_path, str(pg_engine.url))
    assert summary["rows_total"] >= 1

    import sqlalchemy as sa
    with pg_engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT email FROM users WHERE id = :id"), {"id": "u1"}
        ).fetchone()
    assert row[0] == "alice@example.com"


def test_target_has_app_state_probe(tmp_path, pg_engine):
    """The marker's replaced-target guard: empty schema → False (copy must
    run), any user row → True (marker may be honored)."""
    import sqlalchemy as sa

    from scripts.db_state_migrator import alembic_upgrade_head
    from scripts.migrate_duckdb_to_pg.marker import target_has_app_state

    alembic_upgrade_head(str(pg_engine.url))
    assert target_has_app_state(pg_engine) is False

    with pg_engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, email, name) VALUES ('u1', 'a@x', 'A')"))
    assert target_has_app_state(pg_engine) is True


def test_verify_row_counts_match(tmp_path, pg_engine):
    """After copy, source and target row counts match."""
    import duckdb, sqlalchemy as sa
    from src.db import _ensure_schema
    from scripts.db_state_migrator import (
        alembic_upgrade_head,
        copy_duckdb_to_pg,
        verify_row_counts,
    )

    duck_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.execute("INSERT INTO users (id, email, name) VALUES ('u1', 'a@x', 'A'), ('u2', 'b@x', 'B')")
    conn.close()

    alembic_upgrade_head(str(pg_engine.url))
    copy_duckdb_to_pg(duck_path, str(pg_engine.url))

    diffs = verify_row_counts(duck_path, str(pg_engine.url))
    # Empty diffs = all tables match
    assert diffs == [], f"Row count diffs: {diffs}"


def test_verify_row_counts_detects_mismatch(tmp_path, pg_engine):
    """When PG missing rows, verify returns table-level diff."""
    import duckdb, sqlalchemy as sa
    from src.db import _ensure_schema
    from scripts.db_state_migrator import alembic_upgrade_head, verify_row_counts

    duck_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.execute("INSERT INTO users (id, email, name) VALUES ('u1', 'a@x', 'A')")
    conn.close()

    alembic_upgrade_head(str(pg_engine.url))
    # Skip copy — leave PG empty

    diffs = verify_row_counts(duck_path, str(pg_engine.url))
    user_diff = next(d for d in diffs if d["table"] == "users")
    assert user_diff["source_rows"] == 1
    assert user_diff["target_rows"] == 0


def test_main_duckdb_to_side_car_end_to_end(tmp_path, pg_engine, monkeypatch):
    """End-to-end: main(--to=side_car) drives all steps + writes success."""
    import json
    import duckdb
    from src.db import _ensure_schema

    duck_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.execute("INSERT INTO users (id, email, name) VALUES ('u1', 'a@x', 'A')")
    conn.close()

    jobs_dir = tmp_path / "db-jobs"
    backups_dir = tmp_path / "backups"
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    from src.db_state_machine import BackendState, write_backend_state, read_backend_state
    write_backend_state(BackendState.SIDE_CAR_IN_PROGRESS)

    from scripts.db_state_migrator import main
    rc = main(
        job_id="job-test-1",
        to="side_car",
        target_url=str(pg_engine.url),
        duckdb_path=duck_path,
        jobs_dir=jobs_dir,
        backups_dir=backups_dir,
    )
    assert rc == 0

    job = json.loads((jobs_dir / "job-test-1.json").read_text())
    assert job["status"] == "success"
    assert job["summary"]["tables_migrated"] > 0

    # State machine flipped to stable side_car
    state, url = read_backend_state()
    assert state == BackendState.SIDE_CAR
    assert url == str(pg_engine.url)

    # A completed DuckDB-source migration records completion next to the
    # source so the compose data-migrate one-shot stops re-copying the now
    # frozen snapshot (re-copying resurrects rows deleted from PG since).
    from scripts.migrate_duckdb_to_pg.marker import read_completion_marker

    marker = read_completion_marker(duck_path)
    assert marker is not None
    assert marker["source"] == "db_state_migrator"


def test_copy_pg_to_pg_idempotent_same_url(tmp_path, pg_engine):
    """copy_pg_to_pg(url, url) — copying the schema onto itself is a no-op.

    Smoke test guarding the side_car → cloud path; the row-handling
    logic (JSON cast, ARRAY coerce, NOT NULL default sub) is shared
    with the DuckDB path, so the dedicated tests on that path cover
    the per-column edge cases. Real cross-host PG→PG verification
    happens live on agnes-dev with a Cloud SQL target.
    """
    import sqlalchemy as sa
    from src.db_pg import Base
    from scripts.db_state_migrator import copy_pg_to_pg, verify_pg_row_counts

    # Create the empty schema on the test PG.
    Base.metadata.create_all(pg_engine)
    url = str(pg_engine.url)

    # Seed a row in source so we have something non-trivial to copy.
    with pg_engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO users (id, email, name) VALUES ('u1', 'a@x', 'A')"
        ))

    summary = copy_pg_to_pg(url, url)
    assert summary["tables_migrated"] > 0
    # Idempotent — re-running yields the same row count.
    summary2 = copy_pg_to_pg(url, url)
    assert summary2["rows_total"] == summary["rows_total"]

    # Verification reports no diffs.
    diffs = verify_pg_row_counts(url, url)
    assert diffs == []


def test_main_to_cloud_requires_source_url(tmp_path, monkeypatch):
    """main(--to=cloud) without source_url raises ValueError.

    The applier passes --source-url explicitly; CLI fallback reads
    instance.yaml. If neither is set, fail loud rather than silently
    re-migrate from DuckDB (the v6 footgun we fixed in v7).
    """
    from scripts.db_state_migrator import main

    # Isolate the state-machine overlay path so the test doesn't read
    # or write to the host-default /data/state/instance.yaml.
    monkeypatch.setattr(
        "src.db_state_machine._OVERLAY_PATH",
        tmp_path / "instance.yaml",
    )

    rc = main(
        job_id="job-cloud-1",
        to="cloud",
        target_url="postgresql+psycopg://x:y@z/q",
        duckdb_path=tmp_path / "system.duckdb",
        jobs_dir=tmp_path / "db-jobs",
        backups_dir=tmp_path / "backups",
        source_url=None,
        source_backend="side_car",
    )
    # main() catches the exception and writes failed status; rc=1.
    assert rc == 1
    import json
    job = json.loads((tmp_path / "db-jobs" / "job-cloud-1.json").read_text())
    assert job["status"] == "failed"
    assert "source-url" in job["error"]["message"].lower()


def test_verify_raises_on_missing_target_table(tmp_path, pg_engine):
    """If a target table is missing (e.g. partial alembic apply),
    verify_row_counts must raise — not return ``tgt_count = 0`` and
    silently match an empty source. Hides typos AND partial schemas."""
    import duckdb
    import pytest
    import src.models  # noqa: F401 — registers all ORM models onto Base.metadata
    from sqlalchemy import text as sa_text
    from src.db import _ensure_schema
    from src.db_pg import Base
    from scripts.db_state_migrator import verify_row_counts

    duck_path = tmp_path / "src.duckdb"
    duck = duckdb.connect(str(duck_path))
    _ensure_schema(duck)
    duck.close()

    Base.metadata.create_all(pg_engine)
    with pg_engine.begin() as conn:
        conn.execute(sa_text("DROP TABLE users CASCADE"))

    with pytest.raises(RuntimeError, match="target table.*missing"):
        verify_row_counts(duck_path, str(pg_engine.url))


def test_verify_pg_raises_on_missing_target_table(tmp_path, pg_engine):
    """Same contract for the PG -> PG verify variant (used on
    side_car -> cloud and cloud -> side_car transitions)."""
    import pytest
    import src.models  # noqa: F401 — registers all ORM models onto Base.metadata
    from sqlalchemy import text as sa_text
    from src.db_pg import Base
    from scripts.db_state_migrator import verify_pg_row_counts

    Base.metadata.create_all(pg_engine)
    with pg_engine.begin() as conn:
        conn.execute(sa_text("DROP TABLE users CASCADE"))

    # Same URL on both sides — the test exercises the PROGRAMMING
    # error code path (table missing on the target side). The fact
    # that source side ALSO has the table missing is irrelevant; the
    # missing-target raise must fire regardless.
    with pytest.raises(RuntimeError, match="(target|source) table.*missing"):
        verify_pg_row_counts(str(pg_engine.url), str(pg_engine.url))


def test_main_writes_duckdb_backup_before_copy(tmp_path, pg_engine, monkeypatch):
    """The DuckDB backup must exist on disk BEFORE the data_copy step
    overwrites any PG state. The previous flow copied first, verified,
    then backed up — so a crash between verify and flip left the
    operator with neither a backup nor a flipped state."""
    import duckdb
    from src.db import _ensure_schema
    from scripts.db_state_migrator import main

    duck_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.close()

    jobs_dir = tmp_path / "db-jobs"
    backups_dir = tmp_path / "backups"
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)

    # Force a failure AFTER the backup step by patching copy_duckdb_to_pg
    # to raise. If the backup was written before the failure, the file
    # exists on disk.
    def boom(*a, **kw):
        raise RuntimeError("simulated mid-copy crash")
    monkeypatch.setattr("scripts.db_state_migrator.copy_duckdb_to_pg", boom)

    rc = main(
        job_id="job-backup-order",
        to="side_car",
        target_url=str(pg_engine.url),
        duckdb_path=duck_path,
        jobs_dir=jobs_dir,
        backups_dir=backups_dir,
    )
    assert rc == 1
    backups = list(backups_dir.glob("duckdb-pre-sidecar-*.duckdb.gz"))
    assert backups, "backup file should exist even though copy failed"


def test_cancel_sentinel_during_data_copy_step(tmp_path, pg_engine, monkeypatch):
    """Phase 7.4 — sentinel arrives mid-migration (after alembic has run,
    during the data_copy call).  The migrator must observe the sentinel at
    the next step boundary (verify), mark_cancelled, and return 0.

    The existing cancel test in tests/test_db_state_migrator.py pre-creates
    the sentinel BEFORE main() runs (boundary 0).  This test complements it
    by exercising the late-stage boundary check that fires AFTER copy returns.
    """
    import json
    import duckdb
    from src.db import _ensure_schema
    from scripts.db_state_migrator import main, copy_duckdb_to_pg

    duck_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.close()

    jobs_dir = tmp_path / "db-jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    backups_dir = tmp_path / "backups"
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)

    job_id = "job-cancel-mid-copy"
    orig_copy = copy_duckdb_to_pg

    def copy_then_drop_sentinel(duck_path, target_url, **kwargs):
        # Accept the optional writer= kwarg added by C.1 — main()
        # forwards it so per-table progress lands in the JobWriter.
        result = orig_copy(duck_path, target_url, **kwargs)
        # Sentinel arrives after copy finishes — the next boundary check
        # (verify) must trip it.
        (jobs_dir / f"{job_id}.cancel").touch()
        return result

    monkeypatch.setattr(
        "scripts.db_state_migrator.copy_duckdb_to_pg",
        copy_then_drop_sentinel,
    )

    rc = main(
        job_id=job_id,
        to="side_car",
        target_url=str(pg_engine.url),
        duckdb_path=duck_path,
        jobs_dir=jobs_dir,
        backups_dir=backups_dir,
    )
    assert rc == 0, "cancellation is not a process failure"

    job = json.loads((jobs_dir / f"{job_id}.json").read_text())
    assert job["status"] == "cancelled", f"unexpected status: {job['status']}"
    # The cancel must be observed at the verify boundary (first check after
    # copy returns), NOT inside data_copy itself.
    assert job["error"]["step"] == "verify", (
        f"expected cancel at verify step, got: {job['error']['step']}"
    )


def test_bounded_engine_fails_fast_on_unreachable(tmp_path):
    """A bogus host must error within ~connect_timeout, not hang.
    The test asserts the engine raises within 15s end-to-end —
    plenty of headroom over the 10s connect_timeout."""
    import time, pytest, sqlalchemy as sa
    from scripts.db_state_migrator import _bounded_engine
    eng = _bounded_engine("postgresql+psycopg://x:y@10.255.255.1:5432/nope")
    t0 = time.monotonic()
    with pytest.raises((sa.exc.OperationalError, sa.exc.DBAPIError)):
        with eng.connect() as c:
            c.execute(sa.text("SELECT 1"))
    elapsed = time.monotonic() - t0
    assert elapsed < 15, f"connect_timeout did not fire within 15s, took {elapsed:.1f}s"


# ---------------------------------------------------------------------------
# Phase 7.5 — Hung-migrator statement_timeout contract
# ---------------------------------------------------------------------------

def test_bounded_engine_carries_statement_timeout(pg_engine):
    """Phase 7.5 — _bounded_engine must set PG-side statement_timeout
    to 5 minutes (300_000 ms). This bounds any single query so a
    hung migrator subprocess cannot sit forever on a runaway query;
    the unattended applier needs it to surface a clear error within
    a known horizon. Without this guard, a misconfigured target
    (broken index, deadlock with another client) could block the
    migrator indefinitely.

    This test exercises the runtime behaviour: the PG server
    actually honours the setting when a connection is established
    via _bounded_engine.
    """
    import sqlalchemy as sa
    from scripts.db_state_migrator import _bounded_engine

    eng = _bounded_engine(str(pg_engine.url))
    with eng.connect() as conn:
        row = conn.execute(sa.text("SHOW statement_timeout")).fetchone()
    eng.dispose()

    raw = row[0]

    def parse_ms(v: str) -> int:
        """Normalise PG's SHOW statement_timeout output to integer ms.

        PG may return the value in several formats depending on version and
        unit: "300000ms", "300s", "5min", or bare digits (treated as ms
        by PG's SHOW output — unlike SET which treats bare digits as ms
        only in some contexts). We normalise all to integer ms so the
        assertion is unit-independent.
        """
        v = v.strip()
        if v.endswith("ms"):
            return int(v[:-2])
        if v.endswith("min"):
            return int(v[:-3]) * 60_000
        if v.endswith("s"):
            return int(v[:-1]) * 1_000
        # bare integer — PG SHOW output for this GUC uses ms
        return int(v)

    assert parse_ms(raw) == 300_000, (
        f"statement_timeout {raw!r} != 5 min (300_000 ms) — "
        "_bounded_engine is not enforcing the query time-cap"
    )


def test_bounded_engine_connect_args_carry_statement_timeout():
    """Phase 7.5 — static contract: the literal option string
    ``-c statement_timeout=300000`` must be present in
    ``_bounded_engine``'s source.

    This is a pure static guard: it fails immediately on accidental
    deletion of the option, without needing a PG server. The runtime
    counterpart (``test_bounded_engine_carries_statement_timeout``)
    proves PG actually honours the setting; this test catches
    accidental removal before it can reach CI.
    """
    import inspect
    from scripts.db_state_migrator import _bounded_engine

    src = inspect.getsource(_bounded_engine)
    assert "statement_timeout=300000" in src, (
        "5-minute statement_timeout (300_000 ms) is missing from "
        "_bounded_engine — the hung-migrator guard has been removed"
    )


# ---------------------------------------------------------------------------
# Phase 7.2 — DuckDB → CLOUD direct end-to-end
# ---------------------------------------------------------------------------

def test_main_duckdb_to_cloud_end_to_end(tmp_path, pg_engine, monkeypatch):
    """Phase 7.2 — direct DuckDB → CLOUD path.

    Unlike duckdb→side_car (which runs backup_duckdb pre-copy), the
    direct-cloud path SKIPS backup_duckdb because the operator went straight
    to managed PG. This test locks in that distinction:

      - rc=0 and status=success
      - state machine flipped to CLOUD with the target URL stored
      - user row present in PG
      - NO duckdb backup file produced (the duckdb→side_car backup sentinel
        ``duckdb-pre-sidecar-*.duckdb.gz`` must not appear)
    """
    import json
    import duckdb
    import sqlalchemy as sa
    from src.db import _ensure_schema
    from scripts.db_state_migrator import main

    duck_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.execute("INSERT INTO users (id, email, name) VALUES ('u1', 'a@x', 'A')")
    conn.close()

    jobs_dir = tmp_path / "db-jobs"
    backups_dir = tmp_path / "backups"
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)

    from src.db_state_machine import BackendState, write_backend_state, read_backend_state
    write_backend_state(BackendState.CLOUD_IN_PROGRESS)

    rc = main(
        job_id="job-cloud-direct",
        to="cloud",
        source_backend="duckdb",
        target_url=str(pg_engine.url),
        duckdb_path=duck_path,
        jobs_dir=jobs_dir,
        backups_dir=backups_dir,
    )
    assert rc == 0, "duckdb→cloud direct migration must complete with rc=0"

    job = json.loads((jobs_dir / "job-cloud-direct.json").read_text())
    assert job["status"] == "success", f"unexpected status: {job}"

    # State flipped to CLOUD with the target URL.
    state, url = read_backend_state()
    assert state == BackendState.CLOUD
    assert url == str(pg_engine.url)

    # Data copied across.
    with pg_engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT email FROM users WHERE id = :id"), {"id": "u1"}
        ).fetchone()
    assert row is not None
    assert row[0] == "a@x"

    # Contract guard: direct-cloud must NOT have produced a duckdb backup.
    # The duckdb→side_car path produces a ``duckdb-pre-sidecar-*.duckdb.gz``
    # file; duckdb→cloud does not (no backup_duckdb call in that branch).
    backups = list(backups_dir.glob("*.duckdb.gz")) if backups_dir.exists() else []
    assert backups == [], f"direct-cloud must skip duckdb backup, found {backups}"


# ---------------------------------------------------------------------------
# Phase 7.3 — CLOUD → SIDE_CAR DR rollback (smoke variant)
# ---------------------------------------------------------------------------

def test_main_cloud_to_side_car_dr_rollback_smoke(tmp_path, pg_engine, monkeypatch):
    """Phase 7.3 — CLOUD → SIDE_CAR DR rollback.

    The realistic test requires two PG instances (source + target on different
    hosts). Without a second pgserver fixture we run a smoke variant: the same
    engine URL is used for both source and target, relying on copy_pg_to_pg's
    idempotency (which is already covered by
    ``test_copy_pg_to_pg_idempotent_same_url``). This exercises the state-machine
    dispatch, flow control, verify step, and flip end-to-end.

    Limitation documented: the full cross-host case requires a second PG
    server and is validated by live testing on agnes-dev with a Cloud SQL
    source. This test guards the state-machine logic and API contract only.
    """
    import json
    import sqlalchemy as sa
    from src.db_pg import Base
    from scripts.db_state_migrator import main, alembic_upgrade_head

    # Set up: source PG fully migrated + has a row, treated as 'cloud'.
    alembic_upgrade_head(str(pg_engine.url))
    with pg_engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO users (id, email, name) VALUES ('u-dr', 'dr@example.com', 'DR')"
        ))

    jobs_dir = tmp_path / "db-jobs"
    backups_dir = tmp_path / "backups"
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)

    from src.db_state_machine import BackendState, write_backend_state, read_backend_state
    # The API endpoint writes SIDE_CAR_IN_PROGRESS before spawning the migrator.
    write_backend_state(BackendState.SIDE_CAR_IN_PROGRESS, url=str(pg_engine.url))

    # Source URL == target URL (smoke variant). Real DR would have
    # source = cloud SQL, target = sidecar PG container.
    rc = main(
        job_id="job-cloud-to-sidecar",
        to="side_car",
        source_backend="cloud",
        source_url=str(pg_engine.url),
        target_url=str(pg_engine.url),
        duckdb_path=tmp_path / "unused.duckdb",
        jobs_dir=jobs_dir,
        backups_dir=backups_dir,
    )
    assert rc == 0, "cloud→side_car PG→PG migration must complete with rc=0"

    job = json.loads((jobs_dir / "job-cloud-to-sidecar.json").read_text())
    assert job["status"] == "success", f"unexpected status: {job}"

    state, url = read_backend_state()
    assert state == BackendState.SIDE_CAR
    assert url == str(pg_engine.url)

    # User row still present (idempotent copy).
    with pg_engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT email FROM users WHERE id = :id"), {"id": "u-dr"}
        ).fetchone()
    assert row is not None
    assert row[0] == "dr@example.com"


def test_verify_row_counts_opens_duckdb_read_only(tmp_path, pg_engine):
    """E.1 — verify_row_counts must open the DuckDB file read-only;
    a writable open creates a .wal sidecar that adds clutter and can
    confuse subsequent reads if the migrator crashes between verify
    and flip.
    """
    import duckdb
    import src.models  # noqa: F401
    from src.db import _ensure_schema
    from src.db_pg import Base

    from scripts.db_state_migrator import alembic_upgrade_head, verify_row_counts

    duck_path = tmp_path / "src.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.close()

    alembic_upgrade_head(str(pg_engine.url))
    # No pre-existing .wal in the tmp dir.
    wal_path = duck_path.with_suffix(".duckdb.wal")
    if wal_path.exists():
        wal_path.unlink()

    verify_row_counts(duck_path, str(pg_engine.url))

    # Read-only opens do NOT create a .wal sidecar.
    assert not wal_path.exists(), (
        "verify_row_counts must open DuckDB read-only; .wal file appeared"
    )


def test_migrator_subprocess_watchdog_fires_end_to_end(tmp_path):
    """D.3 — End-to-end watchdog: launch the migrator CLI as a real
    subprocess that hangs inside its alembic step; the outer
    ``subprocess.run(timeout=...)`` must terminate it within the
    configured horizon.

    The shell-side equivalent is covered by the applier integration
    test under tests/test_state_applier_host_script.sh (C.2). This
    python-side complement exercises the migrator's own
    subprocess-bound-by-timeout contract from the consumer side: any
    caller wrapping the migrator can rely on a hard wall-clock bound.
    """
    import subprocess
    import sys
    import textwrap
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    shim = tmp_path / "hung_migrator.py"
    shim.write_text(
        textwrap.dedent(
            f"""
            import sys, time, pathlib
            sys.path.insert(0, {str(repo_root)!r})
            import scripts.db_state_migrator as m
            def hang(*a, **kw):
                time.sleep(120)
            m.alembic_upgrade_head = hang
            sys.exit(m.main(
                job_id="hung-d3",
                to="side_car",
                target_url="postgresql+psycopg://x:y@127.0.0.1:1/q",
                duckdb_path=pathlib.Path("/tmp/unused.duckdb"),
                jobs_dir=pathlib.Path({str(tmp_path / "jobs")!r}),
                backups_dir=pathlib.Path({str(tmp_path / "backups")!r}),
                source_url=None,
                source_backend="duckdb",
            ))
            """
        )
    )

    import pytest

    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run(
            [sys.executable, str(shim)],
            timeout=2,
            check=False,
            capture_output=True,
            cwd=str(repo_root),
        )


def test_copy_pg_to_pg_preserves_array_and_jsonb_round_trip(tmp_path, pg_engine):
    """D.2 — PG→PG copy must round-trip both PG ARRAY columns and
    JSONB dict payloads. Round-1 task 7.3's DR smoke seeded only
    users(id, email, name); the v9 JSONB CAST fix and the
    array-coerce helpers only fire when those types are actually
    exercised — a regression hole closed here.
    """
    import json

    import sqlalchemy as sa
    import src.models  # noqa: F401 — registers ORM models onto Base.metadata
    from src.db_pg import Base

    from scripts.db_state_migrator import alembic_upgrade_head, copy_pg_to_pg

    alembic_upgrade_head(str(pg_engine.url))

    audit_params = {
        "user": "alice",
        "scope": ["a", "b", "c"],
        "nested": {"key": "value", "num": 42},
    }
    metric_dimensions = ["country", "device", "channel"]
    metric_synonyms = ["mrr", "monthly_recurring_revenue"]

    with pg_engine.begin() as conn:
        # JSONB audit row — params is a dict.
        conn.execute(
            sa.text(
                "INSERT INTO audit_log (id, action, params, timestamp) "
                "VALUES (:id, :a, CAST(:p AS JSONB), CURRENT_TIMESTAMP)"
            ),
            {"id": "audit-d2", "a": "test", "p": json.dumps(audit_params)},
        )
        # ARRAY metric row — dimensions/synonyms are PG arrays.
        conn.execute(
            sa.text(
                "INSERT INTO metric_definitions "
                "(id, name, display_name, category, description, type, unit, "
                " grain, table_name, tables, expression, time_column, "
                " dimensions, synonyms, sql, source) "
                "VALUES (:id, :n, :dn, :c, :d, :t, :u, :g, :tn, :tb, :e, :tc, "
                "        :dm, :sy, :s, :src)"
            ),
            {
                "id": "metric-d2", "n": "mrr", "dn": "MRR", "c": "finance",
                "d": "monthly recurring revenue", "t": "sum", "u": "USD",
                "g": "monthly", "tn": "facts.subs", "tb": ["facts.subs"],
                "e": "sum(mrr)", "tc": "ts",
                "dm": metric_dimensions, "sy": metric_synonyms,
                "s": "SELECT 1", "src": "manual",
            },
        )

    # Copy onto the same URL (smoke variant; full cross-host case is
    # exercised live). ON CONFLICT DO NOTHING means the existing rows
    # survive and we then read them back via the regular schema.
    summary = copy_pg_to_pg(str(pg_engine.url), str(pg_engine.url))
    assert summary["tables_migrated"] > 0

    with pg_engine.connect() as conn:
        audit_row = conn.execute(
            sa.text("SELECT params FROM audit_log WHERE id = :id"),
            {"id": "audit-d2"},
        ).fetchone()
        metric_row = conn.execute(
            sa.text(
                "SELECT dimensions, synonyms FROM metric_definitions WHERE id = :id"
            ),
            {"id": "metric-d2"},
        ).fetchone()

    # JSONB dict round-trips intact.
    assert audit_row is not None
    params = audit_row[0]
    if isinstance(params, str):
        params = json.loads(params)
    assert params == audit_params, params

    # PG ARRAYs round-trip as Python lists, NOT as escaped strings.
    assert metric_row is not None
    assert list(metric_row[0]) == metric_dimensions, metric_row[0]
    assert list(metric_row[1]) == metric_synonyms, metric_row[1]


# ---------------------------------------------------------------------------
# Phase 7.6 — Host-reboot recovery (unit-level, no bash harness)
# ---------------------------------------------------------------------------

def test_stuck_running_recovery_via_stale_heartbeat(tmp_path):
    """Phase 7.6 — host-reboot simulation.

    JobWriter wrote 'running' + heartbeat at T0; host reboots between T0
    and T+200s; alive file mtime is left at T0. The applier's recovery loop
    (scripts/ops/agnes-state-applier.sh) inspects alive mtime and marks the
    job failed when age >120s.

    We don't invoke the bash applier here — that's covered by the shell
    test suite. This test locks in the UNIT-level file contract between
    JobWriter and the applier: files have the right names, JSON has the
    right shape, and the stale-heartbeat predicate (age > 120s) is
    accurately detectable from the on-disk artifacts.

    The test also emulates the applier's recovery write to confirm the
    resulting JSON is what the API endpoint's status reader expects.
    """
    import os, time, json
    from scripts.db_state_migrator import JobWriter

    job_id = "stuck-running-job"
    w = JobWriter(
        job_id=job_id,
        jobs_dir=tmp_path,
        source="duckdb",
        target="side_car",
    )
    w.write_initial()
    w.update_step("data_copy", progress_pct=40)

    # Verify the alive sentinel was produced by write_initial / update_step.
    alive_path = tmp_path / f"{job_id}.alive"
    assert alive_path.exists(), "JobWriter must produce alive sentinel file"

    # Backdate the alive file to 200s ago to simulate the host having
    # rebooted while the migrator was mid-copy.
    two_hundred_s_ago = time.time() - 200
    os.utime(alive_path, (two_hundred_s_ago, two_hundred_s_ago))

    # Confirm the on-disk state matches what the applier looks for:
    # status=running in the JSON, alive mtime stale by >120s.
    job = json.loads((tmp_path / f"{job_id}.json").read_text())
    assert job["status"] == "running", "job must be running before recovery"
    age_s = time.time() - alive_path.stat().st_mtime
    assert age_s > 120, f"backdating failed — age only {age_s:.1f}s"

    # Emulate the applier's recovery write — the same predicate the
    # bash loop uses, ported to Python for test isolation.  This
    # validates that *if* the applier runs this logic, the JSON ends up
    # in the shape the API status reader expects.
    if job.get("status") == "running" and age_s > 120:
        job["status"] = "failed"
        job["error"] = {
            "step": job.get("current_step", "unknown"),
            "class": "StuckRunning",
            "message": f"stuck running (no heartbeat for {int(age_s)}s)",
        }
        (tmp_path / f"{job_id}.json").write_text(json.dumps(job, indent=2))

    # Verify the recovery wrote the expected shape.
    recovered = json.loads((tmp_path / f"{job_id}.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["error"]["class"] == "StuckRunning"
    assert "stuck running" in recovered["error"]["message"]
    # The step recorded in the error must be the step that was active
    # when the host rebooted.
    assert recovered["error"]["step"] == "data_copy"


def test_alembic_upgrade_head_raises_on_timeout(tmp_path, monkeypatch):
    """H5 — hung alembic must surface as a clean RuntimeError, not pin
    the migrator forever. ``subprocess.run`` MUST be invoked with a
    ``timeout=`` kwarg; ``TimeoutExpired`` is translated to a typed
    error message so :class:`JobWriter.mark_failed` carries an
    actionable string."""
    import subprocess

    import pytest

    from scripts.db_state_migrator import alembic_upgrade_head

    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr("subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match=r"alembic.*timed out"):
        alembic_upgrade_head("postgresql+psycopg://x:y@z/q")

    # Sanity: a timeout was actually passed (not unbounded).
    assert captured["timeout"] is not None and captured["timeout"] > 0


def test_backup_duckdb_raises_on_timeout(tmp_path, monkeypatch):
    """H5 — hung gzip during backup must surface, not pin."""
    import subprocess

    import duckdb
    import pytest

    from scripts.db_state_migrator import backup_duckdb
    from src.db import _ensure_schema

    duck = tmp_path / "src.duckdb"
    conn = duckdb.connect(str(duck))
    _ensure_schema(conn)
    conn.close()

    # backup_duckdb uses gzip + shutil.copyfileobj internally. The fix
    # must wrap that in a subprocess.run('gzip', ..., timeout=...) so
    # the hang has a watchdog. Pre-fix the function never invokes
    # subprocess.run; post-fix it does, and the timeout surfaces as a
    # typed RuntimeError.
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr("subprocess.run", fake_run)

    backups = tmp_path / "backups"
    with pytest.raises(RuntimeError, match=r"backup.*timed out"):
        backup_duckdb(duck, backups)
    assert captured["timeout"] is not None and captured["timeout"] > 0


def test_backup_sidecar_pg_raises_on_timeout(tmp_path, monkeypatch):
    """H5 — hung pg_dump must surface, not pin."""
    import subprocess

    import pytest

    from scripts.db_state_migrator import backup_sidecar_pg

    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr("subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match=r"pg_dump.*timed out"):
        backup_sidecar_pg("agnes-postgres-1", tmp_path / "backups")
    assert captured["timeout"] is not None and captured["timeout"] > 0


def test_copy_duckdb_to_pg_emits_table_progress(tmp_path, pg_engine):
    """C.1 — JobWriter.update_table_progress must fire from copy_*
    so the UI gets per-table % during data_copy. Pre-fix the field
    was defined on JobWriter but never written, so progress_pct
    froze at 40% for the entire copy step.
    """
    import json

    import duckdb

    from src.db import _ensure_schema

    from scripts.db_state_migrator import (
        JobWriter,
        alembic_upgrade_head,
        copy_duckdb_to_pg,
    )

    duck_path = tmp_path / "src.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.close()

    alembic_upgrade_head(str(pg_engine.url))

    writer = JobWriter(
        job_id="job-progress",
        jobs_dir=tmp_path / "jobs",
        source="duckdb",
        target="side_car",
    )
    writer.write_initial()

    copy_duckdb_to_pg(duck_path, str(pg_engine.url), writer=writer)

    job = json.loads((tmp_path / "jobs" / "job-progress.json").read_text())
    tp = job.get("table_progress")
    assert tp is not None, "update_table_progress was never called"
    assert tp["tables_total"] > 0
    # After the copy finishes the most recent callback corresponds to
    # the last table — done == total.
    assert tp["tables_done"] == tp["tables_total"], tp
    # progress_pct is the 40-80% range mapped from done/total — at
    # completion of data_copy it sits at 80%.
    assert job["progress_pct"] >= 40 and job["progress_pct"] <= 80, job["progress_pct"]


def test_side_car_to_cloud_backup_failure_is_hard_fail(tmp_path, pg_engine, monkeypatch):
    """B.4 — side_car → cloud must HARD-FAIL when backup_sidecar_pg
    raises. Pre-fix the exception was swallowed as a warning and the
    UI showed 'success' while the operator was missing a recovery
    point at restore time.

    Post-fix: backup_sidecar_pg exception -> mark_failed(class=
    BackupError) + return 1. The operator must explicitly retry after
    fixing the backup path.
    """
    import json

    from scripts.db_state_migrator import main

    # Force backup_sidecar_pg to raise.
    def boom(*a, **kw):
        raise RuntimeError("pg_dump connection refused")

    monkeypatch.setattr(
        "scripts.db_state_migrator.backup_sidecar_pg", boom
    )
    # Isolate state-machine overlay so the test doesn't touch /data.
    monkeypatch.setattr(
        "src.db_state_machine._OVERLAY_PATH",
        tmp_path / "instance.yaml",
    )

    rc = main(
        job_id="job-b4",
        to="cloud",
        target_url=str(pg_engine.url),
        duckdb_path=tmp_path / "src.duckdb",  # unused for side_car source
        jobs_dir=tmp_path / "db-jobs",
        backups_dir=tmp_path / "backups",
        source_url=str(pg_engine.url),
        source_backend="side_car",
    )
    assert rc == 1, "side_car→cloud backup failure must return 1"

    job = json.loads((tmp_path / "db-jobs" / "job-b4.json").read_text())
    assert job["status"] == "failed"
    assert job["error"]["class"] == "BackupError"
    assert "side-car backup failed" in job["error"]["message"].lower()


def test_pre_copy_scrubs_audit_log_pii(tmp_path, pg_engine):
    """H7 — historical audit_log rows with password/token/api_key in
    params must be scrubbed BEFORE copy so neither the migrated PG
    nor the DuckDB backup retain the secret.

    Pre-fix: ``_sanitize_for_audit`` only ran at WRITE time. Rows
    captured before that sanitiser existed carried raw credentials
    in ``params`` / ``params_before``; the migrator copied them
    verbatim to PG.

    Post-fix: ``scrub_audit_log_pii`` runs at the start of
    ``copy_duckdb_to_pg`` and rewrites offending rows in place in
    the DuckDB source. Clean rows survive verbatim. Idempotent:
    re-running the migration finds no further matches.
    """
    import json

    import duckdb
    import sqlalchemy as sa
    import src.models  # noqa: F401 — registers ORM models onto Base.metadata
    from src.db import _ensure_schema
    from src.db_pg import Base

    from scripts.db_state_migrator import alembic_upgrade_head, copy_duckdb_to_pg

    duck_path = tmp_path / "src.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)

    # Three seed rows:
    #   a1 — params contains a password (must scrub)
    #   a2 — params is clean (must survive verbatim)
    #   a3 — params contains an api_key (must scrub)
    #   a4 — params_before contains a bearer token (must scrub)
    conn.execute(
        """INSERT INTO audit_log (id, action, params, params_before, timestamp) VALUES
            ('a1', 'login',        ?, NULL, current_timestamp),
            ('a2', 'view',         ?, NULL, current_timestamp),
            ('a3', 'config_change',?, NULL, current_timestamp),
            ('a4', 'token_rotate', ?, ?,    current_timestamp)""",
        [
            json.dumps({"password": "secret123", "user": "alice"}),
            json.dumps({"page": "/dashboard"}),
            json.dumps({"name": "Bob", "api_key": "sk-deadbeef"}),
            json.dumps({"event": "rotate"}),
            json.dumps({"bearer": "eyJhbGciOiJIUzI1NiJ9.payload.sig"}),
        ],
    )
    conn.close()

    alembic_upgrade_head(str(pg_engine.url))
    copy_duckdb_to_pg(duck_path, str(pg_engine.url))

    # Source DuckDB is now scrubbed in place — the backup taken from
    # this file will not leak the secret either.
    duck_ro = duckdb.connect(str(duck_path), read_only=True)
    rows = duck_ro.execute(
        "SELECT id, params, params_before FROM audit_log ORDER BY id"
    ).fetchall()
    duck_ro.close()
    by_id = {rid: (p, pb) for rid, p, pb in rows}

    def _payload(s):
        if s is None:
            return None
        return json.loads(s) if isinstance(s, str) else s

    assert "secret123" not in str(by_id["a1"]).lower()
    assert _payload(by_id["a2"][0]) == {"page": "/dashboard"}, "clean rows untouched"
    assert "sk-deadbeef" not in str(by_id["a3"]).lower()
    assert "eyJhbGciOiJIUzI1NiJ9" not in str(by_id["a4"]).lower()

    # Target PG mirrors the scrubbed source.
    with pg_engine.connect() as c:
        pg_rows = c.execute(
            sa.text("SELECT id, params, params_before FROM audit_log ORDER BY id")
        ).fetchall()
    for rid, p, pb in pg_rows:
        blob = (str(p) + str(pb)).lower()
        if rid == "a2":
            continue
        assert "secret123" not in blob
        assert "sk-deadbeef" not in blob
        assert "eyjhbGciOiJIUzI1NiJ9".lower() not in blob


def test_main_side_car_backup_artifact_is_pii_scrubbed(
    tmp_path, pg_engine, monkeypatch
):
    """H7 — the pre-flip ``.duckdb.gz`` backup must NOT retain audit PII.

    ``main()`` scrubs the source before ``backup_duckdb`` on the side_car
    path, so a secret planted in ``audit_log`` is absent from the gunzipped
    backup artifact — not just from the migrated PG. Guards the ordering
    that ``test_pre_copy_scrubs_audit_log_pii`` cannot cover: it calls
    ``copy_duckdb_to_pg`` directly and so never exercises the backup step,
    which in ``main()`` runs *before* the copy's internal scrub."""
    import gzip
    import json
    import shutil

    import duckdb
    from src.db import _ensure_schema
    import scripts.db_state_migrator as m

    duck_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.execute("INSERT INTO users (id, email, name) VALUES ('u1', 'a@x', 'A')")
    conn.execute(
        "INSERT INTO audit_log (id, action, params, params_before, timestamp) "
        "VALUES ('a1', 'login', ?, NULL, current_timestamp)",
        [json.dumps({"password": "secret123", "user": "alice"})],
    )
    conn.close()

    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    from src.db_state_machine import BackendState, write_backend_state
    write_backend_state(BackendState.SIDE_CAR_IN_PROGRESS)

    backups_dir = tmp_path / "backups"
    rc = m.main(
        job_id="job-scrub-before-backup",
        to="side_car",
        target_url=str(pg_engine.url),
        duckdb_path=duck_path,
        jobs_dir=tmp_path / "db-jobs",
        backups_dir=backups_dir,
    )
    assert rc == 0

    backups = list(backups_dir.glob("*.duckdb.gz"))
    assert len(backups) == 1, f"expected exactly one backup, found {backups}"

    restored = tmp_path / "restored.duckdb"
    with gzip.open(backups[0], "rb") as fi, open(restored, "wb") as fo:
        shutil.copyfileobj(fi, fo)
    rconn = duckdb.connect(str(restored), read_only=True)
    blob = str(
        rconn.execute("SELECT params, params_before FROM audit_log").fetchall()
    ).lower()
    rconn.close()
    assert (
        "secret123" not in blob
    ), "backup artifact must not retain the scrubbed audit secret"


def test_scrub_audit_log_pii_missing_table_is_noop(tmp_path):
    """H7 — the scrub is schema-tolerant: a DuckDB without an ``audit_log``
    table returns zeros, not a ``NameError``. The ``except
    duckdb.CatalogException`` handler must be able to resolve the ``duckdb``
    name (it is imported inside the function); pre-fix, ``duckdb`` was
    undefined in scope, so a missing table raised NameError instead of the
    documented silent no-op."""
    import duckdb

    from scripts.db_state_migrator import scrub_audit_log_pii

    duck_path = tmp_path / "no-audit.duckdb"
    conn = duckdb.connect(str(duck_path))
    conn.execute("CREATE TABLE unrelated (x INTEGER)")  # valid db, no audit_log
    conn.close()

    assert scrub_audit_log_pii(duck_path) == {"rows_scanned": 0, "rows_redacted": 0}


def test_content_hash_sample_detects_non_pk_drift():
    """H12 — same PK set, different non-PK content must yield a
    different content hash.

    The pgserver test backend can only host one PG instance per
    process, so we can't seed two side-by-side PGs to exercise the
    full ``verify_pg_row_counts`` path. We test the building block
    instead: ``_content_hash_sample`` is deterministic on the row
    payload, so two engines whose tables disagree on a non-PK column
    yield different hashes. The integration assertion (verify reports
    a ``content_drift`` diff) is exercised indirectly by the
    same-URL idempotent test passing post-fix (a self-hash always
    matches itself).
    """
    import sqlalchemy as sa

    from scripts.db_state_migrator import _content_hash_sample

    eng_a = sa.create_engine("sqlite:///:memory:")
    eng_b = sa.create_engine("sqlite:///:memory:")
    for eng in (eng_a, eng_b):
        with eng.begin() as c:
            c.execute(sa.text("CREATE TABLE u (id TEXT PRIMARY KEY, email TEXT, name TEXT)"))

    # Same PK set ('u1', 'u2'); different non-PK values on row u1.
    with eng_a.begin() as c:
        c.execute(sa.text("INSERT INTO u VALUES ('u1', 'old@x.com', 'Alice')"))
        c.execute(sa.text("INSERT INTO u VALUES ('u2', 'b@x.com', 'Bob')"))
    with eng_b.begin() as c:
        c.execute(sa.text("INSERT INTO u VALUES ('u1', 'fresh@x.com', 'Alice')"))
        c.execute(sa.text("INSERT INTO u VALUES ('u2', 'b@x.com', 'Bob')"))

    h_a = _content_hash_sample(eng_a, "u", ["id"], ["email", "name"])
    h_b = _content_hash_sample(eng_b, "u", ["id"], ["email", "name"])

    assert h_a != h_b, (
        "Content-hash must detect non-PK drift; row counts alone would "
        "report 2==2 'rows match' and let the migration go live with "
        "stale corrupted rows in the target."
    )
    # Self-equality: hashing the same engine twice yields the same digest.
    assert h_a == _content_hash_sample(eng_a, "u", ["id"], ["email", "name"])
    # PK-only table (no non-PK cols) yields a constant sentinel.
    assert _content_hash_sample(eng_a, "u", ["id"], []) == "no-non-pk-content"


def test_verify_pg_row_counts_includes_content_drift_check(tmp_path, pg_engine):
    """H12 — verify_pg_row_counts must surface content-drift entries,
    not just row-count diffs.

    Same-engine constraint of pgserver forces this to be a structural
    test: we patch ``_content_hash_sample`` to return different
    hashes for source vs target on one chosen table and assert that
    the verify call surfaces a ``content_drift`` entry.
    """
    import sqlalchemy as sa
    import src.models  # noqa: F401
    from src.db_pg import Base

    import scripts.db_state_migrator as migrator

    Base.metadata.create_all(pg_engine)
    url = str(pg_engine.url)

    # Seed users so the row counts match on both sides (same URL).
    with pg_engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO users (id, email, name) VALUES ('u1', 'a@x', 'A')"
        ))

    original_hash = migrator._content_hash_sample
    call_count = {"n": 0}

    def fake_hash(engine, table_name, pk_cols, non_pk_cols, sample_size=1000):
        call_count["n"] += 1
        if table_name == "users":
            # Force source != target for the users table only.
            return f"hash-{call_count['n']}"
        return original_hash(engine, table_name, pk_cols, non_pk_cols, sample_size=sample_size)

    migrator._content_hash_sample = fake_hash
    try:
        diffs = migrator.verify_pg_row_counts(url, url)
    finally:
        migrator._content_hash_sample = original_hash

    content_drift = [d for d in diffs if d.get("kind") == "content_drift"]
    assert content_drift, (
        f"verify_pg_row_counts must produce a content_drift diff when "
        f"_content_hash_sample disagrees; got diffs={diffs}"
    )
    assert any(d["table"] == "users" for d in content_drift)


def test_copy_pg_to_pg_streams_without_full_materialize(tmp_path, pg_engine):
    """H4 — PG→PG copy must stream-batch rather than .all()-materialize.

    Seed 1500 rows; verify the per-table copy loop does NOT call
    ``Result.all()`` on the streaming SELECT (the load-into-RAM
    operation). We can't measure RAM directly, but patching
    ``sqlalchemy.engine.Result.all`` to count calls is a reliable
    sentinel: under the batched implementation the loop iterates the
    Result via ``yield_per`` and never touches ``.all()``; under the
    old implementation it calls ``.all()`` once per table.
    """
    import sqlalchemy as sa
    import src.models  # noqa: F401 — registers ORM models onto Base.metadata
    from src.db_pg import Base
    from scripts.db_state_migrator import copy_pg_to_pg

    Base.metadata.create_all(pg_engine)
    with pg_engine.begin() as conn:
        # Bulk insert via executemany — faster than 1500 separate executes.
        conn.execute(
            sa.text("INSERT INTO users (id, email, name) VALUES (:i, :e, :n)"),
            [
                {"i": f"u{i:04d}", "e": f"u{i}@x.com", "n": f"User {i}"}
                for i in range(1500)
            ],
        )

    original_all = sa.engine.Result.all
    calls = {"n": 0}

    def counting_all(self):
        calls["n"] += 1
        return original_all(self)

    sa.engine.Result.all = counting_all
    try:
        url = str(pg_engine.url)
        summary = copy_pg_to_pg(url, url)
    finally:
        sa.engine.Result.all = original_all

    assert summary["tables_migrated"] > 0
    # Under the batched implementation the per-table copy loop never
    # calls .all() on the source SELECT. A small budget allows for
    # unrelated SQLAlchemy bookkeeping (e.g. introspection helpers).
    assert calls["n"] <= 2, (
        f"copy_pg_to_pg still materialises via .all() — {calls['n']} calls. "
        "Switch the per-table copy SELECT to execution_options(yield_per=...) "
        "and iterate the Result instead of calling .all()."
    )


# ---------------------------------------------------------------------------
# B1 — retry into a non-empty target must not keep stale rows
# ---------------------------------------------------------------------------


def test_copy_duckdb_to_pg_retry_replaces_stale_rows(tmp_path, pg_engine):
    """B1 — a retry into a non-empty target rebuilds the target fresh.

    The copy uses bare ``ON CONFLICT DO NOTHING``. Without the pre-copy
    truncate, a row edited in the source between a failed first attempt and
    the retry would collide on its key, be skipped, and keep the stale
    value — invisibly to the COUNT(*)-only verify. The truncate makes the
    retry land every current value.
    """
    import duckdb
    import sqlalchemy as sa
    from src.db import _ensure_schema
    from scripts.db_state_migrator import alembic_upgrade_head, copy_duckdb_to_pg

    duck_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.execute("INSERT INTO users (id, email, name) VALUES ('u1', 'old@x', 'Old')")
    conn.close()

    alembic_upgrade_head(str(pg_engine.url))

    # First attempt copies the 'old' row.
    copy_duckdb_to_pg(duck_path, str(pg_engine.url))
    with pg_engine.connect() as c:
        assert c.execute(
            sa.text("SELECT email FROM users WHERE id = 'u1'")
        ).scalar() == "old@x"

    # The source is edited between attempts (user changed their email + name).
    conn = duckdb.connect(str(duck_path))
    conn.execute("UPDATE users SET email = 'new@x', name = 'New' WHERE id = 'u1'")
    conn.close()

    # Retry into the now-non-empty target.
    s2 = copy_duckdb_to_pg(duck_path, str(pg_engine.url))
    assert s2["target_rows_reset"] >= 1  # discarded the prior attempt's rows

    with pg_engine.connect() as c:
        row = c.execute(
            sa.text("SELECT email, name FROM users WHERE id = 'u1'")
        ).fetchone()
    # Without the truncate, ON CONFLICT DO NOTHING would keep ('old@x','Old').
    assert tuple(row) == ("new@x", "New")


def test_copy_duckdb_to_pg_reports_target_rows_reset(tmp_path, pg_engine):
    """The copy summary surfaces how many pre-existing rows were discarded."""
    import duckdb
    from src.db import _ensure_schema
    from scripts.db_state_migrator import alembic_upgrade_head, copy_duckdb_to_pg

    duck_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.close()

    alembic_upgrade_head(str(pg_engine.url))
    summary = copy_duckdb_to_pg(duck_path, str(pg_engine.url))
    assert "target_rows_reset" in summary
    assert isinstance(summary["target_rows_reset"], int)


# ---------------------------------------------------------------------------
# B2 — checkpoint the DuckDB WAL before backup + copy
# ---------------------------------------------------------------------------


def test_checkpoint_duckdb_runs_and_leaves_no_wal(tmp_path):
    """B2 — checkpoint_duckdb folds the WAL into the main file cleanly."""
    from pathlib import Path

    import duckdb
    from src.db import _ensure_schema
    from scripts.db_state_migrator import checkpoint_duckdb

    duck_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.execute("INSERT INTO users (id, email, name) VALUES ('u1', 'a@x', 'A')")
    conn.close()

    assert checkpoint_duckdb(duck_path) is True
    wal = Path(str(duck_path) + ".wal")
    assert not wal.exists() or wal.stat().st_size == 0


def test_checkpoint_duckdb_missing_file_is_noop(tmp_path):
    """A missing DuckDB path returns False, never raises."""
    from scripts.db_state_migrator import checkpoint_duckdb

    assert checkpoint_duckdb(tmp_path / "does-not-exist.duckdb") is False


def test_main_checkpoints_duckdb_before_backup(tmp_path, pg_engine, monkeypatch):
    """B2 — main() must CHECKPOINT the DuckDB before backing it up, so the
    backup artifact captures WAL-tail commits from a hard-killed app."""
    import duckdb
    from src.db import _ensure_schema
    import scripts.db_state_migrator as m

    duck_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.execute("INSERT INTO users (id, email, name) VALUES ('u1', 'a@x', 'A')")
    conn.close()

    order: list[str] = []
    monkeypatch.setattr(
        m, "checkpoint_duckdb", lambda p: (order.append("checkpoint"), True)[1]
    )
    orig_backup = m.backup_duckdb

    def spy_backup(path, backups_dir):
        order.append("backup")
        return orig_backup(path, backups_dir)

    monkeypatch.setattr(m, "backup_duckdb", spy_backup)

    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    from src.db_state_machine import BackendState, write_backend_state
    write_backend_state(BackendState.SIDE_CAR_IN_PROGRESS)

    rc = m.main(
        job_id="job-checkpoint-order",
        to="side_car",
        target_url=str(pg_engine.url),
        duckdb_path=duck_path,
        jobs_dir=tmp_path / "db-jobs",
        backups_dir=tmp_path / "backups",
    )
    assert rc == 0
    assert "checkpoint" in order and "backup" in order
    assert order.index("checkpoint") < order.index("backup")


def test_checkpoint_duckdb_failure_is_nonfatal(tmp_path, monkeypatch, caplog):
    """B2 — a CHECKPOINT failure is best-effort: returns False + logs a
    WARNING, never raises. The copy still reads WAL through its read-only
    open, so a failed checkpoint only risks a slightly-incomplete backup,
    not a lossy migration. Pins that contract so a future edit can't turn
    the exception into an abort unnoticed."""
    import logging

    from scripts.db_state_migrator import checkpoint_duckdb

    duck_path = tmp_path / "system.duckdb"
    duck_path.touch()  # must exist to get past the missing-file no-op

    def boom(*_args, **_kwargs):
        raise RuntimeError("Cannot CHECKPOINT: there are other transactions active")

    # checkpoint_duckdb imports _open_duckdb from src.duckdb_conn inside the
    # function body, so patching the module attribute intercepts it.
    monkeypatch.setattr("src.duckdb_conn._open_duckdb", boom)

    with caplog.at_level(logging.WARNING, logger="scripts.db_state_migrator"):
        assert checkpoint_duckdb(duck_path) is False
    assert any(
        "CHECKPOINT before backup/copy failed" in r.getMessage()
        for r in caplog.records
    )


def test_main_checkpoints_duckdb_before_direct_cloud_copy(
    tmp_path, pg_engine, monkeypatch
):
    """B2 — the direct DuckDB→CLOUD path has no backup step, so pin the
    checkpoint against the *copy* instead: CHECKPOINT must run before the
    source is read. Guards against a regression that moves the checkpoint
    into the ``to == "side_car"`` branch — the side_car-only order test
    above would still pass while the cloud path silently lost WAL-tail
    commits."""
    import scripts.db_state_migrator as m

    order: list[str] = []
    monkeypatch.setattr(
        m, "checkpoint_duckdb", lambda p: (order.append("checkpoint"), True)[1]
    )

    def spy_copy(duckdb_path, target_url, writer=None):
        order.append("copy")
        return {
            "rows_total": 0,
            "tables_migrated": 0,
            "target_rows_reset": 0,
            "tables_failed": [],
            "tables_skipped": [],
        }

    monkeypatch.setattr(m, "copy_duckdb_to_pg", spy_copy)
    monkeypatch.setattr(m, "verify_row_counts", lambda *a, **k: [])

    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    from src.db_state_machine import BackendState, write_backend_state
    write_backend_state(BackendState.CLOUD_IN_PROGRESS)

    duck_path = tmp_path / "system.duckdb"
    duck_path.touch()  # copy is stubbed — no real schema needed

    rc = m.main(
        job_id="job-cloud-checkpoint-order",
        to="cloud",
        source_backend="duckdb",
        target_url=str(pg_engine.url),
        duckdb_path=duck_path,
        jobs_dir=tmp_path / "db-jobs",
        backups_dir=tmp_path / "backups",
    )
    assert rc == 0
    assert order == ["checkpoint", "copy"]
