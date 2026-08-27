"""
Tests for LLM connector module and Corporate Memory collector integration.

Covers:
- Factory (create_extractor) with various configs
- AnthropicExtractor (mock anthropic SDK)
- OpenAICompatExtractor (mock openai SDK) with fallback strategies
- Security (no secrets in logs)
- Corporate Memory collector using the LLM connector
"""

import json
import logging
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
    LLMUnsupportedError,
)
from connectors.llm.factory import DEFAULT_MODEL, create_extractor
from connectors.llm.openai_compat import (
    OpenAICompatExtractor,
    _extract_json_from_text,
    _sanitize_url,
)

# ---------------------------------------------------------------------------
# Helpers: mock response builders
# ---------------------------------------------------------------------------


def _anthropic_response(text: str, stop_reason: str = "end_turn"):
    """Build a mock Anthropic API response."""
    block = MagicMock()
    block.text = text
    response = MagicMock()
    response.content = [block]
    response.stop_reason = stop_reason
    return response


def _openai_response(content: str | None, finish_reason: str = "stop"):
    """Build a mock OpenAI chat completion response."""
    message = MagicMock()
    message.content = content
    choice = MagicMock()
    choice.message = message
    choice.finish_reason = finish_reason
    response = MagicMock()
    response.choices = [choice]
    return response


# ===================================================================
# Factory tests
# ===================================================================


class TestCreateExtractor:
    """Tests for connectors.llm.factory.create_extractor."""

    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_anthropic_config(self, mock_client_cls):
        """Anthropic provider config returns AnthropicExtractor."""
        config = {"provider": "anthropic", "api_key": "sk-ant-test123"}
        ext = create_extractor(config)
        assert isinstance(ext, AnthropicExtractor)

    @patch("connectors.llm.openai_compat.openai.OpenAI")
    def test_openai_compat_config(self, mock_client_cls):
        """openai_compat provider config returns OpenAICompatExtractor."""
        config = {
            "provider": "openai_compat",
            "api_key": "sk-test",
            "base_url": "https://api.example.com/v1",
        }
        ext = create_extractor(config)
        assert isinstance(ext, OpenAICompatExtractor)

    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_legacy_config(self, mock_client_cls):
        """Legacy config with anthropic_api_key returns AnthropicExtractor."""
        config = {"anthropic_api_key": "sk-ant-legacy"}
        ext = create_extractor(config)
        assert isinstance(ext, AnthropicExtractor)

    def test_empty_config_raises(self):
        """Empty config dict raises ValueError."""
        with pytest.raises(ValueError, match="non-empty dict"):
            create_extractor({})

    def test_none_config_raises(self):
        """None config raises ValueError."""
        with pytest.raises(ValueError, match="non-empty dict"):
            create_extractor(None)  # type: ignore[arg-type]

    def test_missing_api_key_raises(self):
        """Config with provider but empty api_key raises ValueError."""
        with pytest.raises(ValueError, match="must not be empty"):
            create_extractor({"provider": "anthropic", "api_key": ""})

    def test_missing_api_key_whitespace_raises(self):
        """Config with whitespace-only api_key raises ValueError."""
        with pytest.raises(ValueError, match="must not be empty"):
            create_extractor({"provider": "anthropic", "api_key": "   "})

    def test_openai_compat_missing_base_url_raises(self):
        """openai_compat without base_url raises ValueError."""
        with pytest.raises(ValueError, match="base_url is required"):
            create_extractor(
                {
                    "provider": "openai_compat",
                    "api_key": "sk-test",
                }
            )

    def test_unknown_provider_raises(self):
        """Unknown provider string raises ValueError."""
        with pytest.raises(ValueError, match="Unknown ai.provider"):
            create_extractor(
                {
                    "provider": "gemini",
                    "api_key": "sk-test",
                }
            )

    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_default_model(self, mock_client_cls):
        """Default model is claude-haiku-4-5-20251001."""
        config = {"provider": "anthropic", "api_key": "sk-ant-test"}
        ext = create_extractor(config)
        assert ext._model == DEFAULT_MODEL
        assert ext._model == "claude-haiku-4-5-20251001"

    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_custom_model(self, mock_client_cls):
        """Custom model from config is used."""
        config = {
            "provider": "anthropic",
            "api_key": "sk-ant-test",
            "model": "claude-sonnet-4-20250514",
        }
        ext = create_extractor(config)
        assert ext._model == "claude-sonnet-4-20250514"

    def test_invalid_structured_output_raises(self):
        """Invalid structured_output value raises ValueError."""
        with pytest.raises(ValueError, match="strict.*json.*auto"):
            create_extractor(
                {
                    "provider": "openai_compat",
                    "api_key": "sk-test",
                    "base_url": "https://api.example.com/v1",
                    "structured_output": "whatever",
                }
            )


# ===================================================================
# AnthropicExtractor tests
# ===================================================================


class TestStrictJsonSchema:
    """The Anthropic API rejects object schemas without additionalProperties=False."""

    def test_adds_to_top_level_object(self):
        from connectors.llm.anthropic_provider import _strict_json_schema

        out = _strict_json_schema({"type": "object", "properties": {"a": {"type": "string"}}})
        assert out["additionalProperties"] is False

    def test_recurses_into_nested_objects(self):
        from connectors.llm.anthropic_provider import _strict_json_schema

        schema = {
            "type": "object",
            "properties": {
                "nested": {
                    "type": "object",
                    "properties": {"deep": {"type": "object", "properties": {}}},
                },
            },
        }
        out = _strict_json_schema(schema)
        assert out["additionalProperties"] is False
        assert out["properties"]["nested"]["additionalProperties"] is False
        assert out["properties"]["nested"]["properties"]["deep"]["additionalProperties"] is False

    def test_recurses_into_array_items(self):
        from connectors.llm.anthropic_provider import _strict_json_schema

        schema = {
            "type": "object",
            "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {}}}},
        }
        out = _strict_json_schema(schema)
        assert out["properties"]["items"]["items"]["additionalProperties"] is False

    def test_preserves_explicit_additional_properties(self):
        from connectors.llm.anthropic_provider import _strict_json_schema

        schema = {"type": "object", "additionalProperties": True, "properties": {}}
        out = _strict_json_schema(schema)
        assert out["additionalProperties"] is True

    def test_does_not_mutate_input(self):
        from connectors.llm.anthropic_provider import _strict_json_schema

        schema = {"type": "object", "properties": {}}
        _strict_json_schema(schema)
        assert "additionalProperties" not in schema

    def test_non_object_schemas_untouched(self):
        from connectors.llm.anthropic_provider import _strict_json_schema

        out = _strict_json_schema({"type": "string"})
        assert "additionalProperties" not in out


