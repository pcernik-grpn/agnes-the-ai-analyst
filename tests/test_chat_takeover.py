"""Tests for cross-gateway claim-then-respawn takeover (wave-2F task 5).

Two ``ChatManager`` instances sharing one ``ChatRepository`` (same DuckDB
connection) AND one ``FakeProvider`` (same simulated E2B account) model two
gateway replicas — the same "two simulated managers, one shared backend"
convention ``tests/test_chat_routing.py`` and ``tests/test_chat_inbound.py``
already established. ``app.chat.routing.this_gateway_id`` is monkeypatched
around each manager's calls to give the two a distinct identity, since
within one test process the real ``default_holder_id()`` (hostname:pid) is
identical for both — sharing the provider (unlike test_chat_inbound.py,
which never needs the non-owner gateway to actually touch a sandbox) is
what lets these tests observe the SAME destroy()/spawn() call history no
matter which manager made the call, mirroring a real deployment where both
gateways talk to the same E2B account.

Uses asyncio.run() per the project convention (no pytest-asyncio required)
— see tests/test_chat_manager.py.
"""

from __future__ import annotations

import asyncio
import collections
import contextvars
import json
from pathlib import Path

import duckdb
import pytest

from src.db import _ensure_schema
from tests.chat_fakes import FakeHandle, FakeProvider, FakeWS, _wait_until

import app.chat.manager as manager_mod
import app.chat.routing as routing_mod
from app.chat import inbound, routing
from app.chat.config import ChatConfig
from app.chat.manager import ChatManager
from app.chat.persistence import ChatRepository
from app.chat.types import SessionState, Surface
from app.chat.workdir import WorkdirManager
from app.coordination.factory import reset_coordination_for_tests


@pytest.fixture(autouse=True)
def _reset_coordination():
    """Routing leases live in the coordination-backend singleton, which
    persists across tests unless reset — same rationale as
    tests/test_chat_routing.py and tests/test_chat_inbound.py."""
    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


# Per-chat_id count of `inbound.peek_seq` calls — i.e. how many separate
# consumers (the original owner's, then a takeover's fresh one, ...) have
# performed their initial cursor seed. See tests/test_chat_inbound.py's
# `_track_inbound_peeks` for the full rationale: without this, a
# cross-gateway publish/control message landing in the narrow window
# between a consumer TASK existing and it actually running its first peek
# is silently treated as already-delivered and never seen. A plain "has
# been peeked at least once" set isn't enough here — a takeover reuses the
# same chat_id with a SECOND, fresh consumer, and some tests need to wait
# specifically for THAT second peek (not just the original owner's).
_peeked_chat_ids: "collections.Counter[str]" = collections.Counter()


@pytest.fixture(autouse=True)
def _track_inbound_peeks(monkeypatch):
    _peeked_chat_ids.clear()
    orig_peek_seq = inbound.peek_seq

    def _tracking_peek_seq(chat_id: str) -> int:
        result = orig_peek_seq(chat_id)
        _peeked_chat_ids[chat_id] += 1
        return result

    monkeypatch.setattr(inbound, "peek_seq", _tracking_peek_seq)
    yield
    _peeked_chat_ids.clear()


# asyncio only holds a WEAK reference to a task from `create_task()` —
# `_spawn_owned_session` fires `attach()` fire-and-forget; keep a strong
# reference until it's done so a GC pass during a long poll loop can't
# collect it mid-flight (see tests/test_chat_inbound.py's `_BACKGROUND_TASKS`
# for the full rationale).
_BACKGROUND_TASKS: set[asyncio.Task] = set()


class _FakeTicketRepo:
    """Stand-in for src.repositories.ticket_repo() — records mint/revoke
    calls instead of touching a real chat_broker_tickets table. Shared
    between both simulated gateways (a real broker ticket table would be
    shared too — it lives in the same DB both gateways talk to)."""

    def __init__(self) -> None:
        self.minted: list[tuple[str, str]] = []
        self.revoked: list[str] = []

    def mint(self, session_id: str, scope: str, ttl_seconds: int = 3600) -> str:
        self.minted.append((session_id, scope))
        return f"ticket-{scope}-{len(self.minted)}"

    def revoke_session(self, session_id: str) -> None:
        self.revoked.append(session_id)


def _make_workdir_mgr(tmp_path: Path, repo: ChatRepository) -> WorkdirManager:
    bundled = tmp_path / "bundled"
    bundled.mkdir(parents=True)
    (bundled / "CLAUDE.md").write_text("d")
    return WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.55.0",
        get_marketplace_sha=lambda: "sha-1",
        get_template_status=lambda: None,
    )


