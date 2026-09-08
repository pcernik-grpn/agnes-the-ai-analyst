"""LLM call instrumentation — one structured log record per generation.

The signal an operator needs from an LLM call is provider / model / token
counts / latency / whether it failed. It is emitted as a single structured
log record so it lands wherever the deployment already ships logs, with no
second sink and no vendor account to hold it.

Prompts and completions are deliberately NOT recorded: they routinely carry
customer data in this product, and a log pipeline is the wrong place for it.
Their sizes are, because a size is the part that explains a cost or a
latency.
"""

from __future__ import annotations

import logging

import pytest

from src.observability import trace_generation


def _records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event", None) == "llm_generation"]


def test_a_generation_emits_one_record_with_the_operator_facing_numbers(caplog):
    with caplog.at_level(logging.INFO):
        with trace_generation(provider="anthropic", model="claude-x") as trace:
            trace.set_tokens(input_tokens=120, output_tokens=45)

    (record,) = _records(caplog)
    assert record.provider == "anthropic"
    assert record.model == "claude-x"
    assert record.input_tokens == 120
    assert record.output_tokens == 45
    assert record.is_error is False
    assert isinstance(record.latency_ms, int) and record.latency_ms >= 0


def test_a_failing_generation_is_marked_and_the_error_still_propagates(caplog):
    with caplog.at_level(logging.INFO):
        with pytest.raises(RuntimeError, match="upstream is down"):
            with trace_generation(provider="anthropic", model="claude-x"):
                raise RuntimeError("upstream is down")

    (record,) = _records(caplog)
    assert record.is_error is True
    assert record.error_type == "RuntimeError"


def test_the_prompt_is_measured_never_recorded(caplog):
    """A char count explains a cost; the text is customer data."""
    secret = "patient Nováková, birth number 815623/1234"
    with caplog.at_level(logging.INFO):
        with trace_generation(provider="anthropic", model="claude-x") as trace:
            trace.set_input(secret)
            trace.set_output("the answer")

    (record,) = _records(caplog)
    assert record.prompt_chars == len(secret)
    assert record.completion_chars == len("the answer")
    serialized = repr(record.__dict__) + record.getMessage()
    assert "Nováková" not in serialized and "815623" not in serialized


def test_token_counts_are_read_off_an_anthropic_response(caplog):
    class _Usage:
        input_tokens = 11
        output_tokens = 22

    class _Response:
        usage = _Usage()
        content = [type("Block", (), {"type": "text", "text": "hi"})()]

    with caplog.at_level(logging.INFO):
        with trace_generation(provider="anthropic", model="claude-x") as trace:
            trace.set_output_from_anthropic(_Response())

    (record,) = _records(caplog)
    assert (record.input_tokens, record.output_tokens) == (11, 22)


def test_token_counts_are_read_off_an_openai_response(caplog):
    class _Usage:
        prompt_tokens = 7
        completion_tokens = 9

    class _Response:
        usage = _Usage()
        choices: list = []

    with caplog.at_level(logging.INFO):
        with trace_generation(provider="openai_compat", model="gpt-x") as trace:
            trace.set_output_from_openai(_Response())

    (record,) = _records(caplog)
    assert (record.input_tokens, record.output_tokens) == (7, 9)


def test_a_response_shape_it_cannot_read_never_breaks_the_call(caplog):
    """Instrumentation is not allowed to be the thing that fails a generation."""

    class _Hostile:
        @property
        def usage(self):  # noqa: ANN201
            raise ValueError("nope")

    with caplog.at_level(logging.INFO):
        with trace_generation(provider="anthropic", model="claude-x") as trace:
            trace.set_output_from_anthropic(_Hostile())
            result = "the call still returned"

    assert result == "the call still returned"
    (record,) = _records(caplog)
    assert record.is_error is False


# ---------------------------------------------------------------------------
# The call record: four token kinds, a purpose, a price, and the ledger
# ---------------------------------------------------------------------------


def test_cache_tokens_and_cost_are_read_off_an_anthropic_response(caplog):
    class _Usage:
        input_tokens = 1000
        output_tokens = 100
        cache_read_input_tokens = 5000
        cache_creation_input_tokens = 200

    class _Response:
        usage = _Usage()
        model = "claude-sonnet-5-20260101"
        stop_reason = "end_turn"
        content = [type("Block", (), {"type": "text", "text": "hi"})()]

    with caplog.at_level(logging.INFO):
        with trace_generation(provider="anthropic", model="claude-sonnet-5", purpose="unit") as trace:
            trace.set_output_from_anthropic(_Response())

    (record,) = _records(caplog)
    assert record.cache_read_tokens == 5000 and record.cache_creation_tokens == 200
    assert record.purpose == "unit"
    from src.llm_pricing import cost_usd

    assert record.cost_usd == round(
        cost_usd(
            model="claude-sonnet-5-20260101",
            input_tokens=1000,
            output_tokens=100,
            cache_read_tokens=5000,
            cache_creation_tokens=200,
        ),
        6,
    )


