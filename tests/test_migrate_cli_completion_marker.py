"""Completion-marker behavior of ``python -m scripts.migrate_duckdb_to_pg``.

Regression for the row-resurrection loop: the docker-compose
``data-migrate`` one-shot re-runs the DuckDB → Postgres copy on every
``compose up``. The copy is ``INSERT … ON CONFLICT DO NOTHING`` — meant as
crash-resume idempotency — but once the migration has genuinely completed,
the frozen ``system.duckdb`` snapshot stops being a source of truth. From
that point every re-run silently re-inserts any row an admin has since
deleted from Postgres (original values, no audit trail): registry rows,
package memberships, sync bookkeeping. Observed in production as
"unregistered tables come back after every container recreate".

The fix: a successful COMPLETE run writes ``<duckdb>.migrated`` next to the
source, and later runs exit 0 without copying — unless the operator passes
``--force`` (or ``--reset-target``, which already asserts a deliberate
re-cutover), or the target database holds no app state (a replaced or
restored target — e.g. disaster recovery from the DuckDB backup — must
still receive the copy; see ``marker.target_has_app_state``). ``--dry-run``
stays available as a diagnostic and neither respects nor writes the marker.

Everything past the argument parsing uses fakes for the DuckDB connection,
the PG engine, the target-state probe, and ``run_all`` — the PG-backed
gate/probe behavior is covered in ``tests/db_pg``.
"""

from __future__ import annotations

import json
import sys

import pytest

from scripts.migrate_duckdb_to_pg.__main__ import main
from scripts.migrate_duckdb_to_pg.marker import marker_path, read_completion_marker


@pytest.fixture(autouse=True)
def _no_pg_env(monkeypatch):
    """No DATABASE_URL: every test fakes the engine, and a code path that
    slipped past the fakes to build a real engine should fail loudly."""
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.fixture()
def source(tmp_path):
    """A dummy source file standing in for system.duckdb.

    Content never matters: the gated path returns before opening it, and
    the proceeding paths patch ``_open_duckdb``.
    """
    path = tmp_path / "state" / "system.duckdb"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a real duckdb")
    return path


class _FakeConn:
    def close(self):
        pass


class _FakeEngine:
    def dispose(self):
        pass


@pytest.fixture()
def proceed_fakes(monkeypatch):
    """Patch everything past the gate; record run_all invocations.

    The target-state probe defaults to True (a populated target) — the
    replaced-target case overrides it per-test.
    """
    calls: list[dict] = []

    def fake_run_all(duck_conn, pg_engine, only=None, dry_run=False, validate=True, reset_target=False, **kw):
        calls.append({"only": only, "dry_run": dry_run, "reset_target": reset_target})
        return [{"table": "users", "duckdb_rows": 1, "pg_rows": 1, "checksum_match": True}]

    monkeypatch.setattr("scripts.migrate_duckdb_to_pg.run_all", fake_run_all)
    monkeypatch.setattr("src.db_pg.get_engine", lambda: _FakeEngine())
    monkeypatch.setattr("src.duckdb_conn._open_duckdb", lambda *a, **k: _FakeConn())
    monkeypatch.setattr("scripts.migrate_duckdb_to_pg.marker.target_has_app_state", lambda engine: True)
    return calls


def _run_cli(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["migrate_duckdb_to_pg", *argv])
    return main()


def test_successful_full_run_writes_marker(source, monkeypatch, proceed_fakes):
    rc = _run_cli(monkeypatch, "--duckdb-path", str(source))
    assert rc == 0
    marker = read_completion_marker(source)
    assert marker is not None
    assert marker["tables_migrated"] == 1
    assert marker["completed_at"]


def test_marker_skips_copy_and_exits_0(source, monkeypatch, proceed_fakes, capsys):
    """The regression itself: a second run after completion must NOT copy."""
    assert _run_cli(monkeypatch, "--duckdb-path", str(source)) == 0
    assert len(proceed_fakes) == 1

    rc = _run_cli(monkeypatch, "--duckdb-path", str(source))
    assert rc == 0
    assert len(proceed_fakes) == 1, "completed migration must not re-copy"
    out = capsys.readouterr().out
    assert "already completed" in out
    # The skip message must tell the operator both WHY (resurrection) and
    # the escape hatch.
    assert "--force" in out


