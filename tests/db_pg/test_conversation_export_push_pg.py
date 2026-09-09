"""``conversation-export`` worker job kind -- the push sink for the
conversation corpus export (design 2026-09-08 §3.12, Task 11).

PG-side by necessity, not by preference: the job resolves ``llm_calls``,
``chat_message_feedback`` and ``export_watermarks``, all Postgres-only
tables with no DuckDB sibling. A fake ``httpx.MockTransport`` stands in for
the destination collector so every test here runs with no real network
call.
"""

from __future__ import annotations

import contextlib
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


def _default_config(**overrides) -> dict:
    config = {
        "endpoint": "https://collector.example.com/ingest",
        "headers_secret_env": _HEADERS_ENV,
        "interval_minutes": 60,
        "surfaces": (),
    }
    config.update(overrides)
    return config


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
    monkeypatch.setattr(ic, "get_conversation_export_config", lambda: _default_config())
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
    return _latest_export_audit_row(pg_engine)[1]


def _latest_export_audit_row(pg_engine) -> tuple[str, dict]:
    with pg_engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT result, params FROM audit_log WHERE action = 'conversations.export' "
                "ORDER BY timestamp DESC LIMIT 1"
            )
        ).first()
    assert row is not None, "expected one conversations.export audit row"
    return row[0], row[1]


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
        watermark_ts, _cursor_id = watermark
        assert watermark_ts >= _BASE

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

    def test_second_run_after_success_sends_zero_records(self, pg_client, pg_engine):
        """The trailing conversation of a run must not be re-sent on the
        next tick -- the defect the keyset watermark fix closes."""
        from app.worker.kinds_conversation_export import run_conversation_export_once

        _seed_session(pg_engine, index=0)
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200)

        first = run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)
        assert first == {"sent": 1, "batches": 1, "batches_failed": 0}

        second = run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)
        assert second == {"sent": 0, "batches": 0, "batches_failed": 0}
        assert len(requests) == 1

    def test_forked_session_with_diverged_last_message_at_is_not_resent(self, pg_client, pg_engine):
        """A fork bumps ``chat_sessions.last_message_at`` to "now" while the
        message's own ``created_at`` (and thus a record's own
        ``conversation_end``) stays put -- exactly what
        ``chat_session_participants_pg.py``'s fork path does. The watermark
        must track the SESSION's own position, never anything derived from
        the record body, or this session is re-sent every tick forever."""
        from app.worker.kinds_conversation_export import run_conversation_export_once

        session_id = _seed_session(pg_engine, index=0)
        diverged_ts = _BASE + timedelta(hours=1)
        with pg_engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE chat_sessions SET last_message_at = :ts WHERE id = :id"),
                {"ts": diverged_ts, "id": session_id},
            )

        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200)

        first = run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)
        assert first["sent"] == 1
        assert len(requests) == 1

        second = run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)
        assert second == {"sent": 0, "batches": 0, "batches_failed": 0}
        assert len(requests) == 1

    def test_a_failure_on_batch_2_advances_the_key_only_through_batch_1(self, pg_client, pg_engine, monkeypatch):
        import app.worker.kinds_conversation_export as mod
        from src.repositories import export_watermarks_repo

        monkeypatch.setattr(mod, "MAX_BATCH_RECORDS", 1)
        id1 = _seed_session(pg_engine, index=0)
        _id2 = _seed_session(pg_engine, index=1)

        responses = iter(
            [
                httpx.Response(200),  # batch 1 (id1) -- succeeds
                httpx.Response(500),  # batch 2 (id2) -- exhausts every retry
                httpx.Response(500),
                httpx.Response(500),
                httpx.Response(500),
            ]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return next(responses)

        result = mod.run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)

        assert result == {"sent": 1, "batches": 1, "batches_failed": 1}
        watermark = export_watermarks_repo().get(mod.WATERMARK_NAME)
        assert watermark is not None
        _watermark_ts, cursor_id = watermark
        assert cursor_id == id1


class TestSurfacesFilterPushedIntoQuery:
    def test_a_filtered_out_surface_never_resurfaces_and_the_next_run_sends_zero(
        self, pg_client, pg_engine, monkeypatch
    ):
        """``surfaces=("web",)`` must narrow the QUERY itself, not just what
        gets POSTed -- the defect the query-level push-down fix closes. A
        newer, excluded-surface conversation must neither be delivered nor
        keep getting re-fetched and re-discarded on every subsequent tick."""
        import app.instance_config as ic
        from app.chat.types import Surface
        from src.repositories import chat_message_repo, chat_session_repo

        monkeypatch.setattr(ic, "get_conversation_export_config", lambda: _default_config(surfaces=("web",)))

        web_id = _seed_session(pg_engine, index=0, surface="web")
        slack_session = chat_session_repo().create_session(user_email="analyst@test.com", surface=Surface.SLACK_DM)
        chat_message_repo().append_message(session_id=slack_session.id, role="user", content="hi slack", turn_id="s1")
        slack_ts = _BASE + timedelta(minutes=1)  # newer than the web session
        with pg_engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE chat_sessions SET last_message_at = :ts WHERE id = :id"),
                {"ts": slack_ts, "id": slack_session.id},
            )
            conn.execute(
                sa.text("UPDATE chat_messages SET created_at = :ts WHERE session_id = :id"),
                {"ts": slack_ts, "id": slack_session.id},
            )

        from app.worker.kinds_conversation_export import run_conversation_export_once

        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200)

        first = run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)
        assert first == {"sent": 1, "batches": 1, "batches_failed": 0}
        assert len(requests) == 1
        (delivered,) = [json.loads(line) for line in requests[0].content.decode("utf-8").splitlines()]
        assert delivered["thread_id"] == web_id

        second = run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)
        assert second == {"sent": 0, "batches": 0, "batches_failed": 0}
        assert len(requests) == 1  # the slack session never triggers a delivery


