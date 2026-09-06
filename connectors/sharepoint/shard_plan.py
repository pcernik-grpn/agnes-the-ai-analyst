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
  deeper (never further), and hands the result to :func:`plan_shards`.

Reuses :func:`connectors.sharepoint.site_split.pack_folders_into_groups` —
the same greedy longest-processing-time-first packer the manual split-plan
preview already uses — for the balanced (known-count / childCount / Search)
case.

2026-09-04 finding #65 ("the automatic shard planner must not depend on a
Graph Search storm") — first live run of the auto-parallel crawl: a 388-scope
connection sharing ONE drive drove ``compute_shard_plan`` into 20+ minutes
with no run row, no log line, then a burst of ``sharepoint search
document-count failed: HTTP 429`` warnings, because Pass 1 issued one
``search_document_count`` call per TARGET (388 identical calls for one
drive_id, none deduped) and Pass 2 counted every drive's top-level folders
via Search FIRST, at 8-way concurrency, with no budget. Three structural
changes fix this, all in this module:

1. **Signal precedence inverted.** A caller-supplied ``known_totals``/
   ``known_folder_counts`` (built by ``connectors.sharepoint.crawler`` from
   ``corpus_files`` and a previous plan) wins first; ``folder.childCount``
   from the listing already fetched (:func:`connectors.sharepoint.
   graph_client.list_root_children_with_url` now carries it — no extra
   call) is tried next; Graph Search is the LAST resort, only for a folder
   with neither, at concurrency :data:`_COUNT_CONCURRENCY` (2, was 8) and
   bounded by a shared wall-clock :data:`_PLAN_SEARCH_BUDGET_S`. Every
   folder unit records its own ``signal``.
2. **Both passes are memoized by ``drive_id``.** A connection with many
   scopes sharing one drive (the exact incident above) used to repeat every
   root-total search AND every folder listing/count once per TARGET; both
   now compute once per UNIQUE drive and reuse the result for every target
   that shares it — the output shape (one drive-plan entry per target) is
   unchanged, only the number of Graph calls drops.
3. **Progress is observable while it happens** — :func:`compute_shard_plan`
   logs at INFO every :data:`_PROGRESS_LOG_EVERY` folders resolved (any
   signal) and once more at the end, and calls an optional ``on_progress``
   callback the same cadence so a caller (the crawler's already-open parent
   run row) can checkpoint "planning k/N folders" instead of staying silent
   for the whole window.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from connectors.sharepoint.site_split import pack_folders_into_groups

logger = logging.getLogger(__name__)

#: How many `search_document_count` calls the planner keeps in flight at
#: once — LOWERED from 8 (the manual split-plan preview's own concurrency)
#: to 2 by finding #65: Search is now the LAST-RESORT signal (see the module
#: docstring), so a low ceiling costs little on the rare folder that still
#: needs it, while a live 388-scope run drove the OLD 8-way ceiling into a
#: sustained HTTP 429 storm.
_COUNT_CONCURRENCY = 2

#: Wall-clock ceiling, in seconds, on every Graph Search call ONE
#: `compute_shard_plan()` invocation makes — Pass 1's per-drive root count
#: AND Pass 2's per-folder counts draw from the SAME budget (see
#: `_PlanBudget`), so a multi-drive site cannot spend it once per drive.
#: Once exhausted, every remaining unresolved folder takes
#: `SIGNAL_CHILD_COUNT` (even when that is 0) instead of queuing another
#: Search call — a 429 storm degrades the plan's balance quality, never the
#: planning WALL TIME (2026-09-04 finding #65).
_PLAN_SEARCH_BUDGET_S = 120.0

#: How often (in folders resolved, any signal) `compute_shard_plan` logs a
#: progress line and calls `on_progress` — finding #65 item 3: a large site's
#: planning window must never pass in total silence.
_PROGRESS_LOG_EVERY = 50

