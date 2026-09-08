"""Unit test for migration ``0113_corpus_chunks_tsv``'s row-count branch.

The migration always adds the ``corpus_chunks.tsv`` column (a nullable
column add is metadata-only, instant at any table size) and then either
backfills existing rows in place — at or under
``_IN_PLACE_BACKFILL_ROW_THRESHOLD`` — or SKIPS the backfill and logs the
operator follow-up (``scripts/backfill_corpus_chunks_tsv.py``). Same shape
as ``0101_corpus_chunks_fts_index``'s gate; see the migration's docstring
for why the column is not a ``GENERATED … STORED`` one and why the threshold
sits well under ``0101``'s.

Pure unit test — no real Postgres needed, same fake-``alembic.op`` approach
as ``tests/db_pg/test_corpus_chunks_fts_index_migration.py``. The real DDL
is separately exercised against Postgres by
``tests/db_pg/test_alembic_roundtrip.py`` (upgrade/downgrade over the whole
chain, where the seeded test table is always well under the threshold —
i.e. that suite only ever proves the "backfill in place" branch; this file is
what proves the "skip" branch actually skips) and the column's behavior by
``tests/db_pg/test_corpus_chunks_contract.py``.
"""

from __future__ import annotations

import importlib
import logging

MODULE_NAME = "migrations.versions.0113_corpus_chunks_tsv"


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeBind:
    def __init__(self, row_count: int) -> None:
        self.row_count = row_count
        self.executed: list[str] = []

    def execute(self, stmt, *args, **kwargs):
        self.executed.append(str(stmt))
        return _FakeResult(self.row_count)


class _FakeOp:
    def __init__(self, bind: _FakeBind) -> None:
        self._bind = bind
        self.executed: list[str] = []

    def get_bind(self):
        return self._bind

    def execute(self, stmt, *args, **kwargs):
        self.executed.append(str(stmt))


def _load_migration():
    return importlib.import_module(MODULE_NAME)


def _run_upgrade(monkeypatch, row_count: int):
    module = _load_migration()
    fake_op = _FakeOp(_FakeBind(row_count))
    monkeypatch.setattr(module, "op", fake_op)
    module.upgrade()
    return module, fake_op


def test_upgrade_adds_the_column_and_backfills_in_place_when_table_is_small(monkeypatch):
    module, fake_op = _run_upgrade(monkeypatch, row_count=100)

    add_column = [s for s in fake_op.executed if "ADD COLUMN" in s]
    assert len(add_column) == 1
    assert module.COLUMN_NAME in add_column[0] and "tsvector" in add_column[0]
    assert "GENERATED" not in add_column[0], "a generated STORED column would rewrite the whole table"

    backfill = [s for s in fake_op.executed if s.startswith("UPDATE corpus_chunks")]
    assert len(backfill) == 1
    assert "to_tsvector('simple', text)" in backfill[0]
    assert f"{module.COLUMN_NAME} IS NULL" in backfill[0], "the in-place backfill must be idempotent too"
    # Column first, then the backfill that reads it.
    assert fake_op.executed.index(add_column[0]) < fake_op.executed.index(backfill[0])


def test_upgrade_backfills_exactly_at_the_threshold(monkeypatch):
    """Boundary: the threshold row count itself still backfills in place."""
    _module, fake_op = _run_upgrade(monkeypatch, row_count=_load_migration()._IN_PLACE_BACKFILL_ROW_THRESHOLD)
    assert any(s.startswith("UPDATE corpus_chunks") for s in fake_op.executed)


def test_upgrade_adds_the_column_but_skips_the_backfill_and_warns_when_table_is_large(monkeypatch, caplog):
    module = _load_migration()
    with caplog.at_level(logging.WARNING):
        _, fake_op = _run_upgrade(monkeypatch, row_count=module._IN_PLACE_BACKFILL_ROW_THRESHOLD + 1)

    assert [s for s in fake_op.executed if "ADD COLUMN" in s], "the column itself always lands (metadata-only)"
    assert not any(s.startswith("UPDATE") for s in fake_op.executed), "the large-table branch must not rewrite rows"
    assert "scripts/backfill_corpus_chunks_tsv.py" in caplog.text
    assert "off-peak" in caplog.text


def test_in_place_threshold_sits_under_the_index_migrations_gate():
    """An UPDATE rewriting every row is heavier than an index build over the
    same rows (new heap version + every index maintained per row), so the
    backfill gate must be stricter than ``0101``'s index-build gate."""
    fts_index = importlib.import_module("migrations.versions.0101_corpus_chunks_fts_index")
    module = _load_migration()
    assert module._IN_PLACE_BACKFILL_ROW_THRESHOLD < fts_index._LARGE_TABLE_ROW_THRESHOLD


def test_downgrade_drops_the_column(monkeypatch):
    module = _load_migration()
    fake_op = _FakeOp(_FakeBind(row_count=0))
    monkeypatch.setattr(module, "op", fake_op)

    module.downgrade()

    assert any("DROP COLUMN" in s and module.COLUMN_NAME in s for s in fake_op.executed)
