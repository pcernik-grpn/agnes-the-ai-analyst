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


def _make_admin(member_user_id: str) -> None:
    """Add ``member_user_id`` to the seeded ``Admin`` system group — the
    ONLY thing `_readable_ids`/`is_user_admin` treat as admin (a bare dict
    caller with no such membership resolves as narrowed, per
    `test_reader_parity_between_fallback_and_summary_paths_admin`'s own
    comment)."""
    from src.repositories import user_group_members_repo, user_groups_repo

    admin_group = user_groups_repo().get_by_name("Admin")
    assert admin_group is not None, "Admin system group must already be seeded by pg_env"
    user_group_members_repo().add_member(member_user_id, admin_group["id"], source="admin", added_by="test-fixture")


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
        conn.execute(sa.text("DELETE FROM fact_collection_type_counts"))


def _type_counts_rows(pg_engine, corpus_id: str) -> dict:
    with pg_engine.connect() as conn:
        return {
            r["type"]: r["count"]
            for r in conn.execute(
                sa.text("SELECT type, count FROM fact_collection_type_counts WHERE corpus_id = :c"),
                {"c": corpus_id},
            ).mappings()
        }


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
# Per-type companion table (TCRD-296 gap #81) — maintained the SAME way as
# the sibling counters, by the SAME three writers.
# ---------------------------------------------------------------------------


def test_add_claim_maintains_type_counts(pg_env, repo):
    """Writer #1: the hot ingest path (`_bump_collection_stats_impl`, via
    `add_claim`) advances `fact_collection_type_counts` in lockstep with
    `fact_collection_stats.facts_count` — one increment per DISTINCT fact,
    never per claim."""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")

    engagement_id = repo.create_fact(type="engagement")
    industry_id = repo.create_fact(type="industry")
    repo.add_claim(
        fact_id=engagement_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="Quote one is here."
    )
    # A second claim on the SAME fact must not double-count its type.
    repo.add_claim(
        fact_id=engagement_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="Quote two is here."
    )
    repo.add_claim(
        fact_id=industry_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="Quote three here."
    )

    assert _type_counts_rows(pg_env, CORPUS_A) == {"engagement": 1, "industry": 1}


def test_add_claim_edge_claim_does_not_create_a_type_counts_row(pg_env, repo):
    """An edge's own claim carries no fact `type` — `_bump_collection_stats_
    impl`'s edge branch must never touch `fact_collection_type_counts`."""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")

    src_id = repo.create_fact(type="engagement")
    dst_id = repo.create_fact(type="industry")
    edge_id = repo.create_edge(src=src_id, type="works_in_industry", dst=dst_id)
    repo.add_claim(
        edge_id=edge_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="Acme is in SaaS."
    )

    assert _type_counts_rows(pg_env, CORPUS_A) == {}


def test_delete_claims_for_file_decrements_type_counts(pg_env, repo):
    """Writer #2: the per-file delete path (`_decrement_collection_stats_
    impl`) is the exact inverse of the bump above — decrements only when a
    fact's LAST claim in this collection goes away, never while another
    file still cites it."""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a2")

    engagement_id = repo.create_fact(type="engagement")
    industry_id = repo.create_fact(type="industry")
    repo.add_claim(
        fact_id=engagement_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="Quote one is here."
    )
    repo.add_claim(
        fact_id=engagement_id,
        corpus_file_id="cf_a2",
        corpus_id=CORPUS_A,
        file_sha256="s",
        quote="Quote from another file.",
    )
    repo.add_claim(
        fact_id=industry_id, corpus_file_id="cf_a2", corpus_id=CORPUS_A, file_sha256="s", quote="Quote two is here."
    )
    assert _type_counts_rows(pg_env, CORPUS_A) == {"engagement": 1, "industry": 1}

    # cf_a1's delete leaves engagement_id still cited by cf_a2 — the type
    # count must not drop.
    repo.delete_claims_for_file("cf_a1")
    assert _type_counts_rows(pg_env, CORPUS_A) == {"engagement": 1, "industry": 1}

    # cf_a2's delete removes engagement_id's LAST claim in this collection
    # and industry_id's only one.
    repo.delete_claims_for_file("cf_a2")
    assert _type_counts_rows(pg_env, CORPUS_A) == {}


