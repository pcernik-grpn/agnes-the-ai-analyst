"""Pure helpers for the chat broker's Vertex AI mode (``chat.llm.provider: vertex``).

The sandbox CLI runs in Claude Code's documented LLM-gateway mode
(``CLAUDE_CODE_USE_VERTEX=1`` + ``CLAUDE_CODE_SKIP_VERTEX_AUTH=1`` with
``ANTHROPIC_VERTEX_BASE_URL`` pointed at the loopback relay), so the broker
receives Vertex-shaped paths::

    /v1/projects/{project}/locations/{location}/publishers/anthropic/models/{model}:streamRawPredict

Everything here is pure string work so it unit-tests without HTTP fixtures:

- :func:`parse_vertex_path` recognizes those paths. It runs ONLY on the
  output of ``broker._normalize_upstream_path`` (dot-segments/backslashes
  already refused, duplicate slashes collapsed) — the same string the
  outbound URL is built from, extending the existing "guard and destination
  decide on the same value" contract. The regex is anchored and every
  quantifier ranges over a single bounded character class (linear-time per
  the security playbook); a percent-encoded ``%3A`` verb never matches and
  falls through to the vertex-mode refusal — fail closed.
- :class:`VertexTarget` rebuilds the outbound path from the captured groups,
  never re-emitting the raw string.
- :func:`messages_to_vertex` / :func:`count_tokens_to_vertex` rewrite plain
  Anthropic Messages calls (the kai-agent engine, any Messages-format
  client) into the Vertex shape, so those callers need no change.

Project/location are validated by equality against instance config in
:func:`validate_vertex_target` — the sandbox can never choose where spend
lands; tampering with its (non-secret) env hints earns a 403 upstream.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from connectors.llm.vertex_provider import to_vertex_model_id

# Body field Vertex requires in place of the first-party anthropic-version
# semantics (the header is still forwarded; Vertex ignores it).
VERTEX_ANTHROPIC_VERSION = "vertex-2023-10-16"

# The token-counting endpoint is addressed as a pseudo-model on Vertex.
COUNT_TOKENS_MODEL = "count-tokens"

# Anchored, linear-time: each segment is one bounded character class. The
# optional "v1/" covers both known client shapes — the Claude CLI emits
# "{base}/v1/projects/..." while the Anthropic SDK's Vertex client emits
# "/projects/..." against a "/v1" base. Both canonicalize to the /v1 form.
_VERTEX_PATH_RE = re.compile(
    r"^/(?:v1/)?projects/([A-Za-z0-9][A-Za-z0-9._-]{0,63})"
    r"/locations/([a-z0-9][a-z0-9-]{0,31})"
    r"/publishers/anthropic/models/([A-Za-z0-9][A-Za-z0-9._@-]{0,127})"
    r":(streamRawPredict|rawPredict)$"
)

# The exact character class _VERTEX_PATH_RE's model group enforces, applied to
# any model string that is REBUILT into an outbound URL from a request body
# (messages_to_vertex). Without this, a body model like
# "m/../../publishers/google/models/x" would survive json parsing, land in
# VertexTarget.upstream_path, and httpx would collapse the dot-segments —
# walking the signed request out of publishers/anthropic. No '/', '?', '#',
# ':' or whitespace can pass; '..' without '/' is a harmless literal segment.
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$")


@dataclass(frozen=True)
class VertexTarget:
    """One parsed Vertex model-invocation path."""

    project: str
    location: str
    model: str
    verb: str

    @property
    def upstream_path(self) -> str:
        """Canonical outbound path, rebuilt from the captured groups."""
        return (
            f"/v1/projects/{self.project}/locations/{self.location}"
            f"/publishers/anthropic/models/{self.model}:{self.verb}"
        )


def parse_vertex_path(normalized_path: str) -> VertexTarget | None:
    """Parse a normalized broker subpath as a Vertex model invocation.

    Returns None for anything that is not exactly a publishers/anthropic
    model call with a known verb — the broker treats that as an unsupported
    subpath in vertex mode (fail closed), never as "forward it anyway".
    """
    m = _VERTEX_PATH_RE.match(normalized_path)
    if not m:
        return None
    return VertexTarget(project=m.group(1), location=m.group(2), model=m.group(3), verb=m.group(4))


def vertex_upstream_base(region: str) -> str:
    """The pinned Vertex API host for a configured region."""
    r = (region or "").strip().lower()
    if r == "global":
        return "https://aiplatform.googleapis.com"
    return f"https://{r}-aiplatform.googleapis.com"


def validate_vertex_target(target: VertexTarget, project_id: str, region: str) -> str | None:
    """``"vertex_target_not_allowed"`` unless the path's project and location
    equal the instance config, else ``None``.

    Equality against config is the security boundary: the sandbox's env only
    carries routing *hints*; this check is what actually pins where spend
    lands.
    """
    if target.project != project_id or target.location != (region or "").strip().lower():
        return "vertex_target_not_allowed"
    return None


def messages_to_vertex(raw_body: bytes, project_id: str, region: str) -> tuple[str, bytes, str]:
    """Rewrite a first-party ``POST /v1/messages`` body into Vertex shape.

    Returns ``(outbound_path, outbound_body, model)`` where ``model`` is the
    body's original spelling (for the caller's policy gate). The ``model``
    field moves from the body into the URL (translated to the Vertex id
    form); ``anthropic_version`` is injected only when absent; the verb
    follows the body's ``stream`` flag.

    Raises ``ValueError`` on a non-object JSON body or a missing/non-string
    model — the caller maps that to a 400.
    """
    try:
        body = json.loads(raw_body) if raw_body else None
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        raise ValueError("body is not valid JSON") from e
    if not isinstance(body, dict):
        raise ValueError("body is not a JSON object")  # noqa: TRY004 — untrusted input, caller catches ValueError
    model = body.pop("model", None)
    if not model or not isinstance(model, str):
        raise ValueError("body has no model field")
    vertex_model = to_vertex_model_id(model)
    # The body's model moves into the URL — hold it to the same anchored
    # character class the native-path parser enforces, or a crafted model
    # string becomes a path/query escape once httpx canonicalizes the URL.
    if not _MODEL_ID_RE.match(vertex_model):
        raise ValueError("model contains characters not allowed in a Vertex model id")
    body.setdefault("anthropic_version", VERTEX_ANTHROPIC_VERSION)
    verb = "streamRawPredict" if body.get("stream") is True else "rawPredict"
    target = VertexTarget(project=project_id, location=(region or "").strip().lower(), model=vertex_model, verb=verb)
    return target.upstream_path, json.dumps(body).encode("utf-8"), model


def count_tokens_to_vertex(raw_body: bytes, project_id: str, region: str) -> tuple[str, bytes]:
    """Rewrite a ``POST /v1/messages/count_tokens`` body into Vertex shape.

    Unlike a completion, token counting keeps the model IN the body
    (translated to the Vertex form) and is addressed to the
    ``count-tokens:rawPredict`` pseudo-model path.
    """
    try:
        body = json.loads(raw_body) if raw_body else None
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        raise ValueError("body is not valid JSON") from e
    if not isinstance(body, dict):
        raise ValueError("body is not a JSON object")  # noqa: TRY004 — untrusted input, caller catches ValueError
    model = body.get("model")
    if not model or not isinstance(model, str):
        raise ValueError("body has no model field")
    body["model"] = to_vertex_model_id(model)
    body.setdefault("anthropic_version", VERTEX_ANTHROPIC_VERSION)
    target = VertexTarget(
        project=project_id,
        location=(region or "").strip().lower(),
        model=COUNT_TOKENS_MODEL,
        verb="rawPredict",
    )
    return target.upstream_path, json.dumps(body).encode("utf-8")
