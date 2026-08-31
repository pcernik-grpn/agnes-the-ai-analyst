"""``connectors.sharepoint.crawler`` — the built-in document crawler.

No live network and no database: Graph is an ``httpx.MockTransport`` behind
``graph_client._http_client`` (the same seam
``tests/test_sharepoint_graph_client.py`` uses), the token exchange is
stubbed at ``graph_client.get_app_token`` so no certificate is needed, and
the collections ingest path is replaced by a recording fake — this module's
own contract is "what does the crawl do with a Graph response", not "does
Postgres accept the row".

The two seams written in parallel with this module
(``connectors.sharepoint.convert`` / ``src.anonymization``) are substituted
at their wrapper functions, which is exactly the substitution point those
wrappers exist to provide.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx
import pytest

from connectors.sharepoint import crawler
from connectors.sharepoint import graph_client as gc

GRAPH = "https://graph.microsoft.com/v1.0"
DRIVE_DELTA = f"{GRAPH}/drives/b!drive1/root/delta"


# --------------------------------------------------------------------------
# Fixtures / fakes
# --------------------------------------------------------------------------


class FakeIngestor:
    """Stands in for ``crawler._Ingestor`` — records what would be written."""

    instances: List["FakeIngestor"] = []

    def __init__(self) -> None:
        self.ingested: List[Dict[str, Any]] = []
        self.deleted: List[str] = []
        self.known: Dict[str, str] = {}
        FakeIngestor.instances.append(self)

    def ingest(
        self,
        *,
        collection_id: str,
        stable_id: str,
        path: str,
        filename: str,
        markdown: str,
        source_sha256: str,
    ):
        self.ingested.append(
            {
                "collection_id": collection_id,
                "stable_id": stable_id,
                "path": path,
                "filename": filename,
                "markdown": markdown,
                "source_sha256": source_sha256,
            }
        )
        was_new = stable_id not in self.known
        self.known[stable_id] = f"file-{len(self.known)}"
        return self.known[stable_id], was_new

    def delete(self, collection_id: str, stable_id: str) -> bool:
        if stable_id in self.known:
            del self.known[stable_id]
            self.deleted.append(stable_id)
            return True
        self.deleted.append(stable_id)
        return True


class ConvertResult:
    def __init__(self, markdown: str, engine: str = "fake") -> None:
        self.markdown = markdown
        self.engine = engine


class AnonymizeResult:
    def __init__(self, text: str, replaced: int = 0) -> None:
        self.text = text
        self.replaced = replaced


def _connection(scopes: List[Dict[str, Any]], connection_id: str = "conn1") -> Dict[str, Any]:
    return {
        "id": connection_id,
        "source_type": "sharepoint",
        "config": {"tenant_id": "t1", "client_id": "c1", "scopes": scopes},
    }


def _drive_scope(**overrides: Any) -> Dict[str, Any]:
    scope = {
        "source_scope_id": "b!drive1",
        "display_path": "Corp / Documents",
        "collection_id": "col1",
        "anonymize": False,
    }
    scope.update(overrides)
    return scope


def _file_item(
    item_id: str = "item1",
    *,
    name: str = "brief.docx",
    ctag: str = "ctag-1",
    size: int = 1024,
    parent_path: str = "/drives/b!drive1/root:/Reports",
) -> Dict[str, Any]:
    return {
        "id": item_id,
        "name": name,
        "cTag": ctag,
        "size": size,
        "file": {"mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
        "parentReference": {"path": parent_path},
    }


@pytest.fixture
def crawl_env(tmp_path, monkeypatch):
    """DATA_DIR-isolated crawl state + stubbed token + fake ingest path."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("STATE_DIR", raising=False)

    async def _token(tenant_id: str, client_id: str, private_key: str) -> str:
        return "tok"

    monkeypatch.setattr(gc, "get_app_token", _token)
    monkeypatch.setattr(crawler, "_Ingestor", FakeIngestor)
    monkeypatch.setattr(crawler, "convert_to_markdown", lambda path, mime: ConvertResult("# converted"))
    monkeypatch.setattr(crawler, "_max_file_mb", lambda: 50)
    # Certificate resolution is `connectors.sharepoint.settings`' contract,
    # covered by its own tests — stubbed here so no crawl test needs a real
    # key pair, and so a settings failure can never masquerade as a crawl bug.
    monkeypatch.setattr(crawler, "resolve_sharepoint_settings", lambda connection: _FakeSettings())
    FakeIngestor.instances.clear()
    yield tmp_path


