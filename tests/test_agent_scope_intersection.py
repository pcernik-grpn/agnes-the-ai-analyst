"""resolve_agent_authority: an agent's OWN resolved authority per D-C2
(remediation-program C2.2 — ``docs/superpowers/plans/
2026-08-26-one-agent-model.md``), replacing the old owner-intersection
(``compute_agent_intersection``).

Unit-level: monkeypatches ``_allowed_ids_for_user`` (an identity's raw group
grants), ``_agent_scope_rows`` (the agent's declared ``(item_id,
granted_by)`` pairs per item_type), and ``_is_user_admin`` (whether a given
granter is currently an admin) so every scenario below is deterministic and
DB-free — the agent row itself is supplied via a monkeypatched
``agents_repo()`` stub, mirroring how the old suite supplied it as a plain
dict.
"""

from __future__ import annotations

import pytest


def _agent(**over):
    row = {
        "id": "a1",
        "owner_user_id": "u1",
        "deleted_at": None,
        "tables_mode": "all",
        "plugins_mode": "all",
        "connections_mode": "all",
        "memory_mode": "all",
    }
    row.update(over)
    return row


class _StubAgentsRepo:
    def __init__(self, agent_row):
        self._row = agent_row

    def get_by_id(self, agent_id):
        return self._row if agent_id == self._row.get("id") else None


def _stub_repo(monkeypatch, agent_row):
    monkeypatch.setattr("src.repositories.agents_repo", lambda: _StubAgentsRepo(agent_row))


def _rt_aware(mapping):
    """`_allowed_ids_for_user` stand-in keyed by (resource_type)."""
    return lambda uid, rt, conn=None: frozenset(mapping.get(rt, frozenset()))


def _rows(mapping):
    """`_agent_scope_rows` stand-in keyed by item_type ->
    [{"item_id": ..., "granted_by": ...}, ...]."""
    return lambda aid, it, conn=None: list(mapping.get(it, []))


def _admins(admin_ids):
    return lambda uid, conn=None: uid in admin_ids


def _resolve(agent_row):
    from src.agent_scope_intersection import resolve_agent_authority

    return resolve_agent_authority(agent_row["id"])


# ---------------------------------------------------------------------------
# 'all' mode / unmodeled ResourceType — unchanged pass-through to the owner
# ---------------------------------------------------------------------------


def test_all_mode_returns_owner_set_verbatim(monkeypatch):
    import src.agent_scope_intersection as mod

    agent = _agent()
    _stub_repo(monkeypatch, agent)
    monkeypatch.setattr(mod, "_allowed_ids_for_user", lambda uid, rt, conn=None: frozenset({"t1", "t2"}))
    monkeypatch.setattr(mod, "_agent_scope_rows", _rows({}))
    out = _resolve(agent)
    assert out["table"] == frozenset({"t1", "t2"})


def test_missing_inputs_deny_everything(monkeypatch):
    from src.agent_scope_intersection import resolve_agent_authority

    monkeypatch.setattr("src.repositories.agents_repo", lambda: _StubAgentsRepo({"id": "gone"}))
    assert resolve_agent_authority("") == {}
    assert resolve_agent_authority(None) == {}
    assert resolve_agent_authority("no-such-agent") == {}


@pytest.mark.parametrize(
    "agent_row",
    [
        {"id": "a1", "owner_user_id": "u1", "deleted_at": "2026-01-01"},  # soft-deleted
        {"id": "a1", "owner_user_id": ""},  # no owner
    ],
)
def test_missing_agent_or_owner_denies_everything(monkeypatch, agent_row):
    from src.agent_scope_intersection import resolve_agent_authority

    monkeypatch.setattr("src.repositories.agents_repo", lambda: _StubAgentsRepo(agent_row))
    assert resolve_agent_authority(agent_row["id"]) == {}


def test_unrecognized_mode_fails_closed(monkeypatch):
    import src.agent_scope_intersection as mod

    agent = _agent(tables_mode="bogus")
    _stub_repo(monkeypatch, agent)
    monkeypatch.setattr(mod, "_allowed_ids_for_user", lambda uid, rt, conn=None: frozenset({"t1"}))
    monkeypatch.setattr(mod, "_agent_scope_rows", _rows({"table": [{"item_id": "t1", "granted_by": "u1"}]}))
    out = _resolve(agent)
    assert out.get("table", frozenset()) == frozenset()


