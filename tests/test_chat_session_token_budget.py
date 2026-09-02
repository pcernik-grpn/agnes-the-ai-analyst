"""The per-conversation token budget (``chat.max_session_tokens``) after TCRD-291.

The knob sums the tokens BILLED over a conversation's whole life — input,
output and cache writes of every LLM call, across every turn. An agentic turn
re-sends the context on each tool call, so the sum grows by several hundred
thousand tokens per busy turn while the context the engine compacts stays far
below the model's limit. The old 200k default equalled one context window; it
only began tripping on engine sessions in 0.95.0 (broker-observed usage
started reaching the manager) and the refusal — "Per-session token cap of
200000 reached … Start a new chat session", presented by the web client as
something "the engine reported" — read as "compaction does not work".

These tests pin the recalibrated default, the ``0`` = disabled escape hatch,
the copy on every surface, and the removal of the operator signals that kept
calling the cap inert on the very provider it was refusing messages on.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from app.chat.config import ChatConfig, load_chat_config
from app.chat.manager import enforce_sender_limits, session_token_budget_message
from app.coordination.factory import reset_coordination_for_tests

CHAT_JS = Path("app/web/static/js/chat.js")
EXAMPLE_YAML = Path("config/instance.yaml.example")


@pytest.fixture(autouse=True)
def _reset_coordination():
    """The daily-token and message-rate counters live in the coordination
    singleton; reset so one test's "u@x" spend never bleeds into another's."""
    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


def _repo(*, session_tokens: int = 0, daily: tuple[int, int] = (0, 0)) -> MagicMock:
    """The two reads ``enforce_sender_limits`` makes on the repository."""
    repo = MagicMock()
    repo.session_total_tokens.return_value = session_tokens
    repo.daily_anthropic_tokens.return_value = daily
    return repo


async def _enforce(cfg: ChatConfig, repo: MagicMock, frames: list[dict] | None = None) -> list[dict]:
    """Run the gate with an ``on_limit`` that appends to ``frames``. Pass your
    own list when the gate is expected to raise — the frame is broadcast
    BEFORE the raise, and a returned value would never reach the caller."""
    frames = [] if frames is None else frames

    async def _capture(frame: dict) -> None:
        frames.append(frame)

    await enforce_sender_limits(repo, cfg, "u@x", "chat-1", on_limit=_capture)
    return frames


# --- default -----------------------------------------------------------------


def test_the_default_is_a_runaway_guard_not_a_context_window(tmp_path: Path):
    """2M — ten context windows of re-sent input — on the dataclass, the loader
    (no yaml, empty yaml) and the shipped example, so an operator who copies
    the example gets a value that does not trip a few turns into a real
    conversation the way 200k did."""
    assert ChatConfig().max_session_tokens == 2_000_000
    assert load_chat_config(tmp_path / "absent.yaml").max_session_tokens == 2_000_000
    empty = tmp_path / "instance.yaml"
    empty.write_text("instance_name: t\n")
    assert load_chat_config(empty).max_session_tokens == 2_000_000
    example = yaml.safe_load(EXAMPLE_YAML.read_text(encoding="utf-8"))
    assert example["chat"]["max_session_tokens"] == ChatConfig().max_session_tokens
    # The example explains what the number is — the misreading was the bug.
    assert "NOT its context window" in EXAMPLE_YAML.read_text(encoding="utf-8")


def test_zero_parses_as_zero(tmp_path: Path):
    """``0`` must survive the loader untouched (it is the disable value, not a
    blank that falls back to the default)."""
    y = tmp_path / "instance.yaml"
    y.write_text("chat:\n  max_session_tokens: 0\n  daily_anthropic_spend_usd: 0\n")
    cfg = load_chat_config(y)
    assert cfg.max_session_tokens == 0
    assert cfg.daily_anthropic_spend_usd == 0


# --- 0 disables --------------------------------------------------------------


def test_zero_disables_the_per_conversation_budget():
    """Before: ``session_tokens >= 0`` is always true, so ``0`` refused EVERY
    message — the opposite of what readiness already documented it to mean.
    Now it is the escape hatch, and the sum is not even computed."""
    cfg = ChatConfig(enabled=True, max_session_tokens=0, daily_anthropic_spend_usd=10**6)
    repo = _repo(session_tokens=10**9)
    frames = asyncio.run(_enforce(cfg, repo))
    assert frames == []
    repo.session_total_tokens.assert_not_called()


def test_zero_disables_the_daily_spend_cap():
    """Same footgun on the sibling knob: ``spent >= 0.0`` refused everything."""
    cfg = ChatConfig(enabled=True, max_session_tokens=10**9, daily_anthropic_spend_usd=0)
    repo = _repo(daily=(10**9, 10**9))
    assert asyncio.run(_enforce(cfg, repo)) == []
    # Disabled means not even read: no counter round trip, no DB re-seed.
    repo.daily_anthropic_tokens.assert_not_called()


def test_a_positive_budget_still_refuses_once_exhausted():
    """Non-vacuity for the two tests above: the gate itself still works, and
    the reason string every surface keys on is unchanged."""
    cfg = ChatConfig(enabled=True, max_session_tokens=100, daily_anthropic_spend_usd=10**6)
    with pytest.raises(RuntimeError, match="^max_session_tokens_exhausted$"):
        asyncio.run(_enforce(cfg, _repo(session_tokens=100)))


# --- copy --------------------------------------------------------------------


def test_the_refusal_frame_names_the_budget_and_the_next_step():
    """The frame the local sinks broadcast. What ran out is a budget of billed
    tokens, the next step is a new conversation, and the knob is named for
    whoever gets asked — nothing about a cap, the engine, or context."""
    cfg = ChatConfig(enabled=True, max_session_tokens=200_000, daily_anthropic_spend_usd=10**6)
    frames: list[dict] = []
    with pytest.raises(RuntimeError, match="max_session_tokens_exhausted"):
        asyncio.run(_enforce(cfg, _repo(session_tokens=438_679), frames))
    # The frame was broadcast before the raise — exactly one, with the
    # shipped copy (the numbers are the ones from the bug report).
    assert len(frames) == 1
    msg = frames[0]["message"]
    assert msg == session_token_budget_message(438_679, 200_000)
    assert "token budget" in msg
    assert "438,679" in msg and "200,000" in msg
    assert "new conversation" in msg
    assert "chat.max_session_tokens" in msg
    for banned in ("Per-session token cap", "engine", "context", "compact"):
        assert banned not in msg, banned


def test_the_frame_reaches_the_sink_with_the_new_copy():
    """The end-to-end shape of the broadcast: kind unchanged (the web client
    keys on it), message is the budget copy."""
    cfg = ChatConfig(enabled=True, max_session_tokens=100, daily_anthropic_spend_usd=10**6)
    frames: list[dict] = []

    async def _run() -> None:
        async def _capture(frame: dict) -> None:
            frames.append(frame)

        try:
            await enforce_sender_limits(_repo(session_tokens=150), cfg, "u@x", "chat-1", on_limit=_capture)
        except RuntimeError as exc:
            assert str(exc) == "max_session_tokens_exhausted"

    asyncio.run(_run())
    assert len(frames) == 1
    assert frames[0]["type"] == "error"
    assert frames[0]["kind"] == "max_session_tokens"
    assert frames[0]["message"] == session_token_budget_message(150, 100)


def test_the_slack_copy_says_budget():
    from services.slack_bot.events import _SENDER_LIMIT_MESSAGES

    text = _SENDER_LIMIT_MESSAGES["max_session_tokens_exhausted"]
    assert "token budget" in text
    assert "new thread" in text


def _node() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available — the client-copy test needs a runtime")
    return node


def _chat_error_copy_fn() -> str:
    chat = CHAT_JS.read_text(encoding="utf-8")
    decl = re.search(r"function chatErrorCopy\(raw, kind\) \{.*?\n\}\n", chat, re.DOTALL)
    assert decl, "chatErrorCopy moved — re-point this guard"
    return decl.group(0)


def test_the_web_client_never_attributes_agnes_sender_limits_to_the_engine():
    """Runs the shipped ``chatErrorCopy`` out of chat.js. The three sender
    limits are Agnes's own gate; before, only ``daily_budget`` matched a
    family (the agent-monthly one, with the wrong copy) and the other two fell
    through to "Agnes could not finish that answer. The engine reported: …",
    which is what the bug report quoted. The fallback itself must survive for
    a real engine error, and the agent-budget family must keep its copy."""
    node = _node()
    cases = [
        ["max_session_tokens", session_token_budget_message(438_679, 200_000)],
        ["daily_budget", "Daily spend cap of $20.00 reached. Try again tomorrow."],
        ["rate_limit", "Rate limit hit: 100 messages/hour. Slow down or wait an hour."],
        ["engine_error", "engine turn failed: boom"],
        ["budget_exhausted", "429"],
    ]
    script = (
        _chat_error_copy_fn()
        + "\n"
        + f"process.stdout.write(JSON.stringify({json.dumps(cases)}.map(([k, m]) => chatErrorCopy(m, k))));\n"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, f"node failed:\n{out.stderr}"
    session, daily, rate, engine, agent = json.loads(out.stdout)
    for copy in (session, daily, rate):
        assert "engine reported" not in copy, copy
        assert "engine" not in copy.lower(), copy
    assert "token budget" in session and "new conversation" in session
    assert "compact" not in session.lower() and "context" not in session.lower()
    assert "daily spend cap" in daily and "month" not in daily
    assert "faster" in rate
    assert engine == "Agnes could not finish that answer. The engine reported: engine turn failed: boom"
    assert "month" in agent


# --- operator signals --------------------------------------------------------


def test_no_surface_still_calls_the_caps_inert_on_the_engine_provider():
    """The boot warning, readiness and the provider comment all said the two
    token-derived caps are NOT enforced under kai-agent. They are (0.95.0,
    app/chat/turn_usage.py) — TCRD-291 was one of them firing. A stale "not
    enforced" is what sends the operator to debug the engine instead of the
    knob, so every copy of the claim goes at once."""
    main = Path("app/main.py").read_text(encoding="utf-8")
    assert "configured but NOT enforced" not in main
    readiness = Path("app/chat/readiness.py").read_text(encoding="utf-8")
    assert 'provider == "kai-agent" and live' not in readiness
    provider = Path("app/chat/kai_engine_provider.py").read_text(encoding="utf-8")
    assert "caps unmetered" not in provider
    doc = Path("docs/cloud-chat.md").read_text(encoding="utf-8")
    assert "is a budget, not a context window" in doc


# --- the socket survives a refusal -------------------------------------------


def _receive_until_error(ws, limit: int = 30) -> dict:
    """Drain lifecycle frames (``ready``, ``runner_ready``, replay markers…)
    until the refusal ``error`` frame; a closed socket raises out as
    ``WebSocketDisconnect``, which is the failure this test exists to catch."""
    for _ in range(limit):
        frame = ws.receive_json()
        if frame.get("type") == "error":
            return frame
    raise AssertionError("no error frame within the first frames")


def test_a_refused_send_keeps_the_websocket_open():
    """The manager broadcasts the refusal frame to the socket and THEN raises
    ``RuntimeError("max_session_tokens_exhausted")`` out of ``send_user_message``.
    The WebSocket reader loop caught only ``SessionNotFound``, so the raise
    unwound the handler and closed the socket right behind the frame: the
    reader saw the refusal for an instant, then "Disconnected — click the
    conversation again to resume" (the bug report's screenshot). The frame is
    on the socket already; the socket must stay open — so a second send is
    refused the same way on the SAME connection, and a rate-limited sender can
    simply try again."""
    import dataclasses

    from fastapi.testclient import TestClient

    from tests.test_chat_api import _make_app_with_fake_provider

    app = _make_app_with_fake_provider()
    client = TestClient(app)
    mgr = app.state.chat_manager
    mgr._config = dataclasses.replace(mgr._config, max_session_tokens=10, daily_anthropic_spend_usd=10**6)

    chat_id = client.post("/api/chat/sessions", json={"surface": "web"}).json()["id"]
    app.state.chat_repo.append_message(
        session_id=chat_id, role="assistant", content="earlier answer", tokens_in=40, tokens_out=10, model="fake"
    )
    ws_url = client.post(f"/api/chat/sessions/{chat_id}/ticket").json()["ws_url"]
    with client.websocket_connect(ws_url) as ws:
        first = ws.receive_json()
        assert first["type"] == "ready"
        ws.send_json({"type": "user_msg", "text": "one more"})
        refusal = _receive_until_error(ws)
        assert refusal["kind"] == "max_session_tokens"
        assert refusal["message"] == session_token_budget_message(50, 10)
        # Still connected: the same socket takes — and refuses — another send.
        ws.send_json({"type": "user_msg", "text": "and again"})
        again = _receive_until_error(ws)
        assert again["kind"] == "max_session_tokens"
    # Nothing was persisted for the refused sends.
    roles = [m.role for m in app.state.chat_repo.list_messages(chat_id)]
    assert roles == ["assistant"]


# --- who sees the refusal ----------------------------------------------------


def test_the_refusal_reaches_only_the_senders_own_sinks(tmp_path: Path):
    """Co-drive: the owner and a guest each hold a socket on one live session.
    The refusal used to be broadcast, so the guest read "you've reached your
    daily spend cap" for a message they never sent (review finding on
    #2050). It now goes to the sinks attributed to the sender — and is not
    stamped or appended to the replay stream, so a reconnecting guest does
    not replay it either."""
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock

    import duckdb

    from app.chat.manager import ChatManager, LiveSession, SinkEntry
    from app.chat.persistence import ChatRepository
    from app.chat.types import SessionState
    from app.chat.workdir import WorkdirManager
    from src.db import _ensure_schema

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    cfg = ChatConfig(enabled=True, max_session_tokens=10, daily_anthropic_spend_usd=10**6)
    mgr = ChatManager(provider=MagicMock(), workdir_mgr=MagicMock(spec=WorkdirManager), repo=repo, config=cfg)
    owner_ws, guest_ws = MagicMock(), MagicMock()
    owner_ws.send_json, guest_ws.send_json = AsyncMock(), AsyncMock()
    live = LiveSession(
        chat_id="chat-1",
        user_email="owner@x",
        state=SessionState.ACTIVE,
        handle=MagicMock(),
        started_at=datetime.now(timezone.utc),
        last_activity=datetime.now(timezone.utc),
        sinks=[
            SinkEntry(participant_email="owner@x", sink=owner_ws),
            SinkEntry(participant_email="guest@x", sink=guest_ws),
        ],
    )
    from app.chat.types import Surface

    repo.create_session(user_email="owner@x", surface=Surface.WEB, session_id="chat-1")
    repo.append_message(session_id="chat-1", role="assistant", content="x", tokens_in=50, tokens_out=0, model="fake")
    with pytest.raises(RuntimeError, match="max_session_tokens_exhausted"):
        asyncio.run(mgr._enforce_sender_limits("guest@x", "chat-1", live))
    owner_ws.send_json.assert_not_awaited()
    guest_ws.send_json.assert_awaited_once()
    frame = guest_ws.send_json.await_args.args[0]
    assert frame["kind"] == "max_session_tokens"
    assert "seq" not in frame  # not a turn frame — never stamped, never replayed


def test_an_explicit_context_window_sized_budget_warns_at_load(tmp_path: Path, caplog):
    """Deployments that copied ``max_session_tokens: 200000`` from the old
    example keep refusing long conversations — the default change cannot
    reach a key that is set. The loader says so, once, naming the fix."""
    import logging

    y = tmp_path / "instance.yaml"
    y.write_text("chat:\n  max_session_tokens: 200000\n")
    with caplog.at_level(logging.WARNING, logger="app.chat.config"):
        cfg = load_chat_config(y)
    assert cfg.max_session_tokens == 200_000  # honoured, not silently raised
    warnings = [r.getMessage() for r in caplog.records if "max_session_tokens=200000" in r.getMessage()]
    assert len(warnings) == 1 and "set 0 to disable" in warnings[0]
    caplog.clear()
    y.write_text("chat:\n  max_session_tokens: 0\n")
    with caplog.at_level(logging.WARNING, logger="app.chat.config"):
        load_chat_config(y)
    assert not [r for r in caplog.records if "max_session_tokens=" in r.getMessage()], "0 (disabled) is not low"
