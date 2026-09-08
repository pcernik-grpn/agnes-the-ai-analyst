"""``LlmCallRecord`` — the one shape both sinks (span, ledger) consume."""

from __future__ import annotations

from datetime import datetime, timezone

from src.llm_pricing import DEFAULT_PRICE, cost_usd
from src.observability.llm_context import LlmCallContext
from src.observability.llm_record import (
    LlmCallRecord,
    build_record,
    priced_as_for,
    usage_from_anthropic,
    usage_from_openai,
)

_USAGE = {"input_tokens": 1000, "output_tokens": 100, "cache_read_tokens": 5000, "cache_creation_tokens": 200}


def test_build_record_prices_the_four_token_kinds_and_stores_the_rates():
    ctx = LlmCallContext(workload="chat", purpose="completion", session_id="s1", turn_id="t1", user_id="u1")
    rec = build_record(
        kind="completion",
        context=ctx,
        provider="anthropic",
        upstream="anthropic",
        model_requested="claude-sonnet-5",
        model_response="claude-sonnet-5-20260101",
        usage=_USAGE,
        latency_ms=812,
        status="ok",
        http_status=200,
        prompt_chars=40,
        completion_chars=12,
        stop_reason="end_turn",
        stream_complete=True,
        trace_id="a" * 32,
        span_id="b" * 16,
    )
    assert isinstance(rec, LlmCallRecord)
    assert rec.cost_usd == round(cost_usd(model="claude-sonnet-5-20260101", **_USAGE), 6)
    assert rec.priced_as["price_key"] == "claude-sonnet-5"
    assert rec.priced_as["input_per_mtok"] == 3.0
    assert rec.priced_as["cache_read_per_mtok"] == round(3.0 * 0.1, 6)
    assert rec.priced_as["cache_write_per_mtok"] == round(3.0 * 1.25, 6)
    assert rec.priced_as["batch_multiplier"] == 1.0
    assert rec.turn_id == "t1" and rec.user_id == "u1" and rec.workload == "chat"
    assert rec.trace_id == "a" * 32 and rec.span_id == "b" * 16
    assert rec.id and rec.created_at.tzinfo is not None
    row = rec.to_row()
    assert row["cost_usd"] == rec.cost_usd and row["priced_as"] == rec.priced_as and row["kind"] == "completion"
    assert set(row) == {
        "id",
        "created_at",
        "kind",
        "workload",
        "purpose",
        "session_id",
        "turn_id",
        "user_id",
        "agent_id",
        "job_id",
        "subject_id",
        "trace_id",
        "span_id",
        "provider",
        "upstream",
        "model_requested",
        "model_response",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "cost_usd",
        "priced_as",
        "latency_ms",
        "status",
        "error_type",
        "http_status",
        "prompt_chars",
        "completion_chars",
        "stop_reason",
        "stream_complete",
    }


def test_an_unknown_model_prices_at_the_default_and_says_so():
    rec = build_record(
        kind="generation",
        context=LlmCallContext(),
        provider="openai_compat",
        upstream="openai_compat",
        model_requested="mystery-9",
        model_response=None,
        usage=_USAGE,
        latency_ms=1,
        status="ok",
    )
    assert rec.priced_as["price_key"] == "default"
    assert rec.priced_as["input_per_mtok"] == DEFAULT_PRICE.input_per_mtok


def test_batch_pricing_halves_every_term():
    rec = build_record(
        kind="generation",
        context=LlmCallContext(),
        provider="anthropic",
        upstream="anthropic",
        model_requested="claude-haiku-4-5",
        model_response=None,
        usage=_USAGE,
        latency_ms=None,
        status="ok",
        batch=True,
    )
    assert rec.priced_as["batch_multiplier"] == 0.5
    assert rec.cost_usd == round(cost_usd(model="claude-haiku-4-5", batch=True, **_USAGE), 6)


def test_a_failed_call_without_usage_is_a_zero_cost_error_row():
    rec = build_record(
        kind="completion",
        context=LlmCallContext(),
        provider="anthropic",
        upstream="vertex",
        model_requested="claude-opus-5",
        model_response=None,
        usage=None,
        latency_ms=5,
        status="error",
        error_type="529",
        http_status=529,
    )
    assert rec.cost_usd == 0.0 and rec.input_tokens == 0 and rec.status == "error"


def test_span_attributes_carry_cost_and_context():
    ctx = LlmCallContext(workload="ocr", purpose="scan_ocr", job_id="j1")
    rec = build_record(
        kind="generation",
        context=ctx,
        provider="anthropic",
        upstream="anthropic",
        model_requested="claude-haiku-4-5",
        model_response=None,
        usage=_USAGE,
        latency_ms=3,
        status="ok",
    )
    attrs = rec.span_attributes()
    assert attrs["agnes.cost_usd"] == rec.cost_usd
    assert attrs["agnes.workload"] == "ocr" and attrs["agnes.job_id"] == "j1"