def test_agent_narrows_flag():
    from src.agent_scope_intersection import agent_narrows

    assert agent_narrows(_agent()) is False
    assert agent_narrows(_agent(plugins_mode="selected")) is True


# ---------------------------------------------------------------------------
# D-C2 — admin-granted rows resolve UNCONDITIONALLY
# ---------------------------------------------------------------------------


def test_admin_granted_table_resolves_unconditionally(monkeypatch):
    """The core D-C2 flip: an admin-granted row is NOT intersected against
    anyone's current access — it is the agent's own authority."""
    import src.agent_scope_intersection as mod

    agent = _agent(tables_mode="selected")
    _stub_repo(monkeypatch, agent)
    # Nobody (owner included) currently holds t1 by any raw grant.
    monkeypatch.setattr(mod, "_allowed_ids_for_user", lambda uid, rt, conn=None: frozenset())
    monkeypatch.setattr(mod, "_agent_scope_rows", _rows({"table": [{"item_id": "t1", "granted_by": "admin1"}]}))
    monkeypatch.setattr(mod, "_is_user_admin", _admins({"admin1"}))
    out = _resolve(agent)
    assert out["table"] == frozenset({"t1"})


def test_admin_granted_data_package_reaches_the_agent_even_without_owner_grant(monkeypatch):
    """REQUIRED (a): an admin-granted data_package reaches the agent EVEN
    THOUGH the owner has NO grant on it (and never subscribed to it) — kills
    the audit's package-invisible-to-agent bug class for admin-built agents.

    Pre-C2.2 this would have FAILED: ``compute_agent_intersection`` always
    intersected against the OWNER's package reach (``_owner_package_ids``),
    and an owner with zero grant on ``pkg1`` produces an empty owner-side
    package set — narrowing ``pkg1`` (and its member tables) to nothing
    regardless of who declared it.
    """
    import src.agent_scope_intersection as mod

    agent = _agent(tables_mode="selected")
    _stub_repo(monkeypatch, agent)
    # Owner (u1) holds NOTHING — no table grants, no package grants.
    monkeypatch.setattr(mod, "_allowed_ids_for_user", lambda uid, rt, conn=None: frozenset())
    monkeypatch.setattr(
        mod, "_agent_scope_rows", _rows({"data_package": [{"item_id": "pkg1", "granted_by": "admin1"}]})
    )
    monkeypatch.setattr(mod, "_is_user_admin", _admins({"admin1"}))
    monkeypatch.setattr(
        mod, "_package_table_ids", lambda pkg_ids, conn=None: frozenset({"t1", "t2"}) if pkg_ids else frozenset()
    )
    out = _resolve(agent)
    assert out["data_package"] == frozenset({"pkg1"})
    assert out["table"] == frozenset({"t1", "t2"})


def test_admin_granted_collection_resolves_unconditionally(monkeypatch):
    import src.agent_scope_intersection as mod

    agent = _agent(tables_mode="selected")
    _stub_repo(monkeypatch, agent)
    monkeypatch.setattr(mod, "_allowed_ids_for_user", lambda uid, rt, conn=None: frozenset())
    monkeypatch.setattr(mod, "_agent_scope_rows", _rows({"collection": [{"item_id": "c1", "granted_by": "admin1"}]}))
    monkeypatch.setattr(mod, "_is_user_admin", _admins({"admin1"}))
    out = _resolve(agent)
    assert out["collection"] == frozenset({"c1"})


def test_admin_granted_plugin_resolves_unconditionally(monkeypatch):
    import src.agent_scope_intersection as mod

    agent = _agent(plugins_mode="selected")
    _stub_repo(monkeypatch, agent)
    monkeypatch.setattr(mod, "_allowed_ids_for_user", lambda uid, rt, conn=None: frozenset())
    monkeypatch.setattr(mod, "_agent_scope_rows", _rows({"plugin": [{"item_id": "mk/p1", "granted_by": "admin1"}]}))
    monkeypatch.setattr(mod, "_is_user_admin", _admins({"admin1"}))
    out = _resolve(agent)
    assert out["marketplace_plugin"] == frozenset({"mk/p1"})


# ---------------------------------------------------------------------------
# D-C2 — self-granted rows narrow to the GRANTER's current access
# ---------------------------------------------------------------------------


