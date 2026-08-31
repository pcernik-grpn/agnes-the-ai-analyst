"""Exception hierarchy for LLM connector errors.

All exceptions inherit from LLMError so callers can catch the base
class for broad error handling or specific subclasses for targeted
recovery strategies.
"""


class LLMError(Exception):
    """Base exception for all LLM-related errors."""


class LLMAuthError(LLMError):
    """Invalid API key or authentication failure.

    This is a permanent error - do not retry.
    """


class LLMRateLimitError(LLMError):
    """Rate limited by the provider.

    This is a transient error - retry with exponential backoff.
    """


class LLMTimeoutError(LLMError):
    """Timeout or connection error.

    This is a transient error - retry with exponential backoff.
    """


class LLMFormatError(LLMError):
    """Invalid JSON or unexpected response structure.

    The model returned content that could not be parsed as valid JSON
    or did not match the expected schema.
    """


class LLMUnsupportedError(LLMError):
    """Provider does not support a required feature.

    For example, the provider does not support structured output
    (json_schema response format) and the configuration does not
    allow fallback strategies.
    """


class LLMModelNotFoundError(LLMError):
    """The configured model does not exist for this provider/deployment.

    A permanent error with an operator-shaped fix — no retry and no
    fallback strategy will help. Distinct from LLMUnsupportedError, which
    means the model is there but will not do what the request asked for.
    On Vertex this is the "enabled in Model Garden for a different
    project/region than the one configured" case.
    """


class LLMRefusalError(LLMError):
    """Model refused to generate a response.

    Typically triggered by safety filters or content policy violations.
    """
