"""SSE / Streamable-HTTP passthrough closures enforce the same gate stack as REST.

The server-hosted MCP transports (``app/api/mcp_http.py`` SSE and
``app/api/mcp_streamable.py`` Streamable-HTTP) register passthrough tool
closures via ``app/api/mcp/tools_generator.register_passthrough_tools``. Those
closures used to call ``call_tool_async`` directly, bypassing the RBAC + policy
gates the REST endpoint (``invoke_passthrough_tool``) enforces: per-group grant
visibility, the mutating gate, and the per-(tool,user) rate limit.

These tests drive the synthesized closures with different caller identities and
assert the gate now fires — a non-granted caller cannot reach the upstream, a
mutating tool is admin-only, the rate limit trips, PII is redacted, and an
unresolved caller identity fails closed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("mcp", reason="mcp SDK not installed")

from connectors.mcp.client import ToolCallResult
from mcp.server.fastmcp import FastMCP
from src.db import get_system_db
from src.repositories.mcp_sources import MCPSourceRepository
from src.repositories.tool_registry import PASSTHROUGH, ToolRegistryRepository
from src.repositories.user_group_members import UserGroupMembersRepository
from src.repositories.user_groups import UserGroupsRepository


# ── seeding helpers ──────────────────────────────────────────────────────────


def _seed_tool(
    *,
    tool_id: str = "up.lookup",
    original_name: str = "lookup",
    exposed_name: str = "lookup",
    grant_to_analyst: bool = True,
    mutating: bool = False,
    allow_mutating: bool = False,
    rate_limit_pm=None,
    pii_fields=None,
    analyst_id: str = "analyst1",
) -> None:
    conn = get_system_db()
    sources = MCPSourceRepository(conn)
    tools = ToolRegistryRepository(conn)
    groups = UserGroupsRepository(conn)
    members = UserGroupMembersRepository(conn)

    sources.upsert(id="src_up", name="up", transport="stdio", command="/bin/true", args=[])
    tools.upsert(
        tool_id=tool_id,
        source_id="src_up",
        original_name=original_name,
        exposed_name=exposed_name,
        mode=PASSTHROUGH,
        description="test",
        mutating=mutating,
        rate_limit_pm=rate_limit_pm,
        pii_fields=pii_fields,
    )
    grp = groups.create(name=f"grp-{tool_id}", description=None)
    if grant_to_analyst:
        tools.add_grant(tool_id, grp["id"], allow_mutating=allow_mutating)
        members.add_member(analyst_id, grp["id"], source="system_seed")
    conn.close()


def _closure(exposed_name: str, caller_id_fn):
    """Register passthrough tools on a fresh FastMCP and return the closure fn."""
    from app.api.mcp.tools_generator import register_passthrough_tools

    mcp = FastMCP("Test", instructions="t")
    register_passthrough_tools(mcp, caller_id_fn=caller_id_fn)
    return mcp._tool_manager.get_tool(exposed_name).fn


def _patch_upstream(text="ok", data=None, is_error=False):
    return patch(
        "app.api.mcp.tools_generator.call_tool_async",
        new=AsyncMock(return_value=ToolCallResult(text=text, data=data, is_error=is_error)),
    )


# ── grant gate ───────────────────────────────────────────────────────────────


def test_granted_analyst_reaches_upstream(seeded_app):
    _seed_tool(grant_to_analyst=True)
    fn = _closure("lookup", caller_id_fn=lambda: "analyst1")
    with _patch_upstream(text="hit") as mock:
        out = asyncio.run(fn())
    assert out == "hit"
    mock.assert_awaited_once()


def test_non_granted_analyst_denied_and_no_forward(seeded_app):
    _seed_tool(tool_id="up.private", exposed_name="private", grant_to_analyst=False)
    fn = _closure("private", caller_id_fn=lambda: "analyst1")
    with _patch_upstream(text="LEAK") as mock:
        with pytest.raises(RuntimeError, match="no grant"):
            asyncio.run(fn())
    mock.assert_not_called()


def test_admin_reaches_upstream_without_grant(seeded_app):
    _seed_tool(tool_id="up.private2", exposed_name="private2", grant_to_analyst=False)
    fn = _closure("private2", caller_id_fn=lambda: "admin1")
    with _patch_upstream(text="ok") as mock:
        out = asyncio.run(fn())
    assert out == "ok"
    mock.assert_awaited_once()


def test_unresolved_caller_fails_closed(seeded_app):
    """caller_id_fn returning None (bad/absent token) → non-admin, no groups →
    grant check fails closed; upstream never reached."""
    _seed_tool(grant_to_analyst=True)
    fn = _closure("lookup", caller_id_fn=lambda: None)
    with _patch_upstream(text="LEAK") as mock:
        with pytest.raises(RuntimeError, match="no grant"):
            asyncio.run(fn())
    mock.assert_not_called()


# ── source gate (TCRD-236) ───────────────────────────────────────────────────


def _narrow_source_to_group(source_id: str, group_id: str) -> None:
    """Give ``source_id`` its FIRST ResourceType.MCP_SOURCE grant, restricted
    to ``group_id`` — this is what an admin narrowing a server on
    /admin/access does, and it is what flips the source from "ungated
    (visible to everyone)" to "gated (visible only to explicitly granted
    groups)"."""
    from src.repositories import resource_grants_repo

    resource_grants_repo().ensure_grant(group_id=group_id, resource_type="mcp_source", resource_id=source_id)