class _FakeSettings:
    tenant_id = "t1"
    client_id = "c1"
    private_key = "pem"
    credential_source = "vault"
    credential_env = None
    credential_set_at = None


def _install_graph(monkeypatch, handler: Callable[[httpx.Request], httpx.Response]) -> List[str]:
    """Wire ``graph_client._http_client`` to a MockTransport; return the list
    every requested URL is appended to (order-preserving, for assertions)."""
    seen: List[str] = []

    def _wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return handler(request)

    def _client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(_wrapped), timeout=10)

    monkeypatch.setattr(gc, "_http_client", _client)
    return seen


def _content_response() -> httpx.Response:
    return httpx.Response(200, content=b"original-bytes")


def _state(tmp_path: Path, connection_id: str = "conn1") -> Dict[str, Any]:
    path = tmp_path / "state" / "sharepoint_crawl" / f"{connection_id}.json"
    return json.loads(path.read_text())


def _run(connection: Dict[str, Any], monkeypatch, scopes: Optional[List[str]] = None) -> Dict[str, Any]:
    monkeypatch.setattr(
        "src.repositories.source_connections_repo",
        lambda: type("R", (), {"get": staticmethod(lambda cid: connection)})(),
    )
    payload: Dict[str, Any] = {"connection_id": connection["id"]}
    if scopes:
        payload["scopes"] = scopes
    return crawler.run_builtin_crawl(payload)


# --------------------------------------------------------------------------
# Delta flow
# --------------------------------------------------------------------------


