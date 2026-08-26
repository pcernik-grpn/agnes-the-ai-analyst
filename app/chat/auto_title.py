"""Auto-title generation for chat sessions.

After the first assistant turn lands, the manager calls
:func:`generate_title` with the first user message. We ask Haiku 4.5 for
a 2–5 word title and write it back to ``chat_sessions.title``. The
result is broadcast as a ``session_renamed`` frame so the sidebar +
thread header update live.

Design notes
------------
- **Best-effort, never blocking.** Any failure (no key, network error,
  rate limit, refusal, weird response) returns ``None``. The session
  keeps its ``Untitled chat`` fallback — chats never break because
  Haiku is down.
- **Sync SDK in a thread.** The Anthropic SDK call is synchronous; we
  run it via ``asyncio.to_thread`` from the manager so the WS pump
  isn't blocked while Haiku is thinking. Mirrors the existing pattern
  in ``connectors/llm/anthropic_provider.py``.
- **Tight cap on input + output.** First user message is clipped to
  the first ~600 chars (enough signal for a title, keeps token cost
  per call <500 input + ~16 output). Result is stripped, quote-trimmed,
  and capped at 60 chars before being persisted.
- **Best-effort does not mean silent.** "No credential obtainable" used to
  log at debug and vanish — a session just stayed ``Untitled chat`` forever
  with no operator-visible signal (#1526). It now logs a WARNING (once per
  process for "nothing configured"; every time for "configured but minting
  failed", since that's an ongoing operational problem rather than expected
  state) without changing the best-effort contract: the turn still never
  fails because of this.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Haiku is the right tier for this — fast, cheap, and the task is
# trivial. The model id matches the one used by other Haiku call-sites
# in the codebase (Corporate Memory batch extraction).
_TITLE_MODEL = "claude-haiku-4-5-20251001"
_TITLE_MAX_TOKENS = 24
_MESSAGE_CLIP_CHARS = 600
_TITLE_MAX_CHARS = 60

# A reply that ANSWERS the first message instead of titling it. Haiku does this
# occasionally when the message reads as a direct question ("What is in my
# stack?") — and an answer-shaped title is worse than no title, because the
# sidebar then reports an outcome the conversation never had. Seen in
# production: a chat that read the user's stack and removed a plugin from it
# was listed as "I don't have access to information about your stack or the…".
# Both guards run on the normalized text, before the length cap, so a truncated
# answer can't slip through looking like a terse title.
_ANSWER_SHAPED = re.compile(
    r"^(i|i'm|im|sorry|sure|certainly|of course|here|here's|hi|hello|"
    r"as an|unfortunately|based on|it looks|there (is|are))\b",
    re.IGNORECASE,
)
# The prompt asks for 2–6 words; this leaves headroom for a wordy-but-valid
# title while still rejecting a sentence.
_TITLE_MAX_WORDS = 10

_SYSTEM_PROMPT = (
    "You produce a concise title (2–6 words, sentence case, no trailing "
    "punctuation, no quotes) summarizing the topic of a chat conversation "
    "given its first user message. Reply with the title only — no preamble, "
    "no explanation."
)

# WIF env vars a token exchange needs (mirrors app/auth/wif.py::_exchange
# and the boot-time check in app/main.py::_chat_anthropic_key_ok). Checked
# here only to classify a mint failure for logging purposes — never to gate
# the actual exchange, which stays entirely inside app.auth.wif.
_WIF_REQUIRED_ENV_VARS = (
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_SERVICE_ACCOUNT_ID",
)

# Warned about a missing Anthropic credential this process already? Auto-title
# runs on every session's first assistant turn, so without a once-per-process
# guard a keyless/misconfigured instance (local dev, TESTING=1, or WIF simply
# not rolled out yet) would log one WARNING per conversation forever. Mirrors
# the once-per-process pattern in app/instance_config.py::_warn_once. A
# credential that IS configured but fails to *mint* (see
# `_wif_appears_configured` below) is a different, ongoing condition and is
# deliberately NOT rate-limited — surfaced every time, like the
# `logger.exception` in `_generate_title_sync` for a rejected key.
_no_credential_warned = False


def _wif_appears_configured() -> bool:
    """Best-effort read of whether an operator set up WIF at all.

    Used only to pick a log message/severity when a mint attempt fails —
    never to gate the exchange itself (``app.auth.wif`` is the sole source
    of truth there). "Appears" because this only checks env presence, not
    validity; an invalid value still reaches this function as an exception,
    just correctly classified as "configured but failing" rather than "not
    configured".
    """
    if not all(os.environ.get(var, "").strip() for var in _WIF_REQUIRED_ENV_VARS):
        return False
    return bool(
        os.environ.get("ANTHROPIC_IDENTITY_TOKEN", "").strip()
        or os.environ.get("ANTHROPIC_IDENTITY_TOKEN_FILE", "").strip()
    )


def _warn_no_credential(exc: Exception) -> None:
    """Log "no Anthropic credential at all" once per process at WARNING;
    every later occurrence in this process drops to DEBUG (see the
    module-level docstring on `_no_credential_warned` for why)."""
    global _no_credential_warned
    if _no_credential_warned:
        logger.debug("no Anthropic credential (static or WIF) for auto-title: %s", exc)
        return
    _no_credential_warned = True
    logger.warning(
        "auto-title disabled: no Anthropic credential available (set "
        "ANTHROPIC_API_KEY, or the workload_identity vars "
        "ANTHROPIC_FEDERATION_RULE_ID / ANTHROPIC_ORGANIZATION_ID / "
        "ANTHROPIC_SERVICE_ACCOUNT_ID plus an identity token) — sessions will "
        "keep the 'Untitled chat' default until one is configured. Logged "
        "once per process; cause: %s",
        exc,
    )


def _strip_title(raw: str) -> Optional[str]:
    """Normalize Haiku's reply into a stored title.

    Trims whitespace, strips wrapping quotes / brackets, drops trailing
    punctuation, and caps length. Returns ``None`` for empty or
    pathological replies so the caller can keep the default.
    """
    if not raw:
        return None
    text = raw.strip()
    # Drop wrapping quotes/brackets the model sometimes adds despite the
    # system prompt asking it not to.
    for opener, closer in (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"), ("[", "]"), ("(", ")")):
        if text.startswith(opener) and text.endswith(closer) and len(text) >= 2:
            text = text[1:-1].strip()
    # Trailing punctuation looks awkward in a sidebar item.
    text = text.rstrip(".!?,:;")
    text = text.strip()
    if not text:
        return None
    # Collapse internal whitespace runs (newlines, tabs) to single spaces.
    text = " ".join(text.split())
    # Haiku answered the message instead of titling it — keep the honest
    # default rather than labelling the chat with a reply it never gave.
    if _ANSWER_SHAPED.match(text) or len(text.split()) > _TITLE_MAX_WORDS:
        logger.info("auto-title: discarding answer-shaped reply %r", text[:80])
        return None
    if len(text) > _TITLE_MAX_CHARS:
        text = text[: _TITLE_MAX_CHARS - 1].rstrip() + "…"
    return text


def _generate_title_sync(
    user_message: str, *, api_key: Optional[str] = None, auth_token: Optional[str] = None
) -> Optional[str]:
    """Synchronous Haiku call. Returns the trimmed title or ``None``.

    Lives in its own function so :func:`generate_title` can dispatch it
    onto a worker thread without dragging anthropic SDK init into the
    event loop on every call.

    Authenticates with a static ``api_key`` (``x-api-key``) or, in keyless
    (workload_identity) mode, a short-lived federated ``auth_token``
    (``Authorization: Bearer`` + the oauth beta header OAuth-style tokens need).
    """
    try:
        import anthropic  # local import keeps test envs without the SDK clean
    except ImportError:  # pragma: no cover - SDK is a hard dep for chat
        logger.debug("anthropic SDK missing; skipping auto-title")
        return None
    try:
        if auth_token:
            client = anthropic.Anthropic(
                auth_token=auth_token,
                default_headers={"anthropic-beta": "oauth-2025-04-20"},
                timeout=8.0,
            )
        else:
            client = anthropic.Anthropic(api_key=api_key, timeout=8.0)
        resp = client.messages.create(
            model=_TITLE_MODEL,
            max_tokens=_TITLE_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message[:_MESSAGE_CLIP_CHARS]}],
        )
    except Exception:
        logger.exception("auto-title Haiku call failed; keeping default title")
        return None
    parts: list[str] = []
    for block in getattr(resp, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return _strip_title("".join(parts))


async def generate_title(user_message: str, *, llm_auth: str = "api_key") -> Optional[str]:
    """Ask Haiku for a short title for a conversation. Best-effort.

    ``llm_auth`` mirrors the broker's decision (``chat_config.llm_auth``,
    ``app/api/broker.py``) rather than only checking for a static key's
    presence — otherwise a stale ``ANTHROPIC_API_KEY`` left set in
    ``workload_identity`` mode would silently authenticate auto-title with
    the wrong credential while the broker correctly uses WIF.

    Returns the cleaned title string, or ``None`` if the API key is
    missing, the SDK isn't installed, the call fails, or the reply is
    empty/garbage. The caller MUST treat ``None`` as "leave the title
    alone" — never as an error.
    """
    if not user_message or not user_message.strip():
        return None
    import asyncio

    if llm_auth != "workload_identity":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if api_key:
            return await asyncio.to_thread(_generate_title_sync, user_message, api_key=api_key)
    # Keyless (workload_identity), or no static key configured: mint a
    # short-lived federated token the same way the broker does. If neither a
    # static key nor a valid WIF configuration is present,
    # get_federated_access_token raises and we skip.
    try:
        from app.auth.wif import get_federated_access_token

        token = await asyncio.to_thread(get_federated_access_token)
    except Exception as exc:  # noqa: BLE001 — best-effort; missing/failed creds => skip
        if _wif_appears_configured():
            # Configured but minting failed (expired rule, revoked service
            # account, network hiccup...) — an ongoing operational problem,
            # not the expected keyless-instance state, so every occurrence
            # is surfaced (see #1526).
            logger.warning("auto-title disabled: Anthropic WIF token mint failed: %s", exc)
        else:
            _warn_no_credential(exc)
        return None
    return await asyncio.to_thread(_generate_title_sync, user_message, auth_token=token)
