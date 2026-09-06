"""Postgres-only tests for ``sharepoint_crawl_items`` — the per-FILE
``ctag``/``failed_items``/``empty_items`` bookkeeping split out of
``sharepoint_connection_state.payload`` (migration
``0110_sharepoint_crawl_items``).

There is no DuckDB half to parametrize against (PG-first ratchet, A3) — see
``docs/migrations.md`` -> "Adding a PG-only feature". Pattern follows
``tests/db_pg/test_sharepoint_state_pg.py``.

The table is created from ``Base.metadata`` for the single model under test
rather than by running the whole Alembic ladder: this file is about the
repository's own contract; ``tests/db_pg/test_alembic_roundtrip.py`` already
owns "the migration and the model agree". The crawler-level integration
(``connectors/sharepoint/crawler.py``'s ``load_state``/``save_state``,
crash-consistency ordering, the legacy-blob migration) has its own tests in
``tests/db_pg/test_sharepoint_crawler_state_split_pg.py``.
"""

from __future__ import annotations


def _make_repo(pg_engine, monkeypatch):
    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg
    from src.models.sharepoint_state import SharepointCrawlItem

    db_pg.dispose()
    engine = db_pg.get_engine()
    SharepointCrawlItem.__table__.create(engine, checkfirst=True)

    from src.repositories.sharepoint_crawl_items_pg import SharepointCrawlItemsPgRepository

    return SharepointCrawlItemsPgRepository(engine)


_EMPTY_DELTA = {"set": {}, "removed": [], "reset": False}


def _delta(*, set_entries=None, removed=(), reset=False):
    return {"set": dict(set_entries or {}), "removed": list(removed), "reset": reset}


# ---------------------------------------------------------------------------
# get_all — on a never-touched connection
# ---------------------------------------------------------------------------


