"""Full tests for LLM provider factory and extractors."""

import json
from unittest.mock import MagicMock, patch

import anthropic
import openai
import pytest

from connectors.llm.anthropic_provider import AnthropicExtractor
from connectors.llm.exceptions import (
    LLMAuthError,
    LLMFormatError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMTimeoutError,
    LLMUnsupportedError,
)
from connectors.llm.factory import DEFAULT_MODEL, create_extractor
from connectors.llm.openai_compat import OpenAICompatExtractor, _extract_json_from_text


# ---------------------------------------------------------------------------
# Mock response helpers
# ---------------------------------------------------------------------------


def _anthropic_response(text: str, stop_reason: str = "end_turn"):
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    resp.stop_reason = stop_reason
    return resp


def _anthropic_thinking_response(text: str, stop_reason: str = "end_turn"):
    """A response whose FIRST block is a thinking block, second the answer.

    What a thinking-capable model (Claude 5 family, adaptive thinking on by
    default — no ``thinking`` request parameter needed) returns on the turns it
    decides to think. Deliberately NOT a MagicMock: a thinking block carries
    ``.thinking``/``.signature`` and no ``.text`` at all, and a MagicMock would
    invent the very attribute this test is about.
    """

    class _Thinking:
        type = "thinking"
        thinking = "Let me consider the schema..."
        signature = "sig"

    class _Text:
        type = "text"

        def __init__(self, t: str) -> None:
            self.text = t

    resp = MagicMock()
    resp.content = [_Thinking(), _Text(text)]
    resp.stop_reason = stop_reason
    return resp


def _openai_response(content: str | None, finish_reason: str = "stop"):
    message = MagicMock()
    message.content = content
    choice = MagicMock()
    choice.message = message
    choice.finish_reason = finish_reason
    resp = MagicMock()
    resp.choices = [choice]
    return resp


_SCHEMA = {"type": "object", "properties": {"value": {"type": "string"}}}


# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------


class TestCreateExtractor:
    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_anthropic_provider_returns_anthropic_extractor(self, _mock):
        config = {"provider": "anthropic", "api_key": "sk-ant-test"}
        ext = create_extractor(config)
        assert isinstance(ext, AnthropicExtractor)

    @patch("connectors.llm.openai_compat.openai.OpenAI")
    @patch("connectors.llm.openai_compat.httpx.Client")
    def test_openai_compat_provider_returns_openai_extractor(self, _mock_http, _mock_oai):
        config = {
            "provider": "openai_compat",
            "api_key": "sk-test",
            "base_url": "https://api.openai.com/v1",
        }
        ext = create_extractor(config)
        assert isinstance(ext, OpenAICompatExtractor)

    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_legacy_anthropic_key_format(self, _mock):
        """anthropic_api_key (legacy format) still creates AnthropicExtractor."""
        config = {"anthropic_api_key": "sk-ant-legacy"}
        ext = create_extractor(config)
        assert isinstance(ext, AnthropicExtractor)

    def test_missing_provider_raises_value_error(self):
        with pytest.raises(ValueError, match="ai.provider is required"):
            create_extractor({"api_key": "sk-test"})

    def test_empty_config_raises_value_error(self):
        with pytest.raises(ValueError):
            create_extractor({})

    def test_unknown_provider_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown ai.provider"):
            create_extractor({"provider": "cohere", "api_key": "sk-test"})

    @patch("connectors.llm.openai_compat.openai.OpenAI")
    @patch("connectors.llm.openai_compat.httpx.Client")
    def test_openai_compat_missing_base_url_raises(self, _mock_http, _mock_oai):
        with pytest.raises(ValueError, match="base_url is required"):
            create_extractor({"provider": "openai_compat", "api_key": "sk-test"})

    def test_empty_api_key_raises_value_error(self):
        with pytest.raises(ValueError, match="api_key"):
            create_extractor({"provider": "anthropic", "api_key": ""})


# ---------------------------------------------------------------------------
# AnthropicExtractor tests
# ---------------------------------------------------------------------------


class TestAnthropicExtractor:
    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_extract_json_success(self, mock_cls):
        """extract_json returns parsed dict on successful API call."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = _anthropic_response('{"value": "hello"}')

        ext = AnthropicExtractor(api_key="sk-ant-test", model="claude-haiku-4-5-20251001")
        result = ext.extract_json("prompt", 1000, _SCHEMA, "test_schema")

        assert result == {"value": "hello"}

    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_auth_error_raises_immediately(self, mock_cls):
        """AuthenticationError is raised immediately without retry."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.side_effect = anthropic.AuthenticationError(
            message="Invalid key", response=MagicMock(), body={}
        )

        ext = AnthropicExtractor(api_key="bad-key", model="claude-haiku-4-5-20251001")
        with pytest.raises(LLMAuthError):
            ext.extract_json("prompt", 1000, _SCHEMA, "test_schema")

        # Should only be called once — no retry
        assert mock_client.messages.create.call_count == 1

    @patch("connectors.llm.anthropic_provider.time.sleep")
    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_rate_limit_retries_and_raises(self, mock_cls, mock_sleep):
        """RateLimitError is retried MAX_RETRIES times then raises LLMRateLimitError."""
        from connectors.llm.anthropic_provider import MAX_RETRIES

        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.side_effect = anthropic.RateLimitError(
            message="Rate limited", response=MagicMock(), body={}
        )

        ext = AnthropicExtractor(api_key="sk-ant-test", model="claude-haiku-4-5-20251001")
        with pytest.raises(LLMRateLimitError):
            ext.extract_json("prompt", 1000, _SCHEMA, "test_schema")

        assert mock_client.messages.create.call_count == MAX_RETRIES

    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_thinking_block_before_answer_is_parsed(self, mock_cls):
        """A leading thinking block does not hide the answer.

        Reading ``content[0].text`` raised AttributeError on exactly the turns
        a thinking-capable model chose to think — wrapped as "Failed to parse
        ... as JSON (AttributeError)" and re-raised without retry, so every
        structured-output caller (builders, guardrails, ontology, corporate
        memory) failed intermittently and blamed the model for bad JSON.
        """
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = _anthropic_thinking_response('{"value": "hello"}')

        ext = AnthropicExtractor(api_key="sk-ant-test", model="claude-haiku-4-5-20251001")

        assert ext.extract_json("prompt", 1000, _SCHEMA, "test_schema") == {"value": "hello"}

    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_thinking_only_response_raises_format_error(self, mock_cls):
        """Thinking with no text block is a format error, not an AttributeError."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        class _Thinking:
            type = "thinking"
            thinking = "..."

        resp = MagicMock()
        resp.content = [_Thinking()]
        resp.stop_reason = "end_turn"
        mock_client.messages.create.return_value = resp

        ext = AnthropicExtractor(api_key="sk-ant-test", model="claude-haiku-4-5-20251001")
        with pytest.raises(LLMFormatError, match="no text block"):
            ext.extract_json("prompt", 1000, _SCHEMA, "test_schema")

    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_truncated_response_raises_format_error(self, mock_cls):
        """max_tokens stop_reason raises LLMFormatError."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = _anthropic_response(
            '{"partial":', stop_reason="max_tokens"
        )

        ext = AnthropicExtractor(api_key="sk-ant-test", model="claude-haiku-4-5-20251001")
        with pytest.raises(LLMFormatError, match="truncated"):
            ext.extract_json("prompt", 10, _SCHEMA, "test_schema")


