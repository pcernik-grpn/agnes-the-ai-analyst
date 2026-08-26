"""V1d Task 5 — the two scope axes the intersection map cannot express.

``AgentPrincipal.intersection`` is keyed on ``ResourceType``, so it can only
carry resources authorized through ``resource_grants``. Two declared
restrictions therefore have no home in it and need their own live filter:

**A. MCP connections** (``connections_mode``). Passthrough tools are
authorized by ``tool_registry`` grants keyed on *groups*, never by
``resource_grants`` — there is no ``ResourceType.CONNECTION``. Without a
dedicated filter, ``connections_mode='selected'`` is decorative.

**B. Store installs** (``plugins_mode``). ``resolve_user_marketplace``
composes ``(rbac ∩ (subscriptions ∪ required)) ∪ store_installs``; the
trailing union sits OUTSIDE the intersection, so a ``plugins_mode='selected'``
agent kept receiving its owner's personal flea-market installs.

Both filters key off ``mode == 'selected'`` **specifically**, never off "is an
``AgentPrincipal``": the broker only mints an agent-session token for an agent
that narrows *something*, so a tables-narrowed agent legitimately keeps
``connections_mode``/``plugins_mode`` at ``'all'`` and must keep its owner's
full set on those axes.

Both dimensions are covered at the **call** seam, not only at listing —
hiding a tool from ``tools/list`` is discovery, not authorization.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("mcp", reason="mcp SDK not installed")

from app.auth.session_principal import AgentPrincipal, SessionPrincipal
from app.chat.types import Surface
from src.db import get_system_db
from src.repositories import agents_repo, chat_session_repo
from src.repositories.mcp_sources import MCPSourceRepository
from src.repositories.tool_registry import PASSTHROUGH, ToolRegistryRepository
from src.repositories.user_group_members import UserGroupMembersRepository
from src.repositories.user_groups import UserGroupsRepository
from src.repositories.users import UserRepository

# ---------------------------------------------------------------------------
# Part A — MCP connections (end-to-end through the REST seam the sandboxed
# ``agnes mcp`` stdio server actually calls)
# ---------------------------------------------------------------------------

ALPHA_TOOL = "alpha-up.ping"
BETA_TOOL = "beta-up.ping"


def _seed_two_sources(owner_user_id: str) -> None:
    """Two upstream MCP sources, one passthrough tool each, BOTH granted to a
    group the owner belongs to. Everything the agent could reach is therefore
    reachable by its owner — the only thing that can narrow it is the agent's
    own ``connection`` scope."""
    tag = uuid.uuid4().hex[:8]
    conn = get_system_db()
    sources = MCPSourceRepository(conn)
    tools = ToolRegistryRepository(conn)
    groups = UserGroupsRepository(conn)
    members = UserGroupMembersRepository(conn)

    sources.upsert(id="src_alpha", name="alpha-up", transport="stdio", command="/bin/true", args=[])
    sources.upsert(id="src_beta", name="beta-up", transport="stdio", command="/bin/true", args=[])
    tools.upsert(
        tool_id=ALPHA_TOOL,
        source_id="src_alpha",
        original_name="ping",
        exposed_name="alpha_ping",
        mode=PASSTHROUGH,
        description="In the agent's connection scope.",
    )
    tools.upsert(
        tool_id=BETA_TOOL,
        source_id="src_beta",
        original_name="ping",
        exposed_name="beta_ping",
        mode=PASSTHROUGH,
        description="OUTSIDE the agent's connection scope.",
    )
    grp = groups.create(name=f"agent-mcp-grp-{tag}", description="owner's tool grants")
    tools.add_grant(ALPHA_TOOL, grp["id"])
    tools.add_grant(BETA_TOOL, grp["id"])
    members.add_member(owner_user_id, grp["id"], source="system_seed")
    conn.close()


def _make_owner(admin: bool = False) -> tuple[str, str]:
    tag = uuid.uuid4().hex[:8]
    user_id, email = f"agent_mcp_owner_{tag}", f"agent_mcp_owner_{tag}@test.com"
    conn = get_system_db()
    UserRepository(conn).create(id=user_id, email=email, name="Agent MCP Owner")
    if admin:
        from src.db import SYSTEM_ADMIN_GROUP

        admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
        UserGroupMembersRepository(conn).add_member(user_id, admin_gid, source="system_seed")
    conn.close()
    return user_id, email


def _agent_session_token(owner_user_id: str, owner_email: str, *, scope=(), **modes) -> str:
    """Create an agent (with the given ``*_mode`` columns + ``agent_scope``
    rows), bind a live chat session to it, and mint the agent-session JWT the
    broker would mint for that session."""
    from app.auth.access import mint_agent_session_jwt

    agent_id = str(uuid.uuid4())
    kwargs = dict(
        id=agent_id,
        owner_user_id=owner_user_id,
        name="Scoped Agent",
        slug=f"scoped-agent-{uuid.uuid4().hex[:8]}",
        plugins_mode="all",
        connections_mode="all",
        tables_mode="all",
        memory_mode="all",
    )
    kwargs.update(modes)
    agents_repo().create(**kwargs)
    if scope:
        agents_repo().set_scope(agent_id, list(scope))
    session = chat_session_repo().create_session(user_email=owner_email, surface=Surface.WEB, agent_id=agent_id)
    return mint_agent_session_jwt(session.id)


def _patch_upstream_call(text="ok"):
    from connectors.mcp.client import ToolCallResult

    return patch(
        "app.api.mcp_passthrough.call_tool_async",
        new=AsyncMock(return_value=ToolCallResult(text=text, data=None, is_error=False)),
    )


def _list_tool_ids(client, token) -> set[str]:
    r = client.get("/api/mcp/passthrough/tools", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return {t["tool_id"] for t in r.json()}


def _call(client, token, tool_id):
    return client.post(
        f"/api/mcp/passthrough/tools/{tool_id}/call",
        headers={"Authorization": f"Bearer {token}"},
        json={"arguments": {}},
    )


def test_selected_connections_agent_lists_only_scoped_tools(seeded_app):
    """(a) Discovery: only tools whose MCP source is in the agent's
    ``connection`` scope survive the listing."""
    owner_id, owner_email = _make_owner()
    _seed_two_sources(owner_id)
    token = _agent_session_token(
        owner_id,
        owner_email,
        connections_mode="selected",
        scope=[("connection", "src_alpha")],
    )

    assert _list_tool_ids(seeded_app["client"], token) == {ALPHA_TOOL}


def test_selected_connections_agent_cannot_invoke_unlisted_tool(seeded_app):
    """(b) THE authorization test: naming an unlisted tool directly must be
    refused, and the upstream must never be contacted."""
    owner_id, owner_email = _make_owner()
    _seed_two_sources(owner_id)
    token = _agent_session_token(
        owner_id,
        owner_email,
        connections_mode="selected",
        scope=[("connection", "src_alpha")],
    )

    with _patch_upstream_call() as forward:
        r = _call(seeded_app["client"], token, BETA_TOOL)
    assert r.status_code == 403, r.text
    forward.assert_not_awaited()

    # Positive control — the scoped source still works, so the denial above
    # is the connection filter and not a blanket agent lockout.
    with _patch_upstream_call(text="pong") as forward:
        ok = _call(seeded_app["client"], token, ALPHA_TOOL)
    assert ok.status_code == 200, ok.text
    assert ok.json()["text"] == "pong"
    forward.assert_awaited_once()


def test_all_connections_agent_is_unaffected(seeded_app):
    """(c) An agent that narrows some OTHER axis keeps ``connections_mode='all'``
    and must still reach every connection its owner can."""
    owner_id, owner_email = _make_owner()
    _seed_two_sources(owner_id)
    token = _agent_session_token(
        owner_id,
        owner_email,
        connections_mode="all",
        tables_mode="selected",  # narrows elsewhere, so the broker still mints an agent token
        scope=[("table", "some_table")],
    )

    assert _list_tool_ids(seeded_app["client"], token) == {ALPHA_TOOL, BETA_TOOL}
    with _patch_upstream_call():
        assert _call(seeded_app["client"], token, BETA_TOOL).status_code == 200


def test_selected_connections_empty_scope_reaches_nothing(seeded_app):
    """``'selected'`` with no ``connection`` rows is a real (empty) allowlist,
    never a pass-through."""
    owner_id, owner_email = _make_owner()
    _seed_two_sources(owner_id)
    token = _agent_session_token(owner_id, owner_email, connections_mode="selected")

    assert _list_tool_ids(seeded_app["client"], token) == set()
    with _patch_upstream_call() as forward:
        assert _call(seeded_app["client"], token, ALPHA_TOOL).status_code == 403
    forward.assert_not_awaited()


def test_admin_owned_agent_does_not_inherit_admin_passthrough(seeded_app):
    """The admin short-circuit in the tool-grant gate must not fire for an
    agent, even when its owner is an admin — otherwise every connection in
    the instance is reachable regardless of scope."""
    owner_id, owner_email = _make_owner(admin=True)
    _seed_two_sources(owner_id)
    token = _agent_session_token(
        owner_id,
        owner_email,
        connections_mode="selected",
        scope=[("connection", "src_alpha")],
    )

    assert _list_tool_ids(seeded_app["client"], token) == {ALPHA_TOOL}
    with _patch_upstream_call() as forward:
        assert _call(seeded_app["client"], token, BETA_TOOL).status_code == 403
    forward.assert_not_awaited()


def test_co_session_principal_sees_no_passthrough_tools(seeded_app):
    """A co-session has no single owner whose per-user MCP credentials /
    connection grants could be resolved — fail closed rather than crash on
    ``user["id"]``."""
    from app.api.mcp_passthrough import _visible_passthrough_tools

    principal = SessionPrincipal(
        session_id="chat_co",
        participant_user_ids=["u1", "u2"],
        participant_emails=["a@example.com", "b@example.com"],
        intersection={},
    )
    assert _visible_passthrough_tools(principal) == []


def test_sse_transport_rejects_an_agent_session_token(seeded_app):
    """The SSE MCP transport resolves a caller to a bare user id for its
    passthrough closures; a restricted principal has none, so it must be
    refused at the door rather than blowing up (or, worse, resolving to the
    owner and skipping the connection filter entirely).

    Driven as raw ASGI — a real GET on ``/api/mcp/sse`` is an infinite
    event stream that ``TestClient`` would block on forever.
    """
    import asyncio

    from app.api.mcp_http import _AuthMiddleware

    owner_id, owner_email = _make_owner()
    _seed_two_sources(owner_id)
    token = _agent_session_token(
        owner_id,
        owner_email,
        connections_mode="selected",
        scope=[("connection", "src_alpha")],
    )

    async def _inner(scope, receive, send):  # pragma: no cover - must never run
        raise AssertionError("agent-session token reached the SSE MCP app")

    sent: list[dict] = []

    async def _send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
        "query_string": b"",
    }
    asyncio.run(_AuthMiddleware(_inner)(scope, lambda: None, _send))
    assert sent[0]["status"] == 401


def test_agent_cannot_manage_owner_mcp_credentials(seeded_app):
    """Per-user MCP credentials are the OWNER's; an agent must not read,
    store, rotate, delete or live-test them — every one of those routes would
    otherwise open an upstream connection under the owner's identity."""
    owner_id, owner_email = _make_owner()
    _seed_two_sources(owner_id)
    token = _agent_session_token(
        owner_id,
        owner_email,
        connections_mode="selected",
        scope=[("connection", "src_alpha")],
    )
    client, headers = seeded_app["client"], {"Authorization": f"Bearer {token}"}

    assert client.get("/api/mcp/sources/src_alpha/my-secret", headers=headers).status_code == 403
    assert client.put("/api/mcp/sources/src_alpha/my-secret", headers=headers, json={"value": "x"}).status_code == 403
    assert client.delete("/api/mcp/sources/src_alpha/my-secret", headers=headers).status_code == 403
    assert client.post("/api/mcp/sources/src_alpha/my-secret/test", headers=headers).status_code == 403


