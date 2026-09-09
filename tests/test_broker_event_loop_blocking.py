"""The broker's per-completion pre-forward work must not run on the event loop.

Investigation of issue #2246's *second* symptom: a sandbox-to-broker request
(``POST /api/broker/anthropic/v1/messages``) that the caller abandons after
~15 s under a concurrency ramp — 0% of cold turns at 5 and 10 simultaneous
users, 5% at 20, 7.5% at 40 — while the host stayed 83% idle and no container
came near its CPU cap. A failure rate that tracks *in-flight LLM calls* and
not CPU is the signature of a per-process serialisation, and the API process
runs a single uvicorn worker (``docker-compose.yml``, no ``--workers``).

``anthropic_proxy`` resolved the ticket's session/agent/caller rows
(``_agent_and_caller_for_ticket`` — up to three repo round-trips) and read the
monthly budget ledger (``cached_month_total`` — a coordination ``kv_get`` plus,
on a miss, an aggregate over ``llm_usage``) with SYNCHRONOUS calls made
directly on that event loop, before the upstream forward and therefore before
any response byte the caller is waiting on. The module already states the rule
80 lines earlier, for the ``llm``-scope session read it *does* offload:
"a synchronous DB read must not run on the event loop". These tests hold the
rest of the pre-forward path to the same rule.

The test *shape* matters, and it is why these are not happy-path assertions: a
single request cannot tell an on-loop repo call from an off-loop one — both
answer 403. Two CONCURRENT completions meeting at a ``threading.Barrier`` can.
The barrier only releases when both requests are inside the repo call at the
same moment, which is impossible while that call owns the only event loop.
Deterministic in both directions: it releases instantly when the work is
offloaded, and trips its timeout (never a sleep-and-hope) when it is not.

Both requests are pinned to a model the body does not ask for, so the 403
``model_not_allowed`` refusal lands *after* the two pre-forward reads and
*before* any upstream egress — no network, no credential, no fake transport.
"""

from __future__ import annotations

import asyncio
import threading
import uuid

import httpx
import pytest

from app.api import broker as broker_mod
from app.api import broker_agent_policy as policy_mod
from app.chat.types import Surface
from src.db import get_system_db
from src.repositories import agents_repo, chat_session_repo, ticket_repo
from src.repositories.users import UserRepository

#: Generous enough that a loaded CI worker still reaches the barrier from the
#: second request, short enough that the red state fails fast. Nothing sleeps
#: for this long on the green path — the barrier releases as soon as the second
#: caller arrives.
_BARRIER_TIMEOUT_S = 10.0


@pytest.fixture
def broker_app(e2e_env, shared_app):
    return shared_app


def _seed_agent_session(*, model: str, token_budget_monthly: int | None = None) -> str:
    """Seed a user + agent (pinned model, optional budget) + a chat session
    bound to that agent, and return a ``main``-scoped broker ticket for it —
    the same shape ``tests/test_broker_routes.py`` uses for a spawned sandbox.
    """
    tag = uuid.uuid4().hex[:8]
    email = f"broker_loop_{tag}@test.com"
    user_id = f"broker_loop_user_{tag}"

    conn = get_system_db()
    UserRepository(conn).create(id=user_id, email=email, name="Broker Loop User")
    conn.close()

    agent_id = str(uuid.uuid4())
    agents_repo().create(
        id=agent_id,
        owner_user_id=user_id,
        name="Broker Loop Agent",
        slug=f"broker-loop-{tag}",
        model=model,
        token_budget_monthly=token_budget_monthly,
    )
    session = chat_session_repo().create_session(user_email=email, surface=Surface.WEB, agent_id=agent_id)
    return ticket_repo().mint(session.id, "main", ttl_seconds=60)


def _gate_repo_method(monkeypatch, module, factory_name: str, method_name: str, barrier: threading.Barrier) -> None:
    """Make ``module.<factory_name>().<method_name>`` wait on ``barrier``.

    Wraps the real repository rather than faking it, so the surrounding
    handler logic (and the 403 it must still produce) is unchanged.
    """
    real_factory = getattr(module, factory_name)

    def _factory():
        inner = real_factory()

        class _Gated:
            def __getattr__(self, name):
                attr = getattr(inner, name)
                if name != method_name:
                    return attr

                def _wrapped(*args, **kwargs):
                    barrier.wait(timeout=_BARRIER_TIMEOUT_S)
                    return attr(*args, **kwargs)

                return _wrapped

        return _Gated()

    monkeypatch.setattr(module, factory_name, _factory)


def _contains_broken_barrier(exc: BaseException) -> bool:
    """True when ``exc`` is (or wraps) a tripped barrier.

    Starlette runs the handler inside an anyio task group, so the barrier's
    ``BrokenBarrierError`` surfaces wrapped in a ``BaseExceptionGroup``.
    """
    if isinstance(exc, threading.BrokenBarrierError):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_contains_broken_barrier(inner) for inner in exc.exceptions)
    for nested in (exc.__cause__, exc.__context__):
        if nested is not None and _contains_broken_barrier(nested):
            return True
    return False