#: Which counting signal a unit was sized by — recorded per unit AND rolled
#: up per drive, so the preview/UI can say "≈" honestly (design §6 "Search
#: counts are approximate") and so an operator can tell HOW a plan was
#: balanced, not only that it was.
SIGNAL_SEARCH = "search"
SIGNAL_CHILD_COUNT = "child_count"
#: A caller-supplied count (``corpus_files`` or a previous plan) — never
#: touches Graph at all. New in finding #65's precedence rework.
SIGNAL_KNOWN = "known"
SIGNAL_NONE = "none"

#: Signals ranked cheapest (best) to most expensive (worst) — used to pick
#: one representative signal for a whole shard/drive from its units' mixed
#: signals (the WORST one present, since that is what bounds how much to
#: trust the balance).
_SIGNAL_RANK = {SIGNAL_KNOWN: 0, SIGNAL_CHILD_COUNT: 1, SIGNAL_SEARCH: 2, SIGNAL_NONE: 3}


def _dominant_signal(signals: Sequence[str]) -> str:
    """The single WORST (least certain) signal among ``signals`` — empty
    input is :data:`SIGNAL_NONE`, never an IndexError."""
    if not signals:
        return SIGNAL_NONE
    return max(signals, key=lambda s: _SIGNAL_RANK.get(s, len(_SIGNAL_RANK)))


def _state_key(drive_id: str, root_item_id: Optional[str]) -> str:
    """Same formula as ``connectors.sharepoint.crawler.DriveTarget.
    state_key`` — duplicated here rather than imported (``crawler`` imports
    FROM this module; importing back would cycle)."""
    return f"{drive_id}:{root_item_id}" if root_item_id else drive_id


def _join_path(prefix: str, name: str) -> str:
    """``prefix`` (a target's own drive-relative root path — ``""`` for a
    whole-drive target) joined onto ``name`` (a path already computed
    relative to THAT root). This is what makes a FOLDER-scoped target's
    ``exclude_prefixes`` land as the SAME full drive-relative path
    ``connectors.sharepoint.crawler._drive_relative_path`` computes for the
    same item at crawl time — without it, a folder scope's own packed
    child names (e.g. ``"Contracts"``) would never match the drive-relative
    paths (``"HR/Contracts/..."``) the remainder's item-level exclusion
    check actually sees, and the remainder would silently re-walk its own
    already-packed siblings on every first enumeration."""
    return f"{prefix}/{name}" if prefix else name


