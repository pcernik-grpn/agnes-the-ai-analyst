"""Task 5 -- ``policied_relation()``, the resolver every downstream
enforcement point (Tasks 6-12: SQL rewrite, table_id surfaces, BigQuery,
disclosure, caches) binds against (table access policies design doc §5,
§6, §12).

Mostly direct-repository level (no HTTP client, no admin token) -- this
module tests the resolver's own contract, not the admin write path (that is
Task 4's ``tests/test_journey_access_policy_interlock.py``).

``TestEndToEndDuckDbPathResolutionRefusal`` at the bottom is the one
exception (issue #2147 backlog item 5): the BigQuery arm's fail-closed
contract has an end-to-end HTTP test
(``tests/test_access_policy_bigquery.py``'s ``TestFailClosedOnBigQuery
ExecutionFailure``), and the Databricks arm's ``_PolicyResolutionFailed``/
``_table_is_registered`` machinery has one too
(``tests/test_databricks_scan_and_policies.py``) -- but neither engine
matters for a plain ``query_mode='local'`` table, which is the DEFAULT,
most common shape and takes ``rewrite_sql``'s ``dialect="duckdb"`` path
with no remote-engine wrapper in front of ``policied_relation`` at all.
That class drives ``/api/query`` end to end to pin the same fail-closed
contract there too.
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


class TestPatternPositionVariableRejected:
    """(e) Defense in depth (§6.3), scoped to the position that makes a name
    dangerous rather than to the shape of the name.

    No character class validates group or user names anywhere in Agnes, so a
    ``$user_*`` value MATCHED AS A PATTERN (``owner LIKE $user_email``) could
    widen a policy to everyone. Save-time validation already refuses that
    body (``policy_var_in_pattern_position``); the resolver re-derives it
    from the STORED text on every request, so a row that never went through
    the validator is refused too -- for every caller, not only the one whose
    name happens to carry a metacharacter.

    The converse is the part #1979 got wrong: in a NON-pattern position a
    bound parameter is a value on every engine (§6.2) and can never act as a
    pattern, so a perfectly ordinary group name containing ``_`` or ``%``
    (the pilot's ``sales_cz``; the design doc's own ``CC_A``/``CC_B``) binds
    and filters normally instead of erroring out every read that user makes.
    """

    def test_metacharacter_group_name_binds_in_a_value_position_policy(self, policy_env):
        # `list_contains($user_groups, cost_center)` -- an equality
        # comparison against a list element, not a pattern match.
        result = policied_relation("tbl_invoices", policy_env["weird_user"])

        assert result.policied is True
        assert result.params["user_groups"] == ["R&D%"]

    def test_underscore_group_name_binds_too(self, policy_env):
        from src.db import get_system_db
        from src.repositories.user_group_members import UserGroupMembersRepository
        from src.repositories.user_groups import UserGroupsRepository
        from src.repositories.users import UserRepository

        conn = get_system_db()
        try:
            UserRepository(conn).create(id="u_cz", email="cz@example.com", name="CZ")
            gid = UserGroupsRepository(conn).create(name="sales_cz")["id"]
            UserGroupMembersRepository(conn).add_member("u_cz", gid, source="admin")
        finally:
            conn.close()

        result = policied_relation("tbl_invoices", {"id": "u_cz", "email": "cz@example.com"})

        assert result.params["user_groups"] == ["sales_cz"]

    def test_a_variable_in_pattern_position_is_refused_for_everyone(self, policy_env):
        """A policy body the save-time validator would never have accepted --
        written straight to the registry here, which is exactly the case this
        read-time check exists for. Refused for a caller whose own name is
        metacharacter-free, because the body is what is unsafe."""
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository

        conn = get_system_db()
        try:
            TableRegistryRepository(conn).set_access_policy(
                "tbl_invoices",
                sql="SELECT * FROM invoices WHERE owner_email LIKE $user_email",
                note="hand-edited, never validated",
                updated_by="admin",
            )
        finally:
            conn.close()

        with pytest.raises(PolicyError) as exc_info:
            policied_relation("tbl_invoices", policy_env["solo_user"])

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
    """Only the known dialects (``duckdb`` / ``bigquery`` / ``databricks`` /
    ``snowflake``) are accepted -- an unrecognized one fails loudly rather
    than silently falling back to an unfiltered relation. The BigQuery arm
    itself (§7.2) is covered end to end in
    ``tests/test_access_policy_bigquery.py`` (Task 10); the Snowflake arm
    (S2, RLS review issue #1979) in ``tests/test_access_policy_snowflake.py``."""

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

    def test_the_unknown_case_has_its_own_subclass(self, policy_env):
        """A name the registry does not know is the ONE resolution outcome
        that is not a security refusal, so it gets a distinguishable
        subclass -- what lets ``rewrite_sql`` swallow it without also
        swallowing a refusal (#1979). Still a ``PolicyError``, so every
        caller that catches only the base type is unchanged."""
        from src.access_policy import PolicyUnknownTable

        with pytest.raises(PolicyUnknownTable):
            policied_relation("does-not-exist", policy_env["solo_user"])


class TestCaseInsensitiveNameResolution:
    """DuckDB's catalog folds identifiers -- ``FROM INVOICES`` and ``FROM
    "Invoices"`` both resolve to the view created as ``invoices`` (verified:
    DuckDB even folds a QUOTED identifier onto an existing view, and refuses
    to create a second view differing only by case). The registry lookup
    behind the policy resolver was exact-equality on both backends, so a
    caller who merely changed the case of the table name got
    ``PolicyUnknownTable`` -- the ONE outcome ``rewrite_sql`` swallows as
    "not a registered table" -- and read the raw, unfiltered view with a
    200 (#1979, security review). Resolution must fold exactly like the
    catalog it protects.
    """

    def test_upper_cased_name_resolves_to_the_registered_row(self, policy_env):
        result = policied_relation("INVOICES", policy_env["solo_user"])
        assert result.policied is True
        assert result.table_id == "tbl_invoices"
        assert result.relation_sql == GROUPS_ONLY_POLICY

    def test_mixed_case_name_resolves_to_the_registered_row(self, policy_env):
        result = policied_relation("Invoices", policy_env["solo_user"])
        assert result.policied is True
        assert result.table_id == "tbl_invoices"

    def test_exact_name_still_resolves(self, policy_env):
        result = policied_relation("invoices", policy_env["solo_user"])
        assert result.policied is True
        assert result.table_id == "tbl_invoices"

    def test_exact_id_still_wins(self, policy_env):
        result = policied_relation("tbl_invoices", policy_env["solo_user"])
        assert result.policied is True
        assert result.table_id == "tbl_invoices"

    def test_a_genuinely_unknown_name_is_still_the_unknown_subclass(self, policy_env):
        """Folding case must not turn "no such table" into a refusal --
        every query naming a CTE or an information_schema view depends on
        that outcome staying swallowable."""
        from src.access_policy import PolicyUnknownTable

        with pytest.raises(PolicyUnknownTable):
            policied_relation("NoSuchTable", policy_env["solo_user"])

    def test_case_variant_registry_rows_fail_closed(self, policy_env):
        """Two registry rows differing only by case are ambiguous: the
        analytics catalog can hold only ONE of the two views, so which row's
        policy applies is unknowable. Refuse (a ``PolicyError``, which
        ``rewrite_sql`` propagates as a structured ``policy_error``) rather
        than pick one -- never the swallowed ``PolicyUnknownTable``."""
        from src.access_policy import PolicyUnknownTable
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository

        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="tbl_invoices_upper",
                name="INVOICES",
                source_type="keboola",
                query_mode="local",
                server_only=True,
            )
        finally:
            conn.close()

        for spelling in ("invoices", "INVOICES", "Invoices"):
            with pytest.raises(PolicyError) as exc_info:
                policied_relation(spelling, policy_env["solo_user"])
            assert not isinstance(exc_info.value, PolicyUnknownTable)


