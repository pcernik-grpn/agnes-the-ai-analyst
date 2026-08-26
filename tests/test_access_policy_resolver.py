"""Task 5 -- ``policied_relation()``, the resolver every downstream
enforcement point (Tasks 6-12: SQL rewrite, table_id surfaces, BigQuery,
disclosure, caches) binds against (table access policies design doc §5,
§6, §12).

Direct-repository level (no HTTP client, no admin token) -- this module
tests the resolver's own contract, not the admin write path (that is Task
4's ``tests/test_journey_access_policy_interlock.py``).
"""

import pytest

from src.access_policy import (
    PoliciedRelation,
    PolicyError,
    PolicyIdentityUnresolvable,
    policied_relation,
)
from src.sql_ident import quote_ident

# Uses only $user_groups -- exercises "only the referenced keys are bound".
GROUPS_ONLY_POLICY = "SELECT * FROM invoices WHERE list_contains($user_groups, cost_center)"

# Uses all three known variables -- exercises full identity binding
# (email + id + groups), notably for the AgentPrincipal caller-identity
# case (C2.3) -- owner when no distinct caller is set, else the caller.
FULL_IDENTITY_POLICY = (
    "SELECT * FROM contracts WHERE owner_email = $user_email "
    "AND owner_id = $user_id AND list_contains($user_groups, unit)"
)


@pytest.fixture
def policy_env(e2e_env):
    """Seed users/groups/registry rows directly through the repositories --
    an Admin, a solo analyst (two ordinary groups), a user in a
    metacharacter-named group, an agent "owner", plus one policied table per
    policy body above and one table with no policy at all.
    """
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.table_registry import TableRegistryRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.users import UserRepository

    conn = get_system_db()
    users = UserRepository(conn)
    users.create(id="u_admin", email="admin@example.com", name="Admin")
    users.create(id="u_admin_stack", email="admin-stack@example.com", name="Admin (stack PAT)")
    users.create(id="u_solo", email="solo@example.com", name="Solo")
    users.create(id="u_weird", email="weird@example.com", name="Weird")
    users.create(id="u_owner", email="owner@example.com", name="Owner")

    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
    members = UserGroupMembersRepository(conn)
    members.add_member("u_admin", admin_gid, source="system_seed")
    members.add_member("u_admin_stack", admin_gid, source="system_seed")

    groups = UserGroupsRepository(conn)
    finance_gid = groups.create(name="Finance")["id"]
    marketing_gid = groups.create(name="Marketing")["id"]
    weird_gid = groups.create(name="R&D%")["id"]  # pattern metacharacter in the name

    members.add_member("u_solo", finance_gid, source="admin")
    members.add_member("u_solo", marketing_gid, source="admin")
    members.add_member("u_weird", weird_gid, source="admin")
    members.add_member("u_owner", finance_gid, source="admin")

    registry = TableRegistryRepository(conn)
    registry.register(
        id="tbl_invoices",
        name="invoices",
        source_type="keboola",
        query_mode="local",
        server_only=True,
    )
    registry.set_access_policy("tbl_invoices", sql=GROUPS_ONLY_POLICY, note="cost-centre filter", updated_by="admin")

    registry.register(
        id="tbl_contracts",
        name="contracts",
        source_type="keboola",
        query_mode="local",
        server_only=True,
    )
    registry.set_access_policy("tbl_contracts", sql=FULL_IDENTITY_POLICY, note="owner filter", updated_by="admin")

    registry.register(
        id="tbl_orders",
        name="orders",
        source_type="keboola",
        query_mode="local",
    )
    conn.close()

    return {
        "solo_user": {"id": "u_solo", "email": "solo@example.com"},
        "admin_user": {"id": "u_admin", "email": "admin@example.com"},
        "admin_stack_user": {
            "id": "u_admin_stack",
            "email": "admin-stack@example.com",
            "credential_surface": "stack",
        },
        "weird_user": {"id": "u_weird", "email": "weird@example.com"},
    }


