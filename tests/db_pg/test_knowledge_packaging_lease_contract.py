"""Two concurrent knowledge-packaging handlers must not both proceed.

Unlike ``rebuild_lease``/``seed_lease`` (blocking ``pg_advisory_lock``),
``knowledge_packaging_lease`` is NON-blocking (``pg_try_advisory_lock``): a
second concurrent caller must see ``acquired=False`` immediately and skip,
never wait. Mirrors ``tests/db_pg/test_rebuild_lease_contract.py``.
"""

import threading

from src.db_pg import knowledge_packaging_lease


def test_knowledge_packaging_lease_second_caller_does_not_acquire(pg_engine, monkeypatch):
    import src.db_pg as db_pg

    monkeypatch.setenv("DATABASE_URL", str(pg_engine.url))
    monkeypatch.setattr(db_pg, "_lease_use_pg", lambda: True)
    db_pg.dispose()

    results: dict[str, bool] = {}
    first_holding = threading.Event()
    release_first = threading.Event()

    def hold():
        with knowledge_packaging_lease() as acquired:
            results["first"] = acquired
            first_holding.set()
            release_first.wait(timeout=5)

    def contend():
        first_holding.wait(timeout=5)
        with knowledge_packaging_lease() as acquired:
            results["second"] = acquired
        release_first.set()

    try:
        t1, t2 = threading.Thread(target=hold), threading.Thread(target=contend)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
    finally:
        release_first.set()
        db_pg.dispose()

    assert results["first"] is True
    assert results["second"] is False


def test_knowledge_packaging_lease_released_after_use(pg_engine, monkeypatch):
    """After the holder exits, a later caller acquires cleanly (the lock
    was actually released, not leaked)."""
    import src.db_pg as db_pg

    monkeypatch.setenv("DATABASE_URL", str(pg_engine.url))
    monkeypatch.setattr(db_pg, "_lease_use_pg", lambda: True)
    db_pg.dispose()

    try:
        with knowledge_packaging_lease() as first:
            assert first is True
        with knowledge_packaging_lease() as second:
            assert second is True
    finally:
        db_pg.dispose()


def test_knowledge_packaging_lease_noop_on_duckdb(monkeypatch):
    monkeypatch.setattr("src.db_pg._lease_use_pg", lambda: False)
    with knowledge_packaging_lease() as acquired:
        assert acquired is True  # must not require a PG connection