def test_narrowed_source_denies_tool_granted_caller_outside_it(seeded_app):
    """A caller whose group holds the TOOL grant is still refused once an
    admin narrows the SOURCE to a different group — the mcp_source gate is
    ANDed with tool_grants, not replaced by it."""
    _seed_tool(tool_id="up.narrowed", exposed_name="narrowed", grant_to_analyst=True)
    other_grp = UserGroupsRepository(get_system_db()).create(name="other-narrow-grp", description=None)
    _narrow_source_to_group("src_up", other_grp["id"])

    fn = _closure("narrowed", caller_id_fn=lambda: "analyst1")
    with _patch_upstream(text="LEAK") as mock:
        with pytest.raises(RuntimeError, match="no grant on source"):
            asyncio.run(fn())
    mock.assert_not_called()


def test_narrowed_source_allows_member_of_the_granted_group(seeded_app):
    """The tool_grants intersection AND the mcp_source grant both point at
    the same group -> the call goes through."""
    conn = get_system_db()
    sources = MCPSourceRepository(conn)
    tools = ToolRegistryRepository(conn)
    groups = UserGroupsRepository(conn)
    members = UserGroupMembersRepository(conn)

    sources.upsert(id="src_narrow2", name="narrow2", transport="stdio", command="/bin/true", args=[])
    tools.upsert(
        tool_id="narrow2.lookup",
        source_id="src_narrow2",
        original_name="lookup",
        exposed_name="narrowed2",
        mode=PASSTHROUGH,
        description="test",
    )
    grp = groups.create(name="narrow2-grp", description=None)
    tools.add_grant("narrow2.lookup", grp["id"])
    members.add_member("analyst1", grp["id"], source="system_seed")
    conn.close()

    _narrow_source_to_group("src_narrow2", grp["id"])

    fn = _closure("narrowed2", caller_id_fn=lambda: "analyst1")
    with _patch_upstream(text="ok") as mock:
        out = asyncio.run(fn())
    assert out == "ok"
    mock.assert_awaited_once()


def test_admin_bypasses_narrowed_source(seeded_app):
    _seed_tool(tool_id="up.narrowadmin", exposed_name="narrowadmin", grant_to_analyst=False)
    other_grp = UserGroupsRepository(get_system_db()).create(name="other-narrow-admin-grp", description=None)
    _narrow_source_to_group("src_up", other_grp["id"])

    fn = _closure("narrowadmin", caller_id_fn=lambda: "admin1")
    with _patch_upstream(text="ok") as mock:
        out = asyncio.run(fn())
    assert out == "ok"
    mock.assert_awaited_once()