def test_self_granted_row_narrows_to_the_granters_current_access(monkeypatch):
    import src.agent_scope_intersection as mod

    agent = _agent(tables_mode="selected")
    _stub_repo(monkeypatch, agent)
    monkeypatch.setattr(mod, "_allowed_ids_for_user", _rt_aware({"table": {"t1", "t2"}}))
    monkeypatch.setattr(mod, "_agent_scope_rows", _rows({"table": [{"item_id": "t2", "granted_by": "u1"}]}))
    monkeypatch.setattr(mod, "_is_user_admin", _admins(set()))
    out = _resolve(agent)
    assert out["table"] == frozenset({"t2"})


def test_self_granted_row_cannot_widen_beyond_its_granter(monkeypatch):
    """A scope row naming a resource the GRANTER lacks must not appear —
    the D-C2 analogue of the old "agent can never widen beyond owner" test."""
    import src.agent_scope_intersection as mod

    agent = _agent(tables_mode="selected")
    _stub_repo(monkeypatch, agent)
    monkeypatch.setattr(mod, "_allowed_ids_for_user", _rt_aware({"table": {"t1"}}))
    monkeypatch.setattr(
        mod,
        "_agent_scope_rows",
        _rows({"table": [{"item_id": "t1", "granted_by": "u1"}, {"item_id": "SECRET", "granted_by": "u1"}]}),
    )
    monkeypatch.setattr(mod, "_is_user_admin", _admins(set()))
    out = _resolve(agent)
    assert out["table"] == frozenset({"t1"})
    assert "SECRET" not in out["table"]


def test_self_granted_item_stops_resolving_when_the_granter_loses_access(monkeypatch):
    """REQUIRED (b): a self-granted item stops resolving when the GRANTER
    loses access — not the owner, not the caller. The granter (``other1``)
    is a distinct identity from both the agent's owner (``u1``) and any
    hypothetical caller, so this pins the row is keyed to ``granted_by``."""
    import src.agent_scope_intersection as mod

    agent = _agent(tables_mode="selected")
    _stub_repo(monkeypatch, agent)

    # The OWNER (u1) holds t1 — if the row were still owner-keyed it would
    # resolve. It must not, because it was granted by other1, who does not
    # (uid-aware stand-in: u1 holds t1, other1 holds nothing).
    monkeypatch.setattr(
        mod,
        "_allowed_ids_for_user",
        lambda uid, rt, conn=None: frozenset({"t1"}) if (uid, rt) == ("u1", "table") else frozenset(),
    )
    monkeypatch.setattr(mod, "_agent_scope_rows", _rows({"table": [{"item_id": "t1", "granted_by": "other1"}]}))
    monkeypatch.setattr(mod, "_is_user_admin", _admins(set()))
    out = _resolve(agent)
    assert out.get("table", frozenset()) == frozenset()


def test_missing_granted_by_falls_back_to_the_owner(monkeypatch):
    """``granted_by IS NULL`` (DuckDB always; a defensively-possible NULL
    row on Postgres) resolves as if self-granted by the agent's OWNER —
    the cutover-safety fallback."""
    import src.agent_scope_intersection as mod

    agent = _agent(tables_mode="selected", owner_user_id="u1")
    _stub_repo(monkeypatch, agent)
    monkeypatch.setattr(mod, "_allowed_ids_for_user", _rt_aware({"table": {"t1"}}))
    monkeypatch.setattr(mod, "_agent_scope_rows", _rows({"table": [{"item_id": "t1", "granted_by": None}]}))
    monkeypatch.setattr(mod, "_is_user_admin", _admins(set()))
    out = _resolve(agent)
    assert out["table"] == frozenset({"t1"})


def test_mixed_admin_and_self_granted_rows_union(monkeypatch):
    """A single axis can hold both an admin-granted row (unconditioned) and
    a self-granted row (narrowed) at once — the union of both halves."""
    import src.agent_scope_intersection as mod

    agent = _agent(tables_mode="selected")
    _stub_repo(monkeypatch, agent)
    monkeypatch.setattr(mod, "_allowed_ids_for_user", _rt_aware({"table": {"t2"}}))  # owner holds only t2
    monkeypatch.setattr(
        mod,
        "_agent_scope_rows",
        _rows(
            {
                "table": [
                    {"item_id": "t1", "granted_by": "admin1"},  # admin-granted, owner lacks it -> still in
                    {"item_id": "t2", "granted_by": "u1"},  # self-granted, owner holds it -> in
                    {"item_id": "t3", "granted_by": "u1"},  # self-granted, owner lacks it -> dropped
                ]
            }
        ),
    )
    monkeypatch.setattr(mod, "_is_user_admin", _admins({"admin1"}))
    out = _resolve(agent)
    assert out["table"] == frozenset({"t1", "t2"})


