"""Task 12 -- identity-keyed response caches + the save-time execution probe
(table access policies design doc §9, §14.6; plan Task 12).

Tasks 8/9 bypassed `_sample_cache` (`app/api/v2_sample.py`) and
`_schema_cache` (`app/api/v2_schema.py`) entirely for policied tables -- a
safe interim, per §9's own words ("the internal connector already excludes
itself... that precedent is the one to follow"), but one that leaves a
policied table's response UNCACHED forever. This turns the bypass into an
identity-keyed cache instead: a policied table's cache key carries
`(user_id, user_email, sorted(group_names))`, so repeated calls by the SAME
caller still hit cache, but two different callers never share a slot -- the exact
"worse than no policy" failure §5.1 names for a shared key on a
caller-dependent response.

`probe_policy` (§14.6) is the companion save-time guard: a policy that
references a column the underlying table doesn't have is rejected when the
admin attaches it, not discovered by the first analyst's request.
"""

from __future__ import annotations

import pytest

POLICY_SQL = "SELECT * EXCLUDE (secret) FROM orders WHERE list_contains($user_groups, unit)"


@pytest.fixture(autouse=True)
def _clear_response_caches():
    """Both caches are process-global (module-level `TTLCache` singletons),
    so a cache key that happens to be byte-identical across two test
    functions -- every test below reuses the same table id/`n`/user id on
    purpose, to keep the fixture simple -- would otherwise let one test's
    cache entry silently answer the NEXT test's request, defeating the
    "does a repeat call actually hit the cache" assertion below."""
    from app.api import v2_sample, v2_schema

    v2_sample._sample_cache.clear()
    v2_schema._schema_cache.clear()
    yield
    v2_sample._sample_cache.clear()
    v2_schema._schema_cache.clear()


@pytest.fixture
def policied_orders(seeded_app, mock_extract_factory, monkeypatch):
    """A `server_only` `orders` table carrying a row+column policy, granted
    to two non-admin users each in their own group (`TeamA`/`TeamB`, also
    the `unit` values the policy filters on) -- the same fixture shape
    `tests/test_access_policy_table_id_surfaces.py` uses for Task 8,
    reproduced here (pytest fixtures don't cross test files) so the
    cache-leak assertions below run against real, disjoint per-caller
    slices rather than a synthetic stub.
    """
    from app.auth.jwt import create_access_token
    from src.db import get_system_db
    from src.orchestrator import SyncOrchestrator
    from src.repositories.table_registry import TableRegistryRepository
    from src.repositories.users import UserRepository
    from tests.conftest import grant_table_via_package

    monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")

    env = seeded_app["env"]
    mock_extract_factory(
        "keboola",
        [
            {
                "name": "orders",
                "data": [
                    {"id": "1", "unit": "TeamA", "secret": "s1", "amount": "100"},
                    {"id": "2", "unit": "TeamA", "secret": "s2", "amount": "150"},
                    {"id": "3", "unit": "TeamB", "secret": "s3", "amount": "300"},
                ],
            },
        ],
    )
    SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

    conn = get_system_db()
    try:
        registry = TableRegistryRepository(conn)
        registry.register(
            id="orders",
            name="orders",
            source_type="keboola",
            query_mode="local",
            server_only=True,
        )
        registry.set_access_policy("orders", sql=POLICY_SQL, note="unit filter", updated_by="admin")

        users = UserRepository(conn)
        users.create(id="u_team_a", email="team-a@example.com", name="Team A")
        users.create(id="u_team_b", email="team-b@example.com", name="Team B")

        # Each grant also creates the group the policy's $user_groups reads --
        # "TeamA"/"TeamB" are simultaneously the RBAC-visibility group and the
        # row-filter value.
        grant_table_via_package(conn, "orders", "u_team_a", group_name="TeamA")
        grant_table_via_package(conn, "orders", "u_team_b", group_name="TeamB")
    finally:
        conn.close()

    return {
        **seeded_app,
        "team_a_token": create_access_token("u_team_a", "team-a@example.com"),
        "team_b_token": create_access_token("u_team_b", "team-b@example.com"),
    }


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── (a) + (b): _sample_cache is identity-keyed ─────────────────────────


