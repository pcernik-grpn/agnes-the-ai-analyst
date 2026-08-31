"""Typed errors raised by the repository factory.

Its own module, deliberately, rather than a class inside
``src/repositories/__init__.py``: exception handling is keyed on class
IDENTITY. Several test harnesses call ``importlib.reload(src.repositories)``
to pick up a backend env flip (``tests/db_pg/_parity_sweep_util.py``, the
``state_backend`` fixture), and a class DEFINED in the reloaded module becomes
a brand-new object afterwards — at which point ``app/main.py``'s
``@app.exception_handler(RequiresPostgresBackend)``, bound once at app
construction, silently stops matching and the documented clean ``501`` turns
into an unhandled ``500``. ``importlib.reload`` does not touch modules the
reloaded one merely imports, so keeping the class here makes its identity
stable across any number of reloads, in either order.

Kept import-free on purpose (no SQLAlchemy, no DuckDB): the factory imports
this at module scope on every install, including DuckDB-only ones.

``src.repositories`` re-exports the name, so
``from src.repositories import RequiresPostgresBackend`` — how every caller
spells it — is unchanged.
"""

from __future__ import annotations


class RequiresPostgresBackend(RuntimeError):
    """Raised when a Postgres-only repository is resolved on an instance
    still running the frozen DuckDB app-state backend.

    A3 PG-first ratchet (see CLAUDE.md -> "Dual-backend discipline"): new
    app-state repos registered after the ratchet flipped carry only a ``PG``
    entry in ``src.repositories._REGISTRY`` — there is no DuckDB
    implementation to fall back to. Route handlers that can reach such a repo
    must let this exception surface rather than catching it and improvising;
    the app-wide handler in ``app/main.py`` translates it to a clean ``501``
    instead of an unhandled ``500``.
    """

    def __init__(self, feature: str):
        self.feature = feature
        super().__init__(
            f"{feature!r} requires the Postgres app-state backend. This feature "
            "was added after the PG-first ratchet (DuckDB app-state is frozen — "
            "see CLAUDE.md -> 'Dual-backend discipline'); migrate this instance "
            "to Postgres to use it — see docs/migrations.md."
        )
