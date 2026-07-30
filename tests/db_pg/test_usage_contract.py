"""Cross-engine contract tests for the usage repository.

Targets: UsageRepository (DuckDB) / UsagePgRepository (Postgres).
Parametrises over [DuckDB impl, Postgres impl]; identical inputs must
produce identical outputs from both engines.

Follows the fixture pattern in test_rbac_contract.py: DuckDB uses
_ensure_schema; PG runs the alembic ladder to head. Seeding goes through
the repo's own write methods (upsert_summary / upsert_events).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa


# ---------------------------------------------------------------------------
# repo construction helpers — one per backend
# ---------------------------------------------------------------------------


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.usage import UsageRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return UsageRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    from pathlib import Path
    from alembic import command
    from alembic.config import Config

    REPO_ROOT = Path(__file__).resolve().parents[2]
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    engine = db_pg.get_engine()

    from src.repositories.usage_pg import UsagePgRepository

    return UsagePgRepository(engine), None


@pytest.fixture(params=["duckdb", "pg"])
def usage_repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields ``(repo, raw_conn_or_None, backend)`` for both backends."""
    backend = request.param
    if backend == "duckdb":
        repo, conn = _make_duckdb_repo(tmp_path)
        yield repo, conn, backend
        if conn is not None:
            conn.close()
    else:
        repo, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield repo, None, backend


# ---------------------------------------------------------------------------
# seeding helpers
# ---------------------------------------------------------------------------


def _seed_summary(
    repo,
    *,
    session_file,
    username,
    user_id=None,
    started_at,
    primary_model="claude-x",
    input_tokens=0,
    output_tokens=0,
    cache_read_tokens=0,
    cache_creation_tokens=0,
    tool_calls=0,
    tool_errors=0,
    user_messages=0,
):
    repo.upsert_summary(
        {
            "session_file": session_file,
            "session_id": session_file.rsplit("/", 1)[-1],
            "username": username,
            "user_id": user_id,
            "started_at": started_at,
            "ended_at": started_at,
            "active_seconds": 10,
            "wall_seconds": 20,
            "user_messages": user_messages,
            "tool_calls": tool_calls,
            "tool_errors": tool_errors,
            "primary_model": primary_model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_creation_tokens": cache_creation_tokens,
        },
        processor_version=1,
    )


def _seed_event(repo, *, event_id, username, session_file, occurred_at, tool_name="Read", user_id=None):
    repo.upsert_events(
        [
            {
                "id": event_id,
                "session_id": session_file.rsplit("/", 1)[-1],
                "session_file": session_file,
                "username": username,
                "event_type": "tool",
                "tool_name": tool_name,
                "is_error": False,
                "source": "curated",
                "occurred_at": occurred_at,
                "user_id": user_id,
            }
        ],
        processor_version=1,
    )


def _seed_processor_state(repo, processor_name, session_file="ps/s.jsonl"):
    """Seed a session_processor_state checkpoint via that repo's own
    mark_processed UPSERT (it knows the full schema, incl. NOT NULL columns).
    reset_all's clear_processors path deletes these rows, so the contract test
    needs a real row to delete."""
    if hasattr(repo, "conn"):  # DuckDB
        from src.repositories.session_processor_state import (
            SessionProcessorStateRepository,
        )

        sps = SessionProcessorStateRepository(repo.conn)
    else:  # Postgres
        from src.repositories.session_processor_state_pg import (
            SessionProcessorStatePgRepository,
        )

        sps = SessionProcessorStatePgRepository(repo._engine)
    sps.mark_processed(
        processor_name=processor_name,
        session_file=session_file,
        username="seed",
        items_count=0,
        file_hash="h",
    )


# ---------------------------------------------------------------------------
# usage_session_summary schema + upsert stability (index-corruption hotfix,
# 2026-07-20)
# ---------------------------------------------------------------------------


def test_usage_session_summary_has_no_secondary_indexes(usage_repo):
    """The 3 non-unique secondary indexes on usage_session_summary
    (idx_usage_session_user, idx_usage_session_started,
    idx_usage_session_user_id) were dropped — DuckDB v94 / the matching
    Alembic revision: a corrupt entry in one of them, rewritten by
    upsert_summary's ON CONFLICT DO UPDATE on every re-process tick, was
    invalidating the whole DuckDB connection. session_file (the PRIMARY
    KEY) remains the only index-backed constraint on either engine."""
    repo, conn, backend = usage_repo
    if backend == "duckdb":
        names = {
            r[0]
            for r in conn.execute(
                "SELECT index_name FROM duckdb_indexes WHERE table_name='usage_session_summary'"
            ).fetchall()
        }
    else:
        with repo._engine.connect() as c:
            names = {
                r[0]
                for r in c.execute(
                    sa.text("SELECT indexname FROM pg_indexes WHERE tablename='usage_session_summary'")
                ).fetchall()
            }
    for idx in ("idx_usage_session_user", "idx_usage_session_started", "idx_usage_session_user_id"):
        assert idx not in names, f"{idx} must have been dropped, found in {names}"


