"""V1d Task 4 — the three seams honor an ``AgentPrincipal``.

Task 3 made an ``AgentPrincipal`` *reach* the authorization code; this suite
pins what it is allowed to do once it gets there:

1. **Admin denial** — an agent owned by an admin must never inherit admin
   authority. ``require_admin`` hard-denies before any ``is_user_admin``
   lookup (the single most important assertion in the V1d wave).
2. **Tables** — ``get_accessible_tables`` must never return ``None`` (the
   admin "all" sentinel) for a principal, and must return exactly the
   intersection's TABLE set plus the internal tables.
3. **Marketplace/plugins** — a ``plugins_mode='selected'`` agent sees only
   the scoped plugins; an agent that leaves ``plugins_mode='all'`` (and
   narrows on some other axis) still sees its owner's served set.

Plus the crash-surface sweep: every widened seam must return/raise for an
``AgentPrincipal`` instead of blowing up on ``user["id"]``.

C2.2 note (remediation Track C, ``docs/superpowers/plans/
2026-08-26-one-agent-model.md``): every principal below is hand-built with an
explicit ``intersection=`` dict, so this suite is agnostic to HOW that dict
was computed — it pins consumption, not production. In real traffic
``.intersection`` is produced by ``resolve_agent_authority``
(``src/agent_scope_intersection.py``, replacing ``compute_agent_intersection``)
rather than an owner-only lookup; the D-C2 admin/self-granted split it
implements is covered where it is actually produced
(``tests/test_agent_scope_intersection.py``,
``tests/db_pg/test_resolve_agent_authority_pg.py``), not here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from app.auth.session_principal import AgentPrincipal, SessionPrincipal
from app.resource_types import ResourceType


def _agent_principal(**intersection) -> AgentPrincipal:
    return AgentPrincipal(
        session_id="chat_agent_seam",
        agent_id="agent-1",
        owner_user_id="owner-1",
        owner_email="owner@example.com",
        intersection={k: frozenset(v) for k, v in intersection.items()},
    )


def _session_principal(**intersection) -> SessionPrincipal:
    return SessionPrincipal(
        session_id="chat_co_seam",
        participant_user_ids=["u1", "u2"],
        participant_emails=["a@example.com", "b@example.com"],
        intersection={k: frozenset(v) for k, v in intersection.items()},
    )


# ---------------------------------------------------------------------------
# Seam 1 — admin denial
# ---------------------------------------------------------------------------


def test_admin_owned_agent_principal_is_denied_admin(monkeypatch):
    """An agent whose OWNER is an admin must never reach an admin endpoint.

    The denial has to happen BEFORE the ``is_user_admin`` lookup — if the
    lookup ran with the owner's id it would return True and the agent would
    inherit god-mode. ``is_user_admin`` is stubbed to fail the test loudly
    if it is ever consulted.
    """
    import app.auth.access as access

    monkeypatch.setattr(
        access,
        "is_user_admin",
        lambda *a, **k: pytest.fail("is_user_admin must not be consulted for an AgentPrincipal"),
    )

    with pytest.raises(HTTPException) as exc:
        access.require_admin(user=_agent_principal(), conn=None)
    assert exc.value.status_code == 403


def test_require_resource_access_uses_intersection_for_agent_principal(monkeypatch):
    """The entity-scoped gate must route an AgentPrincipal through
    ``can_access_session`` (intersection membership), never through
    ``can_access`` (which would resolve the owner's groups)."""
    import app.auth.access as access
    from unittest.mock import MagicMock

    monkeypatch.setattr(
        access,
        "can_access",
        lambda *a, **k: pytest.fail("can_access must not be consulted for an AgentPrincipal"),
    )

    dep = access.require_resource_access(ResourceType.MEMORY_DOMAIN, "{domain_id}")
    request = MagicMock()
    request.path_params = {"domain_id": "md_ops"}

    granted = _agent_principal(memory_domain={"md_ops"})
    assert dep(request=request, user=granted, conn=None) is granted

    denied = _agent_principal(memory_domain={"md_finance"})
    with pytest.raises(HTTPException) as exc:
        dep(request=request, user=denied, conn=None)
    assert exc.value.status_code == 403


def test_memory_privileged_viewer_denies_agent_principal(monkeypatch):
    import app.api.memory as memory

    monkeypatch.setattr(
        memory,
        "is_user_admin",
        lambda *a, **k: pytest.fail("is_user_admin must not be consulted for an AgentPrincipal"),
    )
    assert memory._is_privileged_viewer(_agent_principal(), None) is False
    assert memory._effective_groups(_agent_principal(), None) == []


def test_deny_principal_denies_agent_principal():
    from app.chat.session_principal_guard import deny_principal

    with pytest.raises(HTTPException) as exc:
        deny_principal(_agent_principal())
    assert exc.value.status_code == 403
    # co-session behaviour unchanged
    with pytest.raises(HTTPException):
        deny_principal(_session_principal())
    assert deny_principal({"id": "u1"}) is None


def test_stack_management_rejects_agent_principal():
    from app.api.stack import _reject_co_session

    with pytest.raises(HTTPException) as exc:
        _reject_co_session(_agent_principal())
    assert exc.value.status_code == 403


def test_agent_runtime_and_sessions_reject_agent_principal():
    """Owner-scoped `/api/v1/agents/...` surfaces need a real owner
    credential — an agent-session sandbox token is not one."""
    import asyncio
    from unittest.mock import MagicMock

    import app.api.agent_runtime as agent_runtime
    import app.api.agent_sessions as agent_sessions

    with pytest.raises(HTTPException) as exc:
        agent_runtime.require_agent_runtime_principal(slug="s", request=MagicMock(), user=_agent_principal(), conn=None)
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc:
        asyncio.run(agent_runtime.get_agent_job(job_id="j", request=MagicMock(), user=_agent_principal()))
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        agent_sessions.require_session_principal(
            session_id="s", request=MagicMock(), user=_agent_principal(), conn=None
        )
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Seam 2 — tables
# ---------------------------------------------------------------------------


def test_get_accessible_tables_never_returns_all_for_agent_principal():
    """``None`` is the admin 'all' sentinel — a principal must get a
    concrete list, even when its intersection is empty."""
    from src.rbac import get_accessible_tables

    assert get_accessible_tables(_agent_principal()) is not None
    assert get_accessible_tables(_agent_principal(table={"t1"})) is not None


def test_get_accessible_tables_returns_intersection_for_agent_principal():
    from connectors.internal.access import INTERNAL_TABLES
    from src.rbac import get_accessible_tables

    result = get_accessible_tables(_agent_principal(table={"t1"}))
    assert "t1" in result
    assert "t2" not in result
    for t in INTERNAL_TABLES:
        assert t.registry_id in result
    # exactly the intersection + internal tables, nothing else
    assert set(result) == {"t1"} | {t.registry_id for t in INTERNAL_TABLES}


def test_can_access_table_uses_intersection_for_agent_principal(monkeypatch):
    import app.auth.access as access
    from src.rbac import can_access_table

    monkeypatch.setattr(
        access,
        "is_user_admin",
        lambda *a, **k: pytest.fail("is_user_admin must not be consulted for an AgentPrincipal"),
    )
    p = _agent_principal(table={"t1"})
    assert can_access_table(p, "t1") is True
    assert can_access_table(p, "t2") is False


def test_get_accessible_ids_never_returns_all_for_agent_principal():
    from src.rbac import get_accessible_ids

    p = _agent_principal(recipe={"r1"})
    assert get_accessible_ids(p, ResourceType.RECIPE.value) == frozenset({"r1"})
    assert get_accessible_ids(p, ResourceType.COLLECTION.value) == frozenset()


def test_stack_resolver_accepts_agent_principal(monkeypatch):
    from app.services.stack_resolver import StackResolver

    resolver = StackResolver.__new__(StackResolver)
    captured = {}

    def _fetch(resource_type, effective_ids, required_ids):
        captured["ids"] = set(effective_ids)
        return []

    monkeypatch.setattr(resolver, "_fetch_entries", _fetch, raising=False)
    monkeypatch.setattr(
        resolver,
        "_user_group_ids",
        lambda *a, **k: pytest.fail("group lookup must not run for an AgentPrincipal"),
        raising=False,
    )

    resolver.stack(_agent_principal(data_package={"dp1"}), ResourceType.DATA_PACKAGE)
    assert captured["ids"] == {"dp1"}


# ---------------------------------------------------------------------------
# Seam 3 — marketplace / plugins
# ---------------------------------------------------------------------------


def _entry(slug: str, name: str) -> dict:
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


@pytest.fixture
def marketplace_stub(monkeypatch):
    """Owner holds grants on mk/p1 + mk/p2, both in-stack, no Store installs."""
    import src.marketplace_filter as mf

    state = {
        "granted": [_entry("mk", "p1"), _entry("mk", "p2")],
        "installs": [],
    }

    monkeypatch.setattr(mf, "resolve_allowed_plugins", lambda conn, user: list(state["granted"]))
    monkeypatch.setattr(mf, "required_plugin_keys", lambda conn, user_id: {("mk", "p1"), ("mk", "p2")})

    class _Subs:
        def subscribed_set(self, user_id):
            return set()

    class _Installs:
        def list_for_user(self, user_id):
            return list(state["installs"])

    monkeypatch.setattr(mf, "user_curated_subscriptions_repo", lambda: _Subs())
    monkeypatch.setattr(mf, "user_store_installs_repo", lambda: _Installs())
    return state


def test_marketplace_selected_agent_sees_only_scoped_plugins(marketplace_stub):
    from src.marketplace_filter import resolve_user_marketplace

    p = _agent_principal(marketplace_plugin={"mk/p1"})
    served = resolve_user_marketplace(None, p)
    assert [e["original_name"] for e in served] == ["p1"]


def test_marketplace_all_plugins_agent_sees_owner_set(marketplace_stub):
    """An agent that narrows on some OTHER axis keeps plugins_mode='all',
    so its intersection is the owner's full marketplace_plugin set — it must
    still receive everything the owner is served."""
    from src.marketplace_filter import resolve_user_marketplace

    p = _agent_principal(marketplace_plugin={"mk/p1", "mk/p2"}, table={"t1"})
    served = resolve_user_marketplace(None, p)
    assert sorted(e["original_name"] for e in served) == ["p1", "p2"]


def test_marketplace_empty_scope_agent_sees_nothing(marketplace_stub):
    from src.marketplace_filter import resolve_user_marketplace

    assert resolve_user_marketplace(None, _agent_principal()) == []


def test_marketplace_serve_path_does_not_crash_on_principal(marketplace_stub):
    """The seam only bites end-to-end if `/marketplace.zip` + `/marketplace/info`
    can actually render for a principal — both build a diagnostic payload from
    ``user.get("id")`` / ``resolve_user_groups``, which a frozen dataclass
    cannot answer."""
    from app.marketplace_server import packager
    from src.marketplace_filter import resolve_user_groups

    p = _agent_principal(marketplace_plugin={"mk/p1"})

    assert resolve_user_groups(None, p) == []
    assert resolve_user_groups(None, _session_principal()) == []

    info = packager.build_info(None, p)
    # Diagnostic identity is the owner; authority still came from the
    # intersection, so `groups` is empty.
    assert info["user_id"] == "owner-1"
    assert info["email"] == "owner@example.com"
    assert info["groups"] == []
    assert [e["original_name"] for e in info["plugins"]] == ["p1"]

    # A co-session names no single participant.
    co_info = packager.build_info(None, _session_principal())
    assert co_info["user_id"] is None
    assert co_info["email"] is None


def test_marketplace_agent_cannot_exceed_owner_served_set(marketplace_stub):
    """A scope row naming a plugin the OWNER is not served must never
    surface — the agent is a restriction of its owner, never an elevation."""
    from src.marketplace_filter import resolve_user_marketplace

    p = _agent_principal(marketplace_plugin={"mk/p1", "other/secret"})
    served = resolve_user_marketplace(None, p)
    assert [e["original_name"] for e in served] == ["p1"]
