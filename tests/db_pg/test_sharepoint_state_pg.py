"""Postgres-only tests for the ``sharepoint_connection_state`` repository —
per-connection crawl/facts bookkeeping moved off the worker's local disk
(horizontal-scale extraction workers).

There is no DuckDB half to parametrize against (PG-first ratchet, A3) — see
``docs/migrations.md`` -> "Adding a PG-only feature". Pattern follows
``tests/db_pg/test_extraction_runs_pg.py``.

The table is created from ``Base.metadata`` for the single model under test
rather than by running the whole Alembic ladder: this file is about the
repository's own contract; ``tests/db_pg/test_alembic_roundtrip.py`` already
owns "the migration and the model agree".

The backend-selection seam (``connectors.sharepoint.state_store``) — the
one-time legacy-file import and the DuckDB fallback lock — has its own
tests in ``tests/db_pg/test_sharepoint_state_store_pg.py`` (needs both a
real Postgres engine AND a ``DATA_DIR``) and in
``tests/test_sharepoint_crawler.py`` / ``tests/test_facts_extraction.py``
(the DuckDB-only fallback half).
"""

from __future__ import annotations


def _make_repo(pg_engine, monkeypatch):
    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg
    from src.models.sharepoint_state import SharepointConnectionState

    db_pg.dispose()
    engine = db_pg.get_engine()
    SharepointConnectionState.__table__.create(engine, checkfirst=True)

    from src.repositories.sharepoint_state_pg import SharepointStatePgRepository

    return SharepointStatePgRepository(engine)


# ---------------------------------------------------------------------------
# get / put / delete
# ---------------------------------------------------------------------------


