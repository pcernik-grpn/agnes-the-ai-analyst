"""Cross-engine contract test for the session_processor_state repository.

Pins the per-(processor, session) bookkeeping read/write helpers that back
the session-pipeline health check (app/api/health.py), the pipeline-status
enrichment in the per-user stats tab (app/api/me_stats.py), and the
usage-reprocess admin action (app/api/admin_usage.py reprocess_usage).

Parametrising over both backends through the repo classes makes a parity
regression at the routing layer impossible: each new method must behave
identically on DuckDB and Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


def _make_duckdb_repo(tmp_path):
    # Route through `_open_duckdb` (not bare `duckdb.connect`) so the session
    # timezone is pinned to UTC — matches the production helper and the
    # `tests/test_duckdb_session_tz.py` regression guard.
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.session_processor_state import (
        SessionProcessorStateRepository,
    )

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return {
        "repo": SessionProcessorStateRepository(conn),
        "conn": conn,
        "backend": "duckdb",
    }


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
    from src import db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.session_processor_state_pg import (
        SessionProcessorStatePgRepository,
    )

    eng = db_pg.get_engine()
    return {
        "repo": SessionProcessorStatePgRepository(eng),
        "engine": eng,
        "backend": "pg",
    }


@pytest.fixture(params=["duckdb", "pg"], ids=["duck", "pg"])
def repos(request, tmp_path, pg_engine, monkeypatch):
    if request.param == "duckdb":
        bundle = _make_duckdb_repo(tmp_path)
        yield bundle
        bundle["conn"].close()
    else:
        bundle = _make_pg_repo(pg_engine, monkeypatch)
        yield bundle


def _seed(repos, processor, session_file, items=1):
    """Seed one state row via the production mark_processed UPSERT."""
    repos["repo"].mark_processed(
        processor_name=processor,
        session_file=session_file,
        username="alice",
        items_count=items,
        file_hash=f"hash-{processor}-{session_file}",
    )


# ---------------------------------------------------------------------------
# mark_processed
# ---------------------------------------------------------------------------


class TestMarkProcessed:
    def test_supplied_read_at_round_trips(self, repos):
        """A caller can pass an explicit observation timestamp; both backends
        store it as ``processed_at`` so ``scan_unprocessed_for`` compares
        mtime against the content-snapshot moment, not the completion time."""
        read_at = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)
        repos["repo"].mark_processed(
            processor_name="verification",
            session_file="alice/a.jsonl",
            username="alice",
            items_count=1,
            file_hash="h1",
            read_at=read_at,
        )
        assert _processed_at_utc(repos, "verification", "alice/a.jsonl") == read_at


# ---------------------------------------------------------------------------
# delete_for_processors
# ---------------------------------------------------------------------------


class TestDeleteForProcessors:
    def test_empty_input_returns_zero(self, repos):
        assert repos["repo"].delete_for_processors([]) == 0

    def test_deletes_only_named_processor(self, repos):
        _seed(repos, "usage", "alice/a.jsonl")
        _seed(repos, "usage", "alice/b.jsonl")
        _seed(repos, "verification", "alice/a.jsonl")

        deleted = repos["repo"].delete_for_processors(["usage"])
        assert deleted == 2

        # The OTHER processor's rows are untouched.
        assert repos["repo"].processed_session_files("usage") == set()
        assert repos["repo"].processed_session_files("verification") == {"alice/a.jsonl"}

    def test_deletes_multiple_processors(self, repos):
        _seed(repos, "usage", "alice/a.jsonl")
        _seed(repos, "marketplace_rollup_30d", "alice/a.jsonl")
        _seed(repos, "verification", "alice/a.jsonl")

        deleted = repos["repo"].delete_for_processors(["usage", "marketplace_rollup_30d"])
        assert deleted == 2
        assert repos["repo"].processed_session_files("verification") == {"alice/a.jsonl"}

    def test_unknown_processor_deletes_nothing(self, repos):
        _seed(repos, "usage", "alice/a.jsonl")
        assert repos["repo"].delete_for_processors(["nope"]) == 0
        assert repos["repo"].processed_session_files("usage") == {"alice/a.jsonl"}


# ---------------------------------------------------------------------------
# max_processed_at
# ---------------------------------------------------------------------------


class TestMaxProcessedAt:
    def test_none_when_no_rows(self, repos):
        assert repos["repo"].max_processed_at("verification") is None

    def test_returns_latest(self, repos):
        _seed(repos, "verification", "alice/a.jsonl")
        result = repos["repo"].max_processed_at("verification")
        assert result is not None

    def test_isolated_per_processor(self, repos):
        _seed(repos, "usage", "alice/a.jsonl")
        # No 'verification' rows even though 'usage' has one.
        assert repos["repo"].max_processed_at("verification") is None


# ---------------------------------------------------------------------------
# processed_session_files
# ---------------------------------------------------------------------------


class TestProcessedSessionFiles:
    def test_empty_when_no_rows(self, repos):
        assert repos["repo"].processed_session_files("verification") == set()

    def test_returns_set_for_processor(self, repos):
        _seed(repos, "verification", "alice/a.jsonl")
        _seed(repos, "verification", "alice/b.jsonl")
        _seed(repos, "usage", "alice/c.jsonl")
        assert repos["repo"].processed_session_files("verification") == {
            "alice/a.jsonl",
            "alice/b.jsonl",
        }


# ---------------------------------------------------------------------------
# get_states_for_session_files
# ---------------------------------------------------------------------------


class TestGetStatesForSessionFiles:
    def test_empty_input_returns_empty(self, repos):
        _seed(repos, "verification", "alice/a.jsonl")
        assert repos["repo"].get_states_for_session_files("verification", []) == {}

    def test_returns_states_for_matching_files(self, repos):
        _seed(repos, "verification", "alice/a.jsonl", items=3)
        _seed(repos, "verification", "alice/b.jsonl", items=0)

        states = repos["repo"].get_states_for_session_files("verification", ["alice/a.jsonl", "alice/b.jsonl"])
        assert set(states.keys()) == {"alice/a.jsonl", "alice/b.jsonl"}
        assert states["alice/a.jsonl"]["items_extracted"] == 3
        assert states["alice/b.jsonl"]["items_extracted"] == 0
        assert states["alice/a.jsonl"]["processed_at"] is not None

    def test_only_returns_requested_files(self, repos):
        _seed(repos, "verification", "alice/a.jsonl")
        _seed(repos, "verification", "alice/b.jsonl")
        states = repos["repo"].get_states_for_session_files("verification", ["alice/a.jsonl"])
        assert set(states.keys()) == {"alice/a.jsonl"}

    def test_scoped_to_processor(self, repos):
        _seed(repos, "usage", "alice/a.jsonl")
        # File exists under 'usage' but we ask 'verification' — no match.
        states = repos["repo"].get_states_for_session_files("verification", ["alice/a.jsonl"])
        assert states == {}

    def test_missing_file_absent_from_result(self, repos):
        _seed(repos, "verification", "alice/a.jsonl")
        states = repos["repo"].get_states_for_session_files("verification", ["alice/a.jsonl", "alice/missing.jsonl"])
        assert set(states.keys()) == {"alice/a.jsonl"}


# ---------------------------------------------------------------------------
# activity_since
# ---------------------------------------------------------------------------


class TestActivitySince:
    def test_no_rows_returns_none_and_zero(self, repos):
        since = datetime.now(UTC) - timedelta(hours=1)
        result = repos["repo"].activity_since("verification", since)
        assert result == {"last_processed_at": None, "items_extracted": 0}

    def test_sums_items_within_window(self, repos):
        _seed(repos, "verification", "alice/a.jsonl", items=3)
        _seed(repos, "verification", "alice/b.jsonl", items=4)
        since = datetime.now(UTC) - timedelta(hours=1)
        result = repos["repo"].activity_since("verification", since)
        assert result["items_extracted"] == 7
        assert result["last_processed_at"] is not None

    def test_excludes_rows_outside_window(self, repos):
        _seed(repos, "verification", "alice/a.jsonl", items=5)
        # A window in the future — the just-seeded row falls before it.
        since = datetime.now(UTC) + timedelta(hours=1)
        result = repos["repo"].activity_since("verification", since)
        assert result == {"last_processed_at": None, "items_extracted": 0}

    def test_scoped_to_processor(self, repos):
        _seed(repos, "usage", "alice/a.jsonl", items=9)
        since = datetime.now(UTC) - timedelta(hours=1)
        result = repos["repo"].activity_since("verification", since)
        assert result == {"last_processed_at": None, "items_extracted": 0}


# ---------------------------------------------------------------------------
# scan_unprocessed_for
# ---------------------------------------------------------------------------


def _md5(path):
    import hashlib

    return hashlib.md5(path.read_bytes()).hexdigest()


def _processed_at_utc(repos, processor, key):
    """The stored processed_at as a tz-aware UTC datetime.

    DuckDB hands back a UTC-clock-naive value (the connection pins the session
    timezone to UTC); PG's TIMESTAMPTZ keeps the offset. Normalize so the test
    does identical arithmetic on both backends.
    """
    pa = repos["repo"].get_states_for_session_files(processor, [key])[key]["processed_at"]
    return pa.replace(tzinfo=UTC) if pa.tzinfo is None else pa.astimezone(UTC)


def _write_session(session_dir, username, name, body):
    user_dir = session_dir / username
    user_dir.mkdir(parents=True, exist_ok=True)
    p = user_dir / name
    p.write_text(body)
    return p


def _set_mtime(path, when):
    import os

    ts = when.timestamp()
    os.utime(path, (ts, ts))


class TestScanUnprocessedFor:
    """The mtime precheck, incl. the clock-skew window and its hash-verify
    fallback. Both backends must make the same include/skip call.
    """

    def test_no_state_row_is_surfaced(self, repos, tmp_path):
        sess = tmp_path / "sessions"
        _write_session(sess, "alice", "a.jsonl", '{"x":1}\n')
        found = repos["repo"].scan_unprocessed_for("verification", sess)
        assert [(u, p.name) for u, p in found] == [("alice", "a.jsonl")]

    def test_stable_session_well_before_processed_at_is_skipped(self, repos, tmp_path):
        """The cheap stat-only optimization still holds: a file untouched since
        the last tick is skipped without hashing."""
        sess = tmp_path / "sessions"
        p = _write_session(sess, "alice", "a.jsonl", '{"x":1}\n')
        _seed(repos, "verification", "alice/a.jsonl")
        pa = _processed_at_utc(repos, "verification", "alice/a.jsonl")
        _set_mtime(p, pa - timedelta(seconds=30))
        assert repos["repo"].scan_unprocessed_for("verification", sess) == []

    def test_mtime_equal_to_processed_at_is_surfaced(self, repos, tmp_path):
        """The comparison is `mtime >= processed_at`, not `>`: a same-instant
        write is a live-append candidate, not a stable file."""
        sess = tmp_path / "sessions"
        p = _write_session(sess, "alice", "a.jsonl", '{"x":1}\n')
        _seed(repos, "verification", "alice/a.jsonl")
        pa = _processed_at_utc(repos, "verification", "alice/a.jsonl")
        _set_mtime(p, pa)
        found = repos["repo"].scan_unprocessed_for("verification", sess)
        assert [(u, f.name) for u, f in found] == [("alice", "a.jsonl")]

    def test_within_skew_window_with_changed_content_is_surfaced(self, repos, tmp_path):
        """The fix: an mtime marginally OLDER than processed_at (clock skew) used
        to be discarded on the stat alone. Inside the skew window the stored
        file_hash is consulted, and a mismatch surfaces the file."""
        sess = tmp_path / "sessions"
        p = _write_session(sess, "alice", "a.jsonl", '{"x":1}\n')
        # _seed stores a sentinel hash, which cannot match the file's real md5.
        _seed(repos, "verification", "alice/a.jsonl")
        pa = _processed_at_utc(repos, "verification", "alice/a.jsonl")
        _set_mtime(p, pa - timedelta(milliseconds=10))
        found = repos["repo"].scan_unprocessed_for("verification", sess)
        assert [(u, f.name) for u, f in found] == [("alice", "a.jsonl")]

    def test_within_skew_window_with_matching_hash_is_skipped(self, repos, tmp_path):
        """The window must not turn every near-mtime file into churn: when the
        stored hash still matches the content there is nothing to reprocess."""
        sess = tmp_path / "sessions"
        p = _write_session(sess, "alice", "a.jsonl", '{"x":1}\n')
        repos["repo"].mark_processed(
            processor_name="verification",
            session_file="alice/a.jsonl",
            username="alice",
            items_count=1,
            file_hash=_md5(p),
        )
        pa = _processed_at_utc(repos, "verification", "alice/a.jsonl")
        _set_mtime(p, pa - timedelta(milliseconds=10))
        assert repos["repo"].scan_unprocessed_for("verification", sess) == []

    def test_outside_skew_window_is_skipped_even_when_content_changed(self, repos, tmp_path):
        """Pins the RESIDUAL gap the 50 ms window does NOT close: a final append
        whose mtime lands more than the window before `processed_at` (a tick
        slower than 50 ms) is still skipped and the stored hash is never
        consulted. Closing it needs mtime-at-read persisted — a schema change.
        Update this test when that lands; it is documentation, not a wish.
        """
        sess = tmp_path / "sessions"
        p = _write_session(sess, "alice", "a.jsonl", '{"x":1}\n')
        _seed(repos, "verification", "alice/a.jsonl")  # sentinel hash != real md5
        pa = _processed_at_utc(repos, "verification", "alice/a.jsonl")
        _set_mtime(p, pa - timedelta(seconds=1))
        assert repos["repo"].scan_unprocessed_for("verification", sess) == []

    def test_missing_session_dir_returns_empty(self, repos, tmp_path):
        assert repos["repo"].scan_unprocessed_for("verification", tmp_path / "nope") == []


# ---------------------------------------------------------------------------
# processor-version invalidation
# ---------------------------------------------------------------------------


class TestProcessorVersionInvalidation:
    """A processor that declares a ``version`` gets its bookkeeping invalidated
    on a bump: rows written at an older version — or before versioning existed
    at all — are dirty even when the file content (and therefore its md5) is
    unchanged. This is the mechanism behind ``USAGE_PROCESSOR_VERSION``
    backfills; before it existed, a bump only re-processed files whose
    CONTENT also changed (live gap observed 2026-09-01: 111 files stuck at
    processor_version 9 across 7+ hours of sweeps).
    """

    def test_same_version_and_hash_is_processed(self, repos):
        repos["repo"].mark_processed(
            processor_name="usage",
            session_file="alice/a.jsonl",
            username="alice",
            items_count=1,
            file_hash="abc123",
            version=3,
        )
        assert repos["repo"].is_processed("usage", "alice/a.jsonl", "abc123", version=3) is True

    def test_version_bump_makes_row_dirty(self, repos):
        repos["repo"].mark_processed(
            processor_name="usage",
            session_file="alice/a.jsonl",
            username="alice",
            items_count=1,
            file_hash="abc123",
            version=3,
        )
        assert repos["repo"].is_processed("usage", "alice/a.jsonl", "abc123", version=4) is False

    def test_legacy_unversioned_row_is_dirty_under_versioned_check(self, repos):
        """Rows written before the processor declared a version have a bare
        md5 stored; introducing a version must re-process them once."""
        repos["repo"].mark_processed(
            processor_name="usage",
            session_file="alice/a.jsonl",
            username="alice",
            items_count=1,
            file_hash="abc123",
        )
        assert repos["repo"].is_processed("usage", "alice/a.jsonl", "abc123", version=3) is False

    def test_versionless_processor_is_unaffected(self, repos):
        """A processor without a version (verification) keeps today's exact
        semantics — bare md5 stored and compared."""
        repos["repo"].mark_processed(
            processor_name="verification",
            session_file="alice/a.jsonl",
            username="alice",
            items_count=1,
            file_hash="abc123",
        )
        assert repos["repo"].is_processed("verification", "alice/a.jsonl", "abc123") is True

    def test_scan_surfaces_version_stale_row_despite_stable_mtime(self, repos, tmp_path):
        """The load-bearing half of the fix: the mtime precheck skips an
        untouched file before any hash is consulted, so the version comparison
        must happen in the scan itself — otherwise a version-stale file never
        even reaches the runner's ``is_processed`` check."""
        sess = tmp_path / "sessions"
        p = _write_session(sess, "alice", "a.jsonl", '{"x":1}\n')
        repos["repo"].mark_processed(
            processor_name="usage",
            session_file="alice/a.jsonl",
            username="alice",
            items_count=1,
            file_hash=_md5(p),
            version=3,
        )
        pa = _processed_at_utc(repos, "usage", "alice/a.jsonl")
        _set_mtime(p, pa - timedelta(seconds=30))

        assert repos["repo"].scan_unprocessed_for("usage", sess, version=3) == []
        found = repos["repo"].scan_unprocessed_for("usage", sess, version=4)
        assert [(u, f.name) for u, f in found] == [("alice", "a.jsonl")]

    def test_scan_surfaces_legacy_row_when_version_introduced(self, repos, tmp_path):
        sess = tmp_path / "sessions"
        p = _write_session(sess, "alice", "a.jsonl", '{"x":1}\n')
        repos["repo"].mark_processed(
            processor_name="usage",
            session_file="alice/a.jsonl",
            username="alice",
            items_count=1,
            file_hash=_md5(p),
        )
        pa = _processed_at_utc(repos, "usage", "alice/a.jsonl")
        _set_mtime(p, pa - timedelta(seconds=30))

        found = repos["repo"].scan_unprocessed_for("usage", sess, version=3)
        assert [(u, f.name) for u, f in found] == [("alice", "a.jsonl")]

    def test_scan_skew_window_compares_content_hash_not_version_prefix(self, repos, tmp_path):
        """Inside the clock-skew window the stored hash is consulted; for a
        versioned row the comparison must be against the md5 *inside* the
        stored value — a whole-string compare would surface every versioned
        row as churn."""
        sess = tmp_path / "sessions"
        p = _write_session(sess, "alice", "a.jsonl", '{"x":1}\n')
        repos["repo"].mark_processed(
            processor_name="usage",
            session_file="alice/a.jsonl",
            username="alice",
            items_count=1,
            file_hash=_md5(p),
            version=3,
        )
        pa = _processed_at_utc(repos, "usage", "alice/a.jsonl")
        _set_mtime(p, pa - timedelta(milliseconds=10))
        assert repos["repo"].scan_unprocessed_for("usage", sess, version=3) == []
