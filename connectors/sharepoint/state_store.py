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
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "FactsPassLocked",  # noqa: F822 — lazily resolved via __getattr__ below
    "StateStoreError",
    "any_facts_pass_running",
    "crawl_items_apply",
    "crawl_items_get",
    "delete",
    "facts_pass_lock",
    "file_state_path",
    "get",
    "list_kinds",
    "merge_docs",
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


def _crawl_items_repo() -> Any:
    from src.repositories import sharepoint_crawl_items_repo

    return sharepoint_crawl_items_repo()


#: The three per-file crawl collections this seam can split out of the hot
#: ``put``/``get`` blob on Postgres — shared with
#: ``connectors.sharepoint.crawler`` so the two modules can never disagree
#: on the field names either side reads/writes.
CRAWL_ITEM_FIELDS: Tuple[str, ...] = ("ctags", "failed_items", "empty_items")


def crawl_items_get(
    kind: str, connection_id: str, *, legacy: Optional[Dict[str, Dict[str, Any]]] = None
) -> Optional[Dict[str, Dict[str, Any]]]:
    """The per-FILE ``{"ctags", "failed_items", "empty_items"}`` maps for
    ``(connection_id, kind)`` — Postgres only, read from
    ``sharepoint_crawl_items`` (migration ``0110_sharepoint_crawl_items``;
    see that migration's docstring for the TOAST/dead-tuple incident this
    split fixes). Returns ``None`` on the DuckDB fallback: these three
    collections stay embedded in the whole ``get``/``put`` payload there,
    unchanged — the caller (``connectors.sharepoint.crawler.load_state``)
    keeps its own pre-split behaviour for that backend.

    ``legacy`` (optional): a connection touched for the first time after
    this split shipped may still carry its pre-split copies embedded in the
    hot blob's own payload (a crawl mid-flight when this change deployed).
    When the item table has NOTHING yet for this ``(connection_id, kind)``
    AND ``legacy`` is non-empty, it is imported once — the same "old shape
    -> new shape, exactly once" pattern :func:`get`'s own module docstring
    already uses for the file -> Postgres-row move.
    """
    from src.repositories import use_pg

    if not use_pg():
        return None
    repo = _crawl_items_repo()
    items = repo.get_all(connection_id, kind)
    if legacy and any(legacy.get(field) for field in CRAWL_ITEM_FIELDS) and not any(items.values()):
        repo.apply_delta(
            connection_id,
            kind,
            ctags={"set": legacy.get("ctags") or {}, "removed": (), "reset": False},
            failed={"set": legacy.get("failed_items") or {}, "removed": (), "reset": False},
            empty={"set": legacy.get("empty_items") or {}, "removed": (), "reset": False},
        )
        items = repo.get_all(connection_id, kind)
        logger.info(
            "sharepoint state: imported legacy embedded ctags/failed_items/empty_items for "
            "connection %s kind %s into the per-item table",
            connection_id,
            kind,
        )
    return items


def crawl_items_apply(kind: str, connection_id: str, deltas: Dict[str, Dict[str, Any]]) -> bool:
    """Apply one checkpoint's ``ctags``/``failed_items``/``empty_items``
    deltas (``connectors.sharepoint.crawler._drain_item_deltas`` — each
    shaped ``{"set": {...}, "removed": [...], "reset": bool}``).

    Returns whether THIS backend keeps the three collections out of the hot
    blob at all: ``True`` on Postgres (even when ``deltas`` is a total
    no-op — nothing to flush this call, but the split still applies, so the
    caller must still exclude these fields from what it hands to
    :func:`put`), ``False`` on the DuckDB fallback (nothing written; the
    caller keeps them embedded in the very same payload, unchanged).
    """
    from src.repositories import use_pg

    if not use_pg():
        return False
    if any(deltas[field]["reset"] or deltas[field]["set"] or deltas[field]["removed"] for field in CRAWL_ITEM_FIELDS):
        _crawl_items_repo().apply_delta(
            connection_id,
            kind,
            ctags=deltas["ctags"],
            failed=deltas["failed_items"],
            empty=deltas["empty_items"],
        )
    return True


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

#: One ``threading.Lock`` per (connection id, partition key), created
#: lazily. The DuckDB app-state backend is frozen single-process (``docs/
#: migrations.md``), so a process-local lock gives the same "only one
#: facts pass per PARTITION at a time" guarantee the Postgres advisory
#: lock gives across processes — distinct partitions of the same
#: connection get distinct dict entries (and so never contend with each
#: other), exactly like the Postgres side's distinct advisory-lock keys.
_facts_locks_guard = threading.Lock()
_facts_locks: Dict[str, threading.Lock] = {}


def _facts_lock_key(connection_id: str, partition: Optional[Tuple[int, int]]) -> str:
    """The lock-table key for ``(connection_id, partition)`` — exactly
    ``connection_id`` (unchanged) when ``partition`` is ``None`` or
    ``count <= 1``, so every existing (un-partitioned) caller keeps
    locking the SAME key it always has."""
    if partition is None or partition[1] <= 1:
        return connection_id
    index, count = partition
    return f"{connection_id}:{index}/{count}"


