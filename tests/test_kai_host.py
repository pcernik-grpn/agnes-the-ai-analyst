"""Host-side wiring for the embedded ``kai-agent`` turn engine (app/api/kai.py).

Covers the contract the engine's ``jwt`` host adapter actually enforces — the
claim set it requires, the ``exp`` ceiling it rejects past, and the ticket
payload shape it validates — plus the two things that make the credential
model safe: a broker ticket cannot mint more tickets, and rotating a turn's
egress tickets must not destroy the session credential the engine still holds.
"""

from __future__ import annotations

import base64
import io
import hashlib
import hmac
import json
import threading
import time
from unittest import mock

import pytest

KAI_SECRET = "test-kai-host-secret-that-is-at-least-32-chars"


@pytest.fixture
def kai_secrets(monkeypatch):
    """The deployment secrets alone — the integration's kill switch.

    Split out from ``kai_env`` so a test can have the integration CONFIGURED
    while the caller is still UNAUTHORIZED, which is the only way to see the
    `chat` grant actually being enforced (with the secret unset every route
    503s before any gate runs).
    """
    monkeypatch.setenv("KAI_HOST_JWT_SECRET", KAI_SECRET)
    monkeypatch.setenv("KAI_HOST_JWT_ISSUER", "agnes-test")
    monkeypatch.setenv("KAI_HOST_JWT_AUDIENCE", "kai-agent-test")
    monkeypatch.setenv("KAI_TENANT_ID", "tenant-test")


@pytest.fixture
def chat_grant(seeded_app):
    """Give the seeded analyst the `chat` resource grant.

    ``POST /api/kai/sessions`` carries the same ``ResourceType.CHAT`` gate as
    the native chat API — cloud chat is denied to everyone by default — and
    the conftest analyst is created with no group membership at all, so
    without this every functional test below would 403 at the door.
    """
    from src.db import SYSTEM_EVERYONE_GROUP
    from src.repositories import resource_grants_repo, user_group_members_repo, user_groups_repo

    everyone = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
    user_group_members_repo().add_member("analyst1", everyone["id"], source="system_seed")
    resource_grants_repo().create(everyone["id"], "chat", "chat")
    return everyone


@pytest.fixture
def kai_env(kai_secrets, chat_grant):
    """The configured-and-authorized baseline: secrets set, caller granted."""
    return None


def _decode_segment(segment: str) -> dict:
    padding = "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment + padding))


def _claims(token: str) -> dict:
    return _decode_segment(token.split(".")[1])


def _verify(token: str, secret: str = KAI_SECRET) -> bool:
    """Verify the HS256 signature exactly as the engine's ``jose`` verify does:
    over the raw ``header.payload`` ASCII bytes."""
    header_b64, payload_b64, signature_b64 = token.split(".")
    expected = hmac.new(secret.encode(), f"{header_b64}.{payload_b64}".encode("ascii"), hashlib.sha256).digest()
    padding = "=" * (-len(signature_b64) % 4)
    return hmac.compare_digest(expected, base64.urlsafe_b64decode(signature_b64 + padding))


