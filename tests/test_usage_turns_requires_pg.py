"""``usage_turns`` is Postgres-only (A3 PG-first ratchet) — prove it fails
CLEAN on a DuckDB-backed instance.

CLAUDE.md -> "Dual-backend discipline": a repository added after the freeze
registers ``PG``-only, and resolving it while the active backend is DuckDB
must raise the typed ``RequiresPostgresBackend`` — which ``app/main.py``
translates into a ``501 requires_postgres_backend`` — never a bare
``KeyError`` surfacing as an unhandled 500. The mechanism itself is pinned by
``tests/test_requires_postgres_backend.py``; this file pins that
``usage_turns`` actually uses it, and that no DuckDB sibling module or
registry backend crept in.

No route exercises the repo yet (the per-turn writers land in later tasks of
the same plan), so there is nothing to add to the parity sweeps'
``_PG_ONLY_ROUTE_EXEMPTIONS`` — the factory call IS the whole reachable
surface today.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_usage_turns_repo_raises_requires_postgres_backend_on_duckdb(monkeypatch):
    import src.repositories as factory

    monkeypatch.setattr(factory, "_active_backend", lambda: factory.DUCKDB)

    with pytest.raises(factory.RequiresPostgresBackend) as exc_info:
        factory.usage_turns_repo()
    assert exc_info.value.feature == "usage_turns"
    assert "Postgres" in str(exc_info.value)


def test_usage_turns_is_registered_postgres_only():
    import src.repositories as factory

    entry = factory._REGISTRY["usage_turns"]
    assert factory.DUCKDB not in entry
    assert entry[factory.PG] == ("src.repositories.usage_turns_pg", "UsageTurnsPgRepository")


def test_there_is_no_duckdb_sibling_module():
    """The A3 ratchet forbids a DuckDB half; ``tests/db_pg/
    test_repo_module_pg_first_ratchet.py`` would flag it repo-wide, but a
    local assertion names the offender at the source."""
    assert not (REPO_ROOT / "src" / "repositories" / "usage_turns.py").exists()


def test_the_factory_is_exported():
    """Callers reach repositories through the factory, never by
    instantiating the class (``tests/test_backend_split_guard.py``)."""
    import src.repositories as factory

    assert "usage_turns_repo" in factory.__all__
