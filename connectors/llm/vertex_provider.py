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

# The exact character classes ``app/api/broker_vertex.py``'s path parser
# enforces for the project and location segments. Configured values are held
# to them too, for two reasons:
#
# 1. A configured project/region outside these classes can never equal the
#    groups a native Vertex path parses to, so every sandbox request would be
#    refused with an opaque 403 and no boot-time signal.
# 2. Both are interpolated straight into the outbound URL for Messages-format
#    callers — and the region becomes part of the HOSTNAME
#    (``{region}-aiplatform.googleapis.com``). A region carrying ``/``, ``.``
#    or ``@`` would point the server-side Google OAuth token at a host that is
#    not Google's; a project id carrying ``/`` or ``?`` would walk the signed
#    request off its intended path. Validating where the value is read keeps
#    credential egress pinned, per the security playbook.
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REGION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def invalid_vertex_setting(project_id: str, region: str) -> str | None:
    """Name of the first malformed setting, or ``None`` when both are sane.

    Returns ``"project_id"`` / ``"region"`` so callers can name the offending
    key in an operator-facing message.
    """
    if not _PROJECT_ID_RE.match((project_id or "").strip()):
        return "project_id"
    if not _REGION_RE.match((region or "").strip().lower()):
        return "region"
    return None


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
    bad = invalid_vertex_setting(project_id, region)
    if bad:
        raise ValueError(
            f"Vertex {bad} is malformed — the value is interpolated into the "
            f"Vertex API URL (the region becomes part of the hostname), so it "
            f"is held to the Google resource-id character set: "
            f"{'letters, digits, dot, dash, underscore' if bad == 'project_id' else 'lowercase letters, digits, dash'}"
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
