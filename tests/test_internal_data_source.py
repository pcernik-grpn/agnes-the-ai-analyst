"""Tests for the ``internal`` data source.

Coverage:
- Per-row RBAC: alice sees only alice's rows; admin sees everyone's.
- Catalog: internal tables show up in /api/v2/catalog.
- Schema: /api/v2/schema/agnes_sessions returns column metadata.
- Cross-source rejection: SQL mixing ``agnes_sessions`` with a registered
  Keboola/BQ table id is rejected before execution.
- Username sanitization: an exotic local-part is rejected, not silently
  scoped to the wrong rows.
"""

from __future__ import annotations

import pytest

from connectors.internal.access import (
    INTERNAL_TABLES_BY_ID,
    InternalAccessError,
    build_filter_clause,
    execute_internal_query,
    find_internal_refs,
    get_schema,
    is_internal_table,
)
from connectors.internal.registry import ensure_internal_tables_registered
from src.db import _ensure_schema, _get_state_dir


@pytest.fixture
def system_db(tmp_path, monkeypatch):
    """Stand up a fresh system.duckdb with the v43 schema + registered internal
    rows. ``get_system_db`` is the singleton consumed by the connector, so we
    redirect AGNES_DATA_DIR to a per-test path before the helper opens the
    file."""
    data_dir = tmp_path / "agnes_data"
    (data_dir / "state").mkdir(parents=True)
    # ``src.db._get_data_dir`` reads ``DATA_DIR`` (not ``AGNES_DATA_DIR``).
    # Setting it before ``get_system_db()`` runs reroutes the singleton to
    # the per-test tmp_path.
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.delenv("STATE_DIR", raising=False)

    # Force close any pre-existing handle held by an earlier test.
    from src.db import close_system_db

    close_system_db()

    from src.db import get_system_db

    conn = get_system_db()
    _ensure_schema(conn)
    ensure_internal_tables_registered()
    # Seed a couple of canonical rows for the RBAC checks.
    conn.execute(
        "INSERT INTO usage_session_summary "
        "(session_file, session_id, username, user_id, tool_calls, tool_errors, processor_version) VALUES "
        "('alice/s1.jsonl', 's-a-1', 'alice', 'alice-uuid', 10, 1, 1)"
    )
    conn.execute(
        "INSERT INTO usage_session_summary "
        "(session_file, session_id, username, user_id, tool_calls, tool_errors, processor_version) VALUES "
        "('bob/s2.jsonl',   's-b-1', 'bob', 'bob-uuid',   20, 0, 1)"
    )
    conn.execute(
        "INSERT INTO audit_log (id, user_id, action, result) VALUES "
        "('a-1', 'alice-uuid', 'session.transcript_view', 'success')"
    )
    conn.execute(
        "INSERT INTO audit_log (id, user_id, action, result) VALUES "
        "('a-2', 'bob-uuid',   'session.transcript_view', 'success')"
    )
    yield conn
    close_system_db()


# ---------------------------------------------------------------------------
# Static helpers — no DB needed
# ---------------------------------------------------------------------------


def test_is_internal_table_recognises_canonical_ids():
    assert is_internal_table("agnes_sessions")
    assert is_internal_table("agnes_telemetry")
    assert is_internal_table("agnes_audit")
    assert not is_internal_table("usage_session_summary")  # underlying physical
    assert not is_internal_table("orders_daily")


def test_find_internal_refs_word_boundary():
    refs = find_internal_refs("SELECT * FROM agnes_sessions WHERE x = 'foo'")
    assert refs == ["agnes_sessions"]


def test_find_internal_refs_multiple_in_declaration_order():
    refs = find_internal_refs("SELECT * FROM agnes_audit JOIN agnes_sessions USING (x)")
    # Order follows INTERNAL_TABLES declaration, not the SQL order.
    assert refs == ["agnes_sessions", "agnes_audit"]


def test_find_internal_refs_ignores_unrelated():
    refs = find_internal_refs("SELECT * FROM orders_daily")
    assert refs == []


