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
from typing import Any, Dict, Iterator, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

#: Namespace ("class id") for the two-int form of the facts-pass advisory
#: lock (``pg_try_advisory_xact_lock(class_id, hashtext(connection_id))``)
#: — "SPLK" packed as a signed int32. Deliberately NOT the single-bigint
#: overload the whole-app leases in ``src/db_pg.py`` use
#: (``_SEED_LEASE_ID`` / ``_REBUILD_LEASE_ID`` / the migration lock): a
#: two-int key occupies a distinct corner of the same 64-bit advisory-lock
#: keyspace, so this can never collide with one of those fixed keys.
_FACTS_LOCK_CLASS_ID = 0x53504C4B


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
    def facts_pass_lock(self, connection_id: str) -> Iterator[None]:
        """Non-blocking, per-connection lock serializing facts-extraction
        passes — the crawl's chained tail and the standalone operator
        trigger can both reach the same connection at once, and only one
        may actually run (``connectors.sharepoint.facts_extraction``'s
        module docstring). Raises :class:`FactsPassLocked` immediately
        rather than waiting.

        A transaction-scoped Postgres advisory lock, held for the lifetime
        of the ``with`` block on its OWN connection (never the caller's):
        released automatically when that transaction ends — commit,
        rollback, or the connection dying with the worker — so a killed
        worker can never leave a connection's facts pass stuck locked.
        Held for as long as the caller's ``with`` block runs, which for a
        standalone pass can be up to its own timeout (default one hour) —
        acceptable here because at most one pass runs per connection at a
        time by construction, so this is one held connection per
        currently-running pass, not per request.
        """
        conn = self._engine.connect()
        trans = conn.begin()
        acquired = False
        try:
            acquired = bool(
                conn.execute(
                    sa.text("SELECT pg_try_advisory_xact_lock(:class_id, hashtext(:cid))"),
                    {"class_id": _FACTS_LOCK_CLASS_ID, "cid": connection_id},
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
