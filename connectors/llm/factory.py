"""Factory for creating structured extractors from instance configuration.

Reads the ai: section from instance.yaml (already resolved by config/loader.py)
and creates the appropriate StructuredExtractor implementation.
"""

import logging
import os
from urllib.parse import urlparse

from .anthropic_provider import AnthropicExtractor
from .base import StructuredExtractor
from .openai_compat import OpenAICompatExtractor
from .vertex_provider import VertexExtractor, resolve_vertex_settings

logger = logging.getLogger(__name__)

# Default model when not specified in config
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# Default structured output strategy
DEFAULT_STRUCTURED_OUTPUT = "auto"

# Tier → concrete model ID. Used by guardrails (and any future feature)
# that wants to expose a "haiku|sonnet|opus" knob to operators without
# pinning them to a specific dated model. Update here when bumping the
# fleet to a newer model family — callers stay on the abstract tier.
MODEL_TIERS: dict[str, str] = {
    "haiku":  "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus":   "claude-opus-4-7",
}


def resolve_model_tier(tier: str) -> str:
    """Map an abstract tier ('haiku'|'sonnet'|'opus') to a concrete model ID.

    Accepts the tier name OR a concrete model ID (passed through unchanged
    so operators who already know the exact ID they want can hard-pin it
    in instance.yaml). Unknown tier names raise ValueError so a typo in
    config surfaces at startup, not at first review call.
    """
    if not tier:
        return DEFAULT_MODEL
    tier = tier.strip()
    if tier in MODEL_TIERS:
        return MODEL_TIERS[tier]
    if tier.startswith("claude-"):
        return tier
    raise ValueError(
        f"Unknown model tier {tier!r}. Use one of "
        f"{sorted(MODEL_TIERS)} or a concrete claude-* model ID."
    )


def create_extractor(ai_config: dict) -> StructuredExtractor:
    """Create a structured extractor from the ai: config section.

    Supports two configuration formats:

    New format (explicit provider):
        ai:
          provider: anthropic | openai_compat | vertex
          api_key: ${ANTHROPIC_API_KEY}       # not used by vertex (Google ADC)
          model: claude-haiku-4-5-20251001
          base_url: https://api.example.com/v1  # required for openai_compat
          structured_output: auto  # strict | json | auto (ignored by
                                   # anthropic/vertex — both are native json_schema)
          vertex:                  # vertex only
            project_id: my-gcp-project
            region: global

    Legacy format (backward compatible):
        ai:
          anthropic_api_key: ${ANTHROPIC_API_KEY}

    Args:
        ai_config: The ai: section dict from instance.yaml,
            already resolved by config/loader.py.

    Returns:
        A StructuredExtractor instance.

    Raises:
        ValueError: If configuration is invalid or incomplete.
    """
    if not ai_config or not isinstance(ai_config, dict):
        raise ValueError(
            "ai: section in instance.yaml must be a non-empty dict. "
            "Example:\n  ai:\n    provider: anthropic\n    api_key: ${ANTHROPIC_API_KEY}"
        )

    provider = ai_config.get("provider")

    # Legacy format detection: anthropic_api_key present, no provider
    if not provider and "anthropic_api_key" in ai_config:
        api_key = ai_config["anthropic_api_key"]
        _validate_api_key(api_key)
        model = ai_config.get("model", DEFAULT_MODEL)
        logger.info(
            "Creating AnthropicExtractor (legacy config), model=%s", model
        )
        return AnthropicExtractor(api_key=api_key, model=model)

    if not provider:
        raise ValueError(
            "ai.provider is required in instance.yaml. "
            "Supported: 'anthropic', 'openai_compat', 'vertex'. "
            "Hint: use ${ENV_VAR} syntax for secrets."
        )

    if provider == "vertex":
        # No API key in this mode — auth is Google ADC inside the SDK.
        # Fail fast on the missing extra: AnthropicVertex constructs fine
        # without google-auth and would only blow up at first request.
        try:
            import google.auth  # noqa: F401
        except ImportError as e:
            raise ValueError(
                "ai.provider 'vertex' requires the anthropic[vertex] extra (pip install 'anthropic[vertex]')"
            ) from e
        project_id, region = resolve_vertex_settings(ai_config.get("vertex"))
        model = ai_config.get("model", DEFAULT_MODEL)
        # project/region are not secrets — safe to log.
        logger.info("Creating VertexExtractor, project=%s, region=%s, model=%s", project_id, region, model)
        return VertexExtractor(project_id=project_id, region=region, model=model)

    api_key = ai_config.get("api_key", "")
    _validate_api_key(api_key)
    model = ai_config.get("model", DEFAULT_MODEL)

    if provider == "anthropic":
        logger.info("Creating AnthropicExtractor, model=%s", model)
        return AnthropicExtractor(api_key=api_key, model=model)

    elif provider == "openai_compat":
        base_url = ai_config.get("base_url", "")
        if not base_url:
            raise ValueError(
                "ai.base_url is required when provider is 'openai_compat'. "
                "Example: base_url: https://api.openai.com/v1"
            )
        structured_output = ai_config.get(
            "structured_output", DEFAULT_STRUCTURED_OUTPUT,
        )
        if structured_output not in ("strict", "json", "auto"):
            raise ValueError(
                f"ai.structured_output must be 'strict', 'json', or 'auto', "
                f"got '{structured_output}'"
            )

        verify_ssl = ai_config.get("verify_ssl", True)

        safe_url = _sanitize_url(base_url)
        logger.info(
            "Creating OpenAICompatExtractor, url=%s, model=%s, "
            "structured_output=%s, verify_ssl=%s",
            safe_url, model, structured_output, verify_ssl,
        )
        return OpenAICompatExtractor(
            api_key=api_key,
            base_url=base_url,
            model=model,
            structured_output=structured_output,
            verify_ssl=verify_ssl,
        )

    else:
        raise ValueError(
            f"Unknown ai.provider '{provider}'. "
            f"Supported: 'anthropic', 'openai_compat', 'vertex'. "
            f"Hint: use ${{ENV_VAR}} syntax for secrets."
        )


