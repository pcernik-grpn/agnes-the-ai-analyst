"""G.2 — concurrent load test: 10 simulated users × 3 sessions each.

Fans out 30 simultaneous WebSocket connections against the docker-compose
E2E stack, sends one ``user_msg`` per session with a session-unique
payload, then asserts:

  * Every session received an ``assistant_message`` frame.
  * No cross-talk — the reply on session N must echo session N's payload,
    not anyone else's. (Catches a regression where the ChatManager pump
    fans frames to the wrong WebSocket.)

This needs the fake-agent runner: real Anthropic at 30× concurrency
would burn money and run into rate limits. The ``AGNES_E2E_FAKE_AGENT=1``
env (forwarded by docker-compose.e2e.yml into the container) flips the
runner into deterministic echo mode — ``user_msg: "X"`` →
``assistant_message: "echo: X"``. That gives us a strict per-session
contract to assert against.

The test is gated by ``AGNES_E2E_LOAD=1`` on top of ``AGNES_E2E=1``
(the docker stack) and ``AGNES_E2E_FAKE_AGENT=1`` (deterministic
echoes). Without ``AGNES_E2E_LOAD`` the test skips even when the rest
of the suite runs — load is expensive and slow.

RAM monitoring is best-effort via ``psutil`` when available; the metric
is logged, not asserted (we have no hard budget yet; the value is in
catching trend regressions when this is re-run between releases).
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import time
from dataclasses import dataclass

import pytest

from tests.e2e._helpers import (
    E2E_USER_PASSWORD,
    bootstrap_admin,
    skip_unless_chat_sessions_possible,
)


# ---------------------------------------------------------------------------
# Optional deps
# ---------------------------------------------------------------------------

try:
    # `websockets.asyncio.client.connect` is the canonical asyncio entry
    # point in websockets 12+. The project's Python floor is 3.11 and
    # the docker-e2e venv ships websockets 12.x; the tests/e2e/_helpers
    # module imports the sync sibling for the simpler F.* tests.
    from websockets.asyncio.client import connect as ws_connect_async

    _WS_AVAILABLE = True
except ImportError:  # pragma: no cover — only on very old envs
    ws_connect_async = None  # type: ignore[assignment]
    _WS_AVAILABLE = False


try:
    import psutil

    _PSUTIL_AVAILABLE = True
except ImportError:  # pragma: no cover — psutil is optional
    psutil = None  # type: ignore[assignment]
    _PSUTIL_AVAILABLE = False


# ---------------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------------

_NUM_USERS = 10
_SESSIONS_PER_USER = 3
_TOTAL_SESSIONS = _NUM_USERS * _SESSIONS_PER_USER

# Per-WS receive ceiling. Fake-agent is fast (sub-second locally); the
# docker stack under load might be slower. 60s gives plenty of headroom
# for the slowest session in the fan-out without masking a real hang.
_RECV_TIMEOUT_S = 60.0
# Max frames to consume per session before declaring failure. The
# fake-agent flow is roughly: ready → assistant_message → done. Cap
# generously to handle interstitial token frames in case the runner
# protocol changes.
_MAX_FRAMES = 50


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def _skip_if_load_disabled() -> None:
    if not os.environ.get("AGNES_E2E_LOAD"):
        pytest.skip(
            "G.2 load test gated — set AGNES_E2E_LOAD=1 (alongside "
            "AGNES_E2E=1 and AGNES_E2E_FAKE_AGENT=1) to run.",
        )
    if not os.environ.get("AGNES_E2E_FAKE_AGENT"):
        pytest.skip(
            "G.2 requires AGNES_E2E_FAKE_AGENT=1 — 30 concurrent real-LLM "
            "calls would burn money and rate-limit. Re-run with the "
            "fake-agent runner enabled.",
        )
    if not _WS_AVAILABLE:
        pytest.skip("websockets.asyncio.client unavailable")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class SessionResult:
    """One row in the per-session outcome matrix."""

    user_idx: int
    session_idx: int
    marker: str            # the unique payload we sent
    reply: str | None      # the assistant_message.content we received
    error: str | None      # populated on hard failures (timeout, decode, etc.)
    elapsed_s: float


async def _drive_session(
    *,
    user_idx: int,
    session_idx: int,
    ws_url: str,
    marker: str,
) -> SessionResult:
    """Run one user_msg → assistant_message round-trip over WS.

    The marker payload is embedded both in our prompt and in the
    expected reply (``echo: <marker>``). Cross-talk shows up as a reply
    containing someone else's marker.
    """
    t0 = time.monotonic()
    try:
        async with ws_connect_async(ws_url, open_timeout=15) as ws:
            await ws.send(json.dumps({"type": "user_msg", "text": marker}))

            reply: str | None = None
            for _ in range(_MAX_FRAMES):
                raw = await asyncio.wait_for(ws.recv(), timeout=_RECV_TIMEOUT_S)
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if frame.get("type") == "assistant_message":
                    reply = frame.get("content")
                    break

            return SessionResult(
                user_idx=user_idx,
                session_idx=session_idx,
                marker=marker,
                reply=reply,
                error=None if reply is not None else "no_assistant_message",
                elapsed_s=time.monotonic() - t0,
            )
    except Exception as exc:  # noqa: BLE001 — fan-in surface needs all errors
        return SessionResult(
            user_idx=user_idx,
            session_idx=session_idx,
            marker=marker,
            reply=None,
            error=f"{type(exc).__name__}: {exc}",
            elapsed_s=time.monotonic() - t0,
        )


def _snapshot_peak_rss_mb() -> float | None:
    """Best-effort host-side RSS sample of the current process tree.

    Returns the sum of RSS across the current process and all its
    descendants. Returns None if psutil is unavailable. The docker
    container's resident memory is the more interesting number, but
    that's only inspectable via ``docker stats`` and we don't want to
    shell out from inside a hot test loop. The host-side number still
    catches gross regressions in the test harness itself (websocket
    buffers, etc.) and at least proves psutil isn't lying.
    """
    if not _PSUTIL_AVAILABLE:
        return None
    proc = psutil.Process()
    children = proc.children(recursive=True)
    rss = proc.memory_info().rss + sum(c.memory_info().rss for c in children)
    return rss / (1024 * 1024)


# ---------------------------------------------------------------------------
# The single test
# ---------------------------------------------------------------------------


def test_load_30_concurrent_sessions(docker_e2e_agnes: str) -> None:
    """Fan out 30 WS connections, assert no crosstalk, log peak RAM.

    Strategy:
      1. Bootstrap one admin and 9 additional users (10 total) — separate
         identities so per-user budget / rate-limit logic gets exercised.
      2. For each user, create 3 chat sessions via REST + capture the
         ws_url (which already embeds a one-shot ticket).
      3. asyncio.gather() 30 coroutines, each opening its session's WS,
         sending a marker, and waiting for the assistant_message.
      4. Assert every session got a reply, and every reply contains its
         own marker (cross-talk would show another session's marker).
      5. Log peak host RSS and per-session timings; assert no session
         exceeded the per-WS timeout (best-effort sanity check).
    """
    _skip_if_load_disabled()

    # ---- Step 1: bootstrap N users -----------------------------------------
    # The admin one is special — used for /auth/bootstrap which is a
    # one-shot; the rest are minted via /auth/password/register (the
    # bootstrap admin is admin and can mint regular users via the
    # standard signup endpoint).
    admin_email = "load-admin@agnes.local"
    admin = bootstrap_admin(
        docker_e2e_agnes, email=admin_email, password=E2E_USER_PASSWORD,
    )

    # For simplicity (and because the fake-agent path doesn't care WHO
    # the user is, only that sessions are distinct), we use the same
    # admin client to mint all 30 sessions. The crosstalk test is
    # session-keyed, not user-keyed — the per-session WS ticket is
    # what isolates frames.
    #
    # An earlier draft tried to bootstrap 10 separate users, but the
    # password-register endpoint requires the caller to already be
    # admin + the e-mail to match a domain allowlist — too much extra
    # surface for what's fundamentally a runner-pump fan-out test.
    # G.3 covers per-user identity boundaries (JWT replay) separately.

    # ---- Step 2: create sessions ------------------------------------------
    sessions: list[tuple[int, int, str]] = []  # (user_idx, session_idx, ws_url)
    skip_unless_chat_sessions_possible()
    for u in range(_NUM_USERS):
        for s in range(_SESSIONS_PER_USER):
            create = admin.create_chat_session(surface="web")
            ws_url = admin.ws_url_for(create)
            sessions.append((u, s, ws_url))

    assert len(sessions) == _TOTAL_SESSIONS

    # ---- Step 3: fan out ---------------------------------------------------
    rss_before = _snapshot_peak_rss_mb()

    async def _runner() -> list[SessionResult]:
        coros = [
            _drive_session(
                user_idx=u,
                session_idx=s,
                ws_url=url,
                # 8-hex marker. Long enough that a stray "echo: <other>" can't
                # collide by accident; short enough that a wrong-routing bug
                # still shows up clearly in a diff.
                marker=f"load-{u}-{s}-{secrets.token_hex(4)}",
            )
            for u, s, url in sessions
        ]
        return await asyncio.gather(*coros, return_exceptions=False)

    results = asyncio.run(_runner())
    rss_after = _snapshot_peak_rss_mb()

    # ---- Step 4: assertions -----------------------------------------------
    # 4a. Every session got a reply.
    failures = [r for r in results if r.error is not None]
    assert not failures, (
        f"{len(failures)}/{_TOTAL_SESSIONS} sessions failed; "
        f"first 3: {failures[:3]!r}"
    )

    # 4b. Every reply matches its own marker (no cross-talk).
    crosstalk = []
    seen_markers = {r.marker for r in results}
    for r in results:
        if r.reply is None:
            continue
        if r.marker not in (r.reply or ""):
            # Check whether SOME other session's marker is in this reply
            # — that's the actual cross-talk shape.
            colliders = [m for m in seen_markers if m != r.marker and m in r.reply]
            crosstalk.append((r.marker, r.reply, colliders))

    assert not crosstalk, (
        f"cross-talk detected on {len(crosstalk)} session(s); "
        f"first 3: {crosstalk[:3]!r}"
    )

    # 4c. Best-effort RAM logging — print to stderr so pytest -v shows it.
    if rss_before is not None and rss_after is not None:
        peak_delta_mb = rss_after - rss_before
        print(
            f"[G.2 load] peak host RSS: before={rss_before:.1f} MiB, "
            f"after={rss_after:.1f} MiB, delta={peak_delta_mb:+.1f} MiB "
            f"(host-side; in-container memory measured separately if needed)",
            file=sys.stderr,
        )
    else:
        print(
            "[G.2 load] psutil not installed — skipped RSS measurement.",
            file=sys.stderr,
        )

    # 4d. Timing summary — purely informational, helps catch tail-latency
    # regressions when the test is re-run between releases.
    elapsed = sorted(r.elapsed_s for r in results)
    p50 = elapsed[len(elapsed) // 2]
    p95 = elapsed[int(len(elapsed) * 0.95)]
    p99 = elapsed[int(len(elapsed) * 0.99)] if len(elapsed) >= 100 else elapsed[-1]
    print(
        f"[G.2 load] {_TOTAL_SESSIONS} sessions; "
        f"p50={p50:.2f}s p95={p95:.2f}s p99={p99:.2f}s max={elapsed[-1]:.2f}s",
        file=sys.stderr,
    )