class TestAnthropicExtractor:
    """Tests for connectors.llm.anthropic_provider.AnthropicExtractor."""

    SCHEMA = {"type": "object", "properties": {"items": {"type": "array"}}}

    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_extract_json_success(self, mock_client_cls):
        """Successful extraction returns parsed dict."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        payload = {"items": [{"name": "test"}]}
        mock_client.messages.create.return_value = _anthropic_response(json.dumps(payload))

        ext = AnthropicExtractor(api_key="sk-ant-test", model="claude-haiku-4-5-20251001")
        result = ext.extract_json(
            prompt="Extract items",
            max_tokens=1024,
            json_schema=self.SCHEMA,
            schema_name="test_schema",
        )

        assert result == payload
        mock_client.messages.create.assert_called_once()

    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_auth_error_raises_immediately(self, mock_client_cls):
        """AuthenticationError raises LLMAuthError without retries."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.messages.create.side_effect = anthropic.AuthenticationError(
            message="bad key",
            response=MagicMock(status_code=401),
            body=None,
        )

        ext = AnthropicExtractor(api_key="sk-bad", model="claude-haiku-4-5-20251001")
        with pytest.raises(LLMAuthError, match="authentication failed"):
            ext.extract_json("test", 1024, self.SCHEMA, "test_schema")

        # Only one call - no retries for auth errors
        assert mock_client.messages.create.call_count == 1

    @patch("connectors.llm.anthropic_provider.time.sleep")
    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_rate_limit_retries_then_succeeds(self, mock_client_cls, mock_sleep):
        """RateLimitError retries and succeeds on second attempt."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        payload = {"items": []}
        mock_client.messages.create.side_effect = [
            anthropic.RateLimitError(
                message="rate limited",
                response=MagicMock(status_code=429),
                body=None,
            ),
            _anthropic_response(json.dumps(payload)),
        ]

        ext = AnthropicExtractor(api_key="sk-ant-test", model="claude-haiku-4-5-20251001")
        result = ext.extract_json("test", 1024, self.SCHEMA, "test_schema")

        assert result == payload
        assert mock_client.messages.create.call_count == 2
        mock_sleep.assert_called_once()

    @patch("connectors.llm.anthropic_provider.time.sleep")
    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_rate_limit_exhausts_retries(self, mock_client_cls, mock_sleep):
        """RateLimitError after max retries raises LLMRateLimitError."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.messages.create.side_effect = anthropic.RateLimitError(
            message="rate limited",
            response=MagicMock(status_code=429),
            body=None,
        )

        ext = AnthropicExtractor(api_key="sk-ant-test", model="claude-haiku-4-5-20251001")
        with pytest.raises(LLMRateLimitError):
            ext.extract_json("test", 1024, self.SCHEMA, "test_schema")

        assert mock_client.messages.create.call_count == 3  # MAX_RETRIES

    @patch("connectors.llm.anthropic_provider.time.sleep")
    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_timeout_retries(self, mock_client_cls, mock_sleep):
        """APITimeoutError retries with backoff then succeeds."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        payload = {"items": []}
        mock_client.messages.create.side_effect = [
            anthropic.APITimeoutError(request=MagicMock()),
            _anthropic_response(json.dumps(payload)),
        ]

        ext = AnthropicExtractor(api_key="sk-ant-test", model="claude-haiku-4-5-20251001")
        result = ext.extract_json("test", 1024, self.SCHEMA, "test_schema")

        assert result == payload
        mock_sleep.assert_called_once_with(2)  # INITIAL_BACKOFF_SECONDS

    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_truncation_retries_with_doubled_budget(self, mock_client_cls):
        """stop_reason='max_tokens' on first attempt retries with 2x budget."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        truncated = _anthropic_response('{"items": [', stop_reason="max_tokens")
        success = _anthropic_response('{"items": ["ok"]}')
        mock_client.messages.create.side_effect = [truncated, success]

        ext = AnthropicExtractor(api_key="sk-ant-test", model="claude-haiku-4-5-20251001")
        result = ext.extract_json("test", 1024, self.SCHEMA, "test_schema")

        assert result == {"items": ["ok"]}
        assert mock_client.messages.create.call_count == 2
        # First call used the caller's budget; second call doubled.
        first_kwargs = mock_client.messages.create.call_args_list[0].kwargs
        second_kwargs = mock_client.messages.create.call_args_list[1].kwargs
        assert first_kwargs["max_tokens"] == 1024
        assert second_kwargs["max_tokens"] == 2048

    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_truncation_persistent_raises_format_error(self, mock_client_cls):
        """Truncation on every attempt eventually surfaces LLMFormatError."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        truncated = _anthropic_response('{"items": [', stop_reason="max_tokens")
        mock_client.messages.create.return_value = truncated

        ext = AnthropicExtractor(api_key="sk-ant-test", model="claude-haiku-4-5-20251001")
        with pytest.raises(LLMFormatError, match="truncated"):
            ext.extract_json("test", 1024, self.SCHEMA, "test_schema")

        # MAX_TRUNCATION_RETRIES=2 → up to 3 calls (initial + 2 retries
        # at 2x and 4x), bounded by MAX_RETRIES=3 loop slots.
        assert mock_client.messages.create.call_count == 3
        # Final call hit the 4x cap.
        last_kwargs = mock_client.messages.create.call_args_list[-1].kwargs
        assert last_kwargs["max_tokens"] == 4096

    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_invalid_json_raises_format_error(self, mock_client_cls):
        """Non-JSON response raises LLMFormatError."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.messages.create.return_value = _anthropic_response("This is not JSON at all")

        ext = AnthropicExtractor(api_key="sk-ant-test", model="claude-haiku-4-5-20251001")
        with pytest.raises(LLMFormatError, match="Failed to parse"):
            ext.extract_json("test", 1024, self.SCHEMA, "test_schema")

    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_empty_content_raises_refusal(self, mock_client_cls):
        """Empty content list with end_turn raises LLMRefusalError."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        response = MagicMock()
        response.content = []
        response.stop_reason = "end_turn"
        mock_client.messages.create.return_value = response

        ext = AnthropicExtractor(api_key="sk-ant-test", model="claude-haiku-4-5-20251001")
        with pytest.raises(LLMRefusalError, match="refused"):
            ext.extract_json("test", 1024, self.SCHEMA, "test_schema")

    @patch("connectors.llm.anthropic_provider.time.sleep")
    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_backoff_multiplier(self, mock_client_cls, mock_sleep):
        """Exponential backoff doubles the delay on each retry."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.messages.create.side_effect = anthropic.RateLimitError(
            message="limited",
            response=MagicMock(status_code=429),
            body=None,
        )

        ext = AnthropicExtractor(api_key="sk-ant-test", model="claude-haiku-4-5-20251001")
        with pytest.raises(LLMRateLimitError):
            ext.extract_json("test", 1024, self.SCHEMA, "test_schema")

        # 3 attempts, 2 sleeps: delay(1)=2, delay(2)=4
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(2)
        mock_sleep.assert_any_call(4)