def create_extractor_from_env_or_config(
    ai_config: dict | None,
) -> StructuredExtractor:
    """Build an extractor from config, falling back to env vars.

    Resolution order (#176):

    1. ``ai_config`` is a non-empty dict → delegate to :func:`create_extractor`.
    2. ``ANTHROPIC_API_KEY`` set → AnthropicExtractor with the default model.
    3. ``LLM_API_KEY`` set without a base_url → AnthropicExtractor (the proxy
       case typically also wires a base_url, in which case the operator should
       use the explicit ai: block; this fallback is a best-effort convenience).
    4. Vertex env pair (ANTHROPIC_VERTEX_PROJECT_ID [+ CLOUD_ML_REGION]) set
       → VertexExtractor with the default model. Static keys deliberately win
       over the env pair so existing deployments are unchanged.
    5. Otherwise raise ``ValueError`` with a clear actionable message — never
       silently exit, never return ``None``. The previous "skip when ai: is
       missing" behavior was the silent-failure root cause in #176.
    """
    if ai_config:
        return create_extractor(ai_config)

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    llm_key = os.environ.get("LLM_API_KEY", "").strip()

    if anthropic_key:
        logger.info(
            "No ai: block in instance.yaml; falling back to ANTHROPIC_API_KEY env var"
        )
        return AnthropicExtractor(api_key=anthropic_key, model=DEFAULT_MODEL)

    if llm_key:
        logger.info(
            "No ai: block in instance.yaml; falling back to LLM_API_KEY env var"
        )
        return AnthropicExtractor(api_key=llm_key, model=DEFAULT_MODEL)

    vertex = vertex_config_or_none(None)
    if vertex:
        logger.info("No ai: block in instance.yaml; falling back to Vertex env vars (project=%s)", vertex[0])
        return VertexExtractor(project_id=vertex[0], region=vertex[1], model=DEFAULT_MODEL)

    raise ValueError(
        "LLM not configured. Add an ai: block to instance.yaml (see "
        "config/instance.yaml.example — provider: anthropic | openai_compat | "
        "vertex) OR set ANTHROPIC_API_KEY / LLM_API_KEY / "
        "ANTHROPIC_VERTEX_PROJECT_ID in the environment. The corporate-memory "
        "and verification-detector services cannot run without one of these."
    )


def _load_ai_config_or_none() -> dict | None:
    """Best-effort load of the instance.yaml ``ai:`` block.

    Lazy import so this module stays importable without the config package
    (precedent: connectors/keboola/extractor.py). Never raises.
    """
    try:
        from config.loader import load_instance_config

        cfg = load_instance_config()
        ai = cfg.get("ai") if isinstance(cfg, dict) else None
        return ai if isinstance(ai, dict) else None
    except Exception:  # pragma: no cover - any config failure means "no ai block"  # noqa: BLE001
        return None


def vertex_config_or_none(ai_config: dict | None = None) -> tuple[str, str] | None:
    """(project_id, region) when this instance should use Vertex, else None.

    Precedence:
    - An ``ai:`` block (passed in, or loaded from instance.yaml when omitted)
      with ``provider: vertex`` → its settings.
    - An ``ai:`` block naming any other provider → None (explicit config wins).
    - No ``ai:`` block → the env pair counts only when neither
      ANTHROPIC_API_KEY nor LLM_API_KEY is set (static keys win, so every
      existing deployment is byte-for-byte unchanged).

    Never raises: a ``provider: vertex`` block with a missing project_id logs
    a warning and returns None here — the creation paths raise instead.
    """
    cfg = ai_config if ai_config is not None else _load_ai_config_or_none()
    if cfg is not None:
        if cfg.get("provider") != "vertex":
            return None
        try:
            return resolve_vertex_settings(cfg.get("vertex"))
        except ValueError as e:
            logger.warning("ai.provider is 'vertex' but its config is incomplete: %s", e)
            return None

    if os.environ.get("ANTHROPIC_API_KEY", "").strip() or os.environ.get("LLM_API_KEY", "").strip():
        return None
    if not os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "").strip():
        return None
    try:
        return resolve_vertex_settings(None)
    except ValueError:  # pragma: no cover - project id checked just above
        return None


def create_vertex_extractor(model: str, ai_config: dict | None = None) -> VertexExtractor:
    """VertexExtractor for a caller that already chose Vertex.

    Resolves project/region the same way as :func:`vertex_config_or_none`
    but raises (instead of returning None) when the config is incomplete.
    """
    cfg = ai_config if ai_config is not None else _load_ai_config_or_none()
    vertex_block = cfg.get("vertex") if isinstance(cfg, dict) else None
    project_id, region = resolve_vertex_settings(vertex_block)
    return VertexExtractor(project_id=project_id, region=region, model=model)


def _validate_api_key(api_key: str) -> None:
    """Validate that an API key is present and non-empty.

    Raises:
        ValueError: If api_key is empty or missing.
    """
    if not api_key or not api_key.strip():
        raise ValueError(
            "ai.api_key (or ai.anthropic_api_key) must not be empty. "
            "Check that the corresponding environment variable is set "
            "and referenced with ${ENV_VAR} syntax in instance.yaml."
        )


def _sanitize_url(url: str) -> str:
    """Extract scheme://host from a URL for safe logging."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"
