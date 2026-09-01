# Broker Turn-Usage Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Attribute broker-observed LLM usage to chat turns so engine-provider instances (whose assistant frames carry no usage) get `usage_turns` rows, `chat_messages` tokens, and working spend/session caps.

**Architecture:** A new coordination-backed counter seam (`app/chat/turn_usage.py`). The broker (`app/api/broker.py`) accumulates provider-reported usage per chat session on every session-bound completion it forwards; `ChatManager` destructively drains the counters once per turn at the `assistant_message` persist seam and hydrates the frame when the frame carries no usage. Everything downstream (message persist, daily spend counters, `usage_turns` write) is existing, unchanged code.

**Tech Stack:** Python, FastAPI, the existing coordination backend (`app/coordination/` — memory or Redis), pytest.

**Spec:** `docs/superpowers/specs/2026-09-01-broker-turn-usage-attribution-design.md`

## Global Constraints

- A3 ratchet: no new DuckDB app-state surface, no `src/db.py` step, no migration — this change needs **no schema change at all**.
- No new config knob: the behavior is always-on (defaults in code).
- Telemetry must never harm a turn: every new call path swallows its own failures (`CoordinationUnavailable` included) and never raises into the frame loop or the broker forward path.
- Frame-carried usage always wins; drained counters are then discarded (double-count guard — native-sandbox calls transit the same broker route).
- Vendor-agnostic wording in code/docs/commits (in-repo names like `kai_engine_provider` are fine; customer names are not).
- CHANGELOG bullet lands in the same change (Task 4).
- Quality hook runs ruff+mypy on every edited Python file automatically; fix what it flags before committing.
- Per-task verification: run the named test file(s); the integrate phase runs `scripts/verify_syncmap.py` + `--lane impacted` + `--lane fast`.

---

### Task 1: `app/chat/turn_usage.py` — the counter seam

**Files:**
- Create: `app/chat/turn_usage.py`
- Test: `tests/test_turn_usage.py` (new)

**Interfaces:**
- Consumes: `app.coordination.factory.coordination`, `app.coordination.base.CoordinationUnavailable` (existing).
- Produces (Tasks 2 and 3 rely on these exact signatures):
  - `add_turn_usage(session_id: str, usage: dict) -> None` — `usage` uses `parse_usage`'s normalized keys: `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens` (ints), `model` (str | None). Never raises.
  - `drain_turn_usage(session_id: str) -> dict | None` — atomic read-and-reset; returns the same five keys (token kinds as ints ≥ 0, `model` as str | None), or `None` when nothing was recorded or the backend is unavailable. Never raises.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_turn_usage.py`:

```python
"""The broker→manager turn-usage seam: coordination-backed per-session
counters written by the broker for every session-bound completion, drained
destructively (Redis GETDEL semantics) exactly once per turn by ChatManager.

Destructive drain is the design's safety property — no watermark state
anywhere, so a process restart or gateway takeover cannot double-count."""

import pytest

from app.chat.turn_usage import add_turn_usage, drain_turn_usage
from app.coordination.base import CoordinationUnavailable
from app.coordination.factory import reset_coordination_for_tests


@pytest.fixture(autouse=True)
def _reset_coordination():
    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


_USAGE = {
    "model": "claude-sonnet-5",
    "input_tokens": 10,
    "output_tokens": 2,
    "cache_read_tokens": 100,
    "cache_creation_tokens": 7,
}


def test_round_trip_accumulates_across_calls():
    add_turn_usage("s1", dict(_USAGE))
    add_turn_usage("s1", {"model": "claude-sonnet-5", "input_tokens": 5, "output_tokens": 3})
    assert drain_turn_usage("s1") == {
        "input_tokens": 15,
        "output_tokens": 5,
        "cache_read_tokens": 100,
        "cache_creation_tokens": 7,
        "model": "claude-sonnet-5",
    }


def test_drain_is_destructive():
    add_turn_usage("s1", dict(_USAGE))
    assert drain_turn_usage("s1") is not None
    assert drain_turn_usage("s1") is None


def test_empty_drain_returns_none():
    assert drain_turn_usage("never-seen") is None