class TestSampleCacheIsIdentityKeyed:
    def test_cross_user_leak_is_prevented(self, policied_orders):
        """User A's request warms the cache; user B's very next request for
        the SAME table (same `n`) must return B's own slice, never A's
        cached rows -- the exact failure a shared `table_id|n` key would
        produce (confirmed by temporarily forcing a constant cache identity
        and watching this assertion fail)."""
        c = policied_orders["client"]

        r_a = c.get("/api/v2/sample/orders?n=10", headers=_auth(policied_orders["team_a_token"]))
        assert r_a.status_code == 200, r_a.text
        ids_a = {row["id"] for row in r_a.json()["rows"]}
        assert ids_a == {"1", "2"}
        assert all("secret" not in row for row in r_a.json()["rows"])

        r_b = c.get("/api/v2/sample/orders?n=10", headers=_auth(policied_orders["team_b_token"]))
        assert r_b.status_code == 200, r_b.text
        ids_b = {row["id"] for row in r_b.json()["rows"]}
        assert ids_b == {"3"}, "user B must never receive user A's cached rows"

    def test_same_user_repeat_call_hits_the_cache(self, policied_orders, monkeypatch):
        """The second identical request by the SAME caller must skip the
        expensive DuckDB read entirely -- proof the cache is actually being
        used, not merely that the endpoint is correct on every call."""
        from app.api import v2_sample

        calls = {"n": 0}
        real_open = v2_sample._open_duckdb

        def counting_open(*a, **kw):
            calls["n"] += 1
            return real_open(*a, **kw)

        monkeypatch.setattr(v2_sample, "_open_duckdb", counting_open)

        c = policied_orders["client"]
        r1 = c.get("/api/v2/sample/orders?n=10", headers=_auth(policied_orders["team_a_token"]))
        assert r1.status_code == 200, r1.text
        r2 = c.get("/api/v2/sample/orders?n=10", headers=_auth(policied_orders["team_a_token"]))
        assert r2.status_code == 200, r2.text

        assert r1.json()["rows"] == r2.json()["rows"]
        assert calls["n"] == 1, "the second identical call by the same caller must hit the cache"


# ── (d): the cache-key guard -- a future edit can't silently revert to a
# shared key without a test failing (§9). ───────────────────────────────