def test_filter_clause_admin_is_empty():
    table = INTERNAL_TABLES_BY_ID["agnes_sessions"]
    assert build_filter_clause(table, {"email": "admin@x", "id": "admin-uuid"}, True) == ""


def test_filter_clause_non_admin_scopes_to_user_id():
    """agnes_sessions filters on user_id with OR username fallback for
    pre-backfill rows."""
    table = INTERNAL_TABLES_BY_ID["agnes_sessions"]
    clause = build_filter_clause(table, {"email": "alice@example.com", "id": "alice-uuid"}, False)
    assert clause == "WHERE (user_id = 'alice-uuid' OR username = 'alice')"


def test_filter_clause_non_admin_scopes_audit_to_user_id():
    table = INTERNAL_TABLES_BY_ID["agnes_audit"]
    clause = build_filter_clause(
        table,
        {"email": "alice@example.com", "id": "alice-uuid"},
        False,
    )
    assert clause == "WHERE user_id = 'alice-uuid'"


def test_filter_clause_rejects_unsafe_user_id():
    table = INTERNAL_TABLES_BY_ID["agnes_sessions"]
    with pytest.raises(InternalAccessError):
        build_filter_clause(
            table,
            {"email": "alice@example.com", "id": "'; DROP TABLE--"},
            False,
        )


# ---------------------------------------------------------------------------
# End-to-end RBAC — runs against a real (fresh) system.duckdb
# ---------------------------------------------------------------------------


def test_admin_sees_every_user_session(system_db, tmp_path):
    db_path = str(_get_state_dir() / "system.duckdb")
    _, rows, _ = execute_internal_query(
        db_path,
        {"email": "admin@x", "id": "admin-uuid"},
        is_admin=True,
        sql="SELECT username FROM agnes_sessions ORDER BY username",
    )
    assert [r[0] for r in rows] == ["alice", "bob"]


def test_non_admin_sees_only_own_sessions(system_db):
    db_path = str(_get_state_dir() / "system.duckdb")
    _, rows, _ = execute_internal_query(
        db_path,
        {"email": "alice@example.com", "id": "alice-uuid"},
        is_admin=False,
        sql="SELECT username, session_id FROM agnes_sessions",
    )
    assert rows == [("alice", "s-a-1")]


def test_non_admin_sees_sessions_uploaded_via_api(system_db):
    """Sessions uploaded via POST /api/upload/sessions are stored under
    user_id (UUID) by the pipeline. The user_id filter covers both
    ingestion paths (collector and upload API) because the pipeline
    resolves the stable user_id for every session at processing time."""
    from src.db import get_system_db

    conn = get_system_db()
    conn.execute(
        "INSERT INTO usage_session_summary "
        "(session_file, session_id, username, user_id, tool_calls, tool_errors, processor_version) VALUES "
        "('550e8400-uuid/api.jsonl', 's-api-1', '550e8400-uuid', '550e8400-uuid', 5, 0, 1)"
    )
    db_path = str(_get_state_dir() / "system.duckdb")
    _, rows, _ = execute_internal_query(
        db_path,
        {"email": "alice@example.com", "id": "550e8400-uuid"},
        is_admin=False,
        sql="SELECT username, session_id FROM agnes_sessions ORDER BY session_id",
    )
    # alice should see both her collector row (user_id='alice-uuid' from
    # fixture — won't match '550e8400-uuid') and her upload-API row
    # (user_id='550e8400-uuid').
    assert ("550e8400-uuid", "s-api-1") in rows
    # bob's rows must NOT appear
    assert all(r[0] != "bob" for r in rows)


def test_email_change_does_not_affect_visibility(system_db):
    """If alice changes her email from alice@example.com to
    alice.new@example.com, her sessions should still be visible
    because the filter uses the stable user_id, not the email."""
    db_path = str(_get_state_dir() / "system.duckdb")
    # Query with a different email but the same user_id.
    _, rows, _ = execute_internal_query(
        db_path,
        {"email": "alice.new@example.com", "id": "alice-uuid"},
        is_admin=False,
        sql="SELECT username, session_id FROM agnes_sessions",
    )
    assert rows == [("alice", "s-a-1")]


