"""The ledger sink — one ``llm_calls`` row per :class:`LlmCallRecord`.

Postgres-only by construction (A3 PG-first ratchet): the table has no DuckDB
sibling, so on a DuckDB-backed instance this is a silent no-op — the spans
and the log line still carry the call, only the queryable ledger is absent.

A measurement never costs the call it measures: every failure here (the repo
not existing yet, a backend that refuses it, a database that is down) is
logged at DEBUG and swallowed.
"""

from __future__ import annotations

import logging

from src.observability.llm_record import LlmCallRecord

logger = logging.getLogger(__name__)


def ledger_available() -> bool:
    """Whether an ``llm_calls`` write could land — i.e. the active app-state
    backend is Postgres. Never raises."""
    try:
        import src.repositories as repos

        return bool(repos.use_pg())
    except Exception:  # noqa: BLE001 - see the module docstring
        return False


def record_call(record: LlmCallRecord) -> None:
    """Write one call to the ledger. Silent no-op on DuckDB, silent failure
    everywhere else."""
    if not ledger_available():
        return
    try:
        import src.repositories as repos

        repos.llm_calls_repo().insert_batch([record.to_row()])
    except Exception:  # noqa: BLE001 - see the module docstring
        logger.debug("llm ledger: could not record call %s", record.id, exc_info=True)


__all__ = ["ledger_available", "record_call"]