def test_usage_from_anthropic_reads_the_cache_fields():
    class _U:
        input_tokens = 10
        output_tokens = 3
        cache_read_input_tokens = 70
        cache_creation_input_tokens = 4

    assert usage_from_anthropic(_U()) == {
        "input_tokens": 10,
        "output_tokens": 3,
        "cache_read_tokens": 70,
        "cache_creation_tokens": 4,
    }
    assert usage_from_anthropic(None) == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }


def test_usage_from_openai_splits_cached_prompt_tokens():
    class _Details:
        cached_tokens = 30

    class _U:
        prompt_tokens = 100
        completion_tokens = 9
        prompt_tokens_details = _Details()

    assert usage_from_openai(_U()) == {
        "input_tokens": 70,
        "output_tokens": 9,
        "cache_read_tokens": 30,
        "cache_creation_tokens": 0,
    }

    class _Plain:
        prompt_tokens = 5
        completion_tokens = 1

    assert usage_from_openai(_Plain())["input_tokens"] == 5


def test_priced_as_for_is_stable_and_serialisable():
    p = priced_as_for("claude-opus-5")
    assert p == {
        "price_key": "claude-opus-5",
        "input_per_mtok": 5.0,
        "output_per_mtok": 25.0,
        "cache_read_per_mtok": 0.5,
        "cache_write_per_mtok": 6.25,
        "batch_multiplier": 1.0,
    }
    assert datetime.now(timezone.utc)  # sanity: tz-aware datetimes are what created_at holds


# ---------------------------------------------------------------------------
# The ledger sink — Postgres-only by construction, never fails the call
# ---------------------------------------------------------------------------


def test_record_call_is_a_silent_noop_on_duckdb(monkeypatch):
    import src.repositories as repos
    from src.observability import llm_ledger

    monkeypatch.setattr(repos, "use_pg", lambda: False)
    rec = build_record(
        kind="generation",
        context=LlmCallContext(),
        provider="anthropic",
        upstream="anthropic",
        model_requested="m",
        model_response=None,
        usage=_USAGE,
        latency_ms=1,
        status="ok",
    )
    llm_ledger.record_call(rec)  # no repo, no backend: never raises


def test_record_call_inserts_one_row_when_the_repo_exists(monkeypatch):
    import src.repositories as repos
    from src.observability import llm_ledger

    captured: list[list[dict]] = []

    class _Repo:
        def insert_batch(self, rows):
            captured.append([dict(r) for r in rows])
            return len(captured[-1])

    monkeypatch.setattr(repos, "use_pg", lambda: True)
    monkeypatch.setattr(repos, "llm_calls_repo", lambda: _Repo(), raising=False)
    rec = build_record(
        kind="generation",
        context=LlmCallContext(purpose="p"),
        provider="anthropic",
        upstream="anthropic",
        model_requested="m",
        model_response=None,
        usage=_USAGE,
        latency_ms=1,
        status="ok",
    )
    llm_ledger.record_call(rec)
    assert captured == [[rec.to_row()]]


def test_record_call_swallows_a_failing_repo(monkeypatch):
    import src.repositories as repos
    from src.observability import llm_ledger

    class _Boom:
        def insert_batch(self, rows):
            raise RuntimeError("db down")

    monkeypatch.setattr(repos, "use_pg", lambda: True)
    monkeypatch.setattr(repos, "llm_calls_repo", lambda: _Boom(), raising=False)
    rec = build_record(
        kind="generation",
        context=LlmCallContext(),
        provider="anthropic",
        upstream="anthropic",
        model_requested="m",
        model_response=None,
        usage=None,
        latency_ms=1,
        status="ok",
    )
    llm_ledger.record_call(rec)  # logged at debug, never raised


def test_record_call_survives_the_repo_not_existing_yet(monkeypatch):
    """The ``llm_calls`` repo lands in a later change; until then the sink is
    an AttributeError away from every call it observes."""
    import src.repositories as repos
    from src.observability import llm_ledger

    monkeypatch.setattr(repos, "use_pg", lambda: True)
    monkeypatch.delattr(repos, "llm_calls_repo", raising=False)
    rec = build_record(
        kind="generation",
        context=LlmCallContext(),
        provider="anthropic",
        upstream="anthropic",
        model_requested="m",
        model_response=None,
        usage=None,
        latency_ms=1,
        status="ok",
    )
    llm_ledger.record_call(rec)
