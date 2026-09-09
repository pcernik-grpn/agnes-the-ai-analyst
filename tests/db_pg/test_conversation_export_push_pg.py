"""``conversation-export`` worker job kind -- the push sink for the
conversation corpus export (design 2026-09-08 §3.12, Task 11).

PG-side by necessity, not by preference: the job resolves ``llm_calls``,
``chat_message_feedback`` and ``export_watermarks``, all Postgres-only
tables with no DuckDB sibling. A fake ``httpx.MockTransport`` stands in for
the destination collector so every test here runs with no real network
call.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa

_BASE = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
_HEADERS_ENV = "TEST_CONVERSATION_EXPORT_HEADERS"
_HEADERS_VALUE = "Authorization=Bearer secret-token-123"


@pytest.fixture
def pg_client(state_backend, seeded_app_both):
    """The PG half of ``seeded_app_both`` -- skips outright on DuckDB. The
    DuckDB-side contract (a stray enqueue swallows RequiresPostgresBackend
    cleanly) is covered separately below, without this skip."""
    if state_backend != "pg":
        pytest.skip("PG-only feature")
    return seeded_app_both


class _FakePolicy:
    def __init__(self, placement: str = "operator") -> None:
        self.placement = placement


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    """Default every test in this file to an ON, `full` content-export
    policy plus a configured push-sink endpoint -- individual tests
    override one or the other where the test is specifically about that
    gate."""
    import app.instance_config as ic
    import app.worker.kinds_conversation_export as mod

    monkeypatch.setattr(mod, "content_export_mode", lambda workload=None: "full")
    monkeypatch.setattr(mod, "load_content_export_policy", lambda: _FakePolicy())
    monkeypatch.setattr(
        ic,
        "get_conversation_export_config",
        lambda: {
            "endpoint": "https://collector.example.com/ingest",
            "headers_secret_env": _HEADERS_ENV,
            "interval_minutes": 60,
            "surfaces": (),
        },
    )
    monkeypatch.setenv(_HEADERS_ENV, _HEADERS_VALUE)


def _seed_session(
    pg_engine, *, index: int, user_email: str = "analyst@test.com", surface: str = "web", content: str | None = None
):
    """One chat session with one user message, `last_message_at` pinned to
    a deterministic, strictly-increasing timestamp -- mirrors
    `tests/db_pg/test_conversation_export_pg.py`'s helper of the same
    shape."""
    from app.chat.types import Surface
    from src.repositories import chat_message_repo, chat_session_repo

    session = chat_session_repo().create_session(user_email=user_email, surface=Surface(surface))
    chat_message_repo().append_message(
        session_id=session.id, role="user", content=content or f"hello {index}", turn_id=f"t{index}"
    )

    ts = _BASE + timedelta(minutes=index)
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE chat_sessions SET last_message_at = :ts WHERE id = :id"),
            {"ts": ts, "id": session.id},
        )
        conn.execute(
            sa.text("UPDATE chat_messages SET created_at = :ts WHERE session_id = :id"),
            {"ts": ts, "id": session.id},
        )
    return session.id


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _latest_export_audit_params(pg_engine) -> dict:
    with pg_engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT params FROM audit_log WHERE action = 'conversations.export' ORDER BY timestamp DESC LIMIT 1"
            )
        ).first()
    assert row is not None, "expected one conversations.export audit row"
    return row[0]


class TestWatermarkAdvancesOnlyOn2xx:
    def test_a_successful_batch_advances_the_watermark(self, pg_client, pg_engine):
        from app.worker.kinds_conversation_export import WATERMARK_NAME, run_conversation_export_once
        from src.repositories import export_watermarks_repo

        _seed_session(pg_engine, index=0)
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200)

        result = run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)

        assert result == {"sent": 1, "batches": 1, "batches_failed": 0}
        assert len(requests) == 1
        watermark = export_watermarks_repo().get(WATERMARK_NAME)
        assert watermark is not None
        assert watermark >= _BASE

    def test_a_500_leaves_the_watermark_and_retries(self, pg_client, pg_engine):
        from app.worker.kinds_conversation_export import MAX_ATTEMPTS, WATERMARK_NAME, run_conversation_export_once
        from src.repositories import export_watermarks_repo

        _seed_session(pg_engine, index=0)
        attempts: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(request)
            return httpx.Response(500)

        result = run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)

        assert len(attempts) == MAX_ATTEMPTS
        assert result == {"sent": 0, "batches": 0, "batches_failed": 1}
        assert export_watermarks_repo().get(WATERMARK_NAME) is None


class TestBatching:
    def test_batches_split_at_200_records(self, pg_client, pg_engine):
        from app.worker.kinds_conversation_export import MAX_BATCH_RECORDS, run_conversation_export_once

        total = MAX_BATCH_RECORDS + 50
        for i in range(total):
            _seed_session(pg_engine, index=i)

        batch_sizes: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            lines = request.content.decode("utf-8").strip("\n").split("\n")
            batch_sizes.append(len(lines))
            return httpx.Response(200)

        result = run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)

        assert result["sent"] == total
        assert batch_sizes == [MAX_BATCH_RECORDS, 50]

    def test_batches_split_at_8_mib(self, pg_client, pg_engine):
        from app.worker.kinds_conversation_export import MAX_BATCH_BYTES, run_conversation_export_once

        # Each record duplicates its message content (once in
        # `messages_json`, once in `first_user_message`), so a ~3 MB
        # message produces a ~6 MB record: comfortably under the 8 MiB cap
        # alone, but the PAIR together (~12 MB) is not -- forces a split
        # after the first record.
        big = "x" * 3_000_000
        _seed_session(pg_engine, index=0, content=big)
        _seed_session(pg_engine, index=1, content=big)

        batch_byte_sizes: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            batch_byte_sizes.append(len(request.content))
            return httpx.Response(200)

        result = run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)

        assert result["sent"] == 2
        assert result["batches"] == 2
        assert all(size <= MAX_BATCH_BYTES for size in batch_byte_sizes)


class TestHeadersAndAudit:
    def test_headers_come_from_the_env_var_and_never_appear_in_the_audit_row(self, pg_client, pg_engine):
        from app.worker.kinds_conversation_export import run_conversation_export_once

        _seed_session(pg_engine, index=0)
        seen_headers: list[httpx.Headers] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_headers.append(request.headers)
            return httpx.Response(200)

        run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)

        assert seen_headers[0]["authorization"] == "Bearer secret-token-123"

        params = _latest_export_audit_params(pg_engine)
        assert params["delivery"] == "push"
        assert params["count"] == 1
        assert params["endpoint_host"] == "collector.example.com"
        serialized = json.dumps(params)
        assert "secret-token-123" not in serialized
        assert "Authorization" not in serialized
        assert "headers" not in params


class TestContentPolicyGate:
    def test_policy_off_makes_no_request_and_never_moves_the_watermark(self, pg_client, pg_engine, monkeypatch):
        import app.worker.kinds_conversation_export as mod
        from src.repositories import export_watermarks_repo

        monkeypatch.setattr(mod, "content_export_mode", lambda workload=None: "off")
        _seed_session(pg_engine, index=0)
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            return httpx.Response(200)

        result = mod.run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)

        assert result == {"skipped": "content_export_disabled"}
        assert calls["count"] == 0
        assert export_watermarks_repo().get(mod.WATERMARK_NAME) is None

    def test_not_configured_makes_no_request(self, pg_client, pg_engine, monkeypatch):
        import app.instance_config as ic
        import app.worker.kinds_conversation_export as mod

        monkeypatch.setattr(ic, "get_conversation_export_config", lambda: None)
        _seed_session(pg_engine, index=0)
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            return httpx.Response(200)

        result = mod.run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)

        assert result == {"skipped": "not_configured"}
        assert calls["count"] == 0


class TestDuckDbSwallowsCleanly:
    def test_run_once_swallows_requires_postgres_backend_on_duckdb(self, state_backend, tmp_path, monkeypatch):
        """A DuckDB-backed instance never actually pushes anything -- the
        handler resolves llm_calls_repo() (PG-only) first and swallows the
        resulting RequiresPostgresBackend into a clean, logged skip."""
        if state_backend != "duckdb":
            pytest.skip("PG side covered by the rest of this file")

        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import app.instance_config as ic
        import app.worker.kinds_conversation_export as mod

        monkeypatch.setattr(mod, "content_export_mode", lambda workload=None: "full")
        monkeypatch.setattr(
            ic,
            "get_conversation_export_config",
            lambda: {
                "endpoint": "https://collector.example.com/ingest",
                "headers_secret_env": None,
                "interval_minutes": 60,
                "surfaces": (),
            },
        )

        result = mod.run_conversation_export_once(sleep=lambda *_: None)

        assert result == {"skipped": "requires_postgres_backend"}