def test_upsert_summary_reprocessing_same_session_file_is_stable(usage_repo):
    """Re-processing the same session_file (the steady-state usage
    session-processor tick) must succeed repeatedly and leave the row
    correct — the scenario that used to force an ART index delete+insert
    on username/user_id/started_at every ~10 minutes."""
    repo, _, _ = usage_repo
    now = datetime.now(timezone.utc)
    _seed_summary(
        repo,
        session_file="reprocess/s1.jsonl",
        username="ivan",
        user_id="uid-ivan",
        started_at=now,
        tool_calls=1,
    )
    # Re-process repeatedly with growing counters — same session_file each time.
    for i in range(2, 5):
        _seed_summary(
            repo,
            session_file="reprocess/s1.jsonl",
            username="ivan",
            user_id="uid-ivan",
            started_at=now,
            tool_calls=i,
        )

    rows = repo.list_sessions_for_user_admin(user_id="uid-ivan", username="ivan")
    assert len(rows) == 1
    assert rows[0]["session_file"] == "reprocess/s1.jsonl"
    assert rows[0]["tool_calls"] == 4


def test_upsert_summary_backfills_late_resolved_identity(usage_repo):
    """A session first processed before its user account is resolvable is
    written with username=<dir_name>, user_id=NULL. When the file is later
    appended and re-processed after the account exists, the DO UPDATE must
    upgrade username/user_id to the resolved identity (late-resolution
    backfill). Removing those columns from the DO UPDATE SET would regress
    this (Devin review, PR #943), so it is locked in here on both engines."""
    repo, _, _ = usage_repo
    now = datetime.now(timezone.utc)
    # First pass: identity not yet resolvable.
    _seed_summary(
        repo,
        session_file="backfill/s1.jsonl",
        username="dir-uuid-1",
        user_id=None,
        started_at=now,
        tool_calls=1,
    )
    # Second pass (same session_file) once the account is resolved.
    _seed_summary(
        repo,
        session_file="backfill/s1.jsonl",
        username="resolved-user",
        user_id="uid-resolved",
        started_at=now,
        tool_calls=2,
    )
    # The row now carries the resolved identity...
    resolved = repo.list_sessions_for_user_admin(user_id="uid-resolved", username="resolved-user")
    assert len(resolved) == 1
    assert resolved[0]["session_file"] == "backfill/s1.jsonl"
    assert resolved[0]["tool_calls"] == 2
    # ...and no longer matches the pre-resolution placeholder identity.
    stale = repo.list_sessions_for_user_admin(user_id="uid-does-not-exist", username="dir-uuid-1")
    assert len(stale) == 0


def test_count_events(usage_repo):
    repo, _, _ = usage_repo
    now = datetime.now(timezone.utc)
    assert repo.count_events() == 0
    _seed_event(repo, event_id="e1", username="alice", session_file="alice/s1.jsonl", occurred_at=now)
    _seed_event(repo, event_id="e2", username="alice", session_file="alice/s1.jsonl", occurred_at=now)
    assert repo.count_events() == 2


def test_reset_all_zeroes_tables_and_returns_counts(usage_repo):
    repo, _, _ = usage_repo
    now = datetime.now(timezone.utc)
    _seed_summary(repo, session_file="bob/s1.jsonl", username="bob", started_at=now)
    _seed_summary(repo, session_file="bob/s2.jsonl", username="bob", started_at=now)
    _seed_event(repo, event_id="r1", username="bob", session_file="bob/s1.jsonl", occurred_at=now)

    counts = repo.reset_all()
    # All five usage tables are reported.
    assert set(counts.keys()) == {
        "events",
        "session_summary",
        "tool_daily",
        "marketplace_item_daily",
        "marketplace_item_window",
    }
    assert counts["events"] == 1
    assert counts["session_summary"] == 2
    assert counts["tool_daily"] == 0
    assert counts["marketplace_item_daily"] == 0
    assert counts["marketplace_item_window"] == 0

    # Everything is gone.
    assert repo.count_events() == 0
    assert repo.list_sessions_for_user_self("bob") == []


def test_reset_all_clear_processors_clears_state_and_usage_together(usage_repo):
    """clear_processors deletes the matching session_processor_state rows in the
    SAME transaction as the usage tables (the reprocess_usage atomicity fix),
    scoped to the named processors only."""
    repo, _, _ = usage_repo
    now = datetime.now(timezone.utc)
    _seed_event(repo, event_id="e1", username="bob", session_file="bob/s1.jsonl", occurred_at=now)
    _seed_processor_state(repo, "usage", "bob/s1.jsonl")
    _seed_processor_state(repo, "verification", "bob/s1.jsonl")  # must survive

    counts = repo.reset_all(clear_processors=["usage", "marketplace_rollup_30d"])
    # Only the 'usage' checkpoint matched (verification untouched, rollup absent).
    assert counts["state_rows"] == 1
    assert counts["events"] == 1
    assert repo.count_events() == 0


def test_reset_all_without_clear_processors_omits_state_rows(usage_repo):
    """Default reset_all (no clear_processors) keeps its original 5-key shape —
    no state_rows key — so existing callers/tests are unaffected."""
    repo, _, _ = usage_repo
    counts = repo.reset_all()
    assert "state_rows" not in counts


def test_list_sessions_for_user_admin_filters_on_user_id_or_username(usage_repo):
    repo, _, _ = usage_repo
    now = datetime.now(timezone.utc)
    # Matches via user_id.
    _seed_summary(repo, session_file="u/s1.jsonl", username="legacyname", user_id="uid-1", started_at=now)
    # Matches via username.
    _seed_summary(repo, session_file="u/s2.jsonl", username="carol", user_id="other", started_at=now)
    # No match.
    _seed_summary(repo, session_file="u/s3.jsonl", username="nobody", user_id="zzz", started_at=now)

    rows = repo.list_sessions_for_user_admin(user_id="uid-1", username="carol")
    files = {r["session_file"] for r in rows}
    assert files == {"u/s1.jsonl", "u/s2.jsonl"}
    # 9-column shape.
    assert set(rows[0].keys()) == {
        "session_file",
        "session_id",
        "started_at",
        "ended_at",
        "active_seconds",
        "wall_seconds",
        "tool_calls",
        "tool_errors",
        "primary_model",
    }