# ===================================================================
# OpenAICompatExtractor tests
# ===================================================================


class TestOpenAICompatExtractor:
    """Tests for connectors.llm.openai_compat.OpenAICompatExtractor."""

    SCHEMA = {"type": "object", "properties": {"items": {"type": "array"}}}
    BASE_URL = "https://api.example.com/v1"

    @patch("connectors.llm.openai_compat.openai.OpenAI")
    def test_json_schema_success(self, mock_client_cls):
        """json_schema strategy returns parsed dict on success."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        payload = {"items": [{"id": 1}]}
        mock_client.chat.completions.create.return_value = _openai_response(json.dumps(payload))

        ext = OpenAICompatExtractor(
            api_key="sk-test",
            base_url=self.BASE_URL,
            model="gpt-4o",
            structured_output="auto",
        )
        result = ext.extract_json("Extract", 1024, self.SCHEMA, "test")

        assert result == payload
        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert call_kwargs["response_format"]["type"] == "json_schema"

    @patch("connectors.llm.openai_compat.openai.OpenAI")
    def test_fallback_json_schema_to_json_object(self, mock_client_cls):
        """Auto mode falls back from json_schema to json_object."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        payload = {"items": []}
        # First call (json_schema) fails with BadRequestError, second (json_object) succeeds
        mock_client.chat.completions.create.side_effect = [
            openai.BadRequestError(
                message="response_format json_schema not supported",
                response=MagicMock(status_code=400),
                body=None,
            ),
            _openai_response(json.dumps(payload)),
        ]

        ext = OpenAICompatExtractor(
            api_key="sk-test",
            base_url=self.BASE_URL,
            model="local-model",
            structured_output="auto",
        )
        result = ext.extract_json("Extract", 1024, self.SCHEMA, "test")

        assert result == payload
        assert mock_client.chat.completions.create.call_count == 2

        # Second call should use json_object format
        second_call_kwargs = mock_client.chat.completions.create.call_args_list[1][1]
        assert second_call_kwargs["response_format"]["type"] == "json_object"

    @patch("connectors.llm.openai_compat.openai.OpenAI")
    def test_fallback_to_text_mode(self, mock_client_cls):
        """Auto mode falls back to text when both json_schema and json_object fail."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        payload = {"items": [{"x": 1}]}
        mock_client.chat.completions.create.side_effect = [
            openai.BadRequestError(
                message="response_format json_schema not supported",
                response=MagicMock(status_code=400),
                body=None,
            ),
            openai.BadRequestError(
                message="response_format json_object not supported",
                response=MagicMock(status_code=400),
                body=None,
            ),
            _openai_response(json.dumps(payload)),
        ]

        ext = OpenAICompatExtractor(
            api_key="sk-test",
            base_url=self.BASE_URL,
            model="local-model",
            structured_output="auto",
        )
        result = ext.extract_json("Extract", 1024, self.SCHEMA, "test")

        assert result == payload
        assert mock_client.chat.completions.create.call_count == 3

        # Third call should NOT have response_format (text fallback)
        third_call_kwargs = mock_client.chat.completions.create.call_args_list[2][1]
        assert "response_format" not in third_call_kwargs

    @patch("connectors.llm.openai_compat.openai.OpenAI")
    def test_strict_mode_raises_unsupported(self, mock_client_cls):
        """strict mode raises LLMUnsupportedError when json_schema fails."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.chat.completions.create.side_effect = openai.BadRequestError(
            message="response_format json_schema not supported",
            response=MagicMock(status_code=400),
            body=None,
        )

        ext = OpenAICompatExtractor(
            api_key="sk-test",
            base_url=self.BASE_URL,
            model="local-model",
            structured_output="strict",
        )
        with pytest.raises(LLMUnsupportedError, match="No supported structured output"):
            ext.extract_json("Extract", 1024, self.SCHEMA, "test")

    @patch("connectors.llm.openai_compat.openai.OpenAI")
    def test_json_mode_no_text_fallback(self, mock_client_cls):
        """json mode tries json_schema + json_object but NOT text."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.chat.completions.create.side_effect = openai.BadRequestError(
            message="response_format json_schema not supported",
            response=MagicMock(status_code=400),
            body=None,
        )

        ext = OpenAICompatExtractor(
            api_key="sk-test",
            base_url=self.BASE_URL,
            model="local-model",
            structured_output="json",
        )
        with pytest.raises(LLMUnsupportedError, match="No supported structured output"):
            ext.extract_json("Extract", 1024, self.SCHEMA, "test")

        # json mode: json_schema -> LLMUnsupportedError (skip), json_object -> same
        assert mock_client.chat.completions.create.call_count == 2

    @patch("connectors.llm.openai_compat.openai.OpenAI")
    def test_text_fallback_strips_markdown_fences(self, mock_client_cls):
        """Text fallback strips markdown code fences from response."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        payload = {"items": [{"v": 42}]}
        fenced_json = f"```json\n{json.dumps(payload)}\n```"

        mock_client.chat.completions.create.side_effect = [
            openai.BadRequestError(
                message="response_format json_schema not supported",
                response=MagicMock(status_code=400),
                body=None,
            ),
            openai.BadRequestError(
                message="response_format json_object not supported",
                response=MagicMock(status_code=400),
                body=None,
            ),
            _openai_response(fenced_json),
        ]

        ext = OpenAICompatExtractor(
            api_key="sk-test",
            base_url=self.BASE_URL,
            model="local-model",
            structured_output="auto",
        )
        result = ext.extract_json("Extract", 1024, self.SCHEMA, "test")

        assert result == payload

    @patch("connectors.llm.openai_compat.openai.OpenAI")
    def test_auth_error(self, mock_client_cls):
        """AuthenticationError raises LLMAuthError without retries."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.chat.completions.create.side_effect = openai.AuthenticationError(
            message="invalid key",
            response=MagicMock(status_code=401),
            body=None,
        )

        ext = OpenAICompatExtractor(
            api_key="sk-bad",
            base_url=self.BASE_URL,
            model="gpt-4o",
            structured_output="auto",
        )
        with pytest.raises(LLMAuthError, match="authentication failed"):
            ext.extract_json("Extract", 1024, self.SCHEMA, "test")

        assert mock_client.chat.completions.create.call_count == 1

    @patch("connectors.llm.openai_compat.openai.OpenAI")
    def test_truncation_raises_format_error(self, mock_client_cls):
        """finish_reason='length' raises LLMFormatError immediately (no recursion)."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        mock_client.chat.completions.create.return_value = _openai_response(
            '{"items": [',
            finish_reason="length",
        )

        ext = OpenAICompatExtractor(
            api_key="sk-test",
            base_url=self.BASE_URL,
            model="gpt-4o",
            structured_output="auto",
        )
        with pytest.raises(LLMFormatError, match="truncated"):
            ext.extract_json("Extract", 1024, self.SCHEMA, "test")

    @patch("connectors.llm.openai_compat.openai.OpenAI")
    def test_empty_content_raises_refusal(self, mock_client_cls):
        """Empty content raises LLMRefusalError."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        mock_client.chat.completions.create.return_value = _openai_response(None, finish_reason="stop")

        ext = OpenAICompatExtractor(
            api_key="sk-test",
            base_url=self.BASE_URL,
            model="gpt-4o",
            structured_output="auto",
        )
        with pytest.raises(LLMRefusalError, match="refused"):
            ext.extract_json("Extract", 1024, self.SCHEMA, "test")

    @patch("connectors.llm.openai_compat.time.sleep")
    @patch("connectors.llm.openai_compat.openai.OpenAI")
    def test_rate_limit_retries(self, mock_client_cls, mock_sleep):
        """RateLimitError retries with backoff then succeeds."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        payload = {"items": []}
        mock_client.chat.completions.create.side_effect = [
            openai.RateLimitError(
                message="too many requests",
                response=MagicMock(status_code=429),
                body=None,
            ),
            _openai_response(json.dumps(payload)),
        ]

        ext = OpenAICompatExtractor(
            api_key="sk-test",
            base_url=self.BASE_URL,
            model="gpt-4o",
            structured_output="auto",
        )
        result = ext.extract_json("Extract", 1024, self.SCHEMA, "test")

        assert result == payload
        mock_sleep.assert_called_once_with(2)

    @patch("connectors.llm.openai_compat.openai.OpenAI")
    def test_bad_request_non_format_raises_format_error(self, mock_client_cls):
        """BadRequestError not about response_format raises LLMFormatError."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.chat.completions.create.side_effect = openai.BadRequestError(
            message="invalid model parameter",
            response=MagicMock(status_code=400),
            body=None,
        )

        ext = OpenAICompatExtractor(
            api_key="sk-test",
            base_url=self.BASE_URL,
            model="gpt-4o",
            structured_output="auto",
        )
        with pytest.raises(LLMFormatError, match="Bad request"):
            ext.extract_json("Extract", 1024, self.SCHEMA, "test")


# ===================================================================
# URL sanitization tests
# ===================================================================


class TestURLSanitization:
    """Tests for URL sanitization in logging."""

    def test_sanitize_url_removes_path(self):
        """Sanitized URL has no path component."""
        result = _sanitize_url("https://api.example.com/v1/chat/completions")
        assert result == "https://api.example.com"

    def test_sanitize_url_removes_query(self):
        """Sanitized URL has no query params."""
        result = _sanitize_url("https://api.example.com/v1?token=secret123")
        assert result == "https://api.example.com"

    def test_sanitize_url_preserves_port(self):
        """Sanitized URL preserves port number."""
        result = _sanitize_url("http://localhost:8080/v1")
        assert result == "http://localhost:8080"


# ===================================================================
# _extract_json_from_text tests
# ===================================================================


class TestExtractJsonFromText:
    """Tests for the text-based JSON extraction helper."""

    def test_direct_json(self):
        """Plain JSON parses directly."""
        result = _extract_json_from_text('{"key": "value"}')
        assert result == {"key": "value"}

    def test_markdown_fence_json(self):
        """JSON wrapped in ```json fences is extracted."""
        text = '```json\n{"key": "value"}\n```'
        result = _extract_json_from_text(text)
        assert result == {"key": "value"}

    def test_markdown_fence_no_lang(self):
        """JSON wrapped in ``` fences (no language) is extracted."""
        text = '```\n{"key": "value"}\n```'
        result = _extract_json_from_text(text)
        assert result == {"key": "value"}

    def test_brace_extraction_fallback(self):
        """Fallback: extract JSON between first { and last }."""
        text = 'Here is the result: {"key": "value"} -- done'
        result = _extract_json_from_text(text)
        assert result == {"key": "value"}

    def test_no_json_raises_format_error(self):
        """No valid JSON raises LLMFormatError."""
        with pytest.raises(LLMFormatError, match="Could not extract valid JSON"):
            _extract_json_from_text("This is just plain text without braces")

    def test_invalid_json_in_braces_raises(self):
        """Malformed JSON in braces raises LLMFormatError."""
        with pytest.raises(LLMFormatError):
            _extract_json_from_text("{not: valid json}")


# ===================================================================
# Security tests (no secrets in logs)
# ===================================================================


class TestSecurity:
    """Verify that API keys, prompts, and responses never appear in log output."""

    SECRET_KEY = "sk-ant-SUPER-SECRET-KEY-12345"
    PROMPT_TEXT = "Extract the following secret data from documents"
    RESPONSE_TEXT = '{"items": [{"classified": "top-secret-info"}]}'

    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_api_key_not_in_logs(self, mock_client_cls, caplog):
        """API key must never appear in log messages."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.messages.create.return_value = _anthropic_response(self.RESPONSE_TEXT)

        ext = AnthropicExtractor(api_key=self.SECRET_KEY, model="claude-haiku-4-5-20251001")
        with caplog.at_level(logging.DEBUG, logger="connectors.llm"):
            ext.extract_json(
                self.PROMPT_TEXT,
                1024,
                {"type": "object"},
                "test_schema",
            )

        full_log = caplog.text
        assert self.SECRET_KEY not in full_log

    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_prompt_not_in_logs(self, mock_client_cls, caplog):
        """Prompt content must never appear in log messages."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.messages.create.return_value = _anthropic_response(self.RESPONSE_TEXT)

        ext = AnthropicExtractor(api_key=self.SECRET_KEY, model="claude-haiku-4-5-20251001")
        with caplog.at_level(logging.DEBUG, logger="connectors.llm"):
            ext.extract_json(
                self.PROMPT_TEXT,
                1024,
                {"type": "object"},
                "test_schema",
            )

        full_log = caplog.text
        assert self.PROMPT_TEXT not in full_log

    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_response_not_in_logs(self, mock_client_cls, caplog):
        """Response content must never appear in log messages."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.messages.create.return_value = _anthropic_response(self.RESPONSE_TEXT)

        ext = AnthropicExtractor(api_key=self.SECRET_KEY, model="claude-haiku-4-5-20251001")
        with caplog.at_level(logging.DEBUG, logger="connectors.llm"):
            ext.extract_json(
                self.PROMPT_TEXT,
                1024,
                {"type": "object"},
                "test_schema",
            )

        full_log = caplog.text
        assert "top-secret-info" not in full_log
        assert self.RESPONSE_TEXT not in full_log

    @patch("connectors.llm.openai_compat.openai.OpenAI")
    def test_openai_api_key_not_in_logs(self, mock_client_cls, caplog):
        """OpenAI-compat API key must never appear in log messages."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _openai_response(self.RESPONSE_TEXT)

        ext = OpenAICompatExtractor(
            api_key=self.SECRET_KEY,
            base_url="https://api.example.com/v1",
            model="gpt-4o",
            structured_output="auto",
        )
        with caplog.at_level(logging.DEBUG, logger="connectors.llm"):
            ext.extract_json(
                self.PROMPT_TEXT,
                1024,
                {"type": "object"},
                "test_schema",
            )

        full_log = caplog.text
        assert self.SECRET_KEY not in full_log

    @patch("connectors.llm.openai_compat.openai.OpenAI")
    def test_openai_url_path_not_in_logs(self, mock_client_cls, caplog):
        """URL paths (may contain tokens) must not appear in logs."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _openai_response('{"ok": true}')

        sensitive_url = "https://api.example.com/v1/secret-path?token=abc123"
        ext = OpenAICompatExtractor(
            api_key="sk-test",
            base_url=sensitive_url,
            model="gpt-4o",
            structured_output="auto",
        )
        with caplog.at_level(logging.DEBUG, logger="connectors.llm"):
            ext.extract_json("test", 1024, {"type": "object"}, "test_schema")

        full_log = caplog.text
        assert "secret-path" not in full_log
        assert "token=abc123" not in full_log
        # But the host SHOULD be present (safe to log)
        assert "api.example.com" in full_log