class TestRewriteThroughTheRealResolver:
    """``rewrite_sql`` with its DEFAULT ``resolve=policied_relation`` -- the
    exact wiring ``POST /api/query`` uses. ``tests/test_access_policy_
    rewrite.py`` drives the rewrite through a fake resolver that always
    matched case-insensitively; the REAL one did not, which is how the
    case-folding leak survived a green suite (#1979, security review).
    """

    def test_every_casing_of_a_policied_name_is_rewritten_identically(self, policy_env):
        from src.access_policy import rewrite_sql

        principal = policy_env["solo_user"]
        ids_seen = []
        for spelling in ("invoices", "INVOICES", "Invoices", '"Invoices"'):
            sql, params, policied_ids = rewrite_sql(f"SELECT * FROM {spelling}", principal)
            assert policied_ids == ["tbl_invoices"], spelling
            assert "list_contains" in sql, spelling
            assert params["user_groups"] == ["Finance", "Marketing"] or "user_groups" in params
            ids_seen.append(tuple(policied_ids))
        assert len(set(ids_seen)) == 1

    def test_an_unregistered_name_is_still_left_alone(self, policy_env):
        from src.access_policy import rewrite_sql

        sql, params, policied_ids = rewrite_sql(
            "WITH totals AS (SELECT 1 AS n) SELECT * FROM totals", policy_env["solo_user"]
        )
        assert policied_ids == []