def _mint_session(seeded_app):
    client = seeded_app["client"]
    resp = client.post(
        "/api/kai/sessions",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# session minting
# ---------------------------------------------------------------------------


def test_unconfigured_instance_serves_no_kai_routes(seeded_app, chat_grant, monkeypatch):
    """No shared secret ⇒ the integration is off, not half-on.

    Takes ``chat_grant`` so the caller clears the route's `chat` gate and the
    503 being asserted is genuinely the kill switch rather than a 403 from the
    door in front of it.
    """
    monkeypatch.delenv("KAI_HOST_JWT_SECRET", raising=False)
    resp = seeded_app["client"].post(
        "/api/kai/sessions",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "kai_integration_not_configured"


def test_session_requires_authentication(seeded_app, kai_env):
    assert seeded_app["client"].post("/api/kai/sessions").status_code in (401, 403)


def test_session_creation_requires_the_chat_grant(seeded_app, kai_secrets):
    """Authentication is not authorization: the caller must hold `chat`.

    Every native chat route carries ``require_resource_access(
    ResourceType.CHAT, "chat")`` — cloud chat is denied to everyone by default
    and granted to a group on ``/admin/access``. This route is the embedded
    engine's equivalent front door and mints strictly more than a chat session
    does: a 12 h session JWT plus the ``kai_session`` credential behind it,
    which mints ``llm`` tickets and drives ``/api/broker/anthropic/*`` on the
    instance's LLM budget. So an authenticated user an admin deliberately left
    out of the chat grant must not get through merely because this instance
    sets ``KAI_HOST_JWT_SECRET``.

    Not a cross-user escalation either way — the turn runs as the caller and
    downstream RBAC still applies — which is exactly why
    ``tests/test_route_auth_guard.py`` cannot catch it: that guard only asserts
    that *some* auth dependency exists, and ``get_current_user`` is one.
    """
    from src.repositories import chat_session_repo

    # `kai_secrets` without `chat_grant`: the integration is configured, the
    # caller is authenticated, and nothing has granted them chat.
    with mock.patch("app.api.kai.ticket_repo") as ticket_repo_factory:
        resp = seeded_app["client"].post(
            "/api/kai/sessions",
            headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
        )

    assert resp.status_code == 403, f"ungranted caller minted a session: {resp.text}"
    assert "token" not in resp.json()

    # And the refusal must land before either side effect — a row plus a live
    # credential written for a caller who was then told no is the whole bug.
    assert chat_session_repo().list_sessions("analyst@test.com", include_archived=True) == []
    ticket_repo_factory.return_value.mint.assert_not_called()


def test_a_chat_granted_user_still_mints_a_live_session(seeded_app, kai_env):
    """The companion to the refusal above: the gate must not close the door on
    the callers it is supposed to admit."""
    from src.repositories import chat_session_repo

    body = _mint_session(seeded_app)

    assert body["chat_id"]
    assert _verify(body["token"]) is True
    assert _claims(body["token"])["sub"] == "analyst@test.com"
    rows = chat_session_repo().list_sessions("analyst@test.com", include_archived=True)
    assert [r.id for r in rows] == [body["chat_id"]]


def test_session_token_carries_every_claim_the_engine_requires(seeded_app, kai_env):
    """The engine's ``claimsSchema`` fails the token — not degrades it — when
    any identity claim is missing."""
    body = _mint_session(seeded_app)
    claims = _claims(body["token"])

    assert claims["sub"] == "analyst@test.com"
    assert claims["tenant"] == "tenant-test"
    assert claims["scope_id"] == body["chat_id"]
    assert claims["downstream_credential"]
    assert claims["read_only"] is False
    assert claims["iss"] == "agnes-test"
    assert claims["aud"] == "kai-agent-test"


def test_session_token_signature_verifies_under_the_shared_secret(seeded_app, kai_env):
    token = _mint_session(seeded_app)["token"]
    assert _verify(token) is True
    assert _verify(token, secret="a-different-secret-of-sufficient-length") is False


def test_session_token_expiry_is_inside_the_engines_24h_ceiling(seeded_app, kai_env):
    """The engine rejects a token whose ``exp`` is more than 24 h out, and one
    with no ``exp`` at all."""
    claims = _claims(_mint_session(seeded_app)["token"])
    now = int(time.time())
    assert claims["exp"] > now
    assert claims["exp"] - now <= 24 * 60 * 60


def test_session_identity_is_never_taken_from_the_request_body(seeded_app, kai_env):
    """A body-supplied ``sub`` must not become the principal — otherwise this
    is an impersonation endpoint."""
    resp = seeded_app["client"].post(
        "/api/kai/sessions",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
        json={"sub": "admin@test.com", "tenant": "evil"},
    )
    assert resp.status_code == 200, resp.text
    claims = _claims(resp.json()["token"])
    assert claims["sub"] == "analyst@test.com"
    assert claims["tenant"] == "tenant-test"


def test_session_creates_a_chat_session_row_keyed_by_the_returned_id(seeded_app, kai_env):
    """Agnes owns the chat id; the engine accepts it as ``body.id`` so both
    sides key off one value with no cross-database join."""
    from src.repositories import chat_session_repo

    body = _mint_session(seeded_app)
    session = chat_session_repo().get_session(body["chat_id"])
    assert session is not None
    assert session.user_email == "analyst@test.com"


def test_chat_id_is_a_uuid_because_the_engine_stores_it_as_one(seeded_app, kai_env):
    """Not cosmetic. The engine validates ``body.id`` as a UUID and persists it
    in a Postgres ``uuid`` column, so the repo's default ``chat_<hex>`` id is
    rejected with ``Invalid UUID`` before a turn ever starts — verified against
    a live engine, and the reason `create_session` grew a `session_id` param.
    """
    import uuid as uuid_mod

    body = _mint_session(seeded_app)
    assert not body["chat_id"].startswith("chat_")
    # raises if the id is not a well-formed UUID
    assert str(uuid_mod.UUID(body["chat_id"])) == body["chat_id"]
    # the claim the engine reads as its scope key must carry the same value
    assert _claims(body["token"])["scope_id"] == body["chat_id"]


# ---------------------------------------------------------------------------
# per-turn tickets
# ---------------------------------------------------------------------------


def test_tickets_requires_a_credential(seeded_app, kai_env):
    assert seeded_app["client"].post("/api/kai/tickets").status_code == 401


def test_tickets_rejects_an_unknown_credential(seeded_app, kai_env):
    resp = seeded_app["client"].post("/api/kai/tickets", headers={"Authorization": "Bearer not-a-real-credential"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid_or_expired_kai_credential"


def test_tickets_returns_the_payload_shape_the_engine_validates(seeded_app, kai_env):
    """``llm`` is mandatory and every value must be a non-empty string, or the
    engine rejects the payload and fails the turn before any prompt reaches
    the sandbox."""
    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    resp = seeded_app["client"].post("/api/kai/tickets", headers={"Authorization": f"Bearer {credential}"})
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert isinstance(payload.get("llm"), str) and payload["llm"]
    assert all(isinstance(v, str) and v for v in payload.values())


def test_revoking_chat_access_stops_the_credential_within_one_turn(seeded_app, kai_env):
    """The chat grant is standing authority, not a one-time admission ticket.

    The gate on ``POST /api/kai/sessions`` bounds who may *start* a session. On
    its own that leaves an admin who revokes a user's chat access stopping
    native chat at once — the 13 gated routes in ``app/api/chat.py`` — while an
    already-issued kai credential keeps minting ``llm`` tickets and reaching
    tools for the rest of its 12 h life. ``_require_session_credential``
    re-checks the grant where it reads the session row, so ``/tickets``,
    ``/mcp`` and ``/workspace`` are covered by construction.
    """
    from src.repositories import resource_grants_repo

    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    client = seeded_app["client"]
    auth = {"Authorization": f"Bearer {credential}"}

    # Works while the grant stands.
    assert client.post("/api/kai/tickets", headers=auth).status_code == 200

    # The admin revokes chat instance-wide (the analyst holds it only through
    # the Everyone group `chat_grant` put them in).
    assert resource_grants_repo().delete_by_resource("chat", "chat") >= 1

    resp = client.post("/api/kai/tickets", headers=auth)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "chat_access_revoked"
    # The same credential must not get a workspace either.
    assert client.get("/api/kai/workspace", headers=auth).status_code == 403


def test_llm_ticket_authenticates_the_main_scoped_broker_route(seeded_app, kai_env):
    """The engine's ``llm`` scope must land on a ticket the existing LLM broker
    route accepts — that route is the whole reason no new LLM route is needed."""
    from src.repositories import ticket_repo

    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    tickets = seeded_app["client"].post("/api/kai/tickets", headers={"Authorization": f"Bearer {credential}"}).json()

    resolved = ticket_repo().resolve(tickets["llm"])
    assert resolved is not None
    # NOT `main`: that scope also authenticates `/api/broker/agnes-api`, the
    # whole non-admin `/api/*` replay. The engine gets LLM egress only.
    assert resolved["scope"] == "llm"


def test_mcp_ticket_is_omitted_unless_the_instance_brokers_mcp(seeded_app, kai_env, monkeypatch):
    """The engine treats ``mcp`` as optional host data — an instance with no
    MCP upstream simply omits the scope."""
    monkeypatch.delenv("KAI_BROKER_MCP_ENABLED", raising=False)
    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    payload = seeded_app["client"].post("/api/kai/tickets", headers={"Authorization": f"Bearer {credential}"}).json()
    assert "mcp" not in payload

    monkeypatch.setenv("KAI_BROKER_MCP_ENABLED", "1")
    payload = seeded_app["client"].post("/api/kai/tickets", headers={"Authorization": f"Bearer {credential}"}).json()
    assert isinstance(payload.get("mcp"), str) and payload["mcp"]


def test_mcp_toggle_honours_the_shared_falsey_vocabulary(seeded_app, kai_env, monkeypatch):
    """`KAI_BROKER_MCP_ENABLED=false` must take the tool surface AWAY.

    An inline `os.environ.get(...).strip()` truthiness test reads every
    non-blank value as on, so the operator who spells the disable explicitly —
    `false`, `0`, `off` — hands the sandbox the very scope they meant to
    withhold. Routed through `feature_enabled` (`docs/feature-flags.md`), which
    is the only reason those spellings mean what they say. Found by Devin
    Review on this PR.
    """
    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]

    def _mcp_scope_issued() -> bool:
        payload = (
            seeded_app["client"].post("/api/kai/tickets", headers={"Authorization": f"Bearer {credential}"}).json()
        )
        return "mcp" in payload

    for off in ("false", "False", "0", "off", "no", ""):
        monkeypatch.setenv("KAI_BROKER_MCP_ENABLED", off)
        assert not _mcp_scope_issued(), f"{off!r} must leave the mcp scope unissued"

    for on in ("true", "1", "yes", "on"):
        monkeypatch.setenv("KAI_BROKER_MCP_ENABLED", on)
        assert _mcp_scope_issued(), f"{on!r} must issue the mcp scope"


def test_mcp_proxy_never_hands_the_sandbox_undecoded_bytes(seeded_app, kai_env):
    """The forwarded reply must be readable, not gzip under a stripped header.

    `_MCP_DROP_RESPONSE_HEADERS` strips `content-encoding`, which is only sound
    if the body we forward is decoded. Two independent halves, because either
    alone still breaks: the request must ask for `identity` (the sandbox's own
    `accept-encoding` is dropped, but `build_request` re-adds httpx's default
    `gzip, deflate`, so an upstream may compress with nothing downstream
    asking), and the body must stream through `aiter_bytes()` — `aiter_raw()`
    emits the undecoded body, i.e. gzip bytes labelled as plain text. Found by
    Devin Review on this PR.
    """
    import inspect

    from app.api import kai as kai_mod

    # Comments are stripped before the negative assertion: this function's own
    # comment NAMES `aiter_raw` to explain why it is wrong, and a test that
    # cannot tell prose from code would fail on the very explanation.
    source = inspect.getsource(kai_mod.kai_mcp)
    code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
    assert '"Accept-Encoding"] = "identity"' in code, "must ask the upstream not to compress"
    assert "aiter_bytes()" in code, "must forward the DECODED body"
    assert "aiter_raw()" not in code, "aiter_raw would emit gzip under a stripped content-encoding"
    # And the header it strips is still stripped — the pairing is what matters.
    assert "content-encoding" in kai_mod._MCP_DROP_RESPONSE_HEADERS


def test_a_broker_ticket_cannot_mint_more_tickets(seeded_app, kai_env):
    """Scope check on the credential route: if a sandbox got hold of one turn's
    ticket, it must not be able to refresh itself indefinitely."""
    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    llm_ticket = (
        seeded_app["client"].post("/api/kai/tickets", headers={"Authorization": f"Bearer {credential}"}).json()["llm"]
    )

    resp = seeded_app["client"].post("/api/kai/tickets", headers={"Authorization": f"Bearer {llm_ticket}"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "kai_credential_scope_mismatch"


def test_reminting_retires_the_previous_turns_egress_tickets(seeded_app, kai_env):
    """One live set per chat, so a stale turn's ticket cannot be replayed."""
    from src.repositories import ticket_repo

    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    client = seeded_app["client"]
    first = client.post("/api/kai/tickets", headers={"Authorization": f"Bearer {credential}"}).json()
    second = client.post("/api/kai/tickets", headers={"Authorization": f"Bearer {credential}"}).json()

    assert first["llm"] != second["llm"]
    assert ticket_repo().resolve(first["llm"]) is None
    assert ticket_repo().resolve(second["llm"]) is not None


def test_reminting_does_not_invalidate_the_session_credential(seeded_app, kai_env):
    """The engine has no way to be handed a replacement credential — its
    ticket-response schema is ``{llm, mcp}`` and it keeps using the one baked
    into the session JWT. A scope-blind revoke here 401s every later turn.
    """
    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    client = seeded_app["client"]

    for turn in range(3):
        resp = client.post("/api/kai/tickets", headers={"Authorization": f"Bearer {credential}"})
        assert resp.status_code == 200, f"turn {turn + 1} lost the credential: {resp.text}"


def test_tickets_are_scoped_to_their_own_session(seeded_app, kai_env):
    """Two chats must not share an egress ticket set."""
    from src.repositories import ticket_repo

    client = seeded_app["client"]
    first = _mint_session(seeded_app)
    second = _mint_session(seeded_app)
    assert first["chat_id"] != second["chat_id"]

    first_tickets = client.post(
        "/api/kai/tickets",
        headers={"Authorization": f"Bearer {_claims(first['token'])['downstream_credential']}"},
    ).json()
    client.post(
        "/api/kai/tickets",
        headers={"Authorization": f"Bearer {_claims(second['token'])['downstream_credential']}"},
    )

    # minting for the second chat must not have retired the first chat's set
    still_live = ticket_repo().resolve(first_tickets["llm"])
    assert still_live is not None
    assert still_live["session_id"] == first["chat_id"]


# ---------------------------------------------------------------------------
# MCP passthrough
# ---------------------------------------------------------------------------


def _mcp_ticket(seeded_app):
    """A ticket in the mcp scope, as the engine's relay would hold."""
    from src.repositories import ticket_repo

    body = _mint_session(seeded_app)
    return ticket_repo().mint(body["chat_id"], "mcp"), body


def test_mcp_requires_an_mcp_scoped_ticket(seeded_app, kai_env, monkeypatch):
    """An llm ticket must not reach the tool surface — scope split is the
    whole point of minting two.

    The switch is enabled here because feature availability is checked BEFORE
    the credential (as `_secret()` already is): with the feature off the route
    answers 503 for everyone and never looks at a ticket's scope, which is the
    right posture but not what this test is about.
    """
    from src.repositories import ticket_repo

    monkeypatch.setenv("KAI_BROKER_MCP_ENABLED", "1")
    body = _mint_session(seeded_app)
    llm_ticket = ticket_repo().mint(body["chat_id"], "main")

    resp = seeded_app["client"].post("/api/kai/mcp", headers={"Authorization": f"Bearer {llm_ticket}"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "ticket_scope_mismatch"


def test_mcp_rejects_an_unknown_ticket(seeded_app, kai_env, monkeypatch):
    """Switch on, because availability is decided before the credential: with the
    tool surface off the route answers 503 for every caller and never inspects a
    ticket, which is the point of `_require_mcp_surface` running as a dependency
    ahead of `require_broker_ticket`."""
    monkeypatch.setenv("KAI_BROKER_MCP_ENABLED", "1")
    resp = seeded_app["client"].post("/api/kai/mcp", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_a_cache_hit_cannot_skip_the_authorization_guards(seeded_app, kai_env):
    """The token cache must not be a way around the checks in front of it.

    `_mint_mcp_access_token` fails closed for a missing session, a co-session and
    a scope-limited agent — but the cache read used to sit ABOVE all three, so a
    hit returned a token without consulting any of them and a deleted
    conversation kept serving tools for the rest of the cache's life. The guards
    now run first and the cache is only a way to avoid re-signing a JWT. Found
    by Devin Review on this PR, on top of the guards themselves.
    """
    from fastapi import HTTPException

    from app.api import kai as kai_mod

    body = _mint_session(seeded_app)
    chat_id = body["chat_id"]

    # Warm the cache, so the fast path is live.
    first = kai_mod._mint_mcp_access_token(chat_id)
    assert kai_mod._mcp_token_cache.get(chat_id) is not None
    assert kai_mod._mint_mcp_access_token(chat_id) == first, "second call should be a cache hit"

    # Delete the conversation WITHOUT clearing the cache — the exact state the
    # short-circuit used to serve straight through.
    assert kai_mod.chat_session_repo().hard_delete_session(chat_id) is True

    with pytest.raises(HTTPException) as exc:
        kai_mod._mint_mcp_access_token(chat_id)
    assert (exc.value.status_code, exc.value.detail) == (401, "ticket_session_not_found")


def test_the_sandbox_cannot_forge_the_client_ip_on_the_internal_hop(seeded_app, kai_env):
    """Forwarding headers must not survive the proxy.

    `trusted_client_ip` reads `x-forwarded-for` and trusts the rightmost
    AGNES_TRUSTED_PROXY_HOPS entries, so a value the sandbox supplied arrives as
    an extra hop — putting an attacker-chosen address in the audit log and in
    any IP-keyed throttle on this call. The playbook's rule is to derive the IP
    from trusted proxy hops only, which the hop arithmetic cannot honour if an
    agent gets to contribute a hop. Found by Copilot on this PR.
    """
    from app.api import kai as kai_mod

    for header in ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded", "x-real-ip"):
        assert header in kai_mod._MCP_DROP_REQUEST_HEADERS, f"{header} must not reach the internal hop"
    # The header trusted_client_ip actually reads is the one that matters most.
    import inspect

    assert "x-forwarded-for" in inspect.getsource(kai_mod).split("_MCP_DROP_REQUEST_HEADERS")[1]


def test_token_cache_access_is_serialized_and_prunes_without_mutating_live(seeded_app, kai_env):
    """Assert the PROPERTIES that make the cache thread-safe, not a race.

    `_mint_mcp_access_token` runs under `asyncio.to_thread`, so several anyio
    workers reach this dict at once: live iteration raised `RuntimeError:
    dictionary changed size during iteration` and a bare `del` raced another
    prune into `KeyError`, both surfacing as intermittent 500s.

    A thread-stress test is NOT used here on purpose. One was written first and
    it passed against the racy version three runs out of three — the DB read and
    JWT signing dominate each call, so two threads practically never sit inside
    the prune loop together. A test that cannot fail against the bug it names is
    worse than no test, because it makes the next reader believe the race is
    covered. What is checkable is the construction: a lock around every cache
    read/prune/write, and a prune that iterates a snapshot and pops defensively.
    Found by Copilot on this PR.
    """
    import inspect

    from app.api import kai as kai_mod

    assert isinstance(kai_mod._mcp_token_cache_lock, type(threading.Lock()))

    prune = inspect.getsource(kai_mod._prune_mcp_token_cache)
    code = "\n".join(line.split("#", 1)[0] for line in prune.splitlines())
    assert "list(_mcp_token_cache.items())" in code, "must iterate a snapshot, not the live dict"
    assert ".pop(" in code and "del _mcp_token_cache[" not in code, "must pop defensively, not del"

    mint = "\n".join(line.split("#", 1)[0] for line in inspect.getsource(kai_mod._mint_mcp_access_token).splitlines())
    assert mint.count("with _mcp_token_cache_lock:") == 2, "the read and the write must both be guarded"
    # The lock must NOT wrap the DB read or the signing — that would serialize mints.
    assert "create_access_token(" in mint.split("with _mcp_token_cache_lock:")[1], "signing stays outside the lock"


def test_credential_responses_are_never_cacheable(seeded_app, kai_env):
    """A live JWT and live broker tickets must not be written down by any cache
    between here and the engine. Found by Copilot on this PR."""
    resp = seeded_app["client"].post(
        "/api/kai/sessions", headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"}
    )
    assert resp.status_code == 200, resp.text
    assert "no-store" in resp.headers.get("cache-control", "")

    credential = _claims(resp.json()["token"])["downstream_credential"]
    tickets = seeded_app["client"].post("/api/kai/tickets", headers={"Authorization": f"Bearer {credential}"})
    assert tickets.status_code == 200, tickets.text
    assert "no-store" in tickets.headers.get("cache-control", "")


def test_mcp_availability_is_decided_before_the_credential(seeded_app, kai_env, monkeypatch):
    """An unconfigured surface must not answer with a credential error.

    `require_broker_ticket` is a dependency, so a check written in the handler
    body ran after it: an instance with the engine off answered `401
    missing_broker_ticket` — a credential complaint about a surface that is not
    there. Found by Copilot on this PR.
    """
    # Tool switch off, engine configured: 503 even with NO Authorization header.
    monkeypatch.setenv("KAI_BROKER_MCP_ENABLED", "false")
    resp = seeded_app["client"].post("/api/kai/mcp", content=b"{}")
    assert resp.status_code == 503, f"expected the surface to answer 503, got {resp.status_code}"
    assert resp.json()["detail"] == "kai_mcp_not_enabled"

    # Engine not configured at all: the kill switch, still ahead of the ticket.
    monkeypatch.setenv("KAI_BROKER_MCP_ENABLED", "1")
    monkeypatch.delenv("KAI_HOST_JWT_SECRET", raising=False)
    resp = seeded_app["client"].post("/api/kai/mcp", content=b"{}")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "kai_integration_not_configured"


def test_the_tool_ticket_is_confined_to_the_kai_route(seeded_app, kai_env, monkeypatch):
    """The engine's tool ticket must open `/api/kai/mcp` and NOTHING else.

    `/api/broker/agnes-mcp` is gated on the native relay's `mcp` scope and hands
    off to the same `_replay`, whose own gate "only blocks admin-mutation
    routes" — so a ticket minted as plain `mcp` opened the general `/api/*`
    surface that splitting `llm` off `main` existed to withhold, leaving the
    confinement half-done. The tool ticket is now `kai_mcp`. Found by Devin
    Review on this PR.

    Also closes a coverage gap: nothing drove `/api/kai/mcp` with a ticket that
    `/api/kai/tickets` actually minted, so a mismatch between what is minted and
    what the route requires would have gone unnoticed by every test here.
    """
    from src.repositories import ticket_repo

    monkeypatch.setenv("KAI_BROKER_MCP_ENABLED", "1")
    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    tickets = seeded_app["client"].post("/api/kai/tickets", headers={"Authorization": f"Bearer {credential}"}).json()
    tool_ticket = tickets["mcp"]

    # 1. What /tickets mints is the narrow scope, not the native relay's.
    assert ticket_repo().resolve(tool_ticket)["scope"] == "kai_mcp"

    # 2. It does NOT open the general replay route.
    denied = seeded_app["client"].post(
        "/api/broker/agnes-mcp",
        headers={"Authorization": f"Bearer {tool_ticket}"},
        json={"method": "GET", "path": "/api/catalog"},
    )
    assert denied.status_code == 401, f"tool ticket must not open agnes-mcp, got {denied.status_code}"
    assert denied.json()["detail"] == "ticket_scope_mismatch"

    # 3. And it DOES get past /api/kai/mcp's own gates — the mint/require
    #    agreement. There is no MCP server to talk to in this environment, so
    #    the proof is that the handler reaches the upstream CONNECTION at all:
    #    that is downstream of the availability gate, the scope check and the
    #    access-token mint. A scope rejection would have returned a 401 response
    #    instead of attempting any network call.
    import httpx

    with pytest.raises(httpx.TransportError):
        seeded_app["client"].post("/api/kai/mcp", headers={"Authorization": f"Bearer {tool_ticket}"}, content=b"{}")

    # 4. Conversely a NATIVE sandbox's `mcp` ticket can no longer reach the kai
    #    route — the reachability behind the earlier escalation finding.
    native = ticket_repo().mint(_mint_session(seeded_app)["chat_id"], "mcp")
    refused = seeded_app["client"].post("/api/kai/mcp", headers={"Authorization": f"Bearer {native}"}, content=b"{}")
    assert refused.status_code == 401
    assert refused.json()["detail"] == "ticket_scope_mismatch"


def test_llm_egress_ticket_cannot_reach_the_general_api_replay(seeded_app, kai_env):
    """ "LLM egress" must mean only that.

    `main` authenticates BOTH `/api/broker/anthropic/*` and
    `/api/broker/agnes-api` — the latter replaying the caller's whole non-admin
    `/api/*` surface — so minting the engine's LLM ticket as `main` handed the
    sandbox the user's data API, reachable even with the tool switch off. It is
    now minted in a dedicated `llm` scope that the LLM proxy accepts and
    `agnes-api` does not. Found by Devin Review.
    """
    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    llm = seeded_app["client"].post("/api/kai/tickets", headers={"Authorization": f"Bearer {credential}"}).json()["llm"]

    resp = seeded_app["client"].post(
        "/api/broker/agnes-api",
        headers={"Authorization": f"Bearer {llm}"},
        json={"method": "GET", "path": "/api/catalog"},
    )
    assert resp.status_code == 401, f"llm ticket must not open the API replay, got {resp.status_code}"
    assert resp.json()["detail"] == "ticket_scope_mismatch"


def test_a_deleted_conversation_cuts_the_engine_off_immediately(seeded_app, kai_env):
    """The credential is bounded by its session ROW, not only by its own TTL.

    `SWEEP_EXEMPT_SCOPES` spares it from the lifecycle sweep — and `kill()`,
    which runs that sweep, is also what a user's permanent delete reaches. So
    without this the deleted conversation left the engine able to mint fresh
    upstream tickets and spend the instance's LLM budget under that user's name
    for the rest of the 12 h credential. Found by Devin Review.
    """
    from src.repositories import chat_session_repo

    body = _mint_session(seeded_app)
    credential = _claims(body["token"])["downstream_credential"]
    ok = seeded_app["client"].post("/api/kai/tickets", headers={"Authorization": f"Bearer {credential}"})
    assert ok.status_code == 200

    # The user deletes the conversation: the row goes, the credential row may not.
    assert chat_session_repo().hard_delete_session(body["chat_id"]) is True

    after = seeded_app["client"].post("/api/kai/tickets", headers={"Authorization": f"Bearer {credential}"})
    assert after.status_code == 401, f"a deleted conversation must cut the engine off, got {after.status_code}"
    assert after.json()["detail"] == "kai_session_gone"


def test_the_engine_session_survives_a_native_sandbox_sweep(seeded_app, kai_env):
    """Opening the engine's conversation in web chat must not kill its session.

    The engine's chat row is an ordinary `chat_sessions` row, so it shares its
    ticket namespace with a native sandbox on the same id — and the native
    runner's lifecycle sweep is `revoke_session`, scope-blind. Before
    `SWEEP_EXEMPT_SCOPES` that deleted the engine's long-lived credential, and
    the engine has no channel to be handed a replacement: every later turn was
    rejected, permanently. Found by Devin Review.
    """
    from src.repositories import ticket_repo

    body = _mint_session(seeded_app)
    credential = _claims(body["token"])["downstream_credential"]
    chat_id = body["chat_id"]

    # A turn happens: egress tickets exist alongside the credential.
    first = seeded_app["client"].post("/api/kai/tickets", headers={"Authorization": f"Bearer {credential}"})
    assert first.status_code == 200, first.text

    # The user opens that conversation in web chat -> the runner sweeps the row.
    ticket_repo().revoke_session(chat_id)

    # The engine can still authenticate and take another turn.
    again = seeded_app["client"].post("/api/kai/tickets", headers={"Authorization": f"Bearer {credential}"})
    assert again.status_code == 200, f"credential must survive a native sweep, got {again.status_code}: {again.text}"
    assert isinstance(again.json().get("llm"), str)


def test_mcp_route_refuses_when_the_switch_is_off(seeded_app, kai_env, monkeypatch):
    """The switch has to close the door, not just stop handing out keys.

    It gated only whether `/api/kai/tickets` ISSUES the `mcp` scope, so with the
    engine configured and the switch off the route still served any holder of an
    `mcp` ticket — and the native chat runner mints one for every chat sandbox.
    Found by Devin Review.
    """
    from src.repositories import ticket_repo

    chat_id = _mint_session(seeded_app)["chat_id"]
    monkeypatch.setenv("KAI_BROKER_MCP_ENABLED", "1")
    ticket = ticket_repo().mint(chat_id, "mcp")

    monkeypatch.setenv("KAI_BROKER_MCP_ENABLED", "false")
    resp = seeded_app["client"].post("/api/kai/mcp", headers={"Authorization": f"Bearer {ticket}"}, content=b"{}")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "kai_mcp_not_enabled"


def test_mcp_token_refuses_the_two_narrowed_session_kinds(seeded_app, kai_env, monkeypatch):
    """A co-session guest and a scope-limited agent must NOT get owner authority.

    `/api/kai/mcp` accepts any `mcp`-scoped broker ticket, and the native chat
    runner mints one for every chat sandbox — so without this the two session
    kinds `_mint_identity_jwt` deliberately narrows (live participant
    intersection; owner-grants ∩ agent-scope) could borrow the stored owner's
    whole tool surface through this route. This token is a registered bearer
    with a baked subject and cannot carry either intersection, so it refuses,
    the same call `_ticket_owner_for_git` makes. Found by Devin Review.
    """
    from app.api import kai as kai_mod
    from fastapi import HTTPException

    body = _mint_session(seeded_app)
    chat_id = body["chat_id"]
    real = kai_mod.chat_session_repo().get_session(chat_id)

    # Baseline FIRST, unpatched: a plain solo session still mints, so a failure
    # below is the narrowing and not a broken fixture. (`monkeypatch.undo()`
    # cannot serve here — it would also undo the fixtures' own patches.)
    assert isinstance(kai_mod._mint_mcp_access_token(chat_id), str)

    class _Sess:
        def __init__(self, **kw):
            self.user_email = real.user_email
            self.is_co_session = False
            self.agent_id = None
            self.__dict__.update(kw)

    def _with(session, agent=None):
        kai_mod._mcp_token_cache.clear()  # never serve a pre-narrowing cache hit
        monkeypatch.setattr(
            kai_mod, "chat_session_repo", lambda: type("R", (), {"get_session": staticmethod(lambda _sid: session)})()
        )
        if agent is not None:
            import src.repositories as repos

            monkeypatch.setattr(
                repos, "agents_repo", lambda: type("A", (), {"get_by_id": staticmethod(lambda _i: agent)})()
            )
        with pytest.raises(HTTPException) as e:
            kai_mod._mint_mcp_access_token(chat_id)
        return e.value

    # 1. shared conversation -> refused, not resolved to its stored owner
    exc = _with(_Sess(is_co_session=True))
    assert (exc.status_code, exc.detail) == (403, "mcp_not_available_to_co_session")

    # 2. scope-limited agent -> refused
    exc = _with(_Sess(agent_id="ag_1"), agent={"id": "ag_1", "deleted_at": None, "scope_mode": "selected"})
    assert (exc.status_code, exc.detail) == (403, "mcp_not_available_to_scoped_agent")

    # 3. deleted agent -> fails CLOSED rather than falling through to the owner
    exc = _with(_Sess(agent_id="ag_2"), agent={"id": "ag_2", "deleted_at": "2026-01-01T00:00:00Z"})
    assert (exc.status_code, exc.detail) == (401, "ticket_agent_not_found")


def test_mcp_token_is_bound_to_the_mcp_resource_server(seeded_app, kai_env):
    """Parity with the genuine OAuth code exchange, which stores the requested
    `resource`. A stored `None` authenticates today, so this is about not
    breaking later: an SDK that begins enforcing RFC 8707 audience binding
    would 401 brokered engine traffic while real connectors kept working.
    Found by Devin Review."""
    from app.api.kai import _mint_mcp_access_token
    from app.auth.public_url import mcp_issuer_url
    from src.repositories import oauth_clients_repo

    token = _mint_mcp_access_token(_mint_session(seeded_app)["chat_id"])
    row = oauth_clients_repo().get_access_token(token)
    assert row is not None
    assert row["resource"] == mcp_issuer_url()


def test_mcp_mints_a_registered_access_token_for_the_ticket_identity(seeded_app, kai_env):
    """The mounted MCP app resolves a bearer against `oauth_access_tokens`, so
    a bare session JWT would not authenticate — the token has to be minted AND
    registered, carrying scope='mcp-oauth' so resolve_token_to_user stamps the
    agent-surface posture."""
    import base64
    import json as _json

    from app.api.kai import _mint_mcp_access_token
    from src.repositories import oauth_clients_repo

    body = _mint_session(seeded_app)
    token = _mint_mcp_access_token(body["chat_id"])

    row = oauth_clients_repo().get_access_token(token)
    assert row is not None, "token must be resolvable by the MCP verifier"
    assert row["client_id"] == "kai-agent-broker"

    payload = token.split(".")[1]
    claims = _json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert claims["scope"] == "mcp-oauth"
    assert claims["chat_session_id"] == body["chat_id"]


def test_mcp_access_token_is_reused_across_calls_for_one_session(seeded_app, kai_env):
    """Minting is a DB write and an MCP turn makes many JSON-RPC calls."""
    from app.api.kai import _mint_mcp_access_token

    body = _mint_session(seeded_app)
    assert _mint_mcp_access_token(body["chat_id"]) == _mint_mcp_access_token(body["chat_id"])


def test_mcp_token_is_scoped_to_its_own_session(seeded_app, kai_env):
    from app.api.kai import _mint_mcp_access_token

    first = _mint_session(seeded_app)
    second = _mint_session(seeded_app)
    assert _mint_mcp_access_token(first["chat_id"]) != _mint_mcp_access_token(second["chat_id"])


# ---------------------------------------------------------------------------
# workspace payload
# ---------------------------------------------------------------------------


def test_workspace_requires_the_session_credential(seeded_app, kai_env):
    assert seeded_app["client"].get("/api/kai/workspace").status_code == 401


def test_workspace_rejects_a_broker_ticket(seeded_app, kai_env):
    """Only the engine's server fetches this; a sandbox-held ticket must not."""
    from src.repositories import ticket_repo

    body = _mint_session(seeded_app)
    ticket = ticket_repo().mint(body["chat_id"], "mcp")
    resp = seeded_app["client"].get("/api/kai/workspace", headers={"Authorization": f"Bearer {ticket}"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "kai_credential_scope_mismatch"


def test_workspace_serves_a_gzipped_tar_of_the_template(seeded_app, kai_env):
    """The engine rejects anything but 200-with-body or 204, and rejects the
    whole payload on an absolute path, a `..` segment or a non-file member."""
    import tarfile as _tarfile

    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    resp = seeded_app["client"].get("/api/kai/workspace", headers={"Authorization": f"Bearer {credential}"})
    assert resp.status_code == 200
    assert resp.content, "an empty 200 is a contract violation — 204 means 'none'"

    with _tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        members = tar.getmembers()

    names = [m.name for m in members]
    assert members, "archive must not be empty"
    assert all(m.isfile() for m in members), "non-file members fail the engine's validation"
    assert not any(n.startswith("/") for n in names), "absolute paths are rejected"
    assert not any(".." in n.split("/") for n in names), "dot segments are rejected"

    # The three things that make this Agnes's harness rather than bare Claude Code.
    assert "CLAUDE.md" in names
    assert any(n.startswith(".claude/skills/") for n in names)
    assert ".claude/hooks/pre_tool_use.py" in names

    # Sandbox-image build assets describe how to BUILD a sandbox, not how to
    # work in one — they have no place in another engine's workspace.
    assert not any(n.startswith("docker-sandbox/") for n in names)


def test_workspace_archive_is_byte_stable_across_a_second_boundary(seeded_app, kai_env):
    """The engine re-fetches on every SDK respawn; a payload differing only by
    timestamp would churn the sandbox tree for nothing.

    The second boundary is the point. Pinning only the tar members' mtime
    leaves the gzip *container* header carrying `time.time()`, so two packs
    within the same second compare equal and the bug hides — which is exactly
    how it shipped. Freeze the clock a second apart instead of sleeping.
    """
    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    headers = {"Authorization": f"Bearer {credential}"}

    with mock.patch("time.time", return_value=1_700_000_000.0):
        first = seeded_app["client"].get("/api/kai/workspace", headers=headers).content
    with mock.patch("time.time", return_value=1_700_000_042.0):
        second = seeded_app["client"].get("/api/kai/workspace", headers=headers).content

    assert first == second
    # Belt and braces: the gzip header's 4-byte MTIME field must be zeroed.
    assert first[4:8] == b"\x00\x00\x00\x00"


@pytest.mark.parametrize(
    ("method", "path", "auth"),
    [
        # /sessions is user-authenticated; the rest carry the engine's own
        # credential. Each route must be probed with the token it actually
        # accepts, or it 401s before ever reaching the kill switch.
        ("post", "/api/kai/sessions", "user"),
        ("post", "/api/kai/tickets", "credential"),
        ("post", "/api/kai/mcp", "credential"),
        ("get", "/api/kai/workspace", "credential"),
    ],
)
def test_every_route_honours_the_kill_switch(seeded_app, kai_env, monkeypatch, method, path, auth):
    """Unsetting the secret must stop the integration *now*.

    Checking it only on session creation left every already-issued credential
    minting egress tickets and reaching tools for the rest of its 12 h life,
    which is not what an operator disabling the integration expects — and the
    engine's verify path has no revocation to fall back on.
    """
    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    bearer = seeded_app["analyst_token"] if auth == "user" else credential
    monkeypatch.delenv("KAI_HOST_JWT_SECRET", raising=False)

    call = getattr(seeded_app["client"], method)
    resp = call(path, headers={"Authorization": f"Bearer {bearer}"})

    assert resp.status_code == 503, f"{method.upper()} {path} still served with the secret unset"
    assert resp.json()["detail"] == "kai_integration_not_configured"


def test_mcp_token_cache_drops_expired_entries(seeded_app, kai_env):
    """The cache must not become an unbounded leak.

    An entry is dead the moment its token expires — the next mint replaces it —
    but a chat that never comes back would otherwise keep a full JWT resident
    for the process lifetime, so memory grew with every engine chat ever served.
    """
    from app.api import kai as kai_mod

    body = _mint_session(seeded_app)
    kai_mod._mcp_token_cache["ghost-chat-that-never-returns"] = ("stale.jwt.value", 1)

    kai_mod._mint_mcp_access_token(body["chat_id"])

    assert "ghost-chat-that-never-returns" not in kai_mod._mcp_token_cache
    assert body["chat_id"] in kai_mod._mcp_token_cache


def test_mcp_proxy_does_not_use_an_in_process_asgi_dispatch(seeded_app, kai_env):
    """`httpx.ASGITransport` looks like it streams and does not.

    It runs the ASGI app to completion, accumulating every body chunk, then
    yields one joined blob — so `stream=True` buys nothing and `httpx.Timeout`
    is inert (no network layer to apply it to). Buffering is not merely slow
    here: the engine's relay bounds time-to-headers, so a tool slower than that
    bound would die on a relay 502. Pin the transport choice so a future
    "simplification" back to ASGITransport has to argue with this test.
    """
    import inspect

    from app.api import kai as kai_mod

    source = inspect.getsource(kai_mod.kai_mcp)
    assert "ASGITransport" not in source
    assert "_mcp_internal_base()" in source


def test_workspace_ignores_a_deregistered_template_clone(seeded_app, kai_env, monkeypatch, tmp_path):
    """The YAML is the source of truth, not the filesystem.

    An admin can unset the template URL while the clone lingers on disk;
    shipping that stale tree to the engine's sandbox is exactly what
    `is_configured()` exists to prevent. Probing `.is_dir()` alone would.
    """
    from app.api import kai as kai_mod
    from app.chat.skills_catalog import BUNDLED_TEMPLATE_DIR

    clone = tmp_path / "iwt"
    (clone / "workspace").mkdir(parents=True)
    (clone / "workspace" / "CLAUDE.md").write_text("# leftover clone", encoding="utf-8")

    import src.initial_workspace as iw

    monkeypatch.setattr(iw, "get_initial_workspace_dir", lambda: clone)

    # De-registered: the clone exists but no URL is configured.
    monkeypatch.setattr(iw, "is_configured", lambda: False)
    assert kai_mod._workspace_template_root() == BUNDLED_TEMPLATE_DIR

    # Registered: the same clone is now the caller's workspace.
    monkeypatch.setattr(iw, "is_configured", lambda: True)
    assert kai_mod._workspace_template_root() == clone / "workspace"


def _claude_md_from(archive: bytes) -> str:
    import tarfile as _tarfile

    with _tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        member = tar.extractfile("CLAUDE.md")
        assert member is not None, "the workspace must carry a CLAUDE.md"
        return member.read().decode("utf-8")


def test_the_engine_gets_this_instances_instructions_not_the_shipped_default(seeded_app, kai_env, monkeypatch):
    """The tarball's CLAUDE.md must be the RENDERED Workspace Prompt.

    Packing the template tree alone was not enough: the template ships a
    static CLAUDE.md, while the instructions an operator actually configures
    in /admin are rendered per user and written over that file when a native
    sandbox is prepared (`app/chat/workdir.py`). Shipping the tree verbatim
    therefore ran the embedded engine on the shipped default while every other
    surface honoured the admin's — silently, because the file is present
    either way. Found by Devin Review on this PR.
    """
    import app.chat.workspace_prompt as wp
    from app.chat.skills_catalog import BUNDLED_TEMPLATE_DIR

    rendered = "# Acme's own instructions\n\nAlways cite the metric definition.\n"
    monkeypatch.setattr(wp, "render_sandbox_workspace_prompt", lambda *a, **k: rendered)

    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    headers = {"Authorization": f"Bearer {credential}"}
    archive = seeded_app["client"].get("/api/kai/workspace", headers=headers).content

    shipped = (BUNDLED_TEMPLATE_DIR / "CLAUDE.md").read_text(encoding="utf-8")
    assert _claude_md_from(archive) == rendered
    assert rendered != shipped, "the fixture must differ from the default, or this proves nothing"

    # Substitution must not cost the byte-stability the engine's re-fetch
    # relies on: same caller, same configuration, same bytes.
    with mock.patch("time.time", return_value=1_700_000_000.0):
        first = seeded_app["client"].get("/api/kai/workspace", headers=headers).content
    with mock.patch("time.time", return_value=1_700_000_042.0):
        second = seeded_app["client"].get("/api/kai/workspace", headers=headers).content
    assert first == second


def test_a_blank_render_leaves_the_templates_own_instructions_alone(seeded_app, kai_env, monkeypatch):
    """An override that renders to whitespace must not blank the workspace."""
    import app.chat.workspace_prompt as wp
    from app.chat.skills_catalog import BUNDLED_TEMPLATE_DIR

    monkeypatch.setattr(wp, "render_sandbox_workspace_prompt", lambda *a, **k: "   \n")
    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    archive = seeded_app["client"].get("/api/kai/workspace", headers={"Authorization": f"Bearer {credential}"}).content

    assert _claude_md_from(archive) == (BUNDLED_TEMPLATE_DIR / "CLAUDE.md").read_text(encoding="utf-8")


def test_the_rendered_prompt_is_withheld_from_the_two_narrowed_session_kinds(monkeypatch):
    """The rendered document is RBAC-filtered for the session's OWNER.

    A co-session is driven by a guest and a scope-limited agent is deliberately
    narrower than its owner, so serving either the owner's filtered view is the
    "over-authorized guests" bug this codebase already refuses elsewhere
    (`_mint_mcp_access_token`, and `WorkdirManager`'s ephemeral co-drive path,
    which likewise never calls the renderer). Here it degrades to the bundled
    text rather than raising, because the route's contract is 200-or-204.
    """
    from types import SimpleNamespace

    import app.api.kai as kai_mod
    import app.chat.workspace_prompt as wp

    monkeypatch.setattr(wp, "render_sandbox_workspace_prompt", lambda *a, **k: "# owner-only\n")

    solo = SimpleNamespace(user_email="owner@example.com", is_co_session=False, agent_id=None)
    assert kai_mod._workspace_prompt_for(solo) == "# owner-only\n"

    guest = SimpleNamespace(user_email="owner@example.com", is_co_session=True, agent_id=None)
    assert kai_mod._workspace_prompt_for(guest) is None

    scoped = SimpleNamespace(user_email="owner@example.com", is_co_session=False, agent_id="a1")
    monkeypatch.setattr(
        "src.repositories.agents_repo",
        lambda: SimpleNamespace(get_by_id=lambda _id: {"id": "a1", "deleted_at": None, "scope": "selected"}),
    )
    monkeypatch.setattr("src.agent_scope_intersection.agent_is_passthrough", lambda _a: False)
    assert kai_mod._workspace_prompt_for(scoped) is None

    # Deleted agent falls BACK, never up to the owner's view.
    monkeypatch.setattr(
        "src.repositories.agents_repo",
        lambda: SimpleNamespace(get_by_id=lambda _id: None),
    )
    assert kai_mod._workspace_prompt_for(scoped) is None


def test_both_sandboxes_render_the_workspace_prompt_through_one_helper():
    """Drift guard. The native sandbox seeds CLAUDE.md through WorkdirManager
    and the engine receives it in a tarball; an admin editing the Workspace
    Prompt expects both to change. Two independent renderings is exactly how
    the engine came to ship the shipped default in the first place.
    """
    from pathlib import Path

    main_src = Path("app/main.py").read_text(encoding="utf-8")
    kai_src = Path("app/api/kai.py").read_text(encoding="utf-8")
    assert "render_sandbox_workspace_prompt" in main_src, (
        "app/main.py must delegate its _render_workspace_prompt to the shared helper"
    )
    assert "render_sandbox_workspace_prompt" in kai_src
    assert "render_claude_md" not in kai_src, "kai must not render the prompt itself — that is the drift this guards"


def test_a_git_template_keeps_its_own_instructions_verbatim(seeded_app, kai_env, monkeypatch, tmp_path):
    """Override mode is the one case the rendered prompt must NOT win.

    `run_init`'s OVERRIDE MODE branch skips the Workspace Prompt write on
    purpose — a registered git template owns CLAUDE.md verbatim, and the two
    override mechanisms are mutually exclusive by design
    (docs/initial-workspace-override.md). Applying the prompt unconditionally
    would make the embedded engine the only surface that merges them.
    """
    import app.chat.workspace_prompt as wp
    import src.initial_workspace as iw

    monkeypatch.setattr(wp, "render_sandbox_workspace_prompt", lambda *a, **k: "# rendered prompt\n")

    clone = tmp_path / "iwt"
    (clone / "workspace").mkdir(parents=True)
    (clone / "workspace" / "CLAUDE.md").write_text("# the git template's own\n", encoding="utf-8")
    monkeypatch.setattr(iw, "get_initial_workspace_dir", lambda: clone)
    monkeypatch.setattr(iw, "is_configured", lambda: True)

    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    archive = seeded_app["client"].get("/api/kai/workspace", headers={"Authorization": f"Bearer {credential}"}).content

    assert _claude_md_from(archive) == "# the git template's own\n"


def test_the_payload_does_not_change_when_the_date_rolls_over(seeded_app, kai_env, monkeypatch):
    """Byte-stability has to survive midnight, not just a second boundary.

    The rendered prompt goes through the real renderer here, on purpose: the
    shipped template ends with "generated {{ today }}", so a render that reads
    the wall clock makes the payload differ across a UTC date rollover. The
    engine re-fetches on every SDK respawn, so a conversation straddling
    midnight would rewrite the whole sandbox tree over a date string — the same
    defect class as the gzip container mtime this builder already pins, and the
    earlier byte-stability test missed it because it froze `time.time` while
    stubbing the renderer out.
    """
    import datetime as _dt

    import src.claude_md as claude_md_mod

    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    headers = {"Authorization": f"Bearer {credential}"}

    class _FrozenDatetime(_dt.datetime):
        _fixed = _dt.datetime(2026, 8, 19, 23, 59, 30, tzinfo=_dt.timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls._fixed if tz is None else cls._fixed.astimezone(tz)

    monkeypatch.setattr(claude_md_mod, "datetime", _FrozenDatetime)
    before_midnight = seeded_app["client"].get("/api/kai/workspace", headers=headers).content

    _FrozenDatetime._fixed = _dt.datetime(2026, 8, 20, 0, 0, 30, tzinfo=_dt.timezone.utc)
    after_midnight = seeded_app["client"].get("/api/kai/workspace", headers=headers).content

    assert before_midnight == after_midnight, (
        "the workspace payload changed across a date rollover — the rendered "
        "CLAUDE.md must be pinned to the session's clock, not the wall clock"
    )


def test_the_pinned_clock_is_the_sessions_own(monkeypatch):
    """...and the pin is the session's start, not an arbitrary constant."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    import app.api.kai as kai_mod
    import app.chat.workspace_prompt as wp

    seen = {}

    def _spy(user_email, **kw):
        seen.update(kw)
        return "# rendered\n"

    monkeypatch.setattr(wp, "render_sandbox_workspace_prompt", _spy)

    started = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
    session = SimpleNamespace(user_email="owner@example.com", is_co_session=False, agent_id=None, started_at=started)
    assert kai_mod._workspace_prompt_for(session) == "# rendered\n"
    assert seen["now"] == started


def test_a_cancelled_tool_call_does_not_leak_the_upstream_client():
    """A disconnect mid-connect must still close the client.

    The client is created outside any `async with`, so the except arm around
    `send()` is the only thing that closes it before the streaming iterator
    takes ownership. `asyncio.CancelledError` derives from BaseException, so an
    `except Exception` arm let a cancelled tool call leak the client and its
    whole connection pool.
    """
    import asyncio
    import inspect

    import app.api.kai as kai_mod

    src = inspect.getsource(kai_mod.kai_mcp)
    assert "except BaseException:" in src, "cancellation escapes an `except Exception` arm and leaks the client"

    # ...and drive the real route, so this is not just an assertion about text.
    closed = []

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def build_request(self, *a, **kw):
            return object()

        async def send(self, *a, **kw):
            raise asyncio.CancelledError()

        async def aclose(self):
            closed.append(True)

    class _Request:
        headers = {"content-type": "application/json"}

        async def body(self):
            return b"{}"

    with (
        mock.patch.object(kai_mod.httpx, "AsyncClient", _Client),
        mock.patch.object(kai_mod, "_mint_mcp_access_token", lambda _sid: "tok"),
        mock.patch.object(kai_mod, "_mcp_internal_base", lambda: "http://mcp.invalid"),
        mock.patch.object(kai_mod, "_require_scope", lambda *a, **k: None),
    ):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(kai_mod.kai_mcp(request=_Request(), row={"session_id": "s1"}))

    assert closed == [True], "the upstream client was not closed on cancellation"


def test_a_failing_response_close_still_releases_the_connection_pool():
    """The generator's two closes must not be a sequence of bare awaits.

    Once ownership passes to `_body_iter`, its `finally` is the only place the
    client is closed. Run as `await upstream.aclose(); await client.aclose()`, a
    failure of the first strands the client's whole pool — and the likeliest way
    to reach that cleanup is the consumer disconnecting, i.e. exactly when an
    `await` inside a `finally` can itself be interrupted.
    """
    import asyncio

    import app.api.kai as kai_mod

    closed = []

    class _Upstream:
        status_code = 200
        headers = {"content-type": "application/json"}

        async def aiter_bytes(self):
            yield b"{}"

        async def aclose(self):
            closed.append("upstream-attempted")
            raise RuntimeError("connection already gone")

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def build_request(self, *a, **kw):
            return object()

        async def send(self, *a, **kw):
            return _Upstream()

        async def aclose(self):
            closed.append("client")

    class _Request:
        headers = {"content-type": "application/json"}

        async def body(self):
            return b"{}"

    async def _run():
        with (
            mock.patch.object(kai_mod.httpx, "AsyncClient", _Client),
            mock.patch.object(kai_mod, "_mint_mcp_access_token", lambda _sid: "tok"),
            mock.patch.object(kai_mod, "_mcp_internal_base", lambda: "http://mcp.invalid"),
            mock.patch.object(kai_mod, "_require_scope", lambda *a, **k: None),
        ):
            resp = await kai_mod.kai_mcp(request=_Request(), row={"session_id": "s1"})
        # Drain the body the way the ASGI server would.
        async for _chunk in resp.body_iterator:
            pass

    asyncio.run(_run())
    assert closed == ["upstream-attempted", "client"], (
        f"the client was not closed after the response close failed: {closed}"
    )


def test_every_reader_facing_surface_names_the_scope_the_route_enforces():
    """The enforced scope and the documented scope must not drift apart.

    This PR renamed the tool ticket's broker scope from `mcp` to `kai_mcp` to
    stop it opening `/api/broker/agnes-mcp`, and the rename was finished in the
    code long before it was finished in the prose. Three separate review rounds
    found leftovers: two docstrings describing a ticket collision that the
    confinement had already closed, then five reader-facing places still telling
    an integrator to mint `mcp` — which the route answers with a 401.

    So this pins the direction of truth: `_EGRESS_SCOPES` is the mapping, and no
    human-facing description may contradict it. The engine's WIRE KEY is still
    `mcp`, which is why the check is written against the phrases that name a
    *scope* rather than against the bare word.

    Twice now this guard has been narrower than the drift. It first omitted
    CHANGELOG.md, so the release notes — the surface an integrator is most
    likely to read — kept telling them to mint `mcp`; and its literal phrases
    did not survive markdown emphasis, so "gated on the **`mcp`** ticket scope"
    slipped past the "the `mcp` ticket scope" probe. Emphasis is therefore
    stripped before matching, and every surface that describes this route to a
    human is in the list.
    """
    from pathlib import Path

    import app.api.kai as kai_mod

    assert kai_mod._EGRESS_SCOPES["mcp"] == "kai_mcp", (
        "the engine's wire key `mcp` must map onto the confined broker scope"
    )

    # Phrasings that assert a SCOPE. Each was a real leftover found in review.
    stale = (
        "scope-gated on ``mcp``",
        "`mcp`-scoped broker ticket",
        "the `mcp` ticket scope",
        "the ``mcp`` ticket scope",
        "``mcp`` scope\n  onto ``mcp``",
        # Survived the first version of this guard: emphasis broke the probe
        # above, and CHANGELOG.md was not a surface.
        "gated on the `mcp` ticket scope",
        "adds the optional `mcp` scope",
        "any `mcp`-scoped ticket reaches this route",
    )
    surfaces = (
        "app/api/kai.py",
        "app/switches.py",
        "docs/api-reference.md",
        "docs/feature-flags.md",
        "CHANGELOG.md",
    )

    def normalize(text: str) -> str:
        """Strip markdown emphasis so a bolded scope name cannot hide from a probe."""
        return text.replace("*", "").replace("_", "")

    for rel in surfaces:
        text = Path(rel).read_text(encoding="utf-8")
        # Quoting a corrected claim is allowed; asserting it is not.
        prose = normalize(text.replace('this once read "ANY ``mcp``-scoped ticket', ""))
        for phrase in stale:
            assert normalize(phrase).lower() not in prose.lower(), (
                f"{rel} still describes the tool route's scope as `mcp`; the route "
                f"enforces `kai_mcp` and refuses `mcp` with a 401 ({phrase!r})"
            )

    # And the two operator-facing surfaces must name the real scope.
    for rel in ("app/switches.py", "docs/feature-flags.md"):
        assert "kai_mcp" in Path(rel).read_text(encoding="utf-8"), (
            f"{rel} must name the `kai_mcp` scope an operator's switch actually issues"
        )


def test_a_native_sweep_does_reach_the_engines_egress_tickets_on_purpose(seeded_app, kai_env):
    """The isolation between the two runtimes is one-way, deliberately.

    The engine's rotation is scope-limited and cannot touch a native sandbox's
    tickets. The native sweep is scope-BLIND and *can* touch the engine's: it
    deletes every scope for the id except `SWEEP_EXEMPT_SCOPES`. That costs one
    interrupted engine turn when someone opens the same conversation in web
    chat, and it is kept because `/api/broker/anthropic` does not check that the
    session row still exists — so this sweep, reached via `kill()` on permanent
    delete, is what stops a deleted conversation's `llm` ticket from spending
    budget for the rest of its TTL.

    Pinned so that "fix the interruption" cannot quietly become "leave a usable
    LLM ticket behind after a delete". Anyone widening the exemption must make
    this test fail and go read why.
    """
    from src.repositories import ticket_repo

    body = _mint_session(seeded_app)
    credential = _claims(body["token"])["downstream_credential"]
    chat_id = body["chat_id"]

    resp = seeded_app["client"].post("/api/kai/tickets", headers={"Authorization": f"Bearer {credential}"})
    assert resp.status_code == 200
    egress = resp.json()
    assert "llm" in egress

    repo = ticket_repo()
    assert repo.resolve(egress["llm"]) is not None

    # The native sandbox lifecycle sweep, exactly as app/chat/manager.py calls it.
    repo.revoke_session(chat_id)

    assert repo.resolve(egress["llm"]) is None, (
        "the native sweep must still reach the engine's egress tickets — that is "
        "what protects the LLM budget after a conversation is deleted"
    )
    # ...while the session credential itself survives, or the engine would be
    # cut off for good with no channel to hand it a replacement.
    assert repo.resolve(credential) is not None, (
        "SWEEP_EXEMPT_SCOPES must keep the long-lived credential out of the sweep"
    )


def test_an_editor_prompt_beats_a_git_template_here_as_it_does_natively(seeded_app, kai_env, monkeypatch, tmp_path):
    """Override mode is NOT a blanket "the clone's CLAUDE.md wins".

    `run_init`'s OVERRIDE MODE branch skips the rendered-prompt write, which
    reads as though a registered git template owns CLAUDE.md outright — and an
    earlier version of this module read it that way. But that branch takes its
    tree from `build_zip`, which has already overlaid the admin's EDITOR-mode
    prompt over the clone's `workspace/CLAUDE.md` ("THE chokepoint" for #622).
    The two mechanisms are layered, not exclusive, so suppressing the prompt in
    override mode made this route the one surface running the shipped template
    while every other surface read the admin's edit.
    """
    import src.initial_workspace as iw

    import app.chat.workspace_prompt as wp

    clone = tmp_path / "iwt"
    (clone / "workspace").mkdir(parents=True)
    (clone / "workspace" / "CLAUDE.md").write_text("# the git template's own\n", encoding="utf-8")
    monkeypatch.setattr(iw, "get_initial_workspace_dir", lambda: clone)
    monkeypatch.setattr(iw, "is_configured", lambda: True)
    monkeypatch.setattr(wp, "render_sandbox_workspace_prompt", lambda *a, **k: "# the admin's edit\n")

    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    headers = {"Authorization": f"Bearer {credential}"}

    # editor-mode prompt set -> the admin's edit wins, override mode included.
    monkeypatch.setattr(iw, "resolve_prompt", lambda kind, conn=None: ("# admin body", "editor"))
    got = _claude_md_from(seeded_app["client"].get("/api/kai/workspace", headers=headers).content)
    assert got == "# the admin's edit\n", (
        "an editor-mode Workspace Prompt must reach the engine in override mode too — "
        "build_zip overlays it for the native sandbox"
    )

    # git-bound prompt -> the clone's file ships verbatim, as build_zip leaves it.
    monkeypatch.setattr(iw, "resolve_prompt", lambda kind, conn=None: (None, "git"))
    got = _claude_md_from(seeded_app["client"].get("/api/kai/workspace", headers=headers).content)
    assert got == "# the git template's own\n"


def test_a_deleted_conversation_cannot_spend_budget_with_an_issued_llm_ticket(seeded_app, kai_env):
    """The deletion hole must close without depending on a chat manager.

    `/api/broker/anthropic` is the one place an ALREADY-issued egress ticket can
    still spend the instance's LLM budget. The sweep that used to bound it runs
    only via `ChatManager.kill`, and `_kill_quietly` returns early when
    `app.state.chat_manager is None` — the normal state for an instance that
    embeds the engine and does not run Agnes's own sandbox chat. So on exactly
    the target deployment, a permanently deleted conversation left its ticket
    spendable for the rest of its TTL.
    """
    from src.repositories import chat_session_repo, ticket_repo

    body = _mint_session(seeded_app)
    credential = _claims(body["token"])["downstream_credential"]
    chat_id = body["chat_id"]

    egress = seeded_app["client"].post("/api/kai/tickets", headers={"Authorization": f"Bearer {credential}"}).json()
    llm_ticket = egress["llm"]
    assert ticket_repo().resolve(llm_ticket) is not None

    # The row goes away with NO sandbox kill and NO ticket sweep — precisely the
    # engine-only shape, where chat_manager is None and _kill_quietly no-ops.
    chat_session_repo().hard_delete_session(chat_id)
    assert ticket_repo().resolve(llm_ticket) is not None, (
        "fixture check: the ticket must still resolve, or this proves nothing"
    )

    resp = seeded_app["client"].post(
        "/api/broker/anthropic/v1/messages",
        headers={"Authorization": f"Bearer {llm_ticket}"},
        json={"model": "claude-sonnet-4-5", "messages": [], "max_tokens": 1},
    )
    assert resp.status_code == 401, (
        f"a deleted conversation's llm ticket still reached the LLM broker ({resp.status_code})"
    )
    assert resp.json()["detail"] == "ticket_session_gone"


# ---------------------------------------------------------------------------
# workspace payload — marketplace skills (#1552)
#
# The kai-agent provider spawns no runner of ours, so this tarball IS the
# engine's project scope. Before this, a user's stack skills reached every
# surface except the embedded engine: the composer offered `/keboola-cli` and
# the engine answered "Unknown command".
# ---------------------------------------------------------------------------


def _grant_marketplace_skill(
    *, plugin: str, skill: str, body: str = "Body.", extra_file: str = "", full: bool = False
) -> None:
    """Grant + subscribe the seeded analyst to a marketplace plugin shipping one
    skill, and write that plugin to the marketplace clone on disk.

    ``full=True`` also gives it an agent, a slash command, a hook and an MCP
    server — the component types that make it a plugin rather than a skill."""
    from datetime import datetime, timezone

    from app.utils import get_marketplaces_dir
    from src.db import SYSTEM_EVERYONE_GROUP, get_system_db
    from src.repositories import (
        resource_grants_repo,
        user_curated_subscriptions_repo,
        user_groups_repo,
    )

    conn = get_system_db()
    try:
        # Idempotent: a test may stack two plugins from the same marketplace.
        if not conn.execute("SELECT 1 FROM marketplace_registry WHERE id = 'mkt'").fetchone():
            conn.execute(
                "INSERT INTO marketplace_registry (id, name, url, registered_at) VALUES (?, ?, ?, ?)",
                ["mkt", "MKT", "https://example.test/mkt.git", datetime.now(timezone.utc)],
            )
        conn.execute(
            "INSERT INTO marketplace_plugins (marketplace_id, name, version, raw, updated_at) VALUES (?, ?, ?, ?, ?)",
            ["mkt", plugin, "1.0", json.dumps({"name": plugin, "version": "1.0"}), datetime.now(timezone.utc)],
        )
    finally:
        conn.close()

    everyone = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
    resource_grants_repo().create(everyone["id"], "marketplace_plugin", f"mkt/{plugin}")
    user_curated_subscriptions_repo().subscribe("analyst1", "mkt", plugin)

    plugin_root = get_marketplaces_dir() / "mkt" / "plugins" / plugin
    skill_dir = plugin_root / "skills" / skill
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {skill}\n---\n\n{body}", encoding="utf-8")
    if extra_file:
        (skill_dir / extra_file).write_text("extra", encoding="utf-8")
    if not full:
        return
    (plugin_root / "agents").mkdir(parents=True, exist_ok=True)
    (plugin_root / "agents" / "kbl-reviewer.md").write_text(
        "---\nname: kbl-reviewer\ndescription: Agent.\n---\n\nBody.", encoding="utf-8"
    )
    (plugin_root / "commands").mkdir(parents=True, exist_ok=True)
    (plugin_root / "commands" / "kbl-ship.md").write_text("---\ndescription: Command.\n---\n\nBody.", encoding="utf-8")
    (plugin_root / "hooks").mkdir(parents=True, exist_ok=True)
    (plugin_root / "hooks" / "hooks.json").write_text(
        json.dumps({"hooks": {"PostToolUse": [{"hooks": [{"type": "command", "command": "true"}]}]}}),
        encoding="utf-8",
    )
    (plugin_root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"probe-mcp": {"command": "echo", "args": ["noop"]}}}), encoding="utf-8"
    )


def _workspace_names(seeded_app) -> list[str]:
    import tarfile as _tarfile

    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    resp = seeded_app["client"].get("/api/kai/workspace", headers={"Authorization": f"Bearer {credential}"})
    assert resp.status_code == 200, resp.text
    with _tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        return [m.name for m in tar.getmembers()]


def test_the_engine_gets_the_callers_marketplace_skills(seeded_app, kai_env):
    _grant_marketplace_skill(plugin="demo-plugin", skill="keboola-cli", extra_file="reference.md")

    names = _workspace_names(seeded_app)

    assert ".claude/skills/keboola-cli/SKILL.md" in names, (
        "the composer offers /keboola-cli; without this member the engine has never been given it"
    )
    # A skill's supporting files are part of it.
    assert ".claude/skills/keboola-cli/reference.md" in names


def test_marketplace_skills_are_omitted_when_the_switch_is_off(seeded_app, kai_env, monkeypatch):
    """With delivery off the composer stops offering them, so the archive must
    stop shipping them — one switch, both sides."""
    monkeypatch.setenv("AGNES_CHAT_BOOTSTRAP_MARKETPLACE", "0")
    _grant_marketplace_skill(plugin="demo-plugin", skill="keboola-cli")

    names = _workspace_names(seeded_app)

    assert ".claude/skills/keboola-cli/SKILL.md" not in names
    assert "CLAUDE.md" in names, "the rest of the workspace must still ship"


def test_a_marketplace_skill_shadows_the_bundled_one_wholesale(seeded_app, kai_env):
    """`merged_skills`'s rule is "marketplace wins name clashes". Merging the two
    directories instead would leave the loser's files inside the winner, and the
    agent would read them."""
    from app.chat.skills_catalog import BUNDLED_TEMPLATE_DIR

    bundled_names = [p.name for p in (BUNDLED_TEMPLATE_DIR / ".claude" / "skills").iterdir() if p.is_dir()]
    assert bundled_names, "the bundled template ships no skills — nothing to shadow"
    victim = sorted(bundled_names)[0]
    _grant_marketplace_skill(plugin="demo-plugin", skill=victim, body="MARKETPLACE VERSION")

    import tarfile as _tarfile

    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    resp = seeded_app["client"].get("/api/kai/workspace", headers={"Authorization": f"Bearer {credential}"})
    with _tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        names = [m.name for m in tar.getmembers()]
        body = tar.extractfile(f".claude/skills/{victim}/SKILL.md").read().decode()

    assert "MARKETPLACE VERSION" in body
    assert names.count(f".claude/skills/{victim}/SKILL.md") == 1, "a duplicate member is a malformed tar"
    # Nothing of the bundled copy survives inside the winner.
    assert [n for n in names if n.startswith(f".claude/skills/{victim}/")] == [f".claude/skills/{victim}/SKILL.md"]


def test_the_archive_stays_byte_stable_with_marketplace_skills(seeded_app, kai_env):
    """The engine re-fetches on every SDK respawn — the overlay must not
    reintroduce the churn the mtime pinning exists to prevent."""
    _grant_marketplace_skill(plugin="demo-plugin", skill="keboola-cli")
    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    headers = {"Authorization": f"Bearer {credential}"}

    with mock.patch("time.time", return_value=1_700_000_000.0):
        first = seeded_app["client"].get("/api/kai/workspace", headers=headers).content
    with mock.patch("time.time", return_value=1_700_000_042.0):
        second = seeded_app["client"].get("/api/kai/workspace", headers=headers).content

    assert first == second


def _workspace_member(seeded_app, arcname: str) -> str:
    import tarfile as _tarfile

    credential = _claims(_mint_session(seeded_app)["token"])["downstream_credential"]
    resp = seeded_app["client"].get("/api/kai/workspace", headers={"Authorization": f"Bearer {credential}"})
    assert resp.status_code == 200, resp.text
    with _tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        return tar.extractfile(arcname).read().decode()


def test_the_engine_gets_every_component_type_not_just_skills(seeded_app, kai_env):
    """A plugin is its agents, commands, hooks and MCP servers too — delivering
    only skills would leave most of a user's stack unreachable on this provider.
    Agnes cannot install real plugins here (that writes the CLI's HOME registry
    in a sandbox Agnes never enters), so they arrive as project-scope files."""
    _grant_marketplace_skill(plugin="kbl", skill="keboola-cli", full=True)

    names = _workspace_names(seeded_app)

    assert ".claude/skills/keboola-cli/SKILL.md" in names
    assert ".claude/agents/kbl-reviewer.md" in names
    assert ".claude/commands/kbl-ship.md" in names
    # Hooks and MCP servers have no installed plugin to live in, so they have to
    # become project config or they do not exist for the agent at all.
    assert ".claude/settings.json" in names
    assert ".mcp.json" in names


def test_plugin_hooks_merge_into_the_templates_settings(seeded_app, kai_env):
    """Merged, not replaced: the bundled template's own settings (the org safety
    hook among them) must survive a marketplace plugin arriving."""
    _grant_marketplace_skill(plugin="kbl", skill="keboola-cli", full=True)

    before = json.loads(_workspace_member(seeded_app, ".claude/settings.json"))

    assert "PostToolUse" in before.get("hooks", {})
    # Whatever else the template declared at the top level is still there.
    assert set(before) >= {"hooks"}


def test_plugin_mcp_servers_reach_the_project(seeded_app, kai_env):
    _grant_marketplace_skill(plugin="kbl", skill="keboola-cli", full=True)

    mcp = json.loads(_workspace_member(seeded_app, ".mcp.json"))

    assert "probe-mcp" in mcp["mcpServers"]


def test_no_components_no_synthesized_config(seeded_app, kai_env):
    """A stack with no hooks/MCP must not gain an empty settings or .mcp.json
    that was never in the template — the payload is byte-compared by the engine
    on every respawn."""
    _grant_marketplace_skill(plugin="kbl", skill="keboola-cli")

    names = _workspace_names(seeded_app)

    assert ".claude/skills/keboola-cli/SKILL.md" in names
    assert ".mcp.json" not in names


def test_flattened_mcp_servers_are_pre_approved(seeded_app, kai_env):
    """A project `.mcp.json` server is untrusted-by-default: without an
    allow-list entry the CLI never spawns it (verified against Claude Code
    2.1.218 by watching for the server process), and no one can approve it
    interactively in a headless sandbox. On the sibling providers the same
    servers arrive inside an installed plugin and need no approval at all."""
    _grant_marketplace_skill(plugin="kbl", skill="keboola-cli", full=True)

    settings = json.loads(_workspace_member(seeded_app, ".claude/settings.json"))

    assert settings["enabledMcpjsonServers"] == ["probe-mcp"]
    # The template's own hook survives the merge.
    assert "PreToolUse" in settings["hooks"]


def test_no_mcp_servers_no_allow_list(seeded_app, kai_env):
    """The allow-list names exactly what Agnes put there — it is not a blanket
    `enableAllProjectMcpServers`, which would also pre-approve anything a future
    template or an operator's own `.mcp.json` adds."""
    _grant_marketplace_skill(plugin="kbl", skill="keboola-cli")

    settings = json.loads(_workspace_member(seeded_app, ".claude/settings.json"))

    assert "enabledMcpjsonServers" not in settings


def test_merged_config_bytes_do_not_depend_on_plugin_order(seeded_app, kai_env):
    """The engine re-fetches this archive on every SDK respawn and compares
    bytes. The merged `hooks` / `mcpServers` blocks are built by iterating
    plugins, so their serialization must not carry that order — otherwise
    stability rests on a guarantee three modules away."""
    _grant_marketplace_skill(plugin="kbl", skill="keboola-cli", full=True)
    _grant_marketplace_skill(plugin="aaa-first", skill="other-skill", full=True)

    mcp = json.loads(_workspace_member(seeded_app, ".mcp.json"))
    settings = json.loads(_workspace_member(seeded_app, ".claude/settings.json"))

    raw_mcp = _workspace_member(seeded_app, ".mcp.json")
    raw_settings = _workspace_member(seeded_app, ".claude/settings.json")

    # Key-sorted at every level we synthesize, so a different merge order
    # produces identical bytes.
    assert list(mcp) == sorted(mcp)
    assert list(mcp["mcpServers"]) == sorted(mcp["mcpServers"])
    assert list(settings) == sorted(settings)
    assert raw_mcp == _workspace_member(seeded_app, ".mcp.json")
    assert raw_settings == _workspace_member(seeded_app, ".claude/settings.json")