def test_ungated_source_stays_visible_by_default(seeded_app):
    """A source nobody has ever granted at the mcp_source level (the common
    case today) is treated as visible-to-everyone — this is the
    backward-compat default the whole feature exists to preserve."""
    _seed_tool(tool_id="up.ungated", exposed_name="ungated", grant_to_analyst=True)
    fn = _closure("ungated", caller_id_fn=lambda: "analyst1")
    with _patch_upstream(text="ok") as mock:
        out = asyncio.run(fn())
    assert out == "ok"
    mock.assert_awaited_once()


# ── mutating gate ──────────────────────────────────────────────────────────


def test_mutating_blocked_for_non_admin(seeded_app):
    _seed_tool(tool_id="up.del", exposed_name="del", mutating=True, grant_to_analyst=True)
    fn = _closure("del", caller_id_fn=lambda: "analyst1")
    with _patch_upstream(text="LEAK") as mock:
        with pytest.raises(RuntimeError, match="mutating"):
            asyncio.run(fn())
    mock.assert_not_called()


def test_mutating_allowed_for_admin(seeded_app):
    _seed_tool(tool_id="up.del2", exposed_name="del2", mutating=True, grant_to_analyst=False)
    fn = _closure("del2", caller_id_fn=lambda: "admin1")
    with _patch_upstream(text="deleted") as mock:
        out = asyncio.run(fn())
    assert out == "deleted"
    mock.assert_awaited_once()


# ── rate limit gate ─────────────────────────────────────────────────────────


def test_rate_limit_trips_after_cap(seeded_app):
    from app.api.mcp_policy import reset_rate_buckets_for_tests

    reset_rate_buckets_for_tests()
    _seed_tool(tool_id="up.search", exposed_name="search", rate_limit_pm=2, grant_to_analyst=True)
    fn = _closure("search", caller_id_fn=lambda: "analyst1")
    with _patch_upstream(text="ok"):
        asyncio.run(fn())
        asyncio.run(fn())
        with pytest.raises(RuntimeError, match="rate limit"):
            asyncio.run(fn())
    reset_rate_buckets_for_tests()


# ── PII redaction parity ─────────────────────────────────────────────────────


def test_pii_redacted_in_closure_output(seeded_app):
    _seed_tool(tool_id="up.pii", exposed_name="piilookup", pii_fields=["email"], grant_to_analyst=True)
    fn = _closure("piilookup", caller_id_fn=lambda: "analyst1")
    with _patch_upstream(text='{"email": "a@x", "name": "Alice"}', data={"email": "a@x", "name": "Alice"}):
        out = asyncio.run(fn())
    assert "a@x" not in out
    assert "[REDACTED]" in out
    assert "Alice" in out


# ── per-user credential fail-closed parity ───────────────────────────────────


def test_per_user_source_without_credential_fails_closed(seeded_app):
    """A granted caller on a scope='per_user' source with NO personal credential
    is refused with the my-secret remedy, and the upstream is never called —
    matching the REST endpoint's fail-closed guard."""
    conn = get_system_db()
    sources = MCPSourceRepository(conn)
    tools = ToolRegistryRepository(conn)
    groups = UserGroupsRepository(conn)
    members = UserGroupMembersRepository(conn)
    sources.upsert(
        id="src_pu",
        name="pu-up",
        transport="http",
        url="https://upstream.example/mcp",
        auth_method="bearer",
        scope="per_user",
    )
    tools.upsert(
        tool_id="pu-up.lookup",
        source_id="src_pu",
        original_name="lookup",
        exposed_name="pulookup",
        mode=PASSTHROUGH,
        description="per-user, granted, no personal secret",
    )
    grp = groups.create(name="pu-grp", description=None)
    tools.add_grant("pu-up.lookup", grp["id"])
    members.add_member("analyst1", grp["id"], source="system_seed")
    conn.close()

    fn = _closure("pulookup", caller_id_fn=lambda: "analyst1")
    with _patch_upstream(text="LEAK") as mock:
        with pytest.raises(RuntimeError, match="my-secret"):
            asyncio.run(fn())
    mock.assert_not_called()


