"""Completeness check: does the corpus really have everything Graph Search
says exists for this SharePoint connection?

Design origin (TCRD-296 synthesis item B.9): an operator's ad hoc script
compared Graph Search document counts per top-level folder against
``corpus_files`` rows per scope collection to answer "did we really get
everything?" after a crawl. This module is that script, promoted to a
read-only admin surface (``app.api.admin_extraction``'s ``GET …/extraction/
completeness``) — one row per confirmed scope, plus (only when the
connection has exactly one "whole drive" scope) one row per top-level
folder under it.

One row's shape::

    {kind, scope_id, parent_scope_id, label, collection_id,
     expected, indexed, rejected, failed, empty, skipped_unsupported,
     oversize, gap, status}

``status`` is derived from ``expected``/``indexed`` plus the four reason
counts (never stored, always recomputed):

* ``"unknown"`` — ``expected`` could not be resolved at all (a "site" scope
  spans multiple drives with no single ``web_url`` to count against, or the
  Graph lookup itself failed) — never rendered as 0 or as complete.
* ``"complete"`` — ``indexed >= expected``: nothing to explain.
* ``"accounted"`` — ``indexed < expected``, but ``failed + empty +
  skipped_unsupported + oversize`` covers the rest: every missing document
  has a recorded reason.
* ``"missing"`` — an unexplained gap remains after every known reason is
  applied. The safe failure mode: every attribution rule below that cannot
  be certain UNDER-counts a reason rather than over-counts it, which can
  only ever push a row from ``accounted`` toward ``missing``, never the
  other way — a false "missing" costs an admin a look; a false "accounted"
  hides a real gap.

**Attribution rules for the four reason counts, and their honest limits.**
``failed``/``empty`` come from ``sharepoint_connection_state(kind="crawl")``
(``connectors.sharepoint.state_store``)'s ``failed_items``/``empty_items`` —
cumulative, itemized dicts keyed by stable id, each entry carrying the
crawler's own ``state_key`` (``drive_id`` or ``drive_id:root_item_id``) and,
usually, a ``path``. ``skipped_unsupported`` and ``oversize`` are NOT
cumulative: the crawler only ever persists them inside ``last_run`` (the
most recently FINISHED run's full ``CrawlStats.report()``), itemized in
``skipped_items`` / ``skipped_oversize.largest`` — both capped samples, not
a complete history.

* **A single-scope connection** (the common case, and the only case that
  also gets folder rows): every persisted entry trivially belongs to that
  one scope, so the scope row's ``failed``/``empty``/``skipped_unsupported``
  are EXACT (``len(failed_items)``, ``len(empty_items)``,
  ``last_run["skipped_unsupported"]`` — the uncapped run total, not the
  capped list length), and ``oversize`` is the exact
  ``last_run["skipped_oversize"]["files"]``.
* **A multi-scope connection**: ``failed``/``empty`` per scope match on the
  entry's own ``state_key`` (exact, for "drive"/"folder" scope kinds —
  "site" scopes span multiple drives and are left at 0 here, still counted
  at the connection level). ``skipped_unsupported`` per scope matches the
  capped ``skipped_items`` sample's own ``drive_id`` field (approximate: a
  folder scope sharing a drive with another scope over-attributes, and the
  cap under-attributes). ``oversize`` per scope is always 0 — the stored
  entries carry no drive/scope attribution at all — but the connection
  TOTAL still reports the true run-level count via
  ``last_run["skipped_oversize"]["files"]``, never silently dropped.
* **Folder rows** (single "drive"-kind scope only) bucket the SAME
  ``failed_items``/``empty_items`` entries (exact — one drive, one scope) by
  the first ``/``-delimited segment of their stored ``path`` (``None`` when
  the scope is anonymize-marked — bucketed under ``""``, the same
  "unattributed at this granularity, still counted at the scope" rule).
  ``skipped_unsupported``/``oversize`` per folder come ONLY from
  ``last_run``'s capped itemized samples, so a folder breakdown can
  under-count relative to the scope row's own exact totals when a sample
  was truncated (``last_run["skipped_items_truncated"]`` /
  the oversize sample's own 200-entry cap) — surfaced via this module's
  ``caveats`` list, never silently.

"expected" is Graph Search's ``IsDocument:1`` count under one item's
``web_url`` (:func:`connectors.sharepoint.graph_client.
search_document_count`, the SAME mechanism ``app.api.admin_sharepoint``'s
site-split planner uses), narrowed to convertible formats via ``AND NOT
(fileextension:… OR …)`` built from the crawler's own
``_unsupported_extensions()`` (the admin-configurable superset of
``_DEFAULT_UNSUPPORTED_EXTENSIONS``) — so "expected" only counts documents a
full crawl would ever attempt, matching what could ever land in the corpus.
Never a delta walk — see :func:`connectors.sharepoint.graph_client.
search_document_count`'s own docstring for why.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Same one-Graph-call-per-folder throttle the site-split planner uses
#: (``app.api.admin_sharepoint._SPLIT_COUNT_CONCURRENCY``) — Graph Search has
#: no batch form, and a connection can have dozens of scopes/folders.
_COUNT_CONCURRENCY = 8


def _status_for(expected: Optional[int], indexed: int, accounted: int) -> tuple:
    """``(gap, status)`` — see the module docstring's status vocabulary."""
    if expected is None:
        return None, "unknown"
    missing_from_index = expected - indexed
    if missing_from_index <= 0:
        return 0, "complete"
    gap = missing_from_index - accounted
    return gap, ("accounted" if gap <= 0 else "missing")