def test_list_sessions_for_user_self_filters_on_username_only(usage_repo):
    repo, _, _ = usage_repo
    now = datetime.now(timezone.utc)
    _seed_summary(repo, session_file="d/s1.jsonl", username="dave", user_id="uid-x", started_at=now)
    # Same user_id but different username — must NOT appear (self filters on username).
    _seed_summary(repo, session_file="d/s2.jsonl", username="someone-else", user_id="uid-x", started_at=now)

    rows = repo.list_sessions_for_user_self("dave")
    files = {r["session_file"] for r in rows}
    assert files == {"d/s1.jsonl"}
    # 14-column shape.
    assert set(rows[0].keys()) == {
        "session_file",
        "session_id",
        "started_at",
        "ended_at",
        "active_seconds",
        "wall_seconds",
        "user_messages",
        "tool_calls",
        "tool_errors",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "primary_model",
    }


def test_tokens_totals_and_by_model(usage_repo):
    repo, _, _ = usage_repo
    now = datetime.now(timezone.utc)
    _seed_summary(
        repo,
        session_file="t/s1.jsonl",
        username="erin",
        started_at=now,
        primary_model="claude-a",
        input_tokens=10,
        output_tokens=20,
        cache_read_tokens=3,
        cache_creation_tokens=2,
    )
    _seed_summary(
        repo,
        session_file="t/s2.jsonl",
        username="erin",
        started_at=now,
        primary_model="claude-b",
        input_tokens=100,
        output_tokens=200,
        cache_read_tokens=0,
        cache_creation_tokens=0,
    )

    totals = repo.tokens_totals("erin")
    assert totals["input"] == 110
    assert totals["output"] == 220
    assert totals["cache_read"] == 3
    assert totals["cache_creation"] == 2
    assert totals["total"] == 335
    assert totals["sessions"] == 2

    by_model = repo.tokens_by_model("erin")
    # Ordered by total desc → claude-b (300) first, claude-a (35) second.
    assert [m["model"] for m in by_model] == ["claude-b", "claude-a"]
    assert by_model[0]["total"] == 300
    assert by_model[1]["total"] == 35


def test_tokens_top_sessions_orders_by_total(usage_repo):
    repo, _, _ = usage_repo
    now = datetime.now(timezone.utc)
    _seed_summary(
        repo, session_file="ts/small.jsonl", username="frank", started_at=now, input_tokens=1, output_tokens=1
    )
    _seed_summary(
        repo, session_file="ts/big.jsonl", username="frank", started_at=now, input_tokens=1000, output_tokens=1000
    )

    top = repo.tokens_top_sessions("frank", limit=10)
    assert top[0]["session_file"] == "ts/big.jsonl"
    assert top[0]["total"] == 2000
    assert top[1]["session_file"] == "ts/small.jsonl"
    # limit is honored.
    assert len(repo.tokens_top_sessions("frank", limit=1)) == 1


def test_tokens_daily_series_window(usage_repo):
    repo, _, _ = usage_repo
    now = datetime.now(timezone.utc)
    _seed_summary(repo, session_file="ds/s1.jsonl", username="grace", started_at=now, input_tokens=5, output_tokens=5)

    series = repo.tokens_daily_series("grace", days=30)
    assert len(series) == 1
    assert series[0]["total"] == 10
    assert series[0]["sessions"] == 1


def test_tokens_daily_series_filters_username_and_days(usage_repo):
    """Pins the username + days predicates: an old same-user row (outside the
    window) and a current other-user row must both be excluded, so the series
    reflects only the requested user's in-window totals."""
    repo, _, _ = usage_repo
    now = datetime.now(timezone.utc)
    _seed_summary(
        repo,
        session_file="ds/current.jsonl",
        username="grace",
        started_at=now - timedelta(days=5),
        input_tokens=5,
        output_tokens=5,
    )
    _seed_summary(
        repo,
        session_file="ds/old.jsonl",
        username="grace",
        started_at=now - timedelta(days=45),
        input_tokens=500,
        output_tokens=500,
    )
    _seed_summary(
        repo,
        session_file="ds/other.jsonl",
        username="heidi",
        started_at=now - timedelta(days=5),
        input_tokens=50,
        output_tokens=50,
    )

    series = repo.tokens_daily_series("grace", days=30)
    assert len(series) == 1
    assert series[0]["total"] == 10
    assert series[0]["sessions"] == 1


def test_delete_older_than_present_on_both_backends(usage_repo):
    repo, _, _ = usage_repo
    # Sanity: method exists + is callable (parity reused by prune_usage).
    assert repo.delete_older_than(3650) == 0