# ── live source re-fetch (freshness parity with REST) ────────────────────────


def test_source_disabled_after_registration_fails_closed(seeded_app):
    """The closure re-fetches the source row per call, so disabling the source
    after registration is reflected immediately (no restart) — the upstream is
    never called, matching the REST endpoint's per-call source fetch."""
    _seed_tool(tool_id="up.fresh", exposed_name="freshtool", grant_to_analyst=True)
    fn = _closure("freshtool", caller_id_fn=lambda: "analyst1")

    # Disable the source AFTER the closure was registered.
    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id="src_up", name="up", transport="stdio", command="/bin/true", args=[], enabled=False
    )
    conn.close()

    with _patch_upstream(text="LEAK") as mock:
        with pytest.raises(RuntimeError, match="missing or disabled"):
            asyncio.run(fn())
    mock.assert_not_called()


# ── source url runtime-policy gate (#1216 part 2) ───────────────────────────


def _seed_http_tool(
    *,
    source_id: str = "src_url_http",
    tool_id: str = "up-http.lookup",
    exposed_name: str = "lookup_http",
    url: str = "https://mcp.vendor.example/mcp",
    analyst_id: str = "analyst1",
) -> None:
    """Seed an http-transport source + one passthrough tool granted to the
    analyst's group, for exercising the url-policy runtime gate."""
    conn = get_system_db()
    sources = MCPSourceRepository(conn)
    tools = ToolRegistryRepository(conn)
    groups = UserGroupsRepository(conn)
    members = UserGroupMembersRepository(conn)

    sources.upsert(id=source_id, name=source_id, transport="http", url=url, auth_method="bearer")
    tools.upsert(
        tool_id=tool_id,
        source_id=source_id,
        original_name="lookup",
        exposed_name=exposed_name,
        mode=PASSTHROUGH,
        description="url policy runtime gate test",
    )
    grp = groups.create(name=f"grp-{tool_id}", description=None)
    tools.add_grant(tool_id, grp["id"])
    members.add_member(analyst_id, grp["id"], source="system_seed")
    conn.close()


def test_switch_off_by_default_a_refused_url_still_forwards(seeded_app):
    """Pin the default: ``mcp.source_url_runtime_enforce`` is OFF, so a row
    whose url the CURRENT policy would refuse (the #1154 metadata-endpoint
    shape) keeps forwarding exactly as it does today — the gap #1216 exists
    to close, unchanged until an admin opts in."""
    _seed_http_tool(url="http://169.254.169.254/mcp")
    fn = _closure("lookup_http", caller_id_fn=lambda: "analyst1")
    with _patch_upstream(text="ok") as mock:
        out = asyncio.run(fn())
    assert out == "ok"
    mock.assert_awaited_once()


def test_switch_on_a_refused_url_blocks_and_never_dials(seeded_app, monkeypatch):
    import app.instance_config as ic

    monkeypatch.setattr(ic, "get_mcp_source_url_runtime_enforce", lambda: True)
    _seed_http_tool(
        source_id="src_url_refused",
        tool_id="up-http.refused",
        exposed_name="refused_http",
        url="http://169.254.169.254/mcp",
    )
    fn = _closure("refused_http", caller_id_fn=lambda: "analyst1")
    with _patch_upstream(text="LEAK") as mock:
        with pytest.raises(RuntimeError, match="Ask an admin") as excinfo:
            asyncio.run(fn())
    # The tool caller is an MCP client, never an admin console: the verdict
    # (which embeds the source's literal address) goes to the log only, the
    # same line the analyst-reachable url-policy gate draws (Devin Review on
    # PR #1301).
    assert "169.254.169.254" not in str(excinfo.value)
    assert "blocked_range" not in str(excinfo.value)
    mock.assert_not_called()


