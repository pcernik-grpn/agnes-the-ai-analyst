"""``scripts/backfill_corpus_chunks_tsv.py`` against a real Postgres.

The out-of-band half of migration ``0114_corpus_chunks_tsv`` (the in-place
half is the migration's own ``UPDATE``, proven by
``test_corpus_chunks_tsv_migration.py``): on a table too large to rewrite
at startup, this script populates ``tsv`` in keyset-paginated batches, one
short transaction each, touching only rows whose vector is still NULL.
"""

from __future__ import annotations

import logging

import sqlalchemy as sa

from scripts.backfill_corpus_chunks_tsv import backfill, main


def _insert_raw(engine, rows):
    """Insert chunk rows the way a PRE-migration writer would have — without
    ``tsv`` — so the column is NULL exactly like rows that predate 0113."""
    with engine.begin() as conn:
        for i, (chunk_id, text) in enumerate(rows):
            conn.execute(
                sa.text(
                    "INSERT INTO corpus_chunks (id, corpus_id, file_id, ordinal, text) "
                    "VALUES (:id, 'col_bf', 'cf_bf', :ordinal, :text)"
                ),
                {"id": chunk_id, "ordinal": i, "text": text},
            )


def _stored_vs_fresh(engine):
    with engine.connect() as conn:
        return conn.execute(
            sa.text(
                "SELECT id, tsv::text AS stored, to_tsvector('simple', text)::text AS fresh "
                "FROM corpus_chunks ORDER BY id"
            )
        ).all()


def test_backfill_populates_null_rows_in_batches_and_is_idempotent(pg_engine_with_schema):
    engine = pg_engine_with_schema
    _insert_raw(engine, [(f"ck_{i:03d}", f"contract renewal number {i}") for i in range(7)])
    # One row already carries a vector (written after the column landed, or
    # by an earlier interrupted run): the backfill must leave it alone.
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE corpus_chunks SET tsv = to_tsvector('simple', 'already stored') WHERE id = 'ck_003'")
        )

    updated = backfill(engine, batch_size=2)  # 6 NULL rows over 3 batches of 2

    assert updated == 6
    rows = _stored_vs_fresh(engine)
    assert len(rows) == 7
    for row in rows:
        if row.id == "ck_003":
            assert row.stored == "'already':1 'stored':2", "a populated row is never rewritten"
        else:
            assert row.stored == row.fresh and row.stored

    # Idempotent: a second run finds nothing to do.
    assert backfill(engine, batch_size=2) == 0


def test_backfill_skips_a_null_text_row_instead_of_rewriting_or_looping_on_it(pg_engine_with_schema):
    """``to_tsvector('simple', NULL)`` is NULL, so a chunk with no text can
    never gain a vector — it must be neither rewritten (a no-op heap churn
    that would still count as "updated") nor re-selected as "still NULL" on
    every batch."""
    engine = pg_engine_with_schema
    _insert_raw(engine, [("ck_a", "alpha"), ("ck_b", None), ("ck_c", "gamma")])

    updated = backfill(engine, batch_size=1)

    assert updated == 2
    stored = {row.id: row.stored for row in _stored_vs_fresh(engine)}
    assert stored["ck_b"] is None
    assert stored["ck_a"] == "'alpha':1" and stored["ck_c"] == "'gamma':1"


def test_backfill_rejects_a_non_positive_batch_size_and_a_negative_sleep(pg_engine_with_schema):
    import pytest

    with pytest.raises(ValueError):
        backfill(pg_engine_with_schema, batch_size=0)
    with pytest.raises(ValueError):
        backfill(pg_engine_with_schema, sleep_seconds=-1)


def test_main_reports_an_unconfigured_postgres_and_exits_2(monkeypatch, caplog):
    """The script resolves the database the way the app does (instance.yaml
    first, then the env vars — ``src.db_pg._resolve_url``), so it must not
    gate on the env vars itself; when nothing is configured it relays the
    resolver's own message and exits 2."""
    from src import db_pg

    def _unset():
        raise RuntimeError("Postgres URL is unset: set instance.yaml::database.url ... or set DATABASE_URL env var")

    monkeypatch.setattr(db_pg, "get_engine", _unset)
    with caplog.at_level(logging.ERROR):
        assert main([]) == 2
    assert "Postgres URL is unset" in caplog.text


def test_main_rejects_bad_operator_input_before_touching_the_database(monkeypatch):
    import pytest

    from src import db_pg

    monkeypatch.setattr(db_pg, "get_engine", lambda: (_ for _ in ()).throw(AssertionError("must not be called")))
    with pytest.raises(SystemExit):
        main(["--sleep", "-1"])
    with pytest.raises(SystemExit):
        main(["--batch-size", "0"])