class TestRetrySemantics:
    def test_connection_error_retries_with_exponential_backoff_and_leaves_the_watermark(self, pg_client, pg_engine):
        from app.worker.kinds_conversation_export import MAX_ATTEMPTS, WATERMARK_NAME, run_conversation_export_once
        from src.repositories import export_watermarks_repo

        _seed_session(pg_engine, index=0)
        attempts: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(request)
            raise httpx.ConnectError("boom", request=request)

        sleeps: list[float] = []
        result = run_conversation_export_once(client=_mock_client(handler), sleep=sleeps.append)

        assert MAX_ATTEMPTS == 4
        assert len(attempts) == 4
        assert sleeps == [1.0, 2.0, 4.0]
        assert result == {"sent": 0, "batches": 0, "batches_failed": 1}
        assert export_watermarks_repo().get(WATERMARK_NAME) is None

    @pytest.mark.parametrize("status", [400, 404, 429])
    def test_a_4xx_response_is_never_retried_within_a_run(self, pg_client, pg_engine, status):
        from app.worker.kinds_conversation_export import run_conversation_export_once

        _seed_session(pg_engine, index=0)
        attempts: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(request)
            return httpx.Response(status)

        sleeps: list[float] = []
        result = run_conversation_export_once(client=_mock_client(handler), sleep=sleeps.append)

        assert len(attempts) == 1
        assert sleeps == []
        assert result == {"sent": 0, "batches": 0, "batches_failed": 1}


class TestFailureHandling:
    def test_a_mid_walk_exception_does_not_raise_and_audits_a_failed_result(self, pg_client, pg_engine, monkeypatch):
        import app.worker.kinds_conversation_export as mod

        _seed_session(pg_engine, index=0)

        def _boom(*args, **kwargs):
            raise RuntimeError("unexpected failure")

        monkeypatch.setattr(mod, "iter_conversations", _boom)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        result = mod.run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)

        assert result == {"failed": "RuntimeError"}
        audit_result, params = _latest_export_audit_row(pg_engine)
        assert audit_result == "failed"
        assert params["error"] == "RuntimeError"
        assert params["delivery"] == "push"
        serialized = json.dumps(params)
        assert "unexpected failure" not in serialized  # class name only, never the message


class TestAdvisoryLease:
    def test_lock_held_skips_without_running_the_export(self, pg_client, pg_engine, monkeypatch):
        import app.worker.kinds_conversation_export as mod

        @contextlib.contextmanager
        def fake_lease():
            yield False

        monkeypatch.setattr("src.db_pg.conversation_export_lease", fake_lease)
        _seed_session(pg_engine, index=0)

        result = mod.run_conversation_export({})

        assert result == {"skipped": "lock_held"}


class TestContentMode:
    def test_pseudonymized_mode_transforms_text_and_marks_content_mode(self, pg_client, pg_engine, monkeypatch):
        import app.worker.kinds_conversation_export as mod

        monkeypatch.setattr(mod, "content_export_mode", lambda workload=None: "pseudonymized")
        monkeypatch.setattr(mod, "export_text", lambda text: text.replace("hello", "REDACTED") if text else text)
        _seed_session(pg_engine, index=0, content="hello world")

        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200)

        result = mod.run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)

        assert result["sent"] == 1
        (record,) = [json.loads(line) for line in requests[0].content.decode("utf-8").splitlines()]
        assert record["content_mode"] == "pseudonymized"
        assert "REDACTED" in record["first_user_message"]
        assert "hello" not in json.dumps(record)

    def test_real_content_export_mode_denies_the_chat_workload_via_policy_workloads(
        self, pg_client, pg_engine, monkeypatch
    ):
        """Uses the REAL ``content_export_mode`` (not monkeypatched to a
        fixed string) with a policy that excludes ``chat`` from its
        ``workloads`` allowlist -- proving the call site really passes
        ``workload="chat"`` rather than an argument-less call that would
        pass an allowlist-scoped policy through unfiltered."""
        import app.worker.kinds_conversation_export as mod
        from src.observability import content_policy as cp

        monkeypatch.setattr(mod, "content_export_mode", cp.content_export_mode)
        monkeypatch.setattr(
            cp,
            "load_content_export_policy",
            lambda config=None: cp.ContentExportPolicy(
                mode="full",
                placement="operator",
                basis="b",
                approved_by="a",
                approved_at="",
                requested_mode="full",
                warnings=(),
                workloads=("builder",),
            ),
        )
        _seed_session(pg_engine, index=0)
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            return httpx.Response(200)

        result = mod.run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)

        assert result == {"skipped": "content_export_disabled"}
        assert calls["count"] == 0


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
        monkeypatch.setattr(ic, "get_conversation_export_config", lambda: _default_config(headers_secret_env=None))

        result = mod.run_conversation_export_once(sleep=lambda *_: None)

        assert result == {"skipped": "requires_postgres_backend"}
