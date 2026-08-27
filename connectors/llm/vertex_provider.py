"""Google Vertex AI provider for structured JSON extraction.

Claude on Vertex AI speaks the same Messages API as the first-party
endpoint — this module reuses :class:`AnthropicExtractor` wholesale and
only swaps the client (``anthropic.AnthropicVertex``) and the model-id
form. Auth is Google Application Default Credentials resolved inside the
SDK (GOOGLE_APPLICATION_CREDENTIALS -> gcloud ADC -> GCE/GKE metadata);
no API key exists in this mode.

This module is also the single home of :func:`to_vertex_model_id` — the
broker (``app/api/broker_vertex.py``) and readiness probes import it from
here rather than growing their own copies.
"""

import logging
import os
import re
from typing import Any

from .anthropic_provider import AnthropicExtractor
from .exceptions import LLMAuthError

logger = logging.getLogger(__name__)


def __getattr__(name: str) -> Any:
    # Same deferred-import hook as anthropic_provider: keeps the heavy SDK
    # off the app.main import path while leaving
    # `connectors.llm.vertex_provider.anthropic` patchable in tests.
    if name == "anthropic":
        import anthropic

        return anthropic
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# First-party dated model ids end in "-YYYYMMDD"; Vertex spells the same
# snapshot with an "@" separator ("claude-haiku-4-5@20251001"). Bare
# current-gen ids ("claude-sonnet-4-6") are identical on both platforms.
_DATED_ID = re.compile(r"^(.*)-(\d{8})$")

_DEFAULT_REGION = "global"


def to_vertex_model_id(model: str) -> str:
    """Translate a first-party model id into its Vertex AI form.

    'claude-haiku-4-5-20251001' -> 'claude-haiku-4-5@20251001'; ids that
    already carry an '@' (or any non-dated id) pass through unchanged, so
    the function is idempotent and accepts either spelling.
    """
    m = (model or "").strip()
    if "@" in m:
        return m
    dated = _DATED_ID.match(m)
    if dated:
        return f"{dated.group(1)}@{dated.group(2)}"
    return m


def resolve_vertex_settings(vertex_cfg: dict | None) -> tuple[str, str]:
    """Resolve (project_id, region) from a config dict + environment.

    Accepts any plain dict with optional ``project_id`` / ``region`` keys
    (works for both the ``ai.vertex`` and ``chat.llm.vertex`` blocks).
    Falls back to the SDK-standard env vars ANTHROPIC_VERTEX_PROJECT_ID and
    CLOUD_ML_REGION; region defaults to "global". Missing project_id is a
    hard error — there is no sane default for where spend lands.
    """
    cfg = vertex_cfg if isinstance(vertex_cfg, dict) else {}
    project_id = str(cfg.get("project_id") or "").strip() or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "").strip()
    region = (
        str(cfg.get("region") or "").strip() or os.environ.get("CLOUD_ML_REGION", "").strip() or _DEFAULT_REGION
    ).lower()
    if not project_id:
        raise ValueError(
            "Vertex provider needs a GCP project id — set ai.vertex.project_id "
            "in instance.yaml or the ANTHROPIC_VERTEX_PROJECT_ID env var"
        )
    return project_id, region


def create_vertex_client(*, project_id: str, region: str, timeout: float | None = None):
    """Raw ``anthropic.AnthropicVertex`` client for non-extractor call-sites

    (auto-title, vision OCR, readiness probes). Credentials come from
    Google ADC inside the SDK; nothing is minted here.
    """
    import anthropic  # deferred — see module __getattr__

    kwargs: dict[str, Any] = {"project_id": project_id, "region": region}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return anthropic.AnthropicVertex(**kwargs)


def _is_google_auth_error(exc: BaseException) -> bool:
    """True when the exception chain contains a google.auth failure."""
    seen: BaseException | None = exc
    while seen is not None:
        if type(seen).__module__.startswith("google.auth"):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


class VertexExtractor(AnthropicExtractor):
    """Structured JSON extractor running Claude through Google Vertex AI.

    Inherits the whole retry/truncation/structured-output loop from
    AnthropicExtractor; only the client construction and the model-id
    spelling differ. Structured output (output_config json_schema) is GA
    on Vertex, so the request shape carries over unchanged.
    """

    _TRACE_PROVIDER = "vertex"

    def __init__(self, project_id: str, region: str, model: str) -> None:
        # Deliberately does NOT call super().__init__ — there is no API key.
        self._client = create_vertex_client(project_id=project_id, region=region)
        self._model = to_vertex_model_id(model)

    def _attempt_extraction(self, *args: Any, **kwargs: Any) -> dict:
        try:
            return super()._attempt_extraction(*args, **kwargs)
        except Exception as e:
            if _is_google_auth_error(e):
                raise LLMAuthError(
                    "Google ADC credentials unavailable — set GOOGLE_APPLICATION_CREDENTIALS "
                    "or run on a GCP workload identity"
                ) from e
            raise
