"""Postgres-backed tests for ``connectors.sharepoint.state_store`` — the
backend-selection seam ``connectors/sharepoint/crawler.py`` and
``connectors/sharepoint/facts_extraction.py`` both read/write their
per-connection state through.

Needs a REAL Postgres engine (unlike ``tests/test_sharepoint_crawler.py`` /
``tests/test_facts_extraction.py``, which only ever exercise the DuckDB
fallback file store) AND a ``DATA_DIR`` (the one-time legacy-file import
reads the pre-existing JSON file from disk). Runs the full Alembic ladder
via ``pg_env`` (mirrors ``tests/db_pg/test_facts_extraction_pg.py``) so the
table exists exactly as a real deploy would create it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def pg_env(tmp_path, monkeypatch, pg_engine):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("STATE_DIR", raising=False)
    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()
    return pg_engine


def test_use_pg_is_true_in_this_fixture(pg_env):
    from src.repositories import use_pg

    assert use_pg() is True


def test_get_on_a_never_seen_connection_returns_none(pg_env):
    from connectors.sharepoint import state_store

    assert state_store.get("crawl", "conn-new") is None


def test_put_then_get_round_trips_through_postgres(pg_env):
    from connectors.sharepoint import state_store

    state_store.put("crawl", "conn-a", {"delta_links": {"d": "u"}})
    assert state_store.get("crawl", "conn-a") == {"delta_links": {"d": "u"}}


def test_crawl_and_facts_kinds_never_collide(pg_env):
    from connectors.sharepoint import state_store

    state_store.put("crawl", "conn-a", {"delta_links": {"x": "1"}})
    state_store.put("facts", "conn-a", {"docs": {"f1": {"status": "done"}}})
    assert state_store.get("crawl", "conn-a") == {"delta_links": {"x": "1"}}
    assert state_store.get("facts", "conn-a") == {"docs": {"f1": {"status": "done"}}}


# ---------------------------------------------------------------------------
# One-time legacy-file import
# ---------------------------------------------------------------------------


def test_a_legacy_file_is_imported_on_first_read(pg_env):
    from connectors.sharepoint import state_store

    legacy_path = state_store.file_state_path("crawl", "conn-legacy")
    legacy_path.write_text(json.dumps({"delta_links": {"drive1": "https://example/delta?token=abc"}}))

    result = state_store.get("crawl", "conn-legacy")

    assert result == {"delta_links": {"drive1": "https://example/delta?token=abc"}}
    # And it actually landed in Postgres — not just returned in-memory.
    from src.repositories import sharepoint_state_repo

    assert sharepoint_state_repo().get("conn-legacy", "crawl") == {
        "delta_links": {"drive1": "https://example/delta?token=abc"}
    }


def test_the_legacy_file_is_left_in_place_after_import(pg_env):
    """An existing instance upgrading to Postgres must not lose its file as
    a side effect of the one-time import — see the module docstring."""
    from connectors.sharepoint import state_store

    legacy_path = state_store.file_state_path("crawl", "conn-legacy")
    legacy_path.write_text(json.dumps({"delta_links": {"x": "1"}}))

    state_store.get("crawl", "conn-legacy")

    assert legacy_path.exists()
    assert json.loads(legacy_path.read_text()) == {"delta_links": {"x": "1"}}


def test_a_second_import_is_a_no_op_once_the_row_exists(pg_env):
    """Simulates the file staying on disk (it is never deleted) while
    Postgres keeps advancing — a later read must never rewind state back
    to the stale file's snapshot."""
    from connectors.sharepoint import state_store

    legacy_path = state_store.file_state_path("crawl", "conn-legacy")
    legacy_path.write_text(json.dumps({"delta_links": {"stale": "1"}}))

    state_store.get("crawl", "conn-legacy")  # first read imports
    state_store.put("crawl", "conn-legacy", {"delta_links": {"advanced": "2"}})  # a later checkpoint

    # The file on disk is still the stale snapshot (never rewritten), but a
    # read must resolve from Postgres now that a row exists.
    assert state_store.get("crawl", "conn-legacy") == {"delta_links": {"advanced": "2"}}


def test_no_legacy_file_and_no_row_is_a_clean_none(pg_env):
    from connectors.sharepoint import state_store

    assert state_store.get("crawl", "conn-brand-new") is None


# ---------------------------------------------------------------------------
# facts_pass_lock through the seam
# ---------------------------------------------------------------------------


def test_facts_pass_lock_dispatches_to_postgres_and_refuses_a_second_attempt(pg_env):
    from connectors.sharepoint.state_store import FactsPassLocked, facts_pass_lock

    with facts_pass_lock("conn-a"):
        with pytest.raises(FactsPassLocked):
            with facts_pass_lock("conn-a"):
                pass


def test_facts_pass_lock_releases_after_the_block(pg_env):
    from connectors.sharepoint.state_store import facts_pass_lock

    with facts_pass_lock("conn-a"):
        pass
    with facts_pass_lock("conn-a"):
        pass  # must not raise


# ---------------------------------------------------------------------------
# End-to-end through the crawler's / facts_extraction's own load_state /
# save_state — the actual call sites, not the seam directly.
# ---------------------------------------------------------------------------


def test_crawler_load_state_imports_a_legacy_file_through_postgres(pg_env):
    from connectors.sharepoint import crawler

    legacy_path = crawler.state_path("conn-legacy")
    legacy_path.write_text(json.dumps({"delta_links": {"d": "u"}, "ctags": {"g:1": "c"}}))

    state = crawler.load_state("conn-legacy")

    assert state["delta_links"] == {"d": "u"}
    assert state["ctags"] == {"g:1": "c"}
    assert state["failed_items"] == {}  # default applied on top of the imported payload


def test_crawler_save_state_then_load_state_round_trips_through_postgres(pg_env):
    from connectors.sharepoint import crawler

    crawler.save_state("conn-a", {"delta_links": {"d": "u"}, "ctags": {"g:1": "c"}, "failed_items": {}})
    state = crawler.load_state("conn-a")
    assert state["delta_links"] == {"d": "u"}
    assert state["ctags"] == {"g:1": "c"}


def test_facts_extraction_load_state_imports_a_legacy_file_through_postgres(pg_env):
    from connectors.sharepoint import facts_extraction

    legacy_path = facts_extraction.state_path("conn-legacy")
    legacy_path.write_text(json.dumps({"version": 1, "docs": {"doc1": {"status": "done"}}}))

    state = facts_extraction.load_state("conn-legacy")

    assert state["docs"] == {"doc1": {"status": "done"}}


def test_facts_extraction_save_state_then_load_state_round_trips_through_postgres(pg_env):
    from connectors.sharepoint import facts_extraction

    facts_extraction.save_state("conn-a", {"version": 1, "docs": {"doc1": {"status": "done"}}})
    state = facts_extraction.load_state("conn-a")
    assert state["docs"] == {"doc1": {"status": "done"}}
