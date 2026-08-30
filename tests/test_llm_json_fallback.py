"""A failed model call has to say WHICH failure it was.

Written after an afternoon spent guessing at one. Every distinguishable
cause — a rejected credential, a model the deployment has never heard of, a
refused request, a rate limit — arrived at the builder as the same 502
reading "The assistant could not answer. Try again." Three of those four
never get better on a retry, and the only place the real cause existed was
a container log the person looking at the screen could not reach.

Two halves, and they are the same idea at two layers: the provider types
what the API told it, and the builders turn that type into something an
operator can act on without a shell.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import anthropic
import pytest

from connectors.llm.anthropic_provider import AnthropicExtractor
from connectors.llm.exceptions import (
    LLMAuthError,
    LLMModelNotFoundError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMUnsupportedError,
)

SCHEMA = {
    "type": "object",
    "properties": {"reply": {"type": "string"}},
    "required": ["reply"],
}


def _response(text: str, stop_reason: str = "end_turn"):
    block = MagicMock()
    block.text = text
    response = MagicMock()
    response.content = [block]
    response.stop_reason = stop_reason
    return response


def _api_error(cls, status: int, message: str = "nope"):
    return cls(message=message, response=MagicMock(status_code=status), body=None)


def _extractor():
    return AnthropicExtractor(api_key="sk-ant-test", model="claude-haiku-4-5-20251001")


class TestTheProviderTypesWhatItWasTold:
    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_an_unknown_model_is_typed_and_names_the_model(self, mock_cls):
        """404 used to escape as a bare SDK exception. On Vertex it is the
        likeliest real cause: the model is enabled in Model Garden for a
        different project/region than the one configured."""
        client = MagicMock()
        mock_cls.return_value = client
        client.messages.create.side_effect = _api_error(anthropic.NotFoundError, 404, "model not found")

        with pytest.raises(LLMModelNotFoundError, match="claude-haiku-4-5-20251001"):
            _extractor().extract_json("hi", 4000, SCHEMA, "builder_turn")

        assert client.messages.create.call_count == 1, "a missing model is not retried"

    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_a_rejected_request_carries_the_providers_own_words(self, mock_cls):
        """400 is the one failure where the provider says which part it
        disliked. Dropping that text is what made this class of bug take a
        server log to diagnose."""
        client = MagicMock()
        mock_cls.return_value = client
        client.messages.create.side_effect = _api_error(
            anthropic.BadRequestError, 400, "output_config is not supported here"
        )

        with pytest.raises(LLMUnsupportedError, match="output_config is not supported here"):
            _extractor().extract_json("hi", 4000, SCHEMA, "builder_turn")

        assert client.messages.create.call_count == 1, "a refused request is not retried"

    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_a_working_provider_is_untouched(self, mock_cls):
        client = MagicMock()
        mock_cls.return_value = client
        client.messages.create.return_value = _response(json.dumps({"reply": "ok"}))

        assert _extractor().extract_json("hi", 4000, SCHEMA, "builder_turn") == {"reply": "ok"}
        assert client.messages.create.call_count == 1
        assert "output_config" in client.messages.create.call_args.kwargs, (
            "native structured output is still the only strategy"
        )


class TestVertexInheritsTheTyping:
    """The Vertex subclass overrides only the client and the model-id
    spelling — and it is the deployment where a 404 is most likely."""

    @patch("connectors.llm.vertex_provider.create_vertex_client")
    def test_vertex_reports_a_missing_model_the_same_way(self, mock_create):
        from connectors.llm.vertex_provider import VertexExtractor

        client = MagicMock()
        mock_create.return_value = client
        client.messages.create.side_effect = _api_error(anthropic.NotFoundError, 404)

        ext = VertexExtractor(project_id="p", region="global", model="claude-haiku-4-5-20251001")
        with pytest.raises(LLMModelNotFoundError, match="claude-haiku-4-5@20251001"):
            ext.extract_json("hi", 4000, SCHEMA, "builder_turn")


class TestBuilderTurnFailureNamesTheCause:
    """``builder_core.turn_failure`` — one shape for all four builders."""

    def _kind(self, exc: Exception) -> str:
        from app.api.builder_core import turn_failure

        return turn_failure(exc, label="entity builder").detail["kind"]

    def test_each_cause_gets_its_own_kind(self):
        assert self._kind(LLMAuthError("bad key")) == "builder_llm_credential_rejected"
        assert self._kind(LLMModelNotFoundError("gone")) == "builder_llm_model_unavailable"
        assert self._kind(LLMRateLimitError("slow down")) == "builder_llm_rate_limited"
        assert self._kind(LLMTimeoutError("no answer")) == "builder_llm_unreachable"
        assert self._kind(LLMUnsupportedError("refused")) == "builder_llm_request_refused"
        assert self._kind(RuntimeError("who knows")) == "builder_turn_failed", (
            "an unrecognised failure keeps the old, honest wording"
        )

    def test_a_permanent_failure_does_not_tell_the_author_to_try_again(self):
        from app.api.builder_core import turn_failure

        for exc in (LLMAuthError("bad key"), LLMModelNotFoundError("gone")):
            hint = turn_failure(exc, label="entity builder").detail["hint"]
            assert "retrying will not help" in hint.lower(), f"{type(exc).__name__} still says to retry"

    def test_the_providers_words_ride_along_but_are_capped(self):
        from app.api.builder_core import MAX_PROVIDER_DETAIL_CHARS, turn_failure

        exc = LLMUnsupportedError("x" * (MAX_PROVIDER_DETAIL_CHARS * 3))
        detail = turn_failure(exc, label="entity builder").detail
        assert len(detail["detail"]) == MAX_PROVIDER_DETAIL_CHARS

    def test_it_is_always_a_502(self):
        from app.api.builder_core import turn_failure

        assert turn_failure(LLMAuthError("x"), label="entity builder").status_code == 502
