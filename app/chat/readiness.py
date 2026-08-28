"""Cloud-chat readiness — secret presence + live key validation.

The chat feature depends on server-env secrets:
  - ``ANTHROPIC_API_KEY``  — the agent (and auto-title) call Claude with it.
  - ``APPS_RUNNER_TOKEN``  — authenticates the gateway to the apps-runner
                             sidecar that owns the Docker socket (only when
                             ``provider='docker'``).
  - ``KAI_HOST_JWT_SECRET``— the embedded engine's shared secret (only when
                             ``provider='kai-agent'``).
  - ``JWT_SECRET_KEY``     — desktop / WS auth tokens are signed with it.

The startup gates in ``app/main.py`` refuse to build ``ChatManager`` when any
required secret is missing (chat then 503s). This module surfaces that state
to admins **without leaking values** (presence only), and offers live
"does the key actually work" probes — a present-but-invalid key otherwise
only fails at the first sandbox spawn, deep inside a user's chat.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

# Env-var names chat reads — the single source of truth for what it needs.
ENV_ANTHROPIC = "ANTHROPIC_API_KEY"
ENV_JWT = "JWT_SECRET_KEY"
ENV_APPS_RUNNER_TOKEN = "APPS_RUNNER_TOKEN"

# Machine-readable LLM-failure reasons. Shared by the admin "test connection"
# probe and the runtime broker forward path so both classify an auth/credit/
# outage failure identically (#884).
LLM_REASON_AUTH = "auth_invalid"  # 401/403 — key invalid, expired, or lacking permission
LLM_REASON_CREDIT = "credit_exhausted"  # 400 "credit balance too low" — valid key, unfunded account
LLM_REASON_PROVIDER = "provider_error"  # network / rate-limit / provider outage / other

# Mirror the threshold app/main.py's ``_chat_jwt_secret_ok`` enforces.
_JWT_MIN_BYTES = 32


def _is_set(env_name: str) -> bool:
    return bool(os.environ.get(env_name, "").strip())


def secret_status(chat_config: Any) -> dict:
    """Presence-only readiness snapshot. Never returns secret values.

    ``chat_config`` may be ``None`` (treated as disabled). Each secret entry
    carries ``set`` (is it present/strong-enough) and ``required`` (does the
    current config actually need it), so the UI can show "missing but needed"
    distinctly from "unset and that's fine".
    """
    enabled = bool(getattr(chat_config, "enabled", False))
    provider = getattr(chat_config, "provider", "kai-agent") or "kai-agent"
    docker_needed = enabled and provider == "docker"
    kai_agent_needed = enabled and provider == "kai-agent"
    # In workload_identity mode there is intentionally NO static ANTHROPIC_API_KEY
    # — don't flag it as a missing secret in the admin UI. Same in vertex mode:
    # no Anthropic credential exists at all (Google ADC signs upstream calls).
    llm_auth = getattr(chat_config, "llm_auth", "api_key")
    llm_provider = getattr(chat_config, "llm_provider", "anthropic") or "anthropic"
    vertex_needed = enabled and llm_provider == "vertex"
    anthropic_key_needed = enabled and llm_auth != "workload_identity" and not vertex_needed

    jwt_val = os.environ.get(ENV_JWT, "")
    jwt_ok = len(jwt_val.encode()) >= _JWT_MIN_BYTES

    # Probe Google ADC only when vertex actually needs it — the resolver can
    # touch the metadata server, and non-vertex deployments must never pay
    # that latency on an admin poll (the helper memoizes successes anyway).
    google_creds_ok = False
    if vertex_needed:
        from app.auth.vertex_gcp import credentials_resolvable

        google_creds_ok, _ = credentials_resolvable()

    secrets = {
        "anthropic_api_key": {"set": _is_set(ENV_ANTHROPIC), "required": anthropic_key_needed},
        "jwt_secret_key": {"set": jwt_ok, "required": enabled},
        # Docker provider: the sidecar credential (a real secret) plus the
        # sandbox image tag. Neither is needed on a kai-agent deployment.
        "apps_runner_token": {"set": _is_set(ENV_APPS_RUNNER_TOKEN), "required": docker_needed},
        "chat_docker_image": {
            "set": bool(getattr(chat_config, "docker_image", None)),
            "required": docker_needed,
        },
        # kai-agent provider: the shared engine JWT secret is the one boot
        # requirement (_chat_kai_agent_ok mirrors this) — without it every
        # session mint 503s, so the admin banner must show it as missing.
        "kai_host_jwt_secret": {"set": _is_set("KAI_HOST_JWT_SECRET"), "required": kai_agent_needed},
        # Vertex mode: not secrets, but the readiness banner must show a
        # missing project/region/ADC the same way it shows a missing key.
        "vertex_project_id": {
            "set": bool(getattr(chat_config, "vertex_project_id", "")),
            "required": vertex_needed,
        },
        "vertex_region": {
            "set": bool(getattr(chat_config, "vertex_region", "")),
            "required": vertex_needed,
        },
        "google_credentials": {"set": google_creds_ok, "required": vertex_needed},
    }
    missing = sorted(k for k, v in secrets.items() if v["required"] and not v["set"])

    # Two cost caps read `chat_messages.tokens_in/out`, which only a frame
    # carrying usage writes. The engine's stream carries none, so on this
    # provider `daily_anthropic_spend_usd` and `max_session_tokens` are inert
    # — and both ship LIVE defaults ($20/day, 200k/session), so flipping one
    # YAML key silently removes two budgets instance-wide. Surfaced rather
    # than left to be discovered from a bill: the operator can still cap
    # spend per agent via `token_budget_monthly`, which the broker enforces
    # on the engine's `llm` ticket.
    unmetered = [
        name
        for name, live in (
            ("daily_anthropic_spend_usd", getattr(chat_config, "daily_anthropic_spend_usd", None)),
            ("max_session_tokens", getattr(chat_config, "max_session_tokens", None)),
        )
        if provider == "kai-agent" and live
    ]

    return {
        "enabled": enabled,
        "provider": provider,
        "llm_provider": llm_provider,
        "secrets": secrets,
        "missing": missing,
        "ready": enabled and not missing,
        "unmetered_caps": unmetered,
    }


def classify_llm_failure(
    status_code: Optional[int],
    message: str = "",
    *,
    exc_name: str = "",
) -> dict:
    """Classify an LLM API failure into an actionable operator reason.

    Distinguishes the three cases #884 calls out:
      - invalid / expired / insufficient-permission key (HTTP 401/403)
      - valid key but unfunded account ("credit balance too low", HTTP 400)
      - everything else (network / rate-limit / provider outage)

    Returns ``{reason, detail}`` where ``reason`` is one of the ``LLM_REASON_*``
    constants. One classifier, two call sites — the SDK-exception path
    (``_classify``, admin "test connection") and the broker forward path
    (an ``httpx.Response`` at chat runtime) — so the admin surface and the live
    signal never diverge.
    """
    text = (message or "").lower()
    # Credit exhaustion is a 400 with a distinctive body — check it before the
    # generic status buckets (a valid key can still hit this).
    if "credit balance is too low" in text or ("credit" in text and "balance" in text):
        return {
            "reason": LLM_REASON_CREDIT,
            "detail": "credit balance too low — the LLM key is valid but the account is unfunded",
        }
    if status_code in (401, 403):
        suffix = f" ({exc_name})" if exc_name else f" (HTTP {status_code})"
        return {
            "reason": LLM_REASON_AUTH,
            "detail": f"authentication failed{suffix} — LLM key invalid, expired, or lacking permission",
        }
    base = exc_name or (f"HTTP {status_code}" if status_code else "provider error")
    msg = (message or "").strip()
    detail = f"{base}: {msg[:200]}" if msg else base
    return {"reason": LLM_REASON_PROVIDER, "detail": detail}


def _classify(exc: Exception) -> str:
    """Turn an SDK exception into a short admin-facing reason.

    Thin wrapper over ``classify_llm_failure`` (the shared classifier) that
    pulls the status code / name / message off the SDK exception. Distinguishes
    auth failures (wrong key), credit exhaustion (unfunded account), and
    everything else (network, rate-limit, provider outage) so the admin knows
    whether to fix the key, fund the account, or look at connectivity.
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
    name = type(exc).__name__
    msg = str(exc)
    # Some SDK versions signal auth via the exception *type* name without a
    # status_code — normalize those to 401 so the auth branch fires.
    if status is None and ("authentication" in name.lower() or "permission" in name.lower()):
        status = 401
    return classify_llm_failure(status, msg, exc_name=name)["detail"]


