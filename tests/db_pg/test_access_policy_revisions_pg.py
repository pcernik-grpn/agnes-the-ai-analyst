"""Postgres-only tests for the ``access_policy_revisions`` repository
(#1979 K1-sweep finding 1 — "restore this version" for table access
policies).

There is no DuckDB half to parametrize against (PG-first ratchet, A3) — see
``docs/migrations.md`` -> "Adding a PG-only feature". Pattern follows
``tests/db_pg/test_extraction_runs_pg.py``.

The table is created from ``Base.metadata`` for the single model under test
rather than by running the whole Alembic ladder: this file is about the
repository's own contract, and ``tests/db_pg/test_alembic_roundtrip.py``
already owns "the migration and the model agree".
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest


def _make_repo(pg_engine, monkeypatch):
    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg
    from src.models.access_policy_revisions import AccessPolicyRevision

    db_pg.dispose()
    engine = db_pg.get_engine()
    AccessPolicyRevision.__table__.create(engine, checkfirst=True)

    from src.repositories.access_policy_revisions_pg import AccessPolicyRevisionsPgRepository

    return AccessPolicyRevisionsPgRepository(engine)


def test_record_returns_an_apr_prefixed_id_and_stores_the_snapshot(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    rev_id = repo.record(
        table_id="orders",
        policy_sql="SELECT * FROM orders WHERE region = $user_email",
        policy_note="regional scoping",
        policy_mapping=True,
        saved_by="admin@example.com",
    )
    assert rev_id.startswith("apr_")

    row = repo.get(rev_id)
    assert row["table_id"] == "orders"
    assert row["policy_sql"] == "SELECT * FROM orders WHERE region = $user_email"
    assert row["policy_note"] == "regional scoping"
    assert row["policy_mapping"] is True
    assert row["saved_by"] == "admin@example.com"
    assert row["saved_at"]
    # `cleared` is derived, never stored: a revision whose SQL is NULL IS
    # the "policy removed" event, and the reader must not have to
    # re-derive that rule for itself.
    assert row["cleared"] is False


def test_a_cleared_policy_is_recorded_as_a_revision_with_null_sql(pg_engine, monkeypatch):
    """Clearing is a policy change like any other — the history has to show
    WHEN protection was removed, which is the single most important row in
    it."""
    repo = _make_repo(pg_engine, monkeypatch)
    rev_id = repo.record(table_id="orders", policy_sql=None, policy_note=None, saved_by="admin@example.com")

    row = repo.get(rev_id)
    assert row["policy_sql"] is None
    assert row["cleared"] is True


def test_get_unknown_revision_is_none(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    assert repo.get("apr_nope") is None


def test_list_for_table_is_newest_first_and_scoped_to_the_table(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    repo.record(table_id="orders", policy_sql="SELECT 1", policy_note="first", saved_by="a@x.com")
    repo.record(table_id="orders", policy_sql="SELECT 2", policy_note="second", saved_by="b@x.com")
    repo.record(table_id="invoices", policy_sql="SELECT 3", policy_note="other table", saved_by="c@x.com")

    rows = repo.list_for_table("orders")
    assert [r["policy_note"] for r in rows] == ["second", "first"]
    assert {r["table_id"] for r in rows} == {"orders"}


def test_consecutive_records_never_share_a_timestamp(pg_engine, monkeypatch):
    """Two saves inside the same clock tick must still order deterministically
    — the history panel's "newest first" is a claim about EDIT order, and a
    tie would let the older body render on top of the newer one (and be the
    one an admin restores)."""
    repo = _make_repo(pg_engine, monkeypatch)
    for i in range(10):
        repo.record(table_id="orders", policy_sql=f"SELECT {i}", policy_note=f"n{i}", saved_by="a@x.com")

    rows = repo.list_for_table("orders", limit=50)
    stamps = [r["saved_at"] for r in rows]
    assert len(set(stamps)) == len(stamps), stamps
    assert [r["policy_note"] for r in rows] == [f"n{i}" for i in reversed(range(10))]


def test_an_explicit_saved_at_is_stored_verbatim(pg_engine, monkeypatch):
    """The backfilled baseline revision (the policy that was already stored
    before this feature existed) carries the ORIGINAL
    ``access_policy_updated_at``, not the moment it was backfilled — a
    history that re-dates the past is worse than one that is short."""
    repo = _make_repo(pg_engine, monkeypatch)
    then = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    rev_id = repo.record(
        table_id="orders",
        policy_sql="SELECT 1",
        policy_note="baseline",
        saved_by="old-admin@example.com",
        saved_at=then,
    )
    # Compared as an INSTANT, not as a string: the driver may hand the
    # value back in the session's local zone, which is the same moment
    # spelled differently.
    assert datetime.fromisoformat(repo.get(rev_id)["saved_at"]) == then


def test_list_for_table_caps_the_limit(pg_engine, monkeypatch):
    """The panel asks for ~10; a caller asking for a million gets the cap,
    not a table scan rendered into a modal."""
    repo = _make_repo(pg_engine, monkeypatch)
    for i in range(12):
        repo.record(table_id="orders", policy_sql=f"SELECT {i}", policy_note=f"n{i}", saved_by="a@x.com")

    assert len(repo.list_for_table("orders")) == 10  # default
    assert len(repo.list_for_table("orders", limit=3)) == 3
    assert len(repo.list_for_table("orders", limit=10_000)) == 12  # clamped to _MAX_LIMIT (50)
    assert len(repo.list_for_table("orders", limit=0)) == 1  # floor of 1, never "all"
    assert len(repo.list_for_table("orders", limit=None)) == 10  # None is the one "no preference"


def test_count_for_table_reports_the_untruncated_total(pg_engine, monkeypatch):
    repo = _make_repo(pg_engine, monkeypatch)
    for i in range(12):
        repo.record(table_id="orders", policy_sql=f"SELECT {i}", policy_note=f"n{i}", saved_by="a@x.com")
    repo.record(table_id="invoices", policy_sql="SELECT 1", policy_note="x", saved_by="a@x.com")

    assert repo.count_for_table("orders") == 12
    assert repo.count_for_table("invoices") == 1
    assert repo.count_for_table("never-policied") == 0


def test_delete_for_table_removes_only_that_tables_revisions(pg_engine, monkeypatch):
    """A re-registered table id must not inherit the deleted table's policy
    history — the SQL bodies of a table that no longer exists have no
    business rendering (and being restorable) on a new one."""
    repo = _make_repo(pg_engine, monkeypatch)
    repo.record(table_id="orders", policy_sql="SELECT 1", policy_note="a", saved_by="a@x.com")
    repo.record(table_id="orders", policy_sql="SELECT 2", policy_note="b", saved_by="a@x.com")
    repo.record(table_id="invoices", policy_sql="SELECT 3", policy_note="c", saved_by="a@x.com")

    assert repo.delete_for_table("orders") == 2
    assert repo.list_for_table("orders") == []
    assert repo.count_for_table("invoices") == 1


def test_saved_at_round_trips_as_an_isoformat_string(pg_engine, monkeypatch):
    """Every consumer is JSON (the API, then the modal) — a driver-native
    datetime leaking through would serialize differently per backend."""
    repo = _make_repo(pg_engine, monkeypatch)
    rev_id = repo.record(table_id="orders", policy_sql="SELECT 1", policy_note="a", saved_by="a@x.com")
    saved_at = repo.get(rev_id)["saved_at"]
    assert isinstance(saved_at, str)
    parsed = datetime.fromisoformat(saved_at)
    assert parsed.tzinfo is not None
    assert abs(parsed - datetime.now(timezone.utc)) < timedelta(minutes=5)


# ---------------------------------------------------------------------------
# ``policy_write_lock`` — per-table serialization of "save the policy, then
# append its revision" (#1979, review follow-up).
#
# The bug it closes: two concurrent PUTs on one table could commit policy A,
# then policy B, then record B's revision, then record A's — leaving a
# history whose newest row is A while the stored policy is B.
# ---------------------------------------------------------------------------


def _acquire_in_thread(repo, table_id: str, *, timeout: float = 5.0) -> bool:
    """True when the lock for ``table_id`` could be taken within ``timeout``.

    A hung acquisition must fail the test, not hang the suite — so the
    attempt runs in a thread we can walk away from.
    """
    took = threading.Event()

    def _try() -> None:
        with repo.policy_write_lock(table_id):
            took.set()

    thread = threading.Thread(target=_try, daemon=True)
    thread.start()
    thread.join(timeout)
    return took.is_set()


def test_policy_write_lock_serializes_two_writers_on_the_same_table(pg_engine, monkeypatch):
    """Two concurrent savers of ONE table's policy never interleave: the
    second waits for the first to commit, so "policy written" and "revision
    appended" stay in the same order for both."""
    repo = _make_repo(pg_engine, monkeypatch)

    events: list[str] = []
    events_lock = threading.Lock()
    ready = threading.Barrier(2, timeout=15)
    failures: list[BaseException] = []

    def _worker(name: str) -> None:
        try:
            ready.wait()
            with repo.policy_write_lock("orders"):
                with events_lock:
                    events.append(f"enter:{name}")
                time.sleep(0.2)
                with events_lock:
                    events.append(f"exit:{name}")
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)

    threads = [threading.Thread(target=_worker, args=(n,), daemon=True) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)

    assert not failures, failures
    assert not any(t.is_alive() for t in threads), "a writer never got the lock"
    # Intervals must not overlap — whoever entered first also exited first.
    assert [e.split(":")[0] for e in events] == ["enter", "exit", "enter", "exit"], events


def test_policy_write_lock_does_not_block_a_different_table(pg_engine, monkeypatch):
    """Serialization is PER TABLE: policy saves on unrelated tables are
    independent and must not queue behind each other."""
    repo = _make_repo(pg_engine, monkeypatch)

    holding = threading.Event()
    release = threading.Event()

    def _hold() -> None:
        with repo.policy_write_lock("orders"):
            holding.set()
            release.wait(15)

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    assert holding.wait(15), "the holder never acquired the lock"
    try:
        assert _acquire_in_thread(repo, "invoices"), "an unrelated table blocked on another table's lock"
    finally:
        release.set()
        holder.join(15)


def test_policy_write_lock_is_released_when_the_body_raises(pg_engine, monkeypatch):
    """The lock is transaction-scoped: a failure inside the ``with`` block
    rolls the holding transaction back and releases it. A save that blew up
    must never wedge every later save of that table."""
    repo = _make_repo(pg_engine, monkeypatch)

    with pytest.raises(RuntimeError):
        with repo.policy_write_lock("orders"):
            raise RuntimeError("boom")

    assert _acquire_in_thread(repo, "orders"), "the lock survived a failed body"