def _make_manager(tmp_path: Path, repo: ChatRepository, provider: FakeProvider) -> ChatManager:
    workdir_mgr = _make_workdir_mgr(tmp_path, repo)
    mgr = ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=ChatConfig(
            enabled=True,
            concurrency_per_user=5,
            on_detach="pause",
            detach_linger_seconds=0,
            idle_grace_seconds=0,
            paused_ttl_seconds=7 * 24 * 3600,
            idle_ttl_seconds=10**9,
        ),
    )
    # Bypass real filesystem workspace prep — irrelevant to the
    # routing/lease/sandbox-lifecycle mechanics under test.
    import unittest.mock as mock

    mgr._workdir_mgr.ensure_user_workdir = mock.MagicMock()
    mgr._workdir_mgr.prepare_session_dir = mock.MagicMock(return_value=Path("/tmp/fake-session-dir"))
    return mgr


@pytest.fixture
def two_gateways(tmp_path: Path) -> tuple[ChatManager, ChatManager, FakeProvider]:
    """Two independent ChatManager instances sharing one ChatRepository/
    DuckDB connection and one FakeProvider — simulated gateway replicas A
    and B talking to the same shared sandbox account. Neither manager's
    ``_live`` dict is shared, matching two separate gateway PROCESSES."""
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    provider = FakeProvider()
    mgr_a = _make_manager(tmp_path / "gw-a", repo, provider)
    mgr_b = _make_manager(tmp_path / "gw-b", repo, provider)
    return mgr_a, mgr_b, provider


#: Backs ``_as_gateway`` below. A plain ``monkeypatch.setattr(routing_mod,
#: "this_gateway_id", lambda: gateway_id)`` replaces a SINGLE shared,
#: mutable value: flipping it to "gw-b" for B's takeover doesn't just affect
#: B's own call chain, it also retroactively changes what gateway A's
#: still-running background ``_wait_for_exit_and_respawn`` task sees the
#: NEXT time it happens to call ``this_gateway_id()`` — however long after
#: A's own spawn that task next gets scheduled. If that scheduling lands
#: after the flip to "gw-b" (a race that gets MORE, not less, likely under a
#: loaded CI runner), A's crash-check wrongly reads itself AS "gw-b", finds
#: ``owner_of() == this_gw``, and respawns a THIRD, orphaned sandbox — a
#: split-brain artifact of the test harness's identity simulation, not of
#: the ownership-check logic itself (real gateways never change their own
#: identity mid-process).
#:
#: ``contextvars`` fixes this at the root: ``asyncio.create_task`` copies
#: the CURRENT context at creation time, so every task spawned from within
#: gateway A's call chain (including its pump/wait/inbound background
#: tasks) keeps resolving to "gw-a" forever, regardless of what a LATER
#: ``_as_gateway(monkeypatch, "gw-b")`` call sets in the test's own
#: top-level context for gateway B's subsequent work.
_gateway_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("test_gateway_id", default="unset")


def _as_gateway(monkeypatch: pytest.MonkeyPatch, gateway_id: str) -> None:
    monkeypatch.setattr(routing_mod, "this_gateway_id", _gateway_id_ctx.get)
    _gateway_id_ctx.set(gateway_id)


async def _spawn_owned_session(mgr: ChatManager) -> tuple[str, FakeHandle]:
    """Create + attach a session on ``mgr`` so it becomes the live ACTIVE
    owner — claims the routing lease under whatever gateway id is currently
    patched onto app.chat.routing.this_gateway_id.

    Polls rather than a single fixed sleep: ``ChatManager._spawn_live``
    registers the ``LiveSession`` as ``state=ACTIVE`` in ``self._live``
    BEFORE it awaits the routing-lease claim (``self._claim_routing_lease``,
    which hops through ``asyncio.to_thread``) and before it starts the
    inbound-stream consumer (``live.inbound_task``) — so a short fixed sleep,
    or even a poll that stops at "state ACTIVE + lease claimed", can still
    race ahead of the consumer's initial ``peek_seq`` cursor-seed. A
    cross-gateway publish/control message landing in that gap is silently
    treated as already-delivered and never seen (see ``_track_inbound_peeks``
    above). Waiting for the lease, the inbound task, AND its initial peek
    to all have landed makes every caller of this helper deterministic
    regardless of scheduling delays.
    """
    session = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
    ws = FakeWS()
    task = asyncio.create_task(mgr.attach(session.id, ws))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    this_gw = routing.this_gateway_id()

    def _ready() -> bool:
        live = mgr._live.get(session.id)
        return (
            live is not None
            and live.state == SessionState.ACTIVE
            and live.inbound_task is not None
            and routing.owner_of(session.id) == this_gw
            and session.id in _peeked_chat_ids
        )

    ok = await _wait_until(_ready)
    if not ok:
        raise AssertionError(
            f"session {session.id} never reached ACTIVE with a claimed routing lease for gateway {this_gw!r}"
        )
    return session.id, mgr._live[session.id].handle


def _stdin_texts(handle: FakeHandle) -> list[str]:
    out = []
    for b in handle._stdin_buf:
        frame = json.loads(b.decode("utf-8"))
        if frame.get("type") == "user_msg":
            out.append(frame["text"])
    return out


# ---------------------------------------------------------------------------
# The headline scenario: WS connect lands on B, session owned by A
# ---------------------------------------------------------------------------