# ---------------------------------------------------------------------------
# Cutover safety — every row granted_by=owner (C2.1 backfill / DuckDB) must
# resolve EXACTLY like the old owner-intersection.
# ---------------------------------------------------------------------------


def test_owner_granted_everything_matches_the_old_owner_intersection_shape(monkeypatch):
    """Pin the cutover-safety property named in the plan: for an agent whose
    every row is (self-)granted by its own owner, ``resolve_agent_authority``
    must equal the same ``owner_set & agent_set`` the old
    ``compute_agent_intersection`` computed — the D-C2 staging is a provable
    no-op for pre-existing agents."""
    import src.agent_scope_intersection as mod

    agent = _agent(tables_mode="selected")
    _stub_repo(monkeypatch, agent)
    monkeypatch.setattr(mod, "_allowed_ids_for_user", _rt_aware({"table": {"t1", "t2", "t3"}}))
    monkeypatch.setattr(mod, "_agent_scope_rows", _rows({"table": [{"item_id": "t2", "granted_by": "u1"}]}))
    monkeypatch.setattr(mod, "_is_user_admin", _admins(set()))
    out = _resolve(agent)
    # owner_set & agent_set == {t1,t2,t3} & {t2} == {t2}, exactly the old formula.
    assert out["table"] == frozenset({"t2"})


# ---------------------------------------------------------------------------
# The data axis beyond bare tables: data_package + collection rows, both
# governed by tables_mode (the builder's "Knowledge" section sells packages
# and file collections, not individual table ids).
# ---------------------------------------------------------------------------


def test_selected_tables_expand_declared_packages(monkeypatch):
    """A self-granted data package the granter holds reaches that package's
    member tables (live expansion), and the DATA_PACKAGE axis narrows too."""
    import src.agent_scope_intersection as mod

    agent = _agent(tables_mode="selected")
    _stub_repo(monkeypatch, agent)
    monkeypatch.setattr(mod, "_allowed_ids_for_user", _rt_aware({"table": {"t1"}, "data_package": {"P1", "P2"}}))
    monkeypatch.setattr(mod, "_owner_package_ids", lambda uid, conn=None: frozenset({"P1", "P2"}))
    monkeypatch.setattr(mod, "_agent_scope_rows", _rows({"data_package": [{"item_id": "P1", "granted_by": "u1"}]}))
    monkeypatch.setattr(
        mod,
        "_package_table_ids",
        lambda pkg_ids, conn=None: frozenset({"t2"}) if "P1" in pkg_ids else frozenset(),
    )
    monkeypatch.setattr(mod, "_owned_collection_ids", lambda uid: frozenset())
    monkeypatch.setattr(mod, "_is_user_admin", _admins(set()))
    out = _resolve(agent)
    assert out["table"] == frozenset({"t2"})
    assert out["data_package"] == frozenset({"P1"})


def test_owner_package_stack_feeds_table_axis(monkeypatch):
    """An owner whose ONLY table reach is via a data package (no per-table
    resource_grants rows — the unified-stack model) still confers those
    tables on a self-granted-by-owner agent. This closes the known gap
    where package-only owners' agents were denied every table."""
    import src.agent_scope_intersection as mod

    agent = _agent(tables_mode="selected")
    _stub_repo(monkeypatch, agent)
    monkeypatch.setattr(mod, "_allowed_ids_for_user", _rt_aware({"table": set(), "data_package": {"P1"}}))
    monkeypatch.setattr(mod, "_owner_package_ids", lambda uid, conn=None: frozenset({"P1"}))
    monkeypatch.setattr(mod, "_agent_scope_rows", _rows({"table": [{"item_id": "t1", "granted_by": "u1"}]}))
    monkeypatch.setattr(
        mod,
        "_package_table_ids",
        lambda pkg_ids, conn=None: frozenset({"t1", "t2"}) if "P1" in pkg_ids else frozenset(),
    )
    monkeypatch.setattr(mod, "_owned_collection_ids", lambda uid: frozenset())
    monkeypatch.setattr(mod, "_is_user_admin", _admins(set()))
    out = _resolve(agent)
    assert out["table"] == frozenset({"t1"})