# ---------------------------------------------------------------------------
# Runtime LLM-failure signal
# ---------------------------------------------------------------------------
# The chat broker (app/api/broker.py) forwards agent traffic to the LLM API; a
# 401/400 there otherwise surfaces only as an opaque synthetic assistant
# message inside a user's chat (#884). The broker records the classified
# failure here, keyed on the app instance's ``state``, so the admin readiness
# surface can show a clear, actionable banner ("LLM key invalid" vs "account
# unfunded" vs "provider outage") instead of operators guessing.
_LLM_DIAG_ATTR = "llm_runtime_diagnostic"


def record_llm_runtime_failure(app_state: Any, status_code: Optional[int], message: str = "") -> dict:
    """Classify and store the latest runtime LLM failure on ``app_state``.

    Returns the recorded diagnostic (``{reason, detail, status_code, at}``).
    Never raises — a failed record must not break the request path.
    """
    diag = classify_llm_failure(status_code, message)
    diag["status_code"] = status_code
    diag["at"] = datetime.now(timezone.utc).isoformat()
    try:
        setattr(app_state, _LLM_DIAG_ATTR, diag)
    except Exception:  # noqa: BLE001 — best-effort signal, never fatal
        pass
    return diag


def clear_llm_runtime_diagnostic(app_state: Any) -> None:
    """Clear any recorded runtime LLM failure (called on a successful call)."""
    try:
        if getattr(app_state, _LLM_DIAG_ATTR, None) is not None:
            setattr(app_state, _LLM_DIAG_ATTR, None)
    except Exception:  # noqa: BLE001
        pass


