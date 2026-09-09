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
    pg_engine,
    *,
    index: int,
    user_email: str = "analyst@test.com",
    surface: str = "web",
    content: str | None = None,
    ts: datetime | None = None,
):
    """One chat session with one user message, `last_message_at` pinned to
    a deterministic, strictly-increasing timestamp -- mirrors
    `tests/db_pg/test_conversation_export_pg.py`'s helper of the same
    shape. `ts` overrides the default `_BASE`-relative timestamp for tests
    that need a NOW-relative one (the settle-window tests below)."""
    from app.chat.types import Surface
    from src.repositories import chat_message_repo, chat_session_repo

    session = chat_session_repo().create_session(user_email=user_email, surface=Surface(surface))
    chat_message_repo().append_message(
        session_id=session.id, role="user", content=content or f"hello {index}", turn_id=f"t{index}"
    )

    ts = ts if ts is not None else _BASE + timedelta(minutes=index)
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


def _watermark_name_for(config: dict | None = None) -> str:
    """The `export_watermarks.name` row a given delivery config resolves
    to -- defaults to `_default_config()` so most tests can call this with
    no arguments."""
    from app.worker.kinds_conversation_export import watermark_name

    cfg = config or _default_config()
    surfaces = tuple(cfg["surfaces"]) if cfg["surfaces"] else ()
    return watermark_name(cfg["endpoint"], surfaces)


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
        from app.worker.kinds_conversation_export import run_conversation_export_once
        from src.repositories import export_watermarks_repo

        _seed_session(pg_engine, index=0)
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200)

        result = run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)

        assert result == {"sent": 1, "batches": 1, "batches_failed": 0, "oversized_skipped": 0}
        assert len(requests) == 1
        watermark = export_watermarks_repo().get(_watermark_name_for())
        assert watermark is not None
        watermark_ts, _cursor_id = watermark
        assert watermark_ts >= _BASE

    def test_a_500_leaves_the_watermark_and_retries(self, pg_client, pg_engine):
        from app.worker.kinds_conversation_export import MAX_ATTEMPTS, run_conversation_export_once
        from src.repositories import export_watermarks_repo

        _seed_session(pg_engine, index=0)
        attempts: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(request)
            return httpx.Response(500)

        result = run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)

        assert len(attempts) == MAX_ATTEMPTS
        assert result == {"sent": 0, "batches": 0, "batches_failed": 1, "oversized_skipped": 0}
        assert export_watermarks_repo().get(_watermark_name_for()) is None

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
        assert first == {"sent": 1, "batches": 1, "batches_failed": 0, "oversized_skipped": 0}

        second = run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)
        assert second == {"sent": 0, "batches": 0, "batches_failed": 0, "oversized_skipped": 0}
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
        assert second == {"sent": 0, "batches": 0, "batches_failed": 0, "oversized_skipped": 0}
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

        assert result == {"sent": 1, "batches": 1, "batches_failed": 1, "oversized_skipped": 0}
        watermark = export_watermarks_repo().get(_watermark_name_for())
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
        assert first == {"sent": 1, "batches": 1, "batches_failed": 0, "oversized_skipped": 0}
        assert len(requests) == 1
        (delivered,) = [json.loads(line) for line in requests[0].content.decode("utf-8").splitlines()]
        assert delivered["thread_id"] == web_id

        second = run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)
        assert second == {"sent": 0, "batches": 0, "batches_failed": 0, "oversized_skipped": 0}
        assert len(requests) == 1  # the slack session never triggers a delivery


class TestRetrySemantics:
    def test_connection_error_retries_with_exponential_backoff_and_leaves_the_watermark(self, pg_client, pg_engine):
        from app.worker.kinds_conversation_export import MAX_ATTEMPTS, run_conversation_export_once
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
        assert result == {"sent": 0, "batches": 0, "batches_failed": 1, "oversized_skipped": 0}
        assert export_watermarks_repo().get(_watermark_name_for()) is None

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
        assert result == {"sent": 0, "batches": 0, "batches_failed": 1, "oversized_skipped": 0}


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
        assert audit_result == "error:RuntimeError"
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