def test_switch_on_a_clean_url_still_forwards(seeded_app, monkeypatch):
    import app.instance_config as ic

    monkeypatch.setattr(ic, "get_mcp_source_url_runtime_enforce", lambda: True)
    _seed_http_tool(
        source_id="src_url_clean",
        tool_id="up-http.clean",
        exposed_name="clean_http",
        url="https://mcp.vendor.example/mcp",
    )
    fn = _closure("clean_http", caller_id_fn=lambda: "analyst1")
    with _patch_upstream(text="ok") as mock:
        out = asyncio.run(fn())
    assert out == "ok"
    mock.assert_awaited_once()


def test_switch_on_stdio_source_is_exempt(seeded_app, monkeypatch):
    """stdio never dials ``url`` — it stays exempt from this policy even with
    the switch on, the same exemption every other caller of the policy applies."""
    import app.instance_config as ic

    monkeypatch.setattr(ic, "get_mcp_source_url_runtime_enforce", lambda: True)
    _seed_tool(tool_id="up.stdio_url_exempt", exposed_name="stdio_url_exempt", grant_to_analyst=True)
    fn = _closure("stdio_url_exempt", caller_id_fn=lambda: "analyst1")
    with _patch_upstream(text="ok") as mock:
        out = asyncio.run(fn())
    assert out == "ok"
    mock.assert_awaited_once()


# ── shared gate unit ─────────────────────────────────────────────────────────


def test_enforce_passthrough_access_shared_by_rest_and_transports(seeded_app):
    """The extracted gate raises the typed exceptions both paths map from."""
    from app.api.mcp_policy import GrantDenied, MutatingNotAllowed, enforce_passthrough_access

    _seed_tool(tool_id="up.g", exposed_name="g", grant_to_analyst=True)
    conn = get_system_db()
    tool = ToolRegistryRepository(conn).get("up.g")
    conn.close()

    # Granted analyst passes.
    enforce_passthrough_access(tool, "analyst1")
    # Unknown caller fails closed.
    with pytest.raises(GrantDenied):
        enforce_passthrough_access(tool, "nobody")
    # Admin short-circuits grant.
    enforce_passthrough_access(tool, "admin1")
    # Mutating tool blocks the granted non-admin (plain grant = read-only).
    tool["mutating"] = True
    with pytest.raises(MutatingNotAllowed):
        enforce_passthrough_access(tool, "analyst1")


# ── mutating opt-in grant (tool_grants.allow_mutating, v120) ────────────────


def test_mutating_tool_passes_with_allow_mutating_grant(seeded_app):
    """A group grant carrying allow_mutating=TRUE opens a mutating tool to a
    non-admin member — the reserved `mutating_grant` evolution."""
    _seed_tool(
        tool_id="up.mut_ok",
        exposed_name="mut_ok",
        mutating=True,
        grant_to_analyst=True,
        allow_mutating=True,
    )
    fn = _closure("mut_ok", caller_id_fn=lambda: "analyst1")
    with _patch_upstream(text="wrote") as mock:
        out = asyncio.run(fn())
    assert out == "wrote"
    mock.assert_awaited_once()


def test_mutating_tool_still_blocked_on_plain_grant(seeded_app):
    """The default stays read-only: a plain grant does not open a mutating
    tool, and the upstream is never dialed."""
    _seed_tool(
        tool_id="up.mut_ro",
        exposed_name="mut_ro",
        mutating=True,
        grant_to_analyst=True,
        allow_mutating=False,
    )
    fn = _closure("mut_ro", caller_id_fn=lambda: "analyst1")
    with _patch_upstream(text="LEAK") as mock:
        with pytest.raises(RuntimeError, match="mutating"):
            asyncio.run(fn())
    mock.assert_not_called()