# ===================================================================
# Corporate Memory collector tests
# ===================================================================


class TestCorporateMemoryCollector:
    """Tests for services.corporate_memory.collector integration with LLM connector."""

    def test_collect_all_no_files_skips(self, tmp_path):
        """collect_all skips when no CLAUDE.local.md files found."""
        from services.corporate_memory.collector import collect_all

        # Use an empty directory as HOME_BASE
        empty_home = tmp_path / "empty_home"
        empty_home.mkdir()

        with patch("services.corporate_memory.collector.HOME_BASE", empty_home):
            stats = collect_all(dry_run=True)

        assert stats["skipped"] is True
        assert stats["files_found"] == 0

    def test_collect_all_no_changes_skips(self, tmp_path):
        """collect_all skips when hashes match (no changes)."""
        import hashlib as hl

        from services.corporate_memory.collector import collect_all

        # Set up a user directory with CLAUDE.local.md
        home = tmp_path / "home"
        home.mkdir()
        user_dir = home / "alice"
        user_dir.mkdir()
        claude_file = user_dir / "CLAUDE.local.md"
        claude_file.write_text("Some knowledge content")

        content_hash = hl.md5(b"Some knowledge content").hexdigest()

        # Stored hashes match current hashes -> no changes
        with (
            patch("services.corporate_memory.collector.HOME_BASE", home),
            patch(
                "services.corporate_memory.collector._read_json",
                return_value={"hashes": {"alice": content_hash}},
            ),
        ):
            stats = collect_all(dry_run=True)

        assert stats["skipped"] is True
        assert stats["files_found"] == 1

    def test_collect_all_with_changes(self, tmp_path):
        """collect_all processes files when changes detected."""
        from services.corporate_memory.collector import collect_all

        home = tmp_path / "home"
        home.mkdir()
        user_dir = home / "bob"
        user_dir.mkdir()
        claude_file = user_dir / "CLAUDE.local.md"
        claude_file.write_text("## Useful DuckDB trick\nUse QUALIFY for window filters")

        # Mock extractor
        mock_extractor = MagicMock()
        mock_extractor.extract_json.side_effect = [
            # First call: catalog refresh
            {
                "items": [
                    {
                        "existing_id": None,
                        "title": "DuckDB QUALIFY clause",
                        "content": "Use QUALIFY for window function filtering",
                        "category": "data_analysis",
                        "tags": ["duckdb", "sql"],
                        "source_users": ["bob"],
                    },
                ],
            },
            # Second call: sensitivity check
            {"safe": True},
        ]

        with (
            patch("services.corporate_memory.collector.HOME_BASE", home),
            patch("services.corporate_memory.collector._read_json", return_value={}),
            patch("services.corporate_memory.collector._write_json"),
            patch(
                "config.loader.load_instance_config",
                return_value={"ai": {"provider": "anthropic", "api_key": "sk-test"}},
            ),
            patch(
                "connectors.llm.create_extractor_from_env_or_config",
                return_value=mock_extractor,
            ),
        ):
            stats = collect_all(dry_run=True)

        assert stats["skipped"] is False
        assert stats["items_extracted"] == 1
        assert stats["items_new"] == 1
        assert stats["items_filtered"] == 0
        assert stats["errors"] == []

    def test_collect_all_filters_sensitive_items(self, tmp_path):
        """collect_all filters items that fail sensitivity check."""
        from services.corporate_memory.collector import collect_all

        home = tmp_path / "home"
        home.mkdir()
        user_dir = home / "carol"
        user_dir.mkdir()
        (user_dir / "CLAUDE.local.md").write_text("API key: sk-secret-123")

        mock_extractor = MagicMock()
        mock_extractor.extract_json.side_effect = [
            # Catalog refresh
            {
                "items": [
                    {
                        "existing_id": None,
                        "title": "API credentials",
                        "content": "Use sk-secret-123 for auth",
                        "category": "api_integration",
                        "tags": ["api", "auth"],
                        "source_users": ["carol"],
                    },
                ],
            },
            # Sensitivity check: NOT safe
            {"safe": False, "reason": "Contains API key"},
        ]

        with (
            patch("services.corporate_memory.collector.HOME_BASE", home),
            patch("services.corporate_memory.collector._read_json", return_value={}),
            patch("services.corporate_memory.collector._write_json"),
            patch(
                "config.loader.load_instance_config",
                return_value={"ai": {"provider": "anthropic", "api_key": "sk-test"}},
            ),
            patch(
                "connectors.llm.create_extractor_from_env_or_config",
                return_value=mock_extractor,
            ),
        ):
            stats = collect_all(dry_run=True)

        assert stats["items_extracted"] == 1
        assert stats["items_new"] == 0
        assert stats["items_filtered"] == 1

    def test_collect_all_preserves_existing_items(self, tmp_path):
        """Existing items (by ID) skip sensitivity check and are preserved."""
        from services.corporate_memory.collector import collect_all

        home = tmp_path / "home"
        home.mkdir()
        user_dir = home / "dave"
        user_dir.mkdir()
        (user_dir / "CLAUDE.local.md").write_text("Updated knowledge")

        existing = {
            "items": {
                "km_abc123": {
                    "id": "km_abc123",
                    "title": "Existing item",
                    "content": "This is already validated",
                    "category": "workflow",
                    "tags": ["existing"],
                    "source_users": ["dave"],
                    "extracted_at": "2026-01-01T00:00:00+00:00",
                },
            },
            "metadata": {},
        }

        def read_json_side_effect(path):
            path_str = str(path)
            if "user_hashes" in path_str:
                return {}  # No stored hashes -> force change detection
            if "knowledge" in path_str:
                return existing
            return {}

        mock_extractor = MagicMock()
        # HAIKU returns the existing item (preserving ID)
        mock_extractor.extract_json.return_value = {
            "items": [
                {
                    "existing_id": "km_abc123",
                    "title": "Existing item (updated)",
                    "content": "This is already validated with updates",
                    "category": "workflow",
                    "tags": ["existing"],
                    "source_users": ["dave"],
                },
            ],
        }

        with (
            patch("services.corporate_memory.collector.HOME_BASE", home),
            patch(
                "services.corporate_memory.collector._read_json",
                side_effect=read_json_side_effect,
            ),
            patch("services.corporate_memory.collector._write_json"),
            patch(
                "config.loader.load_instance_config",
                return_value={"ai": {"provider": "anthropic", "api_key": "sk-test"}},
            ),
            patch(
                "connectors.llm.create_extractor_from_env_or_config",
                return_value=mock_extractor,
            ),
        ):
            stats = collect_all(dry_run=True)

        assert stats["items_preserved"] == 1
        assert stats["items_new"] == 0
        # extract_json called ONCE (catalog refresh only, no sensitivity for existing)
        assert mock_extractor.extract_json.call_count == 1

    def test_collect_all_handles_llm_error(self, tmp_path):
        """collect_all captures LLMError and returns it in stats."""
        from services.corporate_memory.collector import collect_all

        home = tmp_path / "home"
        home.mkdir()
        user_dir = home / "eve"
        user_dir.mkdir()
        (user_dir / "CLAUDE.local.md").write_text("Some content")

        mock_extractor = MagicMock()
        mock_extractor.extract_json.side_effect = LLMRateLimitError("too many requests")

        with (
            patch("services.corporate_memory.collector.HOME_BASE", home),
            patch("services.corporate_memory.collector._read_json", return_value={}),
            patch(
                "config.loader.load_instance_config",
                return_value={"ai": {"provider": "anthropic", "api_key": "sk-test"}},
            ),
            patch(
                "connectors.llm.create_extractor_from_env_or_config",
                return_value=mock_extractor,
            ),
        ):
            stats = collect_all(dry_run=True)

        assert len(stats["errors"]) == 1
        assert "LLM error" in stats["errors"][0]

    def test_collect_all_no_ai_config_or_env_raises(self, tmp_path, monkeypatch):
        """collect_all fails fast when neither ai: config nor LLM env keys exist (#176)."""
        from services.corporate_memory.collector import collect_all

        home = tmp_path / "home"
        home.mkdir()
        user_dir = home / "frank"
        user_dir.mkdir()
        (user_dir / "CLAUDE.local.md").write_text("Some content")

        # Make sure no env-var fallback is available so the factory raises.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("LLM_API_KEY", raising=False)

        with (
            patch("services.corporate_memory.collector.HOME_BASE", home),
            patch("services.corporate_memory.collector._read_json", return_value={}),
            patch(
                "config.loader.load_instance_config",
                return_value={"server": {"host": "example.com"}},
            ),
            pytest.raises(ValueError, match="LLM not configured"),
        ):
            collect_all(dry_run=True)

    def test_main_returns_1_on_no_ai_config_instead_of_traceback(self, tmp_path, monkeypatch, capsys):
        """CLI main() must catch the new fail-fast ValueError from collect_all() and exit cleanly.

        Regression for Devin Review on #179: previously the CLI crashed with an
        unhandled traceback when env + ai: config were both missing.
        """
        from services.corporate_memory import collector as cm

        monkeypatch.setattr("sys.argv", ["corporate_memory"])
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.setattr(cm, "CORPORATE_MEMORY_DIR", tmp_path / "cm")
        monkeypatch.setattr(cm, "COLLECTION_LOG", tmp_path / "cm" / "log")
        monkeypatch.setattr(
            cm, "collect_all", lambda dry_run=False: (_ for _ in ()).throw(ValueError("LLM not configured"))
        )

        rc = cm.main()
        assert rc == 1
        err = capsys.readouterr().err
        assert "Corporate Memory cannot run" in err
        assert "LLM not configured" in err


