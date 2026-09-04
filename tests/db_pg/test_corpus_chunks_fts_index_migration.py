"""Unit test for migration ``0101_corpus_chunks_fts_index``'s row-count branch.

The migration builds the GIN full-text index in place on a small/lightly
loaded table, and SKIPS the build (logging the exact operator follow-up
statement) on a table over ``_LARGE_TABLE_ROW_THRESHOLD`` — see the
migration's own docstring for why (a GIN build holds a SHARE lock for
minutes on a 10M-row table, and ``CREATE INDEX CONCURRENTLY`` cannot run
inside the startup migration's owned transaction).

This is a pure unit test — no real Postgres needed. ``alembic.op`` is a
context-bound proxy that only works inside an active migration run, so a
fake stands in for both ``op.get_bind()`` (returning a fake connection whose
``.scalar()`` reports a controlled row count) and ``op.execute()`` (recording
the SQL text passed). The real SQL syntax is separately exercised for real
against Postgres by ``tests/db_pg/test_alembic_roundtrip.py`` (upgrade/
downgrade over the whole chain, where the seeded test table is always well
under the threshold — i.e. that suite only ever proves the "build" branch;
this file is what proves the "skip" branch actually skips.
"""

from __future__ import annotations

import importlib
import logging

MODULE_NAME = "migrations.versions.0101_corpus_chunks_fts_index"


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


def test_upgrade_builds_index_in_place_when_table_is_small(monkeypatch):
    module = _load_migration()
    bind = _FakeBind(row_count=100)
    fake_op = _FakeOp(bind)
    monkeypatch.setattr(module, "op", fake_op)

    module.upgrade()

    assert any("CREATE INDEX" in stmt for stmt in fake_op.executed)
    assert any(module.INDEX_NAME in stmt for stmt in fake_op.executed)
    assert any("gin" in stmt.lower() for stmt in fake_op.executed)
    assert not any("CONCURRENTLY" in stmt for stmt in fake_op.executed)


def test_upgrade_builds_index_exactly_at_the_threshold(monkeypatch):
    """Boundary: the threshold row count itself still builds in place."""
    module = _load_migration()
    bind = _FakeBind(row_count=module._LARGE_TABLE_ROW_THRESHOLD)
    fake_op = _FakeOp(bind)
    monkeypatch.setattr(module, "op", fake_op)

    module.upgrade()

    assert any(module.INDEX_NAME in stmt for stmt in fake_op.executed)


def test_upgrade_skips_build_and_warns_when_table_is_large(monkeypatch, caplog):
    module = _load_migration()
    bind = _FakeBind(row_count=module._LARGE_TABLE_ROW_THRESHOLD + 1)
    fake_op = _FakeOp(bind)
    monkeypatch.setattr(module, "op", fake_op)

    with caplog.at_level(logging.WARNING):
        module.upgrade()

    assert fake_op.executed == [], "the large-table branch must not issue any CREATE INDEX"
    assert "CREATE INDEX CONCURRENTLY" in caplog.text
    assert module.INDEX_NAME in caplog.text


def test_downgrade_drops_the_index(monkeypatch):
    module = _load_migration()
    bind = _FakeBind(row_count=0)
    fake_op = _FakeOp(bind)
    monkeypatch.setattr(module, "op", fake_op)

    module.downgrade()

    assert any("DROP INDEX" in stmt and module.INDEX_NAME in stmt for stmt in fake_op.executed)