class TestOversizedRecordNeverBlocksTheCorpus:
    """Review finding: a single conversation whose own ndjson line exceeds
    the advertised batch cap used to be posted anyway. A collector that
    enforces the cap rejects it on every run, and the watermark never gets
    past it -- one outsized transcript blocks the whole corpus forever."""

    def test_an_oversized_record_is_skipped_and_the_ones_behind_it_still_deliver(
        self, pg_client, pg_engine, monkeypatch
    ):
        from app.worker import kinds_conversation_export as mod
        from src.repositories import export_watermarks_repo

        # A cap far below 8 MiB so the test does not have to build a real
        # 8 MiB transcript: an ordinary record is a couple of KB and fits,
        # the padded one does not.
        monkeypatch.setattr(mod, "MAX_BATCH_BYTES", 20_000)
        _seed_session(pg_engine, index=0, content="x" * 40_000)
        _seed_session(pg_engine, index=1, content="hi")

        posted: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            posted.append(request.content)
            return httpx.Response(200)

        result = mod.run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)

        assert result["oversized_skipped"] == 1
        assert all(len(body) <= 20_000 for body in posted), "an over-contract body was posted anyway"
        assert b"hi" in b"".join(posted), "the record behind the oversized one never arrived"
        # The watermark moved past BOTH, so the next run does not re-walk
        # the oversized record forever.
        watermark = export_watermarks_repo().get(_watermark_name_for())
        assert watermark is not None
        posted.clear()
        again = mod.run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)
        assert again["sent"] == 0 and posted == []

    def test_the_skip_is_counted_in_the_audit_row(self, pg_client, pg_engine, monkeypatch):
        from app.worker import kinds_conversation_export as mod

        monkeypatch.setattr(mod, "MAX_BATCH_BYTES", 20_000)
        _seed_session(pg_engine, index=0, content="x" * 40_000)

        mod.run_conversation_export_once(client=_mock_client(lambda _r: httpx.Response(200)), sleep=lambda *_: None)
        params = _latest_export_audit_params(pg_engine)
        assert params["oversized_skipped"] == 1
        assert params["count"] == 0


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
        assert export_watermarks_repo().get(_watermark_name_for()) is None

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


class TestWatermarkTracksDeliveryConfig:
    """The watermark identity is a function of ``endpoint``/``surfaces`` --
    the defect fix for an operator who repoints the endpoint or widens the
    surfaces allowlist and would otherwise silently resume the OLD
    configuration's cursor, never re-delivering anything completed before
    the change to the new destination / newly-included surface."""

    def test_changing_the_endpoint_redelivers_everything_to_the_new_endpoint(self, pg_client, pg_engine, monkeypatch):
        import app.instance_config as ic
        from app.worker.kinds_conversation_export import run_conversation_export_once

        _seed_session(pg_engine, index=0)
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200)

        first = run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)
        assert first == {"sent": 1, "batches": 1, "batches_failed": 0, "oversized_skipped": 0}
        assert len(requests) == 1

        new_endpoint = "https://new-collector.example.com/ingest"
        monkeypatch.setattr(ic, "get_conversation_export_config", lambda: _default_config(endpoint=new_endpoint))

        second = run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)

        assert second == {"sent": 1, "batches": 1, "batches_failed": 0, "oversized_skipped": 0}
        assert len(requests) == 2
        assert str(requests[1].url) == new_endpoint

    def test_widening_surfaces_delivers_the_previously_excluded_conversation(self, pg_client, pg_engine, monkeypatch):
        import app.instance_config as ic
        from app.chat.types import Surface
        from app.worker.kinds_conversation_export import run_conversation_export_once
        from src.repositories import chat_message_repo, chat_session_repo

        monkeypatch.setattr(ic, "get_conversation_export_config", lambda: _default_config(surfaces=("web",)))

        web_id = _seed_session(pg_engine, index=0, surface="web")
        slack_session = chat_session_repo().create_session(user_email="analyst@test.com", surface=Surface.SLACK_DM)
        chat_message_repo().append_message(session_id=slack_session.id, role="user", content="hi slack", turn_id="s1")
        slack_ts = _BASE + timedelta(minutes=1)
        with pg_engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE chat_sessions SET last_message_at = :ts WHERE id = :id"),
                {"ts": slack_ts, "id": slack_session.id},
            )
            conn.execute(
                sa.text("UPDATE chat_messages SET created_at = :ts WHERE session_id = :id"),
                {"ts": slack_ts, "id": slack_session.id},
            )

        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200)

        first = run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)
        assert first == {"sent": 1, "batches": 1, "batches_failed": 0, "oversized_skipped": 0}
        (delivered,) = [json.loads(line) for line in requests[0].content.decode("utf-8").splitlines()]
        assert delivered["thread_id"] == web_id

        monkeypatch.setattr(ic, "get_conversation_export_config", lambda: _default_config(surfaces=("web", "slack_dm")))

        second = run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)

        # The widened surfaces list is a NEW delivery configuration -- its
        # watermark starts fresh at the epoch, so the run re-delivers
        # everything that now matches the filter (both sessions), not only
        # the newly-included one. That IS the fix: the slack conversation,
        # previously unreachable under any cursor, is finally delivered.
        assert second == {"sent": 2, "batches": 1, "batches_failed": 0, "oversized_skipped": 0}
        assert len(requests) == 2
        delivered2 = {json.loads(line)["thread_id"] for line in requests[1].content.decode("utf-8").splitlines()}
        assert delivered2 == {web_id, slack_session.id}


