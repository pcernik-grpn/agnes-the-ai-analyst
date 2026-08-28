"""LLM connector module for structured extraction.

Provides a provider-agnostic interface for extracting structured JSON
from language models. Supports Anthropic (native) and OpenAI-compatible
providers with automatic fallback strategies for structured output.
"""

from .base import StructuredExtractor
from .factory import (
    create_extractor,
    create_extractor_from_env_or_config,
    create_vertex_extractor,
    vertex_config_or_none,
)
from .vertex_provider import to_vertex_model_id

__all__ = [
    "StructuredExtractor",
    "create_extractor",
    "create_extractor_from_env_or_config",
    "create_vertex_extractor",
    "to_vertex_model_id",
    "vertex_config_or_none",
]
