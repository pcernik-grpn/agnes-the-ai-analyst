"""A3 PG-first ratchet — the typed ``RequiresPostgresBackend`` error and its
app-wide exception handler.

CLAUDE.md -> "Dual-backend discipline": a repository registered PG-only
(post-A3) raises this error when resolved on an instance still running the
frozen DuckDB app-state backend. The point of the error being *typed* is that
``app/main.py`` can translate it into a clean 501 for every such route
without each handler needing its own try/except — proven here directly
against the registered handler as well as, since F4.1, through a live route
(``/api/admin/semantic-model/coverage*``, pinned in
``tests/test_semantic_model_coverage_endpoint.py``). This is the mechanism
test referenced by ``docs/migrations.md`` -> "Adding a PG-only feature".
"""

from __future__ import annotations

import asyncio
import json

import pytest
from starlette.requests import Request

from src.repositories import RequiresPostgresBackend


def test_requires_postgres_backend_message_names_the_feature_and_the_recipe():
    exc = RequiresPostgresBackend("widgets")
    assert exc.feature == "widgets"
    msg = str(exc)
    assert "widgets" in msg
    assert "Postgres" in msg
    assert "docs/migrations.md" in msg


def test_build_raises_requires_postgres_backend_for_pg_only_entry(monkeypatch):
    """Negative control at the factory level: a registry entry with only a
    PG backend must raise the typed error (not a bare KeyError) when
    resolved while the active backend is DuckDB."""
    import src.repositories as factory

    monkeypatch.setattr(
        factory,
        "_REGISTRY",
        {
            **factory._REGISTRY,
            "_pg_only_probe": {factory.PG: ("src.repositories.users_pg", "UsersPgRepository")},
        },
    )
    monkeypatch.setattr(factory, "_active_backend", lambda: factory.DUCKDB)

    with pytest.raises(RequiresPostgresBackend) as exc_info:
        factory._build("_pg_only_probe")
    assert exc_info.value.feature == "_pg_only_probe"


def test_build_still_raises_key_error_for_a_truly_unknown_key(monkeypatch):
    """Negative control the other direction: an entry that is missing
    entirely (not merely missing the active backend) still raises the
    ordinary ``KeyError`` — the typed error is specific to the PG-only shape."""
    import src.repositories as factory

    with pytest.raises(KeyError):
        factory._build("_totally_unregistered_probe")


def test_requires_postgres_backend_handler_returns_clean_501(shared_app):
    """The app-wide handler (app/main.py) must translate the typed error into
    a 501 with a JSON body naming the feature — never let it fall through to
    the unhandled-exception 500 handler."""
    handler = shared_app.exception_handlers.get(RequiresPostgresBackend)
    assert handler is not None, "no exception handler registered for RequiresPostgresBackend"

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/widgets",
        "headers": [],
        "query_string": b"",
    }
    request = Request(scope)
    exc = RequiresPostgresBackend("widgets")

    response = asyncio.run(handler(request, exc))

    assert response.status_code == 501
    body = json.loads(bytes(response.body))
    assert body["error"] == "requires_postgres_backend"
    assert body["feature"] == "widgets"
    assert "docs/migrations.md" in body["detail"]


def test_the_error_class_survives_a_reload_of_the_repository_factory():
    """Class IDENTITY must be stable across ``importlib.reload(src.repositories)``.

    Exception handlers are keyed on the class object. Several PG test
    harnesses reload the factory module to pick up a backend env flip
    (``tests/db_pg/_parity_sweep_util.py``, the ``state_backend`` fixture); if
    the class were DEFINED there, the reload would mint a new one and
    ``app/main.py``'s handler — bound once at app construction — would stop
    matching, turning the documented clean 501 into an unhandled 500. It bit
    exactly that way when the first PG-only route landed, which is why the
    class lives in ``src/repository_errors.py`` and is only re-exported here.
    """
    import importlib

    import src.repositories as factory

    before = factory.RequiresPostgresBackend
    importlib.reload(factory)
    assert factory.RequiresPostgresBackend is before
    assert factory.RequiresPostgresBackend is RequiresPostgresBackend


def test_requires_postgres_backend_handler_is_distinct_from_the_catch_all(shared_app):
    """Sanity check on the handler wiring itself: ``RequiresPostgresBackend``
    must resolve to its own dedicated handler, not merely fall through to the
    generic ``Exception`` catch-all (which would produce an opaque 500
    instead of the clean, feature-naming 501)."""
    specific = shared_app.exception_handlers.get(RequiresPostgresBackend)
    catch_all = shared_app.exception_handlers.get(Exception)
    assert specific is not None
    assert catch_all is not None
    assert specific is not catch_all