class _PlanBudget:
    """Shared wall-clock ceiling for every Graph Search call ONE
    :func:`compute_shard_plan` invocation makes (finding #65) — Pass 1's
    per-drive root count AND Pass 2's per-folder counts draw from the SAME
    budget, so a large multi-drive site cannot spend it once per drive.
    Logs a single WARNING the moment it first trips — not once per folder
    that subsequently falls back — so the run's log names WHY the plan
    degraded, once, instead of drowning in the same line.
    """

    def __init__(self, budget_s: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._budget_s = budget_s
        self._deadline: Optional[float] = clock() + budget_s if budget_s and budget_s > 0 else None
        self._warned = False

    def exceeded(self) -> bool:
        if self._deadline is None:
            return False
        if self._clock() < self._deadline:
            return False
        if not self._warned:
            self._warned = True
            logger.warning(
                "sharepoint shard planner: search budget (%.0fs) exceeded — remaining folders take "
                "folder.childCount (or 0) instead of another Graph Search call",
                self._budget_s,
            )
        return True


class _PlanProgress:
    """Folder-resolution counter shared across every drive one
    :func:`compute_shard_plan` call plans — drives the "log every N folders"
    and run-row-checkpoint visibility fix (finding #65 item 3: planning a
    large site previously ran with no run row and no log line for 20+
    minutes). ``total`` grows as each drive's own listing resolves — it is a
    running estimate, not known up front for a multi-drive site."""

    def __init__(self, on_progress: Optional[Callable[[int, int], None]]) -> None:
        self._on_progress = on_progress
        self.done = 0
        self.total = 0

    def add_total(self, n: int) -> None:
        self.total += n

    def resolve(self, n: int = 1) -> None:
        self.done += n
        if self.done % _PROGRESS_LOG_EVERY == 0:
            self._emit()

    def finish(self) -> None:
        self._emit()

    def _emit(self) -> None:
        logger.info("sharepoint shard planner: %d/%d folder(s) counted", self.done, self.total)
        if self._on_progress:
            try:
                self._on_progress(self.done, self.total)
            except Exception:  # noqa: BLE001 — a checkpoint failure must never abort planning
                logger.debug("sharepoint shard planner: on_progress callback failed", exc_info=True)


class PlanningBudgetExhausted(RuntimeError):
    """Raised by :func:`compute_shard_plan` when the shared search budget
    ran out before ANY signal (known/childCount/Search) resolved for any
    drive or folder — a plan built on nothing but a 429 storm is worse than
    no plan at all. The caller (``connectors.sharepoint.crawler.
    _plan_or_run_inline``) catches this and falls back to the inline crawl
    (finding #65 item 4)."""


def plan_shards(
    units: Sequence[Dict[str, Any]],
    *,
    drive_id: str,
    target_docs: int,
    max_shards: int = 32,
    force_shard_count: Optional[int] = None,
    balance: bool = True,
    root_item_id: Optional[str] = None,
    root_path: str = "",
) -> Dict[str, Any]:
    """Pack pre-resolved, pre-counted crawl units into ``K`` shards plus one
    remainder shard.

    ``root_item_id``/``root_path`` (2026-09-06 finding — "the remainder
    shard must be scoped to the same subtree its scope covers, never to the
    drive root"): identify the SUBTREE this whole call is packing —
    ``None``/``""`` (the default) for a whole-DRIVE scope, or a folder
    scope's own root item id and its drive-relative path (e.g. ``"HR"``)
    when :func:`compute_shard_plan` is packing one confirmed FOLDER scope's
    own children, never the drive it happens to live on. Every unit's own
    ``item_id`` still becomes ITS OWN packed target's ``root_item_id``
    (a specific child, unaffected) — ``root_item_id``/``root_path`` matter
    only for the WHOLE-SUBTREE targets this function itself manufactures:
    the empty-``units`` shard and the remainder.

    Each ``unit``: ``{"path": <path relative to root_item_id>, "item_id":
    <Graph item id>, "documents": <int>, "signal": <str, optional>}`` — one
    crawl target's worth of work, already at the granularity it will be
    crawled at (a top-level folder under ``root_item_id``, or one of its
    children when :func:`compute_shard_plan` folded it one level deeper).
    This function does not care which. ``signal`` (one of
    :data:`SIGNAL_KNOWN`/:data:`SIGNAL_CHILD_COUNT`/:data:`SIGNAL_SEARCH`/
    :data:`SIGNAL_NONE`, default :data:`SIGNAL_NONE` when absent) rides
    through onto each packed shard's own targets and rolls up into the
    shard's ``signal`` — the WORST (least certain) signal among the shard's
    own units (finding #65 item 1: "record signal per shard in the plan").

    ``K = min(ceil(total_documents / target_docs), max_shards)``, at least
    1, UNLESS ``force_shard_count`` overrides it (the "no counting signal"
    fallback — see :func:`compute_shard_plan`, which also passes
    ``balance=False`` in that case: with every unit's ``documents`` equal,
    the greedy packer degenerates to "everything in the first group" —
    ties never move the running minimum — so the fallback round-robins
    units across shards instead).

    Returns ``{"drive_id", "shards": [...], "expected_total": int}``. Every
    shard but the last is ``{"index", "label", "signal", "targets": [{
    "drive_id", "root_item_id", "state_key", "path", "signal"}], "expected":
    <summed documents>}`` — one target per unit in that group, each unit's
    own ``path`` joined onto ``root_path`` so it reads as a full
    drive-relative path even for a folder-scoped call. The LAST shard is
    always the REMAINDER: a single target scoped to ``root_item_id``
    (``state_key = drive_id`` for a whole-drive call, ``f"{drive_id}:
    {root_item_id}"`` for a folder scope's own remainder — never the drive
    root when the scope itself is not the whole drive) with
    ``exclude_prefixes`` set to every packed unit's own (``root_path``-
    joined) path, so it covers loose files directly under the scope and
    anything created after planning without re-crawling what the other
    shards already own (design §4.1 point 3, generalized from "drive" to
    "the subtree this call packs"). Its ``expected`` is always ``0`` and
    its ``signal`` is :data:`SIGNAL_NONE` — a remainder's true count is
    whatever a full enumeration of its own subtree finds beyond the packed
    folders, which this function has no way to know in advance.

    No I/O, no randomness — safe to call twice on the same input and diff
    the results (a preview endpoint's whole reason to exist).

    ``units`` EMPTY is its own case, not "zero packed shards plus a
    remainder": a subtree with nothing to split by (its own total already
    at or under ``target_docs``, or a flat listing with no subfolders)
    still gets exactly ONE shard — the whole subtree, unsplit — never an
    empty packed shard sitting next to a remainder that duplicates it.
    """
    if not units:
        whole_target = {
            "drive_id": drive_id,
            "root_item_id": root_item_id,
            "state_key": _state_key(drive_id, root_item_id),
            "path": root_path,
            "signal": SIGNAL_NONE,
        }
        return {
            "drive_id": drive_id,
            "shards": [
                {
                    "index": 1,
                    "label": "whole drive",
                    "signal": SIGNAL_NONE,
                    "targets": [whole_target],
                    "expected": 0,
                }
            ],
            "expected_total": 0,
        }

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
                "state_key": _state_key(drive_id, unit["item_id"]),
                "path": _join_path(root_path, unit["path"]),
                "signal": unit.get("signal") or SIGNAL_NONE,
            }
            for unit in group["folders"]
        ]
        shards.append(
            {
                "index": index,
                "label": f"part {index}/{k}",
                "signal": _dominant_signal([t["signal"] for t in targets]),
                "targets": targets,
                "expected": group["documents"],
            }
        )

    exclude_prefixes = sorted({_join_path(root_path, str(u["path"])) for u in units})
    shards.append(
        {
            "index": k + 1,
            "label": "remainder",
            "signal": SIGNAL_NONE,
            "targets": [
                {
                    "drive_id": drive_id,
                    "root_item_id": root_item_id,
                    "state_key": _state_key(drive_id, root_item_id),
                    "path": root_path,
                    "signal": SIGNAL_NONE,
                }
            ],
            "exclude_prefixes": exclude_prefixes,
            "expected": 0,
        }
    )
    return {"drive_id": drive_id, "shards": shards, "expected_total": total}


