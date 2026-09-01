"""Tests for the session-pipeline framework (services/session_pipeline/).

Covers:
- Pure utility functions (parse_jsonl, compute_file_hash) and their behavior on
  edge cases (malformed lines, file changes).
- SessionProcessorStateRepository CRUD on a fresh in-memory schema.
- run_processor end-to-end with fake processors covering success, raise,
  empty-result, and file-hash-invalidation paths.
- v29 migration: existing session_extraction_state rows are copied to
  session_processor_state with processor_name='verification' and the old
  table is dropped.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC
from pathlib import Path
from unittest.mock import MagicMock

import duckdb

from services.session_pipeline.contract import ProcessorResult
from services.session_pipeline.lib import compute_file_hash, parse_jsonl
from services.session_pipeline.runner import (
    resolve_user_id,
    resolve_user_identity,
    run_processor,
)
from src.repositories.session_processor_state import SessionProcessorStateRepository


def _fresh_db(tmp_path, monkeypatch) -> duckdb.DuckDBPyConnection:
    """Same idiom as tests/test_corporate_memory_v1.py — fresh schema in tmp_path."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module

    db_module._system_db_conn = None
    db_module._system_db_path = None
    return db_module.get_system_db()


# ---------------------------------------------------------------------------
# parse_jsonl
# ---------------------------------------------------------------------------


class TestParseJsonl:
    def test_parses_well_formed_lines(self, tmp_path):
        f = tmp_path / "session.jsonl"
        f.write_text(
            json.dumps({"role": "user", "content": "hi"})
            + "\n"
            + json.dumps({"role": "assistant", "content": "hello"})
            + "\n"
        )
        turns = parse_jsonl(f)
        assert len(turns) == 2
        assert turns[0]["role"] == "user"
        assert turns[1]["content"] == "hello"

    def test_skips_malformed_lines(self, tmp_path):
        """Same behavior as pre-refactor verification_detector.parse_session —
        a single corrupt row mustn't abort processing of the rest."""
        f = tmp_path / "session.jsonl"
        f.write_text(
            json.dumps({"role": "user", "content": "ok"})
            + "\n"
            + "this is not json\n"
            + json.dumps({"role": "assistant", "content": "still ok"})
            + "\n"
        )
        turns = parse_jsonl(f)
        assert len(turns) == 2
        assert turns[0]["content"] == "ok"
        assert turns[1]["content"] == "still ok"

    def test_skips_blank_lines(self, tmp_path):
        f = tmp_path / "session.jsonl"
        f.write_text("\n" + json.dumps({"role": "user", "content": "x"}) + "\n" + "   \n")
        turns = parse_jsonl(f)
        assert len(turns) == 1


# ---------------------------------------------------------------------------
# compute_file_hash
# ---------------------------------------------------------------------------


class TestComputeFileHash:
    def test_deterministic(self, tmp_path):
        f = tmp_path / "x.jsonl"
        f.write_text("hello world")
        assert compute_file_hash(f) == compute_file_hash(f)

    def test_changes_with_content(self, tmp_path):
        f = tmp_path / "x.jsonl"
        f.write_text("v1")
        h1 = compute_file_hash(f)
        f.write_text("v2")
        h2 = compute_file_hash(f)
        assert h1 != h2


# ---------------------------------------------------------------------------
# resolve_user_id
# ---------------------------------------------------------------------------