def test_delete_older_than_removes_only_events_before_cutoff(usage_repo):
    """Pins the retention prune semantics behind POST /api/admin/telemetry/prune:
    rows older than the cutoff are deleted, recent rows survive, and the
    returned count is the number actually removed — on both backends (guards the
    dialect-specific interval arithmetic)."""
    repo, _, _ = usage_repo
    now = datetime.now(timezone.utc)
    _seed_event(
        repo, event_id="old", username="alice", session_file="retention/old.jsonl", occurred_at=now - timedelta(days=40)
    )
    _seed_event(
        repo,
        event_id="recent",
        username="alice",
        session_file="retention/recent.jsonl",
        occurred_at=now - timedelta(days=5),
    )

    assert repo.delete_older_than(30) == 1
    assert repo.count_events() == 1
    # Idempotent: nothing left older than the cutoff.
    assert repo.delete_older_than(30) == 0


# ---------------------------------------------------------------------------
# rebuild_rollups (#728 — dual-backend marketplace usage rollup producer)
# ---------------------------------------------------------------------------


def _insert(repo, conn, backend, table, cols, rows):
    """Backend-aware raw INSERT — mirrors test_reports_contract.py's ``_insert``.

    Used to seed the marketplace_plugins / store_entities lookup tables and
    full-shape usage_events rows (skill_name / subagent_type / command_name)
    that the plain ``_seed_event`` helper above doesn't carry.
    """
    collist = ", ".join(cols)
    if backend == "duckdb":
        ph = ", ".join(["?"] * len(cols))
        sql = f"INSERT INTO {table} ({collist}) VALUES ({ph})"
        for r in rows:
            conn.execute(sql, [r[c] for c in cols])
    else:
        ph = ", ".join(f":{c}" for c in cols)
        sql = f"INSERT INTO {table} ({collist}) VALUES ({ph})"
        with repo._engine.begin() as c:
            for r in rows:
                c.execute(sa.text(sql), {k: r[k] for k in cols})


def _seed_curated_plugin(repo, conn, backend, plugin_name, marketplace_id="mp"):
    _insert(
        repo,
        conn,
        backend,
        "marketplace_registry",
        ["id", "name", "url"],
        [{"id": marketplace_id, "name": marketplace_id.upper(), "url": "https://example.test/repo.git"}],
    )
    _insert(
        repo,
        conn,
        backend,
        "marketplace_plugins",
        ["marketplace_id", "name"],
        [{"marketplace_id": marketplace_id, "name": plugin_name}],
    )


def _seed_full_event(
    repo,
    conn,
    backend,
    *,
    event_id,
    occurred_at,
    skill_name=None,
    subagent_type=None,
    command_name=None,
    event_type="tool_use",
    tool_name=None,
    username="alice",
    user_id="uid-alice",
    session_id="s1",
    session_file="s1.jsonl",
):
    cols = [
        "id",
        "session_id",
        "session_file",
        "username",
        "user_id",
        "event_type",
        "tool_name",
        "skill_name",
        "subagent_type",
        "command_name",
        "is_error",
        "source",
        "occurred_at",
        "processor_version",
    ]
    _insert(
        repo,
        conn,
        backend,
        "usage_events",
        cols,
        [
            {
                "id": event_id,
                "session_id": session_id,
                "session_file": session_file,
                "username": username,
                "user_id": user_id,
                "event_type": event_type,
                "tool_name": tool_name,
                "skill_name": skill_name,
                "subagent_type": subagent_type,
                "command_name": command_name,
                "is_error": False,
                "source": "builtin",
                "occurred_at": occurred_at,
                "processor_version": 5,
            }
        ],
    )


def test_rebuild_rollups_daily_fact_identical_across_backends(usage_repo):
    """Same seed -> identical usage_marketplace_item_daily row on both engines."""
    repo, conn, backend = usage_repo
    _seed_curated_plugin(repo, conn, backend, "myplug")
    today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
    for i in range(3):
        _seed_full_event(
            repo,
            conn,
            backend,
            event_id=f"ep-{i}",
            occurred_at=today,
            tool_name="Skill",
            skill_name="myplug:design",
        )

    repo.rebuild_rollups(since_day=today.date())

    if backend == "duckdb":
        rows = conn.execute(
            "SELECT source, type, parent_plugin, name, count FROM usage_marketplace_item_daily "
            "WHERE type='skill' ORDER BY name"
        ).fetchall()
    else:
        with repo._engine.connect() as c:
            rows = c.execute(
                sa.text(
                    "SELECT source, type, parent_plugin, name, count FROM usage_marketplace_item_daily "
                    "WHERE type='skill' ORDER BY name"
                )
            ).fetchall()
    assert [tuple(r) for r in rows] == [("curated", "skill", "myplug", "design", 3)]


