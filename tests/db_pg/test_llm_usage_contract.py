"""Cross-engine contract tests for the llm_usage ledger repository."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.llm_usage import LlmUsageRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return LlmUsageRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.llm_usage_pg import LlmUsagePgRepository

    return LlmUsagePgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    if request.param == "duckdb":
        r, conn = _make_duckdb_repo(tmp_path)
        yield r
        conn.close()
    else:
        r, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield r


def test_batch_and_month_total(repo):
    repo.insert_batch(
        [
            {
                "id": "r1",
                "agent_id": "a1",
                "user_id": "u1",
                "session_id": "c1",
                "model": "claude-sonnet-5",
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_tokens": 10,
                "cache_creation_tokens": 5,
            },
            {
                "id": "r2",
                "agent_id": "a1",
                "user_id": "u1",
                "session_id": "c1",
                "model": "claude-haiku-4-5-20251001",
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
            },
        ]
    )
    ym = datetime.now(timezone.utc).strftime("%Y-%m")
    assert repo.month_total_tokens("a1", ym) == 100 + 50 + 5 + 10 + 5
    assert repo.month_total_tokens("a2", ym) == 0
    assert len(repo.list_for_agent("a1")) == 2


def test_usage_breakdown_for_month(repo):
    repo.insert_batch(
        [
            {
                "id": "r1",
                "agent_id": "a1",
                "user_id": "u1",
                "session_id": "c1",
                "model": "claude-sonnet-5",
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_tokens": 10,
                "cache_creation_tokens": 5,
            },
            {
                "id": "r2",
                "agent_id": "a1",
                "user_id": "u1",
                "session_id": "c1",
                "model": "claude-haiku-4-5-20251001",
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
            },
        ]
    )
    ym = datetime.now(timezone.utc).strftime("%Y-%m")
    breakdown = repo.usage_breakdown_for_month("a1", ym)
    assert breakdown == {
        "input_tokens": 110,
        "output_tokens": 55,
        "cache_read_tokens": 10,
        "cache_creation_tokens": 5,
        # excludes cache_read_tokens — mirrors month_total_tokens.
        "total_tokens": 110 + 55 + 5,
    }
    assert breakdown["total_tokens"] == repo.month_total_tokens("a1", ym)


def test_usage_breakdown_for_month_no_rows_returns_zeros(repo):
    breakdown = repo.usage_breakdown_for_month("a-none", "2020-01")
    assert breakdown == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "total_tokens": 0,
    }


def test_usage_breakdown_for_month_out_of_range_period_excludes_rows(repo):
    repo.insert_batch(
        [
            {
                "id": "r1",
                "agent_id": "a1",
                "user_id": "u1",
                "session_id": "c1",
                "model": "claude-sonnet-5",
                "input_tokens": 100,
                "output_tokens": 100,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
            }
        ]
    )
    breakdown = repo.usage_breakdown_for_month("a1", "2019-01")
    assert breakdown["total_tokens"] == 0


def test_empty_batch_noop(repo):
    repo.insert_batch([])


def test_list_for_agent_limit(repo):
    repo.insert_batch(
        [
            {
                "id": f"r{i}",
                "agent_id": "a1",
                "user_id": "u1",
                "session_id": "c1",
                "model": "claude-sonnet-5",
                "input_tokens": 1,
                "output_tokens": 1,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
            }
            for i in range(3)
        ]
    )
    assert len(repo.list_for_agent("a1", limit=2)) == 2
    assert repo.list_for_agent("a-none") == []


def test_list_for_session_filters_by_session_id_exactly(repo):
    """Review carry-over (Task 9): `usage_for_session` used to scan only
    the agent's most recent `limit` rows via `list_for_agent` and filter by
    `session_id` in Python — `list_for_session` filters in SQL instead, so
    it stays exact regardless of how many OTHER rows the agent (or a
    different agent entirely) has accumulated."""
    repo.insert_batch(
        [
            {
                "id": "s1",
                "agent_id": "a1",
                "user_id": "u1",
                "session_id": "session-x",
                "model": "claude-sonnet-5",
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
            },
            {
                "id": "s2",
                "agent_id": "a1",
                "user_id": "u1",
                "session_id": "session-x",
                "model": "claude-sonnet-5",
                "input_tokens": 1,
                "output_tokens": 1,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
            },
            {
                # Same agent, DIFFERENT session — must not leak into the
                # "session-x" result.
                "id": "s3",
                "agent_id": "a1",
                "user_id": "u1",
                "session_id": "session-y",
                "model": "claude-sonnet-5",
                "input_tokens": 999,
                "output_tokens": 999,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
            },
        ]
    )
    rows = repo.list_for_session("session-x")
    assert len(rows) == 2
    assert {r["id"] for r in rows} == {"s1", "s2"}
    assert repo.list_for_session("session-none") == []


def test_list_for_session_limit(repo):
    repo.insert_batch(
        [
            {
                "id": f"r{i}",
                "agent_id": "a1",
                "user_id": "u1",
                "session_id": "session-limit",
                "model": "claude-sonnet-5",
                "input_tokens": 1,
                "output_tokens": 1,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
            }
            for i in range(3)
        ]
    )
    assert len(repo.list_for_session("session-limit", limit=2)) == 2


# ---------------------------------------------------------------------------
# C2.4 — caller_user_id (per-caller usage attribution). PG-only column
# under the A3 ratchet (migrations/versions/0074_llm_usage_caller_user_id.py)
# — mirrors agent_scope.granted_by's persists-on-PG / dropped-on-DuckDB
# pattern (tests/db_pg/test_agents_contract.py).
# ---------------------------------------------------------------------------


def test_insert_batch_accepts_caller_user_id_kwarg_on_both_backends(repo):
    """Every backend accepts `caller_user_id` in the row dict without
    raising — proof of call-site symmetry regardless of whether the
    backend actually persists it (see the persists/drops pair below)."""
    repo.insert_batch(
        [
            {
                "id": "r1",
                "agent_id": "a1",
                "user_id": "owner1",
                "caller_user_id": "caller1",
                "session_id": "c1",
                "model": "claude-sonnet-5",
                "input_tokens": 1,
                "output_tokens": 1,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
            }
        ]
    )
    assert len(repo.list_for_agent("a1")) == 1


def test_caller_user_id_persists_on_postgres(pg_engine, monkeypatch):
    repo, _ = _make_pg_repo(pg_engine, monkeypatch)
    repo.insert_batch(
        [
            {
                "id": "r1",
                "agent_id": "a1",
                "user_id": "owner1",
                "caller_user_id": "caller1",
                "session_id": "c1",
                "model": "claude-sonnet-5",
                "input_tokens": 1,
                "output_tokens": 1,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
            }
        ]
    )
    rows = repo.list_for_agent("a1")
    assert rows[0]["caller_user_id"] == "caller1"


def test_caller_user_id_is_dropped_on_duckdb(tmp_path):
    """DuckDB has no `caller_user_id` column: `insert_batch` accepts the
    row key (call-site symmetry with PG) but there is nothing to persist
    it into — the returned row simply has no such key."""
    repo, conn = _make_duckdb_repo(tmp_path)
    repo.insert_batch(
        [
            {
                "id": "r1",
                "agent_id": "a1",
                "user_id": "owner1",
                "caller_user_id": "caller1",
                "session_id": "c1",
                "model": "claude-sonnet-5",
                "input_tokens": 1,
                "output_tokens": 1,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
            }
        ]
    )
    rows = repo.list_for_agent("a1")
    assert "caller_user_id" not in rows[0]
    conn.close()


def test_usage_breakdown_by_caller_for_month_no_rows_returns_empty(repo):
    assert repo.usage_breakdown_by_caller_for_month("a-none", "2020-01") == []


def _insert_two_callers(repo, ym_ok: bool = True) -> None:
    repo.insert_batch(
        [
            {
                "id": "r1",
                "agent_id": "a1",
                "user_id": "owner1",
                "caller_user_id": "caller1",
                "session_id": "c1",
                "model": "claude-sonnet-5",
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_tokens": 1,
                "cache_creation_tokens": 0,
            },
            {
                "id": "r2",
                "agent_id": "a1",
                "user_id": "owner1",
                "caller_user_id": "caller2",
                "session_id": "c2",
                "model": "claude-sonnet-5",
                "input_tokens": 20,
                "output_tokens": 10,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 2,
            },
        ]
    )


def test_usage_breakdown_by_caller_distinguishes_callers_on_postgres(pg_engine, monkeypatch):
    """Two DIFFERENT callers running one shared agent produce
    DISTINGUISHABLE per-caller breakdown rows on Postgres."""
    from datetime import datetime, timezone

    repo, _ = _make_pg_repo(pg_engine, monkeypatch)
    _insert_two_callers(repo)
    ym = datetime.now(timezone.utc).strftime("%Y-%m")

    breakdown = repo.usage_breakdown_by_caller_for_month("a1", ym)
    by_caller = {r["caller_user_id"]: r for r in breakdown}
    assert set(by_caller) == {"caller1", "caller2"}
    assert by_caller["caller1"]["total_tokens"] == 10 + 5
    assert by_caller["caller2"]["total_tokens"] == 20 + 10 + 2

    # Sanity: the two per-caller totals sum to the SAME agent-level total
    # `usage_breakdown_for_month` reports — attribution splits the ledger,
    # it never changes the aggregate budget-governing quantity.
    agent_total = repo.usage_breakdown_for_month("a1", ym)["total_tokens"]
    assert sum(r["total_tokens"] for r in breakdown) == agent_total


def test_usage_breakdown_by_caller_duckdb_returns_single_unattributed_bucket(tmp_path):
    """DuckDB cannot distinguish callers it never recorded a column for —
    it reports one combined, honestly-unattributed (`caller_user_id=None`)
    bucket covering the agent's whole month total, matching
    `usage_breakdown_for_month`."""
    from datetime import datetime, timezone

    repo, conn = _make_duckdb_repo(tmp_path)
    _insert_two_callers(repo)
    ym = datetime.now(timezone.utc).strftime("%Y-%m")

    breakdown = repo.usage_breakdown_by_caller_for_month("a1", ym)
    assert len(breakdown) == 1
    assert breakdown[0]["caller_user_id"] is None
    assert breakdown[0]["total_tokens"] == repo.usage_breakdown_for_month("a1", ym)["total_tokens"]
    conn.close()


# ---------------------------------------------------------------------------
# Track E3 Slice 1: prune_older_than — opt-in llm_usage retention
# ---------------------------------------------------------------------------


def _backdate(repo, row_id: str, ts) -> None:
    """Rewrite one row's `created_at` directly — `insert_batch` always
    stamps `now()` via the column default, so the prune tests need an
    implementation-specific path to plant an old row (same reasoning as
    test_audit_contract.py's `_backdate` helper). Detects the backend off
    the repo object itself (DuckDB repos carry `.conn`, PG repos carry
    `._engine`) since this fixture yields only the repo, not a backend tag."""
    if hasattr(repo, "conn"):
        repo.conn.execute("UPDATE llm_usage SET created_at = ? WHERE id = ?", [ts, row_id])
    else:
        import sqlalchemy as sa

        with repo._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE llm_usage SET created_at = :ts WHERE id = :id"),
                {"ts": ts, "id": row_id},
            )


def _insert_one(repo, row_id: str, **overrides) -> None:
    row = {
        "id": row_id,
        "agent_id": "a1",
        "user_id": "u1",
        "session_id": "c1",
        "model": "claude-sonnet-5",
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }
    row.update(overrides)
    repo.insert_batch([row])


def test_prune_older_than_deletes_only_old_rows(repo):
    from datetime import timedelta

    _insert_one(repo, "old-row")
    _insert_one(repo, "new-row")
    _backdate(repo, "old-row", datetime.now(timezone.utc) - timedelta(days=400))

    pruned = repo.prune_older_than(365)

    assert pruned == 1
    remaining_ids = {r["id"] for r in repo.list_for_agent("a1", limit=10)}
    assert "old-row" not in remaining_ids
    assert "new-row" in remaining_ids


def test_prune_older_than_returns_zero_when_nothing_qualifies(repo):
    _insert_one(repo, "recent-1")
    _insert_one(repo, "recent-2")

    pruned = repo.prune_older_than(365)

    assert pruned == 0
    assert len(repo.list_for_agent("a1", limit=10)) == 2
