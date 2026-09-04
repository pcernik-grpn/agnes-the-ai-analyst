"""Postgres-only tests for ``SharePointCollectionConsolidationPgRepository``.

There is no DuckDB half to parametrize against (PG-first ratchet, A3) — see
``docs/migrations.md`` -> "Adding a PG-only feature". Seeding style mirrors
``tests/db_pg/test_facts_ingest_pg.py`` / ``tests/db_pg/test_corpus_file_sources_pg.py``.
"""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def pg_env(monkeypatch, pg_engine):
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
    return pg_engine


@pytest.fixture
def repo(pg_env):
    from src.repositories.sharepoint_collection_consolidation_pg import (
        SharePointCollectionConsolidationPgRepository,
    )

    import src.db_pg as db_pg

    return SharePointCollectionConsolidationPgRepository(db_pg.get_engine())


def _seed_collection(pg_env, corpus_id: str, name: str | None = None) -> None:
    with pg_env.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO file_corpora (id, slug, name, created_by) VALUES (:id, :slug, :name, :by)"),
            {"id": corpus_id, "slug": corpus_id, "name": name or corpus_id, "by": "admin1"},
        )


def _seed_file(pg_env, *, file_id: str, corpus_id: str, path: str | None = None, sha256: str = "sha1") -> None:
    with pg_env.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO corpus_files (id, corpus_id, filename, sha256, processing_status, path) "
                "VALUES (:id, :corpus_id, :filename, :sha256, 'indexed', :path)"
            ),
            {"id": file_id, "corpus_id": corpus_id, "filename": f"{file_id}.md", "sha256": sha256, "path": path},
        )


def _seed_chunk(pg_env, *, corpus_id: str, file_id: str) -> None:
    with pg_env.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO corpus_chunks (id, corpus_id, file_id, ordinal, text) "
                "VALUES (:id, :corpus_id, :file_id, 0, 'hello')"
            ),
            {"id": "ck_" + secrets.token_hex(6), "corpus_id": corpus_id, "file_id": file_id},
        )


def _seed_source_mapping(pg_env, *, corpus_id: str, file_id: str, stable_id: str) -> None:
    with pg_env.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO corpus_file_sources (corpus_file_id, corpus_id, source_stable_id) "
                "VALUES (:file_id, :corpus_id, :stable_id)"
            ),
            {"file_id": file_id, "corpus_id": corpus_id, "stable_id": stable_id},
        )


def _seed_event(pg_env, *, corpus_id: str, file_id: str) -> None:
    with pg_env.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO corpus_file_events (id, corpus_id, file_id, change, name) "
                "VALUES (:id, :corpus_id, :file_id, 'added', 'a.md')"
            ),
            {"id": "ev_" + secrets.token_hex(6), "corpus_id": corpus_id, "file_id": file_id},
        )


def _seed_fact(pg_env, fact_id: str) -> None:
    with pg_env.begin() as conn:
        conn.execute(sa.text("INSERT INTO facts (id, type) VALUES (:id, 'company')"), {"id": fact_id})


def _seed_claim(pg_env, *, claim_id: str, fact_id: str, corpus_file_id: str, corpus_id: str) -> None:
    with pg_env.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO claims (id, fact_id, corpus_file_id, corpus_id, file_sha256, quote, quote_hash) "
                "VALUES (:id, :fact_id, :file_id, :corpus_id, 'sha', 'a quote', :qh)"
            ),
            {"id": claim_id, "fact_id": fact_id, "file_id": corpus_file_id, "corpus_id": corpus_id, "qh": claim_id},
        )


def _seed_fact_alias_source(pg_env, *, fact_id: str, natural_key: str, corpus_id: str) -> None:
    with pg_env.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO fact_aliases (fact_id, type, natural_key) VALUES (:fid, 'company', :nk) "
                "ON CONFLICT DO NOTHING"
            ),
            {"fid": fact_id, "nk": natural_key},
        )
        conn.execute(
            sa.text(
                "INSERT INTO fact_alias_sources (type, natural_key, corpus_id) VALUES ('company', :nk, :corpus_id)"
            ),
            {"nk": natural_key, "corpus_id": corpus_id},
        )


def _seed_group(pg_env, name: str) -> str:
    from src.repositories import user_groups_repo

    return user_groups_repo().ensure(name=name, created_by="test")["id"]


def _grant(pg_env, *, group_id: str, corpus_id: str, assigned_by: str = "admin1") -> None:
    from src.repositories import resource_grants_repo

    resource_grants_repo().ensure_grant(group_id, "collection", corpus_id, assigned_by=assigned_by)


