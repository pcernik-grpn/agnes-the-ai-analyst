"""Anthropic provider for structured JSON extraction.

Uses the Anthropic API with native structured output (json_schema)
for reliable JSON extraction. Includes retry logic for transient errors.
"""

import json
import logging
import time
from typing import Any

from .exceptions import (
    LLMAuthError,
    LLMFormatError,
    LLMModelNotFoundError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMTimeoutError,
    LLMUnsupportedError,
)

logger = logging.getLogger(__name__)


def __getattr__(name: str) -> Any:
    # The anthropic SDK import pulls its full type tree (hundreds of modules,
    # seconds of cold-import wall time), and this module sits on the app.main
    # import path via store_guardrails — so the SDK is imported inside the
    # methods that talk to the API, not at module import. This hook keeps
    # `connectors.llm.anthropic_provider.anthropic` resolvable for
    # mock.patch targets.
    if name == "anthropic":
        import anthropic

        return anthropic
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Retry configuration
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 2
BACKOFF_MULTIPLIER = 2

# Truncation retry: when the model hits max_tokens we retry with a
# doubled budget. Caps the multiplier at 4x the caller's original
# value so a runaway can't drain the per-call budget.
MAX_TRUNCATION_RETRIES = 2  # 2x then 4x
TRUNCATION_BUDGET_MULTIPLIER = 2


def _strict_json_schema(schema):
    """Return a copy of the schema with additionalProperties=False on every object type.

    The Anthropic structured-output API rejects schemas where a `{"type": "object"}` node
    omits `additionalProperties` (HTTP 400 invalid_request_error). We walk the schema
    recursively and force the field where missing.
    """
    if isinstance(schema, dict):
        out = {k: _strict_json_schema(v) for k, v in schema.items()}
        if out.get("type") == "object" and "additionalProperties" not in out:
            out["additionalProperties"] = False
        return out
    if isinstance(schema, list):
        return [_strict_json_schema(item) for item in schema]
    return schema


