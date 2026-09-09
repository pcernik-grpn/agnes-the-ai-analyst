"""POST /api/query must reject direct `bigquery_query()` function calls.

This is a pre-existing RBAC bypass: `bigquery_query('proj', 'SELECT * FROM
ds.tbl')` runs a BQ jobs API call against any reachable dataset, ignoring
the master-view forbidden-table check that gates registered names. Closes
that hole by adding `bigquery_query` to the SQL keyword blocklist.

Internal wrap views (created by the BQ extractor) use bigquery_query()
inside their CREATE VIEW body — those run via DuckDB's view resolution at
query time, NOT via user-submitted SQL, so the blocklist doesn't break
them. Closes part of #160.

#2424 follow-up (2026-09-09 production finding): `SELECT * FROM
bigquery_query(...)` IS a single SELECT, so the blocklist's old one-size
"Only single SELECT queries are allowed" detail was false for this case.
The guard now names the reason class it actually matched (file-access /
remote-query functions) instead.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_bigquery_query_function_call_rejected(seeded_app):
    """Plain `SELECT * FROM bigquery_query(...)` is blocked at the
    keyword-blocklist layer with the file-access/remote-query class detail,
    naming the actual function rather than falsely claiming this isn't a
    single SELECT."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    sql = "SELECT * FROM bigquery_query('proj', 'SELECT 1 AS x')"
    r = c.post(
        "/api/query",
        json={"sql": sql},
        headers=_auth(token),
    )
    assert r.status_code == 400, f"expected 400; got {r.status_code} body={r.json()}"
    detail = str(r.json().get("detail", ""))
    assert "bigquery_query" in detail, f"expected the matched function named; got detail={detail!r}"
    assert "single SELECT" not in detail, (
        f"this IS a single SELECT — the refusal must not claim otherwise; got detail={detail!r}"
    )


def test_bigquery_query_mixed_case_rejected(seeded_app):
    """Existing blocklist runs `sql.strip().lower()` first, so any case
    variant is blocked uniformly."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT * FROM BigQuery_Query('proj', 'SELECT 1')"},
        headers=_auth(token),
    )
    assert r.status_code == 400, r.json()
    detail = str(r.json().get("detail", ""))
    assert "bigquery_query" in detail, f"expected the matched function named; got detail={detail!r}"


def test_bigquery_query_with_whitespace_before_paren_rejected(seeded_app):
    """Substring match catches `bigquery_query (...)` with space too."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT * FROM bigquery_query   ('proj', 'SELECT 1')"},
        headers=_auth(token),
    )
    assert r.status_code == 400, r.json()
    detail = str(r.json().get("detail", ""))
    assert "bigquery_query" in detail, f"expected the matched function named; got detail={detail!r}"