def get_llm_runtime_diagnostic(app_state: Any) -> Optional[dict]:
    """Return the last recorded runtime LLM failure, or ``None`` if healthy."""
    return getattr(app_state, _LLM_DIAG_ATTR, None)


async def test_docker_sandbox(image: Optional[str] = None, *, timeout: float = 8.0) -> dict:
    """Probe the docker chat-sandbox runner.

    One round trip to the apps-runner sidecar's ``/sandboxes/probe`` — the
    gateway never talks to the Docker socket itself — which answers whether the
    daemon is reachable and the configured image is present. Returns
    ``{ok, detail}`` and never raises: an unreachable sidecar is the most
    common misconfiguration and must read as an actionable message, not a 500
    out of the admin endpoint.
    """
    from app.chat.sandbox_runner_client import SandboxRunnerClient

    try:
        result = await SandboxRunnerClient(timeout=timeout).probe(image or "")
    except Exception as exc:  # noqa: BLE001 — classify, never raise to the admin
        return {
            "ok": False,
            "detail": (
                f"apps-runner sidecar unreachable ({exc}) — start it with "
                "`docker compose --profile apps up -d apps-runner` and check "
                "APPS_RUNNER_URL / APPS_RUNNER_TOKEN"
            ),
        }
    return {"ok": bool(result.get("ok")), "detail": str(result.get("detail") or "")}


