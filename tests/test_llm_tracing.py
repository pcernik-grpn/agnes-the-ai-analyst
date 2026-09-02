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