def _row(
    *,
    kind: str,
    scope_id: Optional[str],
    parent_scope_id: Optional[str],
    label: str,
    collection_id: Optional[str],
    expected: Optional[int],
    indexed: int,
    rejected: int,
    failed: int,
    empty: int,
    skipped_unsupported: int,
    oversize: int,
) -> Dict[str, Any]:
    accounted = failed + empty + skipped_unsupported + oversize
    gap, status = _status_for(expected, indexed, accounted)
    return {
        "kind": kind,
        "scope_id": scope_id,
        "parent_scope_id": parent_scope_id,
        "label": label,
        "collection_id": collection_id,
        "expected": expected,
        "indexed": indexed,
        "rejected": rejected,
        "failed": failed,
        "empty": empty,
        "skipped_unsupported": skipped_unsupported,
        "oversize": oversize,
        "gap": gap,
        "status": status,
    }


def _top_folder(path: Optional[str]) -> str:
    if not path:
        return ""
    if "/" in path:
        return path.split("/", 1)[0]
    return ""


def _bucket_by_top_folder(paths: List[Optional[str]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for path in paths:
        key = _top_folder(path)
        out[key] = out.get(key, 0) + 1
    return out


def _exact_reason_counts(state: Dict[str, Any]) -> Dict[str, int]:
    """The connection-wide TRUE totals for all four reasons — always exact,
    regardless of how many scopes exist or whether any entry could be
    attributed to one of them. Used for the totals row (so a reason can
    never silently vanish just because per-scope attribution is
    ambiguous) and for a single-scope connection's own scope row (where
    "connection-wide" and "this scope" are the same set by construction)."""
    failed_items = state.get("failed_items") or {}
    empty_items = state.get("empty_items") or {}
    last_run = state.get("last_run") or {}
    skipped_oversize = last_run.get("skipped_oversize") or {}
    return {
        "failed": len(failed_items) if isinstance(failed_items, dict) else 0,
        "empty": len(empty_items) if isinstance(empty_items, dict) else 0,
        "skipped_unsupported": int(last_run.get("skipped_unsupported") or 0),
        "oversize": int(skipped_oversize.get("files") or 0),
    }


def _scoped_reason_counts(
    state: Dict[str, Any], *, state_key: Optional[str], drive_id: Optional[str]
) -> Dict[str, int]:
    """Best-effort per-scope attribution for a MULTI-scope connection — see
    the module docstring's attribution rules. ``oversize`` is always 0 here
    (unattributable at scope granularity); the connection total still
    carries the true count via :func:`_exact_reason_counts`."""
    failed_items = state.get("failed_items") or {}
    empty_items = state.get("empty_items") or {}
    last_run = state.get("last_run") or {}
    skipped_items = last_run.get("skipped_items") or []

    failed = sum(
        1
        for e in failed_items.values()
        if isinstance(e, dict) and state_key is not None and e.get("state_key") == state_key
    )
    empty = sum(
        1
        for e in empty_items.values()
        if isinstance(e, dict) and state_key is not None and e.get("state_key") == state_key
    )
    skipped_unsupported = sum(
        1 for e in skipped_items if isinstance(e, dict) and drive_id is not None and e.get("drive_id") == drive_id
    )
    return {"failed": failed, "empty": empty, "skipped_unsupported": skipped_unsupported, "oversize": 0}


def _folder_reason_counts(state: Dict[str, Any], *, folder_name: str) -> Dict[str, int]:
    """Per-top-level-folder attribution — only reachable when the connection
    has exactly one "drive" scope, so every persisted entry already belongs
    to it; this buckets by path alone. ``skipped_unsupported``/``oversize``
    come from ``last_run``'s own (possibly capped) itemized samples — see
    the module docstring's caveat."""
    failed_items = state.get("failed_items") or {}
    empty_items = state.get("empty_items") or {}
    last_run = state.get("last_run") or {}
    skipped_items = last_run.get("skipped_items") or []
    oversize_largest = (last_run.get("skipped_oversize") or {}).get("largest") or []

    failed_paths = [e.get("path") for e in failed_items.values() if isinstance(e, dict)]
    empty_paths = [e.get("path") for e in empty_items.values() if isinstance(e, dict)]
    skipped_paths = [e.get("path") for e in skipped_items if isinstance(e, dict)]
    oversize_paths = [e.get("path") for e in oversize_largest if isinstance(e, dict)]

    return {
        "failed": _bucket_by_top_folder(failed_paths).get(folder_name, 0),
        "empty": _bucket_by_top_folder(empty_paths).get(folder_name, 0),
        "skipped_unsupported": _bucket_by_top_folder([e.get("path") for e in skipped_paths]).get(folder_name, 0),
        "oversize": _bucket_by_top_folder([e.get("path") for e in oversize_paths]).get(folder_name, 0),
    }


def _sum_rows(rows: List[Dict[str, Any]], key: str) -> int:
    return sum(int(r[key]) for r in rows if r.get(key) is not None)


async def compute_completeness(
    connection: Dict[str, Any],
    *,
    min_modified: Optional[str],
    token: Optional[str],
) -> Dict[str, Any]:
    """The full completeness report for one connection: one row per
    confirmed scope, folder rows when applicable, and a totals row.
    ``token`` is a pre-resolved Graph access token — the caller (the HTTP
    endpoint) owns cert resolution and its own 409/502 error shape, exactly
    like ``app.api.admin_sharepoint._compute_split_plan``. ``None`` is only
    valid when the connection has no confirmed scope at all (nothing to
    count against, so no Graph call is ever attempted); a connection WITH
    scopes always gets a resolved token from its caller.
    """
    from connectors.sharepoint.crawler import _scope_kind, _unsupported_extensions
    from connectors.sharepoint.graph_client import (
        SharePointGraphError,
        get_item_web_url,
        list_root_children_with_url,
        search_document_count,
    )
    from connectors.sharepoint.state_store import get as state_get
    from src.repositories import corpus_files_repo

    connection_id = connection["id"]
    scopes = [
        s
        for s in ((connection.get("config") or {}).get("scopes") or [])
        if isinstance(s, dict) and s.get("collection_id")
    ]
    if scopes:
        assert token is not None, "compute_completeness: token is required when the connection has confirmed scopes"
    state = state_get("crawl", connection_id) or {}
    excluded_extensions = _unsupported_extensions()
    semaphore = asyncio.Semaphore(_COUNT_CONCURRENCY)
    files_repo = corpus_files_repo()
    caveats: List[str] = []

    async def _expected_for(drive_id: Optional[str], item_id: Optional[str]) -> Optional[int]:
        if not drive_id:
            return None
        async with semaphore:
            web_url = await get_item_web_url(token, drive_id, item_id)
            if not web_url:
                return None
            return await search_document_count(
                token, web_url, min_modified=min_modified, exclude_extensions=excluded_extensions
            )

    single_scope = len(scopes) == 1
    rows: List[Dict[str, Any]] = []
    any_unknown = False

    for scope in scopes:
        source_scope_id = str(scope.get("source_scope_id") or "")
        drive_id = scope.get("drive_id")
        kind = _scope_kind(source_scope_id)
        item_id = source_scope_id if kind == "folder" else None
        if kind == "site":
            expected: Optional[int] = None
            caveats.append(
                f"scope {source_scope_id!r} spans a whole SharePoint site (multiple drives) — "
                "its 'expected' count could not be resolved to a single web_url."
            )
        else:
            expected = await _expected_for(drive_id, item_id)
            if expected is None:
                caveats.append(f"scope {source_scope_id!r}: the Graph document count could not be read.")

        collection_id = scope["collection_id"]
        status_counts = files_repo.status_counts_for_corpora([collection_id]).get(collection_id, {})
        indexed = int(status_counts.get("indexed", 0))
        rejected = int(status_counts.get("rejected", 0))

        if single_scope:
            reasons = _exact_reason_counts(state)
        else:
            state_key: Optional[str]
            if kind == "folder" and drive_id:
                state_key = f"{drive_id}:{source_scope_id}"
            elif kind == "drive" and drive_id:
                state_key = drive_id
            else:
                state_key = None
            reasons = _scoped_reason_counts(state, state_key=state_key, drive_id=drive_id if kind != "site" else None)

        rows.append(
            _row(
                kind="scope",
                scope_id=source_scope_id,
                parent_scope_id=None,
                label=scope.get("display_path") or source_scope_id,
                collection_id=collection_id,
                expected=expected,
                indexed=indexed,
                rejected=rejected,
                **reasons,
            )
        )
        if expected is None:
            any_unknown = True

    folder_rows: List[Dict[str, Any]] = []
    if (
        single_scope
        and _scope_kind(str(scopes[0].get("source_scope_id") or "")) == "drive"
        and scopes[0].get("drive_id")
    ):
        scope = scopes[0]
        drive_id = scope["drive_id"]
        collection_id = scope["collection_id"]
        try:
            children = await list_root_children_with_url(token, drive_id)
        except SharePointGraphError as exc:
            caveats.append(f"folder listing for drive {drive_id!r} failed: {exc}")
            children = []
        folder_items = [c for c in children if c.get("is_folder")]
        folder_status = files_repo.top_folder_status_counts(collection_id)
        last_run = state.get("last_run") or {}
        if last_run.get("skipped_items_truncated") or last_run.get("failed_items_truncated"):
            caveats.append(
                "per-folder skipped_unsupported/oversize/failed counts are derived from the last "
                "completed run's own (capped) sample — they may under-count relative to the scope row."
            )

        async def _folder_expected(item: Dict[str, Any]) -> Optional[int]:
            async with semaphore:
                web_url = item.get("web_url")
                if not web_url:
                    return None
                return await search_document_count(
                    token, web_url, min_modified=min_modified, exclude_extensions=excluded_extensions
                )

        expecteds = await asyncio.gather(*[_folder_expected(item) for item in folder_items])
        for item, folder_expected in zip(folder_items, expecteds):
            name = item["name"]
            status_counts = folder_status.get(name, {})
            reasons = _folder_reason_counts(state, folder_name=name)
            folder_rows.append(
                _row(
                    kind="folder",
                    scope_id=item["id"],
                    parent_scope_id=str(scope.get("source_scope_id") or ""),
                    label=name,
                    collection_id=collection_id,
                    expected=folder_expected,
                    indexed=int(status_counts.get("indexed", 0)),
                    rejected=int(status_counts.get("rejected", 0)),
                    **reasons,
                )
            )

    exact_totals = _exact_reason_counts(state)
    total_expected = None if any_unknown or not scopes else _sum_rows(rows, "expected")
    total_row = _row(
        kind="total",
        scope_id=None,
        parent_scope_id=None,
        label="Total",
        collection_id=None,
        expected=total_expected,
        indexed=_sum_rows(rows, "indexed"),
        rejected=_sum_rows(rows, "rejected"),
        **exact_totals,
    )

    return {
        "connection_id": connection_id,
        "min_modified": min_modified,
        "rows": rows + folder_rows,
        "total": total_row,
        "caveats": caveats,
    }