def test_marker_ignored_when_target_has_no_app_state(source, monkeypatch, proceed_fakes, capsys):
    """Replaced/restored target: the marker describes a migration into a
    database that no longer exists — the copy must run (DR from the DuckDB
    backup starts from an empty PG)."""
    assert _run_cli(monkeypatch, "--duckdb-path", str(source)) == 0
    monkeypatch.setattr("scripts.migrate_duckdb_to_pg.marker.target_has_app_state", lambda engine: False)
    rc = _run_cli(monkeypatch, "--duckdb-path", str(source))
    assert rc == 0
    assert len(proceed_fakes) == 2, "empty target must be re-populated despite the marker"
    assert "replaced or restored" in capsys.readouterr().out


def test_force_bypasses_marker(source, monkeypatch, proceed_fakes):
    assert _run_cli(monkeypatch, "--duckdb-path", str(source)) == 0
    rc = _run_cli(monkeypatch, "--duckdb-path", str(source), "--force")
    assert rc == 0
    assert len(proceed_fakes) == 2


def test_reset_target_bypasses_marker(source, monkeypatch, proceed_fakes):
    assert _run_cli(monkeypatch, "--duckdb-path", str(source)) == 0
    rc = _run_cli(monkeypatch, "--duckdb-path", str(source), "--reset-target")
    assert rc == 0
    assert len(proceed_fakes) == 2
    assert proceed_fakes[1]["reset_target"] is True


def test_dry_run_bypasses_marker_and_does_not_write_it(source, monkeypatch, proceed_fakes):
    rc = _run_cli(monkeypatch, "--duckdb-path", str(source), "--dry-run")
    assert rc == 0
    assert len(proceed_fakes) == 1
    assert read_completion_marker(source) is None, "diagnostic runs must not claim completion"


def test_only_subset_does_not_write_marker(source, monkeypatch, proceed_fakes):
    rc = _run_cli(monkeypatch, "--duckdb-path", str(source), "--only", "users")
    assert rc == 0
    assert read_completion_marker(source) is None, "a partial copy is not a completed migration"


def test_failed_run_does_not_write_marker(source, monkeypatch):
    def failing_run_all(*a, **k):
        return [{"table": "users", "error": "boom"}]

    monkeypatch.setattr("scripts.migrate_duckdb_to_pg.run_all", failing_run_all)
    monkeypatch.setattr("src.db_pg.get_engine", lambda: _FakeEngine())
    monkeypatch.setattr("src.duckdb_conn._open_duckdb", lambda *a, **k: _FakeConn())

    rc = _run_cli(monkeypatch, "--duckdb-path", str(source))
    assert rc == 1
    assert read_completion_marker(source) is None


def test_halted_run_does_not_write_marker(source, monkeypatch):
    """H6 halt-on-failure leaves later tasks skipped — the inventory was
    not fully copied even though the skipped entries carry no error key
    themselves, so completion must not be recorded."""

    def halted_run_all(*a, **k):
        return [
            {"table": "users", "error": "boom"},
            {"table": "audit_log", "skipped": True, "reason": "halted after prior task failure"},
        ]

    monkeypatch.setattr("scripts.migrate_duckdb_to_pg.run_all", halted_run_all)
    monkeypatch.setattr("src.db_pg.get_engine", lambda: _FakeEngine())
    monkeypatch.setattr("src.duckdb_conn._open_duckdb", lambda *a, **k: _FakeConn())

    rc = _run_cli(monkeypatch, "--duckdb-path", str(source))
    assert rc == 1
    assert read_completion_marker(source) is None


def test_unparseable_marker_is_ignored(source, monkeypatch, proceed_fakes):
    """A corrupt marker must not brick the boot gate — treat as absent."""
    marker_path(source).write_text("{corrupt")
    rc = _run_cli(monkeypatch, "--duckdb-path", str(source))
    assert rc == 0
    assert len(proceed_fakes) == 1
    # ... and the successful run repairs it.
    assert read_completion_marker(source) is not None


def test_marker_roundtrip_content(source, monkeypatch, proceed_fakes):
    assert _run_cli(monkeypatch, "--duckdb-path", str(source)) == 0
    raw = json.loads(marker_path(source).read_text())
    assert raw["source"] == "migrate_duckdb_to_pg"
    assert raw["tables_migrated"] == 1
