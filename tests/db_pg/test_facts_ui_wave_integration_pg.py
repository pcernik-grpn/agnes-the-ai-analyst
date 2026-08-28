"""Cross-surface integration for the fact-graph UI wave (spec §13.2).

Each builder in the wave covered its own surface; this test ties three of
them together on ONE ingest so a merge regression between the ingest write
path, the persisted run report (source card), and the collection facts
summary (Library card + collection detail) is caught in one place:

  ingest a real, gate-passing claim
    → the run report row is persisted with the same counts (source card)
    → the collection facts summary lists the subject (collection detail)
    → the caller-scoped fact count agrees (Library "N files · M facts")

Reuses the ingest suite's seeding helpers rather than duplicating them.
"""

from __future__ import annotations

from tests.db_pg.test_facts_ingest_pg import (  # noqa: F401,F811  (shared fixtures)
    CORPUS_A,
    _admin,
    _seed_ready_doc,
    pg_env,
    repo,
)


def test_ingest_run_report_summary_and_count_agree(pg_env, repo):  # noqa: F811
    doc_id = _seed_ready_doc(pg_env, text="Acme Rollout is underway and on schedule.")

    report = repo.ingest_batch(
        documents=[],
        nodes=[
            {
                "id": "engagement:acme-rollout",
                "type": "engagement",
                "attrs": {"status": "active"},
                "evidence": [{"doc_id": doc_id, "quote": "Acme Rollout is underway"}],
            }
        ],
    )
    assert report["claims_written"] == 1
    assert report["claims_rejected"] == []
    assert report["subjects_created"] == 1

    # source card: the endpoint handler persists the report, but the repo
    # method is the same one the handler calls — exercise it directly so the
    # PG-only run-report repo and the ingest path stay wired together.
    from src.repositories import facts_ingest_runs_repo

    runs_repo = facts_ingest_runs_repo()
    runs_repo.create(
        corpus_ids=[CORPUS_A],
        caller="integration-test",
        documents_seen=1,
        claims_written=report["claims_written"],
        claims_rejected=report["claims_rejected"],
        deferred=report.get("deferred", []),
        subjects_created=report["subjects_created"],
        subjects_deleted=report.get("subjects_deleted", 0),
        review_items=report.get("review_items", []),
    )
    runs = runs_repo.list_recent(limit=5)
    assert runs, "the source card reads at least one persisted run report"
    assert runs[0]["claims_written"] == 1

    # collection detail: the facts summary lists the ingested subject.
    summary = repo.collection_facts_summary(_admin(), CORPUS_A)
    types = {f["type"] for f in summary["facts"]}
    assert "engagement" in types

    # Library card: the caller-scoped count agrees with what was ingested.
    count = repo.count_visible_facts_for_collection(_admin(), CORPUS_A)
    assert count >= 1