async def _search_counted(
    token: str,
    items: Sequence[Dict[str, Any]],
    *,
    path_of: Any,
    min_modified: Optional[str],
    budget: _PlanBudget,
    progress: _PlanProgress,
) -> List[Dict[str, Any]]:
    """Count every item's documents via Graph Search, bounded to
    :data:`_COUNT_CONCURRENCY` in flight AND to ``budget`` — shared by the
    top-level and the one-level-deeper fold, which differ only in how they
    build ``path``. An item still unresolved once ``budget`` trips gets
    ``documents=0`` (the caller has already tried every cheaper signal for
    it — there is nothing left to fall back to) rather than firing another
    Search call; the SAME item's counting never blocks a sibling more than
    :data:`_COUNT_CONCURRENCY`-wide (finding #65: "never a 429-backoff
    loop")."""
    from connectors.sharepoint import graph_client

    semaphore = asyncio.Semaphore(_COUNT_CONCURRENCY)

    async def _one(item: Dict[str, Any]) -> Dict[str, Any]:
        count = 0
        if not budget.exceeded():
            async with semaphore:
                if not budget.exceeded():
                    try:
                        count = await graph_client.search_document_count(
                            token, item.get("web_url") or "", min_modified=min_modified
                        )
                    except Exception:  # noqa: BLE001 — a raised fault balances like an empty folder, never loops
                        logger.warning(
                            "sharepoint shard planner: search count raised for %r", path_of(item), exc_info=True
                        )
                        count = 0
        progress.resolve()
        return {
            "path": path_of(item),
            "item_id": item["id"],
            "documents": count,
            "signal": SIGNAL_SEARCH if count else SIGNAL_NONE,
        }

    return list(await asyncio.gather(*[_one(item) for item in items]))