def _two_concurrent_completions(app, tokens: list[str]) -> list[httpx.Response]:
    """POST a completion through the broker on each ticket, concurrently."""

    async def _one(token: str) -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.post(
                "/api/broker/anthropic/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                # A model the agent is NOT pinned to: refused with 403
                # `model_not_allowed` after the pre-forward reads, before any
                # upstream forward — so this test never egresses.
                json={"model": "some-other-vendor-model", "messages": []},
            )

    async def _both():
        return await asyncio.gather(*(_one(t) for t in tokens))

    return asyncio.run(_both())


def test_agent_resolution_does_not_hold_the_event_loop(broker_app, monkeypatch):
    """Two in-flight completions must be able to resolve their agent rows at
    the same time — ``_agent_and_caller_for_ticket``'s repo reads run off the
    event loop (issue #2246, second symptom)."""
    tokens = [
        _seed_agent_session(model="claude-opus-4-7"),
        _seed_agent_session(model="claude-opus-4-7"),
    ]
    barrier = threading.Barrier(2)
    _gate_repo_method(monkeypatch, broker_mod, "agents_repo", "get_by_id", barrier)

    try:
        responses = _two_concurrent_completions(broker_app, tokens)
    except BaseException as exc:
        if not _contains_broken_barrier(exc):
            raise
        pytest.fail(
            "two concurrent completions never overlapped inside "
            "agents_repo().get_by_id — anthropic_proxy resolves the ticket's "
            "agent/caller rows synchronously on the event loop, so in-flight "
            "LLM calls serialise on it (issue #2246)"
        )

    assert [r.status_code for r in responses] == [403, 403], [r.text for r in responses]


def test_budget_ledger_read_does_not_hold_the_event_loop(broker_app, monkeypatch):
    """Same rule for the monthly-budget read: two in-flight completions on
    budgeted agents must be able to consult the ledger at the same time."""
    tokens = [
        _seed_agent_session(model="claude-opus-4-7", token_budget_monthly=1_000_000),
        _seed_agent_session(model="claude-opus-4-7", token_budget_monthly=1_000_000),
    ]
    barrier = threading.Barrier(2)
    _gate_repo_method(monkeypatch, policy_mod, "llm_usage_repo", "month_total_tokens", barrier)

    try:
        responses = _two_concurrent_completions(broker_app, tokens)
    except BaseException as exc:
        if not _contains_broken_barrier(exc):
            raise
        pytest.fail(
            "two concurrent completions never overlapped inside "
            "llm_usage_repo().month_total_tokens — anthropic_proxy reads the "
            "monthly budget ledger synchronously on the event loop "
            "(issue #2246)"
        )

    assert [r.status_code for r in responses] == [403, 403], [r.text for r in responses]


def test_llm_ticket_reads_the_session_row_once_per_completion(broker_app, monkeypatch):
    """An ``llm``-scoped ticket (the embedded engine's LLM-only egress ticket,
    the caller in issue #2246) already has its session row in hand from the
    scope check — the pre-forward agent resolution must reuse it instead of
    paying a second round-trip for the same row on every completion."""
    tag = uuid.uuid4().hex[:8]
    email = f"broker_loop_llm_{tag}@test.com"
    user_id = f"broker_loop_llm_user_{tag}"
    conn = get_system_db()
    UserRepository(conn).create(id=user_id, email=email, name="Broker Loop LLM User")
    conn.close()
    session = chat_session_repo().create_session(user_email=email, surface=Surface.WEB)
    token = ticket_repo().mint(session.id, "llm", ttl_seconds=60)

    calls: list[str] = []
    real_factory = broker_mod.chat_session_repo

    def _counting_factory():
        inner = real_factory()

        class _Counting:
            def __getattr__(self, name):
                attr = getattr(inner, name)
                if name != "get_session":
                    return attr

                def _wrapped(session_id, *args, **kwargs):
                    calls.append(session_id)
                    return attr(session_id, *args, **kwargs)

                return _wrapped

        return _Counting()

    monkeypatch.setattr(broker_mod, "chat_session_repo", _counting_factory)
    # No upstream forward: an unpinned session still reaches the forward, so
    # aim the pinned upstream at a closed port — the broker's own
    # TransportError branch answers a typed 503 without leaving the host.
    monkeypatch.setattr(broker_mod, "_ANTHROPIC_BASE_URL", "http://127.0.0.1:1")

    responses = _two_concurrent_completions(broker_app, [token])
    assert responses[0].status_code == 503, responses[0].text
    assert calls == [session.id], (
        f"the session row was read {len(calls)} times for one completion — the "
        "llm-scope check and the pre-forward agent resolution are each paying "
        "their own round-trip for the same row (issue #2246)"
    )