def test_rebuild_rollups_stale_entity_removed_identically_across_backends(usage_repo):
    """Devin Review finding on PR #909 (BUG_...0001 / BUG_...0002): the ON
    CONFLICT DO UPDATE rewrite on the DuckDB side must still remove a
    marketplace item's daily-fact and sliding-window rows once its source
    event is gone (entity unapproved, event corrected/deleted) — the same
    behavior the Postgres sibling gets for free from its DELETE-then-INSERT.
    Seeds two distinct curated plugins (not a builtin tool — see
    test_builtin_excluded in test_usage_rollups.py — which never enters
    these tables at all and would make the "stale" side a no-op), rebuilds,
    purges one plugin's only event, rebuilds again, and asserts the stale
    plugin's rows are gone from BOTH tables on BOTH backends (not just that
    the crash doesn't recur — see test_usage_rollups.py for that guarantee
    on the DuckDB side alone)."""
    repo, conn, backend = usage_repo
    _seed_curated_plugin(repo, conn, backend, "myplug")
    _seed_curated_plugin(repo, conn, backend, "otherplug", marketplace_id="mp2")
    today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
    _seed_full_event(
        repo,
        conn,
        backend,
        event_id="ep-keep",
        occurred_at=today,
        tool_name="Skill",
        skill_name="myplug:design",
        session_file="keep.jsonl",
    )
    _seed_full_event(
        repo,
        conn,
        backend,
        event_id="ep-stale",
        occurred_at=today,
        tool_name="Skill",
        skill_name="otherplug:gone",
        session_file="stale.jsonl",
    )
    repo.rebuild_rollups(since_day=today.date())

    def _names(table, extra_where=""):
        sql = f"SELECT name FROM {table} WHERE 1=1 {extra_where}"
        if backend == "duckdb":
            return {r[0] for r in conn.execute(sql).fetchall()}
        with repo._engine.connect() as c:
            return {r[0] for r in c.execute(sa.text(sql)).fetchall()}

    assert _names("usage_marketplace_item_daily") == {"design", "myplug", "gone", "otherplug"}
    assert _names("usage_marketplace_item_window", "AND period_label='last_7d'") == {
        "design",
        "myplug",
        "gone",
        "otherplug",
    }

    repo.purge_for_session("stale.jsonl")
    repo.rebuild_rollups(since_day=today.date())

    daily_names = _names("usage_marketplace_item_daily")
    window_names = _names("usage_marketplace_item_window", "AND period_label='last_7d'")
    assert daily_names == {"design", "myplug"}, f"stale entity must be gone from daily fact table, got {daily_names}"
    assert window_names == {"design", "myplug"}, f"stale entity must be gone from window table, got {window_names}"


def test_rebuild_rollups_since_day_none_is_full_rebuild(usage_repo):
    """since_day=None must cover ALL history, not just the last 7 days — the
    #728 semantics fix (docstring already promised this; code defaulted to
    today-7). Seeds a 20-day-old event and asserts it lands in the daily
    fact table when since_day is omitted."""
    repo, conn, backend = usage_repo
    _seed_curated_plugin(repo, conn, backend, "myplug")
    old_day = datetime.now(timezone.utc) - timedelta(days=20)
    _seed_full_event(
        repo,
        conn,
        backend,
        event_id="old-1",
        occurred_at=old_day,
        tool_name="Skill",
        skill_name="myplug:design",
    )

    repo.rebuild_rollups()  # since_day=None -> full rebuild

    if backend == "duckdb":
        n = conn.execute(
            "SELECT COUNT(*) FROM usage_marketplace_item_daily WHERE type='skill' AND name='design'"
        ).fetchone()[0]
    else:
        with repo._engine.connect() as c:
            n = c.execute(
                sa.text("SELECT COUNT(*) FROM usage_marketplace_item_daily WHERE type='skill' AND name='design'")
            ).scalar()
    assert n == 1


def test_rebuild_rollups_explicit_cutoff_is_incremental(usage_repo):
    """An explicit since_day only rebuilds days >= cutoff — the steady-state
    scheduler-tick behaviour. A 20-day-old event must NOT appear when the
    cutoff is 'today'."""
    repo, conn, backend = usage_repo
    _seed_curated_plugin(repo, conn, backend, "myplug")
    today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
    old_day = today - timedelta(days=20)
    _seed_full_event(
        repo,
        conn,
        backend,
        event_id="old-2",
        occurred_at=old_day,
        tool_name="Skill",
        skill_name="myplug:design",
    )

    repo.rebuild_rollups(since_day=today.date())

    if backend == "duckdb":
        n = conn.execute("SELECT COUNT(*) FROM usage_marketplace_item_daily").fetchone()[0]
    else:
        with repo._engine.connect() as c:
            n = c.execute(sa.text("SELECT COUNT(*) FROM usage_marketplace_item_daily")).scalar()
    assert n == 0


def test_rebuild_rollups_force_30d_populates_window(usage_repo):
    repo, conn, backend = usage_repo
    _seed_curated_plugin(repo, conn, backend, "myplug")
    today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
    _seed_full_event(
        repo,
        conn,
        backend,
        event_id="e1",
        occurred_at=today,
        tool_name="Skill",
        skill_name="myplug:design",
    )

    repo.rebuild_rollups(since_day=today.date(), force_30d=True)

    if backend == "duckdb":
        n = conn.execute("SELECT COUNT(*) FROM usage_marketplace_item_window WHERE period_label='last_30d'").fetchone()[
            0
        ]
    else:
        with repo._engine.connect() as c:
            n = c.execute(
                sa.text("SELECT COUNT(*) FROM usage_marketplace_item_window WHERE period_label='last_30d'")
            ).scalar()
    assert n >= 1


def _tracker_ts(repo, conn, backend):
    sql = "SELECT processed_at FROM session_processor_state WHERE processor_name='marketplace_rollup_30d'"
    if backend == "duckdb":
        row = conn.execute(sql).fetchone()
        return row[0] if row else None
    with repo._engine.connect() as c:
        return c.execute(sa.text(sql)).scalar()