# ---------------------------------------------------------------------------
# Part B — Store installs (the ``∪ store_installs`` outside the intersection)
# ---------------------------------------------------------------------------


def _agent_principal(agent_id="agent-store", **intersection) -> AgentPrincipal:
    return AgentPrincipal(
        session_id="chat_agent_store",
        agent_id=agent_id,
        owner_user_id="owner-1",
        owner_email="owner@example.com",
        intersection={k: frozenset(v) for k, v in intersection.items()},
    )


def _curated(slug: str, name: str) -> dict:
    return {
        "marketplace_id": slug,
        "marketplace_slug": slug,
        "original_name": name,
        "prefixed_name": f"{slug}-{name}",
        "manifest_name": name,
        "version": "1.0.0",
        "raw": {},
        "plugin_dir": Path("/nonexistent") / slug / name,
    }


def _install(entity_id: str, name: str, type_: str = "plugin") -> dict:
    return {
        "id": entity_id,
        "owner_username": "someone",
        "name": name,
        "synthetic_name": f"store-{name}",
        "type": type_,
        "version": "0.1.0",
        "description": "",
    }


@pytest.fixture
def store_stub(monkeypatch):
    """Owner is served one curated plugin (mk/p1) plus two personal Store
    installs, and owns an agent whose modes/scope the test picks."""
    import src.marketplace_filter as mf
    import src.repositories as repos

    state = {
        "installs": [_install("ent-1", "notes"), _install("ent-2", "secrets")],
        "agent": {"id": "agent-store", "plugins_mode": "all", "deleted_at": None},
        "scope": [],
    }

    monkeypatch.setattr(mf, "resolve_allowed_plugins", lambda conn, user: [_curated("mk", "p1")])
    monkeypatch.setattr(mf, "required_plugin_keys", lambda conn, user_id: {("mk", "p1")})

    class _Subs:
        def subscribed_set(self, user_id):
            return set()

    class _Installs:
        def list_for_user(self, user_id):
            return list(state["installs"])

    class _Agents:
        def get_by_id(self, agent_id):
            return state["agent"] if state["agent"] and state["agent"]["id"] == agent_id else None

        def get_scope(self, agent_id):
            return list(state["scope"])

    monkeypatch.setattr(mf, "user_curated_subscriptions_repo", lambda: _Subs())
    monkeypatch.setattr(mf, "user_store_installs_repo", lambda: _Installs())
    monkeypatch.setattr(repos, "agents_repo", lambda: _Agents())
    return state