# ===================================================================
# Corporate Memory collector - helper function tests
# ===================================================================


class TestCollectorHelpers:
    """Tests for collector helper functions."""

    def test_generate_id_deterministic(self):
        """_generate_id returns consistent IDs for same content."""
        from services.corporate_memory.collector import _generate_id

        id1 = _generate_id("test content")
        id2 = _generate_id("test content")
        assert id1 == id2
        assert id1.startswith("km_")
        assert len(id1) == 15  # "km_" + 12 hex chars

    def test_generate_id_different_for_different_content(self):
        """_generate_id returns different IDs for different content."""
        from services.corporate_memory.collector import _generate_id

        id1 = _generate_id("content A")
        id2 = _generate_id("content B")
        assert id1 != id2

    def test_format_existing_catalog_empty(self):
        """Empty catalog formats as fresh message."""
        from services.corporate_memory.collector import _format_existing_catalog

        result = _format_existing_catalog({})
        assert "fresh catalog" in result.lower() or "No existing items" in result

    def test_format_existing_catalog_with_items(self):
        """Catalog with items formats each item."""
        from services.corporate_memory.collector import _format_existing_catalog

        existing = {
            "items": {
                "km_abc": {
                    "title": "Test Item",
                    "content": "Test content",
                    "category": "workflow",
                    "tags": ["test"],
                    "source_users": ["alice"],
                },
            },
        }

        result = _format_existing_catalog(existing)
        assert "km_abc" in result
        assert "Test Item" in result
        assert "workflow" in result
        assert "alice" in result

    def test_format_user_files(self):
        """User files are formatted with username headers."""
        from services.corporate_memory.collector import _format_user_files

        user_files = {
            "alice": ("Knowledge from alice", "hash_a"),
            "bob": ("Knowledge from bob", "hash_b"),
        }

        result = _format_user_files(user_files)
        assert "### User: alice" in result
        assert "### User: bob" in result
        assert "Knowledge from alice" in result
        assert "Knowledge from bob" in result

    def test_process_catalog_response_new_items(self):
        """New items get generated IDs."""
        from services.corporate_memory.collector import _process_catalog_response

        items = [
            {
                "existing_id": None,
                "title": "New Knowledge",
                "content": "Fresh insight",
                "category": "data_analysis",
                "tags": ["new"],
                "source_users": ["alice"],
            },
        ]

        result = _process_catalog_response(items, {"items": {}})

        assert len(result) == 1
        item_id = list(result.keys())[0]
        assert item_id.startswith("km_")
        item = result[item_id]
        assert item["title"] == "New Knowledge"
        assert item["content"] == "Fresh insight"
        assert "extracted_at" in item
        assert "updated_at" in item

    def test_process_catalog_response_preserves_existing(self):
        """Existing items keep their original ID and extracted_at."""
        from services.corporate_memory.collector import _process_catalog_response

        existing = {
            "items": {
                "km_existing": {
                    "title": "Old Title",
                    "content": "Old content",
                    "extracted_at": "2026-01-01T00:00:00+00:00",
                },
            },
        }

        items = [
            {
                "existing_id": "km_existing",
                "title": "Updated Title",
                "content": "Updated content",
                "category": "workflow",
                "tags": ["updated"],
                "source_users": ["alice"],
            },
        ]

        result = _process_catalog_response(items, existing)

        assert "km_existing" in result
        assert result["km_existing"]["title"] == "Updated Title"
        assert result["km_existing"]["extracted_at"] == "2026-01-01T00:00:00+00:00"

    def test_process_catalog_response_handles_collision(self):
        """ID collision for new items is resolved."""
        from services.corporate_memory.collector import _process_catalog_response

        # Two items with identical title+content will produce same hash
        items = [
            {
                "existing_id": None,
                "title": "Same",
                "content": "Same",
                "category": "workflow",
                "tags": [],
                "source_users": ["a"],
            },
            {
                "existing_id": None,
                "title": "Same",
                "content": "Same",
                "category": "workflow",
                "tags": [],
                "source_users": ["b"],
            },
        ]

        result = _process_catalog_response(items, {"items": {}})

        # Both items should be present (collision resolved)
        assert len(result) == 2


