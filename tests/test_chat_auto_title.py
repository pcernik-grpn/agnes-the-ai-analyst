"""Tests for the auto-title path.

Covers three layers:
- The pure ``_strip_title`` helper (no SDK)
- The repo's ``set_title`` / ``get_first_user_message`` round-trip
- The manager hook: after an ``assistant_message`` frame, a fake
  ``generate_title`` is called, the title is persisted, and a
  ``session_renamed`` WS frame is sent.

We never hit the real Anthropic API — :func:`generate_title` is
monkey-patched. That keeps tests fast, hermetic, and CI-safe.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import duckdb
import pytest

from app.chat import auto_title
from app.chat.config import ChatConfig
from app.chat.manager import ChatManager
from app.chat.persistence import ChatRepository
from app.chat.types import Surface
from app.chat.workdir import WorkdirManager
from src.db import _ensure_schema


# --- _strip_title ------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Sales analysis", "Sales analysis"),
        ("  Sales analysis  ", "Sales analysis"),
        ('"Sales analysis"', "Sales analysis"),
        ("'Sales analysis'", "Sales analysis"),
        ("“Sales analysis”", "Sales analysis"),
        ("Sales analysis.", "Sales analysis"),
        ("Sales\nanalysis", "Sales analysis"),
        ("", None),
        ("   ", None),
        ('""', None),
    ],
)
def test_strip_title_normalizes(raw, expected):
    assert auto_title._strip_title(raw) == expected


def test_strip_title_truncates_long_titles():
    raw = "A" * 200
    out = auto_title._strip_title(raw)
    assert out is not None
    assert len(out) <= auto_title._TITLE_MAX_CHARS
    assert out.endswith("…")


def test_strip_title_rejects_answer_shaped_replies():
    """Haiku sometimes ANSWERS the first message instead of titling it.

    Observed in production: a chat whose first message was "What is currently
    in my stack? Then remove the sl-toolkit plugin from it" — a turn that went
    on to read the stack and perform the removal — was titled "I don't have
    access to information about your stack or the…". The sidebar then reports
    a refusal the conversation never made. No title beats a wrong one.
    """
    answers = [
        "I don't have access to information about your stack or the plugins in it",
        "I'm sorry, I can't help with that",
        "Sure! Here's a summary of your revenue trends for the quarter",
        "Based on the data you shared, the answer is 42 across all regions",
        "Unfortunately that table is not registered on this instance",
    ]
    for raw in answers:
        assert auto_title._strip_title(raw) is None, raw


def test_strip_title_keeps_real_titles():
    """The guard must not eat legitimate titles — including ones that merely
    start with a letter or word the answer-detector cares about."""
    titles = {
        "MRR metric definition": "MRR metric definition",
        "Invoice reconciliation": "Invoice reconciliation",
        "Czech greeting response": "Czech greeting response",
        "Q4 revenue by product line": "Q4 revenue by product line",
        "Iceberg table migration plan": "Iceberg table migration plan",
    }
    for raw, expected in titles.items():
        assert auto_title._strip_title(raw) == expected


# --- generate_title (top-level coordinator) ---------------------------------


def test_generate_title_returns_none_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert asyncio.run(auto_title.generate_title("Show me revenue last week")) is None


def test_generate_title_returns_none_for_empty_input(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert asyncio.run(auto_title.generate_title("")) is None
    assert asyncio.run(auto_title.generate_title("   ")) is None


def test_generate_title_dispatches_to_thread(monkeypatch):
    """A successful Haiku response is normalized + returned."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    captured = {}

    def fake_sync(user_message, *, api_key):
        captured["user_message"] = user_message
        captured["api_key"] = api_key
        return "Weekly revenue"

    monkeypatch.setattr(auto_title, "_generate_title_sync", fake_sync)
    out = asyncio.run(auto_title.generate_title("Show me revenue last week"))
    assert out == "Weekly revenue"
    assert captured["api_key"] == "test-key"
    assert captured["user_message"] == "Show me revenue last week"