def test_takeover_claims_lease_destroys_old_spawns_fresh_restores_context(two_gateways, monkeypatch):
    """Session owned by gateway A (lease held, in A's _live); a connect
    lands on gateway B → B claims the lease, destroys A's old sandbox,
    spawns exactly one fresh runner, and uploads the restored-conversation
    transcript for it (full history via system prompt — the old
    replay-3-user-turns-over-stdin path is gone)."""
    mgr_a, mgr_b, provider = two_gateways
    fake_tickets = _FakeTicketRepo()
    monkeypatch.setattr(manager_mod, "ticket_repo", lambda: fake_tickets)

    # Capture the takeover spawn's context upload: attach a fake E2B sandbox
    # to every handle B spawns so _spawn_runner's sync branch runs.
    from unittest.mock import AsyncMock, MagicMock

    context_writes: list[tuple[str, object]] = []
    orig_spawn_b = mgr_b._provider.spawn

    async def _spawn_with_sandbox(**kw):
        handle = await orig_spawn_b(**kw)
        sb = MagicMock()
        sb.files = MagicMock()

        async def _write(path, data):
            context_writes.append((path, data))

        sb.files.write = AsyncMock(side_effect=_write)
        sb.commands = MagicMock()
        sb.commands.run = AsyncMock()
        handle._sandbox = sb
        return handle

    mgr_b._provider.spawn = _spawn_with_sandbox

    # Spy on provider.resume — the runner-protocol ticket guard means a
    # foreign takeover must NEVER attempt to reconnect the old runner.
    resume_calls: list[dict] = []
    orig_resume = provider.resume

    async def _spy_resume(**kw):
        resume_calls.append(kw)
        return await orig_resume(**kw)

    provider.resume = _spy_resume

    async def _run():
        _as_gateway(monkeypatch, "gw-a")
        chat_id, handle_a = await _spawn_owned_session(mgr_a)
        assert routing.owner_of(chat_id) == "gw-a"
        assert fake_tickets.minted, "A's spawn should have pushed its own tickets"
        tickets_before_takeover = len(fake_tickets.minted)

        # A turn already happened and is persisted, so B's fresh runner has
        # something concrete to replay.
        mgr_a._repo.append_message(session_id=chat_id, role="user", content="hello before takeover")

        _as_gateway(monkeypatch, "gw-b")
        ws_b = FakeWS()
        await mgr_b.attach(chat_id, ws_b)

        # Ownership moved to B.
        assert routing.owner_of(chat_id) == "gw-b"

        # Exactly one fresh spawn happened for the takeover (A's original
        # spawn + B's single takeover spawn = 2 total).
        assert len(provider.spawned) == 2, (
            f"expected A's original spawn + exactly one takeover spawn, got {len(provider.spawned)}"
        )
        live_b = mgr_b._live[chat_id]
        assert live_b.state == SessionState.ACTIVE
        handle_b = live_b.handle
        assert handle_b is not handle_a
        assert handle_b.sandbox_id != handle_a.sandbox_id

        # The OLD sandbox was destroyed exactly once.
        assert provider.destroyed.count(handle_a.sandbox_id) == 1

        # The runner-protocol guard was respected: no foreign live-resume
        # was ever attempted for this takeover.
        assert resume_calls == [], "provider.resume must never be called for a foreign takeover"

        # The repo row now points at B's fresh sandbox.
        row = mgr_b._repo.get_session(chat_id)
        assert row is not None
        assert row.sandbox_id == handle_b.sandbox_id
        assert row.runner_pid == handle_b.pid

        # Old tickets were revoked and fresh ones minted for the new runner.
        assert chat_id in fake_tickets.revoked
        assert len(fake_tickets.minted) > tickets_before_takeover

        # Continuity travels as the restored-context transcript uploaded to
        # the fresh sandbox — NOT as raw user_msg frames on stdin (each of
        # which ran a full LLM turn and dropped all assistant context).
        from app.chat.sandbox_staging import SANDBOX_CONTEXT_RESTORE

        ctx = [d for p, d in context_writes if p == SANDBOX_CONTEXT_RESTORE]
        assert ctx, f"expected a restore-context upload; writes: {[p for p, _ in context_writes]}"
        assert "hello before takeover" in str(ctx[0])
        assert "Restored conversation context" in str(ctx[0])
        # The message is also the TRAILING UNANSWERED user turn, so it is
        # re-delivered exactly once as a live turn (pending-question
        # redelivery) — not blindly replayed like the old 3-turn path.
        assert _stdin_texts(handle_b).count("hello before takeover") == 1

        await mgr_b.kill(chat_id, reason="test_done")

    asyncio.run(_run())