def _file_backend_lock(connection_id: str, partition: Optional[Tuple[int, int]] = None) -> threading.Lock:
    key = _facts_lock_key(connection_id, partition)
    with _facts_locks_guard:
        lock = _facts_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _facts_locks[key] = lock
        return lock


@contextlib.contextmanager
def facts_pass_lock(connection_id: str, *, partition: Optional[Tuple[int, int]] = None) -> Iterator[None]:
    """Serialize facts-extraction passes for one connection — the crawl's
    chained tail (``facts_extraction.maybe_run_after_crawl``) and the
    standalone operator trigger (``facts_extraction
    .run_standalone_facts_extraction``) can both reach the same connection
    at once, and only one may actually run. Never waits: raises
    :class:`FactsPassLocked` immediately when the lock is already held, so
    each entry point decides for itself whether that means "skip" (the
    chained tail — the standalone pass already covers the corpus) or
    "refuse loudly" (the standalone trigger — it only ever runs because an
    operator explicitly asked).

    ``partition`` (``(index, count)``, TCRD-296 gap #67) — ``None`` or
    ``count <= 1`` (every caller before this feature existed) locks the
    WHOLE connection, byte-identical to today. ``count > 1`` locks only
    THIS partition's own slot, so several partitions of the SAME
    connection can hold this context manager at once without contending —
    see :func:`any_facts_pass_running` for "is ANY of them held right now".

    Postgres: :meth:`~src.repositories.sharepoint_state_pg
    .SharepointStatePgRepository.facts_pass_lock` — a transaction-scoped
    advisory lock, released automatically when its owning transaction ends,
    so a killed worker can never leave a connection's facts pass stuck
    locked.

    DuckDB (frozen backend, single-process by construction): a plain
    per-(connection, partition) ``threading.Lock``, non-blocking —
    sufficient on a backend that never spans more than one process.
    """
    from src.repositories import use_pg
    from src.repositories.sharepoint_state_pg import FactsPassLocked

    if use_pg():
        with _pg_repo().facts_pass_lock(connection_id, partition=partition):
            yield
        return

    lock = _file_backend_lock(connection_id, partition)
    if not lock.acquire(blocking=False):
        raise FactsPassLocked(f"a facts-extraction pass is already running for connection {connection_id!r}")
    try:
        yield
    finally:
        lock.release()


def any_facts_pass_running(connection_id: str) -> bool:
    """Whether ANY facts-extraction pass for this connection currently
    holds :func:`facts_pass_lock` — the un-partitioned whole-connection
    lock, OR any single partition of a fanned-out one (TCRD-296 gap #67).
    Used by ``facts_extraction.reset_no_claims_ledger_entries`` to refuse
    while a pass could be concurrently upserting the same ledger it is
    about to mutate.
    """
    from src.repositories import use_pg

    if not use_pg():
        with _facts_locks_guard:
            for key, lock in _facts_locks.items():
                if (key == connection_id or key.startswith(f"{connection_id}:")) and lock.locked():
                    return True
        return False
    return _pg_repo().any_facts_pass_running(connection_id)


def merge_docs(kind: str, connection_id: str, *, set_entries: Dict[str, Any], removed: Iterable[str] = ()) -> None:
    """Per-document MERGE write into this connection's ``kind`` ledger's
    ``docs`` sub-object — the partitioned-facts-pass persistence primitive
    (TCRD-296 gap #67). Unlike :func:`put`, which REPLACES the whole
    payload, this touches only the given document keys, so two partitions
    of the SAME connection's facts pass can each persist their own
    progress without one clobbering the other's (see
    ``connectors.sharepoint.facts_extraction._PartitionDocsLedger`` /
    ``_persist_facts_docs``, the callers).

    A no-op when both ``set_entries`` and ``removed`` are empty — no
    write, no row created for a connection that has never been touched.

    Postgres: an atomic ``jsonb`` merge in ONE statement
    (:meth:`~src.repositories.sharepoint_state_pg.SharepointStatePgRepository
    .merge_docs`) — safe under concurrent callers because each is a single
    row-locked ``UPDATE``, never a read-modify-write round trip through
    Python.

    DuckDB (frozen backend, single-process by construction): a plain
    read-modify-write of the JSON file — safe because the ONLY way two
    writers could race here is two partitions of the same connection, and
    partitioning only ever produces true concurrency on Postgres (a
    DuckDB-backed instance's job queue is single-process, so at most one
    partition is ever actually running at a time).
    """
    set_entries = dict(set_entries or {})
    removed_list = list(removed)
    if not set_entries and not removed_list:
        return

    from src.repositories import use_pg

    if not use_pg():
        current = _read_file(kind, connection_id) or {}
        docs = current.setdefault("docs", {})
        docs.update(set_entries)
        for key in removed_list:
            docs.pop(key, None)
        current.setdefault("version", 1)
        _write_file(kind, connection_id, current)
        return
    _pg_repo().merge_docs(connection_id, kind, set_entries=set_entries, removed=removed_list)