def test_generate_title_ignores_stale_static_key_in_workload_identity_mode(monkeypatch):
    """A leftover ANTHROPIC_API_KEY must not win over llm_auth="workload_identity" —
    otherwise auto-title silently uses a different credential than the broker
    (which decides purely off chat_config.llm_auth), even though the static
    key would still authenticate. Mirrors the broker's config-driven check."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stale-static-key")
    captured = {}

    def fake_sync(user_message, **kwargs):
        captured.update(kwargs)
        return "WIF title"

    def fake_get_token():
        return "federated-token"

    monkeypatch.setattr(auto_title, "_generate_title_sync", fake_sync)
    monkeypatch.setattr("app.auth.wif.get_federated_access_token", fake_get_token)

    out = asyncio.run(auto_title.generate_title("Show me revenue last week", llm_auth="workload_identity"))
    assert out == "WIF title"
    assert captured == {"auth_token": "federated-token"}


def test_generate_title_swallows_sync_exceptions(monkeypatch):
    """If the sync helper raises, generate_title returns None."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def boom(*a, **kw):
        raise RuntimeError("network fell over")

    monkeypatch.setattr(auto_title, "_generate_title_sync", boom)
    # asyncio.to_thread propagates the exception — generate_title's
    # *job* is to let the manager's outer try/except in
    # _run_auto_title catch it. We assert that propagation here.
    with pytest.raises(RuntimeError):
        asyncio.run(auto_title.generate_title("hi"))


# --- #1526: missing-credential visibility ------------------------------------


