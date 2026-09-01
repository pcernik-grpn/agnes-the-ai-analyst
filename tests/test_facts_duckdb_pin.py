"""Regression test for issue #1658.

A DuckDB-expecting test (most commonly a facts endpoint's
``*_fails_clean_on_duckdb`` typed-501 assertion) must not depend on being the
first thing to run in its pytest-xdist worker process. A ``tests/db_pg/``
test earlier in the SAME process can leave ``src.repositories.use_pg()``
resolving Postgres via two things that do not reset on their own — a stale
``instance.yaml`` overlay at the frozen ``src.db_state_machine._OVERLAY_
PATH``, and the effects of ``importlib.reload(src.repositories)`` — even
though every PG fixture's own env changes (``monkeypatch.setenv("AGNES_DB_
URL", ...)``) are auto-restored at that fixture's teardown.

CI itself never mixes the two families in one process (``test-shard`` ignores
``tests/db_pg/``; ``test-pg`` runs ONLY ``tests/db_pg/``), so this test cannot
rely on collection order to reproduce the leak. It SIMULATES the exact reload
``tests/db_pg/conftest.py``'s ``state_backend`` fixture performs (line 443,
unconditionally — even on its "duckdb" parametrization), proves ``tests/
_backend_pin.py``'s mechanics repair a request against the SAME
session-shared app the rest of the suite runs against, and restores module
state afterward — mirroring what ``tests/db_pg/_parity_sweep_util.py::
build_seeded_client`` does on every call — so it leaves no residue for
whatever test runs next in this worker process.
"""

from __future__ import annotations

import importlib


def test_pin_recovers_a_facts_endpoint_from_a_reload_leak(seeded_app, monkeypatch, tmp_path):
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
    app = seeded_app["client"].app

    # --- 1. Simulate the leak: a `tests/db_pg/` test set AGNES_DB_URL and
    #     reloaded `src.repositories` — exactly what the `state_backend`
    #     fixture (tests/db_pg/conftest.py:443) does on EVERY
    #     parametrization, including "duckdb".
    monkeypatch.setenv("AGNES_DB_URL", "postgresql://fake-leak-simulation/agnes")
    import src.repositories

    importlib.reload(src.repositories)

    # Sanity: prove the simulated leak is real before claiming to fix it.
    assert src.repositories.use_pg() is True, "setup failed to simulate a Postgres-resolving leak"

    # --- 2. The fixture mechanics must repair it for THIS test's request,
    #     independent of the ambient env / reloaded module above.
    from tests._backend_pin import pin_duckdb_backend

    pin_duckdb_backend(monkeypatch, tmp_path, app=app)

    from src.repositories import use_pg

    assert use_pg() is False, "pin_duckdb_backend did not neutralize the simulated reload leak"

    r = seeded_app["client"].post("/api/facts/search", json={}, headers=headers)
    assert r.status_code == 501, r.text
    body = r.json()
    assert body["error"] == "requires_postgres_backend"
    assert body["feature"] == "facts"

    # --- 3. Restore module state so this test leaves no residue for
    #     whatever runs next in this worker process — the same repair
    #     `_parity_sweep_util.build_seeded_client` performs on every call:
    #     drop the env, reload again, and re-pin the exception handler.
    #     Doing this INSIDE the test (rather than trusting monkeypatch's own
    #     end-of-test undo) is the point — `importlib.reload` is a
    #     process-global side effect monkeypatch knows nothing about.
    monkeypatch.delenv("AGNES_DB_URL", raising=False)
    importlib.reload(src.repositories)

    from tests._backend_pin import reregister_requires_pg_handler

    reregister_requires_pg_handler(app)

    # Trailing sanity: a request against the SAME app, with no ambient PG env
    # and no fixture-scoped pin active beyond the restore above, still
    # resolves DuckDB and fails clean the ordinary way — proving the restore
    # actually worked rather than merely relying on this test's own
    # `duckdb_backend_pinned`-shaped teardown (which never ran here).
    from src.repositories import use_pg as use_pg_after_restore

    assert use_pg_after_restore() is False
    r2 = seeded_app["client"].post("/api/facts/search", json={}, headers=headers)
    assert r2.status_code == 501, r2.text
    assert r2.json()["error"] == "requires_postgres_backend"
