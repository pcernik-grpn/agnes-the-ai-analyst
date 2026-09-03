"""Postgres-only tests for :mod:`src.repositories.sharepoint_connection_merge_pg`
— the crawl/facts per-connection state union behind
``POST /api/admin/sharepoint/connections/{id}/splits/merge`` (folding
several sibling SharePoint connections, each holding its own slice of a
manually split site, back into one).

There is no DuckDB half to parametrize against (PG-first ratchet, A3) —
``sharepoint_connection_state`` itself has no DuckDB sibling. Pattern
follows ``tests/db_pg/test_sharepoint_state_pg.py``. The ROUTE's own
orchestration (scope moves, collection folding, running-job refusal) is
covered by ``tests/db_pg/test_sharepoint_connection_split_merge_route_pg.py``;
this file proves the state-union algorithm itself — disjoint union, the
collision tie-breaks, and that ``plan()``/``apply()`` agree.
"""

from __future__ import annotations


def _make_repo(pg_engine, monkeypatch):
    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg
    from src.models.sharepoint_state import SharepointConnectionState

    db_pg.dispose()
    engine = db_pg.get_engine()
    SharepointConnectionState.__table__.create(engine, checkfirst=True)

    from src.repositories.sharepoint_connection_merge_pg import SharePointConnectionMergePgRepository
    from src.repositories.sharepoint_state_pg import SharepointStatePgRepository

    return SharePointConnectionMergePgRepository(engine), SharepointStatePgRepository(engine)


# ---------------------------------------------------------------------------
# Disjoint union — the expected case for a properly split site
# ---------------------------------------------------------------------------


def test_plan_unions_disjoint_delta_links_and_ctags_with_no_conflicts(pg_engine, monkeypatch):
    merge_repo, state_repo = _make_repo(pg_engine, monkeypatch)
    state_repo.put("target", "crawl", {"delta_links": {"drive:root": "u0"}, "ctags": {"graph:1": "c1"}})
    state_repo.put("sib-1", "crawl", {"delta_links": {"drive:a": "u1"}, "ctags": {"graph:2": "c2"}})

    diag = merge_repo.plan(target_id="target", sibling_ids=["sib-1"])
    assert diag["sib-1"]["crawl"]["delta_links_carried"] == 1
    assert diag["sib-1"]["crawl"]["ctags_carried"] == 1
    assert diag["sib-1"]["crawl"]["conflicts"] == []

    # A pure preview — nothing written.
    assert state_repo.get("target", "crawl") == {
        "delta_links": {"drive:root": "u0"},
        "ctags": {"graph:1": "c1"},
    }


def test_apply_writes_the_union_onto_the_target_and_leaves_siblings_untouched(pg_engine, monkeypatch):
    merge_repo, state_repo = _make_repo(pg_engine, monkeypatch)
    state_repo.put("target", "crawl", {"delta_links": {"drive:root": "u0"}})
    state_repo.put("sib-1", "crawl", {"delta_links": {"drive:a": "u1"}})
    state_repo.put("sib-2", "crawl", {"delta_links": {"drive:b": "u2"}})

    merge_repo.apply(target_id="target", sibling_ids=["sib-1", "sib-2"])

    assert state_repo.get("target", "crawl") == {
        "delta_links": {"drive:root": "u0", "drive:a": "u1", "drive:b": "u2"},
        "ctags": {},
        "failed_items": {},
        "empty_items": {},
    }
    # Siblings' own state rows are left alone — never destroyed, so a
    # botched merge stays inspectable/recoverable.
    assert state_repo.get("sib-1", "crawl") == {"delta_links": {"drive:a": "u1"}}
    assert state_repo.get("sib-2", "crawl") == {"delta_links": {"drive:b": "u2"}}


