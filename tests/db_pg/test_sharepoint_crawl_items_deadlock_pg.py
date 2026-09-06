"""Regression coverage for the ``sharepoint_crawl_items`` write-path
deadlock (live-instance incident, 2026-09): a parent crawl planned 776
shards, ran 18 ``corpus-extraction-shard`` jobs concurrently, and 6 of 24
shard children failed within two minutes with::

    OperationalError: (psycopg.errors.DeadlockDetected) deadlock detected
    DETAIL:  Process 429851 waits for ShareLock on transaction 12106022;
             blocked by process 429947.
             Process 429947 waits for ShareLock on transaction 12106026;
             blocked by process 429851.
    CONTEXT: while inserting index tuple (7524,1) in relation
             "sharepoint_crawl_items"

Root cause: :meth:`SharepointCrawlItemsPgRepository.apply_delta` applied a
checkpoint's row-level writes in the CALLER'S dict/set iteration order.
Two shard children can legitimately touch the same
``(connection_id, kind, stable_id)`` row when their scopes overlap
(``kind`` embeds a drive/state key, not a guaranteed-disjoint partition),
and ``connectors.sharepoint.crawler._TrackedDict.drain_dirty`` builds its
``set`` dict from a plain ``set`` of dirty keys — whose iteration order is
Python's per-process hash-randomized order for strings, so two processes
touching the SAME two rows can (and did) acquire their ``ON CONFLICT``
index-tuple locks in opposite order. That is a textbook lock-ordering
deadlock; this file drives it directly with an explicit overlap and
opposite ordering, matching the production shape, rather than depending on
hash-seed randomness to reproduce it.

There is no DuckDB half to parametrize against (PG-first ratchet, A3).
Pattern follows ``tests/db_pg/test_sharepoint_crawl_items_pg.py``.
"""

from __future__ import annotations

import threading

import pytest
import sqlalchemy as sa


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
# 1 + 2: the regression itself — two concurrent, overlapping, oppositely
# ordered checkpoints must both commit, and the final rows must be correct
# (last-writer-wins per key, nothing lost, nothing duplicated).
# ---------------------------------------------------------------------------


