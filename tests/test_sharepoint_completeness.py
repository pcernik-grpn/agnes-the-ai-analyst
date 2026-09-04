"""``connectors.sharepoint.completeness`` — "did we really get everything?"
per scope (and, for a single drive-root scope, per top-level folder).

No live network: the Graph client's ``_http_client()`` seam is monkeypatched
to an ``httpx.MockTransport``, same idiom as ``tests/test_sharepoint_graph_
client.py`` / ``tests/test_admin_sharepoint.py``'s split-plan tests. Crawl
state is faked directly (``connectors.sharepoint.state_store.get``) rather
than run through a real crawl.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import httpx

from connectors.sharepoint import completeness as cpl
from connectors.sharepoint import graph_client as gc


def _connection(scopes: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"id": "conn1", "config": {"scopes": scopes}}


def _drive_scope(
    *, source_scope_id: str = "b!drive1", drive_id: str = "drv1", collection_id: str = "col1", display_path="Docs"
):
    return {
        "source_scope_id": source_scope_id,
        "drive_id": drive_id,
        "collection_id": collection_id,
        "display_path": display_path,
    }


def _folder_scope(
    *,
    source_scope_id: str = "item-folder-1",
    drive_id: str = "drv1",
    collection_id: str = "col1",
    display_path="Docs/Reports",
):
    return {
        "source_scope_id": source_scope_id,
        "drive_id": drive_id,
        "collection_id": collection_id,
        "display_path": display_path,
    }


def _site_scope(*, source_scope_id: str = "site1,web1", collection_id: str = "col1", display_path="Whole site"):
    return {"source_scope_id": source_scope_id, "collection_id": collection_id, "display_path": display_path}


class _FakeFilesRepo:
    """Minimal stand-in for ``corpus_files_repo()`` — only the two read
    methods :mod:`connectors.sharepoint.completeness` calls."""

    def __init__(
        self,
        status_by_collection: Dict[str, Dict[str, int]],
        top_folder_by_collection: Optional[Dict[str, Dict[str, Dict[str, int]]]] = None,
    ):
        self._status = status_by_collection
        self._top_folder = top_folder_by_collection or {}

    def status_counts_for_corpora(self, collection_ids):
        return {cid: self._status.get(cid, {}) for cid in collection_ids if cid in self._status}

    def top_folder_status_counts(self, collection_id):
        return self._top_folder.get(collection_id, {})


def _install_transport(monkeypatch, handler) -> None:
    monkeypatch.setattr(
        gc, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
    )


def _install_repo(monkeypatch, repo) -> None:
    monkeypatch.setattr("src.repositories.corpus_files_repo", lambda: repo)


def _install_state(monkeypatch, state: Dict[str, Any]) -> None:
    monkeypatch.setattr("connectors.sharepoint.state_store.get", lambda kind, connection_id: state)


def _drive_root_and_search_handler(*, drive_id: str, web_url: str, count: int, root_item_id: Optional[str] = None):
    """A single-scope handler covering the scope's own web_url lookup + the
    Search count. A "drive"-kind scope (``root_item_id=None``) ALSO gets a
    per-folder breakdown (:mod:`connectors.sharepoint.completeness`'s
    single-drive-scope rule), so this also answers ``/root/children`` with
    an empty folder list — tests that need real folders install their own
    handler instead."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if root_item_id is None and path == f"/v1.0/drives/{drive_id}/root":
            return httpx.Response(200, json={"webUrl": web_url})
        if root_item_id is not None and path == f"/v1.0/drives/{drive_id}/items/{root_item_id}":
            return httpx.Response(200, json={"webUrl": web_url})
        if root_item_id is None and path == f"/v1.0/drives/{drive_id}/root/children":
            return httpx.Response(200, json={"value": []})
        if path == "/v1.0/search/query":
            body = json.loads(request.content)
            query = body["requests"][0]["query"]["queryString"]
            assert f'path:"{web_url}"' in query
            return httpx.Response(200, json={"value": [{"hitsContainers": [{"total": count}]}]})
        raise AssertionError(f"unexpected path {path}")

    return handler


