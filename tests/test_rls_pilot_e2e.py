"""Phase 1 of the RLS pilot (#1979) — the target pilot CONFIGURATION,
end to end, as a permanent regression test.

One Keboola table (``orders``) registered ``query_mode='materialized'`` +
``server_only=true``, wrapped in a data package granted to two groups
(``sales-cz``, ``sales-de``) with one analyst in each (alice, bob), plus a
third analyst (carol) granted the same package through a group the policy
does not enumerate. The policy is the issue's own body:

    SELECT * FROM orders
    WHERE CASE
        WHEN list_contains($user_groups, 'sales-cz') THEN country = 'CZ'
        WHEN list_contains($user_groups, 'sales-de') THEN country = 'DE'
        ELSE FALSE
    END

Nine properties, one class each:

1. alice and bob get DIFFERENT row counts and different data for the SAME
   ``SELECT country, COUNT(*) ... GROUP BY 1`` on ``POST /api/query``;
2. a caller in neither group gets ZERO rows (the ``ELSE FALSE`` branch) —
   an empty answer, not an error;
3. an admin on a ``surface='all'`` credential sees ALL rows; the SAME admin
   on a ``surface='stack'`` credential is filtered like an analyst
   (``docs/table-access-policies.md`` → "The admin bypass" — deliberate,
   and the reason an admin's own ``agnes query`` from an ``agnes init``-ed
   workspace is filtered);
4. every filtered response carries the ``row_scope`` disclosure; the
   unfiltered admin response carries ``null``;
5. the MCP-facing read path (``POST /api/mcp/query-table/{id}``, what the
   per-table MCP tool calls) filters identically and discloses identically;
6. distribution lockout — the table is listed in the manifest (so ``agnes
   catalog`` still discovers it) but flagged ``server_only``, and both
   byte-serving surfaces ``agnes pull`` would use are 403 for EVERYONE,
   admin included;
7. the interlock, both directions — flipping the policied table back to
   distributable is refused, and attaching a policy to a distributed table
   is refused;
8. ``POST /api/admin/registry/{id}/policy/preview`` as alice's persona and
   as the ad-hoc group set ``['sales-de']`` reports the matching
   ``rows_visible``;
9. the table-level RBAC gate and the row-level policy are SEPARATE layers,
   both load-bearing — the wrapping data package is granted only to the
   three pilot groups (never ``Everyone``), so a caller in no granted group
   is refused at the table layer (403, before the policy runs), distinct
   from the ``ELSE FALSE`` empty slice a package-granted-but-unmatched
   caller gets, and revoking one group's package grant reproduces that same
   table-layer refusal for a caller the policy would otherwise admit rows
   for (#1979 review finding).

Hermetic on purpose: NO live Keboola credentials. The pilot's Keboola-ness
is reproduced by its on-disk shape — ``mock_extract_factory`` writes the
parquet to ``${DATA_DIR}/extracts/keboola/data/<name>.parquet``, which is
exactly where ``app/api/sync.py::_run_materialized_pass`` puts a
materialized row's bytes, and the registry row is registered
``query_mode='materialized'`` over it. What is under test is the pilot's
ACCESS behavior, not the Keboola transport (``tests/test_keboola_
materialized_e2e.py`` covers that, and is skipped without live creds).

THE LEAK THIS FILE FOUND, and its fix — read this before copying the
pilot's group names anywhere. The issue spells the two groups ``sales_cz``
/ ``sales_de``, with an underscore, and on the first cut of this feature
that single character turned the policy OFF on ``POST /api/query``:
``policied_relation`` refused to bind ANY live group name containing a
LIKE/SIMILAR-TO metacharacter (``%`` or ``_``) and raised ``PolicyError``,
while ``rewrite_sql`` — the AST rewrite behind ``POST /api/query`` and
``agnes query`` — swallowed ``PolicyError`` from its per-name
``resolve(...)`` call as "this name is not a registered table" and left the
reference UNSUBSTITUTED. The caller then read the raw base view: HTTP 200,
every row, ``row_scope: null``. Both halves are fixed:
``PolicyUnknownTable`` now carries the benign "not in the registry" signal
and is the only thing ``rewrite_sql`` swallows, so a refusal denies with a
structured ``policy_error``; and the metacharacter refusal is scoped to a
policy body that MATCHES an identity variable as a pattern, so an ordinary
group named ``sales_cz`` binds as the value it is.

The bulk of this file keeps the ``sales-cz`` / ``sales-de`` spelling it was
written with — nothing depends on the hyphen any more, and rewriting six
classes of assertions would only churn the diff. The issue's LITERAL,
underscore-spelled configuration is exercised end to end in
``TestIssueLiteralGroupNamesUnderscore`` at the bottom, which is now a
plain passing test (it was a strict xfail pinning the leak).
"""

from __future__ import annotations

import hashlib
import uuid

import pytest

# ---------------------------------------------------------------------------
# The pilot configuration.
# ---------------------------------------------------------------------------

GROUP_CZ = "sales-cz"
GROUP_DE = "sales-de"
GROUP_OTHER = "sales-fr"  # granted the package, absent from the policy

POLICY_SQL = f"""SELECT * FROM orders
WHERE CASE
    WHEN list_contains($user_groups, '{GROUP_CZ}') THEN country = 'CZ'
    WHEN list_contains($user_groups, '{GROUP_DE}') THEN country = 'DE'
    ELSE FALSE
END"""

