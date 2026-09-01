"""Tests for ``src/memory_detection_logging.py`` (issue #1971 Part 3) — the
best-effort run-log writer both corporate-memory extractor paths call.
"""

from datetime import datetime, timezone


def test_policy_fingerprint_is_a_sha256_hex_digest():
    from src.memory_detection_logging import policy_fingerprint

    fp = policy_fingerprint("some policy text")
    assert fp is not None
    assert len(fp) == 64
    assert all(c in "0123456789abcdef" for c in fp)


def test_policy_fingerprint_is_deterministic():
    from src.memory_detection_logging import policy_fingerprint

    assert policy_fingerprint("same text") == policy_fingerprint("same text")
    assert policy_fingerprint("text a") != policy_fingerprint("text b")


def test_policy_fingerprint_none_for_none_input():
    """None means "no policy applies to this source" — distinct from an
    empty-string policy, which still hashes to a real digest."""
    from src.memory_detection_logging import policy_fingerprint

    assert policy_fingerprint(None) is None
    assert policy_fingerprint("") is not None


def test_record_detection_run_never_raises_when_backend_is_duckdb(monkeypatch):
    """The run path (verification processor, collector wrapper) must never
    break on a DuckDB-backed instance — memory_detection_runs is PG-only, so
    the repo factory raises RequiresPostgresBackend, and this helper must
    swallow it and return None rather than propagate."""
    from src.repository_errors import RequiresPostgresBackend

    def _raising_factory():
        raise RequiresPostgresBackend("memory_detection_runs")

    monkeypatch.setattr("src.repositories.memory_detection_runs_repo", _raising_factory)

    from src.memory_detection_logging import record_detection_run

    result = record_detection_run(
        source="session_transcripts",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        sessions_scanned=1,
    )
    assert result is None


def test_record_detection_run_swallows_any_other_exception_too(monkeypatch):
    def _raising_factory():
        raise RuntimeError("some other backend hiccup")

    monkeypatch.setattr("src.repositories.memory_detection_runs_repo", _raising_factory)

    from src.memory_detection_logging import record_detection_run

    result = record_detection_run(source="claude_local_md", started_at=datetime.now(timezone.utc))
    assert result is None


def test_record_detection_run_hashes_policy_text_never_stores_it_raw(monkeypatch):
    captured = {}

    class _FakeRepo:
        def create(self, **kwargs):
            captured.update(kwargs)
            return "mdr_fake"

    monkeypatch.setattr("src.repositories.memory_detection_runs_repo", lambda: _FakeRepo())

    from src.memory_detection_logging import policy_fingerprint, record_detection_run

    run_id = record_detection_run(
        source="session_transcripts",
        started_at=datetime.now(timezone.utc),
        policy_text="the secret policy prose",
    )
    assert run_id == "mdr_fake"
    assert captured["policy_fingerprint"] == policy_fingerprint("the secret policy prose")
    assert "the secret policy prose" not in captured.values()