def _served(principal) -> list[str]:
    from src.marketplace_filter import resolve_user_marketplace

    return sorted(e["original_name"] for e in resolve_user_marketplace(None, principal))


def test_all_plugins_agent_still_receives_owner_store_installs(store_stub):
    """(e) ``plugins_mode='all'`` — the common case for an agent that narrows
    on some other axis — keeps the owner's Store installs. This is the case
    the previous implementer was protecting; it must not regress."""
    store_stub["agent"]["plugins_mode"] = "all"
    assert _served(_agent_principal(marketplace_plugin={"mk/p1"})) == ["notes", "p1", "secrets"]


def test_selected_plugins_agent_does_not_receive_store_installs(store_stub):
    """(d) The bug: a ``plugins_mode='selected'`` agent whose scope names only
    a curated plugin must NOT receive the owner's personal Store installs."""
    store_stub["agent"]["plugins_mode"] = "selected"
    store_stub["scope"] = [{"item_type": "plugin", "item_id": "mk/p1"}]
    assert _served(_agent_principal(marketplace_plugin={"mk/p1"})) == ["p1"]


def test_selected_plugins_agent_receives_scoped_store_install(store_stub):
    """A Store install NAMED by the scope survives — the filter narrows, it
    does not blanket-drop. Both id forms are accepted."""
    store_stub["agent"]["plugins_mode"] = "selected"
    store_stub["scope"] = [{"item_type": "plugin", "item_id": "ent-1"}]
    assert _served(_agent_principal(marketplace_plugin={"mk/p1"})) == ["notes", "p1"]

    store_stub["scope"] = [{"item_type": "plugin", "item_id": "store/secrets"}]
    assert _served(_agent_principal(marketplace_plugin={"mk/p1"})) == ["p1", "secrets"]