def _session_principal():
    from app.auth.session_principal import SessionPrincipal

    return SessionPrincipal(
        session_id="sess-1",
        participant_user_ids=["u_solo", "u_owner"],
        participant_emails=["solo@example.com", "owner@example.com"],
        intersection={},
    )


def _agent_principal(
    owner_user_id="u_owner",
    owner_email="owner@example.com",
    caller_user_id=None,
    caller_email=None,
):
    from app.auth.session_principal import AgentPrincipal

    return AgentPrincipal(
        session_id="agent-sess-1",
        agent_id="agent-1",
        owner_user_id=owner_user_id,
        owner_email=owner_email,
        intersection={},
        caller_user_id=caller_user_id,
        caller_email=caller_email,
    )


class TestNoPolicyPassthrough:
    """(a) A table with no ``access_policy_sql`` is untouched, for any
    principal shape -- enforcement is inert until a policy is attached."""

    def test_no_policy_is_passthrough_and_resolves_name_to_id(self, policy_env):
        # Called with the registry NAME, not the id -- proves id-or-name
        # resolution (§5.3) as part of the same behavior.
        result = policied_relation("orders", policy_env["solo_user"])

        assert result == PoliciedRelation(
            relation_sql=f"SELECT * FROM {quote_ident('orders')}",
            params={},
            policied=False,
            table_id="tbl_orders",
        )

    def test_no_policy_passthrough_also_holds_for_a_session_principal(self, policy_env):
        """A co-drive session has no single identity, but that is only a
        problem once a policy actually needs one."""
        result = policied_relation("tbl_orders", _session_principal())
        assert result.policied is False
        assert result.params == {}


class TestAdminBypass:
    """(b) An admin on a full-surface credential passes through even when a
    policy is attached (§12)."""

    def test_admin_passthrough_even_with_policy_set(self, policy_env):
        result = policied_relation("tbl_invoices", policy_env["admin_user"])

        assert result.policied is False
        assert result.relation_sql == f"SELECT * FROM {quote_ident('invoices')}"
        assert result.params == {}
        assert result.table_id == "tbl_invoices"

    def test_stack_surface_admin_pat_is_filtered_not_bypassed(self, policy_env):
        """§12's explicit pick: policies follow the credential SURFACE, not
        admin-group membership alone -- a `surface='stack'` PAT (the
        `agnes init` default) is filtered like any analyst."""
        result = policied_relation("tbl_invoices", policy_env["admin_stack_user"])

        assert result.policied is True
        assert result.relation_sql == GROUPS_ONLY_POLICY


class TestSoloUserPolicied:
    """(c) A solo (non-admin) user with a policy attached gets the policy
    body verbatim, bound to their own live identity."""

    def test_policy_applies_and_binds_live_groups(self, policy_env):
        result = policied_relation("tbl_invoices", policy_env["solo_user"])

        assert result.policied is True
        assert result.relation_sql == GROUPS_ONLY_POLICY
        assert result.table_id == "tbl_invoices"
        assert set(result.params["user_groups"]) == {"Finance", "Marketing"}

    def test_only_referenced_variables_are_bound(self, policy_env):
        """The policy references only $user_groups -- $user_email/$user_id
        must not be looked up or included, even though both are resolvable
        for this principal."""
        result = policied_relation("tbl_invoices", policy_env["solo_user"])

        assert set(result.params.keys()) == {"user_groups"}


class TestSessionPrincipalUnresolvable:
    """(d) A co-drive session has no single identity to bind a policy
    against and is refused outright, not guessed."""

    def test_session_principal_raises_on_a_policied_table(self, policy_env):
        with pytest.raises(PolicyIdentityUnresolvable):
            policied_relation("tbl_invoices", _session_principal())


class TestPatternMetacharacterGroupRejected:
    """(e) Defense in depth (§6.3): a group name containing a LIKE/ILIKE
    wildcard is refused before it is ever bound, independent of whether
    save-time validation on this specific policy body would have caught it."""

    def test_percent_containing_group_raises_policy_error(self, policy_env):
        with pytest.raises(PolicyError) as exc_info:
            policied_relation("tbl_invoices", policy_env["weird_user"])

        assert exc_info.value.table_id == "tbl_invoices"