class TestSettleWindow:
    """A conversation is walked only once its `last_message_at` is at
    least `SETTLE_WINDOW` old -- the defect fix for a push tick that lands
    between a user message and its still-pending assistant answer."""

    def test_a_message_one_minute_old_is_not_exported_and_the_watermark_does_not_move(self, pg_client, pg_engine):
        from app.worker.kinds_conversation_export import run_conversation_export_once
        from src.repositories import export_watermarks_repo

        now = datetime.now(UTC)
        _seed_session(pg_engine, index=0, ts=now - timedelta(minutes=1))
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200)

        result = run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)

        assert result == {"sent": 0, "batches": 0, "batches_failed": 0, "oversized_skipped": 0}
        assert len(requests) == 0
        assert export_watermarks_repo().get(_watermark_name_for()) is None

    def test_a_message_ten_minutes_old_is_exported(self, pg_client, pg_engine):
        from app.worker.kinds_conversation_export import run_conversation_export_once

        now = datetime.now(UTC)
        _seed_session(pg_engine, index=0, ts=now - timedelta(minutes=10))
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200)

        result = run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)

        assert result == {"sent": 1, "batches": 1, "batches_failed": 0, "oversized_skipped": 0}
        assert len(requests) == 1

    def test_a_message_inside_the_window_is_exported_once_the_window_passes(self, pg_client, pg_engine, monkeypatch):
        """An interrupted turn (a user message that never gets answered)
        is left for a later tick, not skipped forever -- once the settle
        window has passed the SAME session is exported by the next run."""
        import app.worker.kinds_conversation_export as mod

        base_now = datetime.now(UTC)
        _seed_session(pg_engine, index=0, ts=base_now - timedelta(minutes=1))
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200)

        class _FrozenDatetime(datetime):
            _fixed = base_now

            @classmethod
            def now(cls, tz=None):
                return cls._fixed if tz is None else cls._fixed.astimezone(tz)

        monkeypatch.setattr(mod, "datetime", _FrozenDatetime)

        first = mod.run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)
        assert first == {"sent": 0, "batches": 0, "batches_failed": 0, "oversized_skipped": 0}
        assert len(requests) == 0

        _FrozenDatetime._fixed = base_now + timedelta(minutes=6)
        second = mod.run_conversation_export_once(client=_mock_client(handler), sleep=lambda *_: None)

        assert second == {"sent": 1, "batches": 1, "batches_failed": 0, "oversized_skipped": 0}
        assert len(requests) == 1


class TestEndpointHostAllowlist:
    """Review finding (SSRF): the sink posts a secret header plus customer
    conversations to an operator-chosen URL. With
    ``AGNES_REMOTE_ATTACH_HOST_ALLOWLIST`` set -- the repo's one egress
    control for credentialed outbound requests -- a destination host off
    the list ends the run before the secret is even resolved."""

    def test_a_host_off_the_allowlist_makes_no_request_and_leaves_no_watermark(self, pg_client, pg_engine, monkeypatch):
        from app.worker.kinds_conversation_export import run_conversation_export_once
        from src.repositories import export_watermarks_repo

        monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", "other.example.com")
        _seed_session(pg_engine, index=1)
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200)

        result = run_conversation_export_once(client=_mock_client(handler), sleep=lambda _s: None)
        assert result == {"skipped": "endpoint_not_allowlisted"}
        assert calls == []
        assert export_watermarks_repo().get(_watermark_name_for()) is None

    def test_a_host_on_the_allowlist_is_delivered(self, pg_client, pg_engine, monkeypatch):
        from app.worker.kinds_conversation_export import run_conversation_export_once

        monkeypatch.setenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", "collector.example.com, other.example.com")
        _seed_session(pg_engine, index=1)
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200)

        result = run_conversation_export_once(client=_mock_client(handler), sleep=lambda _s: None)
        assert "skipped" not in result, result
        assert len(calls) == 1