def test_plan_and_apply_report_the_same_diagnostics(pg_engine, monkeypatch):
    merge_repo, state_repo = _make_repo(pg_engine, monkeypatch)
    state_repo.put("target", "crawl", {"failed_items": {"graph:1": {"last_failed_at": "2026-01-01T00:00:00Z"}}})
    state_repo.put("sib-1", "crawl", {"failed_items": {"graph:2": {"last_failed_at": "2026-01-02T00:00:00Z"}}})

    plan_diag = merge_repo.plan(target_id="target", sibling_ids=["sib-1"])
    apply_diag = merge_repo.apply(target_id="target", sibling_ids=["sib-1"])
    assert plan_diag == apply_diag


def test_empty_sibling_state_carries_nothing(pg_engine, monkeypatch):
    merge_repo, state_repo = _make_repo(pg_engine, monkeypatch)
    state_repo.put("target", "crawl", {"delta_links": {"drive:root": "u0"}})
    # sib-1 has never crawled — no row at all.
    diag = merge_repo.plan(target_id="target", sibling_ids=["sib-1"])
    assert diag["sib-1"]["crawl"]["delta_links_carried"] == 0
    assert diag["sib-1"]["crawl"]["conflicts"] == []


# ---------------------------------------------------------------------------
# Collisions — the anomalous case (a manually split site with overlapping
# scopes, or a repeat merge)
# ---------------------------------------------------------------------------


def test_delta_link_collision_keeps_the_targets_own_value(pg_engine, monkeypatch):
    """delta_links/ctags carry no per-entry freshness signal (a raw cursor
    string, never a timestamped envelope) — the target's own resume point
    wins deterministically, since the target is the connection that keeps
    crawling after the merge."""
    merge_repo, state_repo = _make_repo(pg_engine, monkeypatch)
    state_repo.put("target", "crawl", {"delta_links": {"drive:a": "target-cursor"}})
    state_repo.put("sib-1", "crawl", {"delta_links": {"drive:a": "sibling-cursor"}})

    diag = merge_repo.apply(target_id="target", sibling_ids=["sib-1"])
    assert diag["sib-1"]["crawl"]["conflicts"] == [
        {"kind": "delta_links", "key": "drive:a", "resolution": "kept_target"}
    ]
    assert state_repo.get("target", "crawl")["delta_links"] == {"drive:a": "target-cursor"}


def test_an_identical_value_on_both_sides_is_not_reported_as_a_conflict(pg_engine, monkeypatch):
    merge_repo, state_repo = _make_repo(pg_engine, monkeypatch)
    state_repo.put("target", "crawl", {"ctags": {"graph:1": "same"}})
    state_repo.put("sib-1", "crawl", {"ctags": {"graph:1": "same"}})

    diag = merge_repo.apply(target_id="target", sibling_ids=["sib-1"])
    assert diag["sib-1"]["crawl"]["conflicts"] == []
    assert state_repo.get("target", "crawl")["ctags"] == {"graph:1": "same"}


def test_failed_items_collision_keeps_the_newer_entry_by_last_failed_at(pg_engine, monkeypatch):
    merge_repo, state_repo = _make_repo(pg_engine, monkeypatch)
    state_repo.put(
        "target",
        "crawl",
        {"failed_items": {"graph:1": {"last_failed_at": "2026-01-01T00:00:00Z", "attempts": 1}}},
    )
    state_repo.put(
        "sib-1",
        "crawl",
        {"failed_items": {"graph:1": {"last_failed_at": "2026-01-05T00:00:00Z", "attempts": 5}}},
    )

    diag = merge_repo.apply(target_id="target", sibling_ids=["sib-1"])
    assert diag["sib-1"]["crawl"]["conflicts"] == [
        {"kind": "failed_items", "key": "graph:1", "resolution": "kept_sibling_newer"}
    ]
    assert state_repo.get("target", "crawl")["failed_items"]["graph:1"]["attempts"] == 5


def test_empty_items_collision_keeps_the_targets_entry_when_it_is_newer(pg_engine, monkeypatch):
    merge_repo, state_repo = _make_repo(pg_engine, monkeypatch)
    state_repo.put("target", "crawl", {"empty_items": {"graph:1": {"last_seen_at": "2026-02-01T00:00:00Z"}}})
    state_repo.put("sib-1", "crawl", {"empty_items": {"graph:1": {"last_seen_at": "2026-01-01T00:00:00Z"}}})

    diag = merge_repo.apply(target_id="target", sibling_ids=["sib-1"])
    assert diag["sib-1"]["crawl"]["conflicts"] == [
        {"kind": "empty_items", "key": "graph:1", "resolution": "kept_target_newer_or_tied"}
    ]
    assert state_repo.get("target", "crawl")["empty_items"]["graph:1"]["last_seen_at"] == "2026-02-01T00:00:00Z"


