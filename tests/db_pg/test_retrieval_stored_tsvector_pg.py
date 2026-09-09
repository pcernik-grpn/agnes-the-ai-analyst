"""``src.ingest.retrieval.search_with_meta`` on the Postgres backend, before
and after the stored tsvector (migration ``0114_corpus_chunks_tsv``).

The contract this pins: the stored column is a CACHE of the ranking input,
never a different input. A small corpus returns byte-for-byte the same
results whether every candidate row has its ``tsv`` populated, none does
(the per-row ``COALESCE`` fallback — what every pre-migration row looks like
until the backfill reaches it), or only some do; and the candidate-cap
disclosure (``truncated`` / ``cap`` / ``SearchResults.capped``) fires
exactly as before on either path.

DuckDB has no such column, so this is PG-only — the backend-agnostic
retrieval tests live in ``tests/test_ingest_retrieval.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def pg_app_state(pg_engine, monkeypatch):
    """Bind every ``*_repo()`` factory to a Postgres at alembic head for the
    duration of one test (the factories decide per call from the env)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    from src import db_pg

    db_pg.dispose()
    try:
        yield db_pg.get_engine()
    finally:
        db_pg.dispose()


@pytest.fixture(autouse=True)
def _lexical_only(monkeypatch):
    """Pin the lexical-only path — the mode the stored vector matters for —
    so the test never depends on whether the embeddings extra is installed."""
    from src.ingest import retrieval

    monkeypatch.setattr(retrieval, "embed_query", lambda q: None)


def _seed_files(slug: str, files: list[tuple[str, list[str]]]) -> str:
    from src.repositories import corpus_chunks_repo, corpus_files_repo, file_corpora_repo

    cid = file_corpora_repo().create(name=slug, slug=slug, description=None, created_by="u")
    for filename, texts in files:
        fid = corpus_files_repo().add(
            corpus_id=cid, filename=filename, sha256="s", file_type="txt", size_bytes=1, storage_path="/x"
        )
        corpus_chunks_repo().add_many(
            [{"corpus_id": cid, "file_id": fid, "ordinal": i, "text": t} for i, t in enumerate(texts)]
        )
    return cid


def _null_out_tsv(engine, where: str = "TRUE") -> None:
    with engine.begin() as conn:
        conn.execute(sa.text(f"UPDATE corpus_chunks SET tsv = NULL WHERE {where}"))


def _stored_count(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(sa.text("SELECT COUNT(*) FROM corpus_chunks WHERE tsv IS NOT NULL")).scalar() or 0


def test_small_corpus_results_are_identical_with_and_without_the_stored_tsvector(pg_app_state):
    from src.ingest import retrieval

    cid = _seed_files(
        "tsv-small",
        [
            ("contracts.txt", ["contract renewal terms and the renewal window", "unrelated filler text"]),
            ("weather.txt", ["a weather report mentioning one contract"]),
            ("renewals.txt", ["renewal renewal renewal", "the contract is renewed yearly"]),
        ],
    )
    assert _stored_count(pg_app_state) == 5, "add_many stores the vector on every insert"

    stored = retrieval.search_with_meta([cid], "contract renewal")
    assert stored["results"], "sanity: the query matches"
    assert stored["truncated"] is False and stored["cap"] is None
    assert stored["results"][0]["text"] == "contract renewal terms and the renewal window"

    _null_out_tsv(pg_app_state, where="ordinal = 0")  # partially backfilled table
    partial = retrieval.search_with_meta([cid], "contract renewal")
    _null_out_tsv(pg_app_state)  # nothing backfilled at all
    assert _stored_count(pg_app_state) == 0
    fallback = retrieval.search_with_meta([cid], "contract renewal")

    assert stored == partial == fallback


def test_cap_disclosure_fires_the_same_on_both_ranking_paths(pg_app_state, monkeypatch):
    from src.ingest import retrieval

    monkeypatch.setattr(retrieval, "_max_candidate_chunks", lambda: 2)
    cid = _seed_files(
        "tsv-cap",
        [(f"f{i}.txt", [f"shared keyword contract number {i}"]) for i in range(5)],
    )

    stored = retrieval.search_with_meta([cid], "contract")
    assert (stored["truncated"], stored["cap"]) == (True, 2)
    assert 0 < len(stored["results"]) <= 2
    assert retrieval.search([cid], "contract").capped is True

    _null_out_tsv(pg_app_state)
    fallback = retrieval.search_with_meta([cid], "contract")
    assert (fallback["truncated"], fallback["cap"]) == (True, 2)
    assert retrieval.search([cid], "contract").capped is True

    # And a corpus under the cap is not reported as capped on either path.
    monkeypatch.setattr(retrieval, "_max_candidate_chunks", lambda: 100)
    assert retrieval.search_with_meta([cid], "contract")["truncated"] is False