def test_non_admin_sees_only_own_audit_rows(system_db):
    db_path = str(_get_state_dir() / "system.duckdb")
    _, rows, _ = execute_internal_query(
        db_path,
        {"email": "bob@example.com", "id": "bob-uuid"},
        is_admin=False,
        sql="SELECT user_id, action FROM agnes_audit",
    )
    assert rows == [("bob-uuid", "session.transcript_view")]


def test_admin_can_count_per_user_session_views(system_db):
    """The motivating admin query: who is looking at whose session transcripts?"""
    db_path = str(_get_state_dir() / "system.duckdb")
    _, rows, _ = execute_internal_query(
        db_path,
        {"email": "admin@x", "id": "admin-uuid"},
        is_admin=True,
        sql=(
            "SELECT user_id, COUNT(*) AS n FROM agnes_audit "
            "WHERE action = 'session.transcript_view' GROUP BY user_id "
            "ORDER BY user_id"
        ),
    )
    assert rows == [("alice-uuid", 1), ("bob-uuid", 1)]


def test_user_sql_inside_cte_wrapper_still_resolves(system_db):
    """The CTE wrapper that scopes agnes_* aliases must coexist with
    user-supplied SELECT shape — including basic aggregations + LIMIT."""
    db_path = str(_get_state_dir() / "system.duckdb")
    _, rows, _ = execute_internal_query(
        db_path,
        {"email": "alice@example.com", "id": "alice-uuid"},
        is_admin=False,
        sql="SELECT SUM(tool_calls) AS n FROM agnes_sessions",
    )
    assert rows == [(10,)]


def test_trailing_semicolon_does_not_break_the_cte_wrap(system_db):
    """/api/query's SELECT-only guard tolerates one trailing `;`; this wrap
    must honor that instead of raising a DuckDB parser error."""
    db_path = str(_get_state_dir() / "system.duckdb")
    _, rows, _ = execute_internal_query(
        db_path,
        {"email": "alice@example.com", "id": "alice-uuid"},
        is_admin=False,
        sql="SELECT SUM(tool_calls) AS n FROM agnes_sessions;",
    )
    assert rows == [(10,)]


def test_schema_returns_underlying_columns(system_db):
    db_path = str(_get_state_dir() / "system.duckdb")
    cols = get_schema(db_path, "agnes_sessions")
    names = {c["name"] for c in cols}
    # Sample of the documented usage_session_summary columns.
    assert {"session_file", "session_id", "username", "tool_calls"} <= names


def test_get_schema_unknown_table_returns_empty():
    assert get_schema("/nonexistent/path", "not_a_real_internal_table") == []


def test_execute_rejects_sql_without_internal_refs():
    with pytest.raises(InternalAccessError):
        execute_internal_query(
            "/tmp/dummy",
            {"email": "alice@x", "id": "alice-uuid"},
            is_admin=False,
            sql="SELECT 1",
        )


# ---------------------------------------------------------------------------
# FastAPI-level wiring tests — catch the two arity bugs that the unit
# tests on `execute_internal_query` / `build_filter_clause` missed,
# because those test the connector primitives directly and skip the
# request handler layer where `is_user_admin(user)` was mis-called.
# ---------------------------------------------------------------------------


def _seed_internal_via_api():
    """``seeded_app`` bypasses the FastAPI lifespan, so the registry seed
    that puts agnes_sessions / agnes_telemetry / agnes_audit into
    ``table_registry`` doesn't run. Register them inline so the API
    routes (`/api/query`, `/api/v2/sample`) can find them."""
    from connectors.internal.registry import ensure_internal_tables_registered

    ensure_internal_tables_registered()


