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


class PoliciedRowDistributionError(RuntimeError):
    """Raised when a ``table_registry`` upsert would leave a row that carries
    an access policy DISTRIBUTABLE.

    A table access policy is only enforceable while Agnes evaluates the read,
    so a policied row must stay undistributed — ``query_mode='remote'`` or
    ``server_only=TRUE`` (docs/table-access-policies.md -> "Scope: only tables
    that never leave the server"). ``app/api/admin.py`` enforces that at the
    API boundary (``access_policy_requires_undistributed``); this error is the
    same invariant restated one layer down, where every non-admin writer —
    connector auto-discovery, the boot-time internal-table refresh, a
    collection file re-ingest — reaches ``register()`` without passing through
    that endpoint.

    Lives here rather than in ``src/repositories/table_registry.py`` for the
    same reason ``RequiresPostgresBackend`` does: ``app/main.py`` binds an
    exception handler on class IDENTITY at app construction, and this module
    is never reloaded.
    """

    def __init__(self, table_id: str, query_mode: str):
        self.table_id = table_id
        self.query_mode = query_mode
        super().__init__(
            f"access_policy_requires_undistributed: table_registry row {table_id!r} carries an "
            "access policy, so it must stay undistributed (query_mode='remote' or "
            f"server_only=true) -- refusing an upsert that would leave it query_mode={query_mode!r} "
            "with server_only=false. Clear the access policy first if the table is really meant "
            "to be distributed."
        )