def test_rebuild_collection_stats_reconciles_type_counts(pg_env, repo):
    """Writer #3: the full rebuild path (`_rebuild_one_collection_stats`)
    repopulates `fact_collection_type_counts` too — a maintained table with
    an unmaintained writer is the classic way this kind of summary drifts
    silently."""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")

    engagement_id = repo.create_fact(type="engagement")
    industry_id = repo.create_fact(type="industry")
    repo.add_claim(
        fact_id=engagement_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="Quote one is here."
    )
    repo.add_claim(
        fact_id=industry_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="Quote two is here."
    )
    assert _type_counts_rows(pg_env, CORPUS_A) == {"engagement": 1, "industry": 1}

    # Simulate drift: wipe ONLY the type-counts table, as if this writer had
    # never run (the pre-fix bug this migration and its writers now close).
    with pg_env.begin() as conn:
        conn.execute(sa.text("DELETE FROM fact_collection_type_counts WHERE corpus_id = :c"), {"c": CORPUS_A})
    assert _type_counts_rows(pg_env, CORPUS_A) == {}

    result = repo.rebuild_collection_stats(corpus_ids=[CORPUS_A])

    assert result == {"collections_rebuilt": 1}
    assert _type_counts_rows(pg_env, CORPUS_A) == {"engagement": 1, "industry": 1}


def test_type_counts_maintained_equals_exact_computation(pg_env, repo):
    """Anti-drift check: the maintained per-type breakdown, built entirely
    through the normal ingest path (`add_claim`), must equal a from-scratch
    recompute straight from `claims`/`facts` — the same ground-truth query
    `_rebuild_one_collection_stats` writes."""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a2")

    engagement_ids = [repo.create_fact(type="engagement") for _ in range(3)]
    industry_id = repo.create_fact(type="industry")
    for i, fid in enumerate(engagement_ids):
        repo.add_claim(
            fact_id=fid, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote=f"Engagement quote {i}."
        )
    repo.add_claim(
        fact_id=industry_id, corpus_file_id="cf_a2", corpus_id=CORPUS_A, file_sha256="s", quote="Industry quote here."
    )
    # A second claim on an existing fact, from another file, must not
    # double-count its type either.
    repo.add_claim(
        fact_id=engagement_ids[0],
        corpus_file_id="cf_a2",
        corpus_id=CORPUS_A,
        file_sha256="s",
        quote="A second quote for the first one.",
    )

    with pg_env.connect() as conn:
        exact = {
            r["type"]: r["n"]
            for r in conn.execute(
                sa.text(
                    "SELECT f.type AS type, COUNT(DISTINCT c.fact_id) AS n FROM claims c "
                    "JOIN facts f ON f.id = c.fact_id WHERE c.corpus_id = :c GROUP BY f.type"
                ),
                {"c": CORPUS_A},
            ).mappings()
        }
    assert exact == {"engagement": 3, "industry": 1}
    assert _type_counts_rows(pg_env, CORPUS_A) == exact


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


def test_ingest_batch_replace_never_runs_a_full_collection_regroup(pg_env, repo):
    """Regression (TCRD-296 gap #73b, live-Postgres finding 2026-09-04):
    `ingest_batch`'s `full_documents` replace path used to call
    `rebuild_collection_stats` — a full `GROUP BY corpus_id, fact_id` /
    `GROUP BY corpus_id, edge_id` regroup of the WHOLE collection — on
    EVERY batch of a re-extraction pass. Four concurrent passes over one
    2.4M-claim collection turned that into 30+ unique-constraint
    violations and a detected deadlock in 15 minutes, each one a document
    whose claims had already been deleted and were then never
    reconciled. Proven the same way #2238 proved it for
    `delete_claims_for_file`: capture every statement the replace batch
    issues and assert that full-collection regroup signature never
    appears, then confirm the incrementally-maintained counters still
    match a from-scratch recompute (what a rebuild would compute)."""
    from sqlalchemy import event

    from src.repositories import corpus_file_sources_repo

    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    with pg_env.begin() as conn:
        conn.execute(sa.text("UPDATE corpus_files SET processing_status = 'indexed' WHERE id = 'cf_a1'"))
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
    repo.ingest_batch(
        nodes=[
            {
                "id": "engagement:acme",
                "type": "engagement",
                "evidence": [{"doc_id": "doc1", "quote": "Acme Corp operates in the SaaS industry."}],
            }
        ]
    )

    statements: list = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(repo._engine, "before_cursor_execute", _capture)
    try:
        # Re-extraction of the SAME document, replace mode — the exact
        # shape a facts-extraction pass ships every ~25 documents.
        report = repo.ingest_batch(
            full_documents=["doc1"],
            nodes=[
                {
                    "id": "engagement:acme",
                    "type": "engagement",
                    "evidence": [{"doc_id": "doc1", "quote": "Acme Corp operates in the SaaS industry."}],
                }
            ],
        )
    finally:
        event.remove(repo._engine, "before_cursor_execute", _capture)

    assert report["claims_written"] == 1
    # The regroup signature unique to `_rebuild_one_collection_stats` — an
    # UNRELATED query legitimately shares "FROM claims" (`add_claim`'s own
    # attribute-conflict check) or "GROUP BY" (grouping by something else
    # entirely) on their own, so the two must be checked as ONE exact
    # phrase, not two independent substrings.
    assert not any("GROUP BY corpus_id, fact_id" in s for s in statements), statements
    assert not any("GROUP BY corpus_id, edge_id" in s for s in statements), statements

    check = repo.collection_stats_consistency_check(CORPUS_A)
    assert check["consistent"], check