def test_rebuild_rollups_30d_throttled_until_force(usage_repo):
    """The hourly 30d-window throttle behaves identically on both backends:
    a second rebuild within the window leaves the tracker untouched;
    force_30d=True advances it."""
    repo, conn, backend = usage_repo
    _seed_curated_plugin(repo, conn, backend, "myplug")
    today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
    _seed_full_event(
        repo,
        conn,
        backend,
        event_id="thr-1",
        occurred_at=today,
        tool_name="Skill",
        skill_name="myplug:design",
    )

    repo.rebuild_rollups(since_day=today.date())
    t1 = _tracker_ts(repo, conn, backend)
    assert t1 is not None

    _seed_full_event(
        repo,
        conn,
        backend,
        event_id="thr-2",
        occurred_at=today,
        tool_name="Skill",
        skill_name="myplug:design",
    )
    repo.rebuild_rollups(since_day=today.date())
    assert _tracker_ts(repo, conn, backend) == t1, "tracker must not advance within the throttle window"

    repo.rebuild_rollups(since_day=today.date(), force_30d=True)
    assert _tracker_ts(repo, conn, backend) > t1


# ---------------------------------------------------------------------------
# home_stats — /home status frame counter (cross-engine contract)
# ---------------------------------------------------------------------------


def test_home_stats_identical_across_backends(usage_repo):
    """home_stats must return the same sessions/prompts/tokens/projects
    dict on DuckDB and Postgres."""
    repo, _, _ = usage_repo
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    after = datetime(2026, 6, 1, tzinfo=timezone.utc)
    before = datetime(2025, 12, 1, tzinfo=timezone.utc)  # outside window

    _seed_summary(
        repo,
        session_file="alice/s1.jsonl",
        username="alice",
        user_id="uid-alice",
        started_at=after,
        user_messages=3,
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=20,
        cache_creation_tokens=10,
    )
    _seed_summary(
        repo,
        session_file="alice/s2.jsonl",
        username="alice",
        user_id="uid-alice",
        started_at=after,
        user_messages=7,
        input_tokens=200,
        output_tokens=80,
    )
    # Row outside the window — must NOT count.
    _seed_summary(
        repo,
        session_file="alice/s_old.jsonl",
        username="alice",
        user_id="uid-alice",
        started_at=before,
        user_messages=99,
        input_tokens=9999,
        output_tokens=9999,
    )
    # Row for a different user — must NOT count.
    _seed_summary(
        repo,
        session_file="bob/s1.jsonl",
        username="bob",
        user_id="uid-bob",
        started_at=after,
        user_messages=5,
        input_tokens=500,
    )

    # Two distinct cwd values → projects = 2.
    repo.upsert_events(
        [
            {
                "id": "ev-1",
                "session_id": "s1",
                "session_file": "alice/s1.jsonl",
                "username": "alice",
                "user_id": "uid-alice",
                "event_type": "tool",
                "tool_name": "Read",
                "is_error": False,
                "source": "curated",
                "occurred_at": after,
                "cwd": "/projects/alpha",
            },
            {
                "id": "ev-2",
                "session_id": "s2",
                "session_file": "alice/s2.jsonl",
                "username": "alice",
                "user_id": "uid-alice",
                "event_type": "tool",
                "tool_name": "Read",
                "is_error": False,
                "source": "curated",
                "occurred_at": after,
                "cwd": "/projects/beta",
            },
            # Event with cwd=None must NOT count toward projects.
            {
                "id": "ev-3",
                "session_id": "s1",
                "session_file": "alice/s1.jsonl",
                "username": "alice",
                "user_id": "uid-alice",
                "event_type": "tool",
                "tool_name": "Write",
                "is_error": False,
                "source": "curated",
                "occurred_at": after,
                "cwd": None,
            },
        ],
        processor_version=1,
    )

    result = repo.home_stats(user_id="uid-alice", username="alice", since=since)

    assert result["sessions"] == 2
    assert result["prompts"] == 10  # 3 + 7
    assert result["input_tokens"] == 300  # 100 + 200
    assert result["output_tokens"] == 130  # 50 + 80
    assert result["cache_read"] == 20
    assert result["cache_creation"] == 10
    assert result["projects"] == 2


# ---------------------------------------------------------------------------
# admin telemetry export + text-to-SQL execution
# (backend-routing fix: /api/admin/telemetry/{export,ask} read through the
# factory instead of the always-DuckDB request connection)
# ---------------------------------------------------------------------------


def test_export_events_and_count_identical_across_backends(usage_repo):
    repo, _, _ = usage_repo
    base = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    for i in range(3):
        _seed_event(
            repo,
            event_id=f"exp-{i}",
            username="alice" if i < 2 else "bob",
            session_file="alice/a.jsonl",
            occurred_at=base + timedelta(days=i),
        )

    # unfiltered: full width, all rows, ascending occurred_at
    cols, rows = repo.export_events({})
    assert "id" in cols and "occurred_at" in cols and "username" in cols
    assert len(rows) == 3
    ids = [r[cols.index("id")] for r in rows]
    assert ids == ["exp-0", "exp-1", "exp-2"]
    assert repo.count_events_export({}) == 3

    # since / until half-open window
    assert repo.count_events_export({"since": base + timedelta(days=1)}) == 2
    window = {"since": base, "until": base + timedelta(days=1)}
    _, w_rows = repo.export_events(window)
    assert len(w_rows) == 1
    assert repo.count_events_export(window) == 1

    # username + source filters
    assert repo.count_events_export({"username": "bob"}) == 1
    assert repo.count_events_export({"source": "curated"}) == 3
    assert repo.count_events_export({"source": "builtin"}) == 0