def _test_anthropic_sync(key: str, timeout: float) -> dict:
    try:
        import anthropic
    except ImportError:
        return {"ok": False, "detail": "anthropic SDK not installed"}
    # Reuse the cheap Haiku model the auto-title path already uses; a
    # 1-token completion is enough to authenticate the key.
    from app.chat.auto_title import _TITLE_MODEL

    try:
        client = anthropic.Anthropic(api_key=key, timeout=timeout)
        client.messages.create(
            model=_TITLE_MODEL,
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
    except Exception as exc:  # noqa: BLE001 — classify, never raise to the admin
        return {"ok": False, "detail": _classify(exc)}
    return {"ok": True, "detail": "Anthropic API key valid"}


async def test_anthropic_key(api_key: Optional[str] = None, *, timeout: float = 8.0) -> dict:
    """Probe the Anthropic API key with a 1-token Haiku completion.

    Returns ``{ok, detail}``. Runs the blocking SDK call on a worker thread
    so it never stalls the event loop. Falls back to the env key when
    ``api_key`` is omitted.
    """
    key = (api_key or os.environ.get(ENV_ANTHROPIC, "")).strip()
    if not key:
        return {"ok": False, "detail": "ANTHROPIC_API_KEY not set"}
    import asyncio

    return await asyncio.to_thread(_test_anthropic_sync, key, timeout)


def _test_wif_sync(timeout: float) -> dict:
    """Mint a federated token and confirm the API accepts it. Returns {ok, detail}."""
    from app.auth.wif import WIFAuthError, clear_token_cache, get_federated_access_token

    try:
        token = get_federated_access_token()
    except WIFAuthError as exc:
        return {"ok": False, "detail": f"federation token exchange failed: {exc}"}
    # The exchange succeeded; confirm the minted token is actually accepted by the
    # API with the same cheap 1-token Haiku completion test_anthropic_key uses —
    # authenticated the keyless way (Authorization: Bearer + the oauth beta header).
    try:
        import anthropic
    except ImportError:  # pragma: no cover — SDK is a hard dep for chat
        return {"ok": True, "detail": "federated token minted (anthropic SDK unavailable for full probe)"}
    from app.chat.auto_title import _TITLE_MODEL

    try:
        client = anthropic.Anthropic(
            auth_token=token,
            default_headers={"anthropic-beta": "oauth-2025-04-20"},
            timeout=timeout,
        )
        client.messages.create(
            model=_TITLE_MODEL,
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
    except Exception as exc:  # noqa: BLE001 — classify, never raise to the admin
        # The cached token may be scope-limited or revoked — drop it so a later
        # probe/request re-mints rather than reusing a known-bad token.
        clear_token_cache()
        return {"ok": False, "detail": f"federated token minted but API call failed: {_classify(exc)}"}
    return {"ok": True, "detail": "workload identity federation valid"}


async def test_wif_credentials(*, timeout: float = 8.0) -> dict:
    """Live-probe ``workload_identity`` auth: mint a federated token from the
    workload's OIDC identity and confirm the API accepts it. Returns
    ``{ok, detail}``. The blocking exchange + SDK call run on a worker thread so
    they never stall the event loop. This is the keyless-mode analog of
    ``test_anthropic_key`` for the admin "test connection" surface."""
    import asyncio

    return await asyncio.to_thread(_test_wif_sync, timeout)


def _test_vertex_sync(project_id: str, region: str, timeout: float) -> dict:
    """Resolve a Google token, then confirm Vertex serves Claude for this
    project/region with a 1-token completion. Returns {ok, detail}."""
    from app.auth.vertex_gcp import VertexAuthError, clear_token_cache, get_vertex_access_token

    if not project_id or not region:
        return {
            "ok": False,
            "detail": "vertex not configured — set chat.llm.vertex.project_id and chat.llm.vertex.region",
        }
    try:
        get_vertex_access_token()
    except VertexAuthError as exc:
        return {"ok": False, "detail": f"google credential resolution failed: {exc}"}
    try:
        import anthropic

        vertex_client_cls = anthropic.AnthropicVertex
    except (ImportError, AttributeError):  # pragma: no cover — SDK is a hard dep for chat
        return {"ok": True, "detail": "google token minted (anthropic SDK unavailable for full probe)"}
    from app.chat.auto_title import _TITLE_MODEL
    from connectors.llm.vertex_provider import to_vertex_model_id

    try:
        client = vertex_client_cls(project_id=project_id, region=region, timeout=timeout)
        client.messages.create(
            model=to_vertex_model_id(_TITLE_MODEL),
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
    except Exception as exc:  # noqa: BLE001 — classify, never raise to the admin
        # The cached token may be scope-limited or revoked — drop it so a later
        # probe/request re-resolves rather than reusing a known-bad token.
        clear_token_cache()
        return {
            "ok": False,
            "detail": (
                f"google token minted but Vertex call failed: {_classify(exc)} — "
                "check the Claude model is enabled in Model Garden for this "
                "project/region and the identity has roles/aiplatform.user"
            ),
        }
    return {"ok": True, "detail": "vertex credentials valid"}


async def test_vertex_credentials(project_id: str, region: str, *, timeout: float = 8.0) -> dict:
    """Live-probe ``chat.llm.provider: vertex``: resolve Google ADC and run a
    1-token completion through ``anthropic.AnthropicVertex``. Returns
    ``{ok, detail}``; blocking work runs on a worker thread. Takes explicit
    project/region so both the chat config and the server-side ``ai.vertex``
    block can drive it."""
    import asyncio

    return await asyncio.to_thread(_test_vertex_sync, project_id, region, timeout)
