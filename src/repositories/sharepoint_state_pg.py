"""Postgres-only repository for ``sharepoint_connection_state`` — the
per-connection crawl/facts bookkeeping the SharePoint pipeline used to keep
as a JSON file on the worker's local disk (see
``migrations/versions/0096_sharepoint_connection_state.py`` and
``src/models/sharepoint_state.py`` for why).

PG-first ratchet (A3): brand-new app-state surface added after the freeze,
so there is no DuckDB sibling — see ``docs/migrations.md`` -> "Adding a
PG-only feature". Reach this repo only through
``src.repositories.sharepoint_state_repo()`` — but note that
``connectors/sharepoint/state_store.py`` (the module every caller actually
uses) never resolves it while the active backend is DuckDB: unlike a route
that must fail clean with a ``501``, a crawl in flight must keep working
unchanged on a DuckDB-backed instance, so the seam checks ``use_pg()``
itself and falls back to the pre-existing JSON file instead of ever letting
``RequiresPostgresBackend`` reach the crawl.
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

import sqlalchemy as sa
from sqlalchemy.engine import Engine

#: Namespace ("class id") for the two-int form of the facts-pass advisory
#: lock (``pg_try_advisory_xact_lock(class_id, hashtext(connection_id))``)
#: — "SPLK" packed as a signed int32. Deliberately NOT the single-bigint
#: overload the whole-app leases in ``src/db_pg.py`` use
#: (``_SEED_LEASE_ID`` / ``_REBUILD_LEASE_ID`` / the migration lock): a
#: two-int key occupies a distinct corner of the same 64-bit advisory-lock
#: keyspace, so this can never collide with one of those fixed keys. Used
#: for the WHOLE-CONNECTION lock (``partition`` ``None``/``count <= 1``) —
#: unchanged from before TCRD-296 gap #67.
_FACTS_LOCK_CLASS_ID = 0x53504C4B

#: Namespace PREFIX for a PARTITIONED pass's own advisory lock (TCRD-296
#: gap #67). Each partition of one connection's fan-out locks
#: ``pg_try_advisory_xact_lock(hashtext(f"{_FACTS_PARTITION_LOCK_NS}:{cid}"),
#: index)`` — a CONNECTION-SPECIFIC classid (unlike the fixed
#: ``_FACTS_LOCK_CLASS_ID`` above) so distinct partitions of the SAME
#: connection get distinct ``objid``s and never contend, while
#: :meth:`any_facts_pass_running` can still answer "any partition of THIS
#: connection" with one ``pg_locks`` lookup filtered on that one classid —
#: filtering by ``objid`` alone would not work, since ``objid`` here is
#: just the small partition index and collides across unrelated
#: connections.
_FACTS_PARTITION_LOCK_NS = "sharepoint_facts_partition"


class FactsPassLocked(RuntimeError):
    """Another facts-extraction pass already holds this connection's lock.

    Raised by :meth:`SharepointStatePgRepository.facts_pass_lock` — and, on
    the DuckDB fallback, by the mirror lock in
    ``connectors.sharepoint.state_store`` — immediately rather than waited
    out: the design decision at both call sites
    (``connectors.sharepoint.facts_extraction``) is "the other pass already
    covers this corpus", not "queue behind it".
    """


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _decode(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except (ValueError, TypeError):
            return {}
    return dict(payload or {})


class SharepointStatePgRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get(self, connection_id: str, kind: str) -> Optional[Dict[str, Any]]:
        """The raw stored payload for ``(connection_id, kind)``, or ``None``
        when no row exists yet. Callers apply their own defaults — this
        repo only knows about bytes, not either module's state shape."""
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(
                        "SELECT payload FROM sharepoint_connection_state WHERE connection_id = :cid AND kind = :kind"
                    ),
                    {"cid": connection_id, "kind": kind},
                )
                .mappings()
                .first()
            )
        return _decode(row["payload"]) if row is not None else None

    def backlog_counts(self, connection_id: str, kind: str) -> Dict[str, int]:
        """``{"failed_items_count", "empty_items_count"}`` — the SIZE of the
        ``failed_items``/``empty_items`` backlogs (``connectors.sharepoint.
        crawler.load_state``'s docstrings), without ever pulling either
        (potentially thousand-entry) dict into Python.

        This is what the "Retry failed (N)"/"Retry empty (N)" buttons on
        the source card read, through a status poll that runs every few
        seconds — decoding the whole payload in the request path (as the
        one-off retry triggers themselves do, via ``load_state`` +
        ``len()``, cheap enough for a single admin click) would be a
        materially worse cost on a hot polling path. ``jsonb_object_keys``
        counts the object's OWN keys server-side; both backlogs are stored
        as ``stable_id -> entry`` maps, never lists, so this — not
        ``jsonb_array_length`` — is the right primitive.

        A never-crawled connection (no row for ``(connection_id, kind)``
        at all) and a row that predates one of these two keys both count
        as ``0``, not an error — a cheap, honest "nothing to retry".
        """
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(
                        "SELECT "
                        "  (SELECT count(*) FROM jsonb_object_keys(payload -> 'failed_items')) AS failed_items_count, "
                        "  (SELECT count(*) FROM jsonb_object_keys(payload -> 'empty_items')) AS empty_items_count "
                        "FROM sharepoint_connection_state WHERE connection_id = :cid AND kind = :kind"
                    ),
                    {"cid": connection_id, "kind": kind},
                )
                .mappings()
                .first()
            )
        if row is None:
            return {"failed_items_count": 0, "empty_items_count": 0}
        return {
            "failed_items_count": int(row["failed_items_count"] or 0),
            "empty_items_count": int(row["empty_items_count"] or 0),
        }

    def put(self, connection_id: str, kind: str, payload: Dict[str, Any]) -> None:
        """Upsert this connection's ``kind`` state — the crawl/facts
        checkpoint write."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO sharepoint_connection_state (connection_id, kind, payload, updated_at) "
                    "VALUES (:cid, :kind, :payload, :now) "
                    "ON CONFLICT (connection_id, kind) DO UPDATE SET "
                    "  payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at"
                ),
                {"cid": connection_id, "kind": kind, "payload": json.dumps(payload), "now": _now()},
            )

    def delete(self, connection_id: str, kind: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM sharepoint_connection_state WHERE connection_id = :cid AND kind = :kind"),
                {"cid": connection_id, "kind": kind},
            )

    def list_kinds(self, connection_id: str, prefix: str) -> List[str]:
        """Every ``kind`` this connection has a row for, starting with
        ``prefix`` — the shard-state discovery primitive (2026-09-03 auto-
        parallel-crawl design §4.2). ``list_kinds(cid, "crawl:")`` finds
        every per-delta-unit state row a resync (or the planner) needs to
        touch, without the caller having to already know how many shards
        exist or what their keys are. Order is not significant — callers
        that care sort for themselves."""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT kind FROM sharepoint_connection_state WHERE connection_id = :cid AND kind LIKE :prefix"
                    ),
                    {"cid": connection_id, "prefix": f"{prefix}%"},
                )
                .scalars()
                .all()
            )
        return [str(r) for r in rows]

    def import_if_absent(self, connection_id: str, kind: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Insert ``payload`` as this row's initial content ONLY if it does
        not already exist — the one-time legacy-file import
        (``connectors.sharepoint.state_store``'s module docstring).

        Returns whatever ends up stored: ``payload`` itself on a clean
        insert, or the row a concurrent importer/writer already put there
        on a losing race. Two workers reading the same never-before-seen
        connection at once must never let the loser's stale file snapshot
        clobber the winner's already-advancing state — ``ON CONFLICT DO
        NOTHING`` plus a fallback read makes that race safe by
        construction rather than by timing.
        """
        with self._engine.begin() as conn:
            inserted = (
                conn.execute(
                    sa.text(
                        "INSERT INTO sharepoint_connection_state (connection_id, kind, payload, updated_at) "
                        "VALUES (:cid, :kind, :payload, :now) "
                        "ON CONFLICT (connection_id, kind) DO NOTHING "
                        "RETURNING payload"
                    ),
                    {"cid": connection_id, "kind": kind, "payload": json.dumps(payload), "now": _now()},
                )
                .mappings()
                .first()
            )
            if inserted is not None:
                return _decode(inserted["payload"])
            existing = (
                conn.execute(
                    sa.text(
                        "SELECT payload FROM sharepoint_connection_state WHERE connection_id = :cid AND kind = :kind"
                    ),
                    {"cid": connection_id, "kind": kind},
                )
                .mappings()
                .first()
            )
        return _decode(existing["payload"]) if existing is not None else dict(payload)

    @contextlib.contextmanager
    def facts_pass_lock(self, connection_id: str, *, partition: Optional[Tuple[int, int]] = None) -> Iterator[None]:
        """Non-blocking lock serializing facts-extraction passes — the
        crawl's chained tail and the standalone operator trigger can both
        reach the same connection at once, and only one may actually run
        (``connectors.sharepoint.facts_extraction``'s module docstring).
        Raises :class:`FactsPassLocked` immediately rather than waiting.

        ``partition`` (``(index, count)``, TCRD-296 gap #67) — ``None`` or
        ``count <= 1`` (every caller before this feature existed) locks
        the WHOLE connection under :data:`_FACTS_LOCK_CLASS_ID`, byte-
        identical to today. ``count > 1`` locks only THIS partition's own
        slot, under a CONNECTION-SPECIFIC classid
        (:data:`_FACTS_PARTITION_LOCK_NS`) with ``objid = index`` — so
        distinct partitions of the SAME connection never contend, and
        :meth:`any_facts_pass_running` can still find every currently-held
        partition lock for one connection with a single ``pg_locks``
        lookup (see that constant's own docstring for why a connection-
        specific classid, not the fixed one, is what makes that possible).

        A transaction-scoped Postgres advisory lock, held for the lifetime
        of the ``with`` block on its OWN connection (never the caller's):
        released automatically when that transaction ends — commit,
        rollback, or the connection dying with the worker — so a killed
        worker can never leave a connection's facts pass stuck locked.
        Held for as long as the caller's ``with`` block runs, which for a
        standalone pass can be up to its own timeout (default one hour) —
        acceptable here because at most one pass runs per (connection,
        partition) at a time by construction, so this is one held
        connection per currently-running pass, not per request.
        """
        conn = self._engine.connect()
        trans = conn.begin()
        acquired = False
        try:
            if partition is None or partition[1] <= 1:
                acquired = bool(
                    conn.execute(
                        sa.text("SELECT pg_try_advisory_xact_lock(:class_id, hashtext(:cid))"),
                        {"class_id": _FACTS_LOCK_CLASS_ID, "cid": connection_id},
                    ).scalar()
                )
            else:
                index, _count = partition
                acquired = bool(
                    conn.execute(
                        sa.text("SELECT pg_try_advisory_xact_lock(hashtext(:ns), :idx)"),
                        {"ns": f"{_FACTS_PARTITION_LOCK_NS}:{connection_id}", "idx": index},
                    ).scalar()
                )
            if not acquired:
                raise FactsPassLocked(f"a facts-extraction pass is already running for connection {connection_id!r}")
            yield
        finally:
            if acquired:
                trans.commit()
            else:
                trans.rollback()
            conn.close()

    def any_facts_pass_running(self, connection_id: str) -> bool:
        """Whether ANY facts-extraction pass for this connection currently
        holds :meth:`facts_pass_lock` — the un-partitioned whole-connection
        lock (:data:`_FACTS_LOCK_CLASS_ID`, ``objid = hashtext(connection_id)``),
        OR any single partition of a fanned-out one
        (:data:`_FACTS_PARTITION_LOCK_NS`, any ``objid``) — see
        :func:`connectors.sharepoint.state_store.any_facts_pass_running`,
        the facade every caller actually uses.

        A plain ``pg_locks`` read, not itself a lock: this answers "right
        now", accepting the narrow race a check-then-act pattern always
        has (a partition could start immediately after this returns
        ``False``) — the SAME best-effort, immediate-refuse posture every
        other use of this lock already takes.
        """
        with self._engine.connect() as conn:
            legacy = bool(
                conn.execute(
                    sa.text(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks "
                        "WHERE locktype = 'advisory' AND classid = :cls AND objid = hashtext(:cid))"
                    ),
                    {"cls": _FACTS_LOCK_CLASS_ID, "cid": connection_id},
                ).scalar()
            )
            if legacy:
                return True
            partitioned = bool(
                conn.execute(
                    sa.text(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND classid = hashtext(:ns))"
                    ),
                    {"ns": f"{_FACTS_PARTITION_LOCK_NS}:{connection_id}"},
                ).scalar()
            )
            return partitioned

    def merge_docs(self, connection_id: str, kind: str, *, set_entries: Dict[str, Any], removed: List[str]) -> None:
        """Atomic per-document ``jsonb`` merge into ``payload -> 'docs'`` —
        see ``connectors.sharepoint.state_store.merge_docs``'s docstring
        for why this exists instead of :meth:`put`'s whole-payload
        overwrite (TCRD-296 gap #67).

        A row for ``(connection_id, kind)`` is upserted first (an empty
        ``{"version": 1, "docs": {}}`` payload, ``DO NOTHING`` if one
        already exists) so the merge below is always a single, simple
        ``UPDATE`` against an existing row rather than a conditional
        insert-or-update of the merge itself. The ``docs`` value is then
        rebuilt in ONE expression — ``||`` folds in ``set_entries`` (a
        shallow merge: a changed key's value is REPLACED, never deep-
        merged), then one ``-`` per ``removed`` key (jsonb has no
        multi-key subtraction for a single call) — inside a single
        ``jsonb_set`` so every OTHER top-level field on the payload
        (``version``, ``facts_continuation_chain``, ...) is left
        untouched. The whole thing is one row-locked ``UPDATE`` statement,
        which is what makes two partitions calling this concurrently for
        DIFFERENT keys safe: Postgres serializes the two statements
        against the same row, and neither ever re-reads-then-writes the
        other's already-committed keys.

        A no-op — no row created, no statement issued — when both
        ``set_entries`` and ``removed`` are empty: a connection this was
        never called for stays absent, never gets an empty placeholder row.
        """
        if not set_entries and not removed:
            return
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO sharepoint_connection_state (connection_id, kind, payload, updated_at) "
                    "VALUES (:cid, :kind, :empty, :now) "
                    "ON CONFLICT (connection_id, kind) DO NOTHING"
                ),
                {
                    "cid": connection_id,
                    "kind": kind,
                    "empty": json.dumps({"version": 1, "docs": {}}),
                    "now": _now(),
                },
            )
            docs_expr = "COALESCE(payload -> 'docs', '{}'::jsonb)"
            params: Dict[str, Any] = {"cid": connection_id, "kind": kind, "now": _now()}
            if set_entries:
                docs_expr = f"({docs_expr} || CAST(:set_json AS jsonb))"
                params["set_json"] = json.dumps(set_entries)
            for i, key in enumerate(removed):
                docs_expr = f"({docs_expr} - :removed_{i})"
                params[f"removed_{i}"] = key
            conn.execute(
                sa.text(
                    "UPDATE sharepoint_connection_state "
                    f"SET payload = jsonb_set(payload, '{{docs}}', {docs_expr}), updated_at = :now "
                    "WHERE connection_id = :cid AND kind = :kind"
                ),
                params,
            )