def test_concurrent_overlapping_batches_in_opposite_order_both_succeed(pg_engine, monkeypatch):
    """The named production shape: two shard children ("writer A", "writer
    B") both touch the SAME set of stable_ids for the SAME
    (connection_id, kind) in one checkpoint each, but built their batches in
    OPPOSITE order — exactly what two processes disagreeing on a hash-
    randomized ``set``'s iteration order produces. Pre-fix this reliably
    deadlocks (one thread raises ``OperationalError``/``DeadlockDetected``);
    post-fix both threads commit cleanly, every time.
    """
    repo = _make_repo(pg_engine, monkeypatch)

    ids = [f"graph:item-{i:04d}" for i in range(80)]
    forward = {sid: f"fwd-{sid}" for sid in ids}
    backward = {sid: f"bwd-{sid}" for sid in reversed(ids)}

    ready = threading.Barrier(2, timeout=15)
    failures: list[BaseException] = []
    failures_lock = threading.Lock()

    def _writer(set_entries: dict) -> None:
        try:
            ready.wait()
            repo.apply_delta(
                "conn-a",
                "crawl",
                ctags=_delta(set_entries=set_entries),
                failed=_EMPTY_DELTA,
                empty=_EMPTY_DELTA,
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            with failures_lock:
                failures.append(exc)

    threads = [
        threading.Thread(target=_writer, args=(forward,), daemon=True),
        threading.Thread(target=_writer, args=(backward,), daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert not any(t.is_alive() for t in threads), "a writer never finished — looks hung, not deadlocked"
    assert not failures, [repr(exc) for exc in failures]

    # Whichever writer's transaction committed LAST wins every key (both
    # touch the exact same rows) — but every row must exist, with a value
    # from exactly one of the two writers, never a mix and never missing.
    result = repo.get_all("conn-a", "crawl")["ctags"]
    assert set(result) == set(ids)
    for sid in ids:
        assert result[sid] in (forward[sid], backward[sid])
    # last-writer-wins: every surviving row came from the SAME writer.
    assert len({result[sid] == forward[sid] for sid in ids}) == 1


def test_concurrent_overlapping_upserts_touch_no_id_outside_the_two_batches(pg_engine, monkeypatch):
    """A pre-existing row untouched by either concurrent writer survives
    unchanged — the split's whole point (a checkpoint only ever touches the
    files it names) must hold under concurrency too."""
    repo = _make_repo(pg_engine, monkeypatch)
    repo.apply_delta(
        "conn-a",
        "crawl",
        ctags=_delta(set_entries={"graph:untouched": "orig"}),
        failed=_EMPTY_DELTA,
        empty=_EMPTY_DELTA,
    )

    ids = [f"graph:item-{i:04d}" for i in range(40)]
    forward = {sid: f"fwd-{sid}" for sid in ids}
    backward = {sid: f"bwd-{sid}" for sid in reversed(ids)}
    ready = threading.Barrier(2, timeout=15)
    failures: list[BaseException] = []

    def _writer(set_entries: dict) -> None:
        try:
            ready.wait()
            repo.apply_delta(
                "conn-a", "crawl", ctags=_delta(set_entries=set_entries), failed=_EMPTY_DELTA, empty=_EMPTY_DELTA
            )
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)

    threads = [
        threading.Thread(target=_writer, args=(forward,), daemon=True),
        threading.Thread(target=_writer, args=(backward,), daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert not failures, [repr(exc) for exc in failures]
    result = repo.get_all("conn-a", "crawl")["ctags"]
    assert result["graph:untouched"] == "orig"
    assert len(result) == len(ids) + 1


# ---------------------------------------------------------------------------
# 3: bounded retry on a transient DeadlockDetected
# ---------------------------------------------------------------------------


class _FakeDriverError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"fake deadlock detected ({sqlstate})")
        self.sqlstate = sqlstate


def _fake_deadlock() -> sa.exc.OperationalError:
    return sa.exc.OperationalError("INSERT ...", {}, _FakeDriverError("40P01"))


def test_apply_delta_retries_a_transient_deadlock_and_succeeds(pg_engine, monkeypatch):
    """A ``DeadlockDetected`` on the first attempt(s) is retried — the
    checkpoint still lands once the transient contention clears, and the
    caller never sees the transient failure."""
    repo = _make_repo(pg_engine, monkeypatch)
    import src.repositories.sharepoint_crawl_items_pg as mod

    monkeypatch.setattr(mod, "_deadlock_retry_sleep", lambda seconds: None)

    real_once = repo._apply_delta_once
    calls = {"n": 0}

    def _flaky(connection_id, kind, deltas):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _fake_deadlock()
        return real_once(connection_id, kind, deltas)

    monkeypatch.setattr(repo, "_apply_delta_once", _flaky)

    repo.apply_delta(
        "conn-a", "crawl", ctags=_delta(set_entries={"graph:1": "c1"}), failed=_EMPTY_DELTA, empty=_EMPTY_DELTA
    )

    assert calls["n"] == 3
    assert repo.get_all("conn-a", "crawl")["ctags"] == {"graph:1": "c1"}


def test_apply_delta_surfaces_a_persistent_deadlock_instead_of_looping_forever(pg_engine, monkeypatch):
    """A deadlock that NEVER clears must still raise — a bounded retry, not
    an infinite one — so the caller (the crawler's checkpoint) can record
    the failure rather than hang the shard forever."""
    repo = _make_repo(pg_engine, monkeypatch)
    import src.repositories.sharepoint_crawl_items_pg as mod

    monkeypatch.setattr(mod, "_deadlock_retry_sleep", lambda seconds: None)

    calls = {"n": 0}

    def _always_deadlocks(connection_id, kind, deltas):
        calls["n"] += 1
        raise _fake_deadlock()

    monkeypatch.setattr(repo, "_apply_delta_once", _always_deadlocks)

    with pytest.raises(sa.exc.OperationalError):
        repo.apply_delta(
            "conn-a", "crawl", ctags=_delta(set_entries={"graph:1": "c1"}), failed=_EMPTY_DELTA, empty=_EMPTY_DELTA
        )

    assert calls["n"] == mod._DEADLOCK_RETRY_ATTEMPTS


def test_apply_delta_does_not_retry_a_non_transient_error(pg_engine, monkeypatch):
    """A bug (e.g. a bad statement) must fail on the FIRST attempt — a
    retry here would only delay a failure that will never resolve itself,
    same discipline as ``src.db_transient.is_transient_db_error``'s other
    callers."""
    repo = _make_repo(pg_engine, monkeypatch)

    calls = {"n": 0}

    def _boom(connection_id, kind, deltas):
        calls["n"] += 1
        raise RuntimeError("not a deadlock")

    monkeypatch.setattr(repo, "_apply_delta_once", _boom)

    with pytest.raises(RuntimeError):
        repo.apply_delta(
            "conn-a", "crawl", ctags=_delta(set_entries={"graph:1": "c1"}), failed=_EMPTY_DELTA, empty=_EMPTY_DELTA
        )

    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# 4: the batching/ordering function itself, independent of caller order
# ---------------------------------------------------------------------------


def test_touched_stable_ids_are_sorted_regardless_of_input_order():
    from src.repositories.sharepoint_crawl_items_pg import _sorted_touched_stable_ids

    deltas = {
        "ctags": _delta(set_entries={"z": "1", "a": "2"}, removed=["m"]),
        "failed_items": _delta(set_entries={"b": {}}, removed=["y"]),
        "empty_items": _EMPTY_DELTA,
    }
    assert _sorted_touched_stable_ids(deltas) == ["a", "b", "m", "y", "z"]


def test_touched_stable_ids_cross_field_union_is_order_independent():
    """The same LOGICAL delta, built with fields/keys inserted in different
    orders, must produce the identical sorted result — this is what makes
    two callers with the same content but different construction order
    agree on lock order."""
    from src.repositories.sharepoint_crawl_items_pg import _sorted_touched_stable_ids

    deltas_a = {
        "ctags": _delta(set_entries={"x": "1", "y": "2"}),
        "failed_items": _delta(set_entries={"y": {}, "z": {}}),
        "empty_items": _EMPTY_DELTA,
    }
    deltas_b = {
        "ctags": _delta(set_entries={"y": "2", "x": "1"}),
        "failed_items": _delta(set_entries={"z": {}, "y": {}}),
        "empty_items": _EMPTY_DELTA,
    }
    assert _sorted_touched_stable_ids(deltas_a) == _sorted_touched_stable_ids(deltas_b) == ["x", "y", "z"]