class TestDeltaFlow:
    def test_pages_through_delta_and_persists_the_delta_link(self, crawl_env, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/content"):
                return _content_response()
            if "nextpage" in url:
                return httpx.Response(
                    200,
                    json={
                        "value": [_file_item("item2", name="b.pdf", ctag="ctag-2")],
                        "@odata.deltaLink": f"{DRIVE_DELTA}?token=NEW",
                    },
                )
            return httpx.Response(
                200,
                json={"value": [_file_item("item1")], "@odata.nextLink": f"{DRIVE_DELTA}?nextpage=1"},
            )

        _install_graph(monkeypatch, handler)
        report = _run(_connection([_drive_scope()]), monkeypatch)

        ingestor = FakeIngestor.instances[-1]
        assert [row["stable_id"] for row in ingestor.ingested] == ["graph:item1", "graph:item2"]
        assert report["new"] == 2 and report["changed"] == 0
        assert report["drives"] == 1 and report["scopes"] == 1

        state = _state(crawl_env)
        assert state["delta_links"]["b!drive1"] == f"{DRIVE_DELTA}?token=NEW"
        assert state["ctags"] == {"graph:item1": "ctag-1", "graph:item2": "ctag-2"}
        assert state["last_run"]["new"] == 2

    def test_drive_relative_path_and_markdown_filename(self, crawl_env, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return _content_response()
            return httpx.Response(200, json={"value": [_file_item()], "@odata.deltaLink": f"{DRIVE_DELTA}?t=1"})

        _install_graph(monkeypatch, handler)
        _run(_connection([_drive_scope()]), monkeypatch)

        row = FakeIngestor.instances[-1].ingested[0]
        # The library segment is NOT part of a drive-relative path.
        assert row["path"] == "Reports/brief.docx"
        assert row["filename"] == "brief.md"
        assert row["markdown"] == "# converted"
        assert row["collection_id"] == "col1"

    def test_deleted_item_removes_the_anchored_file_and_its_ctag(self, crawl_env, monkeypatch):
        pages = iter(
            [
                {"value": [_file_item()], "@odata.deltaLink": f"{DRIVE_DELTA}?t=1"},
                {
                    "value": [{"id": "item1", "name": "brief.docx", "deleted": {"state": "deleted"}}],
                    "@odata.deltaLink": f"{DRIVE_DELTA}?t=2",
                },
            ]
        )
        page_holder = {"page": next(pages)}

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return _content_response()
            return httpx.Response(200, json=page_holder["page"])

        _install_graph(monkeypatch, handler)
        connection = _connection([_drive_scope()])
        _run(connection, monkeypatch)
        first = FakeIngestor.instances[-1]
        assert first.ingested

        page_holder["page"] = next(pages)
        report = _run(connection, monkeypatch)

        assert report["deleted"] == 1
        assert FakeIngestor.instances[-1].deleted == ["graph:item1"]
        assert "graph:item1" not in _state(crawl_env)["ctags"]

    def test_office_lock_files_and_os_droppings_are_never_crawled(self, crawl_env, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "value": [
                        _file_item("i1", name="~$brief.docx"),
                        _file_item("i2", name=".DS_Store"),
                        {"id": "i3", "name": "a-folder", "folder": {"childCount": 0}},
                    ],
                    "@odata.deltaLink": f"{DRIVE_DELTA}?t=1",
                },
            )

        seen = _install_graph(monkeypatch, handler)
        report = _run(_connection([_drive_scope()]), monkeypatch)

        assert FakeIngestor.instances[-1].ingested == []
        assert report["new"] == 0
        assert not any(url.endswith("/content") for url in seen)


# --------------------------------------------------------------------------
# 410 Gone -> full resync
# --------------------------------------------------------------------------


class TestDeltaGoneResync:
    def test_expired_delta_link_is_dropped_and_the_drive_is_re_enumerated(self, crawl_env, monkeypatch):
        crawler.save_state(
            "conn1",
            {"delta_links": {"b!drive1": f"{DRIVE_DELTA}?token=DEAD"}, "ctags": {}},
        )

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/content"):
                return _content_response()
            if "token=DEAD" in url:
                return httpx.Response(410, json={"error": {"code": "resyncRequired"}})
            return httpx.Response(200, json={"value": [_file_item()], "@odata.deltaLink": f"{DRIVE_DELTA}?token=FRESH"})

        seen = _install_graph(monkeypatch, handler)
        report = _run(_connection([_drive_scope()]), monkeypatch)

        assert report["delta_resyncs"] == 1
        # The dead link was replayed once, then the drive restarted from its
        # parameterless delta base — never from the dead link again.
        assert sum(1 for url in seen if "token=DEAD" in url) == 1
        assert any("/root/delta?%24top=" in url or "/root/delta?$top=" in url for url in seen)
        assert _state(crawl_env)["delta_links"]["b!drive1"] == f"{DRIVE_DELTA}?token=FRESH"
        assert FakeIngestor.instances[-1].ingested

    def test_a_second_410_in_one_pass_propagates(self, crawl_env, monkeypatch):
        crawler.save_state("conn1", {"delta_links": {"b!drive1": f"{DRIVE_DELTA}?token=DEAD"}, "ctags": {}})
        _install_graph(monkeypatch, lambda request: httpx.Response(410, json={}))

        with pytest.raises(crawler.GraphGone):
            _run(_connection([_drive_scope()]), monkeypatch)

        # The pass still recorded what it knew — an interrupted run owes the
        # operator its numbers.
        assert _state(crawl_env)["last_run"]["interrupted"] is True


# --------------------------------------------------------------------------
# Resume / idempotency
# --------------------------------------------------------------------------


class TestResume:
    def test_unchanged_ctag_skips_the_download_entirely(self, crawl_env, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return _content_response()
            return httpx.Response(200, json={"value": [_file_item()], "@odata.deltaLink": f"{DRIVE_DELTA}?t=1"})

        connection = _connection([_drive_scope()])
        seen = _install_graph(monkeypatch, handler)
        first = _run(connection, monkeypatch)
        assert first["new"] == 1
        downloads_after_first = sum(1 for url in seen if url.endswith("/content"))

        second = _run(connection, monkeypatch)
        assert second["unchanged"] == 1
        assert second["new"] == 0 and second["changed"] == 0
        assert FakeIngestor.instances[-1].ingested == []
        assert sum(1 for url in seen if url.endswith("/content")) == downloads_after_first

    def test_a_changed_ctag_re_ingests_the_same_stable_id(self, crawl_env, monkeypatch):
        ctag = {"value": "ctag-1"}

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return _content_response()
            return httpx.Response(
                200,
                json={"value": [_file_item(ctag=ctag["value"])], "@odata.deltaLink": f"{DRIVE_DELTA}?t=1"},
            )

        _install_graph(monkeypatch, handler)
        connection = _connection([_drive_scope()])
        _run(connection, monkeypatch)
        ctag["value"] = "ctag-2"
        report = _run(connection, monkeypatch)

        assert report["unchanged"] == 0
        assert FakeIngestor.instances[-1].ingested[0]["stable_id"] == "graph:item1"
        assert _state(crawl_env)["ctags"]["graph:item1"] == "ctag-2"

    def test_a_ctag_is_recorded_only_after_the_ingest_succeeded(self, crawl_env, monkeypatch):
        """A crash between download and ingest must leave the item looking
        un-crawled, or the resumed run skips a file it never landed."""

        class FailingIngestor(FakeIngestor):
            def ingest(self, **kwargs):
                raise RuntimeError("ingest exploded")

        monkeypatch.setattr(crawler, "_Ingestor", FailingIngestor)

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return _content_response()
            return httpx.Response(200, json={"value": [_file_item()], "@odata.deltaLink": f"{DRIVE_DELTA}?t=1"})

        _install_graph(monkeypatch, handler)
        report = _run(_connection([_drive_scope()]), monkeypatch)

        assert report["errors"] == 1
        assert report["new"] == 0
        assert _state(crawl_env)["ctags"] == {}


# --------------------------------------------------------------------------
# Anonymize-marked scopes: fail CLOSED
# --------------------------------------------------------------------------


class TestAnonymizeFailClosed:
    @pytest.fixture(autouse=True)
    def _key(self, monkeypatch):
        monkeypatch.setenv("AGNES_ANONYMIZATION_HMAC_KEY", "unit-test-key")

    def _handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/content"):
            return _content_response()
        # Distinct item ids per drive: a Graph item id is unique within its
        # drive, and the crawl's cTag map is keyed on it, so a fixture that
        # reused one id across two drives would test a shape Graph never emits.
        drive = "b!drive2" if "b!drive2" in url else "b!drive1"
        return httpx.Response(
            200,
            json={
                "value": [_file_item(f"item-{drive}", parent_path=f"/drives/{drive}/root:/Reports")],
                "@odata.deltaLink": f"{GRAPH}/drives/{drive}/root/delta?t=1",
            },
        )

    def test_anonymized_text_is_what_gets_ingested(self, crawl_env, monkeypatch):
        monkeypatch.setattr(
            crawler, "anonymize_markdown", lambda text, *, key: AnonymizeResult("PERSON_abc met PERSON_def", 2)
        )
        _install_graph(monkeypatch, self._handler)
        report = _run(_connection([_drive_scope(anonymize=True)]), monkeypatch)

        assert report["anonymize_failed"] == 0
        assert FakeIngestor.instances[-1].ingested[0]["markdown"] == "PERSON_abc met PERSON_def"

    def test_a_document_that_cannot_be_anonymized_is_never_ingested_raw(self, crawl_env, monkeypatch):
        def _boom(text, *, key):
            raise RuntimeError("anonymizer blew up")

        monkeypatch.setattr(crawler, "anonymize_markdown", _boom)
        _install_graph(monkeypatch, self._handler)
        report = _run(_connection([_drive_scope(anonymize=True)]), monkeypatch)

        assert report["anonymize_failed"] == 1
        assert FakeIngestor.instances[-1].ingested == []
        # Not marked crawled either — a skipped document must be retried, not
        # silently treated as done.
        assert _state(crawl_env)["ctags"] == {}

    def test_a_missing_anonymization_module_is_treated_as_failure_not_as_pass_through(self, crawl_env, monkeypatch):
        def _not_landed(text, *, key):
            raise ImportError("No module named 'src.anonymization'")

        monkeypatch.setattr(crawler, "anonymize_markdown", _not_landed)
        _install_graph(monkeypatch, self._handler)
        report = _run(_connection([_drive_scope(anonymize=True)]), monkeypatch)

        assert report["anonymize_failed"] == 1
        assert FakeIngestor.instances[-1].ingested == []

    def test_a_non_anonymized_scope_in_the_same_run_still_ingests(self, crawl_env, monkeypatch):
        monkeypatch.setattr(crawler, "anonymize_markdown", lambda text, *, key: AnonymizeResult("redacted"))
        _install_graph(monkeypatch, self._handler)
        scopes = [
            _drive_scope(anonymize=True, source_scope_id="b!drive1", collection_id="secret"),
            _drive_scope(anonymize=False, source_scope_id="b!drive2", collection_id="open"),
        ]
        report = _run(_connection(scopes), monkeypatch)

        by_collection = {row["collection_id"]: row["markdown"] for row in FakeIngestor.instances[-1].ingested}
        assert by_collection == {"secret": "redacted", "open": "# converted"}
        assert report["scopes"] == 2


# --------------------------------------------------------------------------
# Conversion failures
# --------------------------------------------------------------------------


class TestConversion:
    def test_an_unconvertible_file_is_counted_and_skipped_not_fatal(self, crawl_env, monkeypatch):
        def _boom(path, mime):
            raise RuntimeError("markitdown said no")

        monkeypatch.setattr(crawler, "convert_to_markdown", _boom)

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return _content_response()
            return httpx.Response(
                200,
                json={
                    "value": [_file_item("i1"), _file_item("i2", name="ok.txt", ctag="c2")],
                    "@odata.deltaLink": f"{DRIVE_DELTA}?t=1",
                },
            )

        _install_graph(monkeypatch, handler)
        report = _run(_connection([_drive_scope()]), monkeypatch)

        assert report["convert_failed"] == 2
        assert report["new"] == 0
        assert FakeIngestor.instances[-1].ingested == []

    def test_an_empty_conversion_is_not_ingested(self, crawl_env, monkeypatch):
        monkeypatch.setattr(crawler, "convert_to_markdown", lambda path, mime: ConvertResult("   \n "))

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return _content_response()
            return httpx.Response(200, json={"value": [_file_item()], "@odata.deltaLink": f"{DRIVE_DELTA}?t=1"})

        _install_graph(monkeypatch, handler)
        report = _run(_connection([_drive_scope()]), monkeypatch)

        assert report["convert_failed"] == 1
        assert FakeIngestor.instances[-1].ingested == []


# --------------------------------------------------------------------------
# Oversize accounting
# --------------------------------------------------------------------------


class TestOversize:
    def test_an_oversize_file_is_skipped_counted_and_reported(self, crawl_env, monkeypatch):
        monkeypatch.setattr(crawler, "_max_file_mb", lambda: 1)

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return _content_response()
            return httpx.Response(
                200,
                json={
                    "value": [
                        _file_item("big", name="huge.pdf", size=5 * 1024 * 1024),
                        _file_item("small", name="ok.txt", ctag="c2", size=10),
                    ],
                    "@odata.deltaLink": f"{DRIVE_DELTA}?t=1",
                },
            )

        seen = _install_graph(monkeypatch, handler)
        report = _run(_connection([_drive_scope()]), monkeypatch)

        over = report["skipped_oversize"]
        assert over["files"] == 1
        assert over["bytes"] == 5 * 1024 * 1024
        assert over["largest"][0]["path"] == "Reports/huge.pdf"
        assert report["max_file_mb"] == 1
        # Skipped before the download, not after — the bytes are never fetched.
        assert sum(1 for url in seen if url.endswith("/content")) == 1
        assert [row["stable_id"] for row in FakeIngestor.instances[-1].ingested] == ["graph:small"]

    def test_zero_means_unlimited(self, crawl_env, monkeypatch):
        monkeypatch.setattr(crawler, "_max_file_mb", lambda: 0)

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return _content_response()
            return httpx.Response(
                200,
                json={
                    "value": [_file_item("big", name="huge.pdf", size=9 * 1024 * 1024 * 1024)],
                    "@odata.deltaLink": f"{DRIVE_DELTA}?t=1",
                },
            )

        _install_graph(monkeypatch, handler)
        report = _run(_connection([_drive_scope()]), monkeypatch)

        assert report["skipped_oversize"]["files"] == 0
        assert report["max_file_mb"] == "unlimited"
        assert FakeIngestor.instances[-1].ingested


# --------------------------------------------------------------------------
# Transport policy
# --------------------------------------------------------------------------


class TestTransport:
    def test_429_is_honored_within_budget_and_counted(self, crawl_env, monkeypatch):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return _content_response()
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "3"}, json={})
            return httpx.Response(200, json={"value": [_file_item()], "@odata.deltaLink": f"{DRIVE_DELTA}?t=1"})

        _install_graph(monkeypatch, handler)
        slept: List[float] = []

        async def _sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr(crawler, "_sleep", _sleep)
        report = _run(_connection([_drive_scope()]), monkeypatch)

        assert report["http_429"] == 1
        assert slept == [3.0]
        assert report["throttle_wait_s"] == 3.0
        assert report["new"] == 1

    def test_429_budget_exhaustion_raises_instead_of_sleeping_forever(self, crawl_env, monkeypatch):
        _install_graph(monkeypatch, lambda request: httpx.Response(429, headers={"Retry-After": "300"}, json={}))

        async def _sleep(seconds: float) -> None:
            return None

        monkeypatch.setattr(crawler, "_sleep", _sleep)
        with pytest.raises(crawler.GraphThrottled):
            _run(_connection([_drive_scope()]), monkeypatch)

    def test_a_5xx_retries_then_succeeds(self, crawl_env, monkeypatch):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return _content_response()
            calls["n"] += 1
            if calls["n"] <= 2:
                return httpx.Response(503, json={})
            return httpx.Response(200, json={"value": [_file_item()], "@odata.deltaLink": f"{DRIVE_DELTA}?t=1"})

        _install_graph(monkeypatch, handler)

        async def _sleep(seconds: float) -> None:
            return None

        monkeypatch.setattr(crawler, "_sleep", _sleep)
        report = _run(_connection([_drive_scope()]), monkeypatch)

        assert report["retries"] == 2
        assert report["new"] == 1

    def test_a_throttled_download_aborts_the_run_instead_of_being_swallowed(self, crawl_env, monkeypatch):
        """A 429 budget exhausted while DOWNLOADING is a tenant-wide signal,
        not one file's problem — absorbing it per file would keep hammering."""

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return httpx.Response(429, headers={"Retry-After": "300"}, json={})
            return httpx.Response(
                200,
                json={
                    "value": [_file_item("a"), _file_item("b", ctag="c2"), _file_item("c", ctag="c3")],
                    "@odata.deltaLink": f"{DRIVE_DELTA}?t=1",
                },
            )

        seen = _install_graph(monkeypatch, handler)

        async def _sleep(seconds: float) -> None:
            return None

        monkeypatch.setattr(crawler, "_sleep", _sleep)
        with pytest.raises(crawler.GraphThrottled):
            _run(_connection([_drive_scope()]), monkeypatch)

        # Only the FIRST item's download burned a budget; the run stopped
        # rather than spending one per remaining file.
        assert len({url for url in seen if url.endswith("/content")}) == 1

    def test_a_401_forces_exactly_one_token_refresh(self, crawl_env, monkeypatch):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return _content_response()
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(401, json={})
            return httpx.Response(200, json={"value": [], "@odata.deltaLink": f"{DRIVE_DELTA}?t=1"})

        _install_graph(monkeypatch, handler)
        report = _run(_connection([_drive_scope()]), monkeypatch)

        # One acquisition at the start, one forced by the 401.
        assert report["token_refreshes"] == 2

    def test_a_forbidden_drive_is_skipped_counted_and_never_fatal(self, crawl_env, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/content"):
                return _content_response()
            if "/drives?" in url:
                return httpx.Response(
                    200, json={"value": [{"id": "b!d1", "name": "Locked"}, {"id": "b!d2", "name": "Open"}]}
                )
            if "/drives/b!d1/" in url:
                return httpx.Response(403, json={"error": {"code": "accessDenied"}})
            return httpx.Response(
                200,
                json={
                    "value": [_file_item("open1", parent_path="/drives/b!d2/root:/Reports")],
                    "@odata.deltaLink": f"{GRAPH}/drives/b!d2/root/delta?t=1",
                },
            )

        _install_graph(monkeypatch, handler)
        scope = _drive_scope(source_scope_id="host,site-guid,web-guid", display_path="Corp")
        report = _run(_connection([scope]), monkeypatch)

        assert report["permission_skips"] == 1
        assert [row["stable_id"] for row in FakeIngestor.instances[-1].ingested] == ["graph:open1"]
        # The refused drive got no deltaLink — a later run retries it rather
        # than believing it was fully enumerated.
        assert list(_state(crawl_env)["delta_links"]) == ["b!d2"]

    def test_a_persistent_5xx_is_fatal_not_silently_skipped(self, crawl_env, monkeypatch):
        _install_graph(monkeypatch, lambda request: httpx.Response(503, json={}))

        async def _sleep(seconds: float) -> None:
            return None

        monkeypatch.setattr(crawler, "_sleep", _sleep)
        with pytest.raises(gc.SharePointGraphError, match="503"):
            _run(_connection([_drive_scope()]), monkeypatch)

    def test_a_non_graph_next_link_is_refused_before_the_token_is_sent(self, crawl_env, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"value": [], "@odata.nextLink": "https://evil.example/steal"})

        seen = _install_graph(monkeypatch, handler)
        with pytest.raises(crawler.CrawlError, match="non-Graph URL"):
            _run(_connection([_drive_scope()]), monkeypatch)

        assert not any("evil.example" in url for url in seen)