def test_sessions_do_not_bleed_into_each_other():
    add_turn_usage("s1", dict(_USAGE))
    assert drain_turn_usage("s2") is None
    assert drain_turn_usage("s1") is not None


def test_model_is_last_writer():
    add_turn_usage("s1", {"model": "claude-haiku-4-5", "input_tokens": 1})
    add_turn_usage("s1", {"model": "claude-sonnet-5", "input_tokens": 1})
    drained = drain_turn_usage("s1")
    assert drained is not None and drained["model"] == "claude-sonnet-5"


def test_all_zero_usage_records_nothing():
    """Zero tokens is not a measurement worth a row (mirrors the manager's
    'storing zeros asserts a measurement nobody made' rule)."""
    add_turn_usage("s1", {"model": "m", "input_tokens": 0, "output_tokens": 0})
    assert drain_turn_usage("s1") is None


def test_unavailable_backend_never_raises(monkeypatch):
    class _Down:
        def __getattr__(self, name):
            raise CoordinationUnavailable("down")

    monkeypatch.setattr("app.chat.turn_usage.coordination", lambda: _Down())
    add_turn_usage("s1", dict(_USAGE))  # must not raise
    assert drain_turn_usage("s1") is None  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_turn_usage.py -q`
Expected: FAIL (collection error: `ModuleNotFoundError: app.chat.turn_usage`)

- [ ] **Step 3: Write the implementation**

Create `app/chat/turn_usage.py`:

```python
"""Broker-observed per-turn usage counters.

The engine provider emits assistant frames with no usage on them, yet every
LLM byte of an engine turn transits the broker, which parses the provider's
own usage block. This module is the seam between the two: the broker
accumulates each session-bound completion's usage here
(``app/api/broker.py``), and ChatManager drains the counters exactly once
per turn at the ``assistant_message`` persist seam to hydrate a usage-less
frame (``ChatManager._hydrate_frame_usage``).

Coordination-backed, because the broker replica that forwarded the call need
not be the gateway process holding the live chat session. The drain is
DESTRUCTIVE (``kv_delete`` — Redis ``GETDEL``, the in-memory backend's pop),
which is the design's safety property: no watermark state anywhere, so a
process restart or gateway takeover cannot double-count.

Precision, stated rather than implied: counters are conserved, not
turn-perfect — a completion landing exactly at the drain boundary attributes
to the adjacent turn; counters a session never drains again expire with the
TTL; a multi-model turn keeps the LAST completion's model. Both functions
swallow every failure: a measurement must never cost a turn or a forward.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.coordination.base import CoordinationUnavailable
from app.coordination.factory import coordination

logger = logging.getLogger(__name__)

#: Keys live only between two drains of one session — one turn — but a turn
#: can run tools for a long time, so the TTL is generous. It only
#: garbage-collects counters of sessions that never come back for another
#: turn (the memory backend's incr keeps the FIRST write's expiry).
_TTL_SECONDS = 24 * 3600

_COUNTER_KINDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
)


def _key(session_id: str, kind: str) -> str:
    return f"chat:turnusage:{session_id}:{kind}"


def add_turn_usage(session_id: str, usage: dict[str, Any]) -> None:
    """Accumulate one completion's usage onto ``session_id``'s turn counters.

    ``usage`` is ``parse_usage``'s normalized shape (the four token kinds +
    ``model``). Zero amounts are skipped, so an all-zero usage leaves no
    trace — a later drain of an untouched session stays ``None``. Never
    raises.
    """
    try:
        coord = coordination()
        for kind in _COUNTER_KINDS:
            amount = int(usage.get(kind) or 0)
            if amount:
                coord.incr(_key(session_id, kind), amount=amount, ttl_s=_TTL_SECONDS)
        model = usage.get("model")
        if model:
            coord.kv_set(_key(session_id, "model"), str(model), ttl_s=_TTL_SECONDS)
    except CoordinationUnavailable:
        logger.warning(
            "turn-usage counters unavailable; completion not attributed for session %s",
            session_id,
        )
    except Exception:
        logger.warning("turn-usage accumulate failed for session %s", session_id, exc_info=True)


def drain_turn_usage(session_id: str) -> Optional[dict[str, Any]]:
    """Atomically read-and-reset ``session_id``'s turn counters.

    Returns the four token kinds (ints) plus ``model`` (str | None), or
    ``None`` when no counter was recorded since the last drain — including
    when the backend is unavailable. Never raises.
    """
    totals: dict[str, Any] = {}
    any_counter = False
    try:
        coord = coordination()
        for kind in _COUNTER_KINDS:
            raw = coord.kv_delete(_key(session_id, kind))
            if raw is not None:
                any_counter = True
            totals[kind] = int(raw) if raw is not None else 0
        totals["model"] = coord.kv_delete(_key(session_id, "model"))
    except CoordinationUnavailable:
        logger.warning("turn-usage counters unavailable; turn not hydrated for session %s", session_id)
        return None
    except Exception:
        logger.warning("turn-usage drain failed for session %s", session_id, exc_info=True)
        return None
    if not any_counter:
        return None
    return totals
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_turn_usage.py -q`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add app/chat/turn_usage.py tests/test_turn_usage.py
git commit -m "Add coordination-backed per-session turn-usage counters"
```

---

### Task 2: Broker records every session-bound completion

**Files:**
- Modify: `app/api/broker.py` — the anthropic forward handler's two usage-recording sites (streaming `finally` mirror ~lines 1149–1199, buffered path ~lines 1224–1247; anchor on the code shown below, not the line numbers) and the import block.
- Test: `tests/test_broker_routes.py` (new section at the end)

**Interfaces:**
- Consumes: `add_turn_usage(session_id: str, usage: dict) -> None` from Task 1.
- Produces: counters accumulate for ANY 2xx completion whose ticket row carries a `session_id` — with or without a bound agent, on both the SSE and buffered paths, in every upstream mode. The `llm_usage` ledger (`usage_accumulator`) stays exactly as-is: agent-gated.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_broker_routes.py`:

```python
# ---------------------------------------------------------------------------
# Turn-usage counters: the broker accumulates provider-reported usage per
# chat session (app/chat/turn_usage.py) for EVERY session-bound completion —
# agent or no agent — so ChatManager can hydrate usage-less engine frames.
# The llm_usage ledger above stays agent-gated; these are a separate seam.
# ---------------------------------------------------------------------------


@pytest.fixture
def _fresh_turn_counters():
    from app.coordination.factory import reset_coordination_for_tests

    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


def _agentless_session_ticket():
    """A seeded user + chat session with NO bound agent + a broker ticket —
    the shape a Slack session without a channel binding (or any legacy
    session) presents to the broker."""
    tag = uuid.uuid4().hex[:8]
    email = f"broker_turns_{tag}@test.com"
    conn = get_system_db()
    UserRepository(conn).create(id=f"broker_turns_user_{tag}", email=email, name="Turns User")
    conn.close()
    session = chat_session_repo().create_session(user_email=email, surface=Surface.WEB)
    tok = ticket_repo().mint(session.id, "main", ttl_seconds=60)
    return session.id, tok


def test_agentless_completion_feeds_turn_counters(broker_app, e2e_env, _fresh_turn_counters, monkeypatch):
    """Buffered (JSON) path: an agent-less session's completion — which the
    llm_usage ledger ignores — still lands on the session's turn counters."""
    import json as _json

    import app.api.broker as broker_mod
    from app.chat.turn_usage import drain_turn_usage

    session_id, tok = _agentless_session_ticket()
    _StubResponseClient.status_code = 200
    _StubResponseClient.body = _json.dumps(
        {
            "id": "m1",
            "model": "claude-sonnet-5",
            "usage": {
                "input_tokens": 11,
                "output_tokens": 7,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 4,
            },
        }
    ).encode()
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _StubResponseClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-static")

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/anthropic/v1/messages",
                headers={"Authorization": f"Bearer {tok}"},
                json={"model": "claude-sonnet-5", "messages": []},
            )

    r = asyncio.run(_run())
    assert r.status_code == 200, r.text
    assert drain_turn_usage(session_id) == {
        "input_tokens": 11,
        "output_tokens": 7,
        "cache_read_tokens": 100,
        "cache_creation_tokens": 4,
        "model": "claude-sonnet-5",
    }


