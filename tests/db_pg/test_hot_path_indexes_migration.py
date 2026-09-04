"""Unit test for migration ``0106_hot_path_indexes``'s row-count branch.

``idx_corpus_chunks_corpus_id`` reuses ``0101_corpus_chunks_fts_index``'s
row-count gate — it's the same large, actively-written table — building in
place under the threshold and SKIPPING (logging the exact operator
follow-up statement) above it. ``idx_fact_aliases_natural_key`` is
unconditional, same as ``0098_corpus_chunks_file_id_index`` — ``fact_aliases``
is two orders of magnitude smaller, so it always builds regardless of the
``corpus_chunks`` row count.

Pure unit test — no real Postgres needed, same fake-``alembic.op`` approach as
``tests/db_pg/test_corpus_chunks_fts_index_migration.py``. The real SQL
syntax is separately exercised for real against Postgres by
``tests/db_pg/test_alembic_roundtrip.py`` (upgrade/downgrade over the whole
chain, where the seeded test table is always well under the threshold — i.e.
that suite only ever proves the "build" branch; this file is what proves the
"skip" branch actually skips).
"""

from __future__ import annotations

import importlib
import logging

MODULE_NAME = "migrations.versions.0106_hot_path_indexes"


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeBind:
    def __init__(self, row_count: int) -> None:
        self.row_count = row_count

    def execute(self, stmt, *args, **kwargs):
        return _FakeResult(self.row_count)


class _FakeOp:
    def __init__(self, bind: _FakeBind) -> None:
        self._bind = bind
        self.created_indexes: list[tuple] = []
        self.dropped_indexes: list[tuple] = []

    def get_bind(self):
        return self._bind

    def create_index(self, name, table_name, columns, **kwargs):
        self.created_indexes.append((name, table_name, tuple(columns)))

    def drop_index(self, name, table_name=None, **kwargs):
        self.dropped_indexes.append((name, table_name))


def _load_migration():
    return importlib.import_module(MODULE_NAME)


def test_upgrade_builds_both_indexes_when_table_is_small(monkeypatch):
    module = _load_migration()
    bind = _FakeBind(row_count=100)
    fake_op = _FakeOp(bind)
    monkeypatch.setattr(module, "op", fake_op)

    module.upgrade()

    names = [name for name, _table, _cols in fake_op.created_indexes]
    assert module.CORPUS_CHUNKS_INDEX_NAME in names
    assert module.FACT_ALIASES_INDEX_NAME in names
    assert (module.CORPUS_CHUNKS_INDEX_NAME, "corpus_chunks", ("corpus_id",)) in fake_op.created_indexes
    assert (module.FACT_ALIASES_INDEX_NAME, "fact_aliases", ("natural_key",)) in fake_op.created_indexes


def test_upgrade_builds_corpus_chunks_index_exactly_at_the_threshold(monkeypatch):
    """Boundary: the threshold row count itself still builds in place."""
    module = _load_migration()
    bind = _FakeBind(row_count=module._LARGE_TABLE_ROW_THRESHOLD)
    fake_op = _FakeOp(bind)
    monkeypatch.setattr(module, "op", fake_op)

    module.upgrade()

    names = [name for name, _table, _cols in fake_op.created_indexes]
    assert module.CORPUS_CHUNKS_INDEX_NAME in names


def test_upgrade_skips_corpus_chunks_index_but_still_builds_fact_aliases_when_table_is_large(monkeypatch, caplog):
    module = _load_migration()
    bind = _FakeBind(row_count=module._LARGE_TABLE_ROW_THRESHOLD + 1)
    fake_op = _FakeOp(bind)
    monkeypatch.setattr(module, "op", fake_op)

    with caplog.at_level(logging.WARNING):
        module.upgrade()

    names = [name for name, _table, _cols in fake_op.created_indexes]
    assert module.CORPUS_CHUNKS_INDEX_NAME not in names, "the large-table branch must not build the corpus_id index"
    assert module.FACT_ALIASES_INDEX_NAME in names, "fact_aliases is small — it must still build unconditionally"
    assert "CREATE INDEX CONCURRENTLY" in caplog.text
    assert module.CORPUS_CHUNKS_INDEX_NAME in caplog.text


def test_downgrade_drops_both_indexes(monkeypatch):
    module = _load_migration()
    bind = _FakeBind(row_count=0)
    fake_op = _FakeOp(bind)
    monkeypatch.setattr(module, "op", fake_op)

    module.downgrade()

    assert (module.FACT_ALIASES_INDEX_NAME, "fact_aliases") in fake_op.dropped_indexes
    assert (module.CORPUS_CHUNKS_INDEX_NAME, "corpus_chunks") in fake_op.dropped_indexes