def test_old_owner_renew_fails_tears_down_without_second_destroy(two_gateways, monkeypatch):
    """After B's takeover, A's next routing-lease renew fails — A must tear
    down its local LiveSession cleanly (cancel tasks, drop bookkeeping)
    WITHOUT destroying the sandbox a second time and WITHOUT touching the
    repo's sandbox_ref (which now belongs to B's fresh runner)."""
    mgr_a, mgr_b, provider = two_gateways
    fake_tickets = _FakeTicketRepo()
    monkeypatch.setattr(manager_mod, "ticket_repo", lambda: fake_tickets)

    async def _run():
        _as_gateway(monkeypatch, "gw-a")
        chat_id, handle_a = await _spawn_owned_session(mgr_a)
        live_a = mgr_a._live[chat_id]
        a_tasks = list(live_a.tasks)
        a_inbound_task = live_a.inbound_task
        assert a_inbound_task is not None

        _as_gateway(monkeypatch, "gw-b")
        ws_b = FakeWS()
        await mgr_b.attach(chat_id, ws_b)
        handle_b = mgr_b._live[chat_id].handle
        destroyed_after_takeover = list(provider.destroyed)
        assert handle_a.sandbox_id in destroyed_after_takeover

        # A's own reaper tick tries to renew and discovers the lease is gone.
        _as_gateway(monkeypatch, "gw-a")
        await mgr_a._renew_routing_leases()

        # A stopped serving the session locally.
        assert chat_id not in mgr_a._live
        assert chat_id not in mgr_a._known_protocol_sessions

        await _wait_until(  # let cancellation propagate
            lambda: all(t.done() for t in a_tasks) and (a_inbound_task.cancelled() or a_inbound_task.done())
        )
        assert all(t.done() for t in a_tasks), "A's pump/wait tasks must be cancelled on lost-ownership teardown"
        assert a_inbound_task.cancelled() or a_inbound_task.done()

        # No second destroy call — the sandbox was only ever destroyed once,
        # by B's takeover.
        assert provider.destroyed == destroyed_after_takeover

        # B's fresh refs in the repo were NOT clobbered by A's teardown.
        row = mgr_a._repo.get_session(chat_id)
        assert row is not None
        assert row.sandbox_id == handle_b.sandbox_id

        _as_gateway(monkeypatch, "gw-b")
        await mgr_b.kill(chat_id, reason="test_done")

    asyncio.run(_run())