def test_agentless_sse_completion_feeds_turn_counters(broker_app, e2e_env, _fresh_turn_counters, monkeypatch):
    """Streaming path: the passthrough iterator's finally-block mirror must
    feed the counters too — for agent-less sessions it previously did not
    even collect the bytes."""
    import app.api.broker as broker_mod
    from app.chat.turn_usage import drain_turn_usage

    session_id, tok = _agentless_session_ticket()
    real_cls = httpx.AsyncClient

    class _SSEClient(_StreamShimMixin):
        def __init__(self, *a, **k):
            self._real = real_cls(*a, **k) if "transport" in k else None

        async def __aenter__(self):
            return await self._real.__aenter__() if self._real else self

        async def __aexit__(self, *a):
            return await self._real.__aexit__(*a) if self._real else False

        async def send(self, req, stream=False):
            class _R:
                status_code = 200
                headers = {"content-type": "text/event-stream"}

                async def aiter_bytes(self):
                    yield (
                        b"event: message_start\n"
                        b'data: {"type":"message_start","message":{"model":"claude-sonnet-5",'
                        b'"usage":{"input_tokens":11,"output_tokens":0}}}\n\n'
                    )
                    yield (b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":7}}\n\n')

                async def aclose(self):
                    pass

            return _R()

        def __getattr__(self, name):
            return getattr(self._real, name)

    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _SSEClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-static")

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/anthropic/v1/messages",
                headers={"Authorization": f"Bearer {tok}"},
                json={"model": "claude-sonnet-5", "messages": [], "stream": True},
            )

    r = asyncio.run(_run())
    assert r.status_code == 200, r.text

    drained = drain_turn_usage(session_id)
    assert drained is not None
    assert drained["input_tokens"] == 11
    assert drained["output_tokens"] == 7
    assert drained["model"] == "claude-sonnet-5"


