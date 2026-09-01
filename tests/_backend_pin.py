"""Shared helper for pinning a test's app-state backend resolution to DuckDB.

``src.repositories.use_pg()`` resolves the active backend live, per call, from
(in precedence order) an EXPLICIT ``instance.yaml`` ``database.backend``
declaration, ``DATABASE_URL``, then the legacy ``AGNES_DB_URL``. A PG-oriented
fixture that flips one of these back to Postgres always restores it — via
``monkeypatch``, which auto-undoes at test teardown — so ambient env is NOT
the leak (issue #1658).

Two things do NOT reset per test on their own within one pytest-xdist worker
process, and either can make a DuckDB-expecting test (most commonly a
``*_fails_clean_on_duckdb`` typed-501 assertion) silently resolve onto
Postgres — or crash 500 — after a ``tests/db_pg/`` test ran earlier in the
same process:

* ``src.db_state_machine._OVERLAY_PATH`` is computed ONCE, at first import,
  from whatever ``DATA_DIR`` happened to be set at that moment — never
  recomputed per test, even though ``e2e_env`` repoints ``DATA_DIR`` for
  every test via ``monkeypatch``. Nothing DELETES a stale overlay file an
  earlier test wrote at that frozen path. (The per-test autouse
  ``_reset_module_caches`` fixture in ``tests/conftest.py`` already clears
  the PARSED-overlay cache, ``_STATE_CACHE``, every test — that half of the
  problem is already covered; this module handles the file-path half.)
* ``importlib.reload(src.repositories)`` — performed unconditionally by
  ``tests/db_pg/conftest.py``'s ``state_backend`` fixture (even on its
  "duckdb" parametrization) and by ``tests/db_pg/_parity_sweep_util.py``'s
  ``build_seeded_client`` — re-executes the module in place. This cannot
  currently rebind ``RequiresPostgresBackend`` to a new class object
  (``src/repository_errors.py`` keeps that class in an unreloaded module for
  exactly this reason), but :func:`reregister_requires_pg_handler` remains as
  belt-and-braces re-registration on the app's exception-handler table in
  case a future change ever makes that reload mint a new class again — see
  ``tests/db_pg/test_pg_only_route_exemption_mechanism.py::
  test_post_reload_raise_still_translates_to_typed_501``.

A test file that asserts a DuckDB-only outcome should not depend on being the
first thing to run in its worker process. Request the ``duckdb_backend_
pinned`` fixture in ``tests/conftest.py`` (which wraps :func:`pin_duckdb_
backend` below) rather than relying on ambient env.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def pin_duckdb_backend(monkeypatch, tmp_path: Path, *, app: Any = None) -> None:
    """Force ``src.repositories.use_pg()`` to resolve ``False`` for the rest
    of the current test, regardless of ambient ``DATABASE_URL`` /
    ``AGNES_DB_URL`` or a stale ``instance.yaml`` overlay another test in
    this worker process left on disk.

    ``app``, when given, is a FastAPI application whose ``RequiresPostgres
    Backend`` exception handler is re-bound to the CURRENT class object —
    see :func:`reregister_requires_pg_handler`. Pass it whenever the pin
    protects requests against a session-shared app (``shared_app`` /
    ``seeded_app``), since that app was built once, long before this test —
    and possibly before any reload of ``src.repositories``.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)
    # `_OVERLAY_PATH` is frozen at `src.db_state_machine` import time — point
    # it at a path that can never exist so a stale `database: {...}`
    # declaration an earlier test in this worker wrote to the REAL frozen
    # path cannot be read back. `read_backend_state()` treats a missing
    # overlay as "no declaration" (BackendState.DUCKDB, declared=False), the
    # same as a pristine process.
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", tmp_path / "no-such-instance-overlay.yaml")
    if app is not None:
        reregister_requires_pg_handler(app)


def reregister_requires_pg_handler(fastapi_app) -> None:
    """Re-bind app/main's typed-501 handler to the *current*
    ``RequiresPostgresBackend`` class.

    A harness that reloads ``src.repositories`` (``tests/db_pg/conftest.py``'s
    ``state_backend`` fixture, ``tests/db_pg/_parity_sweep_util.py``'s
    ``build_seeded_client``) would rebind ``RequiresPostgresBackend`` to a NEW
    class object if that class were ever defined inside the reloaded module
    again. ``app.main`` imported the class once, at ITS OWN import time, and
    ``create_app`` registers the 501-translation handler against that
    original object; Starlette resolves handlers by walking the raised
    exception's MRO, which would never contain a post-reload class. So
    without this step, a real PG-only exemption raised after such a reload
    would miss the handler and fall through to the catch-all 500 — and
    production (which never reloads the module) would answer the typed 501
    while the test harness answers a 500 for the exact same code path.

    Moved here from ``tests/db_pg/_parity_sweep_util.py`` (issue #1658) so
    the same repair is reachable from ``tests/conftest.py``'s ``duckdb_
    backend_pinned`` fixture, which protects the session-shared app used by
    plain (non-``db_pg``) DuckDB-backend tests. Behavior is unchanged;
    ``_parity_sweep_util.py`` now calls this instead of keeping its own copy.

    Regression: ``tests/db_pg/test_pg_only_route_exemption_mechanism.py::
    test_post_reload_raise_still_translates_to_typed_501``.
    """
    import app.main as app_main
    import src.repositories

    current_cls = src.repositories.RequiresPostgresBackend
    if current_cls in fastapi_app.exception_handlers:
        return  # app.main's binding is already the current class — nothing to do
    handler = fastapi_app.exception_handlers.get(app_main.RequiresPostgresBackend)
    assert handler is not None, (
        "app/main.py no longer registers an exception handler for "
        "RequiresPostgresBackend — the parity sweeps' fail-clean check "
        "depends on that typed 501 translation"
    )
    fastapi_app.add_exception_handler(current_cls, handler)