# ===================================================================
# Corporate Memory - check_sensitivity tests
# ===================================================================


class TestCheckSensitivity:
    """Tests for the sensitivity check function."""

    def test_safe_item_returns_true(self):
        """Safe items return True."""
        from services.corporate_memory.collector import check_sensitivity

        mock_extractor = MagicMock()
        mock_extractor.extract_json.return_value = {"safe": True}

        item = {
            "title": "SQL Tip",
            "content": "Use GROUP BY for aggregation",
            "tags": ["sql"],
        }

        assert check_sensitivity(mock_extractor, item) is True

    def test_unsafe_item_returns_false(self):
        """Unsafe items return False."""
        from services.corporate_memory.collector import check_sensitivity

        mock_extractor = MagicMock()
        mock_extractor.extract_json.return_value = {
            "safe": False,
            "reason": "Contains API key",
        }

        item = {
            "title": "Auth setup",
            "content": "Use key sk-12345",
            "tags": ["auth"],
        }

        assert check_sensitivity(mock_extractor, item) is False

    def test_llm_error_assumes_unsafe(self):
        """LLMError during sensitivity check assumes item is unsafe."""
        from services.corporate_memory.collector import check_sensitivity

        mock_extractor = MagicMock()
        mock_extractor.extract_json.side_effect = LLMRateLimitError("rate limited")

        item = {"title": "Test", "content": "Content", "tags": []}

        assert check_sensitivity(mock_extractor, item) is False

    def test_llm_format_error_assumes_unsafe(self):
        """LLMFormatError during sensitivity check assumes item is unsafe."""
        from services.corporate_memory.collector import check_sensitivity

        mock_extractor = MagicMock()
        mock_extractor.extract_json.side_effect = LLMFormatError("bad json")

        item = {"title": "Test", "content": "Content", "tags": []}

        assert check_sensitivity(mock_extractor, item) is False