class TestCacheKeyCarriesCallerIdentity:
    def test_sample_cache_key_carries_the_callers_identity(self, policied_orders):
        from app.api import v2_sample
        from src.access_policy import policy_cache_identity

        c = policied_orders["client"]
        r = c.get("/api/v2/sample/orders?n=10", headers=_auth(policied_orders["team_a_token"]))
        assert r.status_code == 200, r.text

        identity = policy_cache_identity({"id": "u_team_a", "email": "team-a@example.com"}, table_id="orders")
        expected_key = f"orders|10|policy:{identity!r}"
        assert v2_sample._sample_cache.get(expected_key) is not None, (
            "the policied response must be cached under a key carrying the caller's identity"
        )
        assert v2_sample._sample_cache.get("orders|10") is None, (
            "a policied table must never be cached under the plain key a non-policied table uses"
        )

    def test_schema_cache_key_carries_the_callers_identity(self, policied_orders):
        from app.api import v2_schema
        from src.access_policy import policy_cache_identity

        c = policied_orders["client"]
        r = c.get("/api/v2/schema/orders", headers=_auth(policied_orders["team_a_token"]))
        assert r.status_code == 200, r.text

        identity = policy_cache_identity({"id": "u_team_a", "email": "team-a@example.com"}, table_id="orders")
        expected_key = f"orders|policy:{identity!r}"
        assert v2_schema._schema_cache.get(expected_key) is not None, (
            "the policied schema must be cached under a key carrying the caller's identity"
        )
        assert v2_schema._schema_cache.get("orders") is None, (
            "a policied table must never be cached under the plain key a non-policied table uses"
        )

    def test_policy_cache_identity_shape(self, policied_orders):
        """Direct unit coverage of the resolver-level helper, independent
        of either endpoint's own key-string formatting: `(user_id,
        user_email, sorted-group-tuple)`, per §9."""
        from src.access_policy import policy_cache_identity

        identity = policy_cache_identity({"id": "u_team_a", "email": "team-a@example.com"}, table_id="orders")
        assert identity == ("u_team_a", "team-a@example.com", ("TeamA",))

    def test_identity_differs_when_only_the_email_differs(self, policied_orders):
        """A policy binding `$user_email` makes the response depend on the
        email, so two principals agreeing on id and groups but differing on
        email must never share a cache slot -- the shape an admin changing
        an account's email produces, where the old slice would otherwise
        keep being served for the cache's whole TTL."""
        from src.access_policy import policy_cache_identity

        before = policy_cache_identity({"id": "u_team_a", "email": "team-a@example.com"}, table_id="orders")
        after = policy_cache_identity({"id": "u_team_a", "email": "renamed@example.com"}, table_id="orders")

        assert before != after
        assert before[0] == after[0] and before[2] == after[2], (
            "the fixture must vary ONLY the email, or this proves nothing about the email"
        )

    def test_agent_principal_keys_on_the_callers_email_not_the_owners(self, policied_orders):
        """C2.3: `$user_email` binds to whoever is DRIVING the turn, so the
        cache identity must too -- otherwise a shared agent serves the
        owner-keyed slice to every grantee."""
        from app.auth.session_principal import AgentPrincipal
        from src.access_policy import policy_cache_identity

        def _agent(caller_user_id, caller_email):
            return AgentPrincipal(
                session_id="agent-sess-1",
                agent_id="agent-1",
                owner_user_id="u_team_a",
                owner_email="team-a@example.com",
                intersection={},
                caller_user_id=caller_user_id,
                caller_email=caller_email,
            )

        as_owner = policy_cache_identity(_agent("u_team_a", "team-a@example.com"), table_id="orders")
        as_grantee = policy_cache_identity(_agent("u_team_a", "grantee@example.com"), table_id="orders")

        assert as_owner == ("u_team_a", "team-a@example.com", ("TeamA",))
        assert as_grantee[1] == "grantee@example.com", "the CALLER's email, never the owner's"
        assert as_owner != as_grantee