class AnthropicExtractor:
    """Structured JSON extractor using the Anthropic API.

    Uses output_config with json_schema format for structured output.
    Retries transient errors (rate limit, timeout, connection) with
    exponential backoff.
    """

    # Provider label recorded on observability traces; subclasses backed by
    # a different transport (e.g. Vertex) override this.
    _TRACE_PROVIDER = "anthropic"

    def __init__(self, api_key: str, model: str) -> None:
        """Initialize the Anthropic extractor.

        Args:
            api_key: Anthropic API key.
            model: Model identifier (e.g., "claude-haiku-4-5-20251001").
        """
        import anthropic  # deferred — see module __getattr__

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def extract_json(
        self,
        prompt: str,
        max_tokens: int,
        json_schema: dict,
        schema_name: str,
        system: str | None = None,
    ) -> dict:
        """Extract structured JSON using the Anthropic API.

        Args:
            prompt: User-content prompt sent to the model.
            max_tokens: Maximum tokens in the response.
            json_schema: JSON Schema that the response must conform to.
            schema_name: Human-readable name for the schema.
            system: Optional system prompt — keeps trust boundary intact
                when the user content contains untrusted data (e.g.
                files uploaded by third parties). When the caller passes
                a system prompt here, the prompt-injection threat model
                relies on the SDK's separate ``system=`` parameter so a
                crafted user payload can't override the rules.

        Returns:
            Parsed JSON dictionary conforming to the provided schema.

        Raises:
            LLMAuthError: Invalid API key.
            LLMModelNotFoundError: Model unknown to this provider/deployment.
            LLMUnsupportedError: The provider rejected the request as built.
            LLMRateLimitError: Rate limited after all retries.
            LLMTimeoutError: Timeout/connection error after all retries.
            LLMFormatError: Response is not valid JSON.
            LLMRefusalError: Model refused to respond.
        """
        last_exception: Exception | None = None
        # Truncation retries bump max_tokens; transient retries bump
        # backoff. Accounted separately so a verbose response under
        # rate-limit doesn't burn both budgets at once.
        truncation_retries = 0
        current_max_tokens = max_tokens

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return self._attempt_extraction(
                    prompt,
                    current_max_tokens,
                    json_schema,
                    schema_name,
                    attempt,
                    system=system,
                )
            except LLMAuthError:
                raise
            except LLMRefusalError:
                raise
            except LLMFormatError as e:
                # Truncation is a special case: same prompt + schema,
                # but the model didn't have room to finish. Retry with
                # a doubled budget — capped — instead of giving up.
                # Other format errors (bad JSON, schema mismatch) won't
                # benefit from more tokens, so re-raise immediately.
                if str(e).startswith("Response truncated") and truncation_retries < MAX_TRUNCATION_RETRIES:
                    truncation_retries += 1
                    current_max_tokens *= TRUNCATION_BUDGET_MULTIPLIER
                    logger.warning(
                        "Response truncated on attempt %d for model %s, retrying with max_tokens=%d (%dx initial)",
                        attempt,
                        self._model,
                        current_max_tokens,
                        TRUNCATION_BUDGET_MULTIPLIER**truncation_retries,
                    )
                    continue
                raise
            except (LLMRateLimitError, LLMTimeoutError) as e:
                last_exception = e
                if attempt < MAX_RETRIES:
                    delay = INITIAL_BACKOFF_SECONDS * (BACKOFF_MULTIPLIER ** (attempt - 1))
                    logger.warning(
                        "Transient error on attempt %d/%d for model %s, retrying in %ds: %s",
                        attempt,
                        MAX_RETRIES,
                        self._model,
                        delay,
                        type(e).__name__,
                    )
                    time.sleep(delay)

        raise last_exception  # type: ignore[misc]

    def _attempt_extraction(
        self,
        prompt: str,
        max_tokens: int,
        json_schema: dict,
        schema_name: str,
        attempt: int,
        system: str | None = None,
    ) -> dict:
        """Single extraction attempt against the Anthropic API."""
        logger.info(
            "Anthropic extraction attempt %d/%d, model=%s, schema=%s",
            attempt,
            MAX_RETRIES,
            self._model,
            schema_name,
        )

        import anthropic  # deferred — see module __getattr__

        from src.observability import trace_generation

        try:
            with trace_generation(provider=self._TRACE_PROVIDER, model=self._model) as _trace:
                _trace.set_input(prompt)
                create_kwargs = {
                    "model": self._model,
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                    "output_config": {
                        "format": {
                            "type": "json_schema",
                            "schema": _strict_json_schema(json_schema),
                        },
                    },
                }
                if system:
                    create_kwargs["system"] = system
                response = self._client.messages.create(**create_kwargs)
                _trace.set_output_from_anthropic(response)
        except anthropic.AuthenticationError as e:
            raise LLMAuthError("Anthropic authentication failed (check API key)") from e
        except anthropic.PermissionDeniedError as e:
            # 403: key lacks access, or (on Vertex) the API/model isn't
            # enabled for the project — an auth-shaped, non-retryable error.
            raise LLMAuthError("Anthropic permission denied (check credentials / model access)") from e
        except anthropic.RateLimitError as e:
            raise LLMRateLimitError("Anthropic rate limited") from e
        except (anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
            raise LLMTimeoutError(f"Anthropic connection error ({type(e).__name__})") from e
        except anthropic.NotFoundError as e:
            # 404: the deployment has never heard of this model. On Vertex that
            # usually means it is enabled in Model Garden for a different
            # project/region than the configured pair. Permanent, and only an
            # operator can fix it — so it is typed rather than left to escape
            # as a bare SDK exception the callers can only call "unknown".
            raise LLMModelNotFoundError(
                f"model {self._model!r} not available for this provider/deployment: {e}"
            ) from e
        except anthropic.BadRequestError as e:
            # 400: this deployment rejected the request as built. Carries the
            # provider's own words, which are the only thing that says WHICH
            # part it disliked.
            raise LLMUnsupportedError(f"request rejected by the provider: {e}") from e

        # Check for truncation - raise and let outer retry loop handle it
        if response.stop_reason == "max_tokens":
            raise LLMFormatError(f"Response truncated (max_tokens) for schema {schema_name}")

        # Check for refusal
        if response.stop_reason == "end_turn" and not response.content:
            raise LLMRefusalError(f"Model refused to generate response for schema {schema_name}")

        # Parse JSON from response
        try:
            text = response.content[0].text
            return json.loads(text)
        except (json.JSONDecodeError, IndexError, AttributeError) as e:
            raise LLMFormatError(
                f"Failed to parse Anthropic response as JSON for schema {schema_name} ({type(e).__name__})"
            ) from e