def test_query_internal_via_api_admin_path(seeded_app, admin_user):
    """POST /api/query with SQL referencing agnes_sessions returns rows
    for an admin caller. Catches the regression where
    app/api/query.py:_run_internal_query called is_user_admin(user) with
    a dict instead of (user_id, conn) — request blew up with TypeError.
    """
    _seed_internal_via_api()
    from src.db import get_system_db

    conn = get_system_db()
    conn.execute(
        "INSERT INTO usage_session_summary "
        "(session_file, session_id, username, processor_version) VALUES "
        "('alice/api-1.jsonl', 's-api-1', 'alice', 1) "
        "ON CONFLICT (session_file) DO NOTHING"
    )
    resp = seeded_app["client"].post(
        "/api/query",
        json={"sql": "SELECT COUNT(*) AS n FROM agnes_sessions"},
        headers=admin_user,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["row_count"] == 1
    # Admin sees all rows seeded earlier + this fixture's row.
    assert int(body["rows"][0][0]) >= 1


def test_sample_internal_via_api_admin_path(seeded_app, admin_user):
    """GET /api/v2/sample/agnes_sessions returns rows for an admin caller.

    Catches the regression where v2_sample.py called is_user_admin(user)
    with a dict instead of (user_id, conn) — request blew up with 500.
    """
    _seed_internal_via_api()
    resp = seeded_app["client"].get(
        "/api/v2/sample/agnes_sessions?n=3",
        headers=admin_user,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["table_id"] == "agnes_sessions"
    assert body["source"] == "internal"
    assert isinstance(body["rows"], list)


def _register(table_id):
    from src.repositories import table_registry_repo

    table_registry_repo().register(id=table_id, name=table_id, source_type="keboola", query_mode="local")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT session_id FROM agnes_sessions ORDER BY session_id DESC LIMIT 2",
        "SELECT session_id FROM agnes_sessions LIMIT 2",
        "SELECT count(*) FILTER (WHERE session_id IS NOT NULL) FROM agnes_sessions",
    ],
)
def test_ordinary_syntax_on_an_internal_table_is_not_a_cross_plane_join(seeded_app, admin_user, sql):
    """The reported bug: with tables registered as `order`, `limit` and
    `filter`, ordinary SQL against an internal table was rejected as a join
    with them."""
    _seed_internal_via_api()
    for name in ("order", "limit", "filter"):
        _register(name)
    resp = seeded_app["client"].post("/api/query", json={"sql": sql}, headers=admin_user)
    assert resp.status_code == 200, resp.text


def test_genuine_mix_of_internal_and_registered_table_still_rejected(seeded_app, admin_user):
    """The guard still does its job — including when the registered id has to
    be quoted because it is a SQL keyword."""
    _seed_internal_via_api()
    _register("order")
    resp = seeded_app["client"].post(
        "/api/query",
        json={"sql": 'SELECT s.session_id FROM agnes_sessions s JOIN "order" o ON s.session_id = o.id'},
        headers=admin_user,
    )
    assert resp.status_code == 400, resp.text
    assert "can't be joined" in resp.json()["detail"]


def test_genuine_mix_via_comma_list_still_rejected(seeded_app, admin_user):
    _seed_internal_via_api()
    _register("issues")
    resp = seeded_app["client"].post(
        "/api/query",
        json={"sql": "SELECT s.session_id FROM agnes_sessions s, issues i"},
        headers=admin_user,
    )
    assert resp.status_code == 400, resp.text
    assert "can't be joined" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Negative tests — non-admin must not bypass row filter by referencing
# the underlying physical tables or by smuggling the alias into a string
# literal to enter the privileged code path. Both vectors flagged in the
# round-2 review (PR #278 R2 finding #1).
# ---------------------------------------------------------------------------


def test_string_literal_alone_does_not_route_internal(system_db):
    """A SQL with `agnes_sessions` only inside a string literal should not
    be treated as referencing the internal table — find_internal_refs
    strips literals first."""
    refs = find_internal_refs("SELECT 'agnes_sessions' AS x")
    assert refs == []