def test_get_on_a_never_seen_connection_returns_none(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    assert repo.get("conn-new", "crawl") is None


def test_put_then_get_round_trips_the_payload(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    repo.put("conn-a", "crawl", {"delta_links": {"d": "u"}, "ctags": {"graph:1": "c"}, "failed_items": {}})
    row = repo.get("conn-a", "crawl")
    assert row == {"delta_links": {"d": "u"}, "ctags": {"graph:1": "c"}, "failed_items": {}}


def test_put_is_an_upsert(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    repo.put("conn-a", "crawl", {"ctags": {"g:1": "a"}})
    repo.put("conn-a", "crawl", {"ctags": {"g:1": "b"}})
    assert repo.get("conn-a", "crawl") == {"ctags": {"g:1": "b"}}


def test_crawl_and_facts_state_for_the_same_connection_are_independent_rows(pg_engine, monkeypatch):
    """A corrupt/overwritten facts row must never disturb the crawl row for
    the SAME connection, and vice versa — the whole reason this is
    ``kind``-keyed rather than one JSON blob."""
    repo = _make_repo(pg_engine, monkeypatch)
    repo.put("conn-a", "crawl", {"delta_links": {"x": "1"}})
    repo.put("conn-a", "facts", {"docs": {"f1": {"status": "done"}}})
    assert repo.get("conn-a", "crawl") == {"delta_links": {"x": "1"}}
    assert repo.get("conn-a", "facts") == {"docs": {"f1": {"status": "done"}}}

    repo.delete("conn-a", "facts")
    assert repo.get("conn-a", "facts") is None
    assert repo.get("conn-a", "crawl") == {"delta_links": {"x": "1"}}


def test_an_invalid_kind_is_refused_by_the_check_constraint(pg_engine, monkeypatch):
    import sqlalchemy as sa

    repo = _make_repo(pg_engine, monkeypatch)
    try:
        repo.put("conn-a", "bogus", {})
    except sa.exc.IntegrityError:
        pass
    else:
        raise AssertionError("expected the CHECK constraint on `kind` to refuse an unknown value")


def test_delete_on_an_absent_row_is_a_silent_no_op(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    repo.delete("conn-never-existed", "crawl")  # must not raise


# ---------------------------------------------------------------------------
# backlog_counts — cheap size-only read for the Retry buttons' N
# ---------------------------------------------------------------------------


def test_backlog_counts_on_a_never_seen_connection_is_zero(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    assert repo.backlog_counts("conn-new", "crawl") == {"failed_items_count": 0, "empty_items_count": 0}


def test_backlog_counts_reflects_the_stored_backlogs(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    repo.put(
        "conn-a",
        "crawl",
        {
            "failed_items": {"graph:1": {"item": {}}, "graph:2": {"item": {}}, "graph:3": {"item": {}}},
            "empty_items": {"graph:4": {"item": {}}},
        },
    )
    assert repo.backlog_counts("conn-a", "crawl") == {"failed_items_count": 3, "empty_items_count": 1}


def test_backlog_counts_a_row_missing_one_of_the_two_keys_reads_that_one_as_zero(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    repo.put("conn-a", "crawl", {"delta_links": {}, "ctags": {}})
    assert repo.backlog_counts("conn-a", "crawl") == {"failed_items_count": 0, "empty_items_count": 0}


def test_backlog_counts_is_scoped_to_the_requested_kind(pg_engine, monkeypatch):
    """A connection's `facts` row must never leak into the `crawl` backlog
    count — the two kinds are independent rows for a reason."""
    repo = _make_repo(pg_engine, monkeypatch)
    repo.put("conn-a", "crawl", {"failed_items": {"graph:1": {}}})
    repo.put("conn-a", "facts", {"failed_items": {"graph:1": {}, "graph:2": {}}})
    assert repo.backlog_counts("conn-a", "crawl") == {"failed_items_count": 1, "empty_items_count": 0}


# ---------------------------------------------------------------------------
# import_if_absent — the one-time legacy-file import primitive
# ---------------------------------------------------------------------------


def test_import_if_absent_inserts_when_no_row_exists(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    result = repo.import_if_absent("conn-a", "crawl", {"delta_links": {"legacy": "1"}})
    assert result == {"delta_links": {"legacy": "1"}}
    assert repo.get("conn-a", "crawl") == {"delta_links": {"legacy": "1"}}


def test_import_if_absent_never_clobbers_an_existing_row(pg_engine, monkeypatch):
    """A connection already advancing on Postgres must never be rewound by
    a stale legacy-file snapshot — e.g. a second worker importing the same
    file after the first worker already wrote a newer checkpoint."""
    repo = _make_repo(pg_engine, monkeypatch)
    repo.put("conn-a", "crawl", {"delta_links": {"already": "advanced"}})
    result = repo.import_if_absent("conn-a", "crawl", {"delta_links": {"stale": "legacy"}})
    assert result == {"delta_links": {"already": "advanced"}}
    assert repo.get("conn-a", "crawl") == {"delta_links": {"already": "advanced"}}


def test_import_if_absent_is_idempotent(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    first = repo.import_if_absent("conn-a", "crawl", {"delta_links": {"x": "1"}})
    second = repo.import_if_absent("conn-a", "crawl", {"delta_links": {"y": "2"}})
    assert first == second == {"delta_links": {"x": "1"}}


# ---------------------------------------------------------------------------
# facts_pass_lock — per-connection advisory lock
# ---------------------------------------------------------------------------


def test_a_second_attempt_on_the_same_connection_is_refused(pg_engine, monkeypatch):
    from src.repositories.sharepoint_state_pg import FactsPassLocked

    repo = _make_repo(pg_engine, monkeypatch)
    with repo.facts_pass_lock("conn-a"):
        try:
            with repo.facts_pass_lock("conn-a"):
                pass
        except FactsPassLocked as exc:
            assert "conn-a" in str(exc)
        else:
            raise AssertionError("expected FactsPassLocked on the second concurrent attempt")


def test_a_different_connection_is_never_blocked(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    with repo.facts_pass_lock("conn-a"):
        with repo.facts_pass_lock("conn-b"):
            pass  # must not raise — different connection, independent key


def test_the_lock_releases_on_a_clean_exit_so_a_later_attempt_succeeds(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    with repo.facts_pass_lock("conn-a"):
        pass
    with repo.facts_pass_lock("conn-a"):
        pass  # must not raise — the first block already released it


def test_the_lock_releases_even_when_the_held_block_raises(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    try:
        with repo.facts_pass_lock("conn-a"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    with repo.facts_pass_lock("conn-a"):
        pass  # must not raise — a crash inside the pass must not wedge the lock


def test_the_lock_is_visible_from_a_second_engine_connection(pg_engine, monkeypatch):
    """The lock must serialize across CONNECTIONS (i.e. across worker
    processes), not just within one Python object — this is what makes it
    useful across the horizontally-scaled extraction workers the whole
    change exists for."""
    from src.repositories.sharepoint_state_pg import FactsPassLocked, SharepointStatePgRepository

    repo_a = _make_repo(pg_engine, monkeypatch)
    import src.db_pg as db_pg

    repo_b = SharepointStatePgRepository(db_pg.get_engine())

    with repo_a.facts_pass_lock("conn-shared"):
        try:
            with repo_b.facts_pass_lock("conn-shared"):
                pass
        except FactsPassLocked:
            pass
        else:
            raise AssertionError("expected the second repo instance to see the first's held lock")


# ---------------------------------------------------------------------------
# facts_pass_lock partitions + any_facts_pass_running (TCRD-296 gap #67)
# ---------------------------------------------------------------------------


def test_distinct_partitions_of_the_same_connection_never_contend(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    with repo.facts_pass_lock("conn-a", partition=(0, 4)):
        with repo.facts_pass_lock("conn-a", partition=(1, 4)):
            pass  # must not raise


def test_the_same_partition_index_is_refused_twice(pg_engine, monkeypatch):
    from src.repositories.sharepoint_state_pg import FactsPassLocked

    repo = _make_repo(pg_engine, monkeypatch)
    with repo.facts_pass_lock("conn-a", partition=(2, 4)):
        try:
            with repo.facts_pass_lock("conn-a", partition=(2, 4)):
                pass
        except FactsPassLocked:
            pass
        else:
            raise AssertionError("expected FactsPassLocked on the same partition twice")


def test_a_partition_lock_does_not_block_a_different_connections_same_index(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    with repo.facts_pass_lock("conn-a", partition=(0, 4)):
        with repo.facts_pass_lock("conn-b", partition=(0, 4)):
            pass  # must not raise — the classid is derived per-connection


def test_any_facts_pass_running_sees_the_legacy_whole_connection_lock(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    assert repo.any_facts_pass_running("conn-a") is False
    with repo.facts_pass_lock("conn-a"):
        assert repo.any_facts_pass_running("conn-a") is True
    assert repo.any_facts_pass_running("conn-a") is False


def test_any_facts_pass_running_sees_any_held_partition(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    with repo.facts_pass_lock("conn-a", partition=(3, 8)):
        assert repo.any_facts_pass_running("conn-a") is True
        assert repo.any_facts_pass_running("conn-b") is False
    assert repo.any_facts_pass_running("conn-a") is False


# ---------------------------------------------------------------------------
# merge_docs — per-document ledger merge (TCRD-296 gap #67)
# ---------------------------------------------------------------------------


def test_merge_docs_creates_the_row_on_first_write(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    repo.merge_docs("conn-a", "facts", set_entries={"cf_1": {"status": "done"}}, removed=[])
    assert repo.get("conn-a", "facts") == {"version": 1, "docs": {"cf_1": {"status": "done"}}}


def test_merge_docs_does_not_touch_other_top_level_fields(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    repo.put("conn-a", "facts", {"version": 1, "docs": {}, "facts_continuation_chain": 3})
    repo.merge_docs("conn-a", "facts", set_entries={"cf_1": {"status": "done"}}, removed=[])
    stored = repo.get("conn-a", "facts")
    assert stored["facts_continuation_chain"] == 3
    assert stored["docs"] == {"cf_1": {"status": "done"}}


def test_merge_docs_two_calls_for_different_keys_both_survive(pg_engine, monkeypatch):
    """The crux: a naive whole-payload overwrite would lose whichever call
    ran first once the second call's stale in-memory snapshot re-saved the
    whole payload without it — `merge_docs` never re-reads-then-overwrites
    the OTHER key at all."""
    repo = _make_repo(pg_engine, monkeypatch)
    repo.merge_docs("conn-a", "facts", set_entries={"cf_1": {"status": "done"}}, removed=[])
    repo.merge_docs("conn-a", "facts", set_entries={"cf_2": {"status": "done"}}, removed=[])
    docs = repo.get("conn-a", "facts")["docs"]
    assert docs == {"cf_1": {"status": "done"}, "cf_2": {"status": "done"}}


def test_merge_docs_removed_key_leaves_siblings_untouched(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    repo.merge_docs("conn-a", "facts", set_entries={"cf_1": {"status": "done"}, "cf_2": {"status": "done"}}, removed=[])
    repo.merge_docs("conn-a", "facts", set_entries={}, removed=["cf_1"])
    docs = repo.get("conn-a", "facts")["docs"]
    assert docs == {"cf_2": {"status": "done"}}


def test_merge_docs_is_a_no_op_with_nothing_to_write(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    repo.merge_docs("conn-never-touched", "facts", set_entries={}, removed=[])
    assert repo.get("conn-never-touched", "facts") is None