def _grants_for(pg_env, corpus_id: str) -> list:
    with pg_env.connect() as conn:
        rows = (
            conn.execute(
                sa.text(
                    "SELECT group_id, assigned_by FROM resource_grants WHERE resource_type = 'collection' AND resource_id = :id"
                ),
                {"id": corpus_id},
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


def _corpus_ids(pg_env, table: str) -> list:
    with pg_env.connect() as conn:
        return list(conn.execute(sa.text(f"SELECT corpus_id FROM {table} ORDER BY corpus_id")).scalars().all())


class TestPreview:
    def test_lists_file_counts_for_the_given_ids_only(self, pg_env, repo):
        _seed_collection(pg_env, "col_a", name="Source A")
        _seed_collection(pg_env, "col_b", name="Source B")
        _seed_collection(pg_env, "col_other", name="Unrelated")
        _seed_file(pg_env, file_id="f1", corpus_id="col_a")
        _seed_file(pg_env, file_id="f2", corpus_id="col_a")
        _seed_file(pg_env, file_id="f3", corpus_id="col_b")

        rows = repo.preview(["col_a", "col_b"])
        by_id = {r["id"]: r["file_count"] for r in rows}
        assert by_id == {"col_a": 2, "col_b": 1}
        assert "col_other" not in by_id

    def test_a_collection_with_zero_files_is_still_listed(self, pg_env, repo):
        _seed_collection(pg_env, "col_empty")
        rows = repo.preview(["col_empty"])
        assert rows == [{"id": "col_empty", "name": "col_empty", "slug": "col_empty", "file_count": 0}]

    def test_a_soft_deleted_collection_is_omitted(self, pg_env, repo):
        _seed_collection(pg_env, "col_gone")
        with pg_env.begin() as conn:
            conn.execute(sa.text("UPDATE file_corpora SET deleted_at = CURRENT_TIMESTAMP WHERE id = 'col_gone'"))
        assert repo.preview(["col_gone"]) == []


class TestConsolidate:
    def test_moves_files_chunks_sources_events_and_claims_onto_the_target(self, pg_env, repo):
        _seed_collection(pg_env, "col_a")
        _seed_collection(pg_env, "col_b")
        _seed_collection(pg_env, "col_target")
        _seed_file(pg_env, file_id="fa", corpus_id="col_a", path="a.docx")
        _seed_file(pg_env, file_id="fb", corpus_id="col_b", path="b.docx")
        _seed_chunk(pg_env, corpus_id="col_a", file_id="fa")
        _seed_chunk(pg_env, corpus_id="col_b", file_id="fb")
        _seed_source_mapping(pg_env, corpus_id="col_a", file_id="fa", stable_id="graph:a")
        _seed_source_mapping(pg_env, corpus_id="col_b", file_id="fb", stable_id="graph:b")
        _seed_event(pg_env, corpus_id="col_a", file_id="fa")
        _seed_event(pg_env, corpus_id="col_b", file_id="fb")
        _seed_fact(pg_env, "fact_a")
        _seed_claim(pg_env, claim_id="cl_a", fact_id="fact_a", corpus_file_id="fa", corpus_id="col_a")
        _seed_claim(pg_env, claim_id="cl_b", fact_id="fact_a", corpus_file_id="fb", corpus_id="col_b")

        summary = repo.consolidate(source_ids=["col_a", "col_b"], target_id="col_target")

        assert summary == {
            "files_moved": 2,
            "chunks_moved": 2,
            "sources_moved": 2,
            "events_moved": 2,
            "claims_moved": 2,
            "grants_merged": 0,
        }
        assert _corpus_ids(pg_env, "corpus_files") == ["col_target", "col_target"]
        assert _corpus_ids(pg_env, "corpus_chunks") == ["col_target", "col_target"]
        assert _corpus_ids(pg_env, "corpus_file_sources") == ["col_target", "col_target"]
        assert _corpus_ids(pg_env, "corpus_file_events") == ["col_target", "col_target"]
        assert _corpus_ids(pg_env, "claims") == ["col_target", "col_target"]

    def test_soft_deletes_the_sources_and_leaves_the_target_live(self, pg_env, repo):
        _seed_collection(pg_env, "col_a")
        _seed_collection(pg_env, "col_target")

        repo.consolidate(source_ids=["col_a"], target_id="col_target")

        with pg_env.connect() as conn:
            source_deleted_at = conn.execute(sa.text("SELECT deleted_at FROM file_corpora WHERE id = 'col_a'")).scalar()
            target_deleted_at = conn.execute(
                sa.text("SELECT deleted_at FROM file_corpora WHERE id = 'col_target'")
            ).scalar()
        assert source_deleted_at is not None
        assert target_deleted_at is None

    def test_unions_resource_grants_keeping_the_targets_own_grant_on_overlap(self, pg_env, repo):
        _seed_collection(pg_env, "col_a")
        _seed_collection(pg_env, "col_b")
        _seed_collection(pg_env, "col_target")
        shared_group = _seed_group(pg_env, "sp-consolidate-shared")
        only_a_group = _seed_group(pg_env, "sp-consolidate-only-a")
        only_b_group = _seed_group(pg_env, "sp-consolidate-only-b")

        _grant(pg_env, group_id=shared_group, corpus_id="col_target", assigned_by="target-owner")
        _grant(pg_env, group_id=shared_group, corpus_id="col_a", assigned_by="source-owner")
        _grant(pg_env, group_id=only_a_group, corpus_id="col_a")
        _grant(pg_env, group_id=only_b_group, corpus_id="col_b")

        summary = repo.consolidate(source_ids=["col_a", "col_b"], target_id="col_target")
        assert summary["grants_merged"] == 2

        grants = _grants_for(pg_env, "col_target")
        by_group = {g["group_id"]: g["assigned_by"] for g in grants}
        assert set(by_group) == {shared_group, only_a_group, only_b_group}
        # The target's OWN pre-existing grant wins over the source's for the
        # overlapping group — never silently overwritten by the merge.
        assert by_group[shared_group] == "target-owner"

        assert _grants_for(pg_env, "col_a") == []
        assert _grants_for(pg_env, "col_b") == []

    def test_refuses_on_a_corpus_files_path_conflict_and_applies_nothing(self, pg_env, repo):
        from src.repositories.sharepoint_collection_consolidation_pg import ConsolidationConflict

        _seed_collection(pg_env, "col_a")
        _seed_collection(pg_env, "col_target")
        _seed_file(pg_env, file_id="fa", corpus_id="col_a", path="Reports/same.docx")
        _seed_file(pg_env, file_id="ft", corpus_id="col_target", path="Reports/same.docx")

        with pytest.raises(ConsolidationConflict) as excinfo:
            repo.consolidate(source_ids=["col_a"], target_id="col_target")
        assert excinfo.value.kind == "corpus_files.path"
        assert "Reports/same.docx" in excinfo.value.keys

        # Nothing moved — the transaction rolled back in full.
        assert _corpus_ids(pg_env, "corpus_files") == ["col_a", "col_target"]
        with pg_env.connect() as conn:
            deleted_at = conn.execute(sa.text("SELECT deleted_at FROM file_corpora WHERE id = 'col_a'")).scalar()
        assert deleted_at is None

    def test_refuses_on_a_corpus_file_sources_stable_id_conflict_and_applies_nothing(self, pg_env, repo):
        from src.repositories.sharepoint_collection_consolidation_pg import ConsolidationConflict

        _seed_collection(pg_env, "col_a")
        _seed_collection(pg_env, "col_target")
        _seed_file(pg_env, file_id="fa", corpus_id="col_a")
        _seed_file(pg_env, file_id="ft", corpus_id="col_target")
        _seed_source_mapping(pg_env, corpus_id="col_a", file_id="fa", stable_id="graph:dup")
        _seed_source_mapping(pg_env, corpus_id="col_target", file_id="ft", stable_id="graph:dup")

        with pytest.raises(ConsolidationConflict) as excinfo:
            repo.consolidate(source_ids=["col_a"], target_id="col_target")
        assert excinfo.value.kind == "corpus_file_sources.source_stable_id"
        assert "graph:dup" in excinfo.value.keys

        assert _corpus_ids(pg_env, "corpus_file_sources") == ["col_a", "col_target"]

    def test_dedups_fact_alias_sources_instead_of_failing_on_overlap(self, pg_env, repo):
        _seed_collection(pg_env, "col_a")
        _seed_collection(pg_env, "col_target")
        _seed_fact(pg_env, "fact_dup")
        # The SAME alias independently derived from BOTH the source and the
        # target's own evidence — this table is designed to hold both
        # provenance rows, so the merge must dedup, never fail.
        _seed_fact_alias_source(pg_env, fact_id="fact_dup", natural_key="acme-corp", corpus_id="col_a")
        _seed_fact_alias_source(pg_env, fact_id="fact_dup", natural_key="acme-corp", corpus_id="col_target")

        repo.consolidate(source_ids=["col_a"], target_id="col_target")

        with pg_env.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT corpus_id FROM fact_alias_sources WHERE type = 'company' AND natural_key = 'acme-corp'"
                    )
                )
                .scalars()
                .all()
            )
        assert rows == ["col_target"]