ROWS = [
    {"id": "1", "country": "CZ", "customer": "Novak", "amount": "100"},
    {"id": "2", "country": "CZ", "customer": "Svoboda", "amount": "200"},
    {"id": "3", "country": "CZ", "customer": "Dvorak", "amount": "300"},
    {"id": "4", "country": "DE", "customer": "Mueller", "amount": "400"},
    {"id": "5", "country": "DE", "customer": "Schmidt", "amount": "500"},
    {"id": "6", "country": "FR", "customer": "Dupont", "amount": "600"},
]
CZ_IDS = {"1", "2", "3"}
DE_IDS = {"4", "5"}
ALL_IDS = {"1", "2", "3", "4", "5", "6"}

GROUP_BY_SQL = "SELECT country, COUNT(*) AS n FROM orders GROUP BY 1"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _group_counts(body: dict) -> dict:
    """`{country: n}` from a /api/query response body (columns + rows)."""
    country = body["columns"].index("country")
    n = body["columns"].index("n")
    return {row[country]: row[n] for row in body["rows"]}


def _mint_pat(user_id: str, email: str, *, surface: str) -> str:
    """A PAT with an explicit credential surface — ``surface='stack'`` is
    what ``agnes init``'s token exchange mints, and is the credential the
    admin bypass deliberately does NOT apply to."""
    from app.auth.jwt import create_access_token
    from src.repositories import access_token_repo

    token_id = str(uuid.uuid4())
    jwt_token = create_access_token(user_id=user_id, email=email, token_id=token_id, typ="pat")
    access_token_repo().create(
        id=token_id,
        user_id=user_id,
        name=f"pilot-{surface}",
        token_hash=hashlib.sha256(jwt_token.encode()).hexdigest(),
        prefix=token_id.replace("-", "")[:8],
        surface=surface,
    )
    return jwt_token


