"""``llm_calls`` — the LLM observability ledger, on Postgres.

PG-side by necessity, not by preference: ``llm_calls`` is a Postgres-only
table (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend discipline"), so
Postgres is the only backend on which any of this can run at all. There is
no DuckDB sibling to parametrize against, matching
``tests/db_pg/test_semantic_feedback_pg.py``'s PG-only shape.

Design: ``docs/superpowers/specs/2026-09-08-llm-observability-design.md``
§3.7; plan: ``docs/superpowers/plans/2026-09-08-llm-observability.md`` Task 6.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.observability.llm_context import LlmCallContext
from src.observability.llm_record import build_record

REPO_ROOT = Path(__file__).resolve().parents[2]

_USAGE = {"input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 400, "cache_creation_tokens": 5}


def _row(**kw):
    ctx = LlmCallContext(**{k: kw.pop(k) for k in list(kw) if k in LlmCallContext.__dataclass_fields__})
    return build_record(
        kind=kw.pop("kind", "generation"),
        context=ctx,
        provider="anthropic",
        upstream="anthropic",
        model_requested=kw.pop("model", "claude-haiku-4-5"),
        model_response=None,
        usage=_USAGE,
        latency_ms=7,
        status="ok",
        **kw,
    ).to_row()


@pytest.fixture
def repo(pg_engine):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    from src.repositories.llm_calls_pg import LlmCallsPgRepository

    return LlmCallsPgRepository(pg_engine)


def test_insert_batch_is_idempotent_on_id(repo):
    row = _row(workload="chat", session_id="s1", turn_id="t1", user_id="u1")
    assert repo.insert_batch([row]) == 1
    assert repo.insert_batch([row]) == 0
    assert repo.count() == 1


def test_list_calls_filters_and_pages_newest_first(repo):
    now = datetime.now(UTC)
    rows = [
        _row(workload="chat", session_id="s1", turn_id="t1", created_at=now - timedelta(minutes=i)) for i in range(3)
    ]
    rows.append(_row(workload="extraction", job_id="j1", created_at=now))
    repo.insert_batch(rows)
    got = repo.list_calls(session_id="s1", limit=2)
    assert [r["turn_id"] for r in got] == ["t1", "t1"] and got[0]["created_at"] > got[1]["created_at"]
    older = repo.list_calls(session_id="s1", before=datetime.fromisoformat(got[-1]["created_at"]))
    assert len(older) == 1
    assert [r["job_id"] for r in repo.list_calls(job_id="j1")] == ["j1"]
    assert repo.list_calls(turn_id="t1")[0]["priced_as"]["price_key"] == "claude-haiku-4-5"


def test_cost_summary_groups_and_sums(repo):
    repo.insert_batch(
        [
            _row(workload="chat", user_id="u1", model="claude-haiku-4-5"),
            _row(workload="chat", user_id="u2", model="claude-sonnet-5"),
            _row(workload="builder", user_id="u1", purpose="entity_builder_turn"),
        ]
    )
    by_workload = {r["key"]: r for r in repo.cost_summary(since=None, by="workload")}
    assert by_workload["chat"]["calls"] == 2 and by_workload["builder"]["calls"] == 1
    assert by_workload["chat"]["cache_read_tokens"] == 800
    assert sorted(by_workload["chat"]["priced_models"]) == ["claude-haiku-4-5", "claude-sonnet-5"]
    assert isinstance(by_workload["chat"]["cost_usd"], float) and by_workload["chat"]["cost_usd"] > 0
    assert {r["key"] for r in repo.cost_summary(since=None, by="user")} == {"u1", "u2"}
    assert {r["key"] for r in repo.cost_summary(since=None, by="model")} == {"claude-haiku-4-5", "claude-sonnet-5"}
    assert {r["key"] for r in repo.cost_summary(since=None, by="purpose")} >= {"entity_builder_turn"}
    since = datetime.now(UTC) + timedelta(minutes=1)
    assert repo.cost_summary(since=since, by="workload") == []
    with pytest.raises(ValueError):
        repo.cost_summary(since=None, by="user_id; DROP TABLE llm_calls")


def test_prune_older_than(repo):
    old = datetime.now(UTC) - timedelta(days=40)
    repo.insert_batch([_row(created_at=old), _row()])
    assert repo.prune_older_than(30) == 1 and repo.count() == 1


class TestLedgerEndToEndOnPostgres:
    """Step 7 — the real sinks (``trace_generation`` and
    ``UsageAccumulator``) actually write through ``llm_calls_repo()`` when
    the active backend is this Postgres instance."""

    @pytest.fixture(autouse=True)
    def _point_at_pg(self, repo, pg_engine, monkeypatch):
        monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
        from src import db_pg

        db_pg.dispose()
        db_pg.get_engine()
        yield
        db_pg.dispose()

    def test_trace_generation_writes_a_real_row(self, repo):
        from src.observability.llm_tracing import trace_generation

        with trace_generation(provider="anthropic", model="claude-haiku-4-5", purpose="e2e") as cap:
            cap.set_tokens(5, 1)

        rows = repo.list_calls(limit=10)
        assert any(r["purpose"] == "e2e" for r in rows)

    def test_usage_accumulator_flush_writes_llm_calls_rows(self, repo):
        from app.api.broker_agent_policy import UsageAccumulator

        acc = UsageAccumulator()
        acc.add_call(_row(workload="chat", purpose="flush-e2e"))
        acc.flush()

        rows = repo.list_calls(limit=10)
        assert any(r["purpose"] == "flush-e2e" for r in rows)
