"""AgentPrincipal is a frozen, restricted auth subject (V1d)."""

import pytest


def test_agent_principal_is_frozen_and_carries_intersection():
    from app.auth.session_principal import AgentPrincipal

    p = AgentPrincipal(
        session_id="c1",
        agent_id="a1",
        owner_user_id="u1",
        owner_email="owner@example.com",
        intersection={"table": frozenset({"t1"})},
    )
    assert p.intersection["table"] == frozenset({"t1"})
    with pytest.raises(Exception):  # frozen dataclass
        p.agent_id = "a2"  # type: ignore[misc]


def test_principal_union_covers_both():
    from app.auth.session_principal import AgentPrincipal, Principal, SessionPrincipal

    agent = AgentPrincipal("c1", "a1", "u1", "o@example.com", {})
    co = SessionPrincipal("c2", ["u1"], ["o@example.com"], {})
    for p in (agent, co):
        assert isinstance(p, Principal.__args__)  # both members of the union


def test_agent_principal_caller_fields_default_none_and_are_kept_alongside_owner():
    """C2.3: caller_user_id/caller_email are new, optional, and additive —
    the owner fields must survive untouched (see the dataclass docstring:
    authority still derives from the owner/granter; only row-level access
    policies bind to the caller)."""
    from app.auth.session_principal import AgentPrincipal

    p = AgentPrincipal("c1", "a1", "u1", "o@example.com", {})
    assert p.caller_user_id is None
    assert p.caller_email is None
    assert p.owner_user_id == "u1" and p.owner_email == "o@example.com"


def test_agent_principal_carries_distinct_caller_identity():
    """A shared agent's principal carries a caller distinct from its
    owner — this is the whole point of C2.3's shared-agent runtime."""
    from app.auth.session_principal import AgentPrincipal

    p = AgentPrincipal(
        session_id="c1",
        agent_id="a1",
        owner_user_id="owner-1",
        owner_email="owner@example.com",
        intersection={},
        caller_user_id="grantee-1",
        caller_email="grantee@example.com",
    )
    assert p.owner_user_id == "owner-1" and p.caller_user_id == "grantee-1"
    assert p.owner_email == "owner@example.com" and p.caller_email == "grantee@example.com"
    with pytest.raises(Exception):  # frozen dataclass
        p.caller_user_id = "other"  # type: ignore[misc]
