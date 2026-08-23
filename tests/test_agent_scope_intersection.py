"""compute_agent_intersection: owner ∩ agent scope, fail-closed (V1d)."""

import pytest


def _agent(**over):
    row = {
        "id": "a1",
        "owner_user_id": "u1",
        "tables_mode": "all",
        "plugins_mode": "all",
        "connections_mode": "all",
        "memory_mode": "all",
    }
    row.update(over)
    return row


def test_all_mode_returns_owner_set_verbatim(monkeypatch):
    import src.agent_scope_intersection as mod

    monkeypatch.setattr(mod, "_allowed_ids_for_user", lambda uid, rt, conn=None: frozenset({"t1", "t2"}))
    monkeypatch.setattr(mod, "_agent_scope_ids", lambda aid, it, conn=None: frozenset())
    out = mod.compute_agent_intersection("u1", _agent())
    assert out["table"] == frozenset({"t1", "t2"})


def test_selected_mode_narrows_to_subset(monkeypatch):
    import src.agent_scope_intersection as mod

    monkeypatch.setattr(mod, "_allowed_ids_for_user", lambda uid, rt, conn=None: frozenset({"t1", "t2", "t3"}))
    monkeypatch.setattr(
        mod, "_agent_scope_ids", lambda aid, it, conn=None: frozenset({"t2"}) if it == "table" else frozenset()
    )
    out = mod.compute_agent_intersection("u1", _agent(tables_mode="selected"))
    assert out["table"] == frozenset({"t2"})


def test_agent_can_never_widen_beyond_owner(monkeypatch):
    """A scope row naming a table the OWNER lacks must not appear."""
    import src.agent_scope_intersection as mod

    monkeypatch.setattr(mod, "_allowed_ids_for_user", lambda uid, rt, conn=None: frozenset({"t1"}))
    monkeypatch.setattr(mod, "_agent_scope_ids", lambda aid, it, conn=None: frozenset({"t1", "SECRET"}))
    out = mod.compute_agent_intersection("u1", _agent(tables_mode="selected"))
    assert out["table"] == frozenset({"t1"})
    assert "SECRET" not in out["table"]


def test_unrecognized_mode_fails_closed(monkeypatch):
    import src.agent_scope_intersection as mod

    monkeypatch.setattr(mod, "_allowed_ids_for_user", lambda uid, rt, conn=None: frozenset({"t1"}))
    monkeypatch.setattr(mod, "_agent_scope_ids", lambda aid, it, conn=None: frozenset({"t1"}))
    out = mod.compute_agent_intersection("u1", _agent(tables_mode="bogus"))
    assert out.get("table", frozenset()) == frozenset()


@pytest.mark.parametrize("owner,agent_row", [("", _agent()), ("u1", None), ("u1", {})])
def test_missing_inputs_deny_everything(owner, agent_row):
    from src.agent_scope_intersection import compute_agent_intersection

    assert compute_agent_intersection(owner, agent_row) == {}


def test_agent_narrows_flag():
    from src.agent_scope_intersection import agent_narrows

    assert agent_narrows(_agent()) is False
    assert agent_narrows(_agent(plugins_mode="selected")) is True


# ---------------------------------------------------------------------------
# The data axis beyond bare tables: data_package + collection rows, both
# governed by tables_mode (the builder's "Knowledge" section sells packages
# and file collections, not individual table ids).
# ---------------------------------------------------------------------------


def _rt_aware(mapping):
    """`_allowed_ids_for_user` stand-in keyed by resource_type."""
    return lambda uid, rt, conn=None: frozenset(mapping.get(rt, frozenset()))


def _scope_aware(mapping):
    """`_agent_scope_ids` stand-in keyed by item_type."""
    return lambda aid, it, conn=None: frozenset(mapping.get(it, frozenset()))


def test_selected_tables_expand_declared_packages(monkeypatch):
    """An agent declaring a data package reaches that package's member
    tables (live expansion), and the DATA_PACKAGE axis narrows with it."""
    import src.agent_scope_intersection as mod

    monkeypatch.setattr(mod, "_allowed_ids_for_user", _rt_aware({"table": {"t1"}, "data_package": {"P1", "P2"}}))
    monkeypatch.setattr(mod, "_owner_package_ids", lambda uid, conn=None: frozenset({"P1", "P2"}))
    monkeypatch.setattr(mod, "_agent_scope_ids", _scope_aware({"data_package": {"P1"}}))
    monkeypatch.setattr(
        mod,
        "_package_table_ids",
        lambda pkg_ids, conn=None: frozenset({"t2"}) if "P1" in pkg_ids else frozenset(),
    )
    monkeypatch.setattr(mod, "_owned_collection_ids", lambda uid: frozenset())
    out = mod.compute_agent_intersection("u1", _agent(tables_mode="selected"))
    assert out["table"] == frozenset({"t2"})
    assert out["data_package"] == frozenset({"P1"})


def test_owner_package_stack_feeds_table_axis(monkeypatch):
    """An owner whose ONLY table reach is via a data package (no per-table
    resource_grants rows — the unified-stack model) still confers those
    tables on the agent. This closes the known gap where package-only
    owners' agents were denied every table."""
    import src.agent_scope_intersection as mod

    monkeypatch.setattr(mod, "_allowed_ids_for_user", _rt_aware({"table": set(), "data_package": {"P1"}}))
    monkeypatch.setattr(mod, "_owner_package_ids", lambda uid, conn=None: frozenset({"P1"}))
    monkeypatch.setattr(mod, "_agent_scope_ids", _scope_aware({"table": {"t1"}}))
    monkeypatch.setattr(
        mod,
        "_package_table_ids",
        lambda pkg_ids, conn=None: frozenset({"t1", "t2"}) if "P1" in pkg_ids else frozenset(),
    )
    monkeypatch.setattr(mod, "_owned_collection_ids", lambda uid: frozenset())
    out = mod.compute_agent_intersection("u1", _agent(tables_mode="selected"))
    assert out["table"] == frozenset({"t1"})


