"""Classifies an exception raised against the Postgres app-state connection
as TRANSIENT infrastructure noise (worth a bounded retry) or not.

TCRD-296 C.11 — live finding, 2026-09: a pool-starved worker raised
``sqlalchemy.exc.TimeoutError: QueuePool limit of size N overflow M reached,
connection timed out`` from a SharePoint crawl's ingest step. The worker's
own exception path finalizes a raised error on the FIRST attempt by design
(``app/worker/runtime.py::_run_one`` — a raised exception normally means a
broken handler, and an unattended retry of a genuine bug just delays the
same failure), so a 30-second connection-pool hiccup turned a 4-hour crawl
into one ``failed`` run.

Deliberately narrow: a genuinely bad row (``IntegrityError``, ``DataError``)
or a bad statement (``ProgrammingError``) must still fail on the first
attempt — retrying either only delays a failure that will never resolve
itself. Only a pool-wait timeout, a dropped/reset connection, a deadlock, or
a serialization failure is worth another try.

Used by both :mod:`connectors.sharepoint.crawler` (the ingest-step retry)
and :mod:`app.worker.runtime` (reclassifying a handler's raised exception
into a requeue for a job kind that opts in — see ``JobKind.
transient_retry_in_seconds``), so the two layers can never drift on what
counts as transient.

Kept free of any repository/app import beyond SQLAlchemy/psycopg themselves
— this is a pure classifier, callable from the connector layer as easily as
from the worker runtime.
"""

from __future__ import annotations

import asyncio

import sqlalchemy as sa

#: PostgreSQL SQLSTATE codes worth retrying: Class 08 (connection
#: exception — the connection was refused, reset, or dropped before/while
#: talking to the server), plus deadlock (40P01) and serialization failure
#: (40001), the two multi-transaction races a retry can plausibly outlive.
#: Deliberately NOT the full SQLSTATE space — e.g. 57P03 (admin shutdown)
#: is a deliberate operator action, not a hiccup, and is left to fail on the
#: first attempt like any other. Mirrors the checked-by-code convention
#: already used in ``src/repositories/facts_pg.py`` (``exc.orig.sqlstate ==
#: "23503"``) rather than string-matching the exception's message.
_TRANSIENT_SQLSTATES = frozenset(
    {
        "08000",  # connection_exception
        "08001",  # sqlclient_unable_to_establish_sqlconnection
        "08003",  # connection_does_not_exist
        "08004",  # sqlserver_rejected_establishment_of_sqlconnection
        "08006",  # connection_failure
        "40001",  # serialization_failure
        "40P01",  # deadlock_detected
    }
)


def is_transient_db_error(exc: BaseException) -> bool:
    """True for a DB-layer fault worth retrying.

    Covers, in order:

    1. ``sqlalchemy.exc.TimeoutError`` (a connection-pool wait that timed
       out — ``QueuePool limit ... connection timed out``, the live finding
       this module exists for) and ``asyncio.TimeoutError`` (the pool-wait
       timeout an async caller sees, an alias of the builtin ``TimeoutError``
       on the Python versions this repo targets — listed explicitly for
       clarity, not because it adds coverage).
    2. A ``sqlalchemy.exc.OperationalError``/``DBAPIError`` whose wrapped
       driver exception carries one of :data:`_TRANSIENT_SQLSTATES`, OR an
       ``OperationalError`` with NO sqlstate at all — a raw connection-level
       failure (refused connection, reset, DNS hiccup) never reached the
       server to get one, and is still transient. A DBAPIError subclass with
       a sqlstate NOT in the transient set (``IntegrityError``, ``DataError``,
       ``ProgrammingError``, and any other ``OperationalError`` whose code
       names something else) is NOT transient — retrying it only delays an
       inevitable failure.
    3. A bare, unwrapped ``psycopg.OperationalError`` — the case a caller
       talks to the driver directly rather than through SQLAlchemy.

    Everything else — including every other ``DBAPIError`` subclass, and any
    exception this module has never heard of — is NOT transient.
    """
    if isinstance(exc, (sa.exc.TimeoutError, asyncio.TimeoutError)):
        return True
    if isinstance(exc, (sa.exc.OperationalError, sa.exc.DBAPIError)):
        sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
        if sqlstate in _TRANSIENT_SQLSTATES:
            return True
        return sqlstate is None and isinstance(exc, sa.exc.OperationalError)
    try:
        import psycopg
    except Exception:  # pragma: no cover - psycopg is always installed in this repo
        return False
    return isinstance(exc, psycopg.OperationalError)