# --------------------------------------------------------------------------
# Scope resolution & excluded subtrees
# --------------------------------------------------------------------------


class TestScopes:
    def test_a_site_scope_fans_out_to_every_document_library(self, crawl_env, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/content"):
                return _content_response()
            if "/drives?" in url:
                return httpx.Response(
                    200, json={"value": [{"id": "b!d1", "name": "Documents"}, {"id": "b!d2", "name": "Policies"}]}
                )
            return httpx.Response(200, json={"value": [], "@odata.deltaLink": url})

        _install_graph(monkeypatch, handler)
        scope = _drive_scope(source_scope_id="host,site-guid,web-guid", display_path="Corp")
        report = _run(_connection([scope]), monkeypatch)

        assert report["drives"] == 2
        assert set(_state(crawl_env)["delta_links"]) == {"b!d1", "b!d2"}

    def test_a_folder_scope_deltas_that_folder_subtree(self, crawl_env, monkeypatch):
        seen = _install_graph(
            monkeypatch,
            lambda request: httpx.Response(200, json={"value": [], "@odata.deltaLink": str(request.url)}),
        )
        scope = _drive_scope(source_scope_id="01FOLDERID", drive_id="b!drive1", display_path="Corp / Documents / HR")
        _run(_connection([scope]), monkeypatch)

        assert any("/drives/b!drive1/items/01FOLDERID/delta" in url for url in seen)
        assert "b!drive1:01FOLDERID" in _state(crawl_env)["delta_links"]

    def test_a_folder_scope_without_a_drive_id_fails_that_scope_only(self, crawl_env, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return _content_response()
            return httpx.Response(200, json={"value": [_file_item()], "@odata.deltaLink": f"{DRIVE_DELTA}?t=1"})

        _install_graph(monkeypatch, handler)
        scopes = [_drive_scope(source_scope_id="01FOLDERID", collection_id="broken"), _drive_scope()]
        report = _run(_connection(scopes), monkeypatch)

        assert [e["scope"] for e in report["scope_errors"]] == ["01FOLDERID"]
        assert report["scopes"] == 1
        assert FakeIngestor.instances[-1].ingested[0]["collection_id"] == "col1"

    def test_excluded_subtrees_are_honored_by_drive_relative_path(self, crawl_env, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/content"):
                return _content_response()
            if "/items/EXCL" in url and "/delta" not in url:
                return httpx.Response(
                    200,
                    json={
                        "id": "EXCL",
                        "name": "Private",
                        "parentReference": {"path": "/drives/b!drive1/root:/Reports"},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "value": [
                        _file_item("inside", name="secret.docx", parent_path="/drives/b!drive1/root:/Reports/Private"),
                        _file_item(
                            "outside", name="public.docx", ctag="c2", parent_path="/drives/b!drive1/root:/Reports"
                        ),
                    ],
                    "@odata.deltaLink": f"{DRIVE_DELTA}?t=1",
                },
            )

        _install_graph(monkeypatch, handler)
        scope = _drive_scope(
            drive_id="b!drive1", excluded_subtrees=[{"item_id": "EXCL", "path": "Corp/Documents/Reports/Private"}]
        )
        report = _run(_connection([scope]), monkeypatch)

        assert report["excluded_subtree_skips"] == 1
        assert [row["stable_id"] for row in FakeIngestor.instances[-1].ingested] == ["graph:outside"]

    def test_include_excluded_subtrees_overrides_the_exclusion(self, crawl_env, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return _content_response()
            return httpx.Response(
                200,
                json={
                    "value": [_file_item("inside", parent_path="/drives/b!drive1/root:/Reports/Private")],
                    "@odata.deltaLink": f"{DRIVE_DELTA}?t=1",
                },
            )

        _install_graph(monkeypatch, handler)
        scope = _drive_scope(
            drive_id="b!drive1",
            excluded_subtrees=[{"item_id": "EXCL", "path": "x"}],
            include_excluded_subtrees=True,
        )
        report = _run(_connection([scope]), monkeypatch)

        assert report["excluded_subtree_skips"] == 0
        assert FakeIngestor.instances[-1].ingested

    def test_a_connection_with_no_confirmed_scope_fails_clean(self, crawl_env, monkeypatch):
        _install_graph(monkeypatch, lambda request: httpx.Response(200, json={"value": []}))
        with pytest.raises(crawler.CrawlError, match="no confirmed scope"):
            _run(_connection([{"source_scope_id": "b!drive1"}]), monkeypatch)

    def test_the_payload_can_narrow_the_run_to_named_scopes(self, crawl_env, monkeypatch):
        _install_graph(
            monkeypatch,
            lambda request: httpx.Response(200, json={"value": [], "@odata.deltaLink": str(request.url)}),
        )
        scopes = [_drive_scope(), _drive_scope(source_scope_id="b!drive2", collection_id="col2")]
        report = _run(_connection(scopes), monkeypatch, scopes=["b!drive2"])

        assert report["scopes"] == 1
        assert list(_state(crawl_env)["delta_links"]) == ["b!drive2"]


# --------------------------------------------------------------------------
# State file handling
# --------------------------------------------------------------------------


class TestState:
    def test_a_torn_state_file_degrades_to_a_full_crawl(self, crawl_env):
        path = crawler.state_path("conn1")
        path.write_text("{not json")
        state = crawler.load_state("conn1")
        assert state == {"delta_links": {}, "ctags": {}}

    def test_an_unsafe_connection_id_cannot_escape_the_state_directory(self, crawl_env):
        for bad in ("../../etc/passwd", "a/b", "..", ""):
            with pytest.raises(crawler.CrawlError):
                crawler.state_path(bad)

    def test_state_survives_a_round_trip(self, crawl_env):
        crawler.save_state("conn1", {"delta_links": {"d": "u"}, "ctags": {"graph:1": "c"}})
        assert crawler.load_state("conn1")["ctags"] == {"graph:1": "c"}


# --------------------------------------------------------------------------
# Entry point / mode selection
# --------------------------------------------------------------------------


class TestEntryPoint:
    def test_missing_connection_id_fails_clean(self):
        with pytest.raises(crawler.CrawlError, match="connection_id"):
            crawler.run_builtin_crawl({})

    def test_a_non_sharepoint_connection_is_refused(self, crawl_env, monkeypatch):
        monkeypatch.setattr(
            "src.repositories.source_connections_repo",
            lambda: type("R", (), {"get": staticmethod(lambda cid: {"id": cid, "source_type": "keboola"})})(),
        )
        with pytest.raises(crawler.CrawlError, match="not a sharepoint connection"):
            crawler.run_builtin_crawl({"connection_id": "conn1"})


class TestProducerMode:
    def test_default_stays_external(self, monkeypatch):
        from app.worker import kinds

        monkeypatch.setattr(kinds, "_extraction_producer_mode", kinds._extraction_producer_mode)
        monkeypatch.setattr("app.instance_config.get_value", lambda *keys, default=None: default)
        assert kinds._extraction_producer_mode() == "external"

    def test_builtin_is_selected_only_by_the_explicit_value(self, monkeypatch):
        from app.worker import kinds

        values = {"mode": "builtin"}
        monkeypatch.setattr(
            "app.instance_config.get_value",
            lambda *keys, default=None: values.get(keys[-1], default),
        )
        assert kinds._extraction_producer_mode() == "builtin"

        values["mode"] = "external"
        assert kinds._extraction_producer_mode() == "external"

    def test_builtin_mode_delegates_to_the_crawler_without_a_subprocess(self, monkeypatch):
        from app.worker import kinds

        monkeypatch.setattr(kinds, "_extraction_producer_mode", lambda: "builtin")
        monkeypatch.setattr("app.instance_config.feature_enabled", lambda *a, **k: True)

        def _boom(*args: Any, **kwargs: Any):
            raise AssertionError("builtin mode must never spawn a subprocess")

        monkeypatch.setattr(kinds.subprocess, "run", _boom)
        monkeypatch.setattr(
            "connectors.sharepoint.crawler.run_builtin_crawl",
            lambda payload: {"connection_id": payload["connection_id"], "new": 3},
        )

        result = kinds._run_corpus_extraction({"connection_id": "conn1"})
        assert result == {"connection_id": "conn1", "new": 3}