@pytest.fixture
def pilot(seeded_app, mock_extract_factory, monkeypatch):
    """The pilot as configured in #1979 — one materialized, server_only
    Keboola table under the two-group policy, three analysts and two admin
    credentials of different surfaces.

    The policy is attached through the ADMIN API (``PUT /api/admin/registry/
    {id}``), not the repository, so this fixture also proves the pilot's own
    body survives save-time validation (function allowlist, variable
    positions, transpilability, duplicate output columns) rather than
    smuggling it past the validated write path.
    """
    from app.auth.jwt import create_access_token
    from src.db import get_system_db
    from src.orchestrator import SyncOrchestrator
    from src.repositories.table_registry import TableRegistryRepository
    from src.repositories.users import UserRepository
    from tests.conftest import grant_table_via_package

    monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")

    env = seeded_app["env"]
    mock_extract_factory("keboola", [{"name": "orders", "data": ROWS}])
    SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

    conn = get_system_db()
    try:
        TableRegistryRepository(conn).register(
            id="orders",
            name="orders",
            source_type="keboola",
            query_mode="materialized",
            server_only=True,
            bucket="in.c-sales",
            source_table="orders",
            # Keboola materialized rows take a JSON filter spec, never SQL;
            # NULL means "full-table export", which is what the pilot is.
            source_query=None,
        )

        users = UserRepository(conn)
        users.create(id="u_alice", email="alice@example.com", name="Alice")
        users.create(id="u_bob", email="bob@example.com", name="Bob")
        users.create(id="u_carol", email="carol@example.com", name="Carol")

        # ONE data package wrapping the table, granted to each group in turn
        # (grant_table_via_package reuses the package it already made for a
        # given table_id) — the issue's "a data package containing the table
        # granted to both groups".
        grant_table_via_package(conn, "orders", "u_alice", group_name=GROUP_CZ)
        grant_table_via_package(conn, "orders", "u_bob", group_name=GROUP_DE)
        grant_table_via_package(conn, "orders", "u_carol", group_name=GROUP_OTHER)
        # The admin joins the CZ group so their surface='stack' credential
        # has the package in its stack at all — the surface distinction is
        # about FILTERING, and it can only be observed on a table the
        # narrowed credential can still reach.
        grant_table_via_package(conn, "orders", "admin1", group_name=GROUP_CZ)
    finally:
        conn.close()

    client = seeded_app["client"]
    attach = client.put(
        "/api/admin/registry/orders",
        json={"access_policy_sql": POLICY_SQL, "access_policy_note": "RLS pilot: per-country sales scoping (#1979)"},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert attach.status_code == 200, attach.text

    return {
        **seeded_app,
        "alice_token": create_access_token("u_alice", "alice@example.com"),
        "bob_token": create_access_token("u_bob", "bob@example.com"),
        "carol_token": create_access_token("u_carol", "carol@example.com"),
        "admin_stack_token": _mint_pat("admin1", "admin@test.com", surface="stack"),
        "admin_all_token": _mint_pat("admin1", "admin@test.com", surface="all"),
    }


# ---------------------------------------------------------------------------
# 1. Two analysts, one query, two different answers.
# ---------------------------------------------------------------------------


@pytest.mark.journey
class TestTwoGroupsTwoAnswers:
    def test_alice_and_bob_get_different_counts_for_the_same_query(self, pilot):
        c = pilot["client"]

        alice = c.post("/api/query", json={"sql": GROUP_BY_SQL}, headers=_auth(pilot["alice_token"]))
        bob = c.post("/api/query", json={"sql": GROUP_BY_SQL}, headers=_auth(pilot["bob_token"]))
        assert alice.status_code == 200, alice.text
        assert bob.status_code == 200, bob.text

        assert _group_counts(alice.json()) == {"CZ": 3}
        assert _group_counts(bob.json()) == {"DE": 2}
        assert _group_counts(alice.json()) != _group_counts(bob.json())

    def test_alice_sees_only_cz_rows(self, pilot):
        c = pilot["client"]
        r = c.post("/api/query", json={"sql": "SELECT * FROM orders"}, headers=_auth(pilot["alice_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["row_count"] == 3
        assert {row[body["columns"].index("id")] for row in body["rows"]} == CZ_IDS
        assert "Mueller" not in r.text and "Dupont" not in r.text

    def test_bob_sees_only_de_rows(self, pilot):
        c = pilot["client"]
        r = c.post("/api/query", json={"sql": "SELECT * FROM orders"}, headers=_auth(pilot["bob_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["row_count"] == 2
        assert {row[body["columns"].index("id")] for row in body["rows"]} == DE_IDS
        assert "Novak" not in r.text and "Dupont" not in r.text

    def test_an_aggregate_is_each_analysts_own_slice(self, pilot):
        """The disclosure's whole reason to exist: SUM() over a policied
        table is a slice total, not a company total."""
        c = pilot["client"]
        sql = "SELECT sum(CAST(amount AS DOUBLE)) AS s FROM orders"

        alice = c.post("/api/query", json={"sql": sql}, headers=_auth(pilot["alice_token"])).json()
        bob = c.post("/api/query", json={"sql": sql}, headers=_auth(pilot["bob_token"])).json()
        admin = c.post("/api/query", json={"sql": sql}, headers=_auth(pilot["admin_token"])).json()

        assert alice["rows"][0][0] == 600.0
        assert bob["rows"][0][0] == 900.0
        assert admin["rows"][0][0] == 2100.0


# ---------------------------------------------------------------------------
# 2. The ELSE FALSE branch — empty, not broken.
# ---------------------------------------------------------------------------


@pytest.mark.journey
class TestUngroupedCallerGetsZeroRows:
    def test_carol_gets_an_empty_result_not_an_error(self, pilot):
        c = pilot["client"]
        r = c.post("/api/query", json={"sql": "SELECT * FROM orders"}, headers=_auth(pilot["carol_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["row_count"] == 0
        assert body["rows"] == []
        # An empty slice still returns the table's SHAPE — an analyst can
        # tell "no rows for me" from "no such table".
        assert "country" in body["columns"]

    def test_the_empty_slice_still_discloses_that_it_is_a_slice(self, pilot):
        """Zero rows is exactly where silent filtering is most dangerous —
        it is indistinguishable from a legitimately empty table."""
        c = pilot["client"]
        r = c.post("/api/query", json={"sql": GROUP_BY_SQL}, headers=_auth(pilot["carol_token"]))
        assert r.status_code == 200, r.text
        assert r.json()["row_scope"] is not None
        assert "orders" in r.json()["row_scope"]["policied_tables"]

    def test_effective_access_names_the_empty_slice(self, pilot):
        c = pilot["client"]
        r = c.get("/api/me/effective-access", headers=_auth(pilot["carol_token"]))
        assert r.status_code == 200, r.text
        entry = next((t for t in r.json()["tables"] if t["table_id"] == "orders"), None)
        assert entry is not None, r.text
        assert entry["policy"]["applies"] is True
        assert entry["policy"]["rows_visible"] == 0
        assert entry["policy"]["reason"] == "empty_slice"


# ---------------------------------------------------------------------------
# 3. The admin bypass follows the CREDENTIAL SURFACE, not the group.
# ---------------------------------------------------------------------------


@pytest.mark.journey
class TestAdminSurfaceDistinction:
    def test_admin_on_a_full_surface_credential_sees_every_row(self, pilot):
        c = pilot["client"]
        r = c.post("/api/query", json={"sql": "SELECT * FROM orders"}, headers=_auth(pilot["admin_all_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["row_count"] == 6
        assert {row[body["columns"].index("id")] for row in body["rows"]} == ALL_IDS

    def test_admin_on_a_stack_surface_credential_is_filtered_like_an_analyst(self, pilot):
        """The `agnes init` default. Same human, same Admin group, narrowed
        credential — CZ only, because the admin is in the CZ group."""
        c = pilot["client"]
        r = c.post("/api/query", json={"sql": "SELECT * FROM orders"}, headers=_auth(pilot["admin_stack_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["row_count"] == 3
        assert {row[body["columns"].index("id")] for row in body["rows"]} == CZ_IDS

    def test_the_two_admin_credentials_disagree_on_purpose(self, pilot):
        c = pilot["client"]
        full = c.post("/api/query", json={"sql": GROUP_BY_SQL}, headers=_auth(pilot["admin_all_token"]))
        stack = c.post("/api/query", json={"sql": GROUP_BY_SQL}, headers=_auth(pilot["admin_stack_token"]))
        assert _group_counts(full.json()) == {"CZ": 3, "DE": 2, "FR": 1}
        assert _group_counts(stack.json()) == {"CZ": 3}


# ---------------------------------------------------------------------------
# 4. Disclosure.
# ---------------------------------------------------------------------------


@pytest.mark.journey
class TestRowScopeDisclosure:
    @pytest.mark.parametrize("token_key", ["alice_token", "bob_token", "carol_token", "admin_stack_token"])
    def test_every_filtered_response_carries_row_scope(self, pilot, token_key):
        c = pilot["client"]
        r = c.post("/api/query", json={"sql": "SELECT * FROM orders"}, headers=_auth(pilot[token_key]))
        assert r.status_code == 200, r.text
        scope = r.json()["row_scope"]
        assert scope is not None, f"{token_key} got a filtered slice with no disclosure"
        assert scope["policied_tables"] == ["orders"]
        assert scope["note"]

    def test_the_unfiltered_admin_response_carries_none(self, pilot):
        c = pilot["client"]
        r = c.post("/api/query", json={"sql": "SELECT * FROM orders"}, headers=_auth(pilot["admin_all_token"]))
        assert r.status_code == 200, r.text
        assert r.json()["row_scope"] is None

    def test_the_arrow_surface_discloses_in_a_header_since_it_has_no_json_body(self, pilot):
        """`POST /api/v2/scan` streams Arrow IPC, so the same payload rides
        on ``X-Agnes-Row-Scope`` — the snapshot path an analyst uses to
        materialize a slice must disclose too."""
        import json

        c = pilot["client"]
        r = c.post("/api/v2/scan", json={"table_id": "orders"}, headers=_auth(pilot["alice_token"]))
        assert r.status_code == 200, r.text
        scope = json.loads(r.headers["X-Agnes-Row-Scope"])
        assert scope["policied_tables"] == ["orders"]

        admin = c.post("/api/v2/scan", json={"table_id": "orders"}, headers=_auth(pilot["admin_all_token"]))
        assert admin.status_code == 200, admin.text
        assert "X-Agnes-Row-Scope" not in admin.headers


# ---------------------------------------------------------------------------
# 5. The MCP-facing read path.
# ---------------------------------------------------------------------------


@pytest.mark.journey
class TestMcpReadPath:
    """``POST /api/mcp/query-table/{id}`` — the per-table read the MCP tool
    surface calls. An agent is the caller most likely to present a filtered
    aggregate as an organization-wide figure, so this path must filter AND
    disclose exactly like ``/api/query``."""

    def test_alice_sees_only_cz_rows(self, pilot):
        c = pilot["client"]
        r = c.post("/api/mcp/query-table/orders", json={"filter": {}, "limit": 50}, headers=_auth(pilot["alice_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["row_count"] == 3
        assert {row["id"] for row in body["rows"]} == CZ_IDS
        assert {row["country"] for row in body["rows"]} == {"CZ"}

    def test_bob_sees_only_de_rows(self, pilot):
        c = pilot["client"]
        r = c.post("/api/mcp/query-table/orders", json={"filter": {}, "limit": 50}, headers=_auth(pilot["bob_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["row_count"] == 2
        assert {row["id"] for row in body["rows"]} == DE_IDS

    def test_ungrouped_caller_sees_nothing(self, pilot):
        c = pilot["client"]
        r = c.post("/api/mcp/query-table/orders", json={"filter": {}, "limit": 50}, headers=_auth(pilot["carol_token"]))
        assert r.status_code == 200, r.text
        assert r.json()["row_count"] == 0

    def test_admin_full_surface_sees_everything(self, pilot):
        c = pilot["client"]
        r = c.post(
            "/api/mcp/query-table/orders",
            json={"filter": {}, "limit": 50},
            headers=_auth(pilot["admin_all_token"]),
        )
        assert r.status_code == 200, r.text
        assert r.json()["row_count"] == 6

    def test_the_filtered_mcp_response_discloses_row_scope(self, pilot):
        c = pilot["client"]
        filtered = c.post(
            "/api/mcp/query-table/orders", json={"filter": {}, "limit": 50}, headers=_auth(pilot["alice_token"])
        )
        assert filtered.json()["row_scope"] is not None
        assert filtered.json()["row_scope"]["policied_tables"] == ["orders"]

        unfiltered = c.post(
            "/api/mcp/query-table/orders",
            json={"filter": {}, "limit": 50},
            headers=_auth(pilot["admin_all_token"]),
        )
        assert unfiltered.json()["row_scope"] is None

    def test_a_country_filter_cannot_widen_the_slice(self, pilot):
        """The filter runs INSIDE the policy, never instead of it — asking
        for DE as a CZ analyst returns nothing, not the DE rows."""
        c = pilot["client"]
        r = c.post(
            "/api/mcp/query-table/orders",
            json={"filter": {"country": "DE"}, "limit": 50},
            headers=_auth(pilot["alice_token"]),
        )
        assert r.status_code == 200, r.text
        assert r.json()["row_count"] == 0


# ---------------------------------------------------------------------------
# 6. Distribution lockout.
# ---------------------------------------------------------------------------


@pytest.mark.journey
class TestDistributionLockout:
    def test_the_manifest_lists_the_table_but_flags_it_undistributed(self, pilot):
        """`agnes pull` reads this manifest; ``server_only: true`` is what
        makes it list-but-skip the parquet (tests/test_pull_server_only.py
        pins the client half)."""
        c = pilot["client"]
        r = c.get("/api/sync/manifest", headers=_auth(pilot["alice_token"]))
        assert r.status_code == 200, r.text
        entry = r.json()["tables"].get("orders")
        assert entry is not None, r.json()["tables"]
        assert entry["server_only"] is True

    @pytest.mark.parametrize("token_key", ["alice_token", "bob_token", "carol_token", "admin_token"])
    def test_direct_parquet_download_is_403_for_everyone(self, pilot, token_key):
        c = pilot["client"]
        r = c.get("/api/data/orders/download", headers=_auth(pilot[token_key]))
        assert r.status_code == 403, r.text
        assert "server_only" in r.text

    @pytest.mark.parametrize("token_key", ["alice_token", "admin_token"])
    def test_the_reverse_proxy_check_access_gate_is_403_too(self, pilot, token_key):
        """On a Caddy deployment ``forward_auth`` → ``check-access`` →
        ``file_server`` serves the bytes without the app seeing the GET, so
        this is the only place that can close that path."""
        c = pilot["client"]
        r = c.get("/api/data/orders/check-access", headers=_auth(pilot[token_key]))
        assert r.status_code == 403, r.text
        assert "server_only" in r.text


# ---------------------------------------------------------------------------
# 7. The interlock, both directions.
# ---------------------------------------------------------------------------


@pytest.mark.journey
class TestDistributionInterlock:
    def test_flipping_the_policied_pilot_table_to_distributable_is_refused(self, pilot):
        c = pilot["client"]
        r = c.put(
            "/api/admin/registry/orders",
            json={"server_only": False},
            headers=_auth(pilot["admin_token"]),
        )
        assert r.status_code == 422, r.text
        assert "access_policy_requires_undistributed" in r.text

        from src.repositories import table_registry_repo

        row = table_registry_repo().get("orders")
        assert row["server_only"] is True, "the refused write must not have partially landed"
        assert row["access_policy_sql"] is not None

    def test_moving_the_policied_pilot_table_to_a_distributed_query_mode_is_refused(self, pilot):
        c = pilot["client"]
        r = c.put(
            "/api/admin/registry/orders",
            json={"server_only": False, "query_mode": "local"},
            headers=_auth(pilot["admin_token"]),
        )
        assert r.status_code == 422, r.text
        assert "access_policy_requires_undistributed" in r.text

    def test_attaching_the_pilot_policy_to_a_distributed_table_is_refused(self, pilot):
        """The other direction — the policy may not be the thing that makes
        a table safe; the table must already be undistributed."""
        c = pilot["client"]
        reg = c.post(
            "/api/admin/register-table",
            json={
                "name": "orders_distributed",
                "source_type": "keboola",
                "query_mode": "local",
                "bucket": "in.c-sales",
                "source_table": "orders_distributed",
            },
            headers=_auth(pilot["admin_token"]),
        )
        assert reg.status_code == 201, reg.text
        table_id = reg.json()["id"]

        r = c.put(
            f"/api/admin/registry/{table_id}",
            json={
                "access_policy_sql": POLICY_SQL.replace("FROM orders", "FROM orders_distributed"),
                "access_policy_note": "RLS pilot",
            },
            headers=_auth(pilot["admin_token"]),
        )
        assert r.status_code == 422, r.text
        assert "access_policy_requires_undistributed" in r.text
        assert "server_only=true" in r.text

        from src.repositories import table_registry_repo

        assert table_registry_repo().get(table_id)["access_policy_sql"] is None

    def test_the_pilot_table_still_serves_its_slices_after_both_refusals(self, pilot):
        """A refused interlock write must leave enforcement intact, not a
        half-applied row that stops filtering."""
        c = pilot["client"]
        c.put("/api/admin/registry/orders", json={"server_only": False}, headers=_auth(pilot["admin_token"]))
        r = c.post("/api/query", json={"sql": GROUP_BY_SQL}, headers=_auth(pilot["bob_token"]))
        assert r.status_code == 200, r.text
        assert _group_counts(r.json()) == {"DE": 2}


# ---------------------------------------------------------------------------
# 8. The admin preview.
# ---------------------------------------------------------------------------


@pytest.mark.journey
class TestPolicyPreview:
    def test_preview_as_alices_persona(self, pilot):
        c = pilot["client"]
        r = c.post(
            "/api/admin/registry/orders/policy/preview",
            json={"as_user": "alice@example.com"},
            headers=_auth(pilot["admin_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["rows_total"] == 6
        assert body["rows_visible"] == 3
        assert {row["id"] for row in body["sample_rows"]} == CZ_IDS

    def test_preview_as_the_ad_hoc_de_group(self, pilot):
        c = pilot["client"]
        r = c.post(
            "/api/admin/registry/orders/policy/preview",
            json={"as_groups": [GROUP_DE]},
            headers=_auth(pilot["admin_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["rows_total"] == 6
        assert body["rows_visible"] == 2
        assert {row["id"] for row in body["sample_rows"]} == DE_IDS

    def test_preview_as_an_unenumerated_group_shows_the_else_false_branch(self, pilot):
        """The `CASE`-with-a-missing-branch check the doc tells admins to do
        by hand — an unlisted group must come back 0, not everything."""
        c = pilot["client"]
        r = c.post(
            "/api/admin/registry/orders/policy/preview",
            json={"as_groups": [GROUP_OTHER]},
            headers=_auth(pilot["admin_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["rows_visible"] == 0
        assert body["rows_total"] == 6

    def test_preview_columns_are_all_visible_this_policy_masks_nothing(self, pilot):
        c = pilot["client"]
        r = c.post(
            "/api/admin/registry/orders/policy/preview",
            json={"as_groups": [GROUP_CZ]},
            headers=_auth(pilot["admin_token"]),
        )
        assert r.status_code == 200, r.text
        assert all(col["hidden"] is False for col in r.json()["columns"])


# ---------------------------------------------------------------------------
# 9. The table-level gate is a SEPARATE layer from the row-level policy.
# ---------------------------------------------------------------------------


@pytest.mark.journey
class TestTableLevelGateIsSeparateFromRowLevel:
    """MonikaFeigler's live-pilot finding #3 (issue #1979): in her run the
    target table was already reachable through a pre-existing data package
    granted to ``Everyone``, so the two pilot groups only ever gated ROWS,
    never the TABLE itself — the RBAC layer never visibly fired. This class
    proves the two layers are independent and BOTH load-bearing on the
    pilot's own configuration: the ``grant_table_via_package`` fixture wraps
    ``orders`` in one data package granted ONLY to the three pilot groups
    (``sales-cz`` / ``sales-de`` / ``sales-fr``), never to ``Everyone``, so a
    caller outside all three is refused at the TABLE layer before the
    row-level policy ever runs — a refusal that must be distinguishable from
    the ``ELSE FALSE`` empty slice a package-granted-but-policy-unmatched
    caller (carol) gets.
    """

    def test_a_caller_in_no_granted_group_gets_the_table_level_gate(self, pilot):
        """analyst1 (seeded_app's default analyst) belongs to nothing but
        ``Everyone`` — no grant, direct or through a package, reaches
        ``orders``. This must fail BEFORE the policy: a 403 naming the
        table as "not in your stack", never a 200 with an empty slice."""
        c = pilot["client"]
        r = c.post("/api/query", json={"sql": "SELECT * FROM orders"}, headers=_auth(pilot["analyst_token"]))
        assert r.status_code == 403, r.text
        assert "orders" in r.text
        assert "not in your stack" in r.text

    def test_that_403_is_distinguishable_from_the_else_false_zero_row_case(self, pilot):
        """Carol IS in the wrapping package (via sales-fr) but matches no
        CASE branch: she gets a 200 with zero rows — the row-level policy's
        own refusal, not the table-level gate's. Same table, two different
        callers, two different layers, two different HTTP outcomes."""
        c = pilot["client"]
        gated = c.post("/api/query", json={"sql": "SELECT * FROM orders"}, headers=_auth(pilot["analyst_token"]))
        policied = c.post("/api/query", json={"sql": "SELECT * FROM orders"}, headers=_auth(pilot["carol_token"]))

        assert gated.status_code == 403
        assert policied.status_code == 200, policied.text
        assert policied.json()["row_count"] == 0
        assert policied.json()["rows"] == []

    def test_the_wrapping_package_is_granted_only_to_the_pilot_groups_never_everyone(self, pilot):
        """The exact shape of #1979's finding: assert the grant list for the
        package wrapping ``orders``, not just a query outcome — a future
        fixture change that widens the grant (e.g. adds Everyone) must fail
        here even if it doesn't happen to break a query-outcome test."""
        from src.repositories import data_packages_repo, resource_grants_repo

        packages = data_packages_repo().list_packages_of_table("orders")
        assert len(packages) == 1, packages
        pkg_id = packages[0]["id"]

        grants = resource_grants_repo().list_all(resource_type="data_package")
        relevant = [g for g in grants if g["resource_id"] == pkg_id]
        group_names = {g["group_name"] for g in relevant}

        assert group_names == {GROUP_CZ, GROUP_DE, GROUP_OTHER}
        assert "Everyone" not in group_names

    def test_revoking_one_groups_package_grant_hits_the_table_gate_even_though_the_policy_would_admit_the_rows(
        self, pilot
    ):
        """Bob is in sales-de, whose CASE branch matches DE rows — but that
        is a ROW-level fact. Pull the wrapping package's grant for sales-de
        and Bob's next query must be refused at the TABLE layer; the policy
        never gets the chance to run, let alone admit his 2 DE rows."""
        from src.repositories import data_packages_repo, resource_grants_repo

        # Sanity: before revocation, Bob's normal outcome is the policy's DE slice.
        before = pilot["client"].post("/api/query", json={"sql": GROUP_BY_SQL}, headers=_auth(pilot["bob_token"]))
        assert before.status_code == 200, before.text
        assert _group_counts(before.json()) == {"DE": 2}

        pkg_id = data_packages_repo().list_packages_of_table("orders")[0]["id"]
        grants_repo = resource_grants_repo()
        de_grant = next(
            g
            for g in grants_repo.list_all(resource_type="data_package")
            if g["resource_id"] == pkg_id and g["group_name"] == GROUP_DE
        )

        r = pilot["client"].delete(f"/api/admin/grants/{de_grant['id']}", headers=_auth(pilot["admin_token"]))
        assert r.status_code == 204, r.text

        after = pilot["client"].post(
            "/api/query", json={"sql": "SELECT * FROM orders"}, headers=_auth(pilot["bob_token"])
        )
        assert after.status_code == 403, after.text
        assert "not in your stack" in after.text


# ---------------------------------------------------------------------------
# The issue's literal group spelling — pinned as a known failure.
# ---------------------------------------------------------------------------


@pytest.fixture
def literal_pilot(seeded_app, mock_extract_factory, monkeypatch):
    """The same pilot, with the issue's LITERAL group names (``sales_cz`` /
    ``sales_de``) and the issue's literal policy body."""
    from app.auth.jwt import create_access_token
    from src.db import get_system_db
    from src.orchestrator import SyncOrchestrator
    from src.repositories.table_registry import TableRegistryRepository
    from src.repositories.users import UserRepository
    from tests.conftest import grant_table_via_package

    monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")

    env = seeded_app["env"]
    mock_extract_factory("keboola", [{"name": "orders", "data": ROWS}])
    SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

    conn = get_system_db()
    try:
        TableRegistryRepository(conn).register(
            id="orders",
            name="orders",
            source_type="keboola",
            query_mode="materialized",
            server_only=True,
            bucket="in.c-sales",
            source_table="orders",
        )
        UserRepository(conn).create(id="u_dana", email="dana@example.com", name="Dana")
        grant_table_via_package(conn, "orders", "u_dana", group_name="sales_cz")
    finally:
        conn.close()

    client = seeded_app["client"]
    attach = client.put(
        "/api/admin/registry/orders",
        json={
            "access_policy_sql": POLICY_SQL.replace(GROUP_CZ, "sales_cz").replace(GROUP_DE, "sales_de"),
            "access_policy_note": "RLS pilot, issue-literal group spelling (#1979)",
        },
        headers=_auth(seeded_app["admin_token"]),
    )
    assert attach.status_code == 200, attach.text

    return {**seeded_app, "dana_token": create_access_token("u_dana", "dana@example.com")}


@pytest.mark.journey
class TestIssueLiteralGroupNamesUnderscore:
    """#1979's pilot spells its groups ``sales_cz`` / ``sales_de``, and that
    single underscore used to turn the policy OFF on ``POST /api/query``.

    ``policied_relation`` refused to bind ANY live group name containing a
    LIKE/SIMILAR-TO metacharacter (``%``/``_``) and signalled the refusal
    with ``PolicyError``; ``rewrite_sql`` caught ``PolicyError`` around its
    per-name ``resolve(...)`` call to mean "this identifier is not a
    registered table" (a CTE name, an ``information_schema`` view) and
    ``continue``d, so a SECURITY refusal landed in the same branch as a
    benign unknown name: the reference was never substituted and the caller
    read the unfiltered base view -- HTTP 200, all six rows,
    ``row_scope: null``, while ``/api/mcp/query-table`` and
    ``/api/v2/sample`` (which call ``policied_relation`` directly) fail-closed
    with ``500 policy_error`` on the same configuration and ``/api/v2/scan``
    let the exception escape uncaught. Four surfaces, three answers.

    Fixed on both axes, and this class is the end-to-end proof:
    ``PolicyUnknownTable`` split the benign signal out of ``PolicyError`` so
    only IT is swallowed, and the metacharacter refusal was narrowed to a
    policy body that actually matches an identity variable as a PATTERN --
    which this one (``list_contains($user_groups, 'sales_cz')``, an equality
    comparison against a bound list) does not.
    """

    def test_a_member_of_sales_cz_sees_only_their_cz_slice(self, literal_pilot):
        c = literal_pilot["client"]
        r = c.post("/api/query", json={"sql": GROUP_BY_SQL}, headers=_auth(literal_pilot["dana_token"]))
        assert r.status_code == 200, r.text
        assert _group_counts(r.json()) == {"CZ": 3}
        assert r.json()["row_scope"] is not None

    def test_the_same_slice_on_the_mcp_per_table_surface(self, literal_pilot):
        """The surface that already fail-closed must now agree with
        ``/api/query`` on the ANSWER, not merely on refusing."""
        c = literal_pilot["client"]
        r = c.post(
            "/api/mcp/query-table/orders",
            json={"limit": 100},
            headers=_auth(literal_pilot["dana_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert {row["country"] for row in body["rows"]} == {"CZ"}
        assert body["row_scope"] is not None


# ---------------------------------------------------------------------------
# 10. The case of the table name is not a way out of the policy.
# ---------------------------------------------------------------------------


@pytest.mark.journey
class TestTableNameCaseDoesNotBypassThePolicy:
    """DuckDB's catalog folds identifiers -- ``FROM ORDERS`` and even
    ``FROM "Orders"`` resolve to the view created as ``orders`` -- while the
    registry lookup behind ``policied_relation`` was exact-equality on both
    backends. So a caller who merely SHOUTED the table name got
    ``PolicyUnknownTable``, the one outcome ``rewrite_sql`` swallows as "not
    a registered table", and read every raw row with a 200 and
    ``row_scope: null`` (#1979, security review).
    """

    @pytest.mark.parametrize("spelling", ["ORDERS", "Orders", '"Orders"', '"ORDERS"'])
    def test_alice_is_filtered_whatever_the_casing(self, pilot, spelling):
        c = pilot["client"]
        r = c.post("/api/query", json={"sql": f"SELECT * FROM {spelling}"}, headers=_auth(pilot["alice_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["row_count"] == 3, body
        assert {row[body["columns"].index("id")] for row in body["rows"]} == CZ_IDS
        assert "Mueller" not in r.text and "Dupont" not in r.text
        assert body["row_scope"] is not None
        assert "orders" in body["row_scope"]["policied_tables"]

    def test_an_uppercase_aggregate_is_still_the_callers_own_slice(self, pilot):
        c = pilot["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT sum(CAST(amount AS DOUBLE)) AS s FROM ORDERS"},
            headers=_auth(pilot["bob_token"]),
        )
        assert r.status_code == 200, r.text
        assert r.json()["rows"][0][0] == 900.0

    def test_ambiguous_case_variant_registry_rows_are_refused(self, pilot):
        """Two registry rows differing only by case: the analytics catalog
        can hold only ONE of the two views, so which policy applies is
        unknowable. Fail closed with the structured ``policy_error`` --
        never serve the raw view."""
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository

        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="orders_upper",
                name="ORDERS",
                source_type="keboola",
                query_mode="materialized",
                server_only=True,
            )
        finally:
            conn.close()

        c = pilot["client"]
        r = c.post("/api/query", json={"sql": "SELECT * FROM orders"}, headers=_auth(pilot["alice_token"]))
        assert r.status_code == 500, r.text
        assert r.json()["detail"]["reason"] == "policy_error"
        assert "Novak" not in r.text


# ---------------------------------------------------------------------------
# 11. Policy-CONTENT admin surfaces are surface-checked (F2, security review).
# ---------------------------------------------------------------------------


@pytest.mark.journey
class TestPolicyContentSurfacesRequireFullSurface:
    """The four admin routes that hand back policy CONTENT -- raw unfiltered
    sample rows (`.../policy/preview`), per-group visibility counts
    (`.../policy/preview-groups`), profiler sample values
    (`.../policy/columns`) and historical policy bodies
    (`.../policy/revisions`) -- have no per-table grant check and no policy
    rewrite standing behind them: their admin gate is the ONLY thing between
    the caller and unpolicied data. That is exactly the shape K2 fixed on
    ``POST /api/query/hybrid``, so they take the same
    ``require_admin_all_surface`` gate: a ``surface='stack'`` admin PAT (the
    ``agnes init`` default, filtered like an analyst everywhere else) is
    refused with the distinct 403 that names the fix.
    """

    ROUTES = [
        ("POST", "/api/admin/registry/orders/policy/preview", {"as_groups": [GROUP_CZ]}),
        ("POST", "/api/admin/registry/orders/policy/preview-groups", {}),
        ("GET", "/api/admin/registry/orders/policy/columns", None),
        ("GET", "/api/admin/registry/orders/policy/revisions", None),
    ]

    def _call(self, c, method, url, body, token):
        if method == "POST":
            return c.post(url, json=body, headers=_auth(token))
        return c.get(url, headers=_auth(token))

    @pytest.mark.parametrize("method,url,body", ROUTES)
    def test_stack_surface_admin_pat_is_refused(self, pilot, method, url, body):
        r = self._call(pilot["client"], method, url, body, pilot["admin_stack_token"])
        assert r.status_code == 403, r.text
        detail = r.json()["detail"]
        assert detail != "Admin access required"
        assert "surface" in detail
        # None of the content these routes exist to return may leak out.
        assert "Novak" not in r.text and "Mueller" not in r.text

    @staticmethod
    def _assert_passed_the_gate(r):
        """200, or the typed 501 the PG-only ``access_policy_revisions`` repo
        answers with on this DuckDB app-state fixture (A3) -- either way the
        surface gate let the caller through, which is what is under test."""
        if r.status_code == 501:
            assert r.json()["error"] == "requires_postgres_backend", r.text
            return
        assert r.status_code == 200, r.text

    @pytest.mark.parametrize("method,url,body", ROUTES)
    def test_full_surface_admin_pat_is_allowed(self, pilot, method, url, body):
        r = self._call(pilot["client"], method, url, body, pilot["admin_all_token"])
        self._assert_passed_the_gate(r)

    @pytest.mark.parametrize("method,url,body", ROUTES)
    def test_the_browser_session_credential_still_works(self, pilot, method, url, body):
        """The admin web UI's own credential carries no ``credential_surface``
        key, which reads as ``'all'`` -- the ``/admin/tables`` policy modal
        must keep working."""
        r = self._call(pilot["client"], method, url, body, pilot["admin_token"])
        self._assert_passed_the_gate(r)

    def test_compile_stays_on_the_plain_admin_gate(self, pilot):
        """``.../policy/compile`` persists nothing and returns only generated
        SQL -- no table content -- so it is deliberately NOT narrowed."""
        r = pilot["client"].post(
            "/api/admin/registry/orders/policy/compile",
            json={"row_rules": [], "masks": [], "default_action": "deny"},
            headers=_auth(pilot["admin_stack_token"]),
        )
        assert r.status_code != 403, r.text