class TestPolicyEditInvalidatesTheIdentityKeyedSchemaCache:
    """`invalidate_for_table` did an exact-key `invalidate(table_id)`, which
    can never match a policied table's `f"{table_id}|policy:{identity!r}"`
    entries. With `ttl_seconds=3600` the caller kept the PRE-edit column
    list — including a column the admin had just hidden — for up to an
    hour, and `/api/v2/scan`'s where/select validator (same payload via
    `_resolve_schema`) went on accepting references to it."""

    def _hide_amount(self, client, admin_token):
        return client.put(
            "/api/admin/registry/orders",
            json={
                "access_policy_sql": (
                    "SELECT * EXCLUDE (secret, amount) FROM orders WHERE list_contains($user_groups, unit)"
                ),
                "access_policy_note": "unit filter + hide amount",
            },
            headers=_auth(admin_token),
        )

    def test_a_newly_hidden_column_disappears_from_the_schema_immediately(self, policied_orders):
        c = policied_orders["client"]
        token = policied_orders["team_a_token"]

        before = c.get("/api/v2/schema/orders", headers=_auth(token))
        assert before.status_code == 200, before.text
        assert "amount" in [col["name"] for col in before.json()["columns"]]

        edit = self._hide_amount(c, policied_orders["admin_token"])
        assert edit.status_code == 200, edit.text

        after = c.get("/api/v2/schema/orders", headers=_auth(token))
        assert after.status_code == 200, after.text
        assert "amount" not in [col["name"] for col in after.json()["columns"]], (
            "the pre-edit column list survived the policy edit — the identity-keyed cache entry was never dropped"
        )

    def test_scan_stops_accepting_a_where_on_the_newly_hidden_column(self, policied_orders):
        """The same stale payload backs `/api/v2/scan`'s where-validator
        (via `_resolve_schema`), so the hidden column stayed REFERENCEABLE
        there: the validator waved it through and the rejection came from
        the engine instead — a raw binder error that quotes the policy body
        back at the caller (exactly what §16 says not to do)."""
        c = policied_orders["client"]
        token = policied_orders["team_a_token"]

        warm = c.get("/api/v2/schema/orders", headers=_auth(token))
        assert warm.status_code == 200, warm.text

        edit = self._hide_amount(c, policied_orders["admin_token"])
        assert edit.status_code == 200, edit.text

        r = c.post(
            "/api/v2/scan",
            json={"table_id": "orders", "where": "amount = '100'"},
            headers=_auth(token),
        )
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert detail.get("error") == "validator_rejected", (
            "the where-validator must reject the hidden column itself, not defer to a raw engine error"
        )
        assert detail["details"]["column"] == "amount"
        assert "list_contains" not in r.text, "a rejection must not echo the policy body"

    def test_invalidate_for_table_drops_the_identity_keyed_entry(self, policied_orders):
        """Unit-level: the invalidation itself, independent of what any
        endpoint does with the result."""
        from app.api import v2_schema
        from app.api.v2_catalog import invalidate_for_table

        identity_key = "orders|policy:('u_team_a', 'team-a@example.com', ('TeamA',))"
        v2_schema._schema_cache.set(identity_key, {"columns": []})
        v2_schema._schema_cache.set("orders", {"columns": []})

        invalidate_for_table("orders")

        assert v2_schema._schema_cache.get(identity_key) is None
        assert v2_schema._schema_cache.get("orders") is None


# ── (c): the save-time execution probe (§14.6) ──────────────────────────