def test_selected_plugins_agent_store_bundle_is_filtered_per_entity(store_stub):
    """Installed skills/agents are served as ONE synthetic ``flea`` bundle
    plugin. Scoping must reach inside it — an out-of-scope skill must not ride
    along in a bundle admitted by an in-scope sibling."""
    store_stub["installs"] = [_install("ent-1", "notes", "skill"), _install("ent-2", "secrets", "skill")]
    store_stub["agent"]["plugins_mode"] = "selected"
    store_stub["scope"] = [{"item_type": "plugin", "item_id": "ent-1"}]

    from src.marketplace_filter import resolve_user_marketplace

    served = resolve_user_marketplace(None, _agent_principal(marketplace_plugin={"mk/p1"}))
    bundles = [e for e in served if e["source"] == "store-bundle"]
    assert len(bundles) == 1
    assert bundles[0]["bundle_entity_ids"] == ["ent-1"]


def test_missing_agent_row_drops_store_installs(store_stub):
    """Fail closed: if the agent row cannot be resolved, its personal-install
    passthrough is denied rather than granted."""
    store_stub["agent"] = None
    assert _served(_agent_principal(marketplace_plugin={"mk/p1"})) == ["p1"]


def test_unrecognized_plugins_mode_drops_store_installs(store_stub):
    """Same fail-closed rule ``resolve_agent_authority`` applies to an
    unrecognized mode value."""
    store_stub["agent"]["plugins_mode"] = "bogus"
    assert _served(_agent_principal(marketplace_plugin={"mk/p1"})) == ["p1"]