async def _fold_one_level(
    token: str,
    drive_id: str,
    unit: Dict[str, Any],
    *,
    min_modified: Optional[str],
    budget: _PlanBudget,
    progress: _PlanProgress,
) -> List[Dict[str, Any]]:
    """A single over-target unit -> its own children's units, one level
    deeper and never further (design §4.1 point 3). A folder with no
    subfolders (or none discoverable) is returned unchanged — folding it
    would just discard work, not reduce it. Each child prefers its own
    ``child_count`` (from the SAME listing this fold already pays for)
    before falling back to Search — the same precedence
    :func:`_count_top_level_folders` applies at the top level."""
    from connectors.sharepoint import graph_client

    children = await graph_client.list_item_children_with_url(token, drive_id, unit["item_id"])
    sub_folders = [c for c in children if c.get("is_folder")]
    if not sub_folders:
        return [unit]

    def _path_of(item: Dict[str, Any]) -> str:
        return f"{unit['path']}/{item['name']}"

    progress.add_total(len(sub_folders))
    known_units: List[Dict[str, Any]] = []
    to_search: List[Dict[str, Any]] = []
    for item in sub_folders:
        child_count = item.get("child_count")
        if child_count:
            known_units.append(
                {
                    "path": _path_of(item),
                    "item_id": item["id"],
                    "documents": int(child_count),
                    "signal": SIGNAL_CHILD_COUNT,
                }
            )
            progress.resolve()
        else:
            to_search.append(item)

    searched = (
        await _search_counted(
            token, to_search, path_of=_path_of, min_modified=min_modified, budget=budget, progress=progress
        )
        if to_search
        else []
    )
    return known_units + searched


async def _count_top_level_folders(
    token: str,
    drive_id: str,
    folder_items: Sequence[Dict[str, Any]],
    *,
    min_modified: Optional[str],
    known_counts: Dict[str, int],
    budget: _PlanBudget,
    progress: _PlanProgress,
) -> tuple[List[Dict[str, Any]], str]:
    """One drive's top-level folders, counted by precedence (finding #65
    item 1, cheapest first): a caller-supplied ``known_counts[name]``
    (``corpus_files``/a previous plan — never touches Graph); else
    ``folder.childCount`` from the SAME listing the caller already fetched
    (no extra call); else Graph Search, budgeted and low-concurrency, ONLY
    for a folder with neither. Returns ``(units, signal)`` — ``signal`` is
    the WORST (least certain) signal actually used across every folder, or
    :data:`SIGNAL_NONE` if none resolved to anything."""
    progress.add_total(len(folder_items))

    known_units: List[Dict[str, Any]] = []
    child_count_units: List[Dict[str, Any]] = []
    to_search: List[Dict[str, Any]] = []
    for item in folder_items:
        name = item["name"]
        known = known_counts.get(name)
        if known is not None:
            known_units.append({"path": name, "item_id": item["id"], "documents": int(known), "signal": SIGNAL_KNOWN})
            progress.resolve()
            continue
        child_count = item.get("child_count")
        if child_count:
            child_count_units.append(
                {"path": name, "item_id": item["id"], "documents": int(child_count), "signal": SIGNAL_CHILD_COUNT}
            )
            progress.resolve()
            continue
        to_search.append(item)

    searched = (
        await _search_counted(
            token,
            to_search,
            path_of=lambda item: item["name"],
            min_modified=min_modified,
            budget=budget,
            progress=progress,
        )
        if to_search
        else []
    )

    units = known_units + child_count_units + searched
    # Preserve the listing's own order — deterministic output regardless of
    # which precedence tier each folder landed in.
    order = {item["id"]: idx for idx, item in enumerate(folder_items)}
    units.sort(key=lambda u: order.get(u["item_id"], 0))
    signal = _dominant_signal([u["signal"] for u in units])
    return units, signal