def test_concurrent_ingest_batch_bumps_on_the_same_fact_do_not_raise(pg_env, repo, monkeypatch):
    """Two concurrent `ingest_batch` calls, each for a DIFFERENT document,
    both evidencing the SAME (PRE-EXISTING) fact — real threads, real
    Postgres. A `threading.Barrier` forces both calls' own
    `_bump_collection_stats_impl` to reach the database at (as near as
    possible) the same moment, exercising the `INSERT ... ON CONFLICT DO
    UPDATE` this depends on for correctness rather than hoping OS thread
    scheduling happens to create the overlap on its own. The fact/alias
    is seeded BEFORE the concurrent pair runs (a separate, unrelated race
    on `_resolve_alias`'s own fact-creation path — not what this test is
    about — would otherwise make one thread block on a DB lock the OTHER
    holds while it is itself parked at the barrier, deadlocking the test
    against itself)."""
    import threading

    from src.repositories import corpus_file_sources_repo
    from src.repositories.facts_pg import FactsPgRepository

    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    for fid in ("cf_a0", "cf_a1", "cf_a2"):
        _seed_corpus_file(corpus_id=CORPUS_A, file_id=fid)
    with pg_env.begin() as conn:
        for fid, text in (
            ("cf_a0", "Seed document about Acme Corp."),
            ("cf_a1", "First document about Acme Corp."),
            ("cf_a2", "Second document about Acme Corp."),
        ):
            conn.execute(sa.text("UPDATE corpus_files SET processing_status = 'indexed' WHERE id = :fid"), {"fid": fid})
            conn.execute(
                sa.text(
                    "INSERT INTO corpus_chunks (id, corpus_id, file_id, ordinal, text) VALUES (:id, :c, :fid, 0, :text)"
                ),
                {"id": f"ck_{fid}", "c": CORPUS_A, "fid": fid, "text": text},
            )
    corpus_file_sources_repo().upsert(
        corpus_file_id="cf_a0", corpus_id=CORPUS_A, source_stable_id="cf_a0", source_doc_id="doc0"
    )
    corpus_file_sources_repo().upsert(
        corpus_file_id="cf_a1", corpus_id=CORPUS_A, source_stable_id="cf_a1", source_doc_id="doc1"
    )
    corpus_file_sources_repo().upsert(
        corpus_file_id="cf_a2", corpus_id=CORPUS_A, source_stable_id="cf_a2", source_doc_id="doc2"
    )
    # Seed the fact/alias up front — the concurrent pair below only ever
    # ADDS a claim to an alias that already resolves, never races to
    # MINT it.
    repo.ingest_batch(
        nodes=[
            {
                "id": "engagement:acme",
                "type": "engagement",
                "evidence": [{"doc_id": "doc0", "quote": "Seed document about Acme Corp."}],
            }
        ]
    )

    barrier = threading.Barrier(2)
    original_bump = FactsPgRepository._bump_collection_stats_impl

    def _barriered_bump(self, conn, **kwargs):
        barrier.wait(timeout=5)
        return original_bump(self, conn, **kwargs)

    monkeypatch.setattr(FactsPgRepository, "_bump_collection_stats_impl", _barriered_bump)

    errors: list = []

    def _run(doc_id: str, quote: str) -> None:
        try:
            repo.ingest_batch(
                nodes=[
                    {
                        "id": "engagement:acme",
                        "type": "engagement",
                        "evidence": [{"doc_id": doc_id, "quote": quote}],
                    }
                ]
            )
        except Exception as exc:  # noqa: BLE001 — asserted below, never swallowed
            errors.append(exc)

    threads = [
        threading.Thread(target=_run, args=("doc1", "First document about Acme Corp.")),
        threading.Thread(target=_run, args=("doc2", "Second document about Acme Corp.")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    check = repo.collection_stats_consistency_check(CORPUS_A)
    assert check["consistent"], check
    assert check["computed"]["stats"]["claims_count"] == 3  # seed + the two concurrent claims


def test_rebuild_while_a_bump_runs_does_not_raise(pg_env, repo, monkeypatch):
    """TCRD-296 gap #73b: `_rebuild_one_collection_stats`'s DELETE + plain
    `INSERT ... SELECT` used to be able to violate `fact_collection_
    membership`'s primary key when a CONCURRENT `add_claim` ->
    `_bump_collection_stats_impl` (`INSERT ... ON CONFLICT DO UPDATE`)
    landed a row for the same `(corpus_id, fact_id)` between the
    rebuild's DELETE and its own INSERT. Both INSERTs now use `ON
    CONFLICT DO UPDATE` — a real, barrier-synchronized concurrent run
    must not raise."""
    import threading

    from src.repositories.facts_pg import FactsPgRepository

    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a2")
    fact_id = repo.create_fact(type="engagement")
    repo.add_claim(fact_id=fact_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="First quote.")

    barrier = threading.Barrier(2)
    original_bump = FactsPgRepository._bump_collection_stats_impl
    original_rebuild = FactsPgRepository._rebuild_one_collection_stats

    def _barriered_bump(self, conn, **kwargs):
        barrier.wait(timeout=5)
        return original_bump(self, conn, **kwargs)

    def _barriered_rebuild(self, conn, corpus_id):
        barrier.wait(timeout=5)
        return original_rebuild(self, conn, corpus_id)

    monkeypatch.setattr(FactsPgRepository, "_bump_collection_stats_impl", _barriered_bump)
    monkeypatch.setattr(FactsPgRepository, "_rebuild_one_collection_stats", _barriered_rebuild)

    errors: list = []

    def _rebuild() -> None:
        try:
            repo.rebuild_collection_stats(corpus_ids=[CORPUS_A])
        except Exception as exc:  # noqa: BLE001 — asserted below, never swallowed
            errors.append(exc)

    def _bump() -> None:
        try:
            repo.add_claim(
                fact_id=fact_id, corpus_file_id="cf_a2", corpus_id=CORPUS_A, file_sha256="s", quote="Second quote."
            )
        except Exception as exc:  # noqa: BLE001 — asserted below, never swallowed
            errors.append(exc)

    t1 = threading.Thread(target=_rebuild)
    t2 = threading.Thread(target=_bump)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, errors
    check = repo.collection_stats_consistency_check(CORPUS_A)
    assert check["consistent"], check
    assert check["computed"]["stats"]["claims_count"] == 2


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


def test_collection_facts_summary_type_counts_respects_visibility_boundary(pg_env, repo, monkeypatch):
    """TCRD-296 gap #81 — the maintained fast path is admin-only. A caller
    whose visibility is genuinely narrower than "sees everything" must get
    counts for exactly what THEY can see, never the corpus-wide maintained
    total — leaking the wider number across an audience boundary is the
    primary risk in this change.

    Uses `all_evidence` mode (same shape as `test_facts_ui.py::test_facts_
    section_caller_scoped_two_users_different_grants`) so the narrowing is
    genuinely per-caller RBAC, not a `wrong`/`restricted` correction that
    would also withhold the fact from an admin and prove nothing about the
    fast path."""
    monkeypatch.setenv("AGNES_FACTS_VISIBILITY_MODE", "all_evidence")
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_collection(collection_id=CORPUS_B, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    _seed_corpus_file(corpus_id=CORPUS_B, file_id="cf_b1")

    # Spans both collections — under all_evidence, visible only to a caller
    # who can read BOTH.
    spanning_id = repo.create_fact(type="engagement")
    repo.add_claim(
        fact_id=spanning_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="Part A evidence."
    )
    repo.add_claim(
        fact_id=spanning_id, corpus_file_id="cf_b1", corpus_id=CORPUS_B, file_sha256="s", quote="Part B evidence."
    )
    # Evidenced ONLY by corpus A — visible to anyone who can read A alone.
    a_only_id = repo.create_fact(type="engagement")
    repo.add_claim(
        fact_id=a_only_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="A-only evidence."
    )

    from src.repositories import users_repo

    users_repo().create(id="bob", email="bob@test.com", name="Bob")
    _make_group_with_grant(pg_env, group_name="group-bob-a", collection_id=CORPUS_A, member_user_id="bob")

    users_repo().create(id="root-admin", email="root-admin@test.com", name="Root")
    _make_admin("root-admin")

    admin_summary = repo.collection_facts_summary(_dict_user("root-admin"), CORPUS_A)
    bob_summary = repo.collection_facts_summary(_dict_user("bob"), CORPUS_A)

    # The maintained fast path (admin) sees BOTH facts — either has >=1
    # claim in corpus A — the corpus-wide maintained total.
    assert admin_summary["type_counts"] == {"engagement": 2}
    assert admin_summary["total"] == 2
    # Bob can only read corpus A — under all_evidence, `spanning_id` (which
    # also needs corpus B) is invisible to him. His numbers must reflect
    # exactly his own narrower visibility, never the admin/maintained total.
    assert bob_summary["type_counts"] == {"engagement": 1}
    assert bob_summary["total"] == 1
    assert bob_summary["type_counts"] != admin_summary["type_counts"]


def test_collection_facts_summary_type_counts_maintained_skips_facts_join(pg_env, repo):
    """Regression (TCRD-296 gap #81) — the bug this PR fixes. Once
    `fact_collection_type_counts` is populated, an admin's
    `collection_facts_summary` must not run the facts-joining `GROUP BY
    f.type` aggregate at all. Must fail BEFORE the fast path exists (the
    pre-fix code always ran it, regardless of whether a maintained table
    existed)."""
    from sqlalchemy import event

    from src.repositories import users_repo

    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    fact_id = repo.create_fact(type="engagement")
    repo.add_claim(fact_id=fact_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="s", quote="Some quote.")

    users_repo().create(id="root-admin2", email="root-admin2@test.com", name="Root")
    _make_admin("root-admin2")

    statements: list = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(repo._engine, "before_cursor_execute", _capture)
    try:
        summary = repo.collection_facts_summary(_dict_user("root-admin2"), CORPUS_A)
    finally:
        event.remove(repo._engine, "before_cursor_execute", _capture)

    assert summary["type_counts"] == {"engagement": 1}
    assert not any("GROUP BY f.type" in s for s in statements), (
        f"admin render ran the facts-joining type_counts aggregate even though "
        f"fact_collection_type_counts is populated: {statements}"
    )


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


def test_dup_edges_leg_matches_pre_rewrite_query_and_avoids_double_subplan(pg_env, repo):
    """TCRD-296 gap #81's edges leg: `dup_edges_cte`'s
    `e.src IN (...) OR e.dst IN (...)` compiles each disjunct as its OWN
    hashed SubPlan (Postgres cannot flatten an OR of two IN-subqueries into
    one semi-join) — materializing/hashing the `visible` candidate set
    TWICE for 50 output rows. The UNION-of-two-JOINs rewrite must return
    the exact SAME edges the old query did (#5) and its plan must contain
    NO SubPlan node at all — a real JOIN, never a subquery, regardless of
    data volume (this is a structural compilation difference, not a
    cost-based one, so a small fixture is enough to prove it)."""
    n_facts = 50
    n_other_edges = 50
    n_dup_edges = 10

    with pg_env.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, email, name) VALUES ('uploader1', 'u@test.com', 'U')"))
        conn.execute(
            sa.text("INSERT INTO file_corpora (id, slug, name, created_by) VALUES (:c, :c, :c, 'uploader1')"),
            {"c": CORPUS_A},
        )
        conn.execute(
            sa.text("INSERT INTO corpus_files (id, corpus_id, filename, sha256) VALUES ('cf1', :c, 'cf1', 'sha')"),
            {"c": CORPUS_A},
        )
        conn.execute(
            sa.text(
                "INSERT INTO facts (id, type) SELECT 'edge_fact_' || g, 'engagement' FROM generate_series(0, :n - 1) g"
            ),
            {"n": n_facts},
        )
        conn.execute(
            sa.text(
                "INSERT INTO claims (id, fact_id, corpus_file_id, corpus_id, file_sha256, quote, quote_hash) "
                "SELECT 'edge_claim_' || g, 'edge_fact_' || g, 'cf1', :c, 'sha', 'quote ' || g, 'qh_' || g "
                "FROM generate_series(0, :n - 1) g"
            ),
            {"n": n_facts, "c": CORPUS_A},
        )
        # Deterministic, collision-free (src is unique per row within a
        # type, so the (src, type, dst) unique constraint can never fire)
        # rather than random() — this test is about plan SHAPE, not scale.
        conn.execute(
            sa.text(
                "INSERT INTO edges (id, src, dst, type) "
                "SELECT 'e_other_' || g, 'edge_fact_' || g, "
                "'edge_fact_' || ((g * 13 + 7) % :n), 'other_type' "
                "FROM generate_series(0, :m - 1) g"
            ),
            {"n": n_facts, "m": n_other_edges},
        )
        conn.execute(
            sa.text(
                "INSERT INTO edges (id, src, dst, type) "
                "SELECT 'e_dup_' || g, 'edge_fact_' || g, "
                "'edge_fact_' || ((g * 13 + 7 + 5) % :n), 'possible_duplicate_of' "
                "FROM generate_series(0, :d - 1) g"
            ),
            {"n": n_facts, "d": n_dup_edges},
        )

    repo.rebuild_collection_stats(corpus_ids=[CORPUS_A])
    with pg_env.begin() as conn:
        conn.execute(sa.text("ANALYZE"))

    cte = repo._visible_facts_for_corpus_cte(is_admin=True, all_evidence=False)
    old_sql_text = f"""
        WITH {cte}
        SELECT DISTINCT e.id AS id, e.src AS src, e.dst AS dst
        FROM edges e
        WHERE e.type = 'possible_duplicate_of'
          AND (e.src IN (SELECT subject_id FROM visible) OR e.dst IN (SELECT subject_id FROM visible))
        ORDER BY e.id
        LIMIT 50
        """
    new_sql_text = f"""
        WITH {cte}
        SELECT id, src, dst FROM (
            SELECT e.id AS id, e.src AS src, e.dst AS dst
            FROM edges e JOIN visible v ON v.subject_id = e.src
            WHERE e.type = 'possible_duplicate_of'
            UNION
            SELECT e.id AS id, e.src AS src, e.dst AS dst
            FROM edges e JOIN visible v ON v.subject_id = e.dst
            WHERE e.type = 'possible_duplicate_of'
        ) matched
        ORDER BY id
        LIMIT 50
        """

    with pg_env.begin() as conn:
        old_rows = {(r.id, r.src, r.dst) for r in conn.execute(sa.text(old_sql_text), {"corpus_id": CORPUS_A})}
    with pg_env.begin() as conn:
        new_rows = {(r.id, r.src, r.dst) for r in conn.execute(sa.text(new_sql_text), {"corpus_id": CORPUS_A})}

    assert new_rows == old_rows
    assert len(new_rows) == n_dup_edges  # every seeded dup edge fits under the 50-row cap here

    def _flatten(node):
        out = [node]
        for child in node.get("Plans", []) or []:
            out.extend(_flatten(child))
        return out

    with pg_env.begin() as conn:
        old_plan = conn.execute(sa.text(f"EXPLAIN (FORMAT JSON) {old_sql_text}"), {"corpus_id": CORPUS_A}).scalar()
        new_plan = conn.execute(sa.text(f"EXPLAIN (FORMAT JSON) {new_sql_text}"), {"corpus_id": CORPUS_A}).scalar()
    old_plan = old_plan if isinstance(old_plan, list) else json.loads(old_plan)
    new_plan = new_plan if isinstance(new_plan, list) else json.loads(new_plan)
    old_nodes = _flatten(old_plan[0]["Plan"])
    new_nodes = _flatten(new_plan[0]["Plan"])

    # Scoped to a "CTE Scan" of `visible` reached as a SubPlan specifically —
    # `visible` itself has its OWN internal SubPlan/InitPlan machinery (the
    # `EXISTS (SELECT 1 FROM fact_collection_stats)` bootstrap check), which
    # is IDENTICAL in both plans and unrelated to this rewrite; a broader
    # "any SubPlan anywhere" check would flag that shared, pre-existing
    # machinery as if it were the bug this test targets.
    def _is_visible_in_subplan(n):
        return (
            n.get("Node Type") == "CTE Scan"
            and n.get("CTE Name") == "visible"
            and n.get("Parent Relationship") == "SubPlan"
        )

    old_subplans = [n for n in old_nodes if _is_visible_in_subplan(n)]
    new_subplans = [n for n in new_nodes if _is_visible_in_subplan(n)]
    assert old_subplans, f"expected the pre-rewrite OR to compile as hashed SubPlan(s): {old_nodes}"
    assert not new_subplans, f"the UNION-of-joins rewrite must not compile as a SubPlan: {new_nodes}"