def test_execute_readonly_select_identical_across_backends(usage_repo):
    repo, _, _ = usage_repo
    base = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    for i in range(2):
        _seed_event(
            repo,
            event_id=f"sel-{i}",
            username="alice",
            session_file="alice/a.jsonl",
            occurred_at=base + timedelta(hours=i),
        )

    cols, rows = repo.execute_readonly_select(
        "SELECT username, COUNT(*) AS n FROM usage_events GROUP BY username ORDER BY username"
    )
    assert cols == ["username", "n"]
    assert [(r[0], int(r[1])) for r in rows] == [("alice", 2)]


# ---------------------------------------------------------------------------
# UTC day bucketing (PG-only) — day buckets must be UTC days regardless of the
# Postgres session TimeZone. DuckDB gets this from the pinned UTC session in
# src/duckdb_conn.py; on PG every day-grain CAST must pin AT TIME ZONE 'UTC'
# (see the reports_pg module docstring for the invariant).
# ---------------------------------------------------------------------------


@pytest.fixture
def pg_repo_skewed_tz(pg_engine, monkeypatch):
    """PG repo whose pooled connections run in a session TimeZone whose local
    date is guaranteed to differ from the UTC date right now."""
    repo, _ = _make_pg_repo(pg_engine, monkeypatch)
    # UTC hour >= 12 -> UTC+14 is already tomorrow; else UTC-12 is yesterday.
    # (POSIX Etc/GMT signs are inverted: Etc/GMT-14 == UTC+14.)
    tz = "Etc/GMT-14" if datetime.now(timezone.utc).hour >= 12 else "Etc/GMT+12"

    def _set_tz(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute(f"SET TIME ZONE '{tz}'")
        cur.close()
        # PG's SET is transactional — commit it, or the pool's
        # reset-on-return rollback reverts the GUC on check-in.
        dbapi_conn.commit()

    sa.event.listen(repo._engine, "connect", _set_tz)
    repo._engine.dispose()  # drop pre-listener pooled connections
    yield repo
    sa.event.remove(repo._engine, "connect", _set_tz)
    repo._engine.dispose()


def test_pg_day_bucketing_is_utc_regardless_of_session_timezone(pg_repo_skewed_tz):
    repo = pg_repo_skewed_tz
    utc_now = datetime.now(timezone.utc)
    utc_day = utc_now.date()

    # Sanity: the session clock really is on the other side of midnight.
    with repo._engine.connect() as c:
        local_today = c.execute(sa.text("SELECT CURRENT_DATE")).scalar_one()
    assert local_today != utc_day

    _seed_curated_plugin(repo, None, "pg", "myplug")
    _seed_full_event(
        repo,
        None,
        "pg",
        event_id="tz-1",
        occurred_at=utc_now,
        tool_name="Skill",
        skill_name="myplug:design",
    )

    # Producer: the persisted daily fact must carry the UTC day label
    # (two rows: the skill leaf + the plugin-level rollup, same day).
    repo.rebuild_rollups(since_day=utc_day - timedelta(days=1))
    with repo._engine.connect() as c:
        days = [r[0] for r in c.execute(sa.text("SELECT day FROM usage_marketplace_item_daily")).fetchall()]
    assert days and set(days) == {utc_day}

    # Read path: day-grain aggregates bucket on UTC days too.
    dau = repo.summary_dau(utc_day - timedelta(days=1))
    assert dau.get(utc_day) == 1

    # started_at path (usage_session_summary): same UTC pin.
    _seed_summary(
        repo,
        session_file="alice/tz.jsonl",
        username="alice",
        started_at=utc_now,
        input_tokens=7,
    )
    tokens = repo.tokens_daily_series("alice", days=3)
    assert [r["day"] for r in tokens] == [utc_day.isoformat()]

    # audit_log.timestamp path (query-telemetry frequency): same UTC pin.
    with repo._engine.begin() as c:
        c.execute(
            sa.text(
                "INSERT INTO audit_log (id, timestamp, user_id, action, resource) "
                "VALUES ('tz-a1', :ts, 'uid', 'query.remote', 'table:foo')"
            ),
            {"ts": utc_now},
        )
    telemetry = repo.summary_query_telemetry(cutoff=utc_now - timedelta(days=1))
    assert [f["day"] for f in telemetry["frequency"]] == [utc_day.isoformat()]


def test_upsert_summary_uploaded_at_first_arrival_wins(usage_repo):
    """PR-C contract: uploaded_at stamps at first ingest; re-processing the
    same session keeps the original arrival stamp (both backends)."""
    from datetime import datetime, timezone

    repo, _, _ = usage_repo
    base = {
        "session_file": "u-9/arr.jsonl",
        "session_id": "arr",
        "username": "arr@example.com",
        "user_id": "u-9",
        "started_at": datetime.now(timezone.utc),
    }
    repo.upsert_summary(dict(base), processor_version=1)
    rows = repo.sessions_list(
        {"since": datetime(2000, 1, 1, tzinfo=timezone.utc), "anchor": "uploaded"},
        sort_col="uploaded_at", direction="desc", limit=10, offset=0,
    )
    first = next(r for r in rows if r["session_id"] == "arr")["uploaded_at"]
    assert first is not None
    repo.upsert_summary(dict(base), processor_version=2)
    rows = repo.sessions_list(
        {"since": datetime(2000, 1, 1, tzinfo=timezone.utc), "anchor": "uploaded"},
        sort_col="uploaded_at", direction="desc", limit=10, offset=0,
    )
    second = next(r for r in rows if r["session_id"] == "arr")["uploaded_at"]
    assert second == first


def test_session_file_basenames_since_windows_on_arrival(usage_repo):
    """PR-C: the reconciliation target — basenames of ingested summaries,
    windowed on COALESCE(uploaded_at, started_at). Both backends."""
    from datetime import datetime, timedelta, timezone

    repo, _, _ = usage_repo
    now = datetime.now(timezone.utc)
    for sid, uploaded in (("new1", now), ("new2", now)):
        repo.upsert_summary(
            {
                "session_file": f"u-7/{sid}.jsonl",
                "session_id": sid,
                "username": "rec@example.com",
                "user_id": "u-7",
                "started_at": now - timedelta(days=90),
                "uploaded_at": uploaded,
            },
            processor_version=1,
        )
    repo.upsert_summary(
        {
            "session_file": "u-7/old.jsonl",
            "session_id": "old",
            "username": "rec@example.com",
            "user_id": "u-7",
            "started_at": now - timedelta(days=90),
            "uploaded_at": now - timedelta(days=90),
        },
        processor_version=1,
    )
    got = repo.session_file_basenames_since(now - timedelta(days=1))
    assert got == {"new1.jsonl", "new2.jsonl"}


# ---------------------------------------------------------------------------
# query telemetry: registered-table join + success/failure split
# ---------------------------------------------------------------------------


def _seed_query_audit(repo, backend, conn, rows):
    """Insert raw audit_log query rows through whichever engine is under test."""
    stmt = (
        "INSERT INTO audit_log (id, timestamp, user_id, action, resource, result) "
        "VALUES (:id, :ts, 'uid', :action, :resource, :result)"
    )
    if backend == "duckdb":
        for r in rows:
            conn.execute(
                stmt.replace(":id", "?").replace(":ts", "?").replace(":action", "?")
                    .replace(":resource", "?").replace(":result", "?"),
                [r["id"], r["ts"], r["action"], r["resource"], r["result"]],
            )
    else:
        with repo._engine.begin() as c:
            for r in rows:
                c.execute(sa.text(stmt), r)


def _seed_registered_table(repo, backend, conn, table_id):
    stmt = (
        "INSERT INTO table_registry (id, name, source_type, query_mode) "
        "VALUES (:id, :name, 'bigquery', 'remote')"
    )
    if backend == "duckdb":
        conn.execute(
            stmt.replace(":id", "?").replace(":name", "?"), [table_id, table_id]
        )
    else:
        with repo._engine.begin() as c:
            c.execute(sa.text(stmt), {"id": table_id, "name": table_id})


def test_query_telemetry_flags_unregistered_table_names(usage_repo):
    """A parsed name absent from table_registry must be marked, not silently
    presented as a real table. Both backends."""
    from datetime import datetime, timedelta, timezone

    repo, conn, backend = usage_repo
    now = datetime.now(timezone.utc)
    _seed_registered_table(repo, backend, conn, "orders")
    _seed_query_audit(repo, backend, conn, [
        {"id": "q1", "ts": now, "action": "query.remote",
         "resource": "table:orders", "result": "success"},
        {"id": "q2", "ts": now, "action": "query.remote",
         "resource": "table:not_a_table", "result": "success"},
    ])

    out = repo.summary_query_telemetry(cutoff=now - timedelta(days=1))
    by_id = {r["table_id"]: r for r in out["top_tables"]}

    assert by_id["orders"]["registered"] is True
    assert by_id["not_a_table"]["registered"] is False


def test_query_telemetry_counts_failures_per_table(usage_repo):
    """`failed` must be reported alongside `queries` so a name that never
    succeeded is distinguishable from real usage. Both backends."""
    from datetime import datetime, timedelta, timezone

    repo, conn, backend = usage_repo
    now = datetime.now(timezone.utc)
    _seed_query_audit(repo, backend, conn, [
        {"id": "f1", "ts": now, "action": "query.local",
         "resource": "table:ghost", "result": "error.400"},
        {"id": "f2", "ts": now, "action": "query.local",
         "resource": "table:ghost", "result": "error.400"},
        {"id": "s1", "ts": now, "action": "query.local",
         "resource": "table:real_one", "result": "success"},
    ])

    out = repo.summary_query_telemetry(cutoff=now - timedelta(days=1))
    by_id = {r["table_id"]: r for r in out["top_tables"]}

    assert by_id["ghost"]["queries"] == 2
    assert by_id["ghost"]["failed"] == 2
    assert by_id["real_one"]["queries"] == 1
    assert by_id["real_one"]["failed"] == 0


def test_query_telemetry_ranks_by_successful_queries(usage_repo):
    """A name that failed every time must not outrank a table with genuine
    usage, even when its raw attempt count is higher. Both backends.

    The motivating shape: a scheduled job retrying a deleted local snapshot
    every few minutes put a table that does not exist at the top of the
    ranking, above every table anyone actually queried.
    """
    from datetime import datetime, timedelta, timezone

    repo, conn, backend = usage_repo
    now = datetime.now(timezone.utc)
    rows = [
        {"id": f"g{i}", "ts": now, "action": "query.local",
         "resource": "table:ghost", "result": "error.400"}
        for i in range(3)
    ]
    rows.append({"id": "r1", "ts": now, "action": "query.local",
                 "resource": "table:real_one", "result": "success"})
    _seed_query_audit(repo, backend, conn, rows)

    out = repo.summary_query_telemetry(cutoff=now - timedelta(days=1))
    ranked = [r["table_id"] for r in out["top_tables"]]

    assert ranked.index("real_one") < ranked.index("ghost")
