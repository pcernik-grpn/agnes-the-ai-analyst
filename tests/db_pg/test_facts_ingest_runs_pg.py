"""Postgres-only tests for the ``facts_ingest_runs`` repository.

There is no DuckDB half to parametrize against (PG-first ratchet, A3) — see
``docs/migrations.md`` -> "Adding a PG-only feature". Pattern follows
``tests/db_pg/test_corpus_file_sources_pg.py``'s PG construction helper.

The HTTP-level "a real ingest actually writes a row" round-trip lives in
``tests/db_pg/test_facts_ingest_pg.py`` (extends the existing happy-path
E2E) — this file only exercises the repository directly.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_repo(pg_engine, monkeypatch):
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

    from src.repositories.facts_ingest_runs_pg import FactsIngestRunsPgRepository

    return FactsIngestRunsPgRepository(db_pg.get_engine())


def _create(repo, **overrides):
    defaults = dict(
        corpus_ids=["col_a"],
        caller="scheduler@system.local",
        documents_seen=3,
        claims_written=2,
        claims_rejected=[{"row": 0, "reason": "verbatim_gate_failed", "doc_id": "d1"}],
        deferred=[{"row": 1, "doc_id": "d2"}],
        subjects_created=1,
        subjects_deleted=0,
        review_items=[],
    )
    defaults.update(overrides)
    return repo.create(**defaults)


def test_anonymization_defaults_to_empty_dict_not_null(pg_engine, monkeypatch):
    """A producer that never anonymizes omits the field entirely — the
    stored column must still round-trip as `{}`, never `None`, so every
    reader can treat it as always-present (spec §9.2)."""
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = _create(repo)
    row = repo.get(run_id)
    assert row["anonymization"] == {}


def test_anonymization_declaration_round_trips(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    anonymization = {
        "declared": True,
        "scopes": {"col_a": {"docs_anonymized": 5, "docs_skipped": 1}},
    }
    run_id = _create(repo, anonymization=anonymization)
    row = repo.get(run_id)
    assert row["anonymization"] == anonymization

    listed = repo.list_recent(limit=10)
    assert listed[0]["anonymization"] == anonymization


def test_create_returns_an_ir_prefixed_id(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = _create(repo)
    assert run_id.startswith("ir_")


def test_get_round_trips_every_field(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = _create(repo)
    row = repo.get(run_id)
    assert row is not None
    assert row["corpus_ids"] == ["col_a"]
    assert row["caller"] == "scheduler@system.local"
    assert row["documents_seen"] == 3
    assert row["claims_written"] == 2
    assert row["claims_rejected_count"] == 1
    assert row["claims_rejected"] == [{"row": 0, "reason": "verbatim_gate_failed", "doc_id": "d1"}]
    assert row["source_urls_rejected_count"] == 0
    assert row["source_urls_rejected"] == []
    assert row["deferred"] == [{"row": 1, "doc_id": "d2"}]
    assert row["subjects_created"] == 1
    assert row["subjects_deleted"] == 0
    assert row["review_items"] == []
    assert row["edges_skipped_missing_endpoint"] == 0
    assert row["created_at"] is not None


def test_get_returns_none_when_missing(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    assert repo.get("ir_nonexistent") is None


def test_source_urls_rejected_defaults_to_empty_list_not_null(pg_engine, monkeypatch):
    """A batch with no dropped source_url (the normal case) omits the field
    entirely — the stored column must still round-trip as `[]`, never
    `None`, same never-NULL contract as `claims_rejected`/`deferred`."""
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = _create(repo)
    row = repo.get(run_id)
    assert row["source_urls_rejected"] == []
    assert row["source_urls_rejected_count"] == 0


def test_source_urls_rejected_round_trips(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    rejected = [
        {"doc_id": "d1", "reason": "not_https"},
        {"doc_id": "d2", "reason": "too_long"},
    ]
    run_id = _create(repo, source_urls_rejected=rejected)
    row = repo.get(run_id)
    assert row["source_urls_rejected"] == rejected
    assert row["source_urls_rejected_count"] == 2

    listed = repo.list_recent(limit=10)
    assert listed[0]["source_urls_rejected"] == rejected


def test_edges_skipped_missing_endpoint_defaults_to_zero(pg_engine, monkeypatch):
    """A run whose edges all resolved cleanly (the normal case) omits the
    field entirely — the stored column must still round-trip as `0`, never
    `NULL`, same never-NULL contract as every other count on this table."""
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = _create(repo)
    row = repo.get(run_id)
    assert row["edges_skipped_missing_endpoint"] == 0


def test_edges_skipped_missing_endpoint_round_trips(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = _create(repo, edges_skipped_missing_endpoint=3)
    row = repo.get(run_id)
    assert row["edges_skipped_missing_endpoint"] == 3

    listed = repo.list_recent(limit=10)
    assert listed[0]["edges_skipped_missing_endpoint"] == 3


def test_claims_rejected_count_is_derived_from_the_detail_list(pg_engine, monkeypatch):
    """The stored count column is never trusted from the caller — it is
    always ``len(claims_rejected)``, so it can never drift from the detail
    it summarizes."""
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = _create(
        repo,
        claims_rejected=[
            {"row": 0, "reason": "verbatim_gate_failed"},
            {"row": 1, "reason": "unresolved_doc_id"},
            {"row": 2, "reason": "missing_node_id"},
        ],
    )
    row = repo.get(run_id)
    assert row["claims_rejected_count"] == 3


def test_list_recent_orders_newest_first(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    first = _create(repo, documents_seen=1)
    second = _create(repo, documents_seen=2)
    rows = repo.list_recent(limit=10)
    ids = [r["id"] for r in rows]
    assert ids.index(second) < ids.index(first)


def test_list_recent_respects_limit(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    for _ in range(5):
        _create(repo)
    assert len(repo.list_recent(limit=2)) == 2


def test_corpus_ids_are_deduplicated_and_sorted_on_write(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = _create(repo, corpus_ids=["col_b", "col_a", "col_b"])
    row = repo.get(run_id)
    assert row["corpus_ids"] == ["col_a", "col_b"]


# ---------------------------------------------------------------------------
# llm_usage — cost-visibility tally (unlike `anonymization`, genuinely
# NULLABLE: a run that never reported usage stays `None`, never `{}`).
# ---------------------------------------------------------------------------


def test_llm_usage_defaults_to_null_not_empty_dict(pg_engine, monkeypatch):
    """A producer build that doesn't send `llm_usage` at all — the stored
    column must round-trip as `None`, distinguishing "no figure available"
    from "reported zero usage"."""
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = _create(repo)
    row = repo.get(run_id)
    assert row["llm_usage"] is None


def test_llm_usage_round_trips(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    usage = {
        "input_tokens": 1200,
        "output_tokens": 340,
        "cache_read_input_tokens": 900,
        "cache_creation_input_tokens": 50,
        "models": ["claude-sonnet-4"],
        "documents": 3,
        "wall_seconds": 12.5,
    }
    run_id = _create(repo, llm_usage=usage)
    row = repo.get(run_id)
    assert row["llm_usage"] == usage

    listed = repo.list_recent(limit=10)
    assert listed[0]["llm_usage"] == usage


def test_llm_usage_rollup_is_empty_when_no_runs_report_usage(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    _create(repo)
    rollup = repo.llm_usage_rollup()
    assert rollup["runs_with_usage"] == 0
    assert rollup["input_tokens"] == 0
    assert rollup["estimated_cost_usd"] is None
    assert rollup["priced_runs"] == 0
    assert rollup["models"] == []


def test_llm_usage_rollup_sums_across_multiple_runs(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    _create(
        repo,
        llm_usage={
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 10,
            "models": ["claude-sonnet-4"],
            "documents": 2,
            "wall_seconds": 5.0,
        },
    )
    _create(
        repo,
        llm_usage={
            "input_tokens": 2000,
            "output_tokens": 300,
            "cache_read_input_tokens": 50,
            "cache_creation_input_tokens": 0,
            "models": ["claude-sonnet-4"],
            "documents": 4,
            "wall_seconds": 7.5,
        },
    )
    # A run with no usage reported at all must not perturb the rollup.
    _create(repo)

    rollup = repo.llm_usage_rollup()
    assert rollup["runs_with_usage"] == 2
    assert rollup["input_tokens"] == 3000
    assert rollup["output_tokens"] == 500
    assert rollup["cache_read_input_tokens"] == 150
    assert rollup["cache_creation_input_tokens"] == 10
    assert rollup["documents"] == 6
    assert rollup["wall_seconds"] == 12.5
    assert rollup["models"] == ["claude-sonnet-4"]
    # Both runs name exactly one known model, so both are priced.
    assert rollup["priced_runs"] == 2
    assert rollup["estimated_cost_usd"] is not None
    assert rollup["estimated_cost_usd"] > 0


def test_llm_usage_rollup_leaves_unpriceable_runs_out_of_the_cost_estimate():
    from src.repositories.facts_ingest_runs_pg import _price_run_usd

    # No model named at all — cannot honestly attribute a rate.
    assert _price_run_usd({"input_tokens": 100}) is None
    # More than one model — no per-model breakdown to split tokens by.
    assert _price_run_usd({"input_tokens": 100, "models": ["claude-sonnet-4", "gpt-4o"]}) is None
    # A model absent from the rate card.
    assert _price_run_usd({"input_tokens": 100, "models": ["some-unknown-model-9000"]}) is None


def test_llm_usage_rollup_mixed_priced_and_unpriced_runs_reports_partial_coverage(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    _create(repo, llm_usage={"input_tokens": 1000, "output_tokens": 100, "models": ["claude-sonnet-4"]})
    _create(repo, llm_usage={"input_tokens": 1000, "output_tokens": 100, "models": ["some-unknown-model-9000"]})

    rollup = repo.llm_usage_rollup()
    assert rollup["runs_with_usage"] == 2
    assert rollup["priced_runs"] == 1
    assert rollup["estimated_cost_usd"] is not None
    assert sorted(rollup["models"]) == ["claude-sonnet-4", "some-unknown-model-9000"]


# ---------------------------------------------------------------------------
# documents_done_since — the fleet throughput signal (TCRD-296 gap #67)
# ---------------------------------------------------------------------------


def _backdate(pg_engine, run_id: str, created_at) -> None:
    import sqlalchemy as sa

    with pg_engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE facts_ingest_runs SET created_at = :created_at WHERE id = :id"),
            {"created_at": created_at, "id": run_id},
        )


def test_documents_done_since_sums_only_overlapping_recent_runs(pg_engine, monkeypatch):
    from datetime import datetime, timedelta, timezone

    repo = _make_repo(pg_engine, monkeypatch)
    now = datetime.now(timezone.utc)

    recent_same_corpus = _create(repo, corpus_ids=["col_a"], documents_seen=5)
    recent_other_corpus = _create(repo, corpus_ids=["col_z"], documents_seen=100)
    stale_same_corpus = _create(repo, corpus_ids=["col_a"], documents_seen=50)
    _backdate(pg_engine, recent_same_corpus, now - timedelta(minutes=1))
    _backdate(pg_engine, recent_other_corpus, now - timedelta(minutes=1))
    _backdate(pg_engine, stale_same_corpus, now - timedelta(hours=2))

    total = repo.documents_done_since(["col_a", "col_b"], now - timedelta(minutes=10))
    assert total == 5  # only the recent, overlapping run counts


def test_documents_done_since_empty_corpus_ids_is_zero_without_a_query(pg_engine, monkeypatch):
    from datetime import datetime, timezone

    repo = _make_repo(pg_engine, monkeypatch)
    assert repo.documents_done_since([], datetime.now(timezone.utc)) == 0


def test_documents_done_since_no_matching_runs_is_zero(pg_engine, monkeypatch):
    from datetime import datetime, timedelta, timezone

    repo = _make_repo(pg_engine, monkeypatch)
    assert repo.documents_done_since(["col_never_seen"], datetime.now(timezone.utc) - timedelta(minutes=10)) == 0


# ---------------------------------------------------------------------------
# llm_usage_rollup_by_corpus_ids — the BATCHED, per-connection sibling of
# llm_usage_rollup(), attributed by corpus_ids overlap (fleet cost fix).
# ---------------------------------------------------------------------------


def test_llm_usage_rollup_by_corpus_ids_returns_a_full_entry_for_every_requested_key(pg_engine, monkeypatch):
    """A key with no matching run — including one with an empty corpus_ids
    list — still gets a zeroed-out entry, never a missing dict key, so a
    caller can index every connection it asked about without a membership
    check first."""
    repo = _make_repo(pg_engine, monkeypatch)
    out = repo.llm_usage_rollup_by_corpus_ids({"conn_a": ["col_a"], "conn_none": []})
    assert set(out) == {"conn_a", "conn_none"}
    for key in ("conn_a", "conn_none"):
        assert out[key]["runs_with_usage"] == 0
        assert out[key]["input_tokens"] == 0
        assert out[key]["estimated_cost_usd"] is None
        assert out[key]["models"] == []


def test_llm_usage_rollup_by_corpus_ids_attributes_by_overlap(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    _create(
        repo,
        corpus_ids=["col_a"],
        llm_usage={
            "input_tokens": 1000,
            "output_tokens": 200,
            "models": ["claude-haiku-4-5"],
            "documents": 5,
        },
    )
    _create(
        repo,
        corpus_ids=["col_b"],
        llm_usage={
            "input_tokens": 2000,
            "output_tokens": 400,
            "models": ["claude-haiku-4-5"],
            "documents": 8,
        },
    )

    out = repo.llm_usage_rollup_by_corpus_ids({"conn_a": ["col_a"], "conn_b": ["col_b"]})
    assert out["conn_a"]["runs_with_usage"] == 1
    assert out["conn_a"]["input_tokens"] == 1000
    assert out["conn_a"]["documents"] == 5
    assert out["conn_a"]["estimated_cost_usd"] is not None
    assert out["conn_a"]["estimated_cost_usd"] > 0
    assert out["conn_a"]["models"] == ["claude-haiku-4-5"]

    assert out["conn_b"]["runs_with_usage"] == 1
    assert out["conn_b"]["input_tokens"] == 2000
    # conn_a's own total must not have picked up conn_b's run.
    assert out["conn_a"]["input_tokens"] != out["conn_b"]["input_tokens"]


def test_llm_usage_rollup_by_corpus_ids_never_double_counts_a_run_touching_two_of_the_same_keys_collections(
    pg_engine, monkeypatch
):
    """A run whose corpus_ids overlaps TWO collections that both belong to
    the same connection must contribute its usage ONCE to that connection,
    not twice."""
    repo = _make_repo(pg_engine, monkeypatch)
    _create(
        repo,
        corpus_ids=["col_a", "col_a2"],
        llm_usage={"input_tokens": 1000, "output_tokens": 200, "models": ["claude-haiku-4-5"]},
    )

    out = repo.llm_usage_rollup_by_corpus_ids({"conn_a": ["col_a", "col_a2"]})
    assert out["conn_a"]["runs_with_usage"] == 1
    assert out["conn_a"]["input_tokens"] == 1000


def test_llm_usage_rollup_by_corpus_ids_a_run_overlapping_two_keys_counts_toward_both(pg_engine, monkeypatch):
    """Two connections sharing one collection each see the run that touched
    it — the same "did THIS caller's collections see this run" question
    documents_done_since already answers per-key, extended to every key."""
    repo = _make_repo(pg_engine, monkeypatch)
    _create(
        repo,
        corpus_ids=["col_shared"],
        llm_usage={"input_tokens": 1000, "output_tokens": 200, "models": ["claude-haiku-4-5"]},
    )

    out = repo.llm_usage_rollup_by_corpus_ids({"conn_a": ["col_shared"], "conn_b": ["col_shared"]})
    assert out["conn_a"]["input_tokens"] == 1000
    assert out["conn_b"]["input_tokens"] == 1000


def test_llm_usage_rollup_by_corpus_ids_ignores_runs_with_no_usage(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    _create(repo, corpus_ids=["col_a"])  # no llm_usage at all

    out = repo.llm_usage_rollup_by_corpus_ids({"conn_a": ["col_a"]})
    assert out["conn_a"]["runs_with_usage"] == 0
    assert out["conn_a"]["estimated_cost_usd"] is None


def test_llm_usage_rollup_by_corpus_ids_leaves_unpriceable_runs_out_of_the_cost_but_counts_tokens(
    pg_engine, monkeypatch
):
    """A run naming zero or more than one model cannot be honestly split by
    model, so it is left OUT of estimated_cost_usd while still counting
    toward the token/document totals — same disclosure contract as
    llm_usage_rollup()."""
    repo = _make_repo(pg_engine, monkeypatch)
    _create(repo, corpus_ids=["col_a"], llm_usage={"input_tokens": 1000, "output_tokens": 100})  # no model named
    _create(
        repo,
        corpus_ids=["col_a"],
        llm_usage={"input_tokens": 500, "output_tokens": 50, "models": ["m1", "m2"]},
    )

    out = repo.llm_usage_rollup_by_corpus_ids({"conn_a": ["col_a"]})
    assert out["conn_a"]["runs_with_usage"] == 2
    assert out["conn_a"]["input_tokens"] == 1500
    assert out["conn_a"]["priced_runs"] == 0
    assert out["conn_a"]["estimated_cost_usd"] is None


def test_llm_usage_rollup_by_corpus_ids_prices_via_src_llm_pricing_not_a_hand_rolled_rate_card(pg_engine, monkeypatch):
    """Unlike llm_usage_rollup()'s sampled rate card, the per-connection
    rollup prices through src.llm_pricing.cost_usd — the same model-aware,
    cache-aware table GET /api/admin/telemetry/chat-cost uses — so a cache
    read/write is priced at its real discount/premium rather than folded
    into the plain input rate."""
    from src.llm_pricing import cost_usd

    repo = _make_repo(pg_engine, monkeypatch)
    _create(
        repo,
        corpus_ids=["col_a"],
        llm_usage={
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 50,
            "models": ["claude-sonnet-4-6"],
        },
    )

    out = repo.llm_usage_rollup_by_corpus_ids({"conn_a": ["col_a"]})
    expected = cost_usd(
        model="claude-sonnet-4-6",
        input_tokens=1000,
        output_tokens=200,
        cache_read_tokens=900,
        cache_creation_tokens=50,
    )
    assert out["conn_a"]["estimated_cost_usd"] == round(expected, 4)


def test_llm_usage_rollup_by_corpus_ids_empty_map_short_circuits_without_a_query(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    assert repo.llm_usage_rollup_by_corpus_ids({}) == {}


# ---------------------------------------------------------------------------
# `runs` — per-run breakdown so a caller can de-duplicate a page-wide TOTAL
# across keys that share a collection (fleet cost double-count fix).
# ---------------------------------------------------------------------------


def test_llm_usage_rollup_by_corpus_ids_runs_list_names_each_contributing_run(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = _create(
        repo,
        corpus_ids=["col_a"],
        llm_usage={"input_tokens": 1000, "output_tokens": 200, "models": ["claude-haiku-4-5"]},
    )

    out = repo.llm_usage_rollup_by_corpus_ids({"conn_a": ["col_a"]})
    assert [r["id"] for r in out["conn_a"]["runs"]] == [run_id]
    assert out["conn_a"]["runs"][0]["estimated_cost_usd"] is not None
    assert out["conn_a"]["runs"][0]["estimated_cost_usd"] > 0


def test_llm_usage_rollup_by_corpus_ids_runs_list_carries_none_cost_for_an_unpriceable_run(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = _create(repo, corpus_ids=["col_a"], llm_usage={"input_tokens": 1000, "output_tokens": 100})

    out = repo.llm_usage_rollup_by_corpus_ids({"conn_a": ["col_a"]})
    assert out["conn_a"]["runs"] == [{"id": run_id, "estimated_cost_usd": None}]


def test_llm_usage_rollup_by_corpus_ids_a_run_shared_by_two_keys_appears_in_both_runs_lists_with_the_same_id(
    pg_engine, monkeypatch
):
    """The exact shape a de-duplicating caller relies on: a run attributed
    to two connections (a shared collection) shows up in BOTH keys' `runs`
    lists, carrying the SAME id and the SAME priced cost — so summing by
    unique id across keys counts it once, while each key's own aggregate
    still honestly reflects full attribution."""
    repo = _make_repo(pg_engine, monkeypatch)
    run_id = _create(
        repo,
        corpus_ids=["col_shared"],
        llm_usage={"input_tokens": 1000, "output_tokens": 200, "models": ["claude-haiku-4-5"]},
    )

    out = repo.llm_usage_rollup_by_corpus_ids({"conn_a": ["col_shared"], "conn_b": ["col_shared"]})
    assert [r["id"] for r in out["conn_a"]["runs"]] == [run_id]
    assert [r["id"] for r in out["conn_b"]["runs"]] == [run_id]
    assert out["conn_a"]["runs"][0]["estimated_cost_usd"] == out["conn_b"]["runs"][0]["estimated_cost_usd"]
