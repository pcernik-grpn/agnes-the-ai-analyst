"""Per-connection state store shared by the SharePoint crawler
(``connectors.sharepoint.crawler``, ``kind="crawl"``) and the facts-
extraction pass (``connectors.sharepoint.facts_extraction``,
``kind="facts"``).

Two callers, one seam, two rows: each module keeps its OWN per-connection
JSON blob — the same reason the two never shared a state FILE (see
``facts_extraction``'s ``_STATE_SUBDIR`` docstring): a corrupt facts pass
must never cost the crawl its deltaLinks, and a resync must never touch the
facts corpus's own bookkeeping.

Backend:

* **Postgres** (the normal case once an instance runs the PG app-state
  backend) — ``sharepoint_connection_state`` via
  ``src.repositories.sharepoint_state_pg``, so any extraction worker on any
  host can pick up any connection's job: the state travels with the
  database the shared job queue already uses, not with one worker's disk.
* **DuckDB** (frozen app-state backend, A3 — see ``CLAUDE.md`` -> "Dual-
  backend discipline") — the pre-existing per-connection JSON file under
  ``${DATA_DIR}/state/sharepoint_<kind>/<connection_id>.json``. This is the
  fail-clean path the freeze requires: a crawl in flight on a DuckDB-backed
  instance must keep working exactly as it always has — this module checks
  ``use_pg()`` itself and never lets ``RequiresPostgresBackend`` reach a
  crawl, unlike an HTTP route that is expected to answer a typed ``501``.

One-time import: the first Postgres read for a connection with no row yet
checks for the legacy file and imports it verbatim (:func:`get` below) — an
existing instance upgrading to Postgres must not lose its delta cursors.
The file is left in place, UNTOUCHED (not renamed, not deleted): a second
import is a guaranteed no-op once the Postgres row exists
(``import_if_absent`` only ever inserts once, per connection/kind), and
leaving a safety net alone is one fewer failure mode than trying to
disarm it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "FactsPassLocked",  # noqa: F822 — lazily resolved via __getattr__ below
    "StateStoreError",
    "delete",
    "facts_pass_lock",
    "file_state_path",
    "get",
    "list_kinds",
    "put",
]


def __getattr__(name: str) -> Any:
    """Lazy re-export of ``FactsPassLocked`` (PEP 562) — so
    ``from connectors.sharepoint.state_store import FactsPassLocked`` works
    without this module importing ``src.repositories.sharepoint_state_pg``
    (and transitively SQLAlchemy) at MODULE scope, matching the rest of
    ``connectors/sharepoint/*``'s convention of keeping ``src.db``/
    ``src.repositories`` imports lazy, inside function bodies."""
    if name == "FactsPassLocked":
        from src.repositories.sharepoint_state_pg import FactsPassLocked

        return FactsPassLocked
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

#: Sub-directory of the state dir each ``kind`` reads/writes on the DuckDB
#: fallback — same names the two modules' own legacy ``_STATE_SUBDIR``
#: constants already used, kept identical so an instance still on the file
#: backend finds exactly the files it always has.
_SUBDIR_BY_KIND = {"crawl": "sharepoint_crawl", "facts": "sharepoint_facts"}


class StateStoreError(RuntimeError):
    """An unsafe connection id, or an unknown ``kind``."""


def file_state_path(kind: str, connection_id: str) -> Path:
    """``<state dir>/sharepoint_<kind>/<connection_id>.json`` — the DuckDB
    fallback (and, until imported, the Postgres path's own source of truth)
    storage location.

    Validates ``connection_id`` as a single safe path segment AND contains
    the resolved path inside the state directory — both layers, per the
    security playbook's filesystem rule.
    """
    subdir = _SUBDIR_BY_KIND.get(kind)
    if subdir is None:
        raise StateStoreError(f"unknown sharepoint state kind: {kind!r}")
    if not _SAFE_SEGMENT_RE.match(connection_id or "") or connection_id in (".", ".."):
        raise StateStoreError(f"unsafe connection id for a state file: {connection_id!r}")
    from src.db import _get_state_dir

    base = (_get_state_dir() / subdir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    resolved = (base / f"{connection_id}.json").resolve()
    resolved.relative_to(base)  # containment assertion; raises ValueError if escaped
    return resolved


def _read_file(kind: str, connection_id: str) -> Optional[Dict[str, Any]]:
    path = file_state_path(kind, connection_id)
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "sharepoint state: %s state file for connection %s unreadable (%s) — treating as absent",
            kind,
            connection_id,
            exc,
        )
        return None
    return loaded if isinstance(loaded, dict) else None


def _write_file(kind: str, connection_id: str, payload: Dict[str, Any]) -> None:
    path = file_state_path(kind, connection_id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _pg_repo() -> Any:
    from src.repositories import sharepoint_state_repo

    return sharepoint_state_repo()


def get(kind: str, connection_id: str) -> Optional[Dict[str, Any]]:
    """This connection's raw stored payload for ``kind`` (``"crawl"`` /
    ``"facts"`` / ``"crawl:<state_key>"`` — a per-delta-unit shard row,
    2026-09-03 auto-parallel-crawl design §4.2), or ``None`` when nothing has
    ever been saved. Callers apply their own defaults — this seam only knows
    about bytes, not either module's state shape.
    """
    from src.repositories import use_pg

    if not use_pg():
        return _read_file(kind, connection_id)

    repo = _pg_repo()
    existing = repo.get(connection_id, kind)
    if existing is not None:
        return existing
    # One-time legacy-file import (module docstring) — only ever applies to
    # the two kinds that ever HAD a legacy file. A shard row (`crawl:<key>`)
    # never did: sharding postdates the move to Postgres, so there is
    # nothing to import — `_read_file` would raise on it anyway
    # (`file_state_path` refuses any kind outside `_SUBDIR_BY_KIND` by
    # design, which is also what keeps a DuckDB-backed instance from ever
    # accepting a shard row — see `put`'s docstring).
    if kind not in _SUBDIR_BY_KIND:
        return None
    legacy = _read_file(kind, connection_id)
    if legacy is None:
        return None
    imported = repo.import_if_absent(connection_id, kind, legacy)
    logger.info(
        "sharepoint state: imported legacy %s state file for connection %s into Postgres",
        kind,
        connection_id,
    )
    return imported


def put(kind: str, connection_id: str, payload: Dict[str, Any]) -> None:
    """Persist ``payload`` as this connection's ``kind`` state.

    A ``"crawl:<state_key>"`` kind (a shard's own per-delta-unit row) is
    accepted on Postgres — the CHECK constraint on ``sharepoint_
    connection_state.kind`` allows it (migration ``0103_crawl_shards``) —
    and REFUSED on the DuckDB fallback: ``_write_file`` routes through
    :func:`file_state_path`, which raises :class:`StateStoreError` for any
    kind outside the fixed ``{"crawl", "facts"}`` pair. This is the
    fail-clean half of "the SharePoint auto-parallel-crawl feature is
    PG-only by construction" (design §4.2) — a DuckDB-backed instance never
    shards, so it never even TRIES to write a shard row.
    """
    from src.repositories import use_pg

    if not use_pg():
        _write_file(kind, connection_id, payload)
        return
    _pg_repo().put(connection_id, kind, payload)


def delete(kind: str, connection_id: str) -> None:
    """Drop this connection's ``kind`` state entirely — the resync path."""
    from src.repositories import use_pg

    if not use_pg():
        file_state_path(kind, connection_id).unlink(missing_ok=True)
        return
    _pg_repo().delete(connection_id, kind)


def list_kinds(connection_id: str, prefix: str) -> List[str]:
    """Every ``kind`` this connection has a state row for, starting with
    ``prefix`` (e.g. ``"crawl:"`` — every shard's own row) — Postgres only.

    The DuckDB fallback always answers ``[]`` rather than raising: this is a
    DISCOVERY helper, not a write path, and a DuckDB-backed instance never
    shards (see :func:`put`'s docstring) — a caller asking "does this
    connection have any shard rows" must get a plain, honest empty answer,
    never a crash, on a backend where the question can never have a yes.
    """
    from src.repositories import use_pg

    if not use_pg():
        return []
    return _pg_repo().list_kinds(connection_id, prefix)


# --------------------------------------------------------------------------
# Facts-pass lock — DuckDB fallback
# --------------------------------------------------------------------------

#: One ``threading.Lock`` per connection id, created lazily. The DuckDB
#: app-state backend is frozen single-process (``docs/migrations.md``), so a
#: process-local lock gives the same "only one facts pass per connection at
#: a time" guarantee the Postgres advisory lock gives across processes.
_facts_locks_guard = threading.Lock()
_facts_locks: Dict[str, threading.Lock] = {}


def _file_backend_lock(connection_id: str) -> threading.Lock:
    with _facts_locks_guard:
        lock = _facts_locks.get(connection_id)
        if lock is None:
            lock = threading.Lock()
            _facts_locks[connection_id] = lock
        return lock


@contextlib.contextmanager
def facts_pass_lock(connection_id: str) -> Iterator[None]:
    """Serialize facts-extraction passes for ONE connection — the crawl's
    chained tail (``facts_extraction.maybe_run_after_crawl``) and the
    standalone operator trigger (``facts_extraction
    .run_standalone_facts_extraction``) can both reach the same connection
    at once, and only one may actually run. Never waits: raises
    :class:`FactsPassLocked` immediately when the lock is already held, so
    each entry point decides for itself whether that means "skip" (the
    chained tail — the standalone pass already covers the corpus) or
    "refuse loudly" (the standalone trigger — it only ever runs because an
    operator explicitly asked).

    Postgres: :meth:`~src.repositories.sharepoint_state_pg
    .SharepointStatePgRepository.facts_pass_lock` — a transaction-scoped
    advisory lock, released automatically when its owning transaction ends,
    so a killed worker can never leave a connection's facts pass stuck
    locked.

    DuckDB (frozen backend, single-process by construction): a plain
    per-connection ``threading.Lock``, non-blocking — sufficient on a
    backend that never spans more than one process.
    """
    from src.repositories import use_pg
    from src.repositories.sharepoint_state_pg import FactsPassLocked

    if use_pg():
        with _pg_repo().facts_pass_lock(connection_id):
            yield
        return

    lock = _file_backend_lock(connection_id)
    if not lock.acquire(blocking=False):
        raise FactsPassLocked(f"a facts-extraction pass is already running for connection {connection_id!r}")
    try:
        yield
    finally:
        lock.release()
