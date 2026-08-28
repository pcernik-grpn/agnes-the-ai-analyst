"""Google Cloud access tokens for the chat broker's Vertex AI mode.

Server-side only — mirrors ``app/auth/wif.py``'s consumer interface. When
``chat.llm.provider: vertex`` is set, the broker forwards sandbox requests
to ``…aiplatform.googleapis.com`` and signs them with a Google OAuth access
token minted here. The token never enters the sandbox, so the chat sandbox
secret-broker isolation (INC-01572) is preserved: the sandbox CLI runs in
skip-auth gateway mode and sends no credentials at all.

Credential resolution is Google Application Default Credentials, i.e. the
standard google-auth chain: ``GOOGLE_APPLICATION_CREDENTIALS`` service
account JSON → gcloud user ADC (``gcloud auth application-default login``)
→ GCE/GKE metadata server. The google-auth Credentials object tracks its
own expiry; we hold it module-level (lock-guarded) and refresh on demand,
mirroring ``connectors/bigquery/auth.py``'s cached-token posture.

Only the broker mints raw tokens. Every other Vertex call-site (auto-title,
vision OCR, readiness probes, ``connectors/llm``) goes through
``anthropic.AnthropicVertex``, which owns its credential lifecycle itself.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

# Readiness probes call credentials_resolvable() on every admin poll — memoize
# a success this long so non-vertex latency never creeps into the page.
_RESOLVABLE_MEMO_TTL_S = 300.0

_lock = threading.Lock()
_credentials: Any | None = None
# (ok, detail, expiry_monotonic) or None.
_resolvable_memo: tuple[bool, str, float] | None = None


class VertexAuthError(RuntimeError):
    """Raised when a Google access token for Vertex AI cannot be obtained."""


def clear_token_cache() -> None:
    """Drop the cached credentials so the next call re-resolves from scratch.

    Call after an authoritative 401 from Vertex — the cached token may have
    been revoked before its declared expiry.
    """
    global _credentials, _resolvable_memo
    with _lock:
        _credentials = None
        _resolvable_memo = None


def _resolve_credentials() -> Any:
    """google.auth.default() with the cloud-platform scope.

    Raises VertexAuthError with an actionable, aggregated message — the
    google-auth chain already tried GOOGLE_APPLICATION_CREDENTIALS, gcloud
    ADC, and the metadata server before giving up.
    """
    try:
        import google.auth
    except ImportError as e:
        raise VertexAuthError(
            "google-auth is not installed — the vertex provider requires the "
            "anthropic[vertex] extra (pip install 'anthropic[vertex]')"
        ) from e
    try:
        credentials, project = google.auth.default(scopes=[_SCOPE])
    except Exception as e:
        raise VertexAuthError(
            "Google ADC credentials unavailable for Vertex AI — set "
            "GOOGLE_APPLICATION_CREDENTIALS to a service-account JSON, run "
            "`gcloud auth application-default login`, or run on a GCE/GKE "
            f"workload with an attached service account ({e})"
        ) from e
    logger.debug("Resolved Google ADC credentials (default project=%s)", project)
    return credentials


def get_vertex_access_token() -> str:
    """Return a cached Google access token, minting/refreshing as needed.

    Raises ``VertexAuthError`` if the token cannot be obtained.
    """
    global _credentials
    with _lock:
        if _credentials is None:
            _credentials = _resolve_credentials()
        creds = _credentials
        if not creds.valid:
            try:
                from google.auth.transport.requests import Request

                creds.refresh(Request())
            except VertexAuthError:
                raise
            except Exception as e:
                # A refresh failure may mean the underlying source rotated
                # (revoked SA key, expired gcloud login) — drop the cache so
                # the next call re-resolves instead of retrying a dead object.
                _credentials = None
                raise VertexAuthError(f"Google access token refresh failed: {e}") from e
        token = getattr(creds, "token", None)
        if not token:
            _credentials = None
            raise VertexAuthError("Google credentials refreshed but produced no access token")
        return token


def credentials_resolvable() -> tuple[bool, str]:
    """(ok, detail) — can ADC credentials be resolved at all? No refresh.

    Used by the boot gate and readiness rows; a success is memoized for
    ~5 minutes so repeated admin polls stay cheap. A failure is NOT memoized
    — the operator is presumably fixing it and wants the next poll fresh.
    """
    global _resolvable_memo
    with _lock:
        if _resolvable_memo is not None:
            ok, detail, expiry = _resolvable_memo
            if time.monotonic() < expiry:
                return ok, detail
        try:
            _resolve_credentials()
        except VertexAuthError as e:
            return False, str(e)
        result = (True, "google ADC credentials resolvable")
        _resolvable_memo = (*result, time.monotonic() + _RESOLVABLE_MEMO_TTL_S)
        return result
