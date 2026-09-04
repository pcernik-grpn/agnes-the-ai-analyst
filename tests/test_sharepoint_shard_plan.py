"""``connectors.sharepoint.shard_plan`` — the shard planner (2026-09-03
auto-parallel-crawl design §4.1, Task 3).

``plan_shards`` is pure (no fixture, no I/O). ``compute_shard_plan`` is
exercised the same way ``tests/test_sharepoint_graph_client.py`` exercises
every other Graph call: ``graph_client._http_client`` wired to an
``httpx.MockTransport``, no live network, no certificate.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, List, Optional

import httpx
import pytest

from connectors.sharepoint import graph_client as gc
from connectors.sharepoint import shard_plan


def _install_transport(monkeypatch, handler: Callable[[httpx.Request], httpx.Response]) -> List[str]:
    seen: List[str] = []

    def _wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return handler(request)

    def _client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(_wrapped), timeout=10)

    monkeypatch.setattr(gc, "_http_client", _client)
    return seen


class _FakeAuth:
    """Stands in for ``connectors.sharepoint.crawler.GraphAuth`` — the only
    thing :func:`shard_plan.compute_shard_plan` calls on it is ``token()``."""

    def __init__(self, token: str = "tok") -> None:
        self._token = token

    async def token(self) -> str:
        return self._token


class _FakeTarget:
    def __init__(self, drive_id: str, root_item_id: Optional[str] = None) -> None:
        self.drive_id = drive_id
        self.root_item_id = root_item_id

    @property
    def state_key(self) -> str:
        return f"{self.drive_id}:{self.root_item_id}" if self.root_item_id else self.drive_id


# ---------------------------------------------------------------------------
# plan_shards — pure
# ---------------------------------------------------------------------------


def _units(*paths_and_counts: tuple) -> List[Dict[str, Any]]:
    return [{"path": path, "item_id": f"id-{path}", "documents": count} for path, count in paths_and_counts]


class TestPlanShardsPure:
    def test_every_unit_appears_in_exactly_one_shard(self):
        units = _units(("A", 400), ("B", 300), ("C", 300), ("D", 100))
        plan = shard_plan.plan_shards(units, drive_id="drv1", target_docs=250)

        packed_paths = [t["path"] for s in plan["shards"][:-1] for t in s["targets"]]
        assert sorted(packed_paths) == ["A", "B", "C", "D"]
        assert len(packed_paths) == len(set(packed_paths))

    def test_remainder_excludes_exactly_the_grouped_paths(self):
        units = _units(("A", 100), ("B", 200))
        plan = shard_plan.plan_shards(units, drive_id="drv1", target_docs=150)

        remainder = plan["shards"][-1]
        assert remainder["exclude_prefixes"] == ["A", "B"]
        assert remainder["targets"] == [
            {"drive_id": "drv1", "root_item_id": None, "state_key": "drv1", "path": "", "signal": "none"}
        ]
        assert remainder["expected"] == 0

    def test_k_never_exceeds_max_shards(self):
        units = _units(*[(f"F{i}", 1000) for i in range(50)])
        plan = shard_plan.plan_shards(units, drive_id="drv1", target_docs=10, max_shards=32)

        # shards[-1] is the remainder — every OTHER entry is a packed shard.
        assert len(plan["shards"]) - 1 <= 32

    def test_force_shard_count_round_robins_when_balance_is_false(self):
        """Every unit ties at documents=0 — the greedy packer would put
        everything in the first group (ties never move the running
        minimum); the no-signal fallback must round-robin instead."""
        units = _units(("A", 0), ("B", 0), ("C", 0))
        plan = shard_plan.plan_shards(units, drive_id="drv1", target_docs=5000, force_shard_count=3, balance=False)

        packed = [s for s in plan["shards"][:-1]]
        assert len(packed) == 3
        assert sorted(t["path"] for s in packed for t in s["targets"]) == ["A", "B", "C"]
        # One folder per shard — round-robin with count == number of units.
        assert all(len(s["targets"]) == 1 for s in packed)

    def test_plan_is_json_serializable_and_stable_across_two_calls(self):
        units = _units(("A", 400), ("B", 100))
        first = shard_plan.plan_shards(units, drive_id="drv1", target_docs=250)
        second = shard_plan.plan_shards(units, drive_id="drv1", target_docs=250)

        assert first == second
        assert json.loads(json.dumps(first)) == first

    def test_expected_total_sums_every_units_documents(self):
        units = _units(("A", 400), ("B", 300), ("C", 300))
        plan = shard_plan.plan_shards(units, drive_id="drv1", target_docs=250)
        assert plan["expected_total"] == 1000

    def test_zero_or_negative_target_docs_still_produces_one_packed_shard(self):
        units = _units(("A", 10), ("B", 10))
        plan = shard_plan.plan_shards(units, drive_id="drv1", target_docs=0)
        packed = plan["shards"][:-1]
        assert len(packed) == 1

    def test_empty_units_produces_exactly_one_whole_drive_shard(self):
        """No units to split by (the drive is small enough on its own, or a
        flat listing with no top-level folders) — ONE shard, the whole
        drive, unsplit. Never an empty packed shard sitting next to a
        remainder that would duplicate it."""
        plan = shard_plan.plan_shards([], drive_id="drv1", target_docs=100)
        assert plan["expected_total"] == 0
        assert len(plan["shards"]) == 1
        assert plan["shards"][0]["targets"] == [
            {"drive_id": "drv1", "root_item_id": None, "state_key": "drv1", "path": "", "signal": "none"}
        ]


# ---------------------------------------------------------------------------
# compute_shard_plan — Graph half
# ---------------------------------------------------------------------------

GRAPH = "https://graph.microsoft.com/v1.0"


def _search_response(total: int) -> httpx.Response:
    return httpx.Response(200, json={"value": [{"hitsContainers": [{"total": total}]}]})


def _make_handler(
    *,
    root_total: int = 0,
    folder_totals: Optional[Dict[str, int]] = None,
    folder_child_counts: Optional[Dict[str, int]] = None,
    root_folders: Optional[List[Dict[str, Any]]] = None,
    children_by_item: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    captured_queries: Optional[List[str]] = None,
) -> Callable[[httpx.Request], httpx.Response]:
    folder_totals = folder_totals or {}
    folder_child_counts = folder_child_counts or {}
    root_folders = root_folders if root_folders is not None else []
    children_by_item = children_by_item or {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/root") and request.method == "GET":
            return httpx.Response(200, json={"webUrl": "https://x/root"})
        if path.endswith("/search/query"):
            body = json.loads(request.content.decode())
            query = body["requests"][0]["query"]["queryString"]
            if captured_queries is not None:
                captured_queries.append(query)
            if '"https://x/root"' in query:
                return _search_response(root_total)
            for name, total in folder_totals.items():
                if f"/{name}" in query:
                    return _search_response(total)
            return _search_response(0)
        if path.endswith("/root/children"):
            value = [
                {
                    "id": f["id"],
                    "name": f["name"],
                    "folder": {"childCount": folder_child_counts.get(f["name"], 0)},
                    "webUrl": f"https://x/root/{f['name']}",
                }
                for f in root_folders
            ]
            return httpx.Response(200, json={"value": value})
        if "/items/" in path and path.endswith("/children"):
            item_id = path.split("/items/")[1].split("/")[0]
            kids = children_by_item.get(item_id, [])
            value = [
                {
                    "id": k["id"],
                    "name": k["name"],
                    "folder": {"childCount": 0},
                    "webUrl": f"https://x/{item_id}/{k['name']}",
                }
                for k in kids
            ]
            return httpx.Response(200, json={"value": value})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    return handler


def _folder(item_id: str, name: str) -> Dict[str, Any]:
    return {"id": item_id, "name": name}


class TestComputeShardPlanGraphHalf:
    def test_drive_at_or_under_target_stays_inline(self, monkeypatch):
        _install_transport(monkeypatch, _make_handler(root_total=50))
        target = _FakeTarget("drv1")

        plan = asyncio.run(shard_plan.compute_shard_plan(None, _FakeAuth(), {}, [target], target_docs=100))

        assert plan["drives"] == []
        assert plan["inline_state_keys"] == ["drv1"]

    def test_the_whole_site_stays_inline_when_the_summed_total_is_at_or_under_target(self, monkeypatch):
        """Two drives, neither over target on its own AND their SUM is
        also at or under target_docs — the site-level decision (design
        §4.1 point 2), not a per-drive one."""

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/root") and request.method == "GET":
                return httpx.Response(200, json={"webUrl": f"https://x/{path}"})
            if path.endswith("/search/query"):
                body = json.loads(request.content.decode())
                query = body["requests"][0]["query"]["queryString"]
                if "drv-a" in query:
                    return _search_response(40)
                return _search_response(40)
            raise AssertionError(f"unexpected request: {path}")

        _install_transport(monkeypatch, handler)
        targets = [_FakeTarget("drv-a"), _FakeTarget("drv-b")]

        plan = asyncio.run(shard_plan.compute_shard_plan(None, _FakeAuth(), {}, targets, target_docs=100))

        assert plan["drives"] == []
        assert plan["inline_state_keys"] == ["drv-a", "drv-b"]

    def test_a_site_whose_summed_total_exceeds_target_shards_every_drive(self, monkeypatch):
        """Same two drives as above, but their SUM now exceeds target_docs
        even though NEITHER is individually over it — every drive still
        gets its own (whole-drive) shard, none stay inline."""

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/root") and request.method == "GET":
                return httpx.Response(200, json={"webUrl": f"https://x/{path}"})
            if path.endswith("/search/query"):
                return _search_response(60)
            raise AssertionError(f"unexpected request: {path}")

        _install_transport(monkeypatch, handler)
        targets = [_FakeTarget("drv-a"), _FakeTarget("drv-b")]

        plan = asyncio.run(shard_plan.compute_shard_plan(None, _FakeAuth(), {}, targets, target_docs=100))

        assert plan["inline_state_keys"] == []
        assert {d["drive_id"] for d in plan["drives"]} == {"drv-a", "drv-b"}
        for drive_plan in plan["drives"]:
            assert len(drive_plan["shards"]) == 1
            assert drive_plan["shards"][0]["targets"][0]["root_item_id"] is None

    def test_drive_over_target_shards_by_search_counts(self, monkeypatch):
        handler = _make_handler(
            root_total=1000,
            root_folders=[_folder("f1", "A"), _folder("f2", "B")],
            folder_totals={"A": 600, "B": 400},
        )
        _install_transport(monkeypatch, handler)
        target = _FakeTarget("drv1")

        plan = asyncio.run(shard_plan.compute_shard_plan(None, _FakeAuth(), {}, [target], target_docs=100))

        assert plan["inline_state_keys"] == []
        assert len(plan["drives"]) == 1
        drive_plan = plan["drives"][0]
        assert drive_plan["signal"] == "search"
        assert drive_plan["expected_total"] == 1000
        packed_paths = sorted(t["path"] for s in drive_plan["shards"][:-1] for t in s["targets"])
        assert packed_paths == ["A", "B"]

    def test_search_unavailable_falls_back_to_child_count(self, monkeypatch):
        handler = _make_handler(
            # Root count non-zero so Pass 1 does not short-circuit the
            # whole site to inline — it is only the per-FOLDER search
            # (Pass 2) that is unavailable here.
            root_total=1000,
            root_folders=[_folder("f1", "A"), _folder("f2", "B")],
            folder_totals={},  # every folder-level search call returns 0
            folder_child_counts={"A": 30, "B": 10},
        )
        _install_transport(monkeypatch, handler)
        target = _FakeTarget("drv1")

        plan = asyncio.run(shard_plan.compute_shard_plan(None, _FakeAuth(), {}, [target], target_docs=5))

        drive_plan = plan["drives"][0]
        assert drive_plan["signal"] == "child_count"
        assert drive_plan["expected_total"] == 40

    def test_both_signals_absent_falls_back_to_one_shard_per_folder(self, monkeypatch):
        handler = _make_handler(
            root_total=1000,
            root_folders=[_folder("f1", "A"), _folder("f2", "B"), _folder("f3", "C")],
        )
        _install_transport(monkeypatch, handler)
        target = _FakeTarget("drv1")

        plan = asyncio.run(shard_plan.compute_shard_plan(None, _FakeAuth(), {}, [target], target_docs=5))

        drive_plan = plan["drives"][0]
        assert drive_plan["signal"] == "none"
        packed = drive_plan["shards"][:-1]
        assert len(packed) == 3
        assert all(len(s["targets"]) == 1 for s in packed)

    def test_a_huge_folder_is_folded_one_level_deeper_and_never_further(self, monkeypatch):
        handler = _make_handler(
            root_total=1000,
            root_folders=[_folder("f1", "Huge")],
            folder_totals={"Huge": 900},
            children_by_item={"f1": [_folder("f1a", "Sub1"), _folder("f1b", "Sub2")]},
        )
        _install_transport(monkeypatch, handler)
        target = _FakeTarget("drv1")

        plan = asyncio.run(shard_plan.compute_shard_plan(None, _FakeAuth(), {}, [target], target_docs=100))

        drive_plan = plan["drives"][0]
        packed_paths = sorted(t["path"] for s in drive_plan["shards"][:-1] for t in s["targets"])
        # The parent folder itself is gone — replaced by its two children,
        # never expanded a second level (the mock has no /items/f1a or
        # /items/f1b children handler at all — a second fold would 500).
        assert packed_paths == ["Huge/Sub1", "Huge/Sub2"]

    def test_min_modified_is_passed_through_to_every_search_call(self, monkeypatch):
        captured: List[str] = []
        handler = _make_handler(
            root_total=1000,
            root_folders=[_folder("f1", "A")],
            folder_totals={"A": 1000},
            captured_queries=captured,
        )
        _install_transport(monkeypatch, handler)
        target = _FakeTarget("drv1")

        asyncio.run(
            shard_plan.compute_shard_plan(None, _FakeAuth(), {}, [target], target_docs=100, min_modified="2026-01-01")
        )

        assert captured, "expected at least one search call"
        assert all("LastModifiedTime>=2026-01-01" in q for q in captured)

    def test_target_docs_zero_keeps_everything_inline_with_no_graph_calls(self, monkeypatch):
        seen = _install_transport(monkeypatch, _make_handler())
        targets = [_FakeTarget("drv1"), _FakeTarget("drv2")]

        plan = asyncio.run(shard_plan.compute_shard_plan(None, _FakeAuth(), {}, targets, target_docs=0))

        assert plan["inline_state_keys"] == ["drv1", "drv2"]
        assert plan["drives"] == []
        assert seen == []

    def test_a_drive_with_no_top_level_folders_gets_one_whole_drive_shard(self, monkeypatch):
        """The SITE stays over target (root_total > target_docs), so the
        site as a whole is NOT inline — but this drive has nothing to
        split by, so it gets exactly one shard covering the whole drive."""
        handler = _make_handler(root_total=1000, root_folders=[])
        _install_transport(monkeypatch, handler)
        target = _FakeTarget("drv1")

        plan = asyncio.run(shard_plan.compute_shard_plan(None, _FakeAuth(), {}, [target], target_docs=100))

        assert plan["inline_state_keys"] == []
        assert len(plan["drives"]) == 1
        drive_plan = plan["drives"][0]
        assert len(drive_plan["shards"]) == 1
        assert drive_plan["shards"][0]["targets"][0]["root_item_id"] is None

    def test_loose_root_files_are_recorded_on_the_drive_plan(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/root") and request.method == "GET":
                return httpx.Response(200, json={"webUrl": "https://x/root"})
            if path.endswith("/search/query"):
                body = json.loads(request.content.decode())
                query = body["requests"][0]["query"]["queryString"]
                if '"https://x/root"' in query:
                    return _search_response(1000)
                return _search_response(50)  # folder "A" — under target, never folded
            if path.endswith("/root/children"):
                return httpx.Response(
                    200,
                    json={
                        "value": [
                            {"id": "f1", "name": "A", "folder": {"childCount": 5}, "webUrl": "https://x/root/A"},
                            {"id": "loose1", "name": "readme.txt", "file": {}, "webUrl": "https://x/root/readme.txt"},
                        ]
                    },
                )
            raise AssertionError(f"unexpected request: {path}")

        _install_transport(monkeypatch, handler)
        target = _FakeTarget("drv1")

        plan = asyncio.run(shard_plan.compute_shard_plan(None, _FakeAuth(), {}, [target], target_docs=100))

        assert plan["drives"][0]["loose_root_files"] == ["readme.txt"]

    def test_a_small_drive_alongside_a_big_one_gets_its_own_whole_drive_shard(self, monkeypatch):
        """The SITE total (10 + 1000) is over target, so nothing is inline
        — but the small drive still gets exactly one whole-drive shard,
        never split, while the big one is folder-sharded."""

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/root") and request.method == "GET":
                if "drv-small" in path:
                    return httpx.Response(200, json={"webUrl": "https://x/drv-small/root"})
                return httpx.Response(200, json={"webUrl": "https://x/drv-big/root"})
            if path.endswith("/search/query"):
                body = json.loads(request.content.decode())
                query = body["requests"][0]["query"]["queryString"]
                if "drv-small/root" in query:
                    return _search_response(10)
                if "drv-big/root" in query:
                    return _search_response(1000)
                return _search_response(50)  # folder "A" — under target, never folded
            if path.endswith("/root/children"):
                return httpx.Response(
                    200,
                    json={
                        "value": [
                            {"id": "f1", "name": "A", "folder": {"childCount": 5}, "webUrl": "https://x/root/A"},
                        ]
                    },
                )
            raise AssertionError(f"unexpected request: {path}")

        _install_transport(monkeypatch, handler)
        small = _FakeTarget("drv-small")
        big = _FakeTarget("drv-big")

        plan = asyncio.run(shard_plan.compute_shard_plan(None, _FakeAuth(), {}, [small, big], target_docs=100))

        assert plan["inline_state_keys"] == []
        assert len(plan["drives"]) == 2
        by_drive = {d["drive_id"]: d for d in plan["drives"]}
        assert len(by_drive["drv-small"]["shards"]) == 1
        assert by_drive["drv-small"]["shards"][0]["targets"][0]["root_item_id"] is None
        assert len(by_drive["drv-big"]["shards"]) >= 1


# ---------------------------------------------------------------------------
# 2026-09-04 finding #65 — "the automatic shard planner must not depend on a
# Graph Search storm": signal precedence (known > childCount > Search-last),
# dedup by drive_id, and the shared search budget.
# ---------------------------------------------------------------------------


class TestSignalPrecedence:
    def test_childcount_wins_over_search_and_search_is_never_called_for_that_folder(self, monkeypatch):
        captured: List[str] = []
        handler = _make_handler(
            root_total=1000,
            root_folders=[_folder("f1", "A")],
            folder_child_counts={"A": 30},
            # Would balance to 600 if Search were called — proves it wasn't.
            folder_totals={"A": 600},
            captured_queries=captured,
        )
        _install_transport(monkeypatch, handler)
        target = _FakeTarget("drv1")

        plan = asyncio.run(shard_plan.compute_shard_plan(None, _FakeAuth(), {}, [target], target_docs=100))

        drive_plan = plan["drives"][0]
        assert drive_plan["signal"] == shard_plan.SIGNAL_CHILD_COUNT
        assert drive_plan["expected_total"] == 30
        assert not any('"https://x/root/A"' in q for q in captured), "Search must not be queried for folder A"

    def test_known_counts_win_over_both_childcount_and_search(self, monkeypatch):
        handler = _make_handler(
            root_total=1000,
            root_folders=[_folder("f1", "A")],
            folder_child_counts={"A": 30},
            folder_totals={"A": 600},
        )
        _install_transport(monkeypatch, handler)
        target = _FakeTarget("drv1")

        plan = asyncio.run(
            shard_plan.compute_shard_plan(
                None,
                _FakeAuth(),
                {},
                [target],
                target_docs=100,
                known_folder_counts={"drv1": {"A": 5}},
            )
        )

        drive_plan = plan["drives"][0]
        assert drive_plan["signal"] == shard_plan.SIGNAL_KNOWN
        assert drive_plan["expected_total"] == 5

    def test_known_totals_keep_a_drive_inline_with_zero_graph_calls(self, monkeypatch):
        seen = _install_transport(monkeypatch, _make_handler())
        target = _FakeTarget("drv1")

        plan = asyncio.run(
            shard_plan.compute_shard_plan(None, _FakeAuth(), {}, [target], target_docs=100, known_totals={"drv1": 42})
        )

        assert plan["inline_state_keys"] == ["drv1"]
        assert plan["signal"] == shard_plan.SIGNAL_KNOWN
        assert seen == [], "a known total must skip every Graph call for that drive"

    def test_a_raising_search_call_never_crashes_the_planner_or_loops(self, monkeypatch):
        """Simulates a misbehaving fake counter (a 429/timeout the real
        contract promises never happens) — the planner must treat it as
        'nothing known', not propagate, not retry."""
        calls = {"n": 0}

        async def raising_search(token, web_url, *, min_modified=None, exclude_extensions=None):
            calls["n"] += 1
            raise TimeoutError("simulated 429/timeout")

        monkeypatch.setattr(gc, "search_document_count", raising_search)
        handler = _make_handler(root_total=0, root_folders=[_folder("f1", "A")])
        _install_transport(monkeypatch, handler)
        target = _FakeTarget("drv1")

        plan = asyncio.run(shard_plan.compute_shard_plan(None, _FakeAuth(), {}, [target], target_docs=5))

        # Root search raised -> total unknown -> forced into Pass 2; folder A
        # has no childCount/known signal either, so its own search raises
        # too -> documents=0, signal=none, never an exception out of here.
        drive_plan = plan["drives"][0]
        assert drive_plan["signal"] == shard_plan.SIGNAL_NONE
        assert calls["n"] >= 1


class TestDedupByDriveId:
    def test_pass1_root_search_fires_once_for_many_targets_sharing_one_drive(self, monkeypatch):
        captured: List[str] = []
        handler = _make_handler(root_total=40, captured_queries=captured)
        _install_transport(monkeypatch, handler)
        targets = [_FakeTarget("drv1") for _ in range(5)]

        plan = asyncio.run(shard_plan.compute_shard_plan(None, _FakeAuth(), {}, targets, target_docs=100))

        assert plan["inline_state_keys"] == ["drv1"] * 5
        assert len(captured) == 1, "the root count must not repeat once per target sharing one drive"

    def test_pass2_folder_listing_and_counts_fire_once_for_many_targets_sharing_one_drive(self, monkeypatch):
        captured: List[str] = []
        handler = _make_handler(
            root_total=1000,
            root_folders=[_folder("f1", "A"), _folder("f2", "B")],
            folder_totals={"A": 600, "B": 400},
            captured_queries=captured,
        )
        seen = _install_transport(monkeypatch, handler)
        targets = [_FakeTarget("drv1") for _ in range(5)]

        plan = asyncio.run(shard_plan.compute_shard_plan(None, _FakeAuth(), {}, targets, target_docs=100))

        assert len(plan["drives"]) == 5, "one drive-plan entry per target, even though the drive is shared"
        assert all(d == plan["drives"][0] for d in plan["drives"]), "every entry for a shared drive must be identical"
        listing_calls = [u for u in seen if "/root/children" in u]
        assert len(listing_calls) == 1, "the folder listing must not repeat once per target sharing one drive"
        # 1 root-total search + 2 per-folder searches = 3, never 3 * 5.
        assert len(captured) == 3


class TestSearchBudget:
    class _AfterPass1Clock:
        """A clock that reads 0.0 for `_PlanBudget`'s own deadline
        calculation AND for Pass 1's one `exceeded()` check (so the root
        search itself still fires), then a huge value on every later read —
        trips the budget right after Pass 1, deterministically, with no
        real sleeping (same test-seam idiom as `_Deadline`'s own `clock`
        parameter elsewhere in this connector)."""

        def __init__(self) -> None:
            self._calls = 0

        def __call__(self) -> float:
            self._calls += 1
            return 0.0 if self._calls <= 2 else 1_000_000.0

    def test_budget_exceeded_before_any_call_skips_search_and_uses_zero(self, monkeypatch):
        handler = _make_handler(root_total=999, root_folders=[_folder("f1", "A"), _folder("f2", "B")])
        _install_transport(monkeypatch, handler)
        target = _FakeTarget("drv1")

        plan = asyncio.run(
            shard_plan.compute_shard_plan(
                None,
                _FakeAuth(),
                {},
                [target],
                target_docs=5,
                budget_s=1.0,
                clock=self._AfterPass1Clock(),
            )
        )

        # Pass 1's own root search succeeded (999, nonzero) BEFORE budget()
        # is ever consulted for it — that IS a usable signal, so this is a
        # degraded-but-usable plan, never `PlanningBudgetExhausted`. Every
        # per-folder count still hits the (already-tripped) budget and
        # balances as 0/none.
        drive_plan = plan["drives"][0]
        assert drive_plan["signal"] == shard_plan.SIGNAL_NONE
        packed_shards = drive_plan["shards"][:-1]  # drop the remainder
        assert all(s["expected"] == 0 for s in packed_shards)
        assert all(t["signal"] == shard_plan.SIGNAL_NONE for s in packed_shards for t in s["targets"])

    def test_budget_exhausted_with_no_signal_anywhere_raises_planning_budget_exhausted(self, monkeypatch):
        handler = _make_handler(root_total=999, root_folders=[_folder("f1", "A")])
        _install_transport(monkeypatch, handler)
        target = _FakeTarget("drv1")

        class _AlwaysTrippedClock:
            def __init__(self) -> None:
                self._calls = 0

            def __call__(self) -> float:
                self._calls += 1
                # First read seeds the deadline at 0 + budget_s; EVERY read
                # after that (including the very first `.exceeded()` check,
                # inside Pass 1's own loop) reads far past it.
                return 0.0 if self._calls == 1 else 1_000_000.0

        with pytest.raises(shard_plan.PlanningBudgetExhausted):
            asyncio.run(
                shard_plan.compute_shard_plan(
                    None,
                    _FakeAuth(),
                    {},
                    [target],
                    target_docs=5,
                    budget_s=0.01,
                    clock=_AlwaysTrippedClock(),
                )
            )

    def test_progress_callback_reports_folders_done_and_total(self, monkeypatch):
        handler = _make_handler(
            root_total=1000,
            root_folders=[_folder("f1", "A"), _folder("f2", "B")],
            folder_child_counts={"A": 30, "B": 10},
        )
        _install_transport(monkeypatch, handler)
        target = _FakeTarget("drv1")
        calls: List[tuple] = []

        asyncio.run(
            shard_plan.compute_shard_plan(
                None,
                _FakeAuth(),
                {},
                [target],
                target_docs=5,
                on_progress=lambda done, total: calls.append((done, total)),
            )
        )

        # Both folders resolve via childCount alone (no Search) — the final
        # `finish()` call still reports the completed tally.
        assert calls, "on_progress must fire at least once (the final tally)"
        assert calls[-1] == (2, 2)
