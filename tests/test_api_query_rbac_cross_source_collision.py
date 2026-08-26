"""#868: /api/query must block catalog-qualified access to un-granted extract
catalogs.

Each source's extract.duckdb is ATTACHed as its own catalog named after the
source, while the analyst-facing master views live in the default catalog. The
pre-#868 non-admin RBAC was a denylist of master-VIEW names, so a catalog-
qualified path `<ungranted_source>.main."<name>"` reached the un-granted
source's rows directly — the base relations in other catalogs are invisible to
the view-name denylist. These tests pin the catalog-level gate.
"""

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _grant_table(conn, user_id: str, table_id: str) -> str:
    """Grant a table to a user via an auto data_package + custom group
    (mirrors tests/test_journey_rbac.py)."""
    from tests.conftest import grant_table_via_package

    return grant_table_via_package(conn, table_id, user_id, group_name=f"c868-{user_id}")


def _register(c, admin_token, *, name, source):
    c.post(
        "/api/admin/register-table",
        json={"name": name, "source_type": source, "query_mode": "local", "description": name},
        headers=_auth(admin_token),
    )


@pytest.fixture
def two_sources(seeded_app, mock_extract_factory):
    """Two local extract catalogs: `keboola` (table `pub`, granted to analyst)
    and `jira` (table `secret`, NOT granted). The catalog name is the extract
    dir / source_type; both are valid source types that build local (file-backed
    'duckdb') catalogs. Returns the seeded_app dict."""
    c = seeded_app["client"]
    env = seeded_app["env"]
    admin = seeded_app["admin_token"]

    _register(c, admin, name="pub", source="keboola")
    _register(c, admin, name="secret", source="jira")
    mock_extract_factory("keboola", [{"name": "pub", "data": [{"id": "1", "v": "public"}]}])
    mock_extract_factory("jira", [{"name": "secret", "data": [{"id": "1", "v": "classified"}]}])

    from src.orchestrator import SyncOrchestrator

    SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

    # Grant analyst only the `pub` table (in srca); srcb stays un-granted.
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        pub_row = TableRegistryRepository(conn).get_by_name("pub")
        assert pub_row, "pub not registered — fixture setup broken"
        _grant_table(conn, "analyst1", pub_row["id"])
    finally:
        conn.close()
    return seeded_app


def test_catalog_qualified_ref_to_ungranted_source_is_403(two_sources):
    """The exploit: analyst granted `pub` reads the un-granted `srcb` catalog
    directly via a catalog-qualified path. Must 403 (pre-#868 it leaked)."""
    c = two_sources["client"]
    tok = two_sources["analyst_token"]
    r = c.post(
        "/api/query",
        json={"sql": 'SELECT * FROM jira.main."secret"'},
        headers=_auth(tok),
    )
    assert r.status_code == 403, r.text
    assert "un-granted source catalog" in r.text, r.text


def test_quoted_catalog_qualified_ref_is_403(two_sources):
    """The quoted-catalog evasion (`"jira"."main"."secret"`) resolves to the
    same ATTACHed catalog and must be caught too."""
    c = two_sources["client"]
    tok = two_sources["analyst_token"]
    r = c.post(
        "/api/query",
        json={"sql": 'SELECT * FROM "jira"."main"."secret"'},
        headers=_auth(tok),
    )
    assert r.status_code == 403, r.text
    assert "un-granted source catalog" in r.text, r.text


def test_granted_unqualified_master_view_still_works(two_sources):
    """The legitimate surface — the unqualified granted master view — must NOT
    be caught by the catalog gate."""
    c = two_sources["client"]
    tok = two_sources["analyst_token"]
    r = c.post("/api/query", json={"sql": "SELECT * FROM pub"}, headers=_auth(tok))
    assert r.status_code == 200, r.text
    assert any(row and row[0] == "1" for row in r.json().get("rows", [])) or r.json().get("row_count", 0) >= 1


def test_internal_metadata_table_still_403_m1(two_sources):
    """M1 regression: catalog-qualified internal tables stay blocked (now via
    the catalog gate as well)."""
    c = two_sources["client"]
    tok = two_sources["analyst_token"]
    r = c.post(
        "/api/query",
        json={"sql": 'SELECT * FROM jira.main."_meta"'},
        headers=_auth(tok),
    )
    assert r.status_code == 403, r.text


def test_admin_bypasses_catalog_gate(two_sources):
    """Admins see all catalogs — the catalog gate must not 403 them."""
    c = two_sources["client"]
    tok = two_sources["admin_token"]
    r = c.post(
        "/api/query",
        json={"sql": 'SELECT * FROM jira.main."secret"'},
        headers=_auth(tok),
    )
    assert r.status_code != 403, r.text


# --- #1394: the catalog gate scanned RAW SQL text, so a string literal or
# comment merely CONTAINING a registered catalog name followed by a dot
# (`SELECT 'jira.com' AS x`) was refused as an authorization failure even
# though the query references no catalog at all. Fixed by masking string /
# dollar-quoted literals and comments before the regex runs — quoted
# IDENTIFIERS stay visible (see `test_quoted_catalog_qualified_ref_is_403`
# above, which pins that a genuine quoted-catalog reference must still 403).


def test_string_literal_containing_catalog_name_is_not_403(two_sources):
    """The exact #1394 repro: a literal, not a reference."""
    c = two_sources["client"]
    tok = two_sources["analyst_token"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT 'jira.com' AS x"},
        headers=_auth(tok),
    )
    assert r.status_code == 200, r.text


def test_realistic_domain_filter_literal_is_not_403(two_sources):
    """The realistic shape from the issue: filtering a granted table by an
    email-domain literal that happens to spell a registered catalog name."""
    c = two_sources["client"]
    tok = two_sources["analyst_token"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT count(*) AS n FROM pub WHERE v LIKE '%@jira.com'"},
        headers=_auth(tok),
    )
    assert r.status_code == 200, r.text


def test_comment_containing_catalog_name_is_not_403(two_sources):
    """A SQL comment is inert — DuckDB never executes it — so a catalog name
    inside one must not trip the gate either."""
    c = two_sources["client"]
    tok = two_sources["analyst_token"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT 1 AS n -- jira.com"},
        headers=_auth(tok),
    )
    assert r.status_code == 200, r.text


def test_decoy_literal_does_not_shield_a_real_catalog_reference(two_sources):
    """Mixed case: a literal containing the catalog name AND a genuine
    catalog-qualified reference in the same query — the real one must still
    be caught."""
    c = two_sources["client"]
    tok = two_sources["analyst_token"]
    r = c.post(
        "/api/query",
        json={"sql": 'SELECT * FROM jira.main."secret" WHERE 1=1 -- jira.com decoy'},
        headers=_auth(tok),
    )
    assert r.status_code == 403, r.text
    assert "un-granted source catalog" in r.text, r.text