# ===================================================================
# Integration: collector uses create_extractor
# ===================================================================


class TestCollectorExtractorIntegration:
    """Verify collector properly initializes the LLM extractor."""

    @patch("connectors.llm.anthropic_provider.anthropic.Anthropic")
    def test_collector_creates_anthropic_extractor(self, mock_client_cls, tmp_path):
        """Collector creates AnthropicExtractor from instance.yaml config."""
        from services.corporate_memory.collector import collect_all

        home = tmp_path / "home"
        home.mkdir()
        user_dir = home / "alice"
        user_dir.mkdir()
        (user_dir / "CLAUDE.local.md").write_text("Some knowledge")

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        catalog_response = _anthropic_response(json.dumps({"items": []}))
        mock_client.messages.create.return_value = catalog_response

        with (
            patch("services.corporate_memory.collector.HOME_BASE", home),
            patch("services.corporate_memory.collector._read_json", return_value={}),
            patch("services.corporate_memory.collector._write_json"),
            patch(
                "config.loader.load_instance_config",
                return_value={
                    "ai": {
                        "provider": "anthropic",
                        "api_key": "sk-ant-integration-test",
                        "model": "claude-haiku-4-5-20251001",
                    },
                },
            ),
        ):
            stats = collect_all(dry_run=True)

        # Verify Anthropic client was initialized
        mock_client_cls.assert_called_once_with(api_key="sk-ant-integration-test")
        assert stats["items_extracted"] == 0
        assert stats["errors"] == []

    def test_collector_raises_on_invalid_config(self, tmp_path):
        """Collector fail-fasts (raises ValueError) when ai: config is invalid (#176)."""
        from services.corporate_memory.collector import collect_all

        home = tmp_path / "home"
        home.mkdir()
        user_dir = home / "alice"
        user_dir.mkdir()
        (user_dir / "CLAUDE.local.md").write_text("Some knowledge")

        with (
            patch("services.corporate_memory.collector.HOME_BASE", home),
            patch("services.corporate_memory.collector._read_json", return_value={}),
            patch(
                "config.loader.load_instance_config",
                return_value={"ai": {"provider": "anthropic", "api_key": ""}},
            ),
            pytest.raises(ValueError, match="must not be empty"),
        ):
            collect_all(dry_run=True)