class TestEndToEndDuckDbPathResolutionRefusal:
    """Issue #2147 backlog item 5 -- see the module docstring. The fail-
    closed contract (#1979: a resolution REFUSAL, distinct from "no such
    table", must never fall back to the raw unfiltered view) is already
    pinned end to end for the BigQuery remote arm
    (``tests/test_access_policy_bigquery.py``) and the Databricks remote arm
    (``tests/test_databricks_scan_and_policies.py``). This class pins the
    SAME contract for the path every plain ``query_mode='local'`` table
    takes: ``rewrite_sql``'s default ``dialect="duckdb"``, no remote-engine
    resolver wrapper in front of ``policied_relation`` at all.
    """

    @pytest.fixture
    def duckdb_path_env(self, seeded_app, mock_extract_factory):
        """A granted, non-admin analyst and a ``server_only``, purely-local
        table -- registered with no policy yet, so the RBAC/sync plumbing is
        set up before the test attaches the body that will be refused."""
        from app.auth.jwt import create_access_token
        from src.db import get_system_db
        from src.orchestrator import SyncOrchestrator
        from src.repositories.table_registry import TableRegistryRepository
        from src.repositories.users import UserRepository
        from tests.conftest import grant_table_via_package

        env = seeded_app["env"]
        mock_extract_factory(
            "keboola", [{"name": "invoices", "data": [{"id": "1", "owner_email": "alice@example.com"}]}]
        )
        SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

        conn = get_system_db()
        try:
            UserRepository(conn).create(id="u_duckdb_path", email="duckdb-path@example.com", name="DuckDB Path")
            TableRegistryRepository(conn).register(
                id="invoices",
                name="invoices",
                source_type="keboola",
                query_mode="local",
                server_only=True,
            )
            grant_table_via_package(conn, "invoices", "u_duckdb_path")
        finally:
            conn.close()

        return {
            **seeded_app,
            "token": create_access_token("u_duckdb_path", "duckdb-path@example.com"),
        }

    def test_resolution_refusal_denies_with_500_policy_error_and_no_rows(self, duckdb_path_env):
        """A body the save-time validator would never have accepted --
        written straight to the registry here, matching every other
        ``set_access_policy`` fixture in this file -- refused by the SAME
        read-time pattern-position guard ``TestPatternPositionVariableRejected``
        exercises directly against the resolver, now proven through the live
        HTTP surface every non-admin caller of a local table actually uses."""
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository

        conn = get_system_db()
        try:
            TableRegistryRepository(conn).set_access_policy(
                "invoices",
                sql="SELECT * FROM invoices WHERE owner_email LIKE $user_email",
                note="hand-edited, never validated",
                updated_by="admin",
            )
        finally:
            conn.close()

        c = duckdb_path_env["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT * FROM invoices"},
            headers={"Authorization": f"Bearer {duckdb_path_env['token']}"},
        )

        assert r.status_code == 500, r.text
        body = r.json()
        assert body["detail"]["reason"] == "policy_error"
        assert body["detail"]["table"] == "invoices"
        assert "rows" not in body, "an error response must never carry a rows key"


class TestDataAppViewerPrincipalBindsViewer:
    """A hosted data app in viewer mode (`data_identity='viewer'`) queries as
    a ``DataAppViewerPrincipal`` -- ``$user_*`` binds to whoever is LOOKING
    at the app, never to the app's owner, and never the Admin bypass."""

    @staticmethod
    def _viewer_principal(viewer_user_id="u_solo", viewer_email="solo@example.com"):
        from app.auth.session_principal import DataAppViewerPrincipal

        return DataAppViewerPrincipal(
            slug="sales",
            app_id="app_1",
            owner_user_id="u_owner",
            owner_email="owner@example.com",
            viewer_user_id=viewer_user_id,
            viewer_email=viewer_email,
            intersection={},
        )

    def test_binds_the_viewer_not_the_owner(self, policy_env):
        result = policied_relation("tbl_contracts", self._viewer_principal())

        assert result.policied is True
        assert result.params["user_id"] == "u_solo"
        assert result.params["user_email"] == "solo@example.com"
        assert set(result.params["user_groups"]) == {"Finance", "Marketing"}

    def test_an_admin_viewer_is_still_filtered(self, policy_env):
        """A restricted principal is never admin, whichever identity it names."""
        result = policied_relation("tbl_contracts", self._viewer_principal("u_admin", "admin@example.com"))

        assert result.policied is True
        assert result.params["user_id"] == "u_admin"