def test_collections_narrow_under_tables_mode(monkeypatch):
    """Self-granted collection rows narrow COLLECTION to what the granter
    holds; an undeclared collection is denied, and one the granter cannot
    reach never leaks in."""
    import src.agent_scope_intersection as mod

    agent = _agent(tables_mode="selected")
    _stub_repo(monkeypatch, agent)
    monkeypatch.setattr(mod, "_allowed_ids_for_user", _rt_aware({"collection": {"c1"}}))
    monkeypatch.setattr(
        mod,
        "_agent_scope_rows",
        _rows({"collection": [{"item_id": "c2", "granted_by": "u1"}, {"item_id": "c3", "granted_by": "u1"}]}),
    )
    monkeypatch.setattr(mod, "_package_table_ids", lambda pkg_ids, conn=None: frozenset())
    monkeypatch.setattr(mod, "_owned_collection_ids", lambda uid: frozenset({"c2"}))
    monkeypatch.setattr(mod, "_is_user_admin", _admins(set()))
    out = _resolve(agent)
    # c2: declared ∧ granter-owned. c1: granter's but undeclared. c3: not granter's.
    assert out["collection"] == frozenset({"c2"})


def test_collection_axis_all_passes_owner_grants_and_owned(monkeypatch):
    import src.agent_scope_intersection as mod

    agent = _agent()
    _stub_repo(monkeypatch, agent)
    monkeypatch.setattr(mod, "_allowed_ids_for_user", _rt_aware({"collection": {"c1"}}))
    monkeypatch.setattr(mod, "_agent_scope_rows", _rows({}))
    monkeypatch.setattr(mod, "_package_table_ids", lambda pkg_ids, conn=None: frozenset())
    monkeypatch.setattr(mod, "_owned_collection_ids", lambda uid: frozenset({"c2"}))
    out = _resolve(agent)
    assert out["collection"] == frozenset({"c1", "c2"})


def test_declared_package_granter_lacks_does_not_leak(monkeypatch):
    """A self-granted scope row naming a package its GRANTER cannot reach
    adds nothing — neither the package itself nor its member tables."""
    import src.agent_scope_intersection as mod

    agent = _agent(tables_mode="selected")
    _stub_repo(monkeypatch, agent)
    monkeypatch.setattr(mod, "_allowed_ids_for_user", _rt_aware({}))
    monkeypatch.setattr(mod, "_owner_package_ids", lambda uid, conn=None: frozenset())
    monkeypatch.setattr(mod, "_agent_scope_rows", _rows({"data_package": [{"item_id": "P9", "granted_by": "u1"}]}))
    monkeypatch.setattr(
        mod,
        "_package_table_ids",
        lambda pkg_ids, conn=None: frozenset({"t9"}) if "P9" in pkg_ids else frozenset(),
    )
    monkeypatch.setattr(mod, "_owned_collection_ids", lambda uid: frozenset())
    monkeypatch.setattr(mod, "_is_user_admin", _admins(set()))
    out = _resolve(agent)
    assert out.get("table", frozenset()) == frozenset()
    assert out.get("data_package", frozenset()) == frozenset()


def test_unrecognized_tables_mode_fails_closed_for_the_whole_data_axis(monkeypatch):
    import src.agent_scope_intersection as mod

    agent = _agent(tables_mode="bogus")
    _stub_repo(monkeypatch, agent)
    monkeypatch.setattr(
        mod,
        "_allowed_ids_for_user",
        _rt_aware({"table": {"t1"}, "data_package": {"P1"}, "collection": {"c1"}}),
    )
    monkeypatch.setattr(mod, "_agent_scope_rows", _rows({"table": [{"item_id": "t1", "granted_by": "u1"}]}))
    monkeypatch.setattr(mod, "_package_table_ids", lambda pkg_ids, conn=None: frozenset())
    monkeypatch.setattr(mod, "_owned_collection_ids", lambda uid: frozenset())
    out = _resolve(agent)
    assert out.get("table", frozenset()) == frozenset()
    assert out.get("data_package", frozenset()) == frozenset()
    assert out.get("collection", frozenset()) == frozenset()


def test_owner_package_ids_is_bounded_by_grants_and_by_the_stack(monkeypatch):
    """The identity's package set may exceed NEITHER their raw grants nor
    their effective stack.

    Both bounds are load-bearing and fail in opposite directions: the stack
    alone would include an admin's ungranted self-subscriptions (god-mode
    through the back door), while raw grants alone would include an
    `available` package the identity never subscribed to — whose tables
    403 for them but would reach their agent.
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