class TestVertexProvider:
    """provider: vertex — Claude through Google Vertex AI (ADC, no API key)."""

    def test_to_vertex_model_id_table(self):
        from connectors.llm.vertex_provider import to_vertex_model_id

        cases = {
            "claude-haiku-4-5-20251001": "claude-haiku-4-5@20251001",
            "claude-3-5-sonnet-20241022": "claude-3-5-sonnet@20241022",
            "claude-sonnet-4-6": "claude-sonnet-4-6",  # bare current-gen id
            "claude-haiku-4-5@20251001": "claude-haiku-4-5@20251001",  # idempotent
            "": "",
            "  claude-opus-4-7  ": "claude-opus-4-7",
        }
        for given, expected in cases.items():
            assert to_vertex_model_id(given) == expected, given

    @patch("connectors.llm.vertex_provider.anthropic.AnthropicVertex")
    def test_factory_builds_vertex_extractor(self, mock_vertex_cls):
        from connectors.llm.vertex_provider import VertexExtractor

        ext = create_extractor(
            {
                "provider": "vertex",
                "vertex": {"project_id": "proj-1", "region": "Europe-West1"},
                "model": "claude-haiku-4-5-20251001",
            }
        )
        assert isinstance(ext, VertexExtractor)
        assert ext._model == "claude-haiku-4-5@20251001"
        assert ext._TRACE_PROVIDER == "vertex"
        ctor = mock_vertex_cls.call_args.kwargs
        assert ctor == {"project_id": "proj-1", "region": "europe-west1"}

    @patch("connectors.llm.vertex_provider.anthropic.AnthropicVertex")
    def test_vertex_needs_no_api_key(self, mock_vertex_cls):
        """No api_key in the config is fine — auth is Google ADC in the SDK."""
        ext = create_extractor({"provider": "vertex", "vertex": {"project_id": "p"}})
        assert ext._model == DEFAULT_MODEL.replace("-20251001", "@20251001")

    def test_vertex_missing_project_id_raises(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
        with pytest.raises(ValueError, match="project id"):
            create_extractor({"provider": "vertex"})

    def test_vertex_region_defaults_to_global(self, monkeypatch):
        from connectors.llm.vertex_provider import resolve_vertex_settings

        monkeypatch.delenv("CLOUD_ML_REGION", raising=False)
        assert resolve_vertex_settings({"project_id": "p"}) == ("p", "global")

    def test_vertex_settings_env_fallback(self, monkeypatch):
        from connectors.llm.vertex_provider import resolve_vertex_settings

        monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "env-proj")
        monkeypatch.setenv("CLOUD_ML_REGION", "US-EAST5")
        assert resolve_vertex_settings(None) == ("env-proj", "us-east5")

    def test_vertex_malformed_settings_raise(self, monkeypatch):
        """The region is interpolated into the Vertex HOSTNAME and the project
        into the path, so both are held to the Google resource-id character
        set — otherwise `ai.provider: vertex` with a crafted region would send
        the server-side Google OAuth token to a non-Google host."""
        from connectors.llm.vertex_provider import resolve_vertex_settings

        monkeypatch.delenv("CLOUD_ML_REGION", raising=False)
        monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)

        for region in ("evil.com/x", "us-east5.evil.com", "eu@west", "-eu"):
            with pytest.raises(ValueError, match="region is malformed"):
                resolve_vertex_settings({"project_id": "p", "region": region})
        for project in ("proj/../x", "proj?a=b", "-proj"):
            with pytest.raises(ValueError, match="project_id is malformed"):
                resolve_vertex_settings({"project_id": project, "region": "us-east5"})

        assert resolve_vertex_settings({"project_id": "my-proj", "region": "us-east5"}) == ("my-proj", "us-east5")

    def test_vertex_missing_google_auth_fails_fast(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def _no_google(name, *a, **k):
            if name == "google.auth" or name.startswith("google.auth"):
                raise ImportError("No module named 'google'")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _no_google)
        with pytest.raises(ValueError, match=r"anthropic\[vertex\]"):
            create_extractor({"provider": "vertex", "vertex": {"project_id": "p"}})

    @patch("connectors.llm.vertex_provider.anthropic.AnthropicVertex")
    def test_vertex_extract_json_happy_path(self, mock_vertex_cls):
        from connectors.llm.factory import create_vertex_extractor

        mock_client = MagicMock()
        mock_vertex_cls.return_value = mock_client
        resp = MagicMock()
        resp.stop_reason = "end_turn"
        resp.content = [MagicMock(text='{"answer": 42}')]
        mock_client.messages.create.return_value = resp

        ext = create_vertex_extractor("claude-haiku-4-5-20251001", {"vertex": {"project_id": "p", "region": "global"}})
        out = ext.extract_json(
            prompt="q",
            max_tokens=64,
            json_schema={"type": "object", "properties": {"answer": {"type": "integer"}}},
            schema_name="t",
        )
        assert out == {"answer": 42}
        assert mock_client.messages.create.call_args.kwargs["model"] == "claude-haiku-4-5@20251001"

    def test_vertex_permission_denied_maps_to_auth_error(self):
        """403 (API not enabled / missing aiplatform permission) is an
        auth-shaped, non-retryable failure."""
        from connectors.llm.vertex_provider import VertexExtractor

        class _PermErr(anthropic.PermissionDeniedError):
            def __init__(self):
                Exception.__init__(self, "permission denied")

        ext = VertexExtractor.__new__(VertexExtractor)
        ext._model = "m"
        client = MagicMock()
        client.messages.create.side_effect = _PermErr()
        ext._client = client
        with pytest.raises(LLMAuthError, match="permission denied"):
            ext.extract_json(prompt="q", max_tokens=8, json_schema={"type": "object"}, schema_name="t")

    def test_vertex_google_auth_exception_maps_to_auth_error(self):
        from google.auth.exceptions import DefaultCredentialsError

        from connectors.llm.vertex_provider import VertexExtractor

        ext = VertexExtractor.__new__(VertexExtractor)
        ext._model = "m"
        client = MagicMock()
        client.messages.create.side_effect = DefaultCredentialsError("no ADC")
        ext._client = client
        with pytest.raises(LLMAuthError, match="GOOGLE_APPLICATION_CREDENTIALS"):
            ext.extract_json(prompt="q", max_tokens=8, json_schema={"type": "object"}, schema_name="t")

    @patch("connectors.llm.vertex_provider.anthropic.AnthropicVertex")
    def test_factory_log_carries_no_secrets(self, mock_vertex_cls, caplog):
        """project/region are not secrets and may be logged; nothing else is."""
        with caplog.at_level(logging.INFO, logger="connectors.llm.factory"):
            create_extractor({"provider": "vertex", "vertex": {"project_id": "proj-1", "region": "global"}})
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "proj-1" in joined
        assert "sk-" not in joined


class TestVertexConfigOrNone:
    def test_explicit_vertex_config_wins(self):
        from connectors.llm.factory import vertex_config_or_none

        assert vertex_config_or_none({"provider": "vertex", "vertex": {"project_id": "p", "region": "eu"}}) == (
            "p",
            "eu",
        )

    def test_other_provider_returns_none(self, monkeypatch):
        from connectors.llm.factory import vertex_config_or_none

        monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "env-proj")
        assert vertex_config_or_none({"provider": "anthropic", "api_key": "sk-x"}) is None

    def test_incomplete_vertex_block_warns_and_returns_none(self, monkeypatch, caplog):
        from connectors.llm.factory import vertex_config_or_none

        monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
        with caplog.at_level(logging.WARNING, logger="connectors.llm.factory"):
            assert vertex_config_or_none({"provider": "vertex"}) is None
        assert "incomplete" in caplog.text

    def test_env_pair_counts_only_without_static_keys(self, monkeypatch):
        from connectors.llm import factory

        monkeypatch.setattr(factory, "_load_ai_config_or_none", lambda: None)
        monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "env-proj")
        monkeypatch.setenv("CLOUD_ML_REGION", "global")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        assert factory.vertex_config_or_none(None) == ("env-proj", "global")
        # A static key wins — existing deployments are unchanged.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
        assert factory.vertex_config_or_none(None) is None