def _agent_principal_for(owner_user_id: str, *, connections_mode: str = "all"):
    """A live AgentPrincipal whose agent row really exists (connection-scope
    gate reads agent_scope through the agents table)."""
    from app.auth.session_principal import AgentPrincipal
    from src.repositories import agents_repo

    agent_id = f"agent-{owner_user_id}-{connections_mode}"
    if agents_repo().get_by_id(agent_id) is None:
        agents_repo().create(
            id=agent_id,
            owner_user_id=owner_user_id,
            name="t",
            slug=agent_id,
            connections_mode=connections_mode,
        )
    return AgentPrincipal(
        session_id="sess-t",
        agent_id=agent_id,
        owner_user_id=owner_user_id,
        owner_email=f"{owner_user_id}@example.com",
        intersection={},
    )


def test_agent_principal_rides_owner_mutating_grant(seeded_app):
    """An AgentPrincipal resolves to its OWNER's groups: the owner's
    allow_mutating grant is the agent's ceiling — and suffices."""
    from app.api.mcp_policy import enforce_passthrough_access

    _seed_tool(
        tool_id="up.mut_agent",
        exposed_name="mut_agent",
        mutating=True,
        grant_to_analyst=True,
        allow_mutating=True,
    )
    conn = get_system_db()
    tool = ToolRegistryRepository(conn).get("up.mut_agent")
    conn.close()
    enforce_passthrough_access(tool, _agent_principal_for("analyst1"))


def test_agent_principal_blocked_without_owner_mutating_grant(seeded_app):
    """Owner has only a plain (read-only) grant → the agent is blocked, and
    an admin-owned agent gets no admin short-circuit (is_admin pinned False)."""
    from app.api.mcp_policy import MutatingNotAllowed, enforce_passthrough_access

    _seed_tool(
        tool_id="up.mut_agent_ro",
        exposed_name="mut_agent_ro",
        mutating=True,
        grant_to_analyst=True,
        allow_mutating=False,
    )
    conn = get_system_db()
    tool = ToolRegistryRepository(conn).get("up.mut_agent_ro")
    conn.close()
    with pytest.raises(MutatingNotAllowed):
        enforce_passthrough_access(tool, _agent_principal_for("analyst1"))
    # Admin-owned agent: owner's groups have no grant at all → GrantDenied
    # before the mutating gate is even reached; the point is it does NOT pass.
    from app.api.mcp_policy import GrantDenied

    with pytest.raises((GrantDenied, MutatingNotAllowed)):
        enforce_passthrough_access(tool, _agent_principal_for("admin1"))


def test_enforce_source_url_runtime_policy_shared_by_rest_and_transports(monkeypatch):
    """The extracted gate (#1216) both `_forward_with_gates` and the REST
    endpoint call — pin its own contract directly, independent of the seams."""
    import app.instance_config as ic
    from app.api.mcp_policy import SourceUrlRefused, enforce_source_url_runtime_policy

    refused = {"id": "s1", "name": "s1", "transport": "http", "url": "http://169.254.169.254/mcp"}

    # Off by default: no-op, no exception, even on a refused url.
    monkeypatch.setattr(ic, "get_mcp_source_url_runtime_enforce", lambda: False)
    enforce_source_url_runtime_policy(refused)

    # On: refused url raises, carrying the reason/switch/admin-report facts
    # an operator needs to route themselves without reading source.
    monkeypatch.setattr(ic, "get_mcp_source_url_runtime_enforce", lambda: True)
    with pytest.raises(SourceUrlRefused) as exc_info:
        enforce_source_url_runtime_policy(refused)
    exc = exc_info.value
    assert "blocked_range" in exc.reason
    assert exc.switch == "mcp.source_url_runtime_enforce"
    assert "would_refuse" in exc.admin_report_hint

    # A clean url is a no-op even with the switch on.
    enforce_source_url_runtime_policy({"id": "s2", "transport": "http", "url": "https://mcp.vendor.example/mcp"})

    # stdio is exempt regardless of the switch or the url's shape.
    enforce_source_url_runtime_policy({"id": "s3", "transport": "stdio", "url": "http://169.254.169.254/mcp"})