def test_non_admin_cannot_reference_underlying_physical_table(system_db):
    """If a non-admin's SQL touches an agnes_* alias AND a base
    table (usage_session_summary etc.) directly, the request is
    rejected before execution — the CTE wrapper would otherwise only
    scope the alias, leaking every user's rows."""
    db_path = str(_get_state_dir() / "system.duckdb")
    payloads = (
        "SELECT * FROM usage_session_summary WHERE 'agnes_sessions'='agnes_sessions'",
        "SELECT * FROM agnes_sessions UNION ALL SELECT * FROM audit_log LIMIT 1",
        # CTE shadow attempt — still has to touch the base table to do anything.
        "WITH agnes_sessions AS (SELECT * FROM usage_events) SELECT * FROM agnes_sessions",
    )
    for sql in payloads:
        with pytest.raises(InternalAccessError):
            execute_internal_query(
                db_path,
                {"email": "alice@x", "id": "alice-uuid"},
                is_admin=False,
                sql=sql,
            )


def test_admin_unaffected_by_underlying_table_guard(system_db):
    """Admins can reference underlying physical tables — they're already
    god-mode through the filter clause, and they have legitimate reasons
    to read raw rows."""
    db_path = str(_get_state_dir() / "system.duckdb")
    # Admin must still reference at least one agnes_* alias to enter the
    # internal-query path (the routing rule unchanged), but the base
    # table guard doesn't apply.
    _, rows, _ = execute_internal_query(
        db_path,
        {"email": "admin@x", "id": "admin-uuid"},
        is_admin=True,
        sql="SELECT (SELECT COUNT(*) FROM usage_session_summary) AS n FROM agnes_sessions LIMIT 1",
    )
    assert rows  # admin can join base table inside the wrapper


def test_non_admin_cannot_reference_users_table(system_db):
    """R3 finding: non-admin must not reach any non-agnes_* table in
    system.duckdb (users / personal_access_tokens / resource_grants /
    saved views / etc.). The dynamic denylist from information_schema
    covers all of them; this test pins one critical case (`users`)."""
    db_path = str(_get_state_dir() / "system.duckdb")
    with pytest.raises(InternalAccessError):
        execute_internal_query(
            db_path,
            {"email": "alice@x", "id": "alice-uuid"},
            is_admin=False,
            sql="SELECT * FROM agnes_sessions UNION ALL SELECT email, id, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL FROM users LIMIT 1",
        )


def test_non_admin_cannot_reference_pat_table(system_db):
    """PAT table is one of the most sensitive — explicit pin."""
    db_path = str(_get_state_dir() / "system.duckdb")
    with pytest.raises(InternalAccessError):
        execute_internal_query(
            db_path,
            {"email": "alice@x", "id": "alice-uuid"},
            is_admin=False,
            sql="SELECT * FROM agnes_audit; SELECT * FROM personal_access_tokens",
        )


def test_non_admin_block_survives_block_comment(system_db):
    """`/* */` comment-wrapped table name should still be caught — the
    pre-pass strips comments before the identifier scan."""
    db_path = str(_get_state_dir() / "system.duckdb")
    with pytest.raises(InternalAccessError):
        execute_internal_query(
            db_path,
            {"email": "alice@x", "id": "alice-uuid"},
            is_admin=False,
            sql="SELECT * FROM agnes_sessions /**/ JOIN /**/ users /**/ ON 1=1",
        )


def test_non_admin_block_survives_line_comment(system_db):
    """`--` line-comment shouldn't hide the table name from the scan."""
    db_path = str(_get_state_dir() / "system.duckdb")
    with pytest.raises(InternalAccessError):
        execute_internal_query(
            db_path,
            {"email": "alice@x", "id": "alice-uuid"},
            is_admin=False,
            sql=(
                "SELECT * FROM agnes_sessions\n"
                "-- this is a comment but we still touch the table below\n"
                "UNION ALL SELECT email, id, NULL, NULL, NULL, NULL, NULL, NULL, "
                "NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL FROM users LIMIT 1"
            ),
        )
