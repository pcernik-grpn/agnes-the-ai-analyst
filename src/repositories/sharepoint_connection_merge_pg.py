"""Postgres-only repository backing "fold split connections back together"
(``POST /api/admin/sharepoint/connections/{id}/splits/merge`` — see
``app/api/admin_sharepoint.py::merge_split_connections`` for the full
contract).

A large site manually split across several sibling SharePoint connections
(each with its own folder scopes and, until ``POST …/collections/
consolidate`` existed, its own collection) can be folded back into ONE
connection once the split is no longer needed. Collection folding is
delegated wholesale to :class:`~src.repositories.
sharepoint_collection_consolidation_pg.SharePointCollectionConsolidationPgRepository`
and run-history re-pointing to
:meth:`~src.repositories.extraction_runs_pg.ExtractionRunsPgRepository.repoint_connection`
— this repository's own job is strictly the ONE piece those two cannot do:
union the per-connection crawl/facts bookkeeping in
``sharepoint_connection_state`` (``connectors/sharepoint/state_store.py``'s
module docstring — ``delta_links``/``ctags``/``failed_items``/
``empty_items`` for ``kind='crawl'``, ``docs`` for ``kind='facts'``) so the
merged connection resumes incrementally instead of re-downloading the site.

Collision handling — a genuinely split site has DISJOINT scopes across
siblings, so these keys should never collide in practice; this exists for
the anomalous case (overlapping scopes, or a repeat merge) rather than the
common one:

* ``delta_links`` (state_key -> a raw Graph deltaLink URL) and ``ctags``
  (stable_id -> a raw cTag string) carry NO per-entry freshness signal —
  neither is a timestamped envelope, just a resume cursor. On a genuine
  value collision the TARGET's own entry wins deterministically: the
  target is the connection that keeps crawling after the merge, so
  preferring its own resume point is the safe default (worst case: a
  handful of items get walked again on the next crawl, never lost).
* ``failed_items``/``empty_items`` (stable_id -> ``{..., last_failed_at |
  last_seen_at}``) DO carry a per-entry ISO-8601 timestamp — the newer
  entry wins.
* ``docs`` (the facts ledger, file id -> ``{status, ..., at}``) prefers
  ``status == "done"`` over any other status regardless of timestamp (a
  successfully extracted document beats a failed/in-flight one even if the
  failure is "newer"), then falls back to the newer ``at`` when both sides
  agree on done-ness.

An identical value on both sides is never reported as a conflict — only a
genuine disagreement is.

PG-first ratchet (A3): brand-new app-state surface, Postgres-only by
construction — ``sharepoint_connection_state`` (the table this reads/
writes) has no DuckDB sibling itself, so this repository can never resolve
on a DuckDB-backed instance (``RequiresPostgresBackend``, translated to a
typed ``501`` by ``app/main.py``).

``ctags``/``failed_items``/``empty_items`` for ``kind='crawl'`` moved out of
``sharepoint_connection_state.payload`` into their own per-file table,
``sharepoint_crawl_items`` (migration ``0110_sharepoint_crawl_items`` — see
that migration's docstring for why: a checkpoint rewriting the whole blob
orphaned tens of megabytes of TOAST chunks per write). :meth:`~
SharePointConnectionMergePgRepository._read` transparently overlays the
per-file table's rows onto whatever those three keys still hold on the
blob (a sibling not yet re-crawled since the split shipped can still carry
them there — see ``connectors.sharepoint.state_store.crawl_items_get``'s
own "old shape, new shape" migration), so :func:`_merge_state` below needs
no change at all: it still merges three plain ``stable_id -> ...`` dicts,
regardless of which table they were assembled from. :meth:`~
SharePointConnectionMergePgRepository._write` writes the merged result
back to the per-file table (a full replace — this repository always writes
the deterministic union of target + siblings, never an incremental delta,
so there is nothing to preserve from the previous rows) and keeps those
three keys OUT of the blob it writes, same as a normal crawl checkpoint
now does.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

#: The four ``kind='crawl'`` dict-shaped keys this repo unions. Every other
#: key on a crawl payload (there are none today beyond these four — see
#: ``connectors.sharepoint.crawler.load_state``) is carried through from the
#: target's own payload unchanged.
_CRAWL_DICT_KEYS = ("delta_links", "ctags", "failed_items", "empty_items")


def _decode(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except (ValueError, TypeError):
            return {}
    return dict(payload or {})


def _merge_flat(
    target: Dict[str, Any], sibling: Dict[str, Any], *, kind: str
) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """Union two flat ``key -> raw string`` dicts (``delta_links``/
    ``ctags``) with no per-entry freshness signal. The target's own value
    always wins a genuine collision — see the module docstring."""
    merged = dict(target)
    conflicts: List[Dict[str, str]] = []
    for key, value in sibling.items():
        if key in merged and merged[key] != value:
            conflicts.append({"kind": kind, "key": key, "resolution": "kept_target"})
            continue
        merged[key] = value
    return merged, conflicts


def _merge_timestamped(
    target: Dict[str, Any], sibling: Dict[str, Any], *, kind: str, freshness_key: str
) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """Union two ``stable_id -> entry`` dicts (``failed_items``/
    ``empty_items``) where each entry carries an ISO-8601 timestamp under
    ``freshness_key`` — the newer entry wins a genuine collision. ISO-8601
    strings compare correctly lexicographically, so no parsing is needed."""
    merged = dict(target)
    conflicts: List[Dict[str, str]] = []
    for key, entry in sibling.items():
        if key not in merged:
            merged[key] = entry
            continue
        existing = merged[key]
        if existing == entry:
            continue
        existing_ts = str((existing or {}).get(freshness_key) or "")
        entry_ts = str((entry or {}).get(freshness_key) or "")
        if entry_ts > existing_ts:
            merged[key] = entry
            conflicts.append({"kind": kind, "key": key, "resolution": "kept_sibling_newer"})
        else:
            conflicts.append({"kind": kind, "key": key, "resolution": "kept_target_newer_or_tied"})
    return merged, conflicts


def _merge_docs(target: Dict[str, Any], sibling: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """Union the facts ledger (``docs``: file id -> ``{status, ..., at}``).
    ``status == "done"`` beats any other status regardless of timestamp;
    when both sides agree on done-ness, the newer ``at`` wins."""
    merged = dict(target)
    conflicts: List[Dict[str, str]] = []
    for key, entry in sibling.items():
        if key not in merged:
            merged[key] = entry
            continue
        existing = merged[key]
        if existing == entry:
            continue
        existing_done = (existing or {}).get("status") == "done"
        entry_done = (entry or {}).get("status") == "done"
        if entry_done and not existing_done:
            merged[key] = entry
            resolution = "kept_sibling_done"
        elif existing_done and not entry_done:
            resolution = "kept_target_done"
        else:
            existing_at = str((existing or {}).get("at") or "")
            entry_at = str((entry or {}).get("at") or "")
            if entry_at > existing_at:
                merged[key] = entry
                resolution = "kept_sibling_newer"
            else:
                resolution = "kept_target_newer_or_tied"
        conflicts.append({"kind": "docs", "key": key, "resolution": resolution})
    return merged, conflicts


def _merge_state(
    target_crawl: Dict[str, Any],
    target_facts: Dict[str, Any],
    siblings: List[Tuple[str, Dict[str, Any], Dict[str, Any]]],
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """Pure merge: fold every ``(sibling_id, crawl_payload, facts_payload)``
    in ``siblings`` (a caller-ordered, deterministic sequence) onto the
    target's own payloads, ONE sibling at a time so a collision between two
    SIBLINGS (not just sibling-vs-target) is caught on whichever is
    processed second. Returns ``(merged_crawl, merged_facts,
    diagnostics_by_sibling_id)`` — the SAME shape :meth:`~
    SharePointConnectionMergePgRepository.plan` and :meth:`~
    SharePointConnectionMergePgRepository.apply` both return, so a dry-run
    preview is provably identical to what the real merge does.
    """
    merged_crawl: Dict[str, Any] = dict(target_crawl)
    for key in _CRAWL_DICT_KEYS:
        merged_crawl.setdefault(key, {})
    merged_facts: Dict[str, Any] = dict(target_facts)
    merged_facts.setdefault("docs", {})

    diagnostics: Dict[str, Dict[str, Any]] = {}
    for sibling_id, sib_crawl, sib_facts in siblings:
        sib_delta = (sib_crawl or {}).get("delta_links") or {}
        sib_ctags = (sib_crawl or {}).get("ctags") or {}
        sib_failed = (sib_crawl or {}).get("failed_items") or {}
        sib_empty = (sib_crawl or {}).get("empty_items") or {}
        sib_docs = (sib_facts or {}).get("docs") or {}

        merged_crawl["delta_links"], dl_conflicts = _merge_flat(
            merged_crawl["delta_links"], sib_delta, kind="delta_links"
        )
        merged_crawl["ctags"], ct_conflicts = _merge_flat(merged_crawl["ctags"], sib_ctags, kind="ctags")
        merged_crawl["failed_items"], fi_conflicts = _merge_timestamped(
            merged_crawl["failed_items"], sib_failed, kind="failed_items", freshness_key="last_failed_at"
        )
        merged_crawl["empty_items"], ei_conflicts = _merge_timestamped(
            merged_crawl["empty_items"], sib_empty, kind="empty_items", freshness_key="last_seen_at"
        )
        merged_facts["docs"], docs_conflicts = _merge_docs(merged_facts["docs"], sib_docs)

        diagnostics[sibling_id] = {
            "crawl": {
                "delta_links_carried": len(sib_delta),
                "ctags_carried": len(sib_ctags),
                "failed_items_carried": len(sib_failed),
                "empty_items_carried": len(sib_empty),
                "conflicts": dl_conflicts + ct_conflicts + fi_conflicts + ei_conflicts,
            },
            "facts": {
                "docs_carried": len(sib_docs),
                "conflicts": docs_conflicts,
            },
        }
    return merged_crawl, merged_facts, diagnostics


class SharePointConnectionMergePgRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def _read(self, conn: Connection, connection_id: str, kind: str) -> Dict[str, Any]:
        row = (
            conn.execute(
                sa.text("SELECT payload FROM sharepoint_connection_state WHERE connection_id = :cid AND kind = :kind"),
                {"cid": connection_id, "kind": kind},
            )
            .mappings()
            .first()
        )
        payload = _decode(row["payload"]) if row is not None else {}
        if kind == "crawl":
            # Overlay the per-file table (module docstring) onto whatever
            # the blob still holds for these three keys — a sibling not
            # re-crawled since the split shipped can still carry them on
            # the blob; the per-file table wins a collision as the more
            # current shape.
            items = self._read_items(conn, connection_id, kind)
            payload = dict(payload)
            for field in ("ctags", "failed_items", "empty_items"):
                payload[field] = {**(payload.get(field) or {}), **items[field]}
        return payload

    def _read_items(self, conn: Connection, connection_id: str, kind: str) -> Dict[str, Dict[str, Any]]:
        """Mirrors ``SharepointCrawlItemsPgRepository.get_all`` — kept as
        its own small query (rather than instantiating that repository)
        because it must run on THIS caller's own ``conn``/transaction, not
        open a second one; see :meth:`_write_items` for the write-side
        half of the same constraint."""
        out: Dict[str, Dict[str, Any]] = {"ctags": {}, "failed_items": {}, "empty_items": {}}
        rows = conn.execute(
            sa.text(
                "SELECT stable_id, ctag, failed_entry, empty_entry FROM sharepoint_crawl_items "
                "WHERE connection_id = :cid AND kind = :kind"
            ),
            {"cid": connection_id, "kind": kind},
        ).mappings()
        for row in rows:
            stable_id = row["stable_id"]
            if row["ctag"] is not None:
                out["ctags"][stable_id] = row["ctag"]
            failed = _decode(row["failed_entry"]) if row["failed_entry"] is not None else None
            if failed is not None:
                out["failed_items"][stable_id] = failed
            empty = _decode(row["empty_entry"]) if row["empty_entry"] is not None else None
            if empty is not None:
                out["empty_items"][stable_id] = empty
        return out

    def _write_items(
        self,
        conn: Connection,
        connection_id: str,
        kind: str,
        *,
        ctags: Dict[str, Any],
        failed_items: Dict[str, Any],
        empty_items: Dict[str, Any],
    ) -> None:
        """Replace every per-file row for ``(connection_id, kind)`` with
        the merged union — a full delete-then-insert, not an incremental
        delta: :meth:`apply` always computes the deterministic union of
        target + siblings from scratch, so there is no previous-row state
        worth preserving, and a retried call converges to the identical
        result rather than double-applying anything."""
        conn.execute(
            sa.text("DELETE FROM sharepoint_crawl_items WHERE connection_id = :cid AND kind = :kind"),
            {"cid": connection_id, "kind": kind},
        )
        stable_ids = set(ctags) | set(failed_items) | set(empty_items)
        now = datetime.now(timezone.utc)
        for stable_id in stable_ids:
            conn.execute(
                sa.text(
                    "INSERT INTO sharepoint_crawl_items "
                    "(connection_id, kind, stable_id, ctag, failed_entry, empty_entry, updated_at) "
                    "VALUES (:cid, :kind, :sid, :ctag, CAST(:failed AS JSONB), CAST(:empty AS JSONB), :now)"
                ),
                {
                    "cid": connection_id,
                    "kind": kind,
                    "sid": stable_id,
                    "ctag": ctags.get(stable_id),
                    "failed": json.dumps(failed_items[stable_id]) if stable_id in failed_items else None,
                    "empty": json.dumps(empty_items[stable_id]) if stable_id in empty_items else None,
                    "now": now,
                },
            )

    def _write(self, conn: Connection, connection_id: str, kind: str, payload: Dict[str, Any]) -> None:
        payload = dict(payload)
        if kind == "crawl":
            self._write_items(
                conn,
                connection_id,
                kind,
                ctags=payload.pop("ctags", {}) or {},
                failed_items=payload.pop("failed_items", {}) or {},
                empty_items=payload.pop("empty_items", {}) or {},
            )
        conn.execute(
            sa.text(
                "INSERT INTO sharepoint_connection_state (connection_id, kind, payload, updated_at) "
                "VALUES (:cid, :kind, :payload, now()) "
                "ON CONFLICT (connection_id, kind) DO UPDATE SET "
                "  payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at"
            ),
            {"cid": connection_id, "kind": kind, "payload": json.dumps(payload)},
        )

    def plan(self, *, target_id: str, sibling_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Read-only preview: what :meth:`apply` WOULD carry over and which
        keys WOULD collide, without writing anything. ``sibling_ids`` order
        matters (see :func:`_merge_state`) — pass the same order to
        :meth:`apply` for the preview to match the real merge exactly."""
        with self._engine.connect() as conn:
            target_crawl = self._read(conn, target_id, "crawl")
            target_facts = self._read(conn, target_id, "facts")
            siblings = [(sid, self._read(conn, sid, "crawl"), self._read(conn, sid, "facts")) for sid in sibling_ids]
        _, _, diagnostics = _merge_state(target_crawl, target_facts, siblings)
        return diagnostics

    def apply(self, *, target_id: str, sibling_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Write the union of ``target_id``'s own crawl/facts state and
        every sibling's onto ``target_id`` — ONE transaction. Sibling rows
        are left UNTOUCHED (never cleared/deleted): a botched merge must
        stay inspectable and the union is idempotent by construction, so a
        retried call converges rather than double-counting (see the module
        docstring's collision rules). Returns the same diagnostics shape
        :meth:`plan` does.
        """
        with self._engine.begin() as conn:
            target_crawl = self._read(conn, target_id, "crawl")
            target_facts = self._read(conn, target_id, "facts")
            siblings = [(sid, self._read(conn, sid, "crawl"), self._read(conn, sid, "facts")) for sid in sibling_ids]
            merged_crawl, merged_facts, diagnostics = _merge_state(target_crawl, target_facts, siblings)
            self._write(conn, target_id, "crawl", merged_crawl)
            self._write(conn, target_id, "facts", merged_facts)
        return diagnostics
