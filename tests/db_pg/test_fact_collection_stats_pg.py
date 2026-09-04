"""PG contract tests for the fact-graph collection-stats summary (TCRD-296
synthesis E.21) — see ``src/repositories/facts_pg.py``'s "Collection stats
summary" section.

Covers: incremental maintenance on the hot ingest path (``add_claim``),
scoped reconciliation on the bulk delete/reassign/merge/split/consolidation
paths, ``rebuild_collection_stats`` idempotence, reader parity between the
fallback (summary empty -> original ``claims`` scan) and fast (summary
populated) paths, and a perf-shaped assertion that the fast path is an
index lookup, not a sequential scan over ``claims``.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_A = "col_a"
CORPUS_B = "col_b"


# ---------------------------------------------------------------------------
# fixtures / seeding helpers (mirrors tests/db_pg/test_facts_read_pg.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def pg_env(tmp_path, monkeypatch, pg_engine):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from tests.db_pg._parity_sweep_util import _seed_pg_system_groups

    _seed_pg_system_groups(pg_engine)
    return pg_engine


@pytest.fixture
def repo(pg_env):
    import src.db_pg as db_pg
    from src.repositories.facts_pg import FactsPgRepository

    return FactsPgRepository(db_pg.get_engine())


def _dict_user(user_id: str) -> dict:
    return {"id": user_id, "email": f"{user_id}@test.com"}


def _seed_uploader(user_id: str) -> None:
    from src.repositories import users_repo

    users_repo().create(id=user_id, email=f"{user_id}@test.com", name=user_id)


def _seed_collection(*, collection_id: str, created_by: str) -> str:
    from src.db_pg import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            sa.text("INSERT INTO file_corpora (id, slug, name, created_by) VALUES (:id, :slug, :name, :by)"),
            {"id": collection_id, "slug": collection_id, "name": collection_id, "by": created_by},
        )
    return collection_id


def _seed_corpus_file(*, corpus_id: str, file_id: str, sha256: str = "sha1") -> None:
    from src.db_pg import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO corpus_files (id, corpus_id, filename, sha256) "
                "VALUES (:id, :corpus_id, :filename, :sha256)"
            ),
            {"id": file_id, "corpus_id": corpus_id, "filename": f"{file_id}.md", "sha256": sha256},
        )


def _make_group_with_grant(pg_engine, *, group_name: str, collection_id: str, member_user_id: str) -> None:
    from src.repositories import resource_grants_repo, user_group_members_repo, user_groups_repo

    grp = user_groups_repo().create(name=group_name, description="test", created_by="test-fixture")
    user_group_members_repo().add_member(member_user_id, grp["id"], source="admin", added_by="test-fixture")
    resource_grants_repo().create(grp["id"], "collection", collection_id, "test-fixture", "required")


def _membership_rows(pg_engine, corpus_id: str) -> dict:
    with pg_engine.connect() as conn:
        return {
            r["fact_id"]: (r["claims_count"], r["documents_count"])
            for r in conn.execute(
                sa.text(
                    "SELECT fact_id, claims_count, documents_count FROM fact_collection_membership WHERE corpus_id = :c"
                ),
                {"c": corpus_id},
            ).mappings()
        }


def _stats_row(pg_engine, corpus_id: str):
    with pg_engine.connect() as conn:
        row = (
            conn.execute(
                sa.text(
                    "SELECT facts_count, claims_count, edges_count, documents_with_claims "
                    "FROM fact_collection_stats WHERE corpus_id = :c"
                ),
                {"c": corpus_id},
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None


def _wipe_summary(pg_engine) -> None:
    """Simulate the pre-rebuild bootstrap window: the summary tables exist
    (the migration ran) but are empty (the operator hasn't run the rebuild
    yet) — every reader must fall back to the original `claims` scan."""
    with pg_engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM fact_collection_membership"))
        conn.execute(sa.text("DELETE FROM edge_collection_membership"))
        conn.execute(sa.text("DELETE FROM fact_collection_stats"))


# ---------------------------------------------------------------------------
# Maintenance on the hot ingest path (add_claim)
# ---------------------------------------------------------------------------


def test_add_claim_maintains_membership_and_stats_incrementally(pg_env, repo):
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a2")

    fact_id = repo.create_fact(type="engagement")
    repo.add_claim(
        fact_id=fact_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="Quote one is here."
    )
    repo.add_claim(
        fact_id=fact_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="Quote two is here."
    )
    repo.add_claim(
        fact_id=fact_id, corpus_file_id="cf_a2", corpus_id=CORPUS_A, file_sha256="s", quote="Quote three is here."
    )

    assert _membership_rows(pg_env, CORPUS_A)[fact_id] == (3, 2)
    assert _stats_row(pg_env, CORPUS_A) == {
        "facts_count": 1,
        "claims_count": 3,
        "edges_count": 0,
        "documents_with_claims": 2,
    }


def test_add_claim_replay_does_not_double_count(pg_env, repo):
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")

    fact_id = repo.create_fact(type="engagement")
    kwargs = dict(fact_id=fact_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="Same quote.")
    assert repo.add_claim(**kwargs) is not None
    assert repo.add_claim(**kwargs) is None  # ON CONFLICT DO NOTHING — replay

    assert _membership_rows(pg_env, CORPUS_A)[fact_id] == (1, 1)
    assert _stats_row(pg_env, CORPUS_A) == {
        "facts_count": 1,
        "claims_count": 1,
        "edges_count": 0,
        "documents_with_claims": 1,
    }


def test_add_claim_maintains_edge_membership(pg_env, repo):
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")

    src_id = repo.create_fact(type="engagement")
    dst_id = repo.create_fact(type="industry")
    edge_id = repo.create_edge(src=src_id, type="works_in_industry", dst=dst_id)
    repo.add_claim(
        edge_id=edge_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="Acme is in SaaS."
    )

    with pg_env.connect() as conn:
        row = (
            conn.execute(
                sa.text("SELECT claims_count FROM edge_collection_membership WHERE corpus_id = :c AND edge_id = :e"),
                {"c": CORPUS_A, "e": edge_id},
            )
            .mappings()
            .first()
        )
    assert row["claims_count"] == 1
    stats = _stats_row(pg_env, CORPUS_A)
    assert stats["edges_count"] == 1
    assert stats["claims_count"] == 1
    # The edge's two endpoints carry no claim of their own — facts_count
    # must not count them.
    assert stats["facts_count"] == 0


# ---------------------------------------------------------------------------
# Bulk mutation paths — scoped reconciliation
# ---------------------------------------------------------------------------


def test_delete_claims_for_file_reconciles_stats(pg_env, repo):
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    fact_id = repo.create_fact(type="engagement")
    repo.add_claim(fact_id=fact_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="Some quote.")
    assert _stats_row(pg_env, CORPUS_A)["claims_count"] == 1

    repo.delete_claims_for_file("cf_a1")

    assert _stats_row(pg_env, CORPUS_A) is None  # collection now empty -> row removed
    assert _membership_rows(pg_env, CORPUS_A) == {}


def test_delete_claims_for_file_decrements_membership_when_another_file_still_cites_the_fact(pg_env, repo):
    """TCRD-296 gap #73: `delete_claims_for_file` must be an incremental
    delta, not a full-collection rebuild — the case that distinguishes the
    two is exactly this one, where a SECOND file's claims survive the
    delete."""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a2")
    fact_id = repo.create_fact(type="engagement")
    repo.add_claim(
        fact_id=fact_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="First quote here."
    )
    repo.add_claim(
        fact_id=fact_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="Second quote here."
    )
    repo.add_claim(
        fact_id=fact_id, corpus_file_id="cf_a2", corpus_id=CORPUS_A, file_sha256="s", quote="Third quote here."
    )
    assert _membership_rows(pg_env, CORPUS_A)[fact_id] == (3, 2)
    assert _stats_row(pg_env, CORPUS_A) == {
        "facts_count": 1,
        "claims_count": 3,
        "edges_count": 0,
        "documents_with_claims": 2,
    }

    repo.delete_claims_for_file("cf_a1")

    # cf_a2 still cites the fact -- the membership row survives, decremented
    # by exactly the 2 claims cf_a1 contributed and by 1 document (its own).
    assert _membership_rows(pg_env, CORPUS_A)[fact_id] == (1, 1)
    assert _stats_row(pg_env, CORPUS_A) == {
        "facts_count": 1,
        "claims_count": 1,
        "edges_count": 0,
        "documents_with_claims": 1,
    }

    repo.delete_claims_for_file("cf_a2")

    assert _membership_rows(pg_env, CORPUS_A) == {}
    assert _stats_row(pg_env, CORPUS_A) is None


def test_delete_claims_for_file_decrements_edge_membership_when_another_file_still_cites_the_edge(pg_env, repo):
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a2")
    src_id = repo.create_fact(type="engagement")
    dst_id = repo.create_fact(type="industry")
    edge_id = repo.create_edge(src=src_id, type="works_in_industry", dst=dst_id)
    repo.add_claim(
        edge_id=edge_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="Acme is in SaaS one."
    )
    repo.add_claim(
        edge_id=edge_id, corpus_file_id="cf_a2", corpus_id=CORPUS_A, file_sha256="s", quote="Acme is in SaaS two."
    )

    def _edge_claims_count():
        with pg_env.connect() as conn:
            return conn.execute(
                sa.text("SELECT claims_count FROM edge_collection_membership WHERE corpus_id = :c AND edge_id = :e"),
                {"c": CORPUS_A, "e": edge_id},
            ).scalar()

    assert _edge_claims_count() == 2

    repo.delete_claims_for_file("cf_a1")

    assert _edge_claims_count() == 1
    assert _stats_row(pg_env, CORPUS_A) == {
        "facts_count": 0,
        "claims_count": 1,
        "edges_count": 1,
        "documents_with_claims": 1,
    }

    repo.delete_claims_for_file("cf_a2")

    assert _edge_claims_count() is None
    assert _stats_row(pg_env, CORPUS_A) is None


def test_incremental_maintenance_matches_recompute_after_random_insert_delete_sequence(pg_env, repo):
    """Property-style check: after a sequence of `add_claim` inserts and
    per-file `delete_claims_for_file` deletes, the counters `_bump_
    collection_stats_impl`/`_decrement_collection_stats_impl` maintain
    incrementally must equal a from-scratch recompute straight from
    `claims` (the read-only `collection_stats_consistency_check` helper)
    at every step — not just at the end."""
    rng = random.Random(20260904)
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    file_ids = [f"cf_prop_{i}" for i in range(4)]
    for fid in file_ids:
        _seed_corpus_file(corpus_id=CORPUS_A, file_id=fid)

    fact_ids = [repo.create_fact(type="engagement") for _ in range(3)]
    edge_id = repo.create_edge(src=fact_ids[0], type="works_in_industry", dst=fact_ids[1])
    subjects = [("fact", fid) for fid in fact_ids] + [("edge", edge_id)]

    quote_n = 0
    for fid in file_ids:
        for _ in range(rng.randint(1, 3)):
            kind, subject_id = rng.choice(subjects)
            quote_n += 1
            kwargs = dict(
                corpus_file_id=fid, corpus_id=CORPUS_A, file_sha256="s", quote=f"Random quote {quote_n} in the text."
            )
            if kind == "fact":
                repo.add_claim(fact_id=subject_id, **kwargs)
            else:
                repo.add_claim(edge_id=subject_id, **kwargs)

    check = repo.collection_stats_consistency_check(CORPUS_A)
    assert check["consistent"], check

    shuffled_files = list(file_ids)
    rng.shuffle(shuffled_files)
    for fid in shuffled_files:
        repo.delete_claims_for_file(fid)
        check = repo.collection_stats_consistency_check(CORPUS_A)
        assert check["consistent"], check


def test_delete_claims_for_file_never_runs_a_full_collection_regroup(pg_env, repo):
    """Regression (TCRD-296 gap #73, live-Postgres finding 2026-09-04): a
    full `rebuild_collection_stats` on this per-file, per-document HOT path
    used to re-derive the WHOLE collection's membership from `claims` on
    every single delete — on a collection with millions of claims, four
    concurrent extraction passes turned that into a full `GROUP BY
    corpus_id, fact_id`/`GROUP BY corpus_id, edge_id` regroup every few
    seconds. Proven by recording every statement `delete_claims_for_file`
    issues and asserting that full-collection regroup signature (unique to
    `_rebuild_one_collection_stats`, absent from the targeted per-subject
    UPDATEs this method now issues) never appears."""
    from sqlalchemy import event

    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    fact_id = repo.create_fact(type="engagement")
    repo.add_claim(fact_id=fact_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="Some quote.")

    statements: list = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(repo._engine, "before_cursor_execute", _capture)
    try:
        repo.delete_claims_for_file("cf_a1")
    finally:
        event.remove(repo._engine, "before_cursor_execute", _capture)

    assert not any("GROUP BY corpus_id, fact_id" in s for s in statements), statements
    assert not any("GROUP BY corpus_id, edge_id" in s for s in statements), statements
    assert not any("FROM claims" in s and "GROUP BY" in s for s in statements), statements


def test_reassign_file_corpus_reconciles_both_collections(pg_env, repo):
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_collection(collection_id=CORPUS_B, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    fact_id = repo.create_fact(type="engagement")
    repo.add_claim(fact_id=fact_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="Some quote.")
    assert _stats_row(pg_env, CORPUS_A)["claims_count"] == 1
    assert _stats_row(pg_env, CORPUS_B) is None

    repo.reassign_file_corpus("cf_a1", CORPUS_B)

    assert _stats_row(pg_env, CORPUS_A) is None
    b_stats = _stats_row(pg_env, CORPUS_B)
    assert b_stats == {"facts_count": 1, "claims_count": 1, "edges_count": 0, "documents_with_claims": 1}


def test_ingest_batch_full_documents_replace_reconciles_stats(pg_env, repo):
    """The `full_documents` replace-mode bulk delete inside `ingest_batch`
    itself (a direct SQL DELETE, not via `delete_claims_for_file`) must
    also reconcile — this is the path a re-extraction pass takes."""
    from src.repositories import corpus_file_sources_repo

    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    with pg_env.begin() as conn:
        conn.execute(
            sa.text("UPDATE corpus_files SET processing_status = 'indexed' WHERE id = 'cf_a1'"),
        )
        conn.execute(
            sa.text(
                "INSERT INTO corpus_chunks (id, corpus_id, file_id, ordinal, text) "
                "VALUES ('ck1', :c, 'cf_a1', 0, 'Acme Corp operates in the SaaS industry.')"
            ),
            {"c": CORPUS_A},
        )
    corpus_file_sources_repo().upsert(
        corpus_file_id="cf_a1", corpus_id=CORPUS_A, source_stable_id="cf_a1", source_doc_id="doc1"
    )

    report = repo.ingest_batch(
        nodes=[
            {
                "id": "engagement:acme",
                "type": "engagement",
                "evidence": [{"doc_id": "doc1", "quote": "Acme Corp operates in the SaaS industry."}],
            }
        ]
    )
    assert report["claims_written"] == 1
    assert _stats_row(pg_env, CORPUS_A)["claims_count"] == 1

    # Re-extraction with different text — replace mode drops the stale claim.
    with pg_env.begin() as conn:
        conn.execute(sa.text("DELETE FROM corpus_chunks WHERE file_id = 'cf_a1'"))
        conn.execute(
            sa.text(
                "INSERT INTO corpus_chunks (id, corpus_id, file_id, ordinal, text) "
                "VALUES ('ck2', :c, 'cf_a1', 0, 'Nothing about Acme here now.')"
            ),
            {"c": CORPUS_A},
        )
    report2 = repo.ingest_batch(full_documents=["doc1"])
    assert report2["claims_written"] == 0
    assert _stats_row(pg_env, CORPUS_A) is None


def test_merge_facts_reconciles_stats(pg_env, repo):
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    canonical_id = repo.create_fact(type="engagement")
    merged_id = repo.create_fact(type="engagement")
    repo.add_claim(
        fact_id=merged_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="Merged quote."
    )
    assert _stats_row(pg_env, CORPUS_A) == {
        "facts_count": 1,
        "claims_count": 1,
        "edges_count": 0,
        "documents_with_claims": 1,
    }

    repo.merge_facts(canonical_id=canonical_id, merged_id=merged_id, merged_by="admin@test.com")

    stats = _stats_row(pg_env, CORPUS_A)
    assert stats == {"facts_count": 1, "claims_count": 1, "edges_count": 0, "documents_with_claims": 1}
    membership = _membership_rows(pg_env, CORPUS_A)
    assert canonical_id in membership
    assert merged_id not in membership


def test_split_fact_reconciles_stats(pg_env, repo):
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    canonical_id = repo.create_fact(type="engagement")
    merged_id = repo.create_fact(type="engagement")
    repo.add_claim(
        fact_id=merged_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="Splittable quote."
    )
    snapshot = repo.merge_facts(canonical_id=canonical_id, merged_id=merged_id, merged_by="admin@test.com")

    new_id = repo.split_fact(canonical_id=canonical_id, snapshot=snapshot, split_by="admin@test.com")

    membership = _membership_rows(pg_env, CORPUS_A)
    assert new_id in membership
    assert canonical_id not in membership
    stats = _stats_row(pg_env, CORPUS_A)
    assert stats == {"facts_count": 1, "claims_count": 1, "edges_count": 0, "documents_with_claims": 1}


def test_consolidation_reconciles_collection_stats(pg_env, repo):
    from src.repositories.sharepoint_collection_consolidation_pg import (
        SharePointCollectionConsolidationPgRepository,
    )

    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_collection(collection_id=CORPUS_B, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    fact_id = repo.create_fact(type="engagement")
    repo.add_claim(fact_id=fact_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="Some quote.")
    assert _stats_row(pg_env, CORPUS_A)["claims_count"] == 1
    assert _stats_row(pg_env, CORPUS_B) is None

    SharePointCollectionConsolidationPgRepository(pg_env).consolidate(source_ids=[CORPUS_A], target_id=CORPUS_B)

    assert _stats_row(pg_env, CORPUS_A) is None
    assert _stats_row(pg_env, CORPUS_B)["claims_count"] == 1


# ---------------------------------------------------------------------------
# rebuild_collection_stats — idempotence
# ---------------------------------------------------------------------------


def test_rebuild_collection_stats_is_idempotent(pg_env, repo):
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    fact_id = repo.create_fact(type="engagement")
    repo.add_claim(fact_id=fact_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="One quote.")

    before = _stats_row(pg_env, CORPUS_A)
    before_membership = _membership_rows(pg_env, CORPUS_A)

    result1 = repo.rebuild_collection_stats()
    after1 = _stats_row(pg_env, CORPUS_A)
    result2 = repo.rebuild_collection_stats()
    after2 = _stats_row(pg_env, CORPUS_A)

    assert result1["collections_rebuilt"] >= 1
    assert result2 == result1
    assert after1 == before
    assert after2 == before
    assert _membership_rows(pg_env, CORPUS_A) == before_membership


def test_rebuild_collection_stats_scoped_to_one_collection(pg_env, repo):
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_collection(collection_id=CORPUS_B, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    _seed_corpus_file(corpus_id=CORPUS_B, file_id="cf_b1")
    fact_a = repo.create_fact(type="engagement")
    fact_b = repo.create_fact(type="engagement")
    repo.add_claim(fact_id=fact_a, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="A quote.")
    repo.add_claim(fact_id=fact_b, corpus_file_id="cf_b1", corpus_id=CORPUS_B, file_sha256="s", quote="B quote.")

    _wipe_summary(pg_env)
    result = repo.rebuild_collection_stats(corpus_ids=[CORPUS_A])

    assert result == {"collections_rebuilt": 1}
    assert _stats_row(pg_env, CORPUS_A) is not None
    assert _stats_row(pg_env, CORPUS_B) is None  # untouched — out of scope


def test_rebuild_collection_stats_removes_stale_row_for_now_empty_collection(pg_env, repo):
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    fact_id = repo.create_fact(type="engagement")
    repo.add_claim(fact_id=fact_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="A quote.")
    assert _stats_row(pg_env, CORPUS_A) is not None

    with pg_env.begin() as conn:
        conn.execute(sa.text("DELETE FROM claims WHERE corpus_id = :c"), {"c": CORPUS_A})

    repo.rebuild_collection_stats(corpus_ids=[CORPUS_A])
    assert _stats_row(pg_env, CORPUS_A) is None


# ---------------------------------------------------------------------------
# Reader parity: fallback (summary empty) vs fast (summary populated) paths
# ---------------------------------------------------------------------------


def _seed_reader_parity_fixture(pg_engine, repo):
    """Two collections, three facts (one revealed-but-withheld, one plain,
    one edge-anchor-only), one edge, a non-admin caller (alice) granted
    CORPUS_A only — enough shape to exercise corrections, endpoint
    evidence, and cross-collection visibility in every targeted reader."""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_collection(collection_id=CORPUS_B, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    _seed_corpus_file(corpus_id=CORPUS_B, file_id="cf_b1")

    plain_id = repo.create_fact(type="engagement")
    repo.add_alias(fact_id=plain_id, type="engagement", natural_key="engagement:plain-one")
    repo.add_claim(
        fact_id=plain_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="Plain one is active."
    )
    repo.add_claim(
        fact_id=plain_id, corpus_file_id="cf_b1", corpus_id=CORPUS_B, file_sha256="s", quote="Plain one, more detail."
    )

    withheld_id = repo.create_fact(type="engagement")
    repo.add_alias(fact_id=withheld_id, type="engagement", natural_key="engagement:withheld-one")
    repo.add_claim(
        fact_id=withheld_id, corpus_file_id="cf_b1", corpus_id=CORPUS_B, file_sha256="s", quote="Withheld evidence."
    )
    repo.upsert_correction(
        subject_kind="fact",
        subject_id=withheld_id,
        natural_keys=["engagement:withheld-one"],
        verdict="wrong",
        reason="test",
        decided_by="admin@test.com",
    )

    anchor_src = repo.create_fact(type="engagement")
    repo.add_alias(fact_id=anchor_src, type="engagement", natural_key="engagement:anchor-src")
    repo.add_claim(
        fact_id=anchor_src, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="Anchor src claim."
    )
    anchor_dst = repo.create_fact(type="industry")
    repo.add_alias(fact_id=anchor_dst, type="industry", natural_key="industry:anchor-dst")
    edge_id = repo.create_edge(src=anchor_src, type="works_in_industry", dst=anchor_dst)
    repo.add_claim(
        edge_id=edge_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="Anchor edge claim."
    )

    from src.repositories import users_repo

    users_repo().create(id="alice", email="alice@test.com", name="Alice")
    _make_group_with_grant(pg_engine, group_name="group-alice-parity", collection_id=CORPUS_A, member_user_id="alice")
    return {"plain_id": plain_id, "withheld_id": withheld_id, "anchor_src": anchor_src, "anchor_dst": anchor_dst}


def _snapshot_reader_outputs(repo, caller):
    return {
        "facet_values": repo.facet_values(caller, types=["engagement", "industry"], limit_per_type=25),
        "count_visible_facts_by_type": repo.count_visible_facts_by_type(caller),
        "count_visible_edges_by_type": repo.count_visible_edges_by_type(caller),
        "count_visible_facts_for_collections": repo.count_visible_facts_for_collections(caller, [CORPUS_A, CORPUS_B]),
        "count_visible_edges_for_collections": repo.count_visible_edges_for_collections(caller, [CORPUS_A, CORPUS_B]),
    }


def test_reader_parity_between_fallback_and_summary_paths_non_admin(pg_env, repo):
    _seed_reader_parity_fixture(pg_env, repo)
    alice = _dict_user("alice")

    with_summary = _snapshot_reader_outputs(repo, alice)

    _wipe_summary(pg_env)
    fallback = _snapshot_reader_outputs(repo, alice)

    assert json.dumps(with_summary, sort_keys=True, default=str) == json.dumps(fallback, sort_keys=True, default=str)


def test_reader_parity_between_fallback_and_summary_paths_admin(pg_env, repo):
    _seed_reader_parity_fixture(pg_env, repo)
    admin = _dict_user("root")  # not narrowed by _readable_ids -> admin path in these helpers' callers

    # `count_visible_facts_by_type`/etc. resolve admin-ness via
    # `_readable_ids`, which treats an unrestricted dict user as admin only
    # through the real auth layer — exercise the two corpus-scoped
    # "approximate"/"top values" methods directly instead, which take an
    # explicit corpus_ids list rather than a caller.
    with_summary = {
        "approximate_counts": repo.approximate_counts_for_collections([CORPUS_A, CORPUS_B]),
        "facet_top_values": repo.facet_top_values_for_collections(
            [CORPUS_A, CORPUS_B], types=["engagement", "industry"]
        ),
        "facet_top_values_global": repo.facet_top_values_for_collections(None, types=["engagement", "industry"]),
    }

    _wipe_summary(pg_env)
    fallback = {
        "approximate_counts": repo.approximate_counts_for_collections([CORPUS_A, CORPUS_B]),
        "facet_top_values": repo.facet_top_values_for_collections(
            [CORPUS_A, CORPUS_B], types=["engagement", "industry"]
        ),
        "facet_top_values_global": repo.facet_top_values_for_collections(None, types=["engagement", "industry"]),
    }

    assert with_summary == fallback
    del admin


# ---------------------------------------------------------------------------
# Perf-shaped: the fast path is an index lookup, not a `claims` seq scan.
# ---------------------------------------------------------------------------


def test_candidate_source_avoids_claims_seq_scan_at_scale(pg_env, repo):
    """Bulk-seeds a scaled-down but shape-correct proxy for the live
    incident (390 collections / 2M claims) — enough rows for the Postgres
    planner to make a real cost-based choice, not so many that this test is
    slow. Bulk INSERT (not `add_claim` in a loop) so seeding itself stays
    fast; a single `rebuild_collection_stats()` populates the summary the
    way an operator's one-time backfill would."""
    n_collections = 40
    claims_per_collection = 150

    with pg_env.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, email, name) VALUES ('uploader1', 'u@test.com', 'U')"))
        for i in range(n_collections):
            cid = f"perf_col_{i}"
            conn.execute(
                sa.text("INSERT INTO file_corpora (id, slug, name, created_by) VALUES (:id, :id, :id, 'uploader1')"),
                {"id": cid},
            )
            fid = f"perf_file_{i}"
            conn.execute(
                sa.text("INSERT INTO corpus_files (id, corpus_id, filename, sha256) VALUES (:id, :c, :id, 'sha')"),
                {"id": fid, "c": cid},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO facts (id, type) SELECT 'perf_fact_' || :i || '_' || g, 'engagement' "
                    "FROM generate_series(0, :n - 1) g"
                ),
                {"i": i, "n": claims_per_collection},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO claims (id, fact_id, corpus_file_id, corpus_id, file_sha256, quote, quote_hash) "
                    "SELECT 'perf_claim_' || :i || '_' || g, 'perf_fact_' || :i || '_' || g, :f, :c, 'sha', "
                    "'quote ' || g, 'hash_' || :i || '_' || g "
                    "FROM generate_series(0, :n - 1) g"
                ),
                {"i": i, "f": fid, "c": cid, "n": claims_per_collection},
            )

    repo.rebuild_collection_stats()
    with pg_env.begin() as conn:
        conn.execute(sa.text("ANALYZE claims"))
        conn.execute(sa.text("ANALYZE fact_collection_membership"))
        conn.execute(sa.text("ANALYZE fact_collection_stats"))

    # Per-collection form (`all_collections=False`) — the shape the Library
    # index / `count_visible_facts_for_collections` / `collection_facts_
    # summary` use, and the one where the win is a genuine INDEX lookup
    # (bound by `:corpus_id`, backed by `fact_collection_membership`'s
    # `(corpus_id, fact_id)` primary key) rather than the `all_collections
    # =True` form's necessarily-full scan of the (much smaller) membership
    # table.
    cte = repo._visible_facts_for_corpus_cte(is_admin=True, all_evidence=False, all_collections=False)
    sql = sa.text(f"EXPLAIN (ANALYZE, FORMAT JSON) WITH {cte} SELECT COUNT(*) FROM visible")
    with pg_env.begin() as conn:
        plan = conn.execute(sql, {"corpus_id": "perf_col_5"}).scalar()
    plan = plan if isinstance(plan, list) else json.loads(plan)

    def _flatten(node):
        out = [node]
        for child in node.get("Plans", []) or []:
            out.extend(_flatten(child))
        return out

    nodes = _flatten(plan[0]["Plan"])

    # The candidate-source UNION's `claims`-scan fallback branch (aliased
    # `c` in the SQL text — see `candidate_ids`'s second arm) is genuinely
    # present in the static plan (the "row missing" degrade path) — proving
    # the OPTIMIZATION means proving it's never actually EXECUTED at runtime
    # (`ANALYZE`'s "Actual Loops" is 0) when the summary is populated, not
    # that the plan text omits it. Postgres's own "one-time filter" over the
    # uncorrelated `NOT EXISTS (SELECT 1 FROM fact_collection_stats)` closes
    # that branch off before its child scan ever runs. Scoped to alias `c`
    # specifically — `claims` is ALSO scanned elsewhere in this query (the
    # `vis`/`vis3`/`vis_ec` visibility gate, e.g. `EXISTS (SELECT 1 FROM
    # claims c2 ...)`), which is pre-existing, unrelated work this PR does
    # not touch and makes no claim about.
    candidate_fallback_scans = [n for n in nodes if n.get("Relation Name") == "claims" and n.get("Alias") == "c"]
    assert candidate_fallback_scans, "expected the fallback branch's claims scan (alias c) in the static plan"
    assert all(n.get("Actual Loops", 1) == 0 for n in candidate_fallback_scans), (
        f"fallback claims scan(s) actually executed: {candidate_fallback_scans}"
    )

    # And the fast path really did run: an INDEX lookup (never a sequential
    # scan) on the membership table, scoped to just this one collection's
    # rows.
    membership_scans = [n for n in nodes if n.get("Relation Name") == "fact_collection_membership"]
    assert membership_scans, f"expected a scan of fact_collection_membership, nodes were: {nodes}"
    assert all(n.get("Node Type") != "Seq Scan" for n in membership_scans), (
        f"expected an index lookup on fact_collection_membership, not a Seq Scan: {membership_scans}"
    )
    assert any(n.get("Actual Rows", 0) == claims_per_collection for n in membership_scans)