class TestResolveUserId:
    """Unit tests for the linchpin identity-resolution function."""

    @staticmethod
    def _seed_users(conn: duckdb.DuckDBPyConnection, rows: list[tuple[str, str, str | None]]) -> None:
        for uid, email, updated_at in rows:
            conn.execute(
                "INSERT INTO users (id, email, updated_at) VALUES (?, ?, ?)",
                [uid, email, updated_at],
            )

    def test_exact_uuid_match(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        self._seed_users(conn, [("uuid-aaa", "alice@example.com", "2026-01-01")])
        assert resolve_user_id("uuid-aaa") == "uuid-aaa"
        conn.close()

    def test_email_local_part_match(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        self._seed_users(conn, [("uuid-bbb", "bob@example.com", "2026-01-01")])
        assert resolve_user_id("bob") == "uuid-bbb"
        conn.close()

    def test_null_fallback_for_unknown(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        self._seed_users(conn, [("uuid-aaa", "alice@example.com", "2026-01-01")])
        assert resolve_user_id("nobody") is None
        conn.close()

    def test_tiebreak_picks_most_recently_updated(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        self._seed_users(
            conn,
            [
                ("uuid-old", "zara@old.com", "2025-01-01"),
                ("uuid-new", "zara@new.com", "2026-06-01"),
            ],
        )
        assert resolve_user_id("zara") == "uuid-new"
        conn.close()

    def test_underscore_not_treated_as_wildcard(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        self._seed_users(
            conn,
            [
                ("uuid-alice", "alicexsmith@example.com", "2026-01-01"),
                ("uuid-real", "alice_smith@example.com", "2025-01-01"),
            ],
        )
        # "alice_smith" must match only the literal underscore email
        assert resolve_user_id("alice_smith") == "uuid-real"
        conn.close()

    def test_uuid_branch_takes_priority_over_email(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        self._seed_users(conn, [("uuid-aaa", "uuid-aaa@example.com", "2026-01-01")])
        assert resolve_user_id("uuid-aaa") == "uuid-aaa"
        conn.close()


class TestResolveUserIdentity:
    """Resolver returns (uid, email) — the email is what the runner
    writes as the canonical ``username`` so the telemetry dropdown
    stops listing the same person under their UUID and their email."""

    @staticmethod
    def _seed(conn, rows):
        for uid, email, updated_at in rows:
            conn.execute(
                "INSERT INTO users (id, email, updated_at) VALUES (?, ?, ?)",
                [uid, email, updated_at],
            )

    def test_uuid_dir_resolves_to_email(self, tmp_path, monkeypatch):
        """Upload API writes /data/user_sessions/<uuid>/ — resolver
        must return the user's email so usage_events.username becomes
        readable instead of the raw UUID."""
        conn = _fresh_db(tmp_path, monkeypatch)
        self._seed(conn, [("uuid-aaa", "alice@example.com", "2026-01-01")])
        uid, email = resolve_user_identity("uuid-aaa")
        assert uid == "uuid-aaa"
        assert email == "alice@example.com"
        conn.close()

    def test_local_part_dir_resolves_to_email(self, tmp_path, monkeypatch):
        """Session collector writes /data/user_sessions/<os-username>/
        — resolver returns the matching email."""
        conn = _fresh_db(tmp_path, monkeypatch)
        self._seed(conn, [("uuid-bbb", "bob@example.com", "2026-01-01")])
        uid, email = resolve_user_identity("bob")
        assert uid == "uuid-bbb"
        assert email == "bob@example.com"
        conn.close()

    def test_unknown_returns_none_pair(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        self._seed(conn, [("uuid-aaa", "alice@example.com", "2026-01-01")])
        uid, email = resolve_user_identity("nobody")
        assert uid is None
        assert email is None
        conn.close()


# ---------------------------------------------------------------------------
# SessionProcessorStateRepository
# ---------------------------------------------------------------------------


class TestSessionProcessorStateRepository:
    def test_unprocessed_when_empty(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = SessionProcessorStateRepository(conn)
        assert repo.is_processed("verification", "alice/s.jsonl", "abc") is False
        conn.close()

    def test_mark_then_is_processed(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = SessionProcessorStateRepository(conn)
        repo.mark_processed("verification", "alice/s.jsonl", "alice", 3, "abc")
        assert repo.is_processed("verification", "alice/s.jsonl", "abc") is True
        conn.close()

    def test_independent_per_processor(self, tmp_path, monkeypatch):
        """Two processors track the same session independently — usage might be
        done while verification still has work."""
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = SessionProcessorStateRepository(conn)
        repo.mark_processed("usage", "alice/s.jsonl", "alice", 0, "abc")
        assert repo.is_processed("usage", "alice/s.jsonl", "abc") is True
        assert repo.is_processed("verification", "alice/s.jsonl", "abc") is False
        conn.close()

    def test_hash_mismatch_treated_as_unprocessed(self, tmp_path, monkeypatch):
        """When a session jsonl grows (live append from active Claude Code),
        the stored file_hash no longer matches → processor gets to reprocess."""
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = SessionProcessorStateRepository(conn)
        repo.mark_processed("verification", "alice/s.jsonl", "alice", 1, "old_hash")
        assert repo.is_processed("verification", "alice/s.jsonl", "new_hash") is False
        conn.close()

    def test_mark_upserts_on_re_run(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = SessionProcessorStateRepository(conn)
        repo.mark_processed("verification", "alice/s.jsonl", "alice", 1, "h1")
        repo.mark_processed("verification", "alice/s.jsonl", "alice", 5, "h2")
        row = conn.execute(
            "SELECT items_extracted, file_hash FROM session_processor_state WHERE processor_name=? AND session_file=?",
            ["verification", "alice/s.jsonl"],
        ).fetchone()
        assert row == (5, "h2")
        conn.close()

    def test_scan_unprocessed_returns_all_when_empty_state(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        (sessions / "alice").mkdir(parents=True)
        (sessions / "alice" / "s1.jsonl").write_text("{}")
        (sessions / "alice" / "s2.jsonl").write_text("{}")
        repo = SessionProcessorStateRepository(conn)
        results = repo.scan_unprocessed_for("verification", sessions)
        keys = sorted([f"{u}/{p.name}" for u, p in results])
        assert keys == ["alice/s1.jsonl", "alice/s2.jsonl"]
        conn.close()

    def test_scan_skips_non_directory_entries(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "stray.txt").write_text("not a user dir")
        (sessions / "alice").mkdir()
        (sessions / "alice" / "s.jsonl").write_text("{}")
        repo = SessionProcessorStateRepository(conn)
        results = repo.scan_unprocessed_for("verification", sessions)
        assert len(results) == 1
        assert results[0][0] == "alice"
        conn.close()

    def test_scan_filters_stable_sessions_via_mtime(self, tmp_path, monkeypatch):
        """Files with mtime <= processed_at are filtered at scan — the
        runner never sees them and never hashes them. PR #232 review fix:
        before the mtime precheck, every stable session was rehashed on
        every scheduler tick."""
        import time

        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        (sessions / "alice").mkdir(parents=True)
        stable = sessions / "alice" / "stable.jsonl"
        stable.write_text("{}\n")
        # Force mtime well in the past so we can set processed_at to "now"
        # and have the precheck reliably skip.
        old = time.time() - 3600
        os.utime(stable, (old, old))

        repo = SessionProcessorStateRepository(conn)
        repo.mark_processed("verification", "alice/stable.jsonl", "alice", 1, "h1")

        results = repo.scan_unprocessed_for("verification", sessions)
        assert results == [], "stable session must be filtered at scan"

        # New file alongside it surfaces — not in state at all.
        new_file = sessions / "alice" / "new.jsonl"
        new_file.write_text("{}\n")
        results = repo.scan_unprocessed_for("verification", sessions)
        assert [str(p.name) for _, p in results] == ["new.jsonl"]
        conn.close()

    def test_scan_surfaces_session_modified_after_processing(self, tmp_path, monkeypatch):
        """File touched after processed_at — likely a Claude Code live append —
        must come back through scan so the runner can hash + decide."""
        import time
        from datetime import datetime

        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        (sessions / "alice").mkdir(parents=True)
        f = sessions / "alice" / "live.jsonl"
        f.write_text("{}\n")

        repo = SessionProcessorStateRepository(conn)
        # Mark processed at past time, then bump the file mtime to "now"
        # to simulate a post-processing append.
        past = datetime.now(UTC).replace(microsecond=0)
        conn.execute(
            "INSERT INTO session_processor_state VALUES (?, ?, ?, ?, ?, ?)",
            ["verification", "alice/live.jsonl", "alice", past, 0, "h1"],
        )
        future = time.time() + 60
        os.utime(f, (future, future))

        results = repo.scan_unprocessed_for("verification", sessions)
        assert [str(p.name) for _, p in results] == ["live.jsonl"]
        conn.close()


# ---------------------------------------------------------------------------
# run_processor
# ---------------------------------------------------------------------------


class _FakeProcessor:
    """Test double that records its calls and is configurable per behavior."""

    def __init__(
        self,
        name: str = "fake",
        cadence_minutes: int = 10,
        return_value: ProcessorResult | None = None,
        raise_on_session: str | None = None,
        version: int | None = None,
    ):
        self.name = name
        self.cadence_minutes = cadence_minutes
        self.return_value = return_value if return_value is not None else ProcessorResult(items_count=0)
        self.raise_on_session = raise_on_session
        if version is not None:
            self.version = version
        self.calls: list[str] = []
        self.call_kwargs: list[dict] = []
        self.call_usernames: list[str] = []

    def process_session(self, session_path: Path, username: str, session_key: str, conn, **kwargs: object):
        self.calls.append(session_key)
        self.call_usernames.append(username)
        self.call_kwargs.append(dict(kwargs))
        if self.raise_on_session is not None and session_key == self.raise_on_session:
            raise RuntimeError("simulated processor failure")
        return self.return_value


def _seed_session(sessions_dir: Path, username: str, name: str, content: str = "{}\n") -> Path:
    user_dir = sessions_dir / username
    user_dir.mkdir(parents=True, exist_ok=True)
    path = user_dir / name
    path.write_text(content)
    return path


class TestRunProcessor:
    def test_processed_then_skipped_on_second_call(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        _seed_session(sessions, "alice", "s.jsonl")

        proc = _FakeProcessor(return_value=ProcessorResult(items_count=2))

        stats1 = run_processor(conn, proc, session_data_dir=sessions)
        assert stats1["processed"] == 1
        assert stats1["items_extracted"] == 2
        assert proc.calls == ["alice/s.jsonl"]

        stats2 = run_processor(conn, proc, session_data_dir=sessions)
        # Stable session (mtime <= processed_at) is filtered at scan, so the
        # runner never sees it — `scanned == 0`, not `skipped == 1`. The
        # earlier shape (return-everything-then-runner-skips) caused an
        # MD5-rehash storm per tick (PR #232 review fix).
        assert stats2["processed"] == 0
        assert stats2["scanned"] == 0
        assert proc.calls == ["alice/s.jsonl"]  # not invoked again
        conn.close()

    def test_raise_leaves_state_unwritten(self, tmp_path, monkeypatch):
        """A processor that raises must not be marked as processed — the runner
        retries the same session on the next tick."""
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        _seed_session(sessions, "alice", "s.jsonl")

        proc = _FakeProcessor(raise_on_session="alice/s.jsonl")

        stats = run_processor(conn, proc, session_data_dir=sessions)
        assert stats["errors"] == 1
        assert stats["processed"] == 0

        # State row absent: next call sees the session again.
        repo = SessionProcessorStateRepository(conn)
        assert repo.is_processed(proc.name, "alice/s.jsonl", "anything") is False

        # Second call retries.
        proc.raise_on_session = None  # this time succeed
        stats2 = run_processor(conn, proc, session_data_dir=sessions)
        assert stats2["processed"] == 1
        conn.close()

    def test_empty_result_marks_processed(self, tmp_path, monkeypatch):
        """0 items extracted is a valid outcome — UsageProcessor skeleton
        relies on this so its no-op runs aren't re-scanned every tick."""
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        _seed_session(sessions, "bob", "s.jsonl")

        proc = _FakeProcessor(return_value=ProcessorResult(items_count=0))

        stats1 = run_processor(conn, proc, session_data_dir=sessions)
        assert stats1["processed"] == 1
        assert stats1["items_extracted"] == 0

        stats2 = run_processor(conn, proc, session_data_dir=sessions)
        # Filtered at scan via mtime precheck — see test_processed_then_skipped_on_second_call.
        assert stats2["processed"] == 0
        assert stats2["scanned"] == 0
        conn.close()

    def test_version_bump_reprocesses_unchanged_file(self, tmp_path, monkeypatch):
        """Bumping a processor's declared ``version`` must re-process files
        whose content — and mtime — did not change. This is what makes a
        ``USAGE_PROCESSOR_VERSION`` bump actually backfill existing sessions;
        without it, only files whose content changed ever advanced (live gap
        2026-09-01: 111 files stuck at v9 across 7+ hours of sweeps)."""
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        path = _seed_session(sessions, "alice", "s.jsonl")
        # Push the mtime well into the past so the scan's stable-file skip
        # would win if version invalidation were missing.
        old = time.time() - 3600
        os.utime(path, (old, old))

        proc_v1 = _FakeProcessor(version=1, return_value=ProcessorResult(items_count=1))
        stats1 = run_processor(conn, proc_v1, session_data_dir=sessions)
        assert stats1["processed"] == 1

        # Same version, untouched file → filtered at scan, no churn.
        stats2 = run_processor(conn, proc_v1, session_data_dir=sessions)
        assert stats2["scanned"] == 0

        # Version bump, untouched file → re-processed exactly once.
        proc_v2 = _FakeProcessor(version=2, return_value=ProcessorResult(items_count=1))
        stats3 = run_processor(conn, proc_v2, session_data_dir=sessions)
        assert stats3["processed"] == 1
        assert proc_v2.calls == ["alice/s.jsonl"]

        stats4 = run_processor(conn, proc_v2, session_data_dir=sessions)
        assert stats4["scanned"] == 0
        conn.close()

    def test_version_backfill_respects_attempt_budget(self, tmp_path, monkeypatch):
        """A version bump makes the whole backlog dirty at once; it must drain
        under the existing per-tick budgets (attempt cap here, the 150s time
        budget shares the same code path) rather than in one giant tick."""
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        old = time.time() - 3600
        for name in ("a.jsonl", "b.jsonl", "c.jsonl"):
            p = _seed_session(sessions, "alice", name)
            os.utime(p, (old, old))

        proc_v1 = _FakeProcessor(version=1, return_value=ProcessorResult(items_count=1))
        assert run_processor(conn, proc_v1, session_data_dir=sessions)["processed"] == 3

        proc_v2 = _FakeProcessor(version=2, return_value=ProcessorResult(items_count=1))
        stats = run_processor(conn, proc_v2, session_data_dir=sessions, max_sessions_per_run=2)
        assert stats["processed"] == 2
        assert stats["capped"] == 1

        # Next tick finishes the drain.
        stats2 = run_processor(conn, proc_v2, session_data_dir=sessions, max_sessions_per_run=2)
        assert stats2["processed"] == 1
        conn.close()

    def test_file_hash_invalidates_state(self, tmp_path, monkeypatch):
        """When a session jsonl grows (Claude Code live-appends to an active
        session), the stored hash no longer matches → reprocessed."""
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        path = _seed_session(sessions, "alice", "s.jsonl", content="line1\n")

        proc = _FakeProcessor(return_value=ProcessorResult(items_count=1))

        stats1 = run_processor(conn, proc, session_data_dir=sessions)
        assert stats1["processed"] == 1

        # Mutate the file → new hash → reprocessed on next call.
        path.write_text("line1\nline2\n")
        stats2 = run_processor(conn, proc, session_data_dir=sessions)
        assert stats2["processed"] == 1
        assert proc.calls == ["alice/s.jsonl", "alice/s.jsonl"]
        conn.close()

    def test_file_appended_during_processing_is_reprocessed(self, tmp_path, monkeypatch):
        """A session jsonl appended *while* it is being processed must be seen
        again on the next tick, not dropped because mtime < processed_at."""
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        _seed_session(sessions, "alice", "s.jsonl", content="line1\n")

        class _Appender:
            name = "verification"
            cadence_minutes = 1

            def process_session(self, session_path: Path, username: str, session_key: str, conn, **kwargs):
                # Simulate a live append that happens mid-run. The write/flush
                # sets mtime early; the processor keeps running. With the old
                # completion-time semantics, processed_at ends up well after
                # the file mtime and the skew window hides the live append.
                with session_path.open("a") as f:
                    f.write("line2\n")
                    f.flush()
                time.sleep(0.2)
                return ProcessorResult(items_count=1)

        stats1 = run_processor(conn, _Appender(), session_data_dir=sessions)
        assert stats1["processed"] == 1

        # The append happened during processing, so the stored hash is stale.
        # The next tick must detect mtime > read_at and reprocess.
        stats2 = run_processor(
            conn,
            _FakeProcessor(name="verification", return_value=ProcessorResult(items_count=1)),
            session_data_dir=sessions,
        )
        assert stats2["processed"] == 1
        conn.close()

    def test_uuid_dir_passes_email_as_username(self, tmp_path, monkeypatch):
        """Sessions uploaded via /api/upload/sessions land in
        /data/user_sessions/<uuid>/. The runner must resolve the email
        and pass that to the processor as ``username`` — otherwise
        usage_events.username gets a UUID and the admin telemetry
        dropdown lists the same user under two identities."""
        conn = _fresh_db(tmp_path, monkeypatch)
        conn.execute(
            "INSERT INTO users (id, email, updated_at) VALUES (?, ?, ?)",
            ["uuid-aaa", "alice@example.com", "2026-01-01"],
        )
        sessions = tmp_path / "sessions"
        _seed_session(sessions, "uuid-aaa", "s.jsonl")

        proc = _FakeProcessor(return_value=ProcessorResult(items_count=1))
        run_processor(conn, proc, session_data_dir=sessions)

        assert proc.call_usernames == ["alice@example.com"]
        assert proc.call_kwargs[0]["user_id"] == "uuid-aaa"
        conn.close()

    def test_localpart_dir_passes_email_as_username(self, tmp_path, monkeypatch):
        """Sessions from the legacy collector land under the OS
        username (typically the email local-part). The runner must
        still write the full email so the dropdown collapses."""
        conn = _fresh_db(tmp_path, monkeypatch)
        conn.execute(
            "INSERT INTO users (id, email, updated_at) VALUES (?, ?, ?)",
            ["uuid-bbb", "bob@example.com", "2026-01-01"],
        )
        sessions = tmp_path / "sessions"
        _seed_session(sessions, "bob", "s.jsonl")

        proc = _FakeProcessor(return_value=ProcessorResult(items_count=1))
        run_processor(conn, proc, session_data_dir=sessions)

        assert proc.call_usernames == ["bob@example.com"]
        assert proc.call_kwargs[0]["user_id"] == "uuid-bbb"
        conn.close()

    def test_orphan_dir_falls_back_to_dir_name(self, tmp_path, monkeypatch):
        """If the directory name doesn't resolve to any user (deleted
        user, stray upload), keep the directory name as ``username``
        so the data isn't silently relabelled to a bogus identity."""
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        _seed_session(sessions, "orphan-uuid", "s.jsonl")

        proc = _FakeProcessor(return_value=ProcessorResult(items_count=1))
        run_processor(conn, proc, session_data_dir=sessions)

        assert proc.call_usernames == ["orphan-uuid"]
        assert proc.call_kwargs[0]["user_id"] is None
        conn.close()

    def test_processors_isolated(self, tmp_path, monkeypatch):
        """Two processors on the same session work independently — what one
        marked, the other still has to do."""
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        _seed_session(sessions, "alice", "s.jsonl")

        proc_a = _FakeProcessor(name="a")
        proc_b = _FakeProcessor(name="b")

        run_processor(conn, proc_a, session_data_dir=sessions)
        run_processor(conn, proc_b, session_data_dir=sessions)

        assert proc_a.calls == ["alice/s.jsonl"]
        assert proc_b.calls == ["alice/s.jsonl"]
        conn.close()

    def test_no_sessions_dir_returns_clean_stats(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        proc = _FakeProcessor()
        stats = run_processor(conn, proc, session_data_dir=tmp_path / "does_not_exist")
        assert stats["scanned"] == 0
        assert stats["processed"] == 0
        assert stats["errors"] == 0
        conn.close()

    def test_non_processor_result_return_coerced(self, tmp_path, monkeypatch):
        """A processor that returns the wrong type must not poison the state
        write — the runner coerces it to an empty result and still marks the
        session processed (alternative: retry forever)."""
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        _seed_session(sessions, "alice", "s.jsonl")

        class _BadReturn:
            name = "bad"
            cadence_minutes = 1

            def process_session(self, *a, **kw):
                return None  # type: ignore[return-value]

        stats = run_processor(conn, _BadReturn(), session_data_dir=sessions)
        assert stats["processed"] == 1
        assert stats["items_extracted"] == 0
        conn.close()


class TestRunProcessorMaxSessionsPerRun:
    """max_sessions_per_run bounds a single run's worst-case duration/CPU
    cost by deferring the remainder to the next scheduler tick, rather than
    processing an unbounded burst of unprocessed sessions in one request."""

    def test_caps_candidates_and_reports_deferred_count(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        _seed_session(sessions, "alice", "a.jsonl")
        _seed_session(sessions, "bob", "b.jsonl")
        _seed_session(sessions, "carol", "c.jsonl")

        proc = _FakeProcessor(return_value=ProcessorResult(items_count=1))

        stats = run_processor(conn, proc, session_data_dir=sessions, max_sessions_per_run=2)
        assert stats["scanned"] == 3  # true total, regardless of the cap
        assert stats["processed"] == 2
        assert stats["capped"] == 1
        assert len(proc.calls) == 2
        conn.close()

    def test_deferred_sessions_are_picked_up_on_next_call(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        _seed_session(sessions, "alice", "a.jsonl")
        _seed_session(sessions, "bob", "b.jsonl")

        proc = _FakeProcessor(return_value=ProcessorResult(items_count=1))

        stats1 = run_processor(conn, proc, session_data_dir=sessions, max_sessions_per_run=1)
        assert stats1["processed"] == 1
        assert stats1["capped"] == 1

        stats2 = run_processor(conn, proc, session_data_dir=sessions, max_sessions_per_run=1)
        assert stats2["processed"] == 1
        assert stats2["capped"] == 0
        assert sorted(proc.calls) == ["alice/a.jsonl", "bob/b.jsonl"]
        conn.close()

    def test_none_means_unbounded_default_behavior(self, tmp_path, monkeypatch):
        """No cap (the default when unset) preserves pre-existing behavior —
        every candidate is processed in one call."""
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        for i in range(5):
            _seed_session(sessions, f"user{i}", "s.jsonl")

        proc = _FakeProcessor(return_value=ProcessorResult(items_count=1))
        stats = run_processor(conn, proc, session_data_dir=sessions)
        assert stats["processed"] == 5
        assert stats["capped"] == 0
        conn.close()

    def test_cap_larger_than_candidates_is_a_no_op(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        _seed_session(sessions, "alice", "a.jsonl")

        proc = _FakeProcessor(return_value=ProcessorResult(items_count=1))
        stats = run_processor(conn, proc, session_data_dir=sessions, max_sessions_per_run=50)
        assert stats["processed"] == 1
        assert stats["capped"] == 0
        conn.close()

    def test_skip_only_candidates_do_not_consume_the_budget(self, tmp_path, monkeypatch):
        """Regression (Devin Review, PR #894): scan_unprocessed_for's mtime
        prefilter can resurface a file whose content (hash) hasn't actually
        changed — the runner's hash-aware is_processed() check then skips it
        for free, without ever calling the processor. That skip must NOT
        consume attempt budget: a skip-only candidate visited ahead of a
        genuinely unprocessed one must not push the real work past the cap.

        Both sessions live under the SAME username directory so their
        relative order is deterministic (``scan_unprocessed_for`` sorts
        files *within* a user directory via ``sorted(user_dir.glob(...))``,
        but does not sort the outer per-user directories against each
        other) — "a.jsonl" is always visited before "b.jsonl".
        """
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        a_path = _seed_session(sessions, "alice", "a.jsonl")

        proc = _FakeProcessor(return_value=ProcessorResult(items_count=1))

        # Fully process a.jsonl first...
        stats0 = run_processor(conn, proc, session_data_dir=sessions)
        assert stats0["processed"] == 1
        assert proc.calls == ["alice/a.jsonl"]

        # ...then bump its mtime WITHOUT changing content, so
        # scan_unprocessed_for's cheap mtime prefilter resurfaces it as a
        # candidate again, even though its hash (and therefore
        # is_processed()) says it's already done. Under the pre-fix
        # (candidates[:cap]) slicing, this skip-only candidate would have
        # occupied the single available slot ahead of b.jsonl.
        future = a_path.stat().st_mtime + 10
        os.utime(a_path, (future, future))

        # b.jsonl only arrives now — a genuinely unprocessed candidate that
        # must not be starved out by a.jsonl's free skip.
        _seed_session(sessions, "alice", "b.jsonl")

        stats1 = run_processor(conn, proc, session_data_dir=sessions, max_sessions_per_run=1)
        assert stats1["skipped"] == 1  # a.jsonl: free, doesn't touch the budget
        assert stats1["processed"] == 1  # b.jsonl: real work, gets its attempt
        assert stats1["capped"] == 0
        assert proc.calls == ["alice/a.jsonl", "alice/b.jsonl"]
        conn.close()


class TestRunProcessorTimeBudget:
    """Prod incident 2026-07-20: a bulk onboarding wave left the "usage"
    processor (exempt from ``max_sessions_per_run`` — see admin.py) with
    hundreds of unprocessed sessions. A single ``run_processor`` call drained
    the entire backlog synchronously, holding the request-serving process for
    ~6 minutes and causing app-wide 503s on completely unrelated endpoints.

    ``max_sessions_per_run`` alone doesn't bound this: it's an attempt-count
    cap, and an admin can (as usage did) opt out of it entirely, or a handful
    of unusually large sessions under a small cap can still blow the
    wall-clock. ``time_budget_seconds`` is a second, independent guard that
    bounds elapsed wall-clock time across the whole tick, applied to every
    processor (including ones exempted from the attempt cap) — once the
    budget is exceeded, the loop stops visiting new candidates and returns
    the stats gathered so far. Unlike the per-session
    ``VerificationProcessor`` budget (which raises), this must NOT raise: a
    partial tick that successfully processed some sessions is the normal,
    successful outcome, not an error."""

    def test_stops_early_and_leaves_remaining_for_next_tick(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        # Same user dir so scan order is deterministic (sorted glob within a
        # user dir; see test_skip_only_candidates_do_not_consume_the_budget).
        _seed_session(sessions, "alice", "a.jsonl")
        _seed_session(sessions, "alice", "b.jsonl")
        _seed_session(sessions, "alice", "c.jsonl")

        proc = _FakeProcessor(return_value=ProcessorResult(items_count=1))

        import services.session_pipeline.runner as runner_module

        # monotonic() sequence: [loop_start, check-before-a (under budget),
        # check-before-b (budget blown)] — "a" gets fully processed, "b"/"c"
        # never start.
        monkeypatch.setattr(
            runner_module.time,
            "monotonic",
            MagicMock(side_effect=[0.0, 0.0, 100.0 + 1]),
        )

        stats = run_processor(
            conn,
            proc,
            session_data_dir=sessions,
            time_budget_seconds=100.0,
        )

        assert stats["processed"] == 1
        assert stats["capped"] == 2
        assert proc.calls == ["alice/a.jsonl"]

        # Already-processed session is durably marked; the rest remain
        # eligible scan candidates for the next tick.
        repo = SessionProcessorStateRepository(conn)
        h = compute_file_hash(sessions / "alice" / "a.jsonl")
        assert repo.is_processed(proc.name, "alice/a.jsonl", h) is True
        assert repo.is_processed(proc.name, "alice/b.jsonl", "anything") is False
        conn.close()

    def test_deferred_sessions_are_picked_up_on_next_call(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        _seed_session(sessions, "alice", "a.jsonl")
        _seed_session(sessions, "alice", "b.jsonl")

        proc = _FakeProcessor(return_value=ProcessorResult(items_count=1))

        from unittest.mock import patch

        import services.session_pipeline.runner as runner_module

        # Scope the monotonic() patch to just the first call (via `with`,
        # not monkeypatch.setattr) — monkeypatch.undo() would also revert
        # the DATA_DIR env var the _fresh_db fixture set up, breaking the
        # second call's DB access. Real time is used for call 2, which
        # completes instantly and never approaches the 100s budget.
        with patch.object(
            runner_module.time,
            "monotonic",
            MagicMock(side_effect=[0.0, 0.0, 100.0 + 1]),
        ):
            stats1 = run_processor(
                conn,
                proc,
                session_data_dir=sessions,
                time_budget_seconds=100.0,
            )
        assert stats1["processed"] == 1
        assert stats1["capped"] == 1

        stats2 = run_processor(conn, proc, session_data_dir=sessions, time_budget_seconds=100.0)
        assert stats2["processed"] == 1
        assert stats2["capped"] == 0
        assert sorted(proc.calls) == ["alice/a.jsonl", "alice/b.jsonl"]
        conn.close()

    def test_none_disables_time_budget(self, tmp_path, monkeypatch):
        """Explicit opt-out preserves pre-existing unbounded-in-one-call
        behavior — same escape hatch as ``max_sessions_per_run=None``."""
        conn = _fresh_db(tmp_path, monkeypatch)
        sessions = tmp_path / "sessions"
        for i in range(5):
            _seed_session(sessions, f"user{i}", "s.jsonl")

        proc = _FakeProcessor(return_value=ProcessorResult(items_count=1))
        stats = run_processor(conn, proc, session_data_dir=sessions, time_budget_seconds=None)
        assert stats["processed"] == 5
        assert stats["capped"] == 0
        conn.close()

    def test_default_budget_is_applied_when_unset(self):
        """Every caller (including admin.py's usage-processor invocation,
        which does not pass this kwarg) gets a bounded tick by default —
        the fix must not require every call site to opt in."""
        import inspect

        import services.session_pipeline.runner as runner_module

        sig = inspect.signature(runner_module.run_processor)
        default = sig.parameters["time_budget_seconds"].default
        assert default == runner_module._DEFAULT_TIME_BUDGET_SECONDS
        assert default is not None
        assert 60 <= default <= 300


# ---------------------------------------------------------------------------
# v29 migration — verification rows preserved, old table dropped
# ---------------------------------------------------------------------------


class TestV29Migration:
    """Exercise the v28 → v29 migration directly. Builds a v28 schema (using
    the pre-v29 idiom inline so the test doesn't depend on _SYSTEM_SCHEMA's
    current shape), seeds data, runs the v29 migrations, asserts the result.
    """

    def test_existing_rows_become_verification_processor_rows(self, tmp_path):
        conn = duckdb.connect(":memory:")
        # Recreate the pre-v29 table shape — single-key session_file PK.
        conn.execute(
            """
            CREATE TABLE session_extraction_state (
                session_file VARCHAR PRIMARY KEY,
                username VARCHAR NOT NULL,
                processed_at TIMESTAMP DEFAULT current_timestamp,
                items_extracted INTEGER DEFAULT 0,
                file_hash VARCHAR
            )
            """
        )
        conn.execute(
            "INSERT INTO session_extraction_state VALUES (?, ?, ?, ?, ?)",
            ["alice/s1.jsonl", "alice", "2026-01-01 00:00:00", 3, "abc"],
        )

        # Run v29 migration steps via the helper (which conditionally copies
        # from the legacy table when present).
        from src.db import _v30_to_v31_migrate

        _v30_to_v31_migrate(conn)

        # New table has the row tagged with processor_name='verification'.
        rows = conn.execute(
            "SELECT processor_name, session_file, username, items_extracted, file_hash "
            "FROM session_processor_state ORDER BY session_file"
        ).fetchall()
        assert rows == [("verification", "alice/s1.jsonl", "alice", 3, "abc")]

        # Old table is gone.
        existing = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()
        }
        assert "session_extraction_state" not in existing
        assert "session_processor_state" in existing
        conn.close()

    def test_migration_idempotent_when_new_table_exists(self, tmp_path):
        """Fresh installs run _SYSTEM_SCHEMA (which already has session_processor_state)
        AND the migration ladder. The v29 migration must not crash if the new
        table already exists empty."""
        conn = duckdb.connect(":memory:")
        # Pre-create both tables (simulating fresh install + ladder rerun).
        conn.execute(
            """
            CREATE TABLE session_extraction_state (
                session_file VARCHAR PRIMARY KEY,
                username VARCHAR NOT NULL,
                processed_at TIMESTAMP,
                items_extracted INTEGER,
                file_hash VARCHAR
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE session_processor_state (
                processor_name VARCHAR NOT NULL,
                session_file VARCHAR NOT NULL,
                username VARCHAR NOT NULL,
                processed_at TIMESTAMP,
                items_extracted INTEGER,
                file_hash VARCHAR,
                PRIMARY KEY (processor_name, session_file)
            )
            """
        )

        from src.db import _v30_to_v31_migrate

        _v30_to_v31_migrate(conn)

        existing = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()
        }
        assert "session_extraction_state" not in existing
        assert "session_processor_state" in existing
        conn.close()