async def compute_shard_plan(
    transport: Any,
    auth: Any,
    scope: Dict[str, Any],
    targets: Sequence[Any],
    *,
    min_modified: Optional[str] = None,
    target_docs: int,
    max_shards: int = 32,
    known_totals: Optional[Dict[str, int]] = None,
    known_folder_counts: Optional[Dict[str, Dict[str, int]]] = None,
    budget_s: float = _PLAN_SEARCH_BUDGET_S,
    on_progress: Optional[Callable[[int, int], None]] = None,
    clock: Optional[Callable[[], float]] = None,
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

    ``known_totals`` (``drive_id -> total documents``) and
    ``known_folder_counts`` (``drive_id -> {folder_name -> documents}``) are
    the crawler's cheap, non-Graph signals (finding #65 item 1(a)/(b):
    ``corpus_files`` counts for a scope whose collection already holds
    indexed documents, or a previous plan's own per-shard counts) — when
    given, they are consulted BEFORE any Graph call for the drive/folder
    they cover. Both default to ``{}`` (no known signal at all — every
    decision below falls through to childCount/Search exactly as it did
    before finding #65 added this parameter).

    ``target_docs <= 0`` short-circuits to "every target inline" — the
    "0 = never shard" contract (design §4.8) applies at this layer too, not
    only in the caller that decides whether to invoke this function at all.

    Two passes (design §4.1 points 2-3):

    1. One :func:`connectors.sharepoint.graph_client.search_document_count`
       per UNIQUE DRIVE root (memoized by ``drive_id`` — finding #65 item
       1/2: a connection with many targets sharing one drive, the
       incident's own shape, must not repeat this once per target),
       skipped entirely when ``known_totals`` already names that drive, and
       skipped once the shared search ``budget`` is spent (an unresolved
       drive is treated as "total unknown", never assumed small). If EVERY
       drive answered (none had an unreadable root ``webUrl`` and none hit
       the budget) and their SUM is at or under ``target_docs``, the whole
       site stays inline — ``{"drives": [], "inline_state_keys": [every
       target's state_key], ...}`` — the same "byte-for-byte today's crawl"
       path the caller takes for DuckDB / the knob at 0. This pass answers
       a SITE-level question ("is there enough content anywhere to bother
       sharding at all"), so deduping by the physical drive — coarser than
       a target's own subtree — is deliberate and safe: it can only ever
       UNDER-estimate how much sharding is needed, never drop coverage.
    2. Otherwise every UNIQUE TARGET (memoized by ``target.state_key`` —
       2026-09-06 finding: a connection with many FOLDER-scoped targets
       sharing one drive must get one plan PER TARGET's own subtree, never
       one plan per drive reused verbatim across every scope that happens
       to live on it) gets its own :func:`plan_shards` result: a target
       whose drive-wide total is at or under ``target_docs`` gets exactly
       ONE shard (the whole SUBTREE, unsplit — :func:`plan_shards`'s
       empty-units case, scoped to ``target.root_item_id``); an over-target
       target is listed — under ``target.root_item_id`` when set, the
       drive root otherwise — counted (known -> childCount -> Search, see
       :func:`_count_top_level_folders`), folded and packed as described in
       the module docstring. Two targets that are BYTE-IDENTICAL (same
       ``drive_id`` AND same ``root_item_id`` — the dedup case finding #65
       actually fixed, e.g. two confirmed scopes that resolve to the exact
       same whole drive) still share one computed plan; two targets that
       merely share a ``drive_id`` but cover DIFFERENT subtrees (a
       whole-drive scope and a folder scope on it, or two sibling folder
       scopes) never do — each is listed and packed under its OWN root, so
       its own remainder can never wander into a sibling's subtree, let
       alone the whole drive.

    Returns ``{"drives": [<one plan_shards() result per TARGET, in
    ``targets`` order — identical only for two targets sharing the same
    ``state_key``>], "inline_state_keys": [<every target's state_key, ONLY
    when the whole site stayed inline>], "signal": <the WORST signal
    observed across every target, or "none">}`` — ``drives`` and a
    non-empty ``inline_state_keys`` are mutually exclusive: either the
    whole site is inline, or every target has its own plan (never a mix of
    the two, which would leave an "inline" target uncrawled by anything the
    parent enqueues). If, once every pass above is done, NO signal was ever
    resolved (every target/folder stayed :data:`SIGNAL_NONE`) AND the
    search ``budget`` was exhausted getting there, :class:`PlanningBudgetExhausted`
    is raised instead — the caller (``connectors.sharepoint.crawler.
    _plan_or_run_inline``) falls back to the inline crawl rather than
    enqueue a plan balanced on nothing but a 429 storm (finding #65 item 4).

    **Nested scopes on one drive** (a folder scope's own root lies inside
    ANOTHER confirmed scope's subtree — e.g. a whole-drive scope plus a
    folder scope for one of its subfolders, or two nested folder scopes):
    this function does NOT try to detect or special-case the nesting. Each
    target's plan is built independently, scoped only to ITS OWN
    ``root_item_id`` — so the outer scope's own packed/remainder shards
    will still enumerate (via Graph delta, at crawl time) into the inner
    scope's subtree too, exactly as the INLINE (unsharded) crawl already
    does today. What has always prevented double-INGESTION of that overlap
    is a DIFFERENT, pre-existing mechanism: a nested zone's own
    ``excluded_subtrees`` entry on the OUTER scope (populated by ACL sync
    when the nested zone was carved out as its own confirmed scope),
    applied at item-processing time
    (``connectors.sharepoint.crawler._excluded_path_prefixes`` /
    ``_process_item``) — unchanged by sharding, and untouched by this
    function. The shard planner's own job is narrower and unrelated: keep
    a SINGLE scope's own packed/remainder targets from wandering OUTSIDE
    that scope's subtree (the drive-root leak this finding fixes), not
    resolve overlaps BETWEEN scopes — that has always been, and remains,
    ``excluded_subtrees``'s job.

    ``clock`` is a TEST SEAM only (same idiom as ``connectors.sharepoint.
    crawler._Deadline``'s own ``clock`` parameter) — production never passes
    one; a test can advance a fake clock deterministically instead of
    sleeping real wall-clock time to exercise ``budget_s``.
    """
    from connectors.sharepoint import graph_client

    if target_docs <= 0:
        return {"drives": [], "inline_state_keys": [t.state_key for t in targets], "signal": SIGNAL_NONE}

    known_totals = known_totals or {}
    known_folder_counts = known_folder_counts or {}
    budget = _PlanBudget(budget_s, clock=clock or time.monotonic)
    progress = _PlanProgress(on_progress)

    token = await auth.token()

    # Pass 1 — one search per UNIQUE drive root, deciding the SITE as a
    # whole. Deduped: a connection with many targets sharing one drive_id
    # (finding #65's own incident shape) must not repeat this once per
    # target.
    unique_drive_ids = list(dict.fromkeys(t.drive_id for t in targets))
    drive_totals: Dict[str, Optional[int]] = {}
    drive_total_signal: Dict[str, str] = {}
    for drive_id in unique_drive_ids:
        if drive_id in known_totals:
            drive_totals[drive_id] = int(known_totals[drive_id])
            drive_total_signal[drive_id] = SIGNAL_KNOWN
            continue
        if budget.exceeded():
            drive_totals[drive_id] = None
            continue
        try:
            root_url = await graph_client.get_root_web_url(token, drive_id)
            total = (
                await graph_client.search_document_count(token, root_url, min_modified=min_modified)
                if root_url
                else None
            )
        except Exception:  # noqa: BLE001 — a raised fault is "total unknown", never a crashed plan
            logger.warning("sharepoint shard planner: root search count raised for drive %r", drive_id, exc_info=True)
            total = None
        drive_totals[drive_id] = total
        drive_total_signal[drive_id] = SIGNAL_SEARCH if total else SIGNAL_NONE

    any_signal_found = any(sig != SIGNAL_NONE for sig in drive_total_signal.values())

    if all(total is not None for total in drive_totals.values()):
        site_total = sum(total or 0 for total in drive_totals.values())
        if site_total <= target_docs:
            signal = _dominant_signal(list(drive_total_signal.values()))
            return {"drives": [], "inline_state_keys": [t.state_key for t in targets], "signal": signal}

    # Pass 2 — the site needs sharding; every UNIQUE TARGET (its own
    # subtree — `drive_id` AND `root_item_id`, never `drive_id` alone; see
    # the docstring's 2026-09-06 finding) gets its own plan, computed once
    # and reused only for a target sharing the exact same state_key.
    drive_plan_cache: Dict[str, Dict[str, Any]] = {}
    drive_signal_cache: Dict[str, str] = {}
    drive_plans: List[Dict[str, Any]] = []

    for target in targets:
        drive_id = target.drive_id
        root_item_id = target.root_item_id
        root_path = getattr(target, "root_path", "") or ""
        cache_key = target.state_key
        cached = drive_plan_cache.get(cache_key)
        if cached is not None:
            drive_plans.append(cached)
            if drive_signal_cache[cache_key] != SIGNAL_NONE:
                any_signal_found = True
            continue

        # This target's own subtree can never hold MORE than its whole
        # drive — the drive-wide total from Pass 1 is a safe (if
        # conservative for a folder scope) upper bound: if the WHOLE drive
        # fits under target_docs, this target's own subtree certainly does
        # too, and no listing call is needed to know it.
        drive_total = drive_totals.get(drive_id)
        if drive_total is not None and drive_total <= target_docs:
            # Small enough on its OWN — one whole-subtree shard, scoped to
            # THIS target's own root (never the drive root for a folder
            # scope), no folder split, no extra listing call.
            plan = plan_shards(
                [],
                drive_id=drive_id,
                target_docs=target_docs,
                max_shards=max_shards,
                root_item_id=root_item_id,
                root_path=root_path,
            )
            plan["loose_root_files"] = []
            plan["signal"] = drive_total_signal.get(drive_id, SIGNAL_NONE)
            drive_plan_cache[cache_key] = plan
            drive_signal_cache[cache_key] = plan["signal"]
            drive_plans.append(plan)
            if plan["signal"] != SIGNAL_NONE:
                any_signal_found = True
            continue

        children = (
            await graph_client.list_item_children_with_url(token, drive_id, root_item_id)
            if root_item_id
            else await graph_client.list_root_children_with_url(token, drive_id)
        )
        folder_items = [c for c in children if c.get("is_folder")]
        loose_root_files = [c["name"] for c in children if not c.get("is_folder")]
        if not folder_items:
            # Nothing to shard by — a flat listing of loose files only;
            # still one whole-subtree shard, same as the "small enough"
            # case, scoped to THIS target's own root.
            plan = plan_shards(
                [],
                drive_id=drive_id,
                target_docs=target_docs,
                max_shards=max_shards,
                root_item_id=root_item_id,
                root_path=root_path,
            )
            plan["loose_root_files"] = loose_root_files
            plan["signal"] = SIGNAL_NONE
            drive_plan_cache[cache_key] = plan
            drive_signal_cache[cache_key] = SIGNAL_NONE
            drive_plans.append(plan)
            continue

        units, signal = await _count_top_level_folders(
            token,
            drive_id,
            folder_items,
            min_modified=min_modified,
            known_counts=known_folder_counts.get(drive_id) or {},
            budget=budget,
            progress=progress,
        )

        folded_units: List[Dict[str, Any]] = []
        for unit in units:
            if unit["signal"] != SIGNAL_NONE and unit["documents"] > target_docs:
                folded = await _fold_one_level(
                    token, drive_id, unit, min_modified=min_modified, budget=budget, progress=progress
                )
                folded_units.extend(folded)
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
            root_item_id=root_item_id,
            root_path=root_path,
        )
        plan["loose_root_files"] = loose_root_files
        plan["signal"] = signal
        drive_plan_cache[cache_key] = plan
        drive_signal_cache[cache_key] = signal
        drive_plans.append(plan)
        if signal != SIGNAL_NONE:
            any_signal_found = True

    progress.finish()

    overall_signal = (
        _dominant_signal([s for s in drive_signal_cache.values() if s != SIGNAL_NONE])
        if any(s != SIGNAL_NONE for s in drive_signal_cache.values())
        else SIGNAL_NONE
    )

    if not any_signal_found and budget.exceeded():
        raise PlanningBudgetExhausted(
            f"shard planner: search budget ({budget_s:.0f}s) exhausted with no usable signal for any drive/folder"
        )

    return {"drives": drive_plans, "inline_state_keys": [], "signal": overall_signal}