def test_facts_docs_collision_prefers_status_done_over_not_done(pg_engine, monkeypatch):
    merge_repo, state_repo = _make_repo(pg_engine, monkeypatch)
    state_repo.put("target", "facts", {"docs": {"file-1": {"status": "failed", "at": "2026-01-05T00:00:00Z"}}})
    state_repo.put("sib-1", "facts", {"docs": {"file-1": {"status": "done", "at": "2026-01-01T00:00:00Z"}}})

    diag = merge_repo.apply(target_id="target", sibling_ids=["sib-1"])
    assert diag["sib-1"]["facts"]["conflicts"] == [{"kind": "docs", "key": "file-1", "resolution": "kept_sibling_done"}]
    assert state_repo.get("target", "facts")["docs"]["file-1"]["status"] == "done"


def test_facts_docs_collision_falls_back_to_newer_at_when_both_done(pg_engine, monkeypatch):
    merge_repo, state_repo = _make_repo(pg_engine, monkeypatch)
    state_repo.put("target", "facts", {"docs": {"file-1": {"status": "done", "at": "2026-01-01T00:00:00Z"}}})
    state_repo.put("sib-1", "facts", {"docs": {"file-1": {"status": "done", "at": "2026-01-09T00:00:00Z"}}})

    diag = merge_repo.apply(target_id="target", sibling_ids=["sib-1"])
    assert diag["sib-1"]["facts"]["conflicts"] == [
        {"kind": "docs", "key": "file-1", "resolution": "kept_sibling_newer"}
    ]
    assert state_repo.get("target", "facts")["docs"]["file-1"]["at"] == "2026-01-09T00:00:00Z"


# ---------------------------------------------------------------------------
# Multiple siblings — cross-sibling collisions must be caught too, not just
# sibling-vs-target
# ---------------------------------------------------------------------------


def test_two_siblings_colliding_with_each_other_is_caught_on_the_second(pg_engine, monkeypatch):
    merge_repo, state_repo = _make_repo(pg_engine, monkeypatch)
    state_repo.put("sib-1", "crawl", {"ctags": {"graph:1": "from-sib-1"}})
    state_repo.put("sib-2", "crawl", {"ctags": {"graph:1": "from-sib-2"}})

    diag = merge_repo.apply(target_id="target", sibling_ids=["sib-1", "sib-2"])
    assert diag["sib-1"]["crawl"]["conflicts"] == []
    assert diag["sib-2"]["crawl"]["conflicts"] == [{"kind": "ctags", "key": "graph:1", "resolution": "kept_target"}]
    # sib-1 was folded in first, so it is now "target's own" value by the
    # time sib-2 is processed.
    assert state_repo.get("target", "crawl")["ctags"] == {"graph:1": "from-sib-1"}


# ---------------------------------------------------------------------------
# Idempotency — re-running apply() with the same siblings must converge,
# never double-count or oscillate (the split-merge route's resumable-step
# design relies on this).
# ---------------------------------------------------------------------------


def test_apply_twice_with_the_same_siblings_converges(pg_engine, monkeypatch):
    merge_repo, state_repo = _make_repo(pg_engine, monkeypatch)
    state_repo.put("target", "crawl", {"delta_links": {"drive:root": "u0"}})
    state_repo.put("sib-1", "crawl", {"delta_links": {"drive:a": "u1"}})

    first = merge_repo.apply(target_id="target", sibling_ids=["sib-1"])
    second = merge_repo.apply(target_id="target", sibling_ids=["sib-1"])
    assert first == second
    assert state_repo.get("target", "crawl")["delta_links"] == {"drive:root": "u0", "drive:a": "u1"}