def test_openai_cached_prompt_tokens_are_split_out(caplog):
    class _Details:
        cached_tokens = 40

    class _Usage:
        prompt_tokens = 100
        completion_tokens = 9
        prompt_tokens_details = _Details()

    class _Response:
        usage = _Usage()
        model = "gpt-x"
        choices: list = []

    with caplog.at_level(logging.INFO):
        with trace_generation(provider="openai_compat", model="gpt-x") as trace:
            trace.set_output_from_openai(_Response())

    (record,) = _records(caplog)
    assert (record.input_tokens, record.cache_read_tokens) == (60, 40)


def test_purpose_and_workload_come_from_the_context_when_not_given(caplog):
    from src.observability.llm_context import llm_context

    with caplog.at_level(logging.INFO):
        with llm_context(workload="builder", purpose="entity_builder_turn", user_id="u9"):
            with trace_generation(provider="anthropic", model="m"):
                pass

    (record,) = _records(caplog)
    assert (record.workload, record.purpose, record.user_id) == ("builder", "entity_builder_turn", "u9")


def test_the_record_reaches_the_ledger(monkeypatch):
    import src.repositories as repos

    rows: list[dict] = []

    class _Repo:
        def insert_batch(self, batch):
            rows.extend(dict(r) for r in batch)
            return len(batch)

    monkeypatch.setattr(repos, "use_pg", lambda: True)
    monkeypatch.setattr(repos, "llm_calls_repo", lambda: _Repo(), raising=False)
    with trace_generation(provider="anthropic", model="claude-haiku-4-5", purpose="p") as trace:
        trace.set_tokens(10, 2)
    (row,) = rows
    assert row["kind"] == "generation" and row["purpose"] == "p" and row["input_tokens"] == 10
    assert row["trace_id"] is None  # export off: no span ids, row still written


def test_record_generation_marks_a_batch_result(caplog):
    from src.observability.llm_tracing import record_generation

    class _Usage:
        input_tokens = 10
        output_tokens = 5
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0

    with caplog.at_level(logging.INFO):
        record_generation(
            provider="anthropic",
            model="claude-haiku-4-5",
            purpose="facts_batch",
            usage=_Usage(),
            subject_id="file_1",
            batch=True,
        )
    (record,) = _records(caplog)
    assert record.purpose == "facts_batch" and record.input_tokens == 10 and record.is_error is False


def test_record_generation_carries_a_reported_error_without_raising(caplog):
    """A batch result that failed upstream is an error row, not an exception
    in the collector that reads it hours later."""
    from src.observability.llm_tracing import record_generation

    with caplog.at_level(logging.INFO):
        record_generation(
            provider="anthropic",
            model="claude-haiku-4-5",
            purpose="facts_batch",
            usage=None,
            error_type="overloaded_error",
        )
    (record,) = _records(caplog)
    assert record.is_error is True and record.error_type == "overloaded_error"


def test_the_capture_holds_the_texts_for_the_span_but_the_log_line_stays_sizes_only(caplog):
    """The span emitter is the only consumer of the text, and only under the
    content-export policy; the structured log record never carries it."""

    class _Msg:
        content = "the answer is 42"

    class _Choice:
        message = _Msg()
        finish_reason = "stop"

    class _Resp:
        choices = [_Choice()]
        usage = None
        model = "gpt-x"

    with caplog.at_level(logging.INFO):
        with trace_generation(provider="openai", model="gpt-x") as trace:
            trace.set_input("patient Nováková asks")
            trace.set_output_from_openai(_Resp())
            assert trace.prompt_text == "patient Nováková asks"
            assert trace.completion_text == "the answer is 42"

    (record,) = _records(caplog)
    assert record.prompt_chars == len("patient Nováková asks")
    assert record.completion_chars == len("the answer is 42")
    assert "Nováková" not in str(record.__dict__)


def test_a_non_string_prompt_leaves_no_text_to_export(caplog):
    """A messages list is measured, never reconstructed into an export text —
    the span's content events carry only the text shapes the capture can
    vouch for."""
    with caplog.at_level(logging.INFO):
        with trace_generation(provider="anthropic", model="claude-x") as trace:
            trace.set_input([{"role": "user", "content": "hi"}])
            trace.set_output({"blocks": 2})
            assert trace.prompt_text is None and trace.completion_text is None
            assert trace.prompt_chars and trace.completion_chars