def test_count_tokens_does_not_feed_turn_counters(broker_app, e2e_env, _fresh_turn_counters, monkeypatch):
    """count_tokens spends no tokens (is_completion is False) — even a
    response body that happens to carry a usage-shaped dict must not count."""
    import json as _json

    import app.api.broker as broker_mod
    from app.chat.turn_usage import drain_turn_usage

    session_id, tok = _agentless_session_ticket()
    _StubResponseClient.status_code = 200
    _StubResponseClient.body = _json.dumps({"usage": {"input_tokens": 999}}).encode()
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _StubResponseClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-static")

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/anthropic/v1/messages/count_tokens",
                headers={"Authorization": f"Bearer {tok}"},
                json={"model": "claude-sonnet-5", "messages": []},
            )

    r = asyncio.run(_run())
    assert r.status_code == 200, r.text
    assert drain_turn_usage(session_id) is None


def test_agent_session_feeds_both_ledger_and_turn_counters(
    broker_app, broker_agent_session, _fresh_turn_counters, monkeypatch
):
    """An agent-bound session keeps its llm_usage row AND accumulates turn
    counters — the manager later discards the counters when the frame carries
    its own usage (the native provider), so both consumers coexist."""
    import json as _json

    import app.api.broker as broker_mod
    from app.api.broker_agent_policy import usage_accumulator
    from app.chat.turn_usage import drain_turn_usage
    from src.repositories import llm_usage_repo

    ctx = broker_agent_session(model="claude-opus-4-7", token_budget_monthly=100_000)
    _StubResponseClient.status_code = 200
    _StubResponseClient.body = _json.dumps(
        {"id": "m1", "model": "claude-opus-4-7", "usage": {"input_tokens": 11, "output_tokens": 7}}
    ).encode()
    monkeypatch.setattr(broker_mod.httpx, "AsyncClient", _StubResponseClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-static")

    async def _run():
        transport = httpx.ASGITransport(app=broker_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(
                "/api/broker/anthropic/v1/messages",
                headers={"Authorization": f"Bearer {ctx['tok']}"},
                json={"model": "claude-opus-4-7", "messages": []},
            )

    r = asyncio.run(_run())
    assert r.status_code == 200, r.text

    usage_accumulator.flush()
    assert len(llm_usage_repo().list_for_agent(ctx["agent_id"])) == 1
    drained = drain_turn_usage(ctx["session_id"])
    assert drained is not None and drained["input_tokens"] == 11
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_broker_routes.py -q -k turn_counters`
Expected: the two agent-less tests and the agent-session test FAIL (drain returns `None` — the broker records nothing without the change); `count_tokens` may pass vacuously — that is fine, it pins the gate.

- [ ] **Step 3: Implement the broker changes**

In `app/api/broker.py`:

(a) Add the import next to the other `app.*` imports:

```python
from app.chat.turn_usage import add_turn_usage
```

(b) **Streaming site.** Replace

```python
        collect_usage = agent_row is not None and resp.status_code == 200
```

with

```python
        # Turn-usage counters (app/chat/turn_usage.py) are recorded for ANY
        # session-bound completion — the agent gate below is only for the
        # llm_usage budget ledger. Without this, an agent-less session's
        # bytes were never even mirrored for parsing.
        turn_session_id = row.get("session_id") if is_completion else None
        collect_usage = (agent_row is not None or turn_session_id is not None) and resp.status_code == 200
```

and inside the iterator's `finally`, replace the `usage = parse_usage(...)` consumer block

```python
                            usage = parse_usage(bytes(collected), ctype)
                            if usage:
                                usage_accumulator.add(
                                    {
                                        **usage,
                                        "id": str(uuid.uuid4()),
                                        "agent_id": agent_row["id"],
                                        "user_id": agent_row.get("owner_user_id"),
                                        "caller_user_id": caller_user_id,
                                        "session_id": row.get("session_id"),
                                    },
                                    budget_ttl_s=budget_ttl_s,
                                )
```

with

```python
                            usage = parse_usage(bytes(collected), ctype)
                            if usage and agent_row is not None:
                                usage_accumulator.add(
                                    {
                                        **usage,
                                        "id": str(uuid.uuid4()),
                                        "agent_id": agent_row["id"],
                                        "user_id": agent_row.get("owner_user_id"),
                                        "caller_user_id": caller_user_id,
                                        "session_id": row.get("session_id"),
                                    },
                                    budget_ttl_s=budget_ttl_s,
                                )
                            if usage and turn_session_id:
                                add_turn_usage(turn_session_id, usage)
```

Note: the overflow-skip `logger.warning` in that `finally` interpolates `agent_row.get("id")` — with the widened gate `agent_row` can now be `None` there. Change that message to log the ticket's session instead:

```python
                            logger.warning(
                                "SSE usage recording skipped for session %s: stream exceeded %d bytes",
                                row.get("session_id"),
                                _SSE_USAGE_COLLECT_MAX_BYTES,
                            )
```

(c) **Buffered site.** Replace

```python
    if agent_row is not None and resp.status_code == 200:
        try:
            usage = parse_usage(resp.content, resp.headers.get("content-type", ""))
            if usage:
                usage_accumulator.add(
```

with

```python
    turn_session_id = row.get("session_id") if is_completion else None
    if (agent_row is not None or turn_session_id is not None) and resp.status_code == 200:
        try:
            usage = parse_usage(resp.content, resp.headers.get("content-type", ""))
            if usage and turn_session_id:
                add_turn_usage(turn_session_id, usage)
            if usage and agent_row is not None:
                usage_accumulator.add(
```

(keep the existing `usage_accumulator.add({...})` payload and the surrounding `try/except` exactly as they are).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_broker_routes.py -q`
Expected: all pass — the four new tests AND the whole existing file (the agent-gated ledger tests must be untouched by the widened gate).

- [ ] **Step 5: Commit**

```bash
git add app/api/broker.py tests/test_broker_routes.py
git commit -m "Broker: feed per-session turn-usage counters for every session-bound completion"
```

---

### Task 3: Manager drains once per turn and hydrates usage-less frames

**Files:**
- Modify: `app/chat/manager.py` — new method `_hydrate_frame_usage` (place directly after `_record_turn_usage`, ~line 726), one call-site line in `_handle_frame` (before `self._repo.append_message(...)` in the `ftype == "assistant_message"` branch, ~line 2421), and the import block.
- Test: `tests/test_chat_usage_turns.py` (extend; narrow one existing test's docstring)

**Interfaces:**
- Consumes: `drain_turn_usage(session_id: str) -> dict | None` from Task 1 (keys `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `model`).
- Produces: a usage-less `assistant_message` frame gains `tokens_in` / `tokens_out` / `cache_read_tokens` / `cache_creation_tokens` / `model` before persist, so `append_message`, `_record_daily_tokens`, and `_record_turn_usage` all see them with zero changes of their own.

- [ ] **Step 1: Write the failing tests**

In `tests/test_chat_usage_turns.py`, add the import at the top:

```python
from app.chat.turn_usage import add_turn_usage, drain_turn_usage
```

and append these tests:

```python
_ENGINE_FRAME = {"type": "assistant_message", "content": "Hi"}  # engine provider: no usage fields at all


def test_engine_frame_is_hydrated_from_broker_counters(manager: ChatManager, monkeypatch):
    """The whole feature, end to end at the manager seam: a usage-less frame
    + broker-fed counters → one fully-populated usage_turns row, tokens on
    the persisted message (what max_session_tokens sums), and the daily
    spend counters fed (cache-write folded into the in-bucket)."""
    turns = _RecordingTurnsRepo()
    _use_postgres(monkeypatch, turns)

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        add_turn_usage(
            s.id,
            {
                "model": "claude-sonnet-5",
                "input_tokens": 11,
                "output_tokens": 22,
                "cache_read_tokens": 3333,
                "cache_creation_tokens": 44,
            },
        )
        live = _attach_live(manager, s.id, "u@x", FakeWS())
        await manager.send_user_message(s.id, "hello")
        await _pump_one_turn(manager, live, dict(_ENGINE_FRAME))

        assert len(turns.rows) == 1, f"expected one hydrated turn row, got {turns.rows}"
        row = turns.rows[0]
        assert row["model"] == "claude-sonnet-5"
        assert row["input_tokens"] == 11
        assert row["output_tokens"] == 22
        assert row["cache_read_tokens"] == 3333
        assert row["cache_creation_tokens"] == 44
        assert row["surface"] == Surface.WEB.value

        messages = manager._repo.list_messages(s.id)
        assistant = [m for m in messages if m.role == "assistant"][-1]
        assert assistant.tokens_in == 11 and assistant.tokens_out == 22
        assert assistant.model == "claude-sonnet-5"

        assert manager._daily_token_totals("u@x") == (11 + 44, 22)
        assert drain_turn_usage(s.id) is None, "hydration must consume the counters"

    asyncio.run(_run())


def test_frame_usage_wins_and_counters_are_discarded(manager: ChatManager, monkeypatch):
    """The double-count guard: native-sandbox calls ride the same broker
    route, so counters accumulate there too — but a frame that carries its
    own usage records the FRAME's numbers, and the drained counters are
    thrown away so they cannot leak into the next turn."""
    turns = _RecordingTurnsRepo()
    _use_postgres(monkeypatch, turns)

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        add_turn_usage(s.id, {"model": "some-other-model", "input_tokens": 999, "output_tokens": 999})
        live = _attach_live(manager, s.id, "u@x", FakeWS())
        await manager.send_user_message(s.id, "hello")
        await _pump_one_turn(manager, live, dict(_FULL_FRAME))

        assert len(turns.rows) == 1
        row = turns.rows[0]
        assert row["input_tokens"] == 11, "the frame's own numbers must win"
        assert row["model"] == "claude-sonnet-5"
        assert drain_turn_usage(s.id) is None, "discarded counters must not leak into the next turn"

    asyncio.run(_run())


def test_coordination_down_leaves_engine_frame_unrecorded(manager: ChatManager, monkeypatch):
    """Hydration is telemetry: with the coordination backend down, the turn
    behaves exactly as before this feature — message persisted, no row, no
    exception escaping the frame loop."""
    from app.coordination.base import CoordinationUnavailable

    turns = _RecordingTurnsRepo()
    _use_postgres(monkeypatch, turns)

    class _Down:
        def __getattr__(self, name):
            raise CoordinationUnavailable("down")

    monkeypatch.setattr("app.chat.turn_usage.coordination", lambda: _Down())

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_live(manager, s.id, "u@x", FakeWS())
        await manager.send_user_message(s.id, "hello")
        await _pump_one_turn(manager, live, dict(_ENGINE_FRAME))

        assert turns.rows == []
        messages = manager._repo.list_messages(s.id)
        assert [m.content for m in messages if m.role == "assistant"] == ["Hi"]
        assert not live.turn_in_flight

    asyncio.run(_run())
```

Also update the existing `test_frame_without_usage_records_nothing` docstring (behavior is unchanged — nothing brokered, nothing recorded — but the reason narrowed):

```python
def test_frame_without_usage_records_nothing(manager: ChatManager, monkeypatch):
    """A usage-less frame with NOTHING brokered either (no turn counters)
    still records nothing — zeros would state a measurement nobody made.
    (When the broker DID observe the turn, hydration fills the frame — see
    test_engine_frame_is_hydrated_from_broker_counters.)"""
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `.venv/bin/pytest tests/test_chat_usage_turns.py -q`
Expected: the three new tests FAIL (`turns.rows` empty / counters not drained); all existing tests still PASS.

- [ ] **Step 3: Implement hydration**

In `app/chat/manager.py`:

(a) Import, next to the other `app.*` imports:

```python
from app.chat.turn_usage import drain_turn_usage
```

(b) New method, placed directly after `_record_turn_usage`:

```python
    def _hydrate_frame_usage(self, live: LiveSession, frame: dict) -> None:
        """Fill a usage-less assistant frame from the broker-observed turn
        counters (``app/chat/turn_usage.py``).

        The engine provider emits frames with no usage at all, while every
        LLM call of the turn transited the broker, which accumulated the
        provider-reported usage per session. Draining ALWAYS (destructively)
        and hydrating only a frame with no usage of its own gives two
        properties at once: a frame that carries numbers wins (the native
        provider's self-report), and the discarded drain is the double-count
        guard — native-sandbox calls ride the same broker route, so their
        counters accumulate too and must not leak into a later turn.

        Runs BEFORE ``append_message``, so one hydration point feeds every
        existing consumer unchanged: the persisted ``chat_messages`` tokens
        (what ``max_session_tokens`` sums), the daily spend counters, and
        the ``usage_turns`` row. ``drain_turn_usage`` never raises; a
        coordination outage leaves the frame untouched and the turn records
        exactly as before this feature existed.
        """
        drained = drain_turn_usage(live.chat_id)
        if drained is None:
            return
        token_fields = ("tokens_in", "tokens_out", "cache_read_tokens", "cache_creation_tokens")
        if any(frame.get(field_name) is not None for field_name in token_fields):
            return  # the frame's own usage wins; drained counters are discarded
        frame["tokens_in"] = drained["input_tokens"]
        frame["tokens_out"] = drained["output_tokens"]
        frame["cache_read_tokens"] = drained["cache_read_tokens"]
        frame["cache_creation_tokens"] = drained["cache_creation_tokens"]
        if not frame.get("model") and drained.get("model"):
            frame["model"] = drained["model"]
```

(c) Call site — in `_handle_frame`, the `assistant_message` branch currently starts:

```python
            if ftype == "assistant_message":
                self._repo.append_message(
```

becomes:

```python
            if ftype == "assistant_message":
                # Broker-observed usage: drain once per turn, hydrate a
                # usage-less frame BEFORE persist so chat_messages, the
                # daily counters and usage_turns all see one set of numbers.
                self._hydrate_frame_usage(live, frame)
                self._repo.append_message(
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_chat_usage_turns.py tests/test_turn_usage.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add app/chat/manager.py tests/test_chat_usage_turns.py
git commit -m "Chat: hydrate usage-less assistant frames from broker turn counters"
```

---

### Task 4: Docs and CHANGELOG

**Files:**
- Modify: `app/chat/kai_engine_provider.py` (module docstring, the known-limitations paragraph ~lines 65–68)
- Modify: `docs/cloud-chat.md` (three places: the caps note ~line 196, the limitations bullet ~line 659, the comparison table row ~line 743)
- Modify: `CHANGELOG.md` (`## [Unreleased]`)

**Interfaces:**
- Consumes: the behavior shipped by Tasks 1–3 (nothing at the code level).
- Produces: docs that no longer state the retired limitation.

- [ ] **Step 1: Update the provider docstring**

In `app/chat/kai_engine_provider.py`, replace the first known-limitations clause

```
the engine does not surface token usage on its stream, so
``chat.daily_anthropic_spend_usd`` / ``chat.max_session_tokens`` do not meter
engine sessions (message-rate and concurrency caps still apply); agent
```

with

```
the engine does not surface token usage on its stream, but every engine LLM
call transits the broker, whose per-session turn counters
(``app/chat/turn_usage.py``) hydrate the usage-less assistant frame at
persist — so ``chat.daily_anthropic_spend_usd`` and
``chat.max_session_tokens`` DO meter engine sessions now, from
broker-observed (provider-reported) usage; agent
```

- [ ] **Step 2: Update `docs/cloud-chat.md`**

(a) The caps note (~line 196): replace

```
Token-derived caps (the spend cap and per-session token cap) are metered
on the docker provider only — the kai-agent engine's stream carries no
usage numbers, so engine sessions never trip them (see that provider's
limitations below). Message-rate and concurrency caps apply everywhere.
```

with

```
Token-derived caps (the spend cap and per-session token cap) are metered
on both providers. The kai-agent engine's stream carries no usage numbers,
so engine turns are metered from broker-observed usage instead: the broker
accumulates each session's provider-reported usage and the manager folds it
into the turn at persist (see the provider notes below). Message-rate and
concurrency caps apply everywhere.
```

(b) The limitations bullet (~line 659): replace the whole **"Token-derived caps are not metered."** bullet with

```
- **Token metering is broker-observed, not stream-reported.** The engine's
  stream still carries no usage numbers; Agnes meters engine sessions from
  the usage the broker itself observed while forwarding the turn's LLM
  calls. `chat.daily_anthropic_spend_usd` and `chat.max_session_tokens`
  apply, per-turn rows land in `usage_turns`, and the admin spend view
  counts engine sessions. Precision caveats: a completion finishing exactly
  at a turn boundary attributes to the adjacent turn (session totals are
  always conserved), and a multi-model turn is recorded under its last
  model. Transcripts of turns from before this feature still show
  **Model: — / Tokens: —**.
```

(c) The comparison table row (~line 743): replace

```
| Token spend metering (`daily_anthropic_spend_usd`, `max_session_tokens`) | not metered | metered |
```

with

```
| Token spend metering (`daily_anthropic_spend_usd`, `max_session_tokens`) | metered (broker-observed usage) | metered |
```

- [ ] **Step 3: CHANGELOG**

Under `## [Unreleased]`:

Under `### Added`:

```markdown
- **Per-turn token usage for engine-provider chat sessions.** The kai-agent engine emits assistant messages without usage, so engine instances recorded no `usage_turns` rows and no `chat_messages` tokens. The broker now accumulates the provider-reported usage of every session-bound completion it forwards (agent-bound or not — previously an agent-less session's usage was not collected at all), and the chat manager folds those counters into the turn at message persist. Engine turns now land in `usage_turns`, carry tokens+model on the message, and show up in `/me/activity` and the admin cost views.
```

Under `### Changed`:

```markdown
- Engine-provider chat sessions are now metered by `chat.daily_anthropic_spend_usd` and `chat.max_session_tokens` (from broker-observed usage). These caps previously never tripped on engine sessions because no usage reached the manager.
```

- [ ] **Step 4: Verify docs consistency**

Run: `grep -rn "do not meter\|not metered" app/chat/kai_engine_provider.py docs/cloud-chat.md`
Expected: no hits claiming engine sessions are unmetered.

- [ ] **Step 5: Commit**

```bash
git add app/chat/kai_engine_provider.py docs/cloud-chat.md CHANGELOG.md
git commit -m "Docs: engine sessions are metered from broker-observed usage"
```

---

### Final verification (integrate phase)

- [ ] `scripts/verify_syncmap.py`
- [ ] `.venv/bin/pytest tests/ connectors/ --lane impacted --tb=short -n auto -q`
- [ ] `.venv/bin/pytest tests/ connectors/ --lane fast --tb=short -n auto -q`
- [ ] `/agnes-review` on the unified diff