class TestSaveTimeProbeRejectsAMissingColumn:
    def test_attach_rejected_when_the_policy_references_a_nonexistent_column(
        self, seeded_app, mock_extract_factory, monkeypatch
    ):
        """`SELECT nonexistent_col FROM widgets` passes `validate_policy_sql`
        (it is a structurally valid SELECT referencing only its own table --
        static analysis has no schema to check the column against) but must
        be rejected at save time once the probe actually runs it against the
        real, synced table."""
        from src.db import get_system_db
        from src.orchestrator import SyncOrchestrator
        from src.repositories.table_registry import TableRegistryRepository

        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")

        env = seeded_app["env"]
        mock_extract_factory("keboola", [{"name": "widgets", "data": [{"id": "1", "amount": "10"}]}])
        SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="widgets",
                name="widgets",
                source_type="keboola",
                query_mode="local",
                server_only=True,
            )
        finally:
            conn.close()

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.put(
            "/api/admin/registry/widgets",
            json={
                "access_policy_sql": "SELECT nonexistent_col FROM widgets",
                "access_policy_note": "restrict rows",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "policy_probe_failed" in resp.text

        from src.repositories import table_registry_repo

        assert table_registry_repo().get("widgets")["access_policy_sql"] is None, (
            "the rejected write must not have partially landed"
        )

    def test_probe_is_skipped_for_a_table_that_has_never_synced(self, seeded_app, monkeypatch):
        """A registered-but-never-synced table has no master view yet, so
        there is nothing to validate a column reference against -- this is
        the ordinary register-then-attach-then-sync admin workflow, not a
        probe failure (also pinned end to end by
        `tests/test_journey_access_policy_interlock.py::TestHappyPath`)."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        reg = c.post(
            "/api/admin/register-table",
            json={"name": "never_synced", "source_type": "keboola", "query_mode": "local", "server_only": True},
            headers=_auth(token),
        )
        assert reg.status_code == 201, reg.text
        table_id = reg.json()["id"]

        resp = c.put(
            f"/api/admin/registry/{table_id}",
            json={
                "access_policy_sql": "SELECT * FROM never_synced",
                "access_policy_note": "restrict rows",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

    def test_a_realistic_row_and_column_policy_probes_cleanly_once_synced(
        self, seeded_app, mock_extract_factory, monkeypatch
    ):
        """The design doc's own canonical policy shape (EXCLUDE + md5 +
        list_contains($user_groups, ...)) must attach cleanly through the
        real admin PUT path once the table has real, synced data -- not
        just the trivial `SELECT * FROM t` the interlock tests use. Uses the
        CORRECTED form of the example (excluding `email` before re-deriving
        it) -- see docs/table-access-policies.md's note on
        policy_duplicate_output_column: the uncorrected form (EXCLUDE only
        national_id) is rejected at save, by design, because DuckDB would
        otherwise return two columns both named `email`."""
        from src.db import get_system_db
        from src.orchestrator import SyncOrchestrator
        from src.repositories.table_registry import TableRegistryRepository

        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")

        env = seeded_app["env"]
        mock_extract_factory(
            "keboola",
            [
                {
                    "name": "invoices2",
                    "data": [{"id": "1", "national_id": "N-1", "email": "a@example.com", "cost_center": "Finance"}],
                }
            ],
        )
        SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="invoices2", name="invoices2", source_type="keboola", query_mode="local", server_only=True
            )
        finally:
            conn.close()

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.put(
            "/api/admin/registry/invoices2",
            json={
                "access_policy_sql": (
                    "SELECT * EXCLUDE (national_id, email), md5(email) AS email FROM invoices2 "
                    "WHERE list_contains($user_groups, cost_center)"
                ),
                "access_policy_note": "cost-centre filter",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text


# ── probe_policy: direct unit coverage ───────────────────────────────────


class TestProbePolicyDirect:
    def test_returns_the_effective_column_list(self, seeded_app, mock_extract_factory):
        from src.access_policy_validate import probe_policy
        from src.db import get_analytics_db_readonly
        from src.orchestrator import SyncOrchestrator

        env = seeded_app["env"]
        mock_extract_factory("keboola", [{"name": "gadgets", "data": [{"id": "1", "amount": "10"}]}])
        SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository

        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="gadgets", name="gadgets", source_type="keboola", query_mode="local", server_only=True
            )
        finally:
            conn.close()

        probe_conn = get_analytics_db_readonly()
        try:
            columns = probe_policy("SELECT id, amount FROM gadgets", "gadgets", probe_conn)
        finally:
            probe_conn.close()

        assert {c["name"] for c in columns} == {"id", "amount"}

    def test_raises_policy_validation_error_on_a_bad_column(self, seeded_app, mock_extract_factory):
        from src.access_policy_validate import PolicyValidationError, probe_policy
        from src.db import get_analytics_db_readonly, get_system_db
        from src.orchestrator import SyncOrchestrator
        from src.repositories.table_registry import TableRegistryRepository

        env = seeded_app["env"]
        mock_extract_factory("keboola", [{"name": "gizmos", "data": [{"id": "1", "amount": "10"}]}])
        SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="gizmos", name="gizmos", source_type="keboola", query_mode="local", server_only=True
            )
        finally:
            conn.close()

        probe_conn = get_analytics_db_readonly()
        try:
            with pytest.raises(PolicyValidationError) as exc_info:
                probe_policy("SELECT nope FROM gizmos", "gizmos", probe_conn)
        finally:
            probe_conn.close()
        assert exc_info.value.reason == "policy_probe_failed"

    def test_raises_when_the_table_is_not_registered(self, seeded_app):
        from src.access_policy_validate import PolicyValidationError, probe_policy
        from src.db import get_analytics_db_readonly

        probe_conn = get_analytics_db_readonly()
        try:
            with pytest.raises(PolicyValidationError) as exc_info:
                probe_policy("SELECT * FROM nope", "not_a_real_table_id", probe_conn)
        finally:
            probe_conn.close()
        assert exc_info.value.reason == "policy_probe_failed"