class TestScopeStatus:
    def test_complete_when_indexed_covers_expected(self, monkeypatch):
        _install_transport(
            monkeypatch,
            _drive_root_and_search_handler(
                drive_id="drv1", web_url="https://example.sharepoint.com/sites/s/Docs", count=5
            ),
        )
        _install_repo(monkeypatch, _FakeFilesRepo({"col1": {"indexed": 5}}))
        _install_state(monkeypatch, {})

        connection = _connection([_drive_scope()])
        report = asyncio.run(cpl.compute_completeness(connection, min_modified=None, token="tok"))
        scope_row = next(r for r in report["rows"] if r["kind"] == "scope")
        assert scope_row["expected"] == 5
        assert scope_row["indexed"] == 5
        assert scope_row["gap"] == 0
        assert scope_row["status"] == "complete"
        assert report["total"]["status"] == "complete"

    def test_accounted_when_reasons_cover_the_gap(self, monkeypatch):
        _install_transport(
            monkeypatch,
            _drive_root_and_search_handler(
                drive_id="drv1", web_url="https://example.sharepoint.com/sites/s/Docs", count=10
            ),
        )
        _install_repo(monkeypatch, _FakeFilesRepo({"col1": {"indexed": 7}}))
        _install_state(
            monkeypatch,
            {
                "failed_items": {"a": {"state_key": "drv1", "path": "Docs/a.pdf"}},
                "empty_items": {"b": {"state_key": "drv1", "path": "Docs/b.pdf"}},
                "last_run": {"skipped_unsupported": 1, "skipped_oversize": {"files": 0}},
            },
        )

        connection = _connection([_drive_scope()])
        report = asyncio.run(cpl.compute_completeness(connection, min_modified=None, token="tok"))
        scope_row = next(r for r in report["rows"] if r["kind"] == "scope")
        assert scope_row["expected"] == 10
        assert scope_row["indexed"] == 7
        assert scope_row["failed"] == 1
        assert scope_row["empty"] == 1
        assert scope_row["skipped_unsupported"] == 1
        assert scope_row["gap"] == 0
        assert scope_row["status"] == "accounted"

    def test_missing_when_gap_is_unexplained(self, monkeypatch):
        _install_transport(
            monkeypatch,
            _drive_root_and_search_handler(
                drive_id="drv1", web_url="https://example.sharepoint.com/sites/s/Docs", count=10
            ),
        )
        _install_repo(monkeypatch, _FakeFilesRepo({"col1": {"indexed": 7}}))
        _install_state(monkeypatch, {})

        connection = _connection([_drive_scope()])
        report = asyncio.run(cpl.compute_completeness(connection, min_modified=None, token="tok"))
        scope_row = next(r for r in report["rows"] if r["kind"] == "scope")
        assert scope_row["gap"] == 3
        assert scope_row["status"] == "missing"

    def test_site_scope_is_unknown_not_zero(self, monkeypatch):
        """A 'site' scope spans multiple drives — there is no single
        web_url to count against, so expected is None/'unknown', never a
        silent 0 that would read as 'everything is missing'."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"no Graph call expected for a site scope, got {request.url.path}")

        _install_transport(monkeypatch, handler)
        _install_repo(monkeypatch, _FakeFilesRepo({"col1": {"indexed": 3}}))
        _install_state(monkeypatch, {})

        connection = _connection([_site_scope()])
        report = asyncio.run(cpl.compute_completeness(connection, min_modified=None, token="tok"))
        scope_row = next(r for r in report["rows"] if r["kind"] == "scope")
        assert scope_row["expected"] is None
        assert scope_row["gap"] is None
        assert scope_row["status"] == "unknown"
        assert report["total"]["status"] == "unknown"
        assert report["caveats"]

    def test_folder_scope_state_key_includes_root_item_id(self, monkeypatch):
        _install_transport(
            monkeypatch,
            _drive_root_and_search_handler(
                drive_id="drv1",
                web_url="https://example.sharepoint.com/sites/s/Docs/Reports",
                count=2,
                root_item_id="item-folder-1",
            ),
        )
        _install_repo(monkeypatch, _FakeFilesRepo({"col1": {"indexed": 2}}))
        _install_state(monkeypatch, {})

        connection = _connection([_folder_scope()])
        report = asyncio.run(cpl.compute_completeness(connection, min_modified=None, token="tok"))
        scope_row = next(r for r in report["rows"] if r["kind"] == "scope")
        assert scope_row["expected"] == 2
        assert scope_row["status"] == "complete"


class TestFolderRows:
    def test_single_drive_scope_gets_per_folder_rows(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/v1.0/drives/drv1/root":
                return httpx.Response(200, json={"webUrl": "https://example.sharepoint.com/sites/s/Docs"})
            if path == "/v1.0/drives/drv1/root/children":
                return httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "id": "f-reports",
                                "name": "Reports",
                                "folder": {"childCount": 1},
                                "webUrl": "https://example.sharepoint.com/sites/s/Docs/Reports",
                            },
                            {
                                "id": "f-notes",
                                "name": "Notes",
                                "folder": {"childCount": 1},
                                "webUrl": "https://example.sharepoint.com/sites/s/Docs/Notes",
                            },
                        ]
                    },
                )
            if path == "/v1.0/search/query":
                body = json.loads(request.content)
                query = body["requests"][0]["query"]["queryString"]
                counts = {
                    "https://example.sharepoint.com/sites/s/Docs": 5,
                    "https://example.sharepoint.com/sites/s/Docs/Reports": 3,
                    "https://example.sharepoint.com/sites/s/Docs/Notes": 2,
                }
                for web_url, total in counts.items():
                    if f'path:"{web_url}"' in query:
                        return httpx.Response(200, json={"value": [{"hitsContainers": [{"total": total}]}]})
                raise AssertionError(query)
            raise AssertionError(path)

        _install_transport(monkeypatch, handler)
        _install_repo(
            monkeypatch,
            _FakeFilesRepo(
                {"col1": {"indexed": 4}},
                {"col1": {"Reports": {"indexed": 3}, "Notes": {"indexed": 1}}},
            ),
        )
        _install_state(
            monkeypatch,
            {
                "failed_items": {"a": {"state_key": "drv1", "path": "Notes/broken.pdf"}},
                "last_run": {"skipped_unsupported": 0, "skipped_oversize": {"files": 0}},
            },
        )

        connection = _connection([_drive_scope()])
        report = asyncio.run(cpl.compute_completeness(connection, min_modified=None, token="tok"))
        folder_rows = {r["label"]: r for r in report["rows"] if r["kind"] == "folder"}
        assert set(folder_rows) == {"Reports", "Notes"}
        assert folder_rows["Reports"]["expected"] == 3
        assert folder_rows["Reports"]["indexed"] == 3
        assert folder_rows["Reports"]["status"] == "complete"
        assert folder_rows["Notes"]["expected"] == 2
        assert folder_rows["Notes"]["indexed"] == 1
        assert folder_rows["Notes"]["failed"] == 1
        # expected 2, indexed 1: the missing document has a recorded reason
        # (the one failed item), so this is accounted-for, not unexplained.
        assert folder_rows["Notes"]["status"] == "accounted"
        for row in folder_rows.values():
            assert row["parent_scope_id"] == "b!drive1"

    def test_folder_scope_never_gets_folder_rows(self, monkeypatch):
        """Only a whole-DRIVE scope is broken into folders — a folder-kind
        scope is already one folder, so a further breakdown would be
        meaningless."""
        _install_transport(
            monkeypatch,
            _drive_root_and_search_handler(
                drive_id="drv1",
                web_url="https://example.sharepoint.com/sites/s/Docs/Reports",
                count=2,
                root_item_id="item-folder-1",
            ),
        )
        _install_repo(monkeypatch, _FakeFilesRepo({"col1": {"indexed": 2}}))
        _install_state(monkeypatch, {})

        connection = _connection([_folder_scope()])
        report = asyncio.run(cpl.compute_completeness(connection, min_modified=None, token="tok"))
        assert not any(r["kind"] == "folder" for r in report["rows"])

    def test_multi_scope_connection_never_gets_folder_rows(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/v1.0/drives/drv1/root":
                return httpx.Response(200, json={"webUrl": "https://example.sharepoint.com/sites/s/Docs"})
            if path == "/v1.0/drives/drv2/root":
                return httpx.Response(200, json={"webUrl": "https://example.sharepoint.com/sites/s2/Docs"})
            if path == "/v1.0/search/query":
                return httpx.Response(200, json={"value": [{"hitsContainers": [{"total": 1}]}]})
            raise AssertionError(path)

        _install_transport(monkeypatch, handler)
        _install_repo(monkeypatch, _FakeFilesRepo({"col1": {"indexed": 1}, "col2": {"indexed": 1}}))
        _install_state(monkeypatch, {})

        connection = _connection(
            [
                _drive_scope(source_scope_id="b!drive1", drive_id="drv1"),
                _drive_scope(source_scope_id="b!drive2", drive_id="drv2", collection_id="col2"),
            ]
        )
        report = asyncio.run(cpl.compute_completeness(connection, min_modified=None, token="tok"))
        assert not any(r["kind"] == "folder" for r in report["rows"])


class TestMultiScopeAttribution:
    def test_failed_items_attributed_by_state_key_and_totals_stay_exact(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/v1.0/drives/drv1/root":
                return httpx.Response(200, json={"webUrl": "https://example.sharepoint.com/sites/s1/Docs"})
            if path == "/v1.0/drives/drv2/root":
                return httpx.Response(200, json={"webUrl": "https://example.sharepoint.com/sites/s2/Docs"})
            if path == "/v1.0/search/query":
                body = json.loads(request.content)
                query = body["requests"][0]["query"]["queryString"]
                total = 5 if "s1" in query else 4
                return httpx.Response(200, json={"value": [{"hitsContainers": [{"total": total}]}]})
            raise AssertionError(path)

        _install_transport(monkeypatch, handler)
        _install_repo(monkeypatch, _FakeFilesRepo({"col1": {"indexed": 4}, "col2": {"indexed": 4}}))
        _install_state(
            monkeypatch,
            {
                "failed_items": {
                    "a": {"state_key": "drv1", "path": "Docs/a.pdf"},
                    "b": {"state_key": "drv2", "path": "Docs/b.pdf"},
                },
                "last_run": {"skipped_unsupported": 0, "skipped_oversize": {"files": 0}},
            },
        )

        connection = _connection(
            [
                _drive_scope(source_scope_id="b!drive1", drive_id="drv1"),
                _drive_scope(source_scope_id="b!drive2", drive_id="drv2", collection_id="col2"),
            ]
        )
        report = asyncio.run(cpl.compute_completeness(connection, min_modified=None, token="tok"))
        rows = {r["scope_id"]: r for r in report["rows"] if r["kind"] == "scope"}
        assert rows["b!drive1"]["failed"] == 1
        assert rows["b!drive2"]["failed"] == 1
        # The connection total is always the EXACT count, never a sum of
        # possibly-incomplete per-scope attributions.
        assert report["total"]["failed"] == 2

    def test_oversize_unattributable_per_scope_still_shows_in_total(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/v1.0/drives/drv1/root":
                return httpx.Response(200, json={"webUrl": "https://example.sharepoint.com/sites/s1/Docs"})
            if path == "/v1.0/drives/drv2/root":
                return httpx.Response(200, json={"webUrl": "https://example.sharepoint.com/sites/s2/Docs"})
            if path == "/v1.0/search/query":
                return httpx.Response(200, json={"value": [{"hitsContainers": [{"total": 3}]}]})
            raise AssertionError(path)

        _install_transport(monkeypatch, handler)
        _install_repo(monkeypatch, _FakeFilesRepo({"col1": {"indexed": 3}, "col2": {"indexed": 3}}))
        _install_state(monkeypatch, {"last_run": {"skipped_unsupported": 0, "skipped_oversize": {"files": 4}}})

        connection = _connection(
            [
                _drive_scope(source_scope_id="b!drive1", drive_id="drv1"),
                _drive_scope(source_scope_id="b!drive2", drive_id="drv2", collection_id="col2"),
            ]
        )
        report = asyncio.run(cpl.compute_completeness(connection, min_modified=None, token="tok"))
        rows = [r for r in report["rows"] if r["kind"] == "scope"]
        assert all(r["oversize"] == 0 for r in rows)
        assert report["total"]["oversize"] == 4


class TestExcludedExtensions:
    def test_expected_query_excludes_unsupported_extensions(self, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/v1.0/drives/drv1/root":
                return httpx.Response(200, json={"webUrl": "https://example.sharepoint.com/sites/s/Docs"})
            if path == "/v1.0/drives/drv1/root/children":
                return httpx.Response(200, json={"value": []})
            if path == "/v1.0/search/query":
                body = json.loads(request.content)
                captured["query"] = body["requests"][0]["query"]["queryString"]
                return httpx.Response(200, json={"value": [{"hitsContainers": [{"total": 1}]}]})
            raise AssertionError(path)

        _install_transport(monkeypatch, handler)
        _install_repo(monkeypatch, _FakeFilesRepo({"col1": {"indexed": 1}}))
        _install_state(monkeypatch, {})

        connection = _connection([_drive_scope()])
        asyncio.run(cpl.compute_completeness(connection, min_modified=None, token="tok"))
        assert "fileextension:mp4" in captured["query"]
        assert "NOT (" in captured["query"]