class TestNoCredentialWarning:
    """When no Anthropic credential can be obtained, `generate_title` must
    still return ``None`` cleanly (never raise — a missing title is
    cosmetic) but say so at WARNING once per process, instead of the old
    `logger.debug` that left a keyless/misconfigured instance silently
    stuck on 'Untitled chat' forever (#1526)."""

    @pytest.fixture(autouse=True)
    def _reset_warn_once(self):
        """`_no_credential_warned` is a module-global once-per-process
        guard — reset it before/after every test in this class so test
        order can't suppress the warning a later test asserts on."""
        auto_title._no_credential_warned = False
        yield
        auto_title._no_credential_warned = False

    @staticmethod
    def _clear_credential_env(monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        for var in auto_title._WIF_REQUIRED_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv("ANTHROPIC_IDENTITY_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_IDENTITY_TOKEN_FILE", raising=False)

    def test_warns_once_and_leaves_title_none(self, monkeypatch, caplog):
        """(a) No credential at all: exactly one WARNING naming the cause
        and the fix, and the turn's return value stays ``None`` (the
        caller leaves the session 'Untitled chat', it does not raise)."""
        import logging

        self._clear_credential_env(monkeypatch)
        with caplog.at_level(logging.WARNING, logger="app.chat.auto_title"):
            title = asyncio.run(auto_title.generate_title("Show me revenue last week"))
        assert title is None
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, f"expected exactly one WARNING, got {[r.getMessage() for r in warnings]}"
        msg = warnings[0].getMessage()
        assert "auto-title" in msg.lower()
        assert "ANTHROPIC_API_KEY" in msg

    def test_no_warning_when_credential_present(self, monkeypatch, caplog):
        """(b) A working credential must not regress: title is produced
        as before and no credential warning fires."""
        import logging

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(auto_title, "_generate_title_sync", lambda *a, **kw: "Weekly revenue")
        with caplog.at_level(logging.WARNING, logger="app.chat.auto_title"):
            title = asyncio.run(auto_title.generate_title("Show me revenue last week"))
        assert title == "Weekly revenue"
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_warning_does_not_repeat_across_calls(self, monkeypatch, caplog):
        """(c) A keyless/misconfigured instance must not get one WARNING
        per conversation — auto-title runs on every session's first
        assistant turn."""
        import logging

        self._clear_credential_env(monkeypatch)
        with caplog.at_level(logging.WARNING, logger="app.chat.auto_title"):
            asyncio.run(auto_title.generate_title("First session"))
            asyncio.run(auto_title.generate_title("Second session"))
            asyncio.run(auto_title.generate_title("Third session"))
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, f"expected the warning once, got {len(warnings)}"

    def test_configured_but_minting_failed_warns_every_time(self, monkeypatch, caplog):
        """A credential that IS configured (WIF env vars present) but
        fails to mint (expired rule, revoked SA, transient network error)
        is a different, ongoing condition from 'nothing configured' — it
        must stay visible on every occurrence, not just the first."""
        import logging

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_FEDERATION_RULE_ID", "fdrl_test")
        monkeypatch.setenv("ANTHROPIC_ORGANIZATION_ID", "org_test")
        monkeypatch.setenv("ANTHROPIC_SERVICE_ACCOUNT_ID", "svac_test")
        monkeypatch.setenv("ANTHROPIC_IDENTITY_TOKEN", "fake-oidc-jwt")

        def fake_get_token():
            raise RuntimeError("token exchange failed: HTTP 401 invalid_grant")

        monkeypatch.setattr("app.auth.wif.get_federated_access_token", fake_get_token)
        with caplog.at_level(logging.WARNING, logger="app.chat.auto_title"):
            asyncio.run(auto_title.generate_title("First session"))
            asyncio.run(auto_title.generate_title("Second session"))
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2, f"expected a warning on every mint failure, got {len(warnings)}"


# --- ChatRepository ----------------------------------------------------------


@pytest.fixture
def repo() -> ChatRepository:
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    return ChatRepository(conn)


def test_set_title_persists(repo: ChatRepository):
    s = repo.create_session(user_email="u@x", surface=Surface.WEB)
    repo.set_title(s.id, "Hello world")
    again = repo.get_session(s.id)
    assert again is not None
    assert again.title == "Hello world"


def test_set_title_survives_existing_messages(repo: ChatRepository):
    """Regression guard for the DuckDB 1.5.3 FK+index bug. ``title`` is
    not indexed, so UPDATE must succeed even after child rows exist."""
    s = repo.create_session(user_email="u@x", surface=Surface.WEB)
    repo.append_message(session_id=s.id, role="user", content="hi")
    repo.append_message(session_id=s.id, role="assistant", content="hello")
    repo.set_title(s.id, "Greetings")
    again = repo.get_session(s.id)
    assert again is not None and again.title == "Greetings"


def test_get_first_user_message(repo: ChatRepository):
    s = repo.create_session(user_email="u@x", surface=Surface.WEB)
    repo.append_message(session_id=s.id, role="user", content="What tables do I have?")
    repo.append_message(session_id=s.id, role="assistant", content="You have 42.")
    repo.append_message(session_id=s.id, role="user", content="More detail please")
    assert repo.get_first_user_message(s.id) == "What tables do I have?"


def test_get_first_user_message_none_when_empty(repo: ChatRepository):
    s = repo.create_session(user_email="u@x", surface=Surface.WEB)
    assert repo.get_first_user_message(s.id) is None


# --- ChatManager integration -------------------------------------------------


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def close(self) -> None:  # pragma: no cover - unused
        pass


class _FakeHandle:
    def __init__(self) -> None:
        self.pid = 1234
        self.sandbox_id = "fake-sbx-auto-title"
        self._lines: asyncio.Queue[bytes] = asyncio.Queue()
        self.killed = False

    @property
    def stdin(self):
        outer = self

        class S:
            def write(self, b):
                outer  # noqa: B018 - ref keeps stdin alive

            async def drain(self):
                return None

        return S()

    @property
    def stdout(self):
        outer = self

        class _OutReader:
            async def readline(self):
                return await outer._lines.get()

        return _OutReader()

    @property
    def stderr(self):
        return self.stdout

    async def wait(self) -> int:
        while not self.killed:
            await asyncio.sleep(0.01)
        return 0

    async def kill(self, *, grace_sec: float = 5.0) -> None:
        self.killed = True

    def emit(self, payload: dict) -> None:
        self._lines.put_nowait((json.dumps(payload) + "\n").encode())

    def emit_eof(self) -> None:
        self._lines.put_nowait(b"")


def _make_manager(tmp_path: Path) -> ChatManager:
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "CLAUDE.md").write_text("d")
    workdir_mgr = WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.55.0",
        get_marketplace_sha=lambda: "sha-1",
        get_template_status=lambda: None,
    )
    provider = MagicMock()
    provider.spawn = AsyncMock()
    return ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=ChatConfig(enabled=True, concurrency_per_user=2),
    )


def test_assistant_message_triggers_auto_title(tmp_path: Path, monkeypatch):
    """End-to-end: a fake assistant_message frame causes the manager to
    call our fake generate_title, persist the result, and broadcast a
    ``session_renamed`` frame on the WS."""

    async def fake_gen(_msg: str, **_kwargs):
        return "Revenue trend"

    monkeypatch.setattr("app.chat.auto_title.generate_title", fake_gen)

    async def _run():
        manager = _make_manager(tmp_path)
        handle = _FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = _FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await asyncio.sleep(0.05)
        # Seed the first user message so get_first_user_message has
        # something to feed Haiku.
        manager._repo.append_message(session_id=s.id, role="user", content="Revenue?")
        # The runner would emit assistant_message after its turn; we
        # stand in for that here.
        handle.emit(
            {
                "type": "assistant_message",
                "content": "You had $42 in revenue.",
                "tokens_in": 10,
                "tokens_out": 5,
                "model": "fake",
            }
        )
        # Auto-title is scheduled as a separate task; give the event
        # loop room to run it.
        for _ in range(20):
            await asyncio.sleep(0.05)
            renamed = [m for m in ws.sent if m.get("type") == "session_renamed"]
            if renamed:
                break
        await manager.kill(s.id, reason="test_done")
        handle.emit_eof()
        try:
            await asyncio.wait_for(attach_task, timeout=1.0)
        except asyncio.TimeoutError:
            pass
        return manager, s.id, ws

    manager, chat_id, ws = asyncio.run(_run())
    persisted = manager._repo.get_session(chat_id)
    assert persisted is not None
    assert persisted.title == "Revenue trend"
    renamed = [m for m in ws.sent if m.get("type") == "session_renamed"]
    assert renamed, f"expected session_renamed frame in ws.sent={ws.sent}"
    assert renamed[0]["chat_id"] == chat_id
    assert renamed[0]["title"] == "Revenue trend"