def test_concurrent_takeover_attempts_on_same_gateway_serialized_single_spawn(two_gateways, monkeypatch):
    """Two WS connects landing on B at (nearly) the same instant for the
    same foreign-owned session must produce exactly ONE fresh spawn and ONE
    destroy of the old sandbox — composes with the takeover lock the same
    way _resume_lock composes with a same-process PAUSED-session race."""
    mgr_a, mgr_b, provider = two_gateways
    fake_tickets = _FakeTicketRepo()
    monkeypatch.setattr(manager_mod, "ticket_repo", lambda: fake_tickets)

    async def _run():
        _as_gateway(monkeypatch, "gw-a")
        chat_id, handle_a = await _spawn_owned_session(mgr_a)

        # Inject latency into spawn (only now, AFTER A's own spawn already
        # completed) so B's two concurrent takeover attempts actually
        # interleave instead of one finishing before the other starts.
        orig_spawn = provider.spawn

        async def _slow_spawn(**kw):
            await asyncio.sleep(0.05)
            return await orig_spawn(**kw)

        provider.spawn = _slow_spawn

        _as_gateway(monkeypatch, "gw-b")
        ws1, ws2 = FakeWS(), FakeWS()
        results = await asyncio.gather(
            mgr_b.attach(chat_id, ws1),
            mgr_b.attach(chat_id, ws2),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                raise r

        # A's original spawn + exactly one takeover spawn on B.
        assert len(provider.spawned) == 2
        assert provider.destroyed.count(handle_a.sandbox_id) == 1
        assert chat_id in mgr_b._live
        live_b = mgr_b._live[chat_id]
        # Both websockets should have been seated on the SAME live session.
        assert {e.sink for e in live_b.sinks} == {ws1, ws2}

        await mgr_b.kill(chat_id, reason="test_done")

    asyncio.run(_run())


def test_second_attach_during_inflight_takeover_respawn_single_spawn(two_gateways, monkeypatch):
    """Critical-1 lock-composition gap: a SECOND attach() for the same
    chat_id, landing on the SAME gateway (B) while the FIRST attach()'s
    takeover has already registered a state=NEW LiveSession and claimed the
    routing lease but has NOT yet finished spawning, must not produce a
    second spawn.

    Pre-fix, the second attach() call reads `self._live.get(chat_id)`,
    finds the NEW-state stub (not ACTIVE/PAUSED — falls through both early
    branches), and since `owner_of` already resolves to THIS gateway (the
    first call's claim already landed), it skips the takeover branch too;
    the repo row's sandbox refs are already cleared, so it falls all the
    way through to `_spawn_live` — a second, independent spawn. The
    previous `_takeover_locks` lock never protected this fallthrough path at
    all (it only wrapped `_takeover_foreign_session`'s own body), so the
    race was open. Post-fix, `attach()` wraps its ENTIRE decision tree in
    one per-chat_id lock (`self._get_session_lock`), so the second call
    simply blocks until the first fully settles.

    Load-bearing: reverting the `self._get_session_lock` wrapper around
    `attach()` in `app/chat/manager.py` (`git stash` the fix) makes this
    fail with 3 total spawns instead of 2.
    """
    mgr_a, mgr_b, provider = two_gateways
    fake_tickets = _FakeTicketRepo()
    monkeypatch.setattr(manager_mod, "ticket_repo", lambda: fake_tickets)

    async def _run():
        _as_gateway(monkeypatch, "gw-a")
        chat_id, handle_a = await _spawn_owned_session(mgr_a)

        # Slow down B's fresh spawn so its takeover has time to register the
        # state=NEW LiveSession and claim the routing lease, but NOT finish
        # spawning, before a second attach() call lands.
        orig_spawn = provider.spawn
        spawn_started = asyncio.Event()

        async def _slow_spawn(**kw):
            spawn_started.set()
            await asyncio.sleep(0.1)
            return await orig_spawn(**kw)

        provider.spawn = _slow_spawn

        _as_gateway(monkeypatch, "gw-b")
        ws1, ws2 = FakeWS(), FakeWS()
        attach1 = asyncio.create_task(mgr_b.attach(chat_id, ws1))

        # Wait until the takeover is mid-spawn — i.e., past the exact point
        # where the pre-fix code's owner_of check would already resolve to
        # gw-b, but before the fresh handle is registered / state flips to
        # ACTIVE.
        await asyncio.wait_for(spawn_started.wait(), timeout=2.0)
        assert routing.owner_of(chat_id) == "gw-b", "precondition: B must already hold the lease mid-spawn"
        live_mid = mgr_b._live.get(chat_id)
        assert live_mid is not None and live_mid.state == SessionState.NEW, (
            "precondition: the second attach() must race a state=NEW, not-yet-ACTIVE entry"
        )

        attach2 = asyncio.create_task(mgr_b.attach(chat_id, ws2))
        results = await asyncio.gather(attach1, attach2, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                raise r

        assert len(provider.spawned) == 2, (
            f"expected A's original spawn + exactly ONE takeover spawn on B, got {len(provider.spawned)} — "
            "a second spawn means the in-flight-takeover race produced a leaked, orphaned sandbox"
        )
        live_b = mgr_b._live[chat_id]
        assert live_b.state == SessionState.ACTIVE
        # Both websockets must have landed on the SAME live session.
        assert {e.sink for e in live_b.sinks} == {ws1, ws2}

        await mgr_b.kill(chat_id, reason="test_done")

    asyncio.run(_run())


def test_old_owner_crash_path_does_not_respawn_after_foreign_takeover(two_gateways, monkeypatch):
    """Critical-2 split-brain: when B's takeover destroys A's ACTIVE
    sandbox (a cross-owner destroy — FakeProvider.destroy now correctly
    breaks the corresponding FakeHandle.wait(), see tests/chat_fakes.py),
    A's OWN crash-respawn path (_wait_for_exit_and_respawn) observes the
    resulting non-zero exit exactly as if the runner had genuinely crashed.
    It must notice that `routing.owner_of` no longer resolves to A and
    must NOT respawn or overwrite the repo's sandbox_ref — otherwise there
    would be two live runners for one chat_id and a leaked third sandbox.

    This exercises the CRASH path specifically (no explicit
    `_renew_routing_leases()` call, unlike
    test_old_owner_renew_fails_tears_down_without_second_destroy above) —
    A's background wait task must notice on its own.

    Load-bearing: reverting the ownership check in
    `ChatManager._wait_for_exit_and_respawn` (`git stash` the fix) makes
    this fail — A respawns a THIRD sandbox and clobbers the repo row back
    to its own fresh (orphaned) sandbox_id.
    """
    mgr_a, mgr_b, provider = two_gateways
    fake_tickets = _FakeTicketRepo()
    monkeypatch.setattr(manager_mod, "ticket_repo", lambda: fake_tickets)

    async def _run():
        _as_gateway(monkeypatch, "gw-a")
        chat_id, handle_a = await _spawn_owned_session(mgr_a)

        _as_gateway(monkeypatch, "gw-b")
        ws_b = FakeWS()
        await mgr_b.attach(chat_id, ws_b)  # takeover: destroys handle_a
        handle_b = mgr_b._live[chat_id].handle
        assert handle_a.killed, "precondition: B's takeover must have destroyed A's sandbox"

        # `_as_gateway` monkeypatches `routing.this_gateway_id` GLOBALLY
        # (it's a shared module-level function, not per-manager) — B's own
        # takeover work above is all synchronous and has already finished,
        # so it's now safe to flip the patched identity back to "gw-a"
        # before A's BACKGROUND wait task (still asynchronously polling
        # `handle_a.wait()`) gets scheduled and calls `this_gateway_id()`
        # itself. Without this, A's crash-check would incorrectly see
        # itself AS "gw-b" (whatever the monkeypatch was last left at) and
        # wrongly conclude it still owns the lease.
        _as_gateway(monkeypatch, "gw-a")

        # Give A's own background wait task time to notice the crash and
        # run its ownership check and teardown.
        await _wait_until(lambda: chat_id not in mgr_a._live)
        assert chat_id not in mgr_a._live, "A's crash-respawn path must tear itself down after losing ownership"

        # Exactly the spawns we expect: A's original + B's takeover spawn —
        # NOT a third spawn from A wrongly respawning after losing
        # ownership.
        assert len(provider.spawned) == 2, (
            f"expected exactly 2 spawns (A's original + B's takeover), got {len(provider.spawned)} — "
            "A's crash-respawn path spawned a THIRD sandbox after losing ownership (split-brain)"
        )

        # B's fresh refs in the repo were NOT clobbered by A's crash path.
        row = mgr_a._repo.get_session(chat_id)
        assert row is not None
        assert row.sandbox_id == handle_b.sandbox_id
        assert row.runner_pid == handle_b.pid

        # No extra destroy call beyond B's takeover destroy.
        assert provider.destroyed.count(handle_a.sandbox_id) == 1

        _as_gateway(monkeypatch, "gw-b")
        await mgr_b.kill(chat_id, reason="test_done")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Memory backend / single-process: the non-owner branch is unreachable
# ---------------------------------------------------------------------------


def test_memory_mode_never_triggers_takeover_on_reconnect(tmp_path, monkeypatch):
    """Single-process (default `memory` coordination backend) story: this
    process's own routing.this_gateway_id() is the only holder any lease
    it claims can ever have, so a reconnect for a session it once spawned
    — even with no local LiveSession entry, simulating this same process
    having "forgotten" it in-memory without ever releasing the lease (a
    stale self-claim, e.g. the ``owner == this_gw`` edge case attach()'s
    docstring calls out) — must fall through to the normal resume-from-row/
    spawn story, NEVER the foreign-takeover path."""
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    provider = FakeProvider()
    mgr = _make_manager(tmp_path, repo, provider)

    async def _run():
        chat_id, handle = await _spawn_owned_session(mgr)
        this_gw = routing.this_gateway_id()
        assert routing.owner_of(chat_id) == this_gw

        # Park the sandbox (pause) so a later resume-from-row can succeed
        # by reconnecting to it — mirrors a real post-restart resume.
        live = mgr._live[chat_id]
        await mgr._pause_live(live)
        assert live.state == SessionState.PAUSED

        # Simulate "no LiveSession in this process" (e.g. this exact process
        # forgot its in-memory registry across a restart) WITHOUT releasing
        # the routing lease — owner_of(chat_id) still resolves to this_gw,
        # exactly the ``owner == this_gw`` edge case attach()'s docstring
        # calls out.
        mgr._live.pop(chat_id, None)
        assert routing.owner_of(chat_id) == this_gw

        async def _must_not_be_called(*a, **k):
            raise AssertionError("_takeover_foreign_session must never be reached in single-process/memory mode")

        monkeypatch.setattr(mgr, "_takeover_foreign_session", _must_not_be_called)

        ws2 = FakeWS()
        await mgr.attach(chat_id, ws2)

        # Normal resume-from-row story worked: the SAME sandbox handle was
        # reconnected, not destroyed-and-replaced.
        assert chat_id in mgr._live
        assert mgr._live[chat_id].state == SessionState.ACTIVE
        assert mgr._live[chat_id].handle is handle
        # No sandbox was ever destroyed — this is NOT a takeover.
        assert provider.destroyed == []

        await mgr.kill(chat_id, reason="test_done")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Wave-2F final review F2: kill() racing a mid-flight respawn/takeover spawn
# ---------------------------------------------------------------------------


def _count_kills(handle: FakeHandle, counts: dict) -> None:
    """Wrap ``handle.kill`` so the test can assert 'destroyed exactly once'."""
    orig_kill = handle.kill

    async def _counted(**kw):
        counts[handle.sandbox_id] = counts.get(handle.sandbox_id, 0) + 1
        await orig_kill(**kw)

    handle.kill = _counted


def test_kill_during_takeover_spawn_window_no_leaked_sandbox(two_gateways, monkeypatch):
    """F2 headline regression: an /agnes-new–style archive+kill fired while
    a takeover's fresh spawn is still in flight must not leak the
    just-spawned sandbox or resurrect sandbox refs onto the archived row.

    Pre-fix, ``kill()`` popped ``_live`` without the per-chat_id session
    lock: it removed the takeover's state=NEW stub mid-spawn (nothing to
    tear down yet — handle still None), and the completing spawn then
    registered an ACTIVE handle and ``set_sandbox_ref``'d fresh refs onto
    the already-archived row — an untracked, still-billable sandbox with
    nothing left pointing at it. Post-fix, kill() waits on
    ``_get_session_lock`` for the takeover to settle and then tears the
    fully-registered session down normally (``_respawn_fresh``'s post-spawn
    re-check is the second layer for respawn paths that don't hold the
    lock — see test_kill_racing_lockless_respawn_destroys_orphan below).

    Load-bearing: with BOTH F2 layers reverted (git stash), the takeover's
    spawn wins the race — the row keeps the fresh sandbox_id and the new
    handle is never killed — and this test fails.
    """
    mgr_a, mgr_b, provider = two_gateways
    fake_tickets = _FakeTicketRepo()
    monkeypatch.setattr(manager_mod, "ticket_repo", lambda: fake_tickets)

    async def _run():
        _as_gateway(monkeypatch, "gw-a")
        chat_id, handle_a = await _spawn_owned_session(mgr_a)

        kill_counts: dict[str, int] = {}
        orig_spawn = provider.spawn
        spawn_started = asyncio.Event()
        release_spawn = asyncio.Event()

        async def _slow_spawn(**kw):
            spawn_started.set()
            await release_spawn.wait()
            h = await orig_spawn(**kw)
            _count_kills(h, kill_counts)
            return h

        provider.spawn = _slow_spawn

        _as_gateway(monkeypatch, "gw-b")
        ws_b = FakeWS()
        attach_task = asyncio.create_task(mgr_b.attach(chat_id, ws_b))
        await asyncio.wait_for(spawn_started.wait(), timeout=2.0)
        live_mid = mgr_b._live.get(chat_id)
        assert live_mid is not None and live_mid.state == SessionState.NEW, (
            "precondition: kill must land inside the takeover's spawn window"
        )

        # /agnes-new on a non-owning-but-same-gateway path: archive + kill,
        # exactly what services.slack_bot.commands._soft_archive_dm does.
        mgr_b._repo.archive_session(chat_id)
        kill_task = asyncio.create_task(mgr_b.kill(chat_id, reason="agnes_new"))
        await asyncio.sleep(0.05)  # let kill() reach (and block on) the session lock
        release_spawn.set()
        results = await asyncio.gather(attach_task, kill_task, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                raise r

        new_handle = provider.spawned[-1]
        assert new_handle is not handle_a
        # The freshly-spawned sandbox was torn down exactly once, not leaked.
        assert new_handle.killed, "the takeover's fresh sandbox must be destroyed, not leaked"
        assert kill_counts.get(new_handle.sandbox_id) == 1, (
            f"fresh sandbox must be destroyed exactly once, got {kill_counts}"
        )
        # No live entry survived on B; the archived row's refs stayed cleared.
        assert mgr_b._live == {}
        row = mgr_b._repo.get_session(chat_id)
        assert row is None or row.sandbox_id is None, (
            "kill racing the takeover spawn must not resurrect sandbox refs on the archived row"
        )
        # A's old sandbox: destroyed exactly once, by the takeover.
        assert provider.destroyed.count(handle_a.sandbox_id) == 1

    asyncio.run(_run())


def test_kill_racing_lockless_respawn_destroys_orphan(two_gateways, monkeypatch):
    """F2 defense-in-depth layer: ``_respawn_fresh`` reached WITHOUT the
    per-chat_id session lock (the ``_resume_live`` path — inbound consumer
    or send_user_message's already-live PAUSED branch) can still interleave
    with kill(). After its spawn completes it must notice the LiveSession
    was torn down mid-spawn (state DEAD / no longer the registered ``_live``
    entry) and destroy the just-spawned sandbox instead of registering it
    and ``set_sandbox_ref``-ing refs onto the killed row.

    Load-bearing: reverting only the post-spawn re-check in
    ``_respawn_fresh`` (git stash) makes this fail — the killed row gets
    fresh sandbox refs back and the new sandbox is never destroyed.
    """
    mgr_a, _mgr_b, provider = two_gateways
    fake_tickets = _FakeTicketRepo()
    monkeypatch.setattr(manager_mod, "ticket_repo", lambda: fake_tickets)

    async def _run():
        _as_gateway(monkeypatch, "gw-a")
        chat_id, _handle = await _spawn_owned_session(mgr_a)
        live = mgr_a._live[chat_id]

        # Park the session, then make it "legacy" so _resume_live takes the
        # _respawn_fresh branch (fresh spawn, not provider.resume). Both the
        # same-process fast-confirm set AND the persisted column (stamped
        # current by set_sandbox_ref at spawn time) must be cleared —
        # otherwise _is_current_protocol falls back to the still-current DB
        # value (Tier 1, restart-invariant reuse).
        await mgr_a._pause_live(live)
        assert live.state == SessionState.PAUSED
        mgr_a._known_protocol_sessions.discard(chat_id)
        mgr_a._repo._conn.execute("UPDATE chat_sessions SET relay_protocol_version = NULL WHERE id = ?", [chat_id])

        kill_counts: dict[str, int] = {}
        orig_spawn = provider.spawn
        spawn_started = asyncio.Event()
        release_spawn = asyncio.Event()

        async def _slow_spawn(**kw):
            spawn_started.set()
            await release_spawn.wait()
            h = await orig_spawn(**kw)
            _count_kills(h, kill_counts)
            return h

        provider.spawn = _slow_spawn

        # The inbound-consumer resume path: holds only live._resume_lock,
        # NOT the per-chat_id session lock kill() takes.
        resume_task = asyncio.create_task(mgr_a._resume_live(live))
        await asyncio.wait_for(spawn_started.wait(), timeout=2.0)

        # kill() lands mid-spawn — the session lock is free, so it proceeds
        # immediately: pops _live, marks DEAD, clears refs.
        await mgr_a.kill(chat_id, reason="remote_kill")
        assert chat_id not in mgr_a._live
        assert live.state == SessionState.DEAD

        release_spawn.set()
        await resume_task

        new_handle = provider.spawned[-1]
        # The orphaned spawn was destroyed exactly once, never registered.
        assert new_handle.killed, "the raced respawn's sandbox must be destroyed, not leaked"
        assert kill_counts.get(new_handle.sandbox_id) == 1
        assert live.handle is None, "a torn-down LiveSession must not get the raced handle registered"
        assert mgr_a._live == {}
        row = mgr_a._repo.get_session(chat_id)
        assert row is not None and row.sandbox_id is None, (
            "the raced respawn must not resurrect sandbox refs on the killed row"
        )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Wave-2F final review F3: takeover must not re-deliver retained inbound
# entries (or re-execute stale control entries)
# ---------------------------------------------------------------------------


def test_takeover_seeds_inbound_cursor_skips_retained_entries(two_gateways, monkeypatch):
    """F3: a fresh consumer (here: the one started by B's takeover) seeds
    its dedup cursor from the current chat-in-seq counter instead of
    reading the retained stream from 0. Pre-fix, the takeover re-fed every
    retained (already-delivered) user message to the fresh runner — a
    duplicate LLM answer per retained turn — and RE-EXECUTED retained
    control entries, so a stale ``control:kill`` tore the just-taken-over
    session right back down. Entries published AFTER the takeover must
    still be delivered.

    Load-bearing: with the seed reverted (git stash), B's session is
    killed by the stale control entry (and/or "m1" reaches B's runner a
    second time via the stream on top of the history replay) — this test
    fails on both assertions.
    """
    mgr_a, mgr_b, provider = two_gateways
    fake_tickets = _FakeTicketRepo()
    monkeypatch.setattr(manager_mod, "ticket_repo", lambda: fake_tickets)

    from app.chat import inbound as inbound_mod

    async def _run():
        _as_gateway(monkeypatch, "gw-a")
        chat_id, handle_a = await _spawn_owned_session(mgr_a)

        # Forward a message from B: A's consumer delivers it, and the entry
        # stays RETAINED in the chat-in:{chat_id} stream after delivery.
        _as_gateway(monkeypatch, "gw-b")
        await mgr_b.send_user_message(chat_id, "m1")
        await _wait_until(lambda: "m1" in _stdin_texts(handle_a))
        assert "m1" in _stdin_texts(handle_a), "precondition: A must deliver the forwarded turn"

        # Freeze A's consumer (simulating the old owner wedged mid-handoff)
        # and publish a control:kill it never executes — the stale-control
        # hazard a takeover used to re-execute.
        live_a = mgr_a._live[chat_id]
        assert live_a.inbound_task is not None
        live_a.inbound_task.cancel()
        await _wait_until(lambda: live_a.inbound_task.cancelled() or live_a.inbound_task.done())
        await inbound_mod.publish_control(chat_id, "kill", reason="stale")

        # B takes over: fresh LiveSession, fresh consumer, cursor seeded.
        ws_b = FakeWS()
        await mgr_b.attach(chat_id, ws_b)
        live_b = mgr_b._live[chat_id]
        handle_b = live_b.handle
        assert handle_b is not None

        # Wait for B's fresh consumer to have performed ITS OWN initial
        # peek (the SECOND peek of this chat_id — A's original spawn did
        # the first) before checking it didn't re-execute the stale kill —
        # a fixed sleep here raced the same "peek swallows a pre-published
        # entry" class of flake `_spawn_owned_session` guards against above.
        await _wait_until(lambda: _peeked_chat_ids[chat_id] >= 2)
        # A few extra ticks to let the fresh consumer's first read/dispatch
        # actually run past the peek before asserting on its effects.
        await asyncio.sleep(0.05)

        # The stale kill was NOT re-executed against the fresh session...
        assert chat_id in mgr_b._live, "a retained stale control:kill must not tear the takeover down"
        assert mgr_b._live[chat_id].state == SessionState.ACTIVE
        # ...and the retained "m1" was NOT re-delivered from the stream —
        # it reaches B's fresh runner exactly once, via _respawn_fresh's
        # persisted-history replay.
        # "m1" is the trailing unanswered user turn, so the takeover
        # re-delivers it exactly once as a live turn. A correctly seeded
        # cursor means the RETAINED STREAM copy is never delivered on top —
        # a count of 2 here would mean the stale stream entry leaked through.
        assert _stdin_texts(handle_b).count("m1") == 1, f"retained stream entry re-delivered: {_stdin_texts(handle_b)}"

        # A message published AFTER the takeover IS delivered.
        await inbound_mod.publish_inbound(chat_id, "after-takeover")
        await _wait_until(lambda: "after-takeover" in _stdin_texts(handle_b))
        assert "after-takeover" in _stdin_texts(handle_b), "post-takeover publishes must still be delivered"

        await mgr_b.kill(chat_id, reason="test_done")

    asyncio.run(_run())