# ---------------------------------------------------------------------------
# OpenAICompatExtractor tests
# ---------------------------------------------------------------------------


class TestOpenAICompatExtractor:
    def _make_extractor(self, structured_output: str = "auto") -> OpenAICompatExtractor:
        with patch("connectors.llm.openai_compat.openai.OpenAI"), \
             patch("connectors.llm.openai_compat.httpx.Client"):
            return OpenAICompatExtractor(
                api_key="sk-test",
                base_url="https://api.example.com/v1",
                model="gpt-4o-mini",
                structured_output=structured_output,
            )

    def test_extract_json_success_json_schema(self):
        """extract_json succeeds with json_schema strategy."""
        ext = self._make_extractor()
        ext._client = MagicMock()
        ext._client.chat.completions.create.return_value = _openai_response('{"value": "ok"}')

        result = ext.extract_json("prompt", 1000, _SCHEMA, "test")
        assert result == {"value": "ok"}

    def test_strategy_cascade_falls_back_on_bad_request(self):
        """json_schema unsupported -> falls back to json_object strategy (auto mode)."""
        ext = self._make_extractor(structured_output="auto")
        ext._client = MagicMock()

        # First call (json_schema) raises BadRequestError about response_format
        bad_request_error = openai.BadRequestError(
            message="response_format json_schema not supported",
            response=MagicMock(status_code=400),
            body={"error": {"message": "response_format json_schema not supported"}},
        )
        success_response = _openai_response('{"value": "fallback"}')
        ext._client.chat.completions.create.side_effect = [bad_request_error, success_response]

        result = ext.extract_json("prompt", 1000, _SCHEMA, "test")
        assert result == {"value": "fallback"}

    def test_auth_error_raises_immediately(self):
        """AuthenticationError is not retried."""
        ext = self._make_extractor()
        ext._client = MagicMock()
        ext._client.chat.completions.create.side_effect = openai.AuthenticationError(
            message="Invalid key",
            response=MagicMock(status_code=401),
            body={},
        )

        with pytest.raises(LLMAuthError):
            ext.extract_json("prompt", 1000, _SCHEMA, "test")

        assert ext._client.chat.completions.create.call_count == 1

    def test_strict_mode_raises_unsupported(self):
        """strict mode does not fall back; raises LLMUnsupportedError."""
        ext = self._make_extractor(structured_output="strict")
        ext._client = MagicMock()
        ext._client.chat.completions.create.side_effect = openai.BadRequestError(
            message="json_schema not supported",
            response=MagicMock(status_code=400),
            body={"error": {"message": "json_schema not supported"}},
        )

        with pytest.raises(LLMUnsupportedError):
            ext.extract_json("prompt", 1000, _SCHEMA, "test")

    def test_refusal_raises_immediately(self):
        """Empty content (refusal) raises LLMRefusalError."""
        ext = self._make_extractor()
        ext._client = MagicMock()
        ext._client.chat.completions.create.return_value = _openai_response(None)

        with pytest.raises(LLMRefusalError):
            ext.extract_json("prompt", 1000, _SCHEMA, "test")


# ---------------------------------------------------------------------------
# _extract_json_from_text tests
# ---------------------------------------------------------------------------


class TestExtractJsonFromText:
    def test_direct_json_parse(self):
        assert _extract_json_from_text('{"key": "val"}') == {"key": "val"}

    def test_strips_markdown_code_fence(self):
        text = '```json\n{"key": "fenced"}\n```'
        assert _extract_json_from_text(text) == {"key": "fenced"}

    def test_strips_plain_code_fence(self):
        text = "```\n{\"key\": \"plain\"}\n```"
        assert _extract_json_from_text(text) == {"key": "plain"}

    def test_brace_extraction_fallback(self):
        text = "Here is the JSON: {\"key\": \"brace\"} and some trailing text."
        assert _extract_json_from_text(text) == {"key": "brace"}

    def test_raises_format_error_on_invalid(self):
        with pytest.raises(LLMFormatError):
            _extract_json_from_text("This is definitely not JSON at all.")