def test_auto_title_fires_only_once(tmp_path: Path, monkeypatch):
    """Two assistant_message frames must not produce two Haiku calls."""
    call_count = {"n": 0}

    async def fake_gen(_msg: str, **_kwargs):
        call_count["n"] += 1
        return "Once only"

    monkeypatch.setattr("app.chat.auto_title.generate_title", fake_gen)

    async def _run():
        manager = _make_manager(tmp_path)
        handle = _FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = _FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await asyncio.sleep(0.05)
        manager._repo.append_message(session_id=s.id, role="user", content="q?")
        handle.emit({"type": "assistant_message", "content": "a1", "tokens_in": 1, "tokens_out": 1})
        handle.emit({"type": "assistant_message", "content": "a2", "tokens_in": 1, "tokens_out": 1})
        for _ in range(20):
            await asyncio.sleep(0.05)
            if call_count["n"] >= 1:
                break
        await asyncio.sleep(0.15)  # extra slack to catch a second call
        await manager.kill(s.id, reason="test_done")
        handle.emit_eof()
        try:
            await asyncio.wait_for(attach_task, timeout=1.0)
        except asyncio.TimeoutError:
            pass

    asyncio.run(_run())
    assert call_count["n"] == 1, f"auto-title fired {call_count['n']} times; want 1"


def test_auto_title_skipped_when_title_preset(tmp_path: Path, monkeypatch):
    """A session created with an explicit title is left alone."""
    called = {"n": 0}

    async def fake_gen(_msg: str, **_kwargs):
        called["n"] += 1
        return "Robot pick"

    monkeypatch.setattr("app.chat.auto_title.generate_title", fake_gen)

    async def _run():
        manager = _make_manager(tmp_path)
        handle = _FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(
            user_email="u@x",
            surface=Surface.WEB,
            title="User chose this",
        )
        ws = _FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await asyncio.sleep(0.05)
        manager._repo.append_message(session_id=s.id, role="user", content="q")
        handle.emit({"type": "assistant_message", "content": "a", "tokens_in": 1, "tokens_out": 1})
        await asyncio.sleep(0.2)
        await manager.kill(s.id, reason="test_done")
        handle.emit_eof()
        try:
            await asyncio.wait_for(attach_task, timeout=1.0)
        except asyncio.TimeoutError:
            pass
        return manager._repo.get_session(s.id)

    persisted = asyncio.run(_run())
    assert called["n"] == 0, "auto-title should not run when a title is preset"
    assert persisted is not None
    assert persisted.title == "User chose this"


def test_auto_title_swallows_haiku_failure(tmp_path: Path, monkeypatch):
    """A crashed Haiku call must not kill the session; the title stays NULL."""

    async def fake_gen(_msg: str, **_kwargs):
        raise RuntimeError("Haiku down")

    monkeypatch.setattr("app.chat.auto_title.generate_title", fake_gen)

    async def _run():
        manager = _make_manager(tmp_path)
        handle = _FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = _FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await asyncio.sleep(0.05)
        manager._repo.append_message(session_id=s.id, role="user", content="q")
        handle.emit({"type": "assistant_message", "content": "a", "tokens_in": 1, "tokens_out": 1})
        await asyncio.sleep(0.2)
        await manager.kill(s.id, reason="test_done")
        handle.emit_eof()
        try:
            await asyncio.wait_for(attach_task, timeout=1.0)
        except asyncio.TimeoutError:
            pass
        return manager._repo.get_session(s.id)

    persisted = asyncio.run(_run())
    assert persisted is not None
    assert persisted.title is None
