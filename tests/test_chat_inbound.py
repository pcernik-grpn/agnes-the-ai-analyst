"""Tests for app/chat/inbound.py + ChatManager's cross-gateway inbound
message routing (wave-2F task 4).

Two ``ChatManager`` instances sharing one ``ChatRepository`` (same DuckDB
connection) and the process-wide ``coordination()`` singleton simulate two
gateway replicas — the same "two simulated managers, one shared backend"
convention ``tests/test_chat_routing.py`` already established for the
routing-lease primitives themselves (task 1). ``app.chat.routing.
this_gateway_id`` is monkeypatched around each manager's calls to give the
two a distinct identity, since within one test process the real
``default_holder_id()`` (hostname:pid) is identical for both.

Uses asyncio.run() per the project convention (no pytest-asyncio required)
— see tests/test_chat_manager.py.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import duckdb
import pytest

from src.db import _ensure_schema
from tests.chat_fakes import FakeHandle, _wait_until

import app.chat.routing as routing_mod
from app.chat import inbound
from app.chat.config import ChatConfig
from app.chat.manager import ChatManager
from app.chat.persistence import ChatRepository
from app.chat.types import Surface
from app.chat.workdir import WorkdirManager
from app.coordination.base import CoordinationUnavailable
from app.coordination.factory import coordination, reset_coordination_for_tests


@pytest.fixture(autouse=True)
def _reset_coordination():
    """chat-in / chat-in-seq keys and routing leases all live in the
    coordination-backend singleton, which persists across tests unless
    reset — same rationale as tests/test_chat_routing.py."""
    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


# chat_ids whose owning consumer (ChatManager._inbound_consumer_loop) has
# performed its initial `inbound.peek_seq` cursor seed — see
# `_track_inbound_peeks` below for why `_spawn_owned_session` needs this.
_peeked_chat_ids: set[str] = set()


@pytest.fixture(autouse=True)
def _track_inbound_peeks(monkeypatch):
    """Test-only instrumentation: wrap ``inbound.peek_seq`` to record which
    chat_ids have been peeked.

    ``_inbound_consumer_loop`` seeds its dedup cursor via a single
    ``inbound.peek_seq(chat_id)`` call BEFORE it starts reading — by design
    (see that method's docstring), any entry published before this peek is
    silently treated as already-delivered, even if no consumer ever actually
    read it. `_spawn_owned_session`'s wait used to only confirm
    ``live.inbound_task is not None`` (the task OBJECT exists) — not that the
    task had progressed far enough to actually perform the peek. A
    cross-gateway kill/cancel/message published in that narrow gap (task
    created, peek not yet run) gets silently swallowed: the owner's consumer
    peeks a cursor that already covers it and never sees it, so the
    assertion that follows either hangs until `_wait_until`'s timeout or
    (worse) observes a handle killed via some unrelated later path with no
    matching counted call. Reproduced deterministically with a 400-iteration
    stress loop before this fix, clean after. Polling
    ``session.id in _peeked_chat_ids`` closes the gap.
    """
    _peeked_chat_ids.clear()
    orig_peek_seq = inbound.peek_seq

    def _tracking_peek_seq(chat_id: str) -> int:
        result = orig_peek_seq(chat_id)
        _peeked_chat_ids.add(chat_id)
        return result

    monkeypatch.setattr(inbound, "peek_seq", _tracking_peek_seq)
    yield
    _peeked_chat_ids.clear()


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


def _make_manager(tmp_path: Path, repo: ChatRepository) -> ChatManager:
    workdir_mgr = _make_workdir_mgr(tmp_path, repo)
    provider = MagicMock()
    provider.spawn = AsyncMock()
    return ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=ChatConfig(enabled=True, concurrency_per_user=2),
    )


@pytest.fixture
def two_gateways(tmp_path: Path) -> tuple[ChatManager, ChatManager]:
    """Two independent ChatManager instances sharing one ChatRepository/
    DuckDB connection — simulated gateway replicas A and B. Neither
    manager's ``_live`` dict is shared (each is a fresh instance), matching
    two separate gateway PROCESSES that happen to share both the
    coordination backend and the underlying session storage."""
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    mgr_a = _make_manager(tmp_path / "gw-a", repo)
    mgr_b = _make_manager(tmp_path / "gw-b", repo)
    return mgr_a, mgr_b


def _as_gateway(monkeypatch: pytest.MonkeyPatch, gateway_id: str) -> None:
    monkeypatch.setattr(routing_mod, "this_gateway_id", lambda: gateway_id)


# asyncio only holds a WEAK reference to a task from `create_task()` — per
# the stdlib docs, "save a reference to the result of this function, to
# avoid a task disappearing mid-execution." `_spawn_owned_session` fires
# `attach()` fire-and-forget and previously discarded the return value
# entirely; with nothing else referencing it, the task was eligible for GC
# mid-flight, and a `_wait_until` poll loop (which allocates plenty of
# short-lived objects across many ticks) can trigger a collection before
# `attach()` finishes — silently aborting the spawn/takeover instead of
# raising, which surfaced as assertions on `handle`/`kills` seeing a state
# that was never actually reached. Keeping tasks alive in this set until
# they're done closes that gap.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


async def _spawn_owned_session(mgr: ChatManager) -> tuple[str, FakeHandle]:
    """Create + attach a session on ``mgr`` so it becomes the live owner —
    claims the routing lease under whatever gateway id is currently patched
    onto app.chat.routing.this_gateway_id.

    Polls for the FULL settle of ``attach()``'s spawn path rather than a
    fixed sleep: ``ChatManager._spawn_live`` registers the LiveSession into
    ``mgr._live`` synchronously, well before it claims the ``chat:{id}``
    routing lease (``_claim_routing_lease``) or starts the inbound-stream
    consumer (``live.inbound_task``). A cross-gateway caller's
    ``send_user_message``/``kill``/``cancel`` on the OTHER manager consults
    ``routing.owner_of(chat_id)`` to decide whether to forward — if that
    read races ahead of the real claim, it sees ``None``, falls through to
    the "no local live session, no resumable repo row" branch, and raises
    ``SessionNotFound`` (this was a confirmed CI flake under pytest-xdist
    CPU contention, since a fixed 50ms sleep isn't reliably enough time for
    the claim to land). Also waits for the inbound consumer's initial
    ``peek_seq`` cursor-seed (see ``_track_inbound_peeks``) — otherwise a
    cross-gateway publish landing before that peek runs is silently treated
    as already-delivered and never reaches the runner. Together these make
    every cross-gateway test that calls this helper deterministic regardless
    of scheduling delays.
    """
    handle = FakeHandle()
    mgr._provider.spawn = AsyncMock(return_value=handle)
    session = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
    ws = MagicMock()
    ws.send_json = AsyncMock()
    task = asyncio.create_task(mgr.attach(session.id, ws))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)

    def _owned_and_ready() -> bool:
        live = mgr._live.get(session.id)
        return (
            live is not None
            and live.inbound_task is not None
            and routing_mod.owner_of(session.id) is not None
            and session.id in _peeked_chat_ids
        )

    await _wait_until(_owned_and_ready)
    return session.id, handle


def _stdin_texts(handle: FakeHandle) -> list[str]:
    out = []
    for b in handle._stdin_buf:
        frame = json.loads(b.decode("utf-8"))
        if frame.get("type") == "user_msg":
            out.append(frame["text"])
    return out


# ---------------------------------------------------------------------------
# app.chat.inbound primitives
# ---------------------------------------------------------------------------


class TestInboundPrimitives:
    def test_publish_inbound_assigns_increasing_seq(self):
        async def _run():
            seq1 = await inbound.publish_inbound("chat-x", "one")
            seq2 = await inbound.publish_inbound("chat-x", "two")
            assert seq1 == 1
            assert seq2 == 2

        asyncio.run(_run())

    def test_read_new_returns_entries_after_seq_in_order(self):
        async def _run():
            await inbound.publish_inbound("chat-y", "a")
            await inbound.publish_inbound("chat-y", "b")
            await inbound.publish_inbound("chat-y", "c")
            entries = inbound.read_new("chat-y", after_seq=1)
            assert [e["text"] for e in entries] == ["b", "c"]
            assert [e["seq"] for e in entries] == [2, 3]

        asyncio.run(_run())

    def test_read_new_after_seq_zero_returns_everything(self):
        async def _run():
            await inbound.publish_inbound("chat-z", "a")
            entries = inbound.read_new("chat-z", after_seq=0)
            assert len(entries) == 1
            assert entries[0]["text"] == "a"

        asyncio.run(_run())

    def test_read_new_unknown_chat_id_returns_empty(self):
        assert inbound.read_new("chat-never-published", after_seq=0) == []

    def test_publish_inbound_raises_inbound_publish_failed_on_coordination_unavailable(self, monkeypatch):
        class _Broken:
            def incr(self, *a, **k):
                raise CoordinationUnavailable("boom")

        monkeypatch.setattr(inbound, "coordination", lambda: _Broken())

        async def _run():
            with pytest.raises(inbound.InboundPublishFailed):
                await inbound.publish_inbound("chat-broken", "hi")

        asyncio.run(_run())

    def test_read_new_degrades_to_empty_on_coordination_unavailable(self, monkeypatch):
        class _Broken:
            def stream_read(self, *a, **k):
                raise CoordinationUnavailable("boom")

        monkeypatch.setattr(inbound, "coordination", lambda: _Broken())
        assert inbound.read_new("chat-broken-2", after_seq=0) == []

    def test_subscribe_notify_returns_none_on_coordination_unavailable(self, monkeypatch):
        class _Broken:
            def subscribe(self, *a, **k):
                raise CoordinationUnavailable("boom")

        monkeypatch.setattr(inbound, "coordination", lambda: _Broken())
        assert inbound.subscribe_notify("chat-broken-3", lambda m: None) is None


# ---------------------------------------------------------------------------
# ChatManager cross-gateway routing
# ---------------------------------------------------------------------------


class TestCrossGatewayRouting:
    def test_owner_delivers_directly_no_stream_write(self, two_gateways, monkeypatch):
        """Memory-mode / owner path: the message goes straight to the local
        runner's stdin, and the inbound coordination stream is never
        touched at all — verifies no double-delivery machinery kicks in
        when this gateway already owns the session."""
        mgr_a, _mgr_b = two_gateways

        async def _run():
            _as_gateway(monkeypatch, "gw-a")
            chat_id, handle = await _spawn_owned_session(mgr_a)

            await mgr_a.send_user_message(chat_id, "hello")

            assert _stdin_texts(handle) == ["hello"]
            assert inbound.read_new(chat_id, after_seq=0) == []

            await mgr_a.kill(chat_id, reason="test_done")

        asyncio.run(_run())

    def test_non_owner_publishes_to_stream_instead_of_spawning(self, two_gateways, monkeypatch):
        mgr_a, mgr_b = two_gateways

        async def _run():
            _as_gateway(monkeypatch, "gw-a")
            chat_id, _handle_a = await _spawn_owned_session(mgr_a)

            _as_gateway(monkeypatch, "gw-b")
            await mgr_b.send_user_message(chat_id, "from-b")

            # gw-b never spawned a competing runner for this session.
            assert mgr_b._live.get(chat_id) is None
            mgr_b._provider.spawn.assert_not_called()

            entries = inbound.read_new(chat_id, after_seq=0)
            assert [e["text"] for e in entries] == ["from-b"]

            _as_gateway(monkeypatch, "gw-a")
            await mgr_a.kill(chat_id, reason="test_done")

        asyncio.run(_run())

    def test_message_published_on_gateway_b_reaches_owner_a_runner_once(self, two_gateways, monkeypatch):
        """The headline scenario: a message published on gateway B while A
        owns the session reaches A's runner, exactly once."""
        mgr_a, mgr_b = two_gateways

        async def _run():
            _as_gateway(monkeypatch, "gw-a")
            chat_id, handle_a = await _spawn_owned_session(mgr_a)

            _as_gateway(monkeypatch, "gw-b")
            await mgr_b.send_user_message(chat_id, "hi-from-b")

            await _wait_until(lambda: len(_stdin_texts(handle_a)) >= 1)
            assert _stdin_texts(handle_a) == ["hi-from-b"]

            # Give the consumer a few more ticks to prove it doesn't
            # redeliver the same already-consumed entry.
            await asyncio.sleep(0.2)
            assert _stdin_texts(handle_a) == ["hi-from-b"]

            _as_gateway(monkeypatch, "gw-a")
            await mgr_a.kill(chat_id, reason="test_done")

        asyncio.run(_run())

    def test_forwarded_turn_id_links_user_row_to_delivered_turn(self, two_gateways, monkeypatch):
        """The bug this closes: the thin-producer forward path used to
        persist the user row with NO turn_id at all, while
        ``_deliver_local_user_message`` minted a fresh one for the live
        turn — an exported forwarded conversation could never pair the
        question with its answer. The producer must mint the id BEFORE
        persisting the user row (mirroring ``send_user_message``) and it
        must survive the trip through the chat-in stream to become the
        turn's actual id, so the user row and the assistant row that
        follows share one turn_id."""
        mgr_a, mgr_b = two_gateways
        repo = mgr_a._repo  # two_gateways shares one repo/connection across both managers

        captured_turn_ids: list[str | None] = []
        orig_append = repo.append_message

        def _spy_append(*, role, **kwargs):
            if role == "user":
                captured_turn_ids.append(kwargs.get("turn_id"))
            return orig_append(role=role, **kwargs)

        monkeypatch.setattr(repo, "append_message", _spy_append)

        async def _run():
            _as_gateway(monkeypatch, "gw-a")
            chat_id, handle_a = await _spawn_owned_session(mgr_a)

            _as_gateway(monkeypatch, "gw-b")
            await mgr_b.send_user_message(chat_id, "hi-from-b")

            await _wait_until(lambda: len(_stdin_texts(handle_a)) >= 1)

            assert len(captured_turn_ids) == 1
            persisted_turn_id = captured_turn_ids[0]
            assert persisted_turn_id  # minted before persist, not None/empty
            assert mgr_a._live[chat_id].turn_id == persisted_turn_id

            _as_gateway(monkeypatch, "gw-a")
            await mgr_a.kill(chat_id, reason="test_done")

        asyncio.run(_run())

    def test_legacy_inbound_entry_with_no_turn_id_still_delivers(self, two_gateways, monkeypatch):
        """An entry published by an older replica before this field existed
        carries no turn_id at all — the consumer must still deliver it and
        mint a fresh id locally, exactly as before this fix, never raise."""
        mgr_a, _mgr_b = two_gateways

        async def _run():
            _as_gateway(monkeypatch, "gw-a")
            chat_id, handle_a = await _spawn_owned_session(mgr_a)

            coordination().stream_append(
                inbound.stream_key(chat_id),
                {"seq": 1, "type": "user_message", "text": "legacy"},
                maxlen=inbound.STREAM_MAXLEN,
            )

            await _wait_until(lambda: len(_stdin_texts(handle_a)) >= 1)
            assert _stdin_texts(handle_a) == ["legacy"]
            assert mgr_a._live[chat_id].turn_id  # minted locally, non-empty

            _as_gateway(monkeypatch, "gw-a")
            await mgr_a.kill(chat_id, reason="test_done")

        asyncio.run(_run())

    def test_ordering_preserved_across_multiple_forwarded_messages(self, two_gateways, monkeypatch):
        mgr_a, mgr_b = two_gateways

        async def _run():
            _as_gateway(monkeypatch, "gw-a")
            chat_id, handle_a = await _spawn_owned_session(mgr_a)

            _as_gateway(monkeypatch, "gw-b")
            await mgr_b.send_user_message(chat_id, "one")
            await mgr_b.send_user_message(chat_id, "two")
            await mgr_b.send_user_message(chat_id, "three")

            await _wait_until(lambda: len(_stdin_texts(handle_a)) >= 3)
            assert _stdin_texts(handle_a) == ["one", "two", "three"]

            _as_gateway(monkeypatch, "gw-a")
            await mgr_a.kill(chat_id, reason="test_done")

        asyncio.run(_run())

    def test_dedup_on_redelivery(self, two_gateways, monkeypatch):
        """An at-least-once redelivery of an already-consumed inbound seq
        (e.g. a retried publish that actually landed) must not be applied
        to the runner twice."""
        mgr_a, mgr_b = two_gateways

        async def _run():
            _as_gateway(monkeypatch, "gw-a")
            chat_id, handle_a = await _spawn_owned_session(mgr_a)

            _as_gateway(monkeypatch, "gw-b")
            await mgr_b.send_user_message(chat_id, "once-only")

            await _wait_until(lambda: len(_stdin_texts(handle_a)) >= 1)
            assert _stdin_texts(handle_a) == ["once-only"]

            # Simulate a redelivery of the same (already-consumed) entry.
            coordination().stream_append(
                inbound.stream_key(chat_id),
                {"seq": 1, "text": "once-only"},
                maxlen=inbound.STREAM_MAXLEN,
            )
            await asyncio.sleep(0.3)  # a few consumer poll ticks

            assert _stdin_texts(handle_a) == ["once-only"]  # still exactly one delivery

            _as_gateway(monkeypatch, "gw-a")
            await mgr_a.kill(chat_id, reason="test_done")

        asyncio.run(_run())

    def test_memory_mode_direct_path_is_unchanged(self, two_gateways, monkeypatch):
        """Single-gateway (memory backend) sanity: owner_of can never
        differ from this_gateway_id under the memory backend, so a lone
        manager's send_user_message always takes the direct path —
        unchanged behavior from before this task."""
        mgr_a, _mgr_b = two_gateways

        async def _run():
            _as_gateway(monkeypatch, "solo-gw")
            chat_id, handle = await _spawn_owned_session(mgr_a)

            await mgr_a.send_user_message(chat_id, "solo message")

            assert _stdin_texts(handle) == ["solo message"]
            assert inbound.read_new(chat_id, after_seq=0) == []

            await mgr_a.kill(chat_id, reason="test_done")

        asyncio.run(_run())

    def test_coordination_unavailable_on_forward_raises_clean_error(self, two_gateways, monkeypatch):
        """A coordination-backend blip in the publish path must surface as
        a clean, specific InboundPublishFailed — not a crash, and not a
        silently dropped message."""
        mgr_a, mgr_b = two_gateways

        async def _run():
            _as_gateway(monkeypatch, "gw-a")
            chat_id, _handle_a = await _spawn_owned_session(mgr_a)

            _as_gateway(monkeypatch, "gw-b")

            async def _boom(*_a, **_k):
                raise inbound.InboundPublishFailed("boom")

            monkeypatch.setattr(inbound, "publish_inbound", _boom)
            with pytest.raises(inbound.InboundPublishFailed):
                await mgr_b.send_user_message(chat_id, "will not land")

            _as_gateway(monkeypatch, "gw-a")
            await mgr_a.kill(chat_id, reason="test_done")

        asyncio.run(_run())

    def test_cross_gateway_kill_executed_by_owner(self, two_gateways, monkeypatch):
        """FINDING 2 (multi-replica gate lift): kill() on a NON-owning
        replica used to be a process-local no-op (`self._live.pop -> None ->
        early return`) — the caller archived the row and revoked tickets
        while the foreign owner's sandbox kept running. Now the non-owner
        publishes a control:kill over the chat-in stream; the OWNER's
        inbound consumer executes its local kill (sandbox destroyed exactly
        once, by the owner; routing lease released), and the non-owner
        archives the row locally (idempotent)."""
        mgr_a, mgr_b = two_gateways

        async def _run():
            _as_gateway(monkeypatch, "gw-a")
            chat_id, handle_a = await _spawn_owned_session(mgr_a)

            kills: list[int] = []
            orig_kill = handle_a.kill

            async def _counting_kill(**kw):
                kills.append(1)
                await orig_kill(**kw)

            handle_a.kill = _counting_kill

            _as_gateway(monkeypatch, "gw-b")
            await mgr_b.kill(chat_id, reason="user_archive")

            # The non-owner never touched a sandbox itself.
            mgr_b._provider.destroy.assert_not_called()
            assert mgr_b._live.get(chat_id) is None
            # DB archive done locally on the non-owner (idempotent).
            assert mgr_b._repo.get_session(chat_id).archived is True

            _as_gateway(monkeypatch, "gw-a")
            await _wait_until(lambda: handle_a.killed)
            assert sum(kills) == 1, "sandbox must be destroyed exactly once, by the owner"
            await _wait_until(lambda: mgr_a._live.get(chat_id) is None)
            # Owner's local kill released the routing lease.
            assert routing_mod.owner_of(chat_id) is None
            # And it never redelivers: give the (now-cancelled) consumer time.
            await asyncio.sleep(0.1)
            assert sum(kills) == 1

        asyncio.run(_run())

    def test_kill_with_no_owner_keeps_prior_local_noop(self, two_gateways, monkeypatch):
        """No routing lease anywhere (orphan) → kill keeps today's behavior:
        ticket hygiene + early return, no control entry published."""
        _mgr_a, mgr_b = two_gateways

        async def _run():
            _as_gateway(monkeypatch, "gw-b")
            await mgr_b.kill("chat-orphan", reason="user_archive")
            assert inbound.read_new("chat-orphan", after_seq=0) == []

        asyncio.run(_run())

    def test_cross_gateway_cancel_reaches_owner_runner(self, two_gateways, monkeypatch):
        """FINDING 3: cancel() on a NON-owning replica publishes a
        control:cancel; the owner's consumer runs its local cancel, which
        writes the `cancel` frame to the runner's stdin."""
        mgr_a, mgr_b = two_gateways

        def _cancel_frames(handle: FakeHandle) -> int:
            return sum(1 for b in handle._stdin_buf if json.loads(b.decode("utf-8")).get("type") == "cancel")

        async def _run():
            _as_gateway(monkeypatch, "gw-a")
            chat_id, handle_a = await _spawn_owned_session(mgr_a)

            _as_gateway(monkeypatch, "gw-b")
            await mgr_b.cancel(chat_id)
            assert mgr_b._live.get(chat_id) is None

            _as_gateway(monkeypatch, "gw-a")
            await _wait_until(lambda: _cancel_frames(handle_a) == 1)
            # Session stays live — cancel is not kill.
            assert mgr_a._live.get(chat_id) is not None

            await mgr_a.kill(chat_id, reason="test_done")

        asyncio.run(_run())

    def test_slack_stop_button_cancels_cross_replica(self, two_gateways, monkeypatch):
        """The Slack Stop button POST (load-balanced, can land anywhere) goes
        through interactivity._on_stop -> mgr.cancel — assert the whole path
        now works when the click lands on the NON-owning replica."""
        from types import SimpleNamespace

        from services.slack_bot import interactivity as inter

        mgr_a, mgr_b = two_gateways

        def _cancel_frames(handle: FakeHandle) -> int:
            return sum(1 for b in handle._stdin_buf if json.loads(b.decode("utf-8")).get("type") == "cancel")

        async def _run():
            _as_gateway(monkeypatch, "gw-a")
            chat_id, handle_a = await _spawn_owned_session(mgr_a)

            _as_gateway(monkeypatch, "gw-b")
            monkeypatch.setattr(inter, "lookup_user_email", lambda repo, uid: "u@x")
            app = SimpleNamespace(state=SimpleNamespace(chat_repo=mgr_b._repo, chat_manager=mgr_b))
            it = inter.Interaction(
                action_id=inter.blocks.ACTION_STOP,
                slack_user_id="U1",
                channel_id="D1",
                response_url="https://hooks.slack/r1",
                value={"chat_id": chat_id, "owner": "u@x"},
            )
            await inter._on_stop(app, it)

            _as_gateway(monkeypatch, "gw-a")
            await _wait_until(lambda: _cancel_frames(handle_a) == 1)

            await mgr_a.kill(chat_id, reason="test_done")

        asyncio.run(_run())

    def test_forwarded_slack_message_reestablishes_slack_sink_on_owner(self, two_gateways, monkeypatch):
        """FINDING 6 (forward side): a user message forwarded from a Slack
        webhook on a NON-owning replica carries its Slack origin; the owner's
        consumer must (re-)establish a SlackSinkBridge for that channel
        before delivering — and must not stack duplicates."""
        from services.slack_bot.sink import SlackSinkBridge

        mgr_a, mgr_b = two_gateways

        async def _run():
            _as_gateway(monkeypatch, "gw-a")
            chat_id, handle_a = await _spawn_owned_session(mgr_a)

            _as_gateway(monkeypatch, "gw-b")
            await mgr_b.send_user_message(chat_id, "hi", slack_origin={"channel": "D9", "thread_ts": "1.2"})

            _as_gateway(monkeypatch, "gw-a")
            await _wait_until(lambda: _stdin_texts(handle_a) == ["hi"])
            live = mgr_a._live[chat_id]
            bridges = [e.sink for e in live.sinks if isinstance(e.sink, SlackSinkBridge)]
            assert len(bridges) == 1, "owner must seat a SlackSinkBridge for the forwarded Slack message"
            assert bridges[0]._channel == "D9"

            # Idempotent per (session, channel): a second forward reuses it.
            _as_gateway(monkeypatch, "gw-b")
            await mgr_b.send_user_message(chat_id, "again", slack_origin={"channel": "D9", "thread_ts": "1.2"})
            _as_gateway(monkeypatch, "gw-a")
            await _wait_until(lambda: _stdin_texts(handle_a) == ["hi", "again"])
            assert sum(isinstance(e.sink, SlackSinkBridge) for e in live.sinks) == 1

            await mgr_a.kill(chat_id, reason="test_done")

        asyncio.run(_run())

    def test_concurrency_cap_counts_sessions_across_gateways(self, two_gateways, monkeypatch):
        """FINDING 5: the per-user concurrency cap is lease-derived — a
        replica with NO local live sessions still sees the user's sessions
        live on other gateways and enforces the cap (config cap = 2)."""
        from app.chat.manager import ConcurrencyCapHit

        mgr_a, mgr_b = two_gateways

        async def _run():
            _as_gateway(monkeypatch, "gw-a")
            chat1, _h1 = await _spawn_owned_session(mgr_a)
            # Non-empty so create_session's empty-session GC doesn't archive it.
            await mgr_a.send_user_message(chat1, "keep me")
            chat2, _h2 = await _spawn_owned_session(mgr_a)
            assert mgr_a.active_count_for_user("u@x") == 2

            _as_gateway(monkeypatch, "gw-b")
            assert mgr_b.active_count_for_user("u@x") == 2, "leases must make A's sessions visible on B"
            with pytest.raises(ConcurrencyCapHit):
                await mgr_b.create_session(user_email="u@x", surface=Surface.WEB)

            _as_gateway(monkeypatch, "gw-a")
            await mgr_a.kill(chat1, reason="test_done")
            await mgr_a.kill(chat2, reason="test_done")
            # Leases released → count drops to zero on both replicas.
            assert mgr_a.active_count_for_user("u@x") == 0
            _as_gateway(monkeypatch, "gw-b")
            assert mgr_b.active_count_for_user("u@x") == 0

        asyncio.run(_run())

    def test_inbound_consumer_task_started_and_cancelled_on_kill(self, two_gateways, monkeypatch):
        mgr_a, _mgr_b = two_gateways

        async def _run():
            _as_gateway(monkeypatch, "gw-a")
            chat_id, _handle = await _spawn_owned_session(mgr_a)

            task = mgr_a._live[chat_id].inbound_task
            assert task is not None
            assert not task.done()

            await mgr_a.kill(chat_id, reason="test_done")
            await _wait_until(lambda: task.cancelled() or task.done())
            assert task.cancelled() or task.done()

        asyncio.run(_run())