def test_collections_narrow_under_tables_mode(monkeypatch):
    """Declared collection rows narrow COLLECTION; an undeclared collection
    is denied, and one the owner cannot reach never leaks in."""
    import src.agent_scope_intersection as mod

    monkeypatch.setattr(mod, "_allowed_ids_for_user", _rt_aware({"collection": {"c1"}}))
    monkeypatch.setattr(mod, "_agent_scope_ids", _scope_aware({"collection": {"c2", "c3"}}))
    monkeypatch.setattr(mod, "_package_table_ids", lambda pkg_ids, conn=None: frozenset())
    monkeypatch.setattr(mod, "_owned_collection_ids", lambda uid: frozenset({"c2"}))
    out = mod.compute_agent_intersection("u1", _agent(tables_mode="selected"))
    # c2: declared ∧ owner-owned. c1: owner's but undeclared. c3: not owner's.
    assert out["collection"] == frozenset({"c2"})


def test_collection_axis_all_passes_owner_grants_and_owned(monkeypatch):
    import src.agent_scope_intersection as mod

    monkeypatch.setattr(mod, "_allowed_ids_for_user", _rt_aware({"collection": {"c1"}}))
    monkeypatch.setattr(mod, "_agent_scope_ids", _scope_aware({}))
    monkeypatch.setattr(mod, "_package_table_ids", lambda pkg_ids, conn=None: frozenset())
    monkeypatch.setattr(mod, "_owned_collection_ids", lambda uid: frozenset({"c2"}))
    out = mod.compute_agent_intersection("u1", _agent())
    assert out["collection"] == frozenset({"c1", "c2"})


def test_declared_package_owner_lacks_does_not_leak(monkeypatch):
    """A scope row naming a package the OWNER cannot reach adds nothing —
    neither the package itself nor its member tables."""
    import src.agent_scope_intersection as mod

    monkeypatch.setattr(mod, "_allowed_ids_for_user", _rt_aware({}))
    monkeypatch.setattr(mod, "_owner_package_ids", lambda uid, conn=None: frozenset())
    monkeypatch.setattr(mod, "_agent_scope_ids", _scope_aware({"data_package": {"P9"}}))
    monkeypatch.setattr(
        mod,
        "_package_table_ids",
        lambda pkg_ids, conn=None: frozenset({"t9"}) if "P9" in pkg_ids else frozenset(),
    )
    monkeypatch.setattr(mod, "_owned_collection_ids", lambda uid: frozenset())
    out = mod.compute_agent_intersection("u1", _agent(tables_mode="selected"))
    assert out.get("table", frozenset()) == frozenset()
    assert out.get("data_package", frozenset()) == frozenset()


def test_unrecognized_tables_mode_fails_closed_for_the_whole_data_axis(monkeypatch):
    import src.agent_scope_intersection as mod

    monkeypatch.setattr(
        mod,
        "_allowed_ids_for_user",
        _rt_aware({"table": {"t1"}, "data_package": {"P1"}, "collection": {"c1"}}),
    )
    monkeypatch.setattr(mod, "_agent_scope_ids", _scope_aware({"table": {"t1"}}))
    monkeypatch.setattr(mod, "_package_table_ids", lambda pkg_ids, conn=None: frozenset())
    monkeypatch.setattr(mod, "_owned_collection_ids", lambda uid: frozenset())
    out = mod.compute_agent_intersection("u1", _agent(tables_mode="bogus"))
    assert out.get("table", frozenset()) == frozenset()
    assert out.get("data_package", frozenset()) == frozenset()
    assert out.get("collection", frozenset()) == frozenset()


def test_owner_package_ids_is_bounded_by_grants_and_by_the_stack(monkeypatch):
    """The owner's package set may exceed NEITHER their raw grants nor their
    effective stack.

    Both bounds are load-bearing and fail in opposite directions: the stack
    alone would include an admin's ungranted self-subscriptions (god-mode
    through the back door), while raw grants alone would include an
    `available` package the owner never subscribed to — whose tables 403 for
    the owner but would reach their agent.
    """
    import src.agent_scope_intersection as mod

    monkeypatch.setattr(mod, "_allowed_ids_for_user", lambda uid, rt, conn=None: frozenset({"granted", "both"}))

    class _Entry:
        def __init__(self, id):
            self.id = id

    class _Resolver:
        def __init__(self, conn=None):
            pass

        def stack(self, uid, rt):
            return [_Entry("both"), _Entry("stack_only")]

    monkeypatch.setattr("app.services.stack_resolver.StackResolver", _Resolver)
    assert mod._owner_package_ids("u1") == frozenset({"both"})


def test_owner_package_ids_fails_closed_when_the_resolver_raises(monkeypatch):
    import src.agent_scope_intersection as mod

    monkeypatch.setattr(mod, "_allowed_ids_for_user", lambda uid, rt, conn=None: frozenset({"P1"}))

    class _Boom:
        def __init__(self, conn=None):
            pass

        def stack(self, uid, rt):
            raise RuntimeError("resolver down")

    monkeypatch.setattr("app.services.stack_resolver.StackResolver", _Boom)
    assert mod._owner_package_ids("u1") == frozenset()
