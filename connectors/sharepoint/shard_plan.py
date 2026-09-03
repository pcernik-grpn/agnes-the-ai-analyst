"""Shard planner for the automatic parallel site crawl (2026-09-03
auto-parallel-crawl design §4.1) — Task 3 of the implementation plan.

Two halves, deliberately split so the packing algorithm is testable without
a mocked Graph client:

* :func:`plan_shards` — PURE. Given a drive's already-counted folder units,
  packs them into ``K`` shards plus one remainder shard. No I/O, no
  randomness: the same input always produces the same output (JSON-
  serializable), which is what lets a read-only preview and the actual
  sharded run agree without re-deriving anything from data that may have
  shifted between the two calls — the same guarantee
  ``app.api.admin_sharepoint._compute_split_plan`` already gives the manual
  split-plan preview.
* :func:`compute_shard_plan` — the Graph half. For each already-resolved
  ``DriveTarget`` (see ``connectors.sharepoint.crawler._drive_targets``):
  decides whether the drive needs sharding at all (its total is a single
  :func:`connectors.sharepoint.graph_client.search_document_count` against
  the drive root — a small/empty drive stays on the inline path), lists and
  counts its top-level folders, folds a still-over-target folder ONE level
  deeper (never further), and falls back from the Search signal to
  ``folder.childCount`` and finally to "one shard per top-level folder"
  when neither counting signal is available (design §4.1 point 4) — then
  hands the result to :func:`plan_shards`.

Reuses :func:`connectors.sharepoint.site_split.pack_folders_into_groups` —
the same greedy longest-processing-time-first packer the manual split-plan
preview already uses — for the balanced (Search / childCount) case.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any, Dict, List, Optional, Sequence

from connectors.sharepoint.site_split import pack_folders_into_groups

#: How many `search_document_count` (or child-listing) calls the planner
#: keeps in flight at once — same rationale, and same value, as
#: ``app.api.admin_sharepoint._SPLIT_COUNT_CONCURRENCY``.
_COUNT_CONCURRENCY = 8

#: Which counting signal a drive's folders were sized by — recorded on the
#: plan so the preview/UI can say "≈" honestly (design §6 "Search counts are
#: approximate").
SIGNAL_SEARCH = "search"
SIGNAL_CHILD_COUNT = "child_count"
SIGNAL_NONE = "none"


def plan_shards(
    units: Sequence[Dict[str, Any]],
    *,
    drive_id: str,
    target_docs: int,
    max_shards: int = 32,
    force_shard_count: Optional[int] = None,
    balance: bool = True,
) -> Dict[str, Any]:
    """Pack pre-resolved, pre-counted crawl units into ``K`` shards plus one
    remainder shard.

    Each ``unit``: ``{"path": <drive-relative path>, "item_id": <Graph item
    id>, "documents": <int>}`` — one crawl target's worth of work, already
    at the granularity it will be crawled at (a top-level folder, or one of
    its children when :func:`compute_shard_plan` folded it one level
    deeper). This function does not care which.

    ``K = min(ceil(total_documents / target_docs), max_shards)``, at least
    1, UNLESS ``force_shard_count`` overrides it (the "no counting signal"
    fallback — see :func:`compute_shard_plan`, which also passes
    ``balance=False`` in that case: with every unit's ``documents`` equal,
    the greedy packer degenerates to "everything in the first group" —
    ties never move the running minimum — so the fallback round-robins
    units across shards instead).

    Returns ``{"drive_id", "shards": [...], "expected_total": int}``. Every
    shard but the last is ``{"index", "label", "targets": [{"drive_id",
    "root_item_id", "state_key", "path"}], "expected": <summed documents>}``
    — one target per unit in that group. The LAST shard is always the
    REMAINDER: a single whole-drive target (``root_item_id=None``,
    ``state_key=drive_id``) with ``exclude_prefixes`` set to every packed
    unit's own path, so it covers loose root files and anything created
    after planning without re-crawling what the other shards already own
    (design §4.1 point 3). Its ``expected`` is always ``0`` — a remainder's
    true count is whatever a full drive enumeration finds beyond the packed
    folders, which this function has no way to know in advance.

    No I/O, no randomness — safe to call twice on the same input and diff
    the results (a preview endpoint's whole reason to exist).
    """
    total = sum(int(u.get("documents") or 0) for u in units)
    if force_shard_count is not None:
        k = max(1, min(int(force_shard_count), max_shards))
    elif target_docs and target_docs > 0 and total:
        k = max(1, min(math.ceil(total / target_docs), max_shards))
    else:
        k = 1

    if balance:
        groups = pack_folders_into_groups(list(units), k)
    else:
        groups = [{"folders": [], "documents": 0} for _ in range(k)]
        for i, unit in enumerate(units):
            bucket = groups[i % k]
            bucket["folders"].append(unit)
            bucket["documents"] += int(unit.get("documents") or 0)

    shards: List[Dict[str, Any]] = []
    for index, group in enumerate(groups, start=1):
        targets = [
            {
                "drive_id": drive_id,
                "root_item_id": unit["item_id"],
                "state_key": f"{drive_id}:{unit['item_id']}",
                "path": unit["path"],
            }
            for unit in group["folders"]
        ]
        shards.append(
            {
                "index": index,
                "label": f"part {index}/{k}",
                "targets": targets,
                "expected": group["documents"],
            }
        )

    exclude_prefixes = sorted({str(u["path"]) for u in units})
    shards.append(
        {
            "index": k + 1,
            "label": "remainder",
            "targets": [{"drive_id": drive_id, "root_item_id": None, "state_key": drive_id, "path": ""}],
            "exclude_prefixes": exclude_prefixes,
            "expected": 0,
        }
    )
    return {"drive_id": drive_id, "shards": shards, "expected_total": total}


async def _search_counted(
    token: str, items: Sequence[Dict[str, Any]], *, path_of: Any, min_modified: Optional[str]
) -> List[Dict[str, Any]]:
    """Count every item's documents via Graph Search, bounded to
    :data:`_COUNT_CONCURRENCY` in flight — shared by the top-level and the
    one-level-deeper fold, which differ only in how they build ``path``."""
    from connectors.sharepoint import graph_client

    semaphore = asyncio.Semaphore(_COUNT_CONCURRENCY)

    async def _one(item: Dict[str, Any]) -> Dict[str, Any]:
        async with semaphore:
            count = await graph_client.search_document_count(
                token, item.get("web_url") or "", min_modified=min_modified
            )
        return {"path": path_of(item), "item_id": item["id"], "documents": count}

    return list(await asyncio.gather(*[_one(item) for item in items]))


async def _fold_one_level(
    token: str, drive_id: str, unit: Dict[str, Any], *, min_modified: Optional[str]
) -> List[Dict[str, Any]]:
    """A single over-target unit -> its own children's units, one level
    deeper and never further (design §4.1 point 3). A folder with no
    subfolders (or none discoverable) is returned unchanged — folding it
    would just discard work, not reduce it."""
    from connectors.sharepoint import graph_client

    children = await graph_client.list_item_children_with_url(token, drive_id, unit["item_id"])
    sub_folders = [c for c in children if c.get("is_folder")]
    if not sub_folders:
        return [unit]
    return await _search_counted(
        token,
        sub_folders,
        path_of=lambda item, _parent=unit["path"]: f"{_parent}/{item['name']}",
        min_modified=min_modified,
    )


async def _count_top_level_folders(
    token: str, drive_id: str, folder_items: Sequence[Dict[str, Any]], *, min_modified: Optional[str]
) -> tuple[List[Dict[str, Any]], str]:
    """One drive's top-level folders, counted via Search first; if every
    folder comes back ``0`` (Search unavailable, or the drive is genuinely
    all-zero — indistinguishable, and it does not matter which: neither
    balances anything), falls back to ``folder.childCount`` from a SECOND,
    single-page listing (:func:`connectors.sharepoint.graph_client
    .list_root_children`, which — unlike ``list_root_children_with_url`` —
    already carries it; not folded together since that function's own
    return shape is a tested contract elsewhere). Returns ``(units,
    signal)``; ``signal`` is :data:`SIGNAL_NONE` only when BOTH came back
    all-zero."""
    from connectors.sharepoint import graph_client

    searched = await _search_counted(token, folder_items, path_of=lambda item: item["name"], min_modified=min_modified)
    if any(u["documents"] for u in searched):
        return searched, SIGNAL_SEARCH

    child_rows = await graph_client.list_root_children(token, drive_id)
    counts_by_id = {row["id"]: row.get("child_count") for row in child_rows}
    fallback = [
        {"path": item["name"], "item_id": item["id"], "documents": int(counts_by_id.get(item["id"]) or 0)}
        for item in folder_items
    ]
    if any(u["documents"] for u in fallback):
        return fallback, SIGNAL_CHILD_COUNT
    return searched, SIGNAL_NONE


async def compute_shard_plan(
    transport: Any,
    auth: Any,
    scope: Dict[str, Any],
    targets: Sequence[Any],
    *,
    min_modified: Optional[str] = None,
    target_docs: int,
    max_shards: int = 32,
) -> Dict[str, Any]:
    """This scope's full shard plan — one already-resolved ``DriveTarget``
    (``connectors.sharepoint.crawler.DriveTarget``) at a time, in ``targets``
    order (the same order :func:`connectors.sharepoint.crawler._drive_targets`
    already produces).

    ``transport``/``auth`` are the SAME ``GraphTransport``/``GraphAuth``
    objects the calling run already built (``_run_crawl_async``) —
    ``auth.token()`` supplies the bearer token every call below needs.
    ``transport`` itself is accepted for parity with every other function
    this module's caller threads it through (its ``stats`` already track
    every Graph call the run makes) but is not called directly here: each
    helper below already has its own single-shot contract (notably
    :func:`connectors.sharepoint.graph_client.search_document_count`, which
    never raises — a failed count balances like an empty folder, never
    drops one).

    ``target_docs <= 0`` short-circuits to "every target inline" — the
    "0 = never shard" contract (design §4.8) applies at this layer too, not
    only in the caller that decides whether to invoke this function at all.

    Returns ``{"drives": [<one plan_shards() result per drive that needed
    sharding, plus "loose_root_files" and "signal">], "inline_state_keys":
    [<DriveTarget.state_key, ...>], "signal": <the last non-"none" signal
    observed, or "none">}``. A drive whose total is at or under
    ``target_docs`` — or that has no top-level folders to shard by —
    contributes its target's ``state_key`` to ``inline_state_keys`` and
    nothing to ``drives`` (design §4.1 point 2).
    """
    from connectors.sharepoint import graph_client

    if target_docs <= 0:
        return {"drives": [], "inline_state_keys": [t.state_key for t in targets], "signal": SIGNAL_NONE}

    token = await auth.token()
    drive_plans: List[Dict[str, Any]] = []
    inline_state_keys: List[str] = []
    overall_signal = SIGNAL_NONE

    for target in targets:
        drive_id = target.drive_id
        root_url = await graph_client.get_root_web_url(token, drive_id)
        drive_total = (
            await graph_client.search_document_count(token, root_url, min_modified=min_modified) if root_url else 0
        )
        if drive_total and drive_total <= target_docs:
            inline_state_keys.append(target.state_key)
            continue

        children = await graph_client.list_root_children_with_url(token, drive_id)
        folder_items = [c for c in children if c.get("is_folder")]
        loose_root_files = [c["name"] for c in children if not c.get("is_folder")]
        if not folder_items:
            # Nothing to shard by — a flat listing of loose files only.
            inline_state_keys.append(target.state_key)
            continue

        units, signal = await _count_top_level_folders(token, drive_id, folder_items, min_modified=min_modified)

        folded_units: List[Dict[str, Any]] = []
        for unit in units:
            if signal != SIGNAL_NONE and unit["documents"] > target_docs:
                folded_units.extend(await _fold_one_level(token, drive_id, unit, min_modified=min_modified))
            else:
                folded_units.append(unit)

        force_shard_count = None
        balance = True
        if signal == SIGNAL_NONE:
            force_shard_count = min(len(folded_units), max_shards)
            balance = False

        plan = plan_shards(
            folded_units,
            drive_id=drive_id,
            target_docs=target_docs,
            max_shards=max_shards,
            force_shard_count=force_shard_count,
            balance=balance,
        )
        plan["loose_root_files"] = loose_root_files
        plan["signal"] = signal
        drive_plans.append(plan)
        if signal != SIGNAL_NONE:
            overall_signal = signal

    return {"drives": drive_plans, "inline_state_keys": inline_state_keys, "signal": overall_signal}