class TestAgentPrincipalBindsCaller:
    """(f) An AgentPrincipal binds the CALLER's identity (C2.3,
    shared-agent runtime) -- whoever is actually driving the turn -- never
    the agent's own scope-derived identity (it has none) and never the
    Admin bypass (an agent is never admin, whichever identity names one).

    For an owner running their own agent (no distinct caller supplied),
    this reproduces the exact pre-C2.3 owner-binding behavior -- see
    ``test_agent_principal_falls_back_to_owner_identity_when_no_caller_set``.
    The whole point of C2.3 is the OTHER case: a shared agent's row policy
    must filter by who is asking, not who built it.
    """

    def test_agent_principal_falls_back_to_owner_identity_when_no_caller_set(self, policy_env):
        result = policied_relation("tbl_contracts", _agent_principal())

        assert result.policied is True
        assert result.relation_sql == FULL_IDENTITY_POLICY
        assert result.params["user_id"] == "u_owner"
        assert result.params["user_email"] == "owner@example.com"
        assert result.params["user_groups"] == ["Finance"]

    def test_agent_principal_binds_a_distinct_caller_not_the_owner(self, policy_env):
        """A shared agent's principal carries a caller (a grantee, group
        Marketing+Finance) distinct from its owner (Finance only) -- the
        bound identity/groups must be the CALLER's, never the owner's."""
        shared_agent = _agent_principal(caller_user_id="u_solo", caller_email="solo@example.com")

        result = policied_relation("tbl_contracts", shared_agent)

        assert result.policied is True
        assert result.params["user_id"] == "u_solo"
        assert result.params["user_email"] == "solo@example.com"
        assert set(result.params["user_groups"]) == {"Finance", "Marketing"}

    def test_agent_principal_never_bypasses_even_when_its_owner_is_admin(self, policy_env):
        """An agent is never the Admin god-mode short-circuit -- not even
        transitively through an owner who happens to be an Admin. Only a
        plain user dict can take the admin-bypass branch (§12)."""
        agent_owned_by_admin = _agent_principal(owner_user_id="u_admin", owner_email="admin@example.com")

        result = policied_relation("tbl_invoices", agent_owned_by_admin)

        assert result.policied is True
        assert result.relation_sql == GROUPS_ONLY_POLICY

    def test_agent_principal_never_bypasses_even_when_its_caller_is_admin(self, policy_env):
        """Symmetric guard on the caller side (C2.3): sharing an agent TO
        an admin must not let that admin's god-mode leak into row-level
        filtering -- an AgentPrincipal never takes the dict-shaped
        admin-bypass branch, regardless of which identity names an admin."""
        agent_shared_to_admin = _agent_principal(caller_user_id="u_admin", caller_email="admin@example.com")

        result = policied_relation("tbl_invoices", agent_shared_to_admin)

        assert result.policied is True
        assert result.relation_sql == GROUPS_ONLY_POLICY


class TestUnknownDialect:
    """Only the two known dialects are accepted -- an unrecognized one fails
    loudly rather than silently falling back to an unfiltered relation. The
    BigQuery arm itself (§7.2) is covered end to end in
    ``tests/test_access_policy_bigquery.py`` (Task 10)."""

    def test_unknown_dialect_raises_value_error(self, policy_env):
        with pytest.raises(ValueError):
            policied_relation("tbl_invoices", policy_env["solo_user"], dialect="postgres")


class TestUnknownTable:
    """Every failure denies (§17) -- resolving an unregistered table_id is a
    failure, not a passthrough."""

    def test_unknown_table_raises_policy_error(self, policy_env):
        with pytest.raises(PolicyError) as exc_info:
            policied_relation("does-not-exist", policy_env["solo_user"])

        assert exc_info.value.table_id == "does-not-exist"