def test_get_all_on_a_never_seen_connection_is_three_empty_maps(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    assert repo.get_all("conn-new", "crawl") == {"ctags": {}, "failed_items": {}, "empty_items": {}}


def test_counts_on_a_never_seen_connection_is_zero(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    assert repo.counts("conn-new", "crawl") == {"failed_items_count": 0, "empty_items_count": 0}


# ---------------------------------------------------------------------------
# apply_delta — set / removed / reset, per field, independently
# ---------------------------------------------------------------------------


def test_apply_delta_sets_a_ctag_and_get_all_reflects_it(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    repo.apply_delta(
        "conn-a", "crawl", ctags=_delta(set_entries={"graph:1": "c1"}), failed=_EMPTY_DELTA, empty=_EMPTY_DELTA
    )
    assert repo.get_all("conn-a", "crawl") == {"ctags": {"graph:1": "c1"}, "failed_items": {}, "empty_items": {}}


def test_apply_delta_only_touches_the_stable_ids_it_names(pg_engine, monkeypatch):
    """The whole point of the split: a checkpoint that changes ONE file
    must not disturb any other file's row."""
    repo = _make_repo(pg_engine, monkeypatch)
    seed = {f"graph:{i}": f"c{i}" for i in range(50)}
    repo.apply_delta("conn-a", "crawl", ctags=_delta(set_entries=seed), failed=_EMPTY_DELTA, empty=_EMPTY_DELTA)

    repo.apply_delta(
        "conn-a", "crawl", ctags=_delta(set_entries={"graph:25": "changed"}), failed=_EMPTY_DELTA, empty=_EMPTY_DELTA
    )

    result = repo.get_all("conn-a", "crawl")["ctags"]
    expected = dict(seed)
    expected["graph:25"] = "changed"
    assert result == expected


def test_apply_delta_records_a_failed_entry_and_an_empty_entry_independently(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    repo.apply_delta(
        "conn-a",
        "crawl",
        ctags=_EMPTY_DELTA,
        failed=_delta(set_entries={"graph:1": {"attempts": 1, "error_class": "ingest_error"}}),
        empty=_delta(set_entries={"graph:2": {"first_seen_at": "t"}}),
    )
    items = repo.get_all("conn-a", "crawl")
    assert items["failed_items"] == {"graph:1": {"attempts": 1, "error_class": "ingest_error"}}
    assert items["empty_items"] == {"graph:2": {"first_seen_at": "t"}}
    assert repo.counts("conn-a", "crawl") == {"failed_items_count": 1, "empty_items_count": 1}


def test_a_success_can_clear_a_prior_failed_entry_via_removed(pg_engine, monkeypatch):
    """The same row that carries a ctag can also carry (and then clear) a
    failed/empty entry — a file's lifecycle, not three disjoint tables."""
    repo = _make_repo(pg_engine, monkeypatch)
    repo.apply_delta(
        "conn-a",
        "crawl",
        ctags=_EMPTY_DELTA,
        failed=_delta(set_entries={"graph:1": {"attempts": 1}}),
        empty=_EMPTY_DELTA,
    )
    repo.apply_delta(
        "conn-a",
        "crawl",
        ctags=_delta(set_entries={"graph:1": "c1"}),
        failed=_delta(removed=["graph:1"]),
        empty=_EMPTY_DELTA,
    )
    items = repo.get_all("conn-a", "crawl")
    assert items["ctags"] == {"graph:1": "c1"}
    assert items["failed_items"] == {}
    assert repo.counts("conn-a", "crawl") == {"failed_items_count": 0, "empty_items_count": 0}


def test_reset_wipes_only_its_own_column_for_every_row(pg_engine, monkeypatch):
    """The resync path: ``failed_items`` is wiped connection-wide, ``ctags``
    is untouched — same behaviour the old single-blob write always gave."""
    repo = _make_repo(pg_engine, monkeypatch)
    repo.apply_delta(
        "conn-a",
        "crawl",
        ctags=_delta(set_entries={"graph:1": "c1", "graph:2": "c2"}),
        failed=_delta(set_entries={"graph:1": {"attempts": 1}, "graph:2": {"attempts": 2}}),
        empty=_EMPTY_DELTA,
    )
    repo.apply_delta("conn-a", "crawl", ctags=_EMPTY_DELTA, failed=_delta(reset=True), empty=_EMPTY_DELTA)

    items = repo.get_all("conn-a", "crawl")
    assert items["ctags"] == {"graph:1": "c1", "graph:2": "c2"}
    assert items["failed_items"] == {}


def test_reset_with_set_entries_wipes_then_repopulates(pg_engine, monkeypatch):
    """The defensive general case: a plain-dict reassignment that is NOT
    empty still lands correctly — wipe, then write the new complete
    state."""
    repo = _make_repo(pg_engine, monkeypatch)
    repo.apply_delta(
        "conn-a", "crawl", ctags=_delta(set_entries={"graph:1": "stale"}), failed=_EMPTY_DELTA, empty=_EMPTY_DELTA
    )
    repo.apply_delta(
        "conn-a",
        "crawl",
        ctags=_delta(set_entries={"graph:9": "fresh"}, reset=True),
        failed=_EMPTY_DELTA,
        empty=_EMPTY_DELTA,
    )
    assert repo.get_all("conn-a", "crawl")["ctags"] == {"graph:9": "fresh"}


def test_apply_delta_is_a_no_op_write_when_every_field_is_empty(pg_engine, monkeypatch):
    """Calling with nothing to do must not create a placeholder row for a
    stable_id that was never named."""
    repo = _make_repo(pg_engine, monkeypatch)
    repo.apply_delta("conn-a", "crawl", ctags=_EMPTY_DELTA, failed=_EMPTY_DELTA, empty=_EMPTY_DELTA)
    assert repo.get_all("conn-a", "crawl") == {"ctags": {}, "failed_items": {}, "empty_items": {}}


# ---------------------------------------------------------------------------
# kind scoping — the shard seam
# ---------------------------------------------------------------------------


def test_crawl_and_shard_kinds_for_the_same_connection_are_independent(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    repo.apply_delta(
        "conn-a", "crawl", ctags=_delta(set_entries={"graph:parent": "p"}), failed=_EMPTY_DELTA, empty=_EMPTY_DELTA
    )
    repo.apply_delta(
        "conn-a",
        "crawl:b!drive1",
        ctags=_delta(set_entries={"graph:child": "c"}),
        failed=_EMPTY_DELTA,
        empty=_EMPTY_DELTA,
    )

    assert repo.get_all("conn-a", "crawl")["ctags"] == {"graph:parent": "p"}
    assert repo.get_all("conn-a", "crawl:b!drive1")["ctags"] == {"graph:child": "c"}


def test_counts_are_scoped_to_the_requested_kind(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    repo.apply_delta(
        "conn-a", "crawl", ctags=_EMPTY_DELTA, failed=_delta(set_entries={"graph:1": {}}), empty=_EMPTY_DELTA
    )
    repo.apply_delta(
        "conn-a",
        "crawl:b!drive1",
        ctags=_EMPTY_DELTA,
        failed=_delta(set_entries={"graph:1": {}, "graph:2": {}}),
        empty=_EMPTY_DELTA,
    )
    assert repo.counts("conn-a", "crawl") == {"failed_items_count": 1, "empty_items_count": 0}
