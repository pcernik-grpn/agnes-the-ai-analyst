"""A3 PG-first ratchet — DuckDB schema-version freeze.

CLAUDE.md -> "Dual-backend discipline": the DuckDB app-state migration ladder
(``src/db.py``, ``SCHEMA_VERSION`` / ``_ensure_schema``) is frozen. New schema
work lands as an Alembic-only revision (``migrations/versions/``), with no
matching ``_vN_to_v(N+1)`` step here — see ``docs/migrations.md`` -> "Adding
a PG-only feature". This is the DuckDB-schema-side complement to the
registry-level ratchets in ``tests/test_repository_registry_pg_first_ratchet.py``
and ``tests/db_pg/test_repo_module_pg_first_ratchet.py``.

A4 deletes the whole DuckDB ladder (and this test) once the fleet has
migrated off the DuckDB app-state backend.
"""

from __future__ import annotations

import pytest

import src.db as db_module
from src.db import FROZEN_DUCKDB_SCHEMA_VERSION, SCHEMA_VERSION


def test_duckdb_schema_version_is_frozen():
    assert SCHEMA_VERSION == FROZEN_DUCKDB_SCHEMA_VERSION, (
        f"SCHEMA_VERSION moved to {SCHEMA_VERSION}, past the A3 freeze at "
        f"{FROZEN_DUCKDB_SCHEMA_VERSION} -- the DuckDB app-state ladder is "
        "frozen; new schema work is Alembic-only (see docs/migrations.md -> "
        "'Adding a PG-only feature'), not a new `_vN_to_v(N+1)` step here."
    )


def test_frozen_constant_is_the_literal_124():
    """Pin the literal value (not just "equal to SCHEMA_VERSION") so a
    drive-by edit of the constant itself shows up as a diff a reviewer must
    consciously approve, rather than two numbers silently moving together."""
    assert FROZEN_DUCKDB_SCHEMA_VERSION == 124


def test_meta_detects_a_planted_version_bump(monkeypatch):
    """Negative control: proves the freeze assertion actually fires when
    SCHEMA_VERSION advances past the frozen constant, rather than passing
    vacuously because both sides happen to read the same value."""
    monkeypatch.setattr(db_module, "SCHEMA_VERSION", db_module.FROZEN_DUCKDB_SCHEMA_VERSION + 1)
    with pytest.raises(AssertionError):
        assert db_module.SCHEMA_VERSION == db_module.FROZEN_DUCKDB_SCHEMA_VERSION
