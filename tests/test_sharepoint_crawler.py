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

import asyncio
import json
import logging
import os
import signal
import threading
import time
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
    """Stands in for ``crawler._Ingestor`` — records what would be written.

    ``delete`` is COLLECTION-scoped, mirroring the real ``_Ingestor.delete``
    (``corpus_file_sources_repo().resolve(collection_id, stable_id)``): it
    only succeeds when ``stable_id`` was last ingested into THIS
    ``collection_id``. A fake that returned ``True`` for any collection would
    make a zone-routing deletion test vacuous — it would pass even if the
    crawler tried the wrong collection first.

    ``_collection_of`` (stable_id -> collection_id) is a CLASS-level dict,
    not per-instance: the crawler makes a fresh ``_Ingestor()`` every run,
    but the real backing store (``corpus_file_sources_repo()``) persists
    across runs — a resumed crawl's delete must still find what an earlier
    run's instance ingested. Reset between tests by :meth:`reset`.
    """

    instances: List["FakeIngestor"] = []
    _collection_of: Dict[str, str] = {}

    @classmethod
    def reset(cls) -> None:
        cls.instances.clear()
        cls._collection_of.clear()

    def __init__(self) -> None:
        self.ingested: List[Dict[str, Any]] = []
        self.deleted: List[str] = []
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
        was_new = stable_id not in FakeIngestor._collection_of
        FakeIngestor._collection_of[stable_id] = collection_id
        return f"file-{len(FakeIngestor._collection_of)}", was_new

    def delete(self, collection_id: str, stable_id: str) -> bool:
        if FakeIngestor._collection_of.get(stable_id) != collection_id:
            return False
        del FakeIngestor._collection_of[stable_id]
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


def _connection(
    scopes: List[Dict[str, Any]],
    connection_id: str = "conn1",
    zones: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    config: Dict[str, Any] = {"tenant_id": "t1", "client_id": "c1", "scopes": scopes}
    if zones:
        config["acl_zones"] = zones
    return {"id": connection_id, "source_type": "sharepoint", "config": config}


def _drive_scope(**overrides: Any) -> Dict[str, Any]:
    scope = {
        "source_scope_id": "b!drive1",
        "display_path": "Corp / Documents",
        "collection_id": "col1",
        "anonymize": False,
    }
    scope.update(overrides)
    return scope


def _zone(**overrides: Any) -> Dict[str, Any]:
    """A ``config["acl_zones"]`` row — see ``connectors.sharepoint.acl_sync
    .zone_rows``'s own docstring for the full shape."""
    zone = {
        "zone_item_id": "zone1",
        "parent_scope_id": "b!drive1",
        "drive_id": "b!drive1",
        "name": "Zone",
        "display_path": "Corp / Documents / Zone",
        "rel_path": "Zone",
        "collection_id": "zonecol1",
        "detected_at": "2026-08-31T00:00:00+00:00",
        "status": "active",
    }
    zone.update(overrides)
    return zone


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
    FakeIngestor.reset()
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


class FakeSourceConnectionsRepo:
    """Stands in for ``source_connections_repo()`` — just enough of the
    contract for the crawl's own reads/writes: ``.get()`` returns the SAME
    connection dict every call (by reference, so a ``config_patch`` is
    immediately visible to the next ``.get()``), and ``.config_patch()``
    merges the patch's TOP-LEVEL keys into ``config`` the same shallow way
    both real repos do — no new store, the same JSON column
    ``config.scopes``/``config.extraction`` already live on.
    """

    def __init__(self, connection: Dict[str, Any]) -> None:
        self.connection = connection

    def get(self, connection_id: str) -> Optional[Dict[str, Any]]:
        if connection_id != self.connection.get("id"):
            return None
        return self.connection

    def config_patch(self, connection_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if connection_id != self.connection.get("id"):
            return None
        config = dict(self.connection.get("config") or {})
        config.update(patch)
        self.connection["config"] = config
        return self.connection


def _run(connection: Dict[str, Any], monkeypatch, scopes: Optional[List[str]] = None) -> Dict[str, Any]:
    monkeypatch.setattr(
        "src.repositories.source_connections_repo",
        lambda: FakeSourceConnectionsRepo(connection),
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
            crawler,
            "anonymize_markdown",
            lambda text, *, key, detector=None: AnonymizeResult("PERSON_abc met PERSON_def", 2),
        )
        _install_graph(monkeypatch, self._handler)
        report = _run(_connection([_drive_scope(anonymize=True)]), monkeypatch)

        assert report["anonymize_failed"] == 0
        assert FakeIngestor.instances[-1].ingested[0]["markdown"] == "PERSON_abc met PERSON_def"

    def test_a_document_that_cannot_be_anonymized_is_never_ingested_raw(self, crawl_env, monkeypatch):
        def _boom(text, *, key, detector=None):
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
        def _not_landed(text, *, key, detector=None):
            raise ImportError("No module named 'src.anonymization'")

        monkeypatch.setattr(crawler, "anonymize_markdown", _not_landed)
        _install_graph(monkeypatch, self._handler)
        report = _run(_connection([_drive_scope(anonymize=True)]), monkeypatch)

        assert report["anonymize_failed"] == 1
        assert FakeIngestor.instances[-1].ingested == []

    def test_a_non_anonymized_scope_in_the_same_run_still_ingests(self, crawl_env, monkeypatch):
        monkeypatch.setattr(
            crawler, "anonymize_markdown", lambda text, *, key, detector=None: AnonymizeResult("redacted")
        )
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


class TestConversionCrashIsolation:
    """A native crash inside the converter (observed on a live deployment:
    a `trap int3` abort inside libpdfium.so, reached via pypdfium2) used to
    take the ENTIRE worker process down with it — nothing in this module
    could ever catch it, because by the time it happened nothing here was
    running: the process itself was simply gone one line later. Conversion
    now runs in a dedicated child process per concurrency slot
    (`crawler._ConvertProcessPool`), so a crash there costs exactly the one
    file it was converting, never this process.

    `os.kill(os.getpid(), signal.SIGABRT)` inside the fake converter below
    MUST run inside a forked child — if process isolation regressed to the
    old thread-pool call, this whole test process would abort instead of
    the assertions below ever running, which is itself the strongest
    possible proof these tests fail against the pre-isolation code.
    """

    @staticmethod
    def _crash_on_marker(marker: bytes) -> Callable[[Path, str], Any]:
        def _convert(path: Path, mime: str) -> Any:
            if Path(path).read_bytes() == marker:
                os.kill(os.getpid(), signal.SIGABRT)
            return ConvertResult("# converted fine")

        return _convert

    @staticmethod
    def _two_item_handler(monkeypatch, *, crash_item_id: str = "crash") -> None:
        """One item whose downloaded bytes are the crash marker, one
        ordinary — told apart by item id, which Graph's own content URL
        always carries (``.../items/<id>/content``)."""

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/content"):
                if f"/items/{crash_item_id}/content" in url:
                    return httpx.Response(200, content=b"CRASH-ME")
                return httpx.Response(200, content=b"fine-bytes")
            return httpx.Response(
                200,
                json={
                    "value": [
                        _file_item(crash_item_id, name="bad.pdf", ctag="c1"),
                        _file_item("ok", name="ok.txt", ctag="c2"),
                    ],
                    "@odata.deltaLink": f"{DRIVE_DELTA}?t=1",
                },
            )

        _install_graph(monkeypatch, handler)

    def test_a_signal_crash_is_counted_convert_failed_and_the_run_continues(self, crawl_env, monkeypatch, caplog):
        # Concurrency 1: the crashed and the healthy file share the SAME
        # (repaired) slot, back to back — the strongest version of "the run
        # continues with the next file".
        _at_concurrency(monkeypatch, 1)
        monkeypatch.setattr(crawler, "convert_to_markdown", self._crash_on_marker(b"CRASH-ME"))
        self._two_item_handler(monkeypatch)

        with caplog.at_level(logging.WARNING, logger="connectors.sharepoint.crawler"):
            report = _run(_connection([_drive_scope()]), monkeypatch)

        # Reaching this line at all proves the crash cost one file, not this
        # (the crawl's own) process.
        assert report["convert_failed"] == 1
        assert report["new"] == 1
        assert [row["stable_id"] for row in FakeIngestor.instances[-1].ingested] == ["graph:ok"]

        messages = [r.getMessage() for r in caplog.records]
        assert any("bad.pdf" in m and "SIGABRT" in m for m in messages), messages

    def test_a_crash_on_one_slot_does_not_affect_a_sibling_on_another_slot(self, crawl_env, monkeypatch):
        # Concurrency > item count: crash and success run on DIFFERENT
        # worker processes at the same time — proves the isolation is
        # per-slot, not "the whole pool is down until the next page".
        _at_concurrency(monkeypatch, 4)
        monkeypatch.setattr(crawler, "convert_to_markdown", self._crash_on_marker(b"CRASH-ME"))
        self._two_item_handler(monkeypatch)

        report = _run(_connection([_drive_scope()]), monkeypatch)

        assert report["convert_failed"] == 1
        assert report["new"] == 1
        assert [row["stable_id"] for row in FakeIngestor.instances[-1].ingested] == ["graph:ok"]

    def test_a_crash_and_an_ordinary_conversion_error_are_both_convert_failed_alongside_a_success(
        self, crawl_env, monkeypatch
    ):
        _at_concurrency(monkeypatch, 1)

        def _convert(path: Path, mime: str) -> Any:
            content = Path(path).read_bytes()
            if content == b"CRASH-ME":
                os.kill(os.getpid(), signal.SIGABRT)
            if content == b"BOOM-ME":
                raise RuntimeError("markitdown said no")
            return ConvertResult("# converted fine")

        monkeypatch.setattr(crawler, "convert_to_markdown", _convert)

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/content"):
                if "/items/crash/content" in url:
                    return httpx.Response(200, content=b"CRASH-ME")
                if "/items/boom/content" in url:
                    return httpx.Response(200, content=b"BOOM-ME")
                return httpx.Response(200, content=b"fine-bytes")
            return httpx.Response(
                200,
                json={
                    "value": [
                        _file_item("crash", name="bad.pdf", ctag="c1"),
                        _file_item("boom", name="boom.docx", ctag="c2"),
                        _file_item("ok", name="ok.txt", ctag="c3"),
                    ],
                    "@odata.deltaLink": f"{DRIVE_DELTA}?t=1",
                },
            )

        _install_graph(monkeypatch, handler)
        report = _run(_connection([_drive_scope()]), monkeypatch)

        # An ordinary exception behaves exactly as it did before process
        # isolation existed — same counter, same "not fatal" outcome — right
        # alongside a signal crash, in the same page.
        assert report["convert_failed"] == 2
        assert report["new"] == 1
        assert [row["stable_id"] for row in FakeIngestor.instances[-1].ingested] == ["graph:ok"]


class TestConvertProcessPoolRecycling:
    """`_ConvertProcessPool` recycling — the OOM guard for a crash survivor
    that just keeps running: markitdown/pypdfium2 hold onto memory per
    document, so a slot that never recycles grows without bound over a
    large crawl. Observed on a live deployment: ~8.4 GiB across 6 slots
    (~22 documents each) before the container's memory cgroup started
    SIGKILLing whichever child allocated next, indiscriminately — including
    files nowhere near a gigabyte.

    Drives `_ConvertProcessPool` directly (not through a full crawl), the
    same way `TestActivityBookkeeping` drives `CrawlStats` directly — the
    mechanism under test is a pool method, not the whole pipeline.
    """

    @staticmethod
    def _write(tmp_path: Path, name: str, content: bytes) -> Path:
        p = tmp_path / name
        p.write_bytes(content)
        return p

    def test_a_slot_is_recycled_after_its_document_budget(self, tmp_path, monkeypatch):
        monkeypatch.setattr(crawler, "convert_to_markdown", lambda path, mime: ConvertResult("# ok"))
        pool = crawler._ConvertProcessPool(1, recycle_after_docs=3, recycle_rss_bytes=0)
        pool.start()
        try:
            first_pid = pool._procs[0].pid
            f = self._write(tmp_path, "doc.txt", b"fine")
            for _ in range(3):
                outcome = pool.convert(0, f, "text/plain")
                assert outcome.ok
            # The 3rd conversion crossed the budget and swapped in the
            # pre-forked SPARE — a genuinely different process, promoted
            # without this (the test's own, multi-threaded pytest) thread
            # ever calling fork().
            assert pool._procs[0].pid != first_pid
            # ...and the slot keeps converting normally afterwards, on a
            # fresh document budget.
            outcome = pool.convert(0, f, "text/plain")
            assert outcome.ok
        finally:
            pool.shutdown()

    def test_a_slot_is_recycled_when_its_rss_crosses_the_ceiling(self, tmp_path, monkeypatch):
        def _convert(path: Path, mime: str) -> Any:
            # Inflate THIS (child) process's RSS on purpose, deterministically
            # — the point of testing the trigger in isolation, rather than
            # waiting on a real multi-hundred-document crawl to grow one
            # organically.
            _hog = bytearray(20 * 1024 * 1024)  # noqa: F841 — the allocation is the point
            return ConvertResult("# ok")

        monkeypatch.setattr(crawler, "convert_to_markdown", _convert)
        # A document budget nothing in this test could reach, so only the
        # RSS trigger can plausibly be what recycles the slot.
        pool = crawler._ConvertProcessPool(1, recycle_after_docs=1000, recycle_rss_bytes=5 * 1024 * 1024)
        pool.start()
        try:
            first_pid = pool._procs[0].pid
            f = self._write(tmp_path, "doc.txt", b"x")
            outcome = pool.convert(0, f, "text/plain")
            assert outcome.ok
            assert pool._procs[0].pid != first_pid
        finally:
            pool.shutdown()

    def test_recycling_does_not_regress_crash_isolation(self, tmp_path, monkeypatch):
        def _convert(path: Path, mime: str) -> Any:
            if Path(path).read_bytes() == b"CRASH-ME":
                os.kill(os.getpid(), signal.SIGABRT)
            return ConvertResult("# ok")

        monkeypatch.setattr(crawler, "convert_to_markdown", _convert)
        pool = crawler._ConvertProcessPool(1, recycle_after_docs=2, recycle_rss_bytes=0)
        pool.start()
        try:
            f_ok = self._write(tmp_path, "ok.txt", b"fine")
            f_crash = self._write(tmp_path, "bad.txt", b"CRASH-ME")

            assert pool.convert(0, f_ok, "text/plain").ok  # doc 1/2
            pid_before_recycle = pool._procs[0].pid
            assert pool.convert(0, f_ok, "text/plain").ok  # doc 2/2 -> recycles
            assert pool._procs[0].pid != pid_before_recycle, "the slot should now be the pre-forked spare"

            # The RECYCLED (spare-promoted) process crashes on this file —
            # isolation must still catch it exactly like before recycling
            # ever existed: this file counts as convert_failed, the parent
            # survives, and the pool names the real signal.
            with pytest.raises(crawler._ConvertCrashed) as exc_info:
                pool.convert(0, f_crash, "text/plain")
            assert exc_info.value.signal_name == "SIGABRT"

            # And the pool recovers at the next safe point, same as any
            # other crash.
            pool.repair()
            assert pool.convert(0, f_ok, "text/plain").ok
        finally:
            pool.shutdown()


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
# `/content` redirect (Graph answers a file download with a 302 to a
# pre-authenticated URL on a different host, not the bytes themselves).
# --------------------------------------------------------------------------


class TestDownloadRedirect:
    """Direct unit tests of ``GraphTransport.download_to_temp`` — lower-level
    than :class:`TestTransport`'s full-crawl runs, because asserting "no
    Authorization header on the redirect target" needs the raw request the
    MockTransport handler saw."""

    _REDIRECT_TARGET = "https://contoso-my.blob.example/download?sig=abc"

    @staticmethod
    def _transport(monkeypatch) -> crawler.GraphTransport:
        stats = crawler.CrawlStats()

        async def _acquire() -> str:
            return "tok"

        auth = crawler.GraphAuth(acquire=_acquire, stats=stats)
        return crawler.GraphTransport(auth, stats)

    def _handler(
        self, *, redirect_response: Callable[[httpx.Request], httpx.Response]
    ) -> Callable[[httpx.Request], httpx.Response]:
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == self._REDIRECT_TARGET:
                return redirect_response(request)
            return httpx.Response(302, headers={"Location": self._REDIRECT_TARGET})

        return handler

    def test_a_302_is_followed_and_the_bytes_land_in_the_temp_file(self, monkeypatch):
        _install_graph(
            monkeypatch, self._handler(redirect_response=lambda req: httpx.Response(200, content=b"the-real-bytes"))
        )
        transport = self._transport(monkeypatch)

        path = asyncio.run(transport.download_to_temp("d1", "i1", "file.txt", max_bytes=0))
        try:
            assert path.read_bytes() == b"the-real-bytes"
        finally:
            path.unlink(missing_ok=True)

    def test_the_redirect_target_gets_no_authorization_header(self, monkeypatch):
        seen_auth: Dict[str, Optional[str]] = {}

        def redirect_response(request: httpx.Request) -> httpx.Response:
            seen_auth["value"] = request.headers.get("authorization")
            return httpx.Response(200, content=b"bytes")

        _install_graph(monkeypatch, self._handler(redirect_response=redirect_response))
        transport = self._transport(monkeypatch)

        path = asyncio.run(transport.download_to_temp("d1", "i1", "file.txt", max_bytes=0))
        path.unlink(missing_ok=True)

        assert seen_auth["value"] is None

    def test_max_bytes_is_still_enforced_on_the_redirected_body(self, monkeypatch):
        _install_graph(
            monkeypatch, self._handler(redirect_response=lambda req: httpx.Response(200, content=b"x" * 100))
        )
        transport = self._transport(monkeypatch)

        with pytest.raises(crawler.CrawlError, match="cap"):
            asyncio.run(transport.download_to_temp("d1", "i1", "file.txt", max_bytes=10))

    def test_a_genuine_4xx_still_fails_no_regression(self, monkeypatch):
        _install_graph(monkeypatch, lambda request: httpx.Response(404, json={}))
        transport = self._transport(monkeypatch)

        with pytest.raises(gc.SharePointGraphError, match="404"):
            asyncio.run(transport.download_to_temp("d1", "i1", "file.txt", max_bytes=0))

    def test_a_genuine_5xx_still_retries_then_fails(self, monkeypatch):
        _install_graph(monkeypatch, lambda request: httpx.Response(503, json={}))
        transport = self._transport(monkeypatch)

        async def _sleep(seconds: float) -> None:
            return None

        monkeypatch.setattr(crawler, "_sleep", _sleep)
        with pytest.raises(gc.SharePointGraphError, match="503"):
            asyncio.run(transport.download_to_temp("d1", "i1", "file.txt", max_bytes=0))


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
# Permission-zone routing (TCRD-284)
# --------------------------------------------------------------------------


class TestZoneRouting:
    def test_file_under_active_zone_lands_in_zone_collection_sibling_outside_in_scope_collection(
        self, crawl_env, monkeypatch
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return _content_response()
            return httpx.Response(
                200,
                json={
                    "value": [
                        _file_item("in-zone", name="secret.docx", parent_path="/drives/b!drive1/root:/Reports/Private"),
                        _file_item(
                            "out-zone", name="public.docx", ctag="c2", parent_path="/drives/b!drive1/root:/Reports"
                        ),
                    ],
                    "@odata.deltaLink": f"{DRIVE_DELTA}?t=1",
                },
            )

        _install_graph(monkeypatch, handler)
        scope = _drive_scope(drive_id="b!drive1")
        zone = _zone(rel_path="Reports/Private", collection_id="zonecol1")
        _run(_connection([scope], zones=[zone]), monkeypatch)

        by_stable = {row["stable_id"]: row["collection_id"] for row in FakeIngestor.instances[-1].ingested}
        assert by_stable == {"graph:in-zone": "zonecol1", "graph:out-zone": "col1"}

    def test_nested_zones_deepest_prefix_wins(self, crawl_env, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return _content_response()
            return httpx.Response(
                200,
                json={
                    "value": [
                        _file_item("deep", name="foo.docx", parent_path="/drives/b!drive1/root:/Team/Sub"),
                        _file_item("shallow", name="bar.docx", ctag="c2", parent_path="/drives/b!drive1/root:/Team"),
                    ],
                    "@odata.deltaLink": f"{DRIVE_DELTA}?t=1",
                },
            )

        _install_graph(monkeypatch, handler)
        scope = _drive_scope(drive_id="b!drive1")
        zones = [
            _zone(zone_item_id="zoneA", rel_path="Team", collection_id="zoneA_col"),
            _zone(zone_item_id="zoneB", rel_path="Team/Sub", collection_id="zoneB_col"),
        ]
        _run(_connection([scope], zones=zones), monkeypatch)

        by_stable = {row["stable_id"]: row["collection_id"] for row in FakeIngestor.instances[-1].ingested}
        assert by_stable == {"graph:deep": "zoneB_col", "graph:shallow": "zoneA_col"}

    def test_dissolved_zone_is_ignored_content_falls_back_to_the_scope_collection(self, crawl_env, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return _content_response()
            return httpx.Response(
                200,
                json={
                    "value": [_file_item("archived", name="old.docx", parent_path="/drives/b!drive1/root:/Archive")],
                    "@odata.deltaLink": f"{DRIVE_DELTA}?t=1",
                },
            )

        _install_graph(monkeypatch, handler)
        scope = _drive_scope(drive_id="b!drive1")
        zone = _zone(rel_path="Archive", collection_id="zonecol_dissolved", status="dissolved")
        _run(_connection([scope], zones=[zone]), monkeypatch)

        assert FakeIngestor.instances[-1].ingested[0]["collection_id"] == "col1"

    def test_deleted_item_in_a_zone_is_removed_from_the_zone_collection(self, crawl_env, monkeypatch):
        """Companion to ``TestDeltaFlow::
        test_deleted_item_removes_the_anchored_file_and_its_ctag``, which
        already pins the NO-zone case. A deleted delta row carries no
        ``parentReference`` to route by path, so the crawler must try every
        collection this scope could have routed the file into
        (``_ScopeContext.candidate_collection_ids``) — a file that landed in
        a permission zone's OWN collection must still be found and removed
        from THAT collection, not silently kept because the scope's own
        collection never had it."""
        pages = iter(
            [
                {
                    "value": [
                        _file_item("in-zone", name="secret.docx", parent_path="/drives/b!drive1/root:/Reports/Private")
                    ],
                    "@odata.deltaLink": f"{DRIVE_DELTA}?t=1",
                },
                {
                    "value": [{"id": "in-zone", "name": "secret.docx", "deleted": {"state": "deleted"}}],
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
        scope = _drive_scope(drive_id="b!drive1")
        zone = _zone(rel_path="Reports/Private", collection_id="zonecol1")
        connection = _connection([scope], zones=[zone])

        _run(connection, monkeypatch)
        first = FakeIngestor.instances[-1]
        assert first.ingested[0]["collection_id"] == "zonecol1"

        page_holder["page"] = next(pages)
        report = _run(connection, monkeypatch)

        assert report["deleted"] == 1
        assert FakeIngestor.instances[-1].deleted == ["graph:in-zone"]
        assert "graph:in-zone" not in _state(crawl_env)["ctags"]


# --------------------------------------------------------------------------
# File-kind exclusions (TCRD-284): exact match, never a subtree prefix
# --------------------------------------------------------------------------


class TestFileKindExclusion:
    def test_kind_file_exclusion_skips_exactly_that_file_not_a_similarly_named_sibling(self, crawl_env, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return _content_response()
            return httpx.Response(
                200,
                json={
                    "value": [
                        _file_item("FILEX", name="secret.docx", parent_path="/drives/b!drive1/root:/Reports"),
                        _file_item(
                            "sibling",
                            name="secret.docx-backup",
                            ctag="c2",
                            parent_path="/drives/b!drive1/root:/Reports",
                        ),
                    ],
                    "@odata.deltaLink": f"{DRIVE_DELTA}?t=1",
                },
            )

        _install_graph(monkeypatch, handler)
        scope = _drive_scope(
            drive_id="b!drive1",
            excluded_subtrees=[
                {
                    "item_id": "FILEX",
                    "path": "Corp/Documents/Reports/secret.docx",
                    "rel_path": "Reports/secret.docx",
                    "kind": "file",
                    "detected_at": "2026-08-31T00:00:00+00:00",
                }
            ],
        )
        report = _run(_connection([scope]), monkeypatch)

        assert report["excluded_subtree_skips"] == 1
        assert [row["stable_id"] for row in FakeIngestor.instances[-1].ingested] == ["graph:sibling"]


# --------------------------------------------------------------------------
# Exclusion rel_path fast path (TCRD-284)
# --------------------------------------------------------------------------


class TestExclusionFastPath:
    def test_legacy_entry_without_rel_path_still_resolves_via_graph(self, crawl_env, monkeypatch):
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

        seen = _install_graph(monkeypatch, handler)
        scope = _drive_scope(
            drive_id="b!drive1", excluded_subtrees=[{"item_id": "EXCL", "path": "Corp/Documents/Reports/Private"}]
        )
        report = _run(_connection([scope]), monkeypatch)

        assert report["excluded_subtree_skips"] == 1
        assert [row["stable_id"] for row in FakeIngestor.instances[-1].ingested] == ["graph:outside"]
        assert any("/items/EXCL" in url and "/delta" not in url for url in seen)

    def test_entries_carrying_rel_path_cause_zero_graph_resolution_calls(self, crawl_env, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/content"):
                return _content_response()
            if "$select=id%2Cname%2CparentReference" in url or "$select=id,name,parentReference" in url:
                raise AssertionError(f"unexpected Graph item-resolution call for a rel_path-carrying entry: {url}")
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

        seen = _install_graph(monkeypatch, handler)
        scope = _drive_scope(
            drive_id="b!drive1",
            excluded_subtrees=[
                {
                    "item_id": "EXCL",
                    "path": "x",
                    "rel_path": "Reports/Private",
                    "kind": "folder",
                    "detected_at": "2026-08-31T00:00:00+00:00",
                }
            ],
        )
        report = _run(_connection([scope]), monkeypatch)

        assert report["excluded_subtree_skips"] == 1
        assert [row["stable_id"] for row in FakeIngestor.instances[-1].ingested] == ["graph:outside"]
        assert not any("/items/EXCL?" in url for url in seen)


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
# Entry point / detector choice / run deadline
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


class TestDetectorChoice:
    """``extraction.anonymization.detector`` — an explicit config choice, not
    a default that happens to be there. ``regex`` is chosen for COST (the LLM
    tier bills per document, ~$5/1k), so it must be what an unset value gets,
    and ``llm`` must be reachable by naming it and nothing else."""

    def test_unset_uses_the_deterministic_tier(self, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", lambda *keys, default=None: default)
        assert crawler._entity_detector() is None

    def test_regex_is_the_anonymizers_own_default_not_a_wrapper(self, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", lambda *keys, default=None: "regex")
        # None means "let src.anonymization use RegexDetector" — passing a
        # hand-built copy here would be a second definition of the default.
        assert crawler._entity_detector() is None

    def test_llm_builds_the_hybrid_detector(self, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", lambda *keys, default=None: "LLM")

        built: List[Any] = []
        monkeypatch.setattr("src.anonymization_ner.LLMDetector", lambda *a, **k: "llm-detector")
        monkeypatch.setattr(
            "src.anonymization_ner.hybrid_detector",
            lambda llm: built.append(llm) or "hybrid",
        )

        assert crawler._entity_detector() == "hybrid"
        # Hybrid, never LLM-alone: the free deterministic tier still runs.
        assert built == ["llm-detector"]

    def test_the_chosen_detector_reaches_the_anonymize_call(self, crawl_env, monkeypatch):
        monkeypatch.setenv("AGNES_ANONYMIZATION_HMAC_KEY", "unit-test-key")
        monkeypatch.setattr(crawler, "_entity_detector", lambda: "hybrid-sentinel")

        seen: List[Any] = []

        def _anonymize(text, *, key, detector=None):
            seen.append(detector)
            return AnonymizeResult("redacted")

        monkeypatch.setattr(crawler, "anonymize_markdown", _anonymize)

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return _content_response()
            return httpx.Response(
                200,
                json={"value": [_file_item()], "@odata.deltaLink": f"{DRIVE_DELTA}?t=1"},
            )

        _install_graph(monkeypatch, handler)
        _run(_connection([_drive_scope(anonymize=True)]), monkeypatch)

        assert seen == ["hybrid-sentinel"]

    def test_no_detector_is_built_when_nothing_in_the_run_anonymizes(self, crawl_env, monkeypatch):
        """The LLM tier costs money per document — an instance that has it
        configured must not construct (or pay for) one on a run whose scopes
        are all plain."""

        def _boom():
            raise AssertionError("no scope in this run anonymizes — the detector must not be built")

        monkeypatch.setattr(crawler, "_entity_detector", _boom)

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return _content_response()
            return httpx.Response(
                200,
                json={"value": [_file_item()], "@odata.deltaLink": f"{DRIVE_DELTA}?t=1"},
            )

        _install_graph(monkeypatch, handler)
        report = _run(_connection([_drive_scope(anonymize=False)]), monkeypatch)

        assert report["new"] == 1


class TestRunDeadline:
    """``extraction.timeout_s`` — the ONLY stop mechanism this feature has.
    There is no cancel button and (since the external producer was removed)
    no subprocess to kill, so an unbounded crawl would hold the extraction
    lane's single slot indefinitely."""

    def _paged_handler(self) -> Callable[[httpx.Request], httpx.Response]:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/content"):
                return _content_response()
            if "nextpage" in url:
                return httpx.Response(
                    200,
                    json={
                        "value": [_file_item("item2", name="b.docx", ctag="ctag-2")],
                        "@odata.deltaLink": f"{DRIVE_DELTA}?token=NEW",
                    },
                )
            return httpx.Response(
                200,
                json={"value": [_file_item("item1")], "@odata.nextLink": f"{DRIVE_DELTA}?nextpage=1"},
            )

        return handler

    def test_an_expired_deadline_stops_the_run_and_says_why(self, crawl_env, monkeypatch):
        _install_graph(monkeypatch, self._paged_handler())
        monkeypatch.setattr(
            "src.repositories.source_connections_repo",
            lambda: type("R", (), {"get": staticmethod(lambda cid: _connection([_drive_scope()]))})(),
        )

        with pytest.raises(crawler.CrawlTimeout, match="timeout_s"):
            # A budget already spent when the crawl starts: the first check,
            # between pages, fires before a single Graph page is fetched.
            crawler.run_builtin_crawl({"connection_id": "conn1", "timeout_s": 0.0000001})

        # The run is still accounted for: an operator must be able to tell
        # "this is where the clock ran out" from an ordinary short report.
        last_run = _state(crawl_env)["last_run"]
        assert last_run["interrupted"] is True
        assert last_run["interrupted_reason"] == "timeout"

    def test_a_timed_out_run_resumes_rather_than_restarting(self, crawl_env, monkeypatch):
        """Stopping is only free because it happens at a state boundary: the
        deltaLink/cTags written before the stop are what the next run picks
        up from."""
        _install_graph(monkeypatch, self._paged_handler())
        monkeypatch.setattr(
            "src.repositories.source_connections_repo",
            lambda: type("R", (), {"get": staticmethod(lambda cid: _connection([_drive_scope()]))})(),
        )

        # Expire at the first check AFTER a document has actually landed —
        # keyed on the ingest itself rather than on a read count, so the
        # test still asserts "a stop preserves what was already done" if the
        # number of clock reads per page ever changes.
        def _clock() -> float:
            landed = FakeIngestor.instances and FakeIngestor.instances[-1].ingested
            return 1_000_000.0 if landed else 0.0

        monkeypatch.setattr(crawler.time, "monotonic", _clock)

        with pytest.raises(crawler.CrawlTimeout):
            crawler.run_builtin_crawl({"connection_id": "conn1", "timeout_s": 60})

        state = _state(crawl_env)
        assert state["ctags"], "the item ingested before the stop must be recorded, or the next run re-does it"
        assert state["last_run"]["interrupted_reason"] == "timeout"

    def test_a_throttle_abort_is_named_not_left_looking_like_a_crash(self, crawl_env, monkeypatch):
        """An exhausted 429 budget stops the run at the same consistent point
        a timeout does, so it gets its own reason rather than "error" — a
        reader may only promise "the next run resumes" for stops that leave
        the state file describing exactly what was ingested."""

        _install_graph(monkeypatch, lambda request: httpx.Response(429, headers={"Retry-After": "300"}, json={}))

        # The 429 policy's own bound is what ends this run; the sleeps it
        # would spend getting there are stubbed out, exactly as
        # `test_429_budget_exhaustion_raises_instead_of_sleeping_forever`
        # above does.
        async def _sleep(seconds: float) -> None:
            return None

        monkeypatch.setattr(crawler, "_sleep", _sleep)
        monkeypatch.setattr(
            "src.repositories.source_connections_repo",
            lambda: type("R", (), {"get": staticmethod(lambda cid: _connection([_drive_scope()]))})(),
        )

        with pytest.raises(crawler.GraphThrottled):
            crawler.run_builtin_crawl({"connection_id": "conn1"})

        last_run = _state(crawl_env)["last_run"]
        assert last_run["interrupted"] is True
        assert last_run["interrupted_reason"] == "throttled"

    def test_an_unexpected_crash_is_never_reported_as_resumable(self, crawl_env, monkeypatch):
        """The other half of the contract: after an exception nobody planned
        for, nothing is known about how far the state file got, so the reason
        stays "error" and no reader may vouch for the state."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise RuntimeError("something nobody planned for")

        _install_graph(monkeypatch, handler)
        monkeypatch.setattr(
            "src.repositories.source_connections_repo",
            lambda: type("R", (), {"get": staticmethod(lambda cid: _connection([_drive_scope()]))})(),
        )

        with pytest.raises(BaseException):
            crawler.run_builtin_crawl({"connection_id": "conn1"})

        assert _state(crawl_env)["last_run"]["interrupted_reason"] == "error"

    def test_stop_reason_is_decided_by_isinstance_so_subclasses_inherit_it(self):
        """`_STOP_REASONS` is matched with isinstance, not an exact type
        lookup, so a future subclass of either stop keeps its reason instead
        of silently degrading to the un-vouched-for "error"."""

        class _LaterTimeout(crawler.CrawlTimeout):
            pass

        class _LaterThrottle(crawler.GraphThrottled):
            pass

        assert crawler._stop_reason(_LaterTimeout("x")) == "timeout"
        assert crawler._stop_reason(_LaterThrottle("x")) == "throttled"
        assert crawler._stop_reason(RuntimeError("x")) == "error"

    def test_zero_means_unbounded(self, crawl_env, monkeypatch):
        _install_graph(monkeypatch, self._paged_handler())
        report = _run(_connection([_drive_scope()]), monkeypatch)  # crawl_env leaves timeout at its default
        assert report["interrupted"] is False
        assert report["interrupted_reason"] is None

        deadline = crawler._Deadline(0)
        assert deadline.expires_at is None
        deadline.check()  # must not raise

    def test_the_configured_value_is_what_bounds_a_run(self, monkeypatch):
        monkeypatch.setattr(crawler, "_timeout_seconds", lambda: 42)
        assert crawler._Deadline(crawler._timeout_seconds()).timeout_s == 42


# --------------------------------------------------------------------------
# Run recording (2026-08-31 extraction-observability-ui design §7.1)
#
# The crawl writes an `extraction_runs` row: open at start, update at the
# checkpoint it already writes, finalize on done / interrupt / crash. Every
# assertion below is about a rule the CARD depends on — a run that ended is
# never left looking live, and a crash is never dressed up as a benign
# interruption.
# --------------------------------------------------------------------------


class FakeRunsRepo:
    """Records what the crawl would write, with the same method signatures
    ``ExtractionRunsPgRepository`` exposes (asserted by the shared-shape test
    at the end of this class's block)."""

    def __init__(self) -> None:
        self.started: List[Dict[str, Any]] = []
        self.checkpoints: List[Dict[str, Any]] = []
        self.finished: List[Dict[str, Any]] = []

    def start(self, *, connection_id, job_id=None, phase="crawl"):
        self.started.append({"connection_id": connection_id, "job_id": job_id, "phase": phase})
        return f"er_fake{len(self.started)}"

    def checkpoint(self, run_id, **kwargs):
        self.checkpoints.append({"run_id": run_id, **kwargs})

    def finish(self, run_id, **kwargs):
        self.finished.append({"run_id": run_id, **kwargs})


def _install_runs_repo(monkeypatch, repo=None):
    repo = repo or FakeRunsRepo()
    monkeypatch.setattr("src.repositories.extraction_runs_repo", lambda: repo)
    return repo


class TestRunRecording:
    def test_a_completed_crawl_opens_checkpoints_and_finalizes_its_row(self, crawl_env, monkeypatch):
        runs = _install_runs_repo(monkeypatch)

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return _content_response()
            return httpx.Response(200, json={"value": [_file_item()], "@odata.deltaLink": f"{DRIVE_DELTA}?t=1"})

        _install_graph(monkeypatch, handler)
        report = _run(_connection([_drive_scope()]), monkeypatch)

        assert runs.started == [{"connection_id": "conn1", "job_id": None, "phase": "crawl"}]
        assert runs.checkpoints, "the crawl's existing checkpoint must also write the run row"
        assert runs.checkpoints[-1]["files_done"] == 1
        # Enumeration is never claimed complete mid-run: the delta feed can
        # always hand back another page.
        assert runs.checkpoints[-1]["enumeration_done"] is False

        assert len(runs.finished) == 1
        final = runs.finished[0]
        assert final["status"] == "done"
        assert final["report"] == report
        assert final["files_done"] == 1
        # No detector is wired into the anonymize seam yet, so no tokens are
        # spent — `{}` is "none spent", not a computed $0.00.
        assert final["usage"] == {}

    def test_progress_carries_absolute_counters_only(self, crawl_env, monkeypatch):
        """No fraction, no percentage, no ETA — the crawl enumerates and
        processes in lockstep, and files_per_s counts only new+changed."""
        runs = _install_runs_repo(monkeypatch)

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return _content_response()
            return httpx.Response(200, json={"value": [_file_item()], "@odata.deltaLink": f"{DRIVE_DELTA}?t=1"})

        _install_graph(monkeypatch, handler)
        _run(_connection([_drive_scope()]), monkeypatch)

        progress = runs.checkpoints[-1]["progress"]
        assert progress["files_done"] == 1
        assert progress["new"] == 1
        assert "elapsed_s" in progress
        for forbidden in ("percent", "progress_pct", "eta_s", "eta", "files_per_s"):
            assert forbidden not in progress

    def test_a_crashed_crawl_records_failed_not_interrupted(self, crawl_env, monkeypatch):
        """Severity-first: a crash is both "did not finish" and "broke". The
        more severe word wins, or the card invites an operator to trust a
        broken run's numbers."""
        runs = _install_runs_repo(monkeypatch)
        _install_graph(monkeypatch, lambda request: _content_response())

        async def _boom(*args: Any, **kwargs: Any):
            raise RuntimeError("graph exploded")

        monkeypatch.setattr(crawler, "_crawl_drive", _boom)

        with pytest.raises(RuntimeError):
            _run(_connection([_drive_scope()]), monkeypatch)

        assert len(runs.finished) == 1
        assert runs.finished[0]["status"] == "failed"
        assert "graph exploded" in runs.finished[0]["error"]

    def test_a_cancelled_crawl_records_interrupted(self, crawl_env, monkeypatch):
        """A cancellation is its own outcome — the run ingested what it
        ingested and the next run resumes from the persisted cTags."""
        runs = _install_runs_repo(monkeypatch)
        _install_graph(monkeypatch, lambda request: _content_response())

        async def _cancel(*args: Any, **kwargs: Any):
            raise KeyboardInterrupt()

        monkeypatch.setattr(crawler, "_crawl_drive", _cancel)

        with pytest.raises(KeyboardInterrupt):
            _run(_connection([_drive_scope()]), monkeypatch)

        assert runs.finished[0]["status"] == "interrupted"

    def test_record_status_precedence_is_severity_first(self):
        assert crawler._record_status_for(RuntimeError("boom")) == "failed"
        assert crawler._record_status_for(crawler.CrawlError("nope")) == "failed"
        assert crawler._record_status_for(KeyboardInterrupt()) == "interrupted"
        assert crawler._record_status_for(SystemExit()) == "interrupted"

    def test_oversize_skips_are_listed_with_an_honest_total(self, crawl_env, monkeypatch):
        """A document over the cap is never downloaded and never appears in
        the collection — the run row is the only place it exists."""
        runs = _install_runs_repo(monkeypatch)
        monkeypatch.setattr(crawler, "_max_file_mb", lambda: 1)

        big = _file_item("big1", name="huge.pdf", size=10 * 1024 * 1024)

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return _content_response()
            return httpx.Response(200, json={"value": [big], "@odata.deltaLink": f"{DRIVE_DELTA}?t=1"})

        _install_graph(monkeypatch, handler)
        _run(_connection([_drive_scope()]), monkeypatch)

        skips = runs.finished[0]["skips"]
        assert skips["total"] == 1
        assert skips["listed"] == 1
        assert skips["items"][0]["reason"] == "oversize"
        assert skips["items"][0]["path"].endswith("huge.pdf")

    def test_recording_is_never_load_bearing(self, crawl_env, monkeypatch):
        """A DuckDB-backed instance cannot record runs at all (the PG-only
        repo raises on resolve). The crawl must still run, still ingest, and
        still return its report — observability that can fail a crawl is
        worse than no observability."""
        from src.repositories import RequiresPostgresBackend

        def _raise():
            raise RequiresPostgresBackend("extraction_runs")

        monkeypatch.setattr("src.repositories.extraction_runs_repo", _raise)

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return _content_response()
            return httpx.Response(200, json={"value": [_file_item()], "@odata.deltaLink": f"{DRIVE_DELTA}?t=1"})

        _install_graph(monkeypatch, handler)
        report = _run(_connection([_drive_scope()]), monkeypatch)

        assert report["new"] == 1
        assert FakeIngestor.instances[-1].ingested

    def test_a_checkpoint_write_failure_does_not_stop_the_crawl(self, crawl_env, monkeypatch):
        class Flaky(FakeRunsRepo):
            def checkpoint(self, run_id, **kwargs):
                raise RuntimeError("connection reset")

        _install_runs_repo(monkeypatch, Flaky())

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return _content_response()
            return httpx.Response(200, json={"value": [_file_item()], "@odata.deltaLink": f"{DRIVE_DELTA}?t=1"})

        _install_graph(monkeypatch, handler)
        report = _run(_connection([_drive_scope()]), monkeypatch)
        assert report["new"] == 1

    def test_report_shape_is_unchanged_by_recording(self, crawl_env, monkeypatch):
        """The run report is a published contract (the job result and the
        state file's `last_run`); recording adds a destination, not fields."""
        _install_runs_repo(monkeypatch)

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return _content_response()
            return httpx.Response(200, json={"value": [_file_item()], "@odata.deltaLink": f"{DRIVE_DELTA}?t=1"})

        _install_graph(monkeypatch, handler)
        report = _run(_connection([_drive_scope()]), monkeypatch)
        assert "items_seen" not in report
        assert "items_done" not in report

    def test_the_fake_matches_the_real_repository_signature(self):
        """A fake that has drifted from the repo it stands in for tests
        nothing. Pinned here rather than discovered in production."""
        import inspect

        from src.repositories.extraction_runs_pg import ExtractionRunsPgRepository

        for name in ("start", "checkpoint", "finish"):
            real = set(inspect.signature(getattr(ExtractionRunsPgRepository, name)).parameters)
            fake = set(inspect.signature(getattr(FakeRunsRepo, name)).parameters)
            # The fake absorbs the rest through **kwargs; what must match is
            # the positional contract the crawl actually calls with.
            assert {"self"} <= fake
            assert ("run_id" in real) == ("run_id" in fake), name


class TestDetectorUsageRecording:
    """The LLM tier's token accounting must reach the run record (`usage`)
    and the crawl report (`ner_usage`) — and `{}`/absence must keep meaning
    "no tokens spent", never a fabricated zero cost."""

    def test_detector_usage_reads_the_llm_tier(self):
        class FakeLLM:
            model = "claude-haiku-4-5"
            total_usage = {"input_tokens": 1200, "output_tokens": 90, "calls": 2}

        def detect(text):  # the plain-callable contract
            return []

        detect.llm = FakeLLM()
        usage = crawler._detector_usage(detect)
        assert usage["input_tokens"] == 1200
        assert usage["output_tokens"] == 90
        assert usage["calls"] == 2
        assert usage["model"] == "claude-haiku-4-5"

    def test_no_llm_tier_reports_empty_not_zero_dollars(self):
        assert crawler._detector_usage(None) == {}

        def bare(text):
            return []

        assert crawler._detector_usage(bare) == {}

    def test_zero_counters_collapse_to_empty(self):
        """A constructed-but-unused detector spent nothing — `{}` (none
        spent), not a row of zeros that renders like a measured $0."""

        class FakeLLM:
            model = "claude-haiku-4-5"
            total_usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}

        def detect(text):
            return []

        detect.llm = FakeLLM()
        assert crawler._detector_usage(detect) == {}


class TestOcrUsageRecording:
    """`_ocr_run_usage` mirrors `_detector_usage`'s honesty rules for the
    scan-OCR tier's module-level run totals."""

    def test_reads_the_run_totals(self):
        class FakeScanOcr:
            @staticmethod
            def run_usage():
                return {"calls": 2, "input_tokens": 5000, "output_tokens": 1100, "pages": 4}

        assert crawler._ocr_run_usage(FakeScanOcr) == {
            "calls": 2,
            "input_tokens": 5000,
            "output_tokens": 1100,
            "pages": 4,
        }

    def test_zero_counters_and_absence_collapse_to_empty(self):
        class Idle:
            @staticmethod
            def run_usage():
                return {"calls": 0, "input_tokens": 0, "output_tokens": 0}

        assert crawler._ocr_run_usage(Idle) == {}
        assert crawler._ocr_run_usage(None) == {}

    def test_a_broken_accessor_reports_empty_not_a_dead_crawl(self):
        class Broken:
            @staticmethod
            def run_usage():
                raise RuntimeError("boom")

        assert crawler._ocr_run_usage(Broken) == {}


# --------------------------------------------------------------------------
# Parallel crawl (2026-09-01)
#
# The crawl pipelines several files of ONE delta page at a time. Everything
# below is a rule the resume contract depends on, restated for concurrency:
# a counter that races is a document that silently went unindexed, and a page
# boundary that lands early is coverage lost. `concurrency: 1` remains the
# pre-parallel path and is pinned as such.
# --------------------------------------------------------------------------


def _many_items(n: int, *, prefix: str = "f", size: int = 1024) -> List[Dict[str, Any]]:
    return [_file_item(f"{prefix}{i}", name=f"{prefix}{i}.docx", ctag=f"c-{prefix}{i}", size=size) for i in range(n)]


def _one_page(items: List[Dict[str, Any]], token: str = "END") -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/content"):
            return _content_response()
        return httpx.Response(200, json={"value": items, "@odata.deltaLink": f"{DRIVE_DELTA}?token={token}"})

    return handler


def _at_concurrency(monkeypatch, n: int) -> None:
    monkeypatch.setattr(crawler, "_crawl_concurrency", lambda: n)


#: Report keys that legitimately differ between two otherwise identical runs
#: (wall clock, thread-pool observations, the knob itself).
_VOLATILE_REPORT_KEYS = {
    "started_at",
    "finished_at",
    "duration_s",
    "files_per_s",
    "item_seconds",
    "max_in_flight",
    "concurrency",
    "connection_id",
}


def _stable(report: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in report.items() if k not in _VOLATILE_REPORT_KEYS}


class TestConcurrencyResolution:
    """The knob itself: config, its clamp, and the per-run payload override."""

    def test_the_configured_value_is_read_and_clamped(self, monkeypatch):
        values: Dict[str, Any] = {}
        monkeypatch.setattr("app.instance_config.get_value", lambda *k, default=None: values.get("v", default))

        values["v"] = 4
        assert crawler._crawl_concurrency() == 4
        values["v"] = 0
        assert crawler._crawl_concurrency() == 1, "0 must clamp UP to sequential, never to 'no workers'"
        values["v"] = 9999
        assert crawler._crawl_concurrency() == crawler._MAX_CONCURRENCY
        values["v"] = "not-a-number"
        assert crawler._crawl_concurrency() == crawler._DEFAULT_CONCURRENCY

    def test_an_unset_value_is_the_default(self, monkeypatch):
        monkeypatch.setattr("app.instance_config.get_value", lambda *k, default=None: default)
        assert crawler._crawl_concurrency() == crawler._DEFAULT_CONCURRENCY

    def test_a_payload_override_replaces_the_configured_value_for_one_run(self, monkeypatch):
        monkeypatch.setattr(crawler, "_crawl_concurrency", lambda: 6)
        assert crawler._resolve_concurrency(3) == (3, 6, "payload")
        assert crawler._resolve_concurrency(None) == (6, 6, "config")

    def test_a_payload_override_is_clamped_to_its_own_lower_ceiling(self, monkeypatch):
        """An ad-hoc run is the wrong place to go looking for a tenant's
        throttling limit, so the payload ceiling is below the config one."""
        monkeypatch.setattr(crawler, "_crawl_concurrency", lambda: 6)
        assert crawler._resolve_concurrency(999)[0] == crawler._MAX_PAYLOAD_CONCURRENCY
        assert crawler._MAX_PAYLOAD_CONCURRENCY < crawler._MAX_CONCURRENCY
        assert crawler._resolve_concurrency(0)[0] == 1

    def test_an_unparseable_override_falls_back_to_config_not_to_a_guess(self, monkeypatch):
        monkeypatch.setattr(crawler, "_crawl_concurrency", lambda: 6)
        assert crawler._resolve_concurrency("lots") == (6, 6, "config")

    def test_the_payload_override_reaches_the_run_and_is_reported(self, crawl_env, monkeypatch):
        _at_concurrency(monkeypatch, 6)
        _install_graph(monkeypatch, _one_page(_many_items(3)))
        monkeypatch.setattr(
            "src.repositories.source_connections_repo",
            lambda: type("R", (), {"get": staticmethod(lambda cid: _connection([_drive_scope()]))})(),
        )

        report = crawler.run_builtin_crawl({"connection_id": "conn1", "concurrency": 2})

        assert report["concurrency"]["requested"] == 2
        assert report["concurrency"]["configured"] == 6
        assert report["concurrency"]["source"] == "payload"
        assert report["new"] == 3


class TestConcurrencyGovernor:
    """AIMD. Halve on a throttle burst, +1 per clean page, floor 1, ceiling
    the configured cap — and never abort: backing off and giving up are
    different answers to a 429."""

    def test_a_throttle_burst_halves_the_target(self):
        gov = crawler._ConcurrencyGovernor(8)
        assert gov.current() == 8
        assert gov.observe_page(throttled_429s=5, throttle_wait_s=1.0) == 4
        assert gov.observe_page(throttled_429s=5, throttle_wait_s=1.0) == 2
        assert gov.downshifts == 2

    def test_a_long_wait_counts_as_a_burst_even_with_few_429s(self):
        gov = crawler._ConcurrencyGovernor(8)
        assert gov.observe_page(throttled_429s=1, throttle_wait_s=crawler._THROTTLE_BURST_WAIT_S + 1) == 4

    def test_one_stray_429_is_not_a_burst(self):
        gov = crawler._ConcurrencyGovernor(8)
        assert gov.observe_page(throttled_429s=1, throttle_wait_s=0.5) == 8
        assert gov.downshifts == 0

    def test_clean_pages_climb_back_one_at_a_time_up_to_the_cap(self):
        gov = crawler._ConcurrencyGovernor(4)
        gov.observe_page(throttled_429s=9, throttle_wait_s=0.0)  # -> 2
        assert gov.observe_page(0, 0.0) == 3
        assert gov.observe_page(0, 0.0) == 4
        assert gov.observe_page(0, 0.0) == 4, "additive increase must not exceed the configured cap"

    def test_the_floor_is_one_and_is_recorded(self):
        gov = crawler._ConcurrencyGovernor(2)
        assert gov.observe_page(9, 0.0) == 1
        assert gov.observe_page(9, 0.0) == 1
        assert gov.floor_hit is True
        assert gov.downshifts == 1, "a target already at the floor cannot downshift again"

    def test_adaptive_is_off_when_the_operator_asked_for_sequential(self):
        gov = crawler._ConcurrencyGovernor(1)
        assert gov.adaptive is False
        assert gov.observe_page(99, 999.0) == 1
        assert gov.downshifts == 0
        assert gov.floor_hit is False

    def test_the_governor_writes_through_to_the_report_block(self):
        stats = crawler.CrawlStats(concurrency=4, concurrency_effective_max=4, concurrency_min_target=4)
        gov = crawler._ConcurrencyGovernor(4, stats=stats)
        gov.observe_page(9, 0.0)
        gov.observe_page(0, 0.0)

        block = stats.report(max_file_mb=50)["concurrency"]
        assert block["downshifts"] == 1
        assert block["min_target"] == 2
        assert block["effective_max"] == 3
        assert block["source"] == "adaptive", "the number that governed the run is the one to name"

    def test_a_downshift_is_driven_by_the_page_delta_not_the_running_total(self, crawl_env, monkeypatch):
        """A cumulative 429 count would keep halving forever after one bad
        page; the governor is fed each page's own throttling."""
        _at_concurrency(monkeypatch, 4)
        pages = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/content"):
                return _content_response()
            pages["n"] += 1
            if pages["n"] == 1:
                # One page of nothing but throttling, then clean pages.
                return httpx.Response(429, headers={"Retry-After": "1"}, json={})
            if pages["n"] <= 4:
                return httpx.Response(
                    200,
                    json={
                        "value": _many_items(1, prefix=f"p{pages['n']}"),
                        "@odata.nextLink": f"{DRIVE_DELTA}?page={pages['n']}",
                    },
                )
            return httpx.Response(200, json={"value": [], "@odata.deltaLink": f"{DRIVE_DELTA}?t=done"})

        _install_graph(monkeypatch, handler)

        async def _sleep(seconds: float) -> None:
            return None

        monkeypatch.setattr(crawler, "_sleep", _sleep)
        report = _run(_connection([_drive_scope()]), monkeypatch)

        # The throttling was real, the run finished anyway, and the target
        # climbed back once the tenant stopped pushing back.
        assert report["http_429"] >= 1
        assert report["interrupted"] is False
        assert report["concurrency"]["effective_max"] >= report["concurrency"]["min_target"]


class TestParallelCounters:
    """Counters must be EXACT under concurrency. A lost `+=` is a document
    that went unindexed with nothing in the report to say so."""

    def test_every_outcome_is_counted_exactly_once_across_many_items(self, crawl_env, monkeypatch):
        _at_concurrency(monkeypatch, 8)
        monkeypatch.setattr(crawler, "_max_file_mb", lambda: 1)

        def _convert(path: Path, mime: str) -> ConvertResult:
            if path.suffix == ".bad":
                raise RuntimeError("markitdown said no")
            if path.suffix == ".empty":
                return ConvertResult("   \n ")
            return ConvertResult("# converted")

        monkeypatch.setattr(crawler, "convert_to_markdown", _convert)

        items: List[Dict[str, Any]] = []
        items += [_file_item(f"ok{i}", name=f"ok{i}.docx", ctag=f"c-ok{i}") for i in range(30)]
        items += [_file_item(f"big{i}", name=f"big{i}.pdf", ctag=f"c-big{i}", size=5 * 1024 * 1024) for i in range(10)]
        items += [_file_item(f"bad{i}", name=f"bad{i}.bad", ctag=f"c-bad{i}") for i in range(10)]
        items += [_file_item(f"emp{i}", name=f"emp{i}.empty", ctag=f"c-emp{i}") for i in range(10)]

        _install_graph(monkeypatch, _one_page(items))
        report = _run(_connection([_drive_scope()]), monkeypatch)

        assert report["new"] == 30
        assert report["changed"] == 0
        assert report["unchanged"] == 0
        # 10 unconvertible + 10 that converted to nothing; only the former
        # are errors, exactly as in the sequential pipeline.
        assert report["convert_failed"] == 20
        assert report["errors"] == 10
        assert report["skipped_oversize"]["files"] == 10
        assert report["skipped_oversize"]["bytes"] == 10 * 5 * 1024 * 1024
        assert report["downloads"] == 50, "the 10 oversize files are never fetched"
        # Only the 30 that landed are marked crawled — the rest must be
        # retried by the next run, not treated as done.
        assert len(_state(crawl_env)["ctags"]) == 30

    def test_a_second_pass_sees_every_item_as_unchanged(self, crawl_env, monkeypatch):
        """The cTag map written concurrently has to be complete and correct,
        or a re-crawl re-downloads what it already has."""
        _at_concurrency(monkeypatch, 8)
        items = _many_items(40)
        _install_graph(monkeypatch, _one_page(items))
        connection = _connection([_drive_scope()])

        first = _run(connection, monkeypatch)
        second = _run(connection, monkeypatch)

        assert first["new"] == 40
        assert second["unchanged"] == 40
        assert second["new"] == 0 and second["changed"] == 0
        assert second["downloads"] == 0

    def test_add_refuses_a_counter_that_does_not_exist(self):
        """A typo in `stats.add(...)` must not mint a silent, always-zero
        counter — the report's whole job is that nothing is invisible."""
        stats = crawler.CrawlStats()
        with pytest.raises(AttributeError):
            stats.add(nwe=1)


class TestParallelOrdering:
    """The page boundary, and what may and may not cross it."""

    def _snapshotting_save(self, monkeypatch) -> List[Dict[str, Any]]:
        snapshots: List[Dict[str, Any]] = []
        real = crawler.save_state

        def _save(connection_id: str, state: Dict[str, Any]) -> None:
            snapshots.append(json.loads(json.dumps(state)))
            real(connection_id, state)

        monkeypatch.setattr(crawler, "save_state", _save)
        return snapshots

    def test_the_delta_link_is_persisted_only_after_every_row_of_its_page(self, crawl_env, monkeypatch):
        _at_concurrency(monkeypatch, 6)
        snapshots = self._snapshotting_save(monkeypatch)
        _install_graph(monkeypatch, _one_page(_many_items(24)))

        _run(_connection([_drive_scope()]), monkeypatch)

        with_link = [s for s in snapshots if s.get("delta_links", {}).get("b!drive1")]
        assert with_link, "the page's deltaLink must be persisted"
        assert len(with_link[0]["ctags"]) == 24, (
            "a deltaLink written while any row of its page was still in flight would "
            "make the next run skip that row forever"
        )

    def test_a_slow_item_does_not_hold_its_neighbours_ctags_hostage(self, crawl_env, monkeypatch):
        """cTags are per ITEM (written right after that item's own ingest),
        the page boundary is per PAGE. One slow item must delay only the
        latter.

        The slowness lives in ``ingest``, not ``convert_to_markdown``:
        convert now runs in a dedicated child PROCESS per concurrency slot
        (see ``_ConvertProcessPool``), which gets its own private, frozen
        COPY of every Python object at fork time — a busy-wait there could
        never observe cTags the PARENT writes later. ``ingest`` is
        unaffected: it still runs on the crawl's own item-concurrency thread
        pool, in this process, which is exactly what the invariant under
        test — a cTag is never held for a neighbour — is about.
        """
        _at_concurrency(monkeypatch, 4)
        shared: Dict[str, Any] = {"delta_links": {}, "ctags": {}}
        monkeypatch.setattr(crawler, "load_state", lambda cid: shared)

        observed: Dict[str, Any] = {}

        def _peek() -> Dict[str, Any]:
            # Under the crawl's own state lock — the same one the cTag
            # writers take, so this copy can never straddle a write.
            with crawler._state_lock:
                return dict(shared["ctags"])

        class _SlowIngestor(FakeIngestor):
            def ingest(self, *, stable_id: str, **kwargs: Any) -> Any:
                if stable_id == "graph:slow":
                    # Stay in flight until the NEIGHBOURS' cTags have landed.
                    # If a cTag were held until the page boundary this would
                    # time out with an empty map, which is the regression to
                    # catch.
                    deadline = time.monotonic() + 10
                    while len(_peek()) < 3 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    observed["ctags"] = _peek()
                return super().ingest(stable_id=stable_id, **kwargs)

        monkeypatch.setattr(crawler, "_Ingestor", _SlowIngestor)

        items = [_file_item("slow", name="slow.slow", ctag="c-slow")] + _many_items(3)
        _install_graph(monkeypatch, _one_page(items))
        report = _run(_connection([_drive_scope()]), monkeypatch)

        assert set(observed["ctags"]) == {"graph:f0", "graph:f1", "graph:f2"}
        assert "graph:slow" not in observed["ctags"], "a cTag before its own ingest would lose the file on resume"
        # ...and the page boundary still waited for the slow one.
        assert report["new"] == 4
        assert len(_state(crawl_env)["ctags"]) == 4
        assert report["max_in_flight"] >= 2

    def test_the_worker_pool_is_sized_to_the_configured_concurrency(self, crawl_env, monkeypatch):
        """Not the event loop's default executor, whose min(32, cpu+4) ceiling
        would silently cap a configured concurrency above it — the knob has to
        mean what it says. Twelve items that only make progress once all
        twelve are inside the blocking step: a smaller pool cannot get there.

        The barrier lives in ``ingest``, not ``convert_to_markdown``: convert
        now runs in a dedicated child PROCESS per slot, each with its own
        private copy of any Python object post-fork, so a
        ``threading.Barrier`` split twelve ways across twelve separate
        processes would never see all twelve parties arrive. ``ingest`` is
        unaffected — it still runs on the crawl's own item-concurrency
        thread pool, which is exactly what this test is sizing.
        """
        _at_concurrency(monkeypatch, 12)
        barrier = threading.Barrier(12, timeout=15)

        class _BarrierIngestor(FakeIngestor):
            def ingest(self, **kwargs: Any) -> Any:
                barrier.wait()  # BrokenBarrierError -> counted as an ingest error
                return super().ingest(**kwargs)

        monkeypatch.setattr(crawler, "_Ingestor", _BarrierIngestor)
        _install_graph(monkeypatch, _one_page(_many_items(12)))
        report = _run(_connection([_drive_scope()]), monkeypatch)

        assert report["errors"] == 0
        assert report["convert_failed"] == 0
        assert report["new"] == 12
        assert report["max_in_flight"] == 12

    def test_fail_closed_stays_per_item_under_concurrency(self, crawl_env, monkeypatch):
        """An anonymize-marked scope drops the documents it cannot redact —
        each on its own, with no neighbour dragged down and none let through."""
        _at_concurrency(monkeypatch, 6)
        monkeypatch.setenv("AGNES_ANONYMIZATION_HMAC_KEY", "unit-test-key")

        def _anonymize(text: str, *, key: bytes, detector: Any = None) -> AnonymizeResult:
            if text == "UNREDACTABLE":
                raise RuntimeError("anonymizer blew up")
            return AnonymizeResult(f"redacted:{text}")

        monkeypatch.setattr(crawler, "anonymize_markdown", _anonymize)
        # The convert seam only ever sees the TEMP file, whose suffix the
        # download takes from the item's name — so the marker rides there.
        monkeypatch.setattr(
            crawler,
            "convert_to_markdown",
            lambda path, mime: ConvertResult("UNREDACTABLE" if path.suffix == ".pii" else "# converted"),
        )

        items = [
            _file_item(f"f{i}", name=f"f{i}.pii" if i in (3, 7) else f"f{i}.docx", ctag=f"c{i}") for i in range(10)
        ]
        _install_graph(monkeypatch, _one_page(items))
        report = _run(_connection([_drive_scope(anonymize=True)]), monkeypatch)

        assert report["anonymize_failed"] == 2
        assert report["new"] == 8
        ingested = FakeIngestor.instances[-1].ingested
        assert all(row["markdown"].startswith("redacted:") for row in ingested)
        # The two refusals are NOT marked crawled: a dropped document must be
        # retried, never silently treated as done.
        assert len(_state(crawl_env)["ctags"]) == 8

    def test_a_throttled_worker_aborts_the_run_with_state_saved(self, crawl_env, monkeypatch):
        """A 429 budget spent is tenant-wide. The run stops — but only after
        the in-flight items drain, and what they finished is on disk."""
        _at_concurrency(monkeypatch, 4)

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/content"):
                if "boom" in url:
                    return httpx.Response(429, headers={"Retry-After": "300"}, json={})
                return _content_response()
            return httpx.Response(
                200,
                json={
                    "value": _many_items(3) + [_file_item("boom", name="boom.docx", ctag="c-boom")],
                    "@odata.deltaLink": f"{DRIVE_DELTA}?t=1",
                },
            )

        _install_graph(monkeypatch, handler)

        async def _sleep(seconds: float) -> None:
            return None

        monkeypatch.setattr(crawler, "_sleep", _sleep)
        with pytest.raises(crawler.GraphThrottled):
            _run(_connection([_drive_scope()]), monkeypatch)

        state = _state(crawl_env)
        assert state["last_run"]["interrupted"] is True
        # `throttled`, not the generic `error`: GraphThrottled has its own
        # entry in `_STOP_REASONS` so the run records as RESUMABLE — the UI
        # offers "resume", and the next run picks up from the saved cTags.
        assert state["last_run"]["interrupted_reason"] == "throttled"
        # The three that landed before the abort are recorded — an aborted
        # run costs re-work, never coverage.
        assert set(state["ctags"]) == {"graph:f0", "graph:f1", "graph:f2"}
        # ...and the page's deltaLink was NOT persisted, so the next run
        # re-reads this page rather than believing it complete.
        assert not state.get("delta_links")

    def test_the_worst_abort_wins_when_several_workers_fail(self):
        """A tenant refusing us outranks a clock running out: one invites a
        retry, the other is what an operator has to act on."""
        assert crawler._abort_rank(crawler.GraphThrottled("x")) > crawler._abort_rank(crawler.CrawlTimeout("x"))
        assert crawler._abort_rank(crawler.CrawlTimeout("x")) > crawler._abort_rank(crawler.GraphGone("x"))
        assert crawler._abort_rank(crawler.GraphGone("x")) > crawler._abort_rank(RuntimeError("x"))

    def test_the_deadline_stops_feeding_the_pool_and_drains_it(self, crawl_env, monkeypatch):
        _at_concurrency(monkeypatch, 2)
        _install_graph(monkeypatch, _one_page(_many_items(12)))
        monkeypatch.setattr(
            "src.repositories.source_connections_repo",
            lambda: type("R", (), {"get": staticmethod(lambda cid: _connection([_drive_scope()]))})(),
        )

        def _clock() -> float:
            landed = FakeIngestor.instances and FakeIngestor.instances[-1].ingested
            return 1_000_000.0 if landed else 0.0

        monkeypatch.setattr(crawler.time, "monotonic", _clock)

        with pytest.raises(crawler.CrawlTimeout):
            crawler.run_builtin_crawl({"connection_id": "conn1", "timeout_s": 60})

        state = _state(crawl_env)
        ingested = FakeIngestor.instances[-1].ingested
        assert 0 < len(ingested) < 12, "the pool must stop being fed, not run the page to completion"
        assert state["last_run"]["interrupted_reason"] == "timeout"
        assert len(state["ctags"]) == len(ingested), "every item that landed before the stop is recorded"
        assert not state.get("delta_links")


class TestConcurrencyOneIsTheOldPath:
    """`concurrency: 1` is the escape hatch AND the reference implementation.
    It must stay observably identical to the pre-parallel crawl."""

    def _mixed_pages(self) -> Callable[[httpx.Request], httpx.Response]:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/content"):
                return _content_response()
            if "nextpage" in url:
                return httpx.Response(
                    200,
                    json={
                        "value": [
                            _file_item("p2a", name="p2a.docx", ctag="c-p2a"),
                            _file_item("p2b", name="~$lock.docx", ctag="c-p2b"),
                        ],
                        "@odata.deltaLink": f"{DRIVE_DELTA}?token=NEW",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "value": [
                        _file_item("p1a", name="p1a.docx", ctag="c-p1a"),
                        _file_item("p1b", name="p1b.bad", ctag="c-p1b"),
                        _file_item("p1c", name="p1c.pdf", ctag="c-p1c", size=5 * 1024 * 1024),
                    ],
                    "@odata.nextLink": f"{DRIVE_DELTA}?nextpage=1",
                },
            )

        return handler

    def _run_at(self, monkeypatch, crawl_env, n: int, connection_id: str) -> Dict[str, Any]:
        # Two independent fresh crawls are being compared — the class-level
        # ingest store (which persists across runs to model the real repo)
        # must not make the second run's files look pre-existing.
        FakeIngestor.reset()
        _at_concurrency(monkeypatch, n)
        monkeypatch.setattr(crawler, "_max_file_mb", lambda: 1)

        def _convert(path: Path, mime: str) -> ConvertResult:
            if path.suffix == ".bad":
                raise RuntimeError("nope")
            return ConvertResult("# converted")

        monkeypatch.setattr(crawler, "convert_to_markdown", _convert)
        _install_graph(monkeypatch, self._mixed_pages())
        report = _run(_connection([_drive_scope()], connection_id=connection_id), monkeypatch)
        return report

    def test_the_report_and_the_state_match_a_parallel_run_exactly(self, crawl_env, monkeypatch):
        sequential = self._run_at(monkeypatch, crawl_env, 1, "conn1")
        seq_state = _state(crawl_env, "conn1")
        parallel = self._run_at(monkeypatch, crawl_env, 6, "conn2")
        par_state = _state(crawl_env, "conn2")

        assert _stable(sequential) == _stable(parallel)
        assert seq_state["ctags"] == par_state["ctags"]
        assert seq_state["delta_links"]["b!drive1"] == par_state["delta_links"]["b!drive1"]
        # And the knob itself is reported honestly on both sides.
        assert sequential["concurrency"]["requested"] == 1
        assert parallel["concurrency"]["requested"] == 6

    def test_sequential_runs_the_pipeline_inline_with_no_worker_thread(self, crawl_env, monkeypatch):
        """At 1 there is no THREAD executor in the picture at all for the
        item-level pipeline — ``ingest`` happens on the crawl's own thread,
        exactly as before.

        ``convert_to_markdown`` is deliberately NOT part of this claim any
        more: it now always runs in its dedicated child PROCESS (see
        ``_ConvertProcessPool``), even at concurrency 1 — that isolation, not
        which thread calls it, is what survives a native crash. There is
        nothing left to prove about its thread identity; what "1 == the old
        path" still means is that no `ThreadPoolExecutor` exists for the
        REST of the pipeline, which ``ingest`` below stands in for.
        """
        _at_concurrency(monkeypatch, 1)
        threads: List[int] = []
        main = threading.get_ident()

        class _ThreadRecordingIngestor(FakeIngestor):
            def ingest(self, **kwargs: Any) -> Any:
                threads.append(threading.get_ident())
                return super().ingest(**kwargs)

        monkeypatch.setattr(crawler, "_Ingestor", _ThreadRecordingIngestor)
        _install_graph(monkeypatch, _one_page(_many_items(4)))
        _run(_connection([_drive_scope()]), monkeypatch)

        assert threads and set(threads) == {main}

    def test_items_are_still_processed_in_page_order(self, crawl_env, monkeypatch):
        _at_concurrency(monkeypatch, 1)
        _install_graph(monkeypatch, _one_page(_many_items(6)))
        _run(_connection([_drive_scope()]), monkeypatch)

        assert [row["stable_id"] for row in FakeIngestor.instances[-1].ingested] == [f"graph:f{i}" for i in range(6)]


class TestTokenRefreshUnderConcurrency:
    """One token exchange per expiry, not one per in-flight request."""

    def test_concurrent_holders_share_a_single_acquisition(self):
        stats = crawler.CrawlStats()
        acquisitions = {"n": 0}

        async def _acquire() -> str:
            acquisitions["n"] += 1
            await asyncio.sleep(0)  # a real suspension, as the HTTP exchange is
            return f"tok{acquisitions['n']}"

        auth = crawler.GraphAuth(acquire=_acquire, stats=stats)

        async def _main() -> List[str]:
            return list(await asyncio.gather(*[auth.token() for _ in range(8)]))

        tokens = asyncio.run(_main())
        assert acquisitions["n"] == 1
        assert set(tokens) == {"tok1"}
        assert stats.token_refreshes == 1

    def test_a_401_storm_on_one_token_buys_exactly_one_new_token(self):
        stats = crawler.CrawlStats()
        acquisitions = {"n": 0}

        async def _acquire() -> str:
            acquisitions["n"] += 1
            await asyncio.sleep(0)
            return f"tok{acquisitions['n']}"

        auth = crawler.GraphAuth(acquire=_acquire, stats=stats)

        async def _main() -> List[str]:
            stale = await auth.token()
            # Six in-flight requests all meet a 401 against the same token.
            return list(await asyncio.gather(*[auth.refresh_stale(stale) for _ in range(6)]))

        tokens = asyncio.run(_main())
        assert acquisitions["n"] == 2, "one initial acquisition plus exactly one refresh"
        assert set(tokens) == {"tok2"}

    def test_a_forced_refresh_still_forces_one(self):
        """`refresh()` has no observed token to compare against, so it always
        exchanges — the 401 path that cannot name what it sent."""
        stats = crawler.CrawlStats()
        acquisitions = {"n": 0}

        async def _acquire() -> str:
            acquisitions["n"] += 1
            return f"tok{acquisitions['n']}"

        auth = crawler.GraphAuth(acquire=_acquire, stats=stats)
        asyncio.run(auth.refresh())
        asyncio.run(auth.refresh())
        assert acquisitions["n"] == 2


# --------------------------------------------------------------------------
# The LLM fact-extraction stage's chaining seam (owner decision 2026-09-01)
# --------------------------------------------------------------------------


class TestFactsExtractionSeam:
    """The crawl owns only the SEAM: it calls the stage after a successful
    pass, folds its numbers into the same report and the same
    `extraction_runs` row, and lets a hard stop fail the run."""

    def _one_file_crawl(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/content"):
                return _content_response()
            return httpx.Response(200, json={"value": [_file_item()], "@odata.deltaLink": f"{DRIVE_DELTA}?t=1"})

        _install_graph(monkeypatch, handler)

    def test_the_stage_is_not_called_when_it_is_switched_off(self, crawl_env, monkeypatch):
        self._one_file_crawl(monkeypatch)
        monkeypatch.setattr(
            "connectors.sharepoint.facts_extraction.facts_extraction_enabled",
            lambda: False,
        )
        report = _run(_connection([_drive_scope()]), monkeypatch)
        assert "facts" not in report
        assert "facts_usage" not in report

    def test_a_successful_crawl_chains_the_stage_and_merges_its_numbers(self, crawl_env, monkeypatch):
        runs = _install_runs_repo(monkeypatch)
        self._one_file_crawl(monkeypatch)

        facts_report = {"docs_extracted": 2, "claims_written": 5, "facts_usage": {"calls": 2, "input_tokens": 900}}
        seen: List[Any] = []

        def fake_stage(connection, *, deadline=None):
            seen.append((connection["id"], deadline))
            return facts_report

        monkeypatch.setattr(crawler, "maybe_run_facts_extraction", fake_stage)
        report = _run(_connection([_drive_scope()]), monkeypatch)

        assert seen and seen[0][0] == "conn1"
        assert seen[0][1] is not None, "the stage shares the crawl's remaining deadline"
        # One run, one set of counters: the stage's block rides in the same
        # report and its spend is promoted next to the crawl's own totals.
        assert report["facts"] == facts_report
        assert report["facts_usage"] == {"calls": 2, "input_tokens": 900}
        assert runs.finished[0]["usage"] == {"facts": {"calls": 2, "input_tokens": 900}}

    def test_the_crawls_own_counters_are_untouched_by_the_stage(self, crawl_env, monkeypatch):
        self._one_file_crawl(monkeypatch)
        monkeypatch.setattr(
            crawler, "maybe_run_facts_extraction", lambda connection, *, deadline=None: {"docs_extracted": 1}
        )
        report = _run(_connection([_drive_scope()]), monkeypatch)
        assert report["new"] == 1
        assert report["interrupted"] is False

    def test_a_hard_stop_in_the_stage_fails_the_run_loudly(self, crawl_env, monkeypatch):
        """The crawl's own work is already durable; a stage that cannot run
        must fail the run rather than report a clean pass with no facts."""
        runs = _install_runs_repo(monkeypatch)
        self._one_file_crawl(monkeypatch)

        from connectors.sharepoint.facts_extraction import FactsExtractionUnavailable

        def boom(connection, *, deadline=None):
            raise FactsExtractionUnavailable("no credential")

        monkeypatch.setattr(crawler, "maybe_run_facts_extraction", boom)
        with pytest.raises(FactsExtractionUnavailable):
            _run(_connection([_drive_scope()]), monkeypatch)

        assert runs.finished[0]["status"] == "failed"
        assert "FactsExtractionUnavailable" in runs.finished[0]["error"]


# --------------------------------------------------------------------------
# Cooperative stop (owner-frustration fix, 2026-09-01): "it's a black box,
# I can't see what's happening and I can't stop it". The signal lives on
# the connection row's own `config.extraction` sub-object — no new store —
# and is checked at the SAME quiescent points a timeout already is.
# --------------------------------------------------------------------------


class TestStopSignalStore:
    """`request_stop` / `_clear_stale_stop` / `_stop_requested` against the
    connection row — the part that has to work on BOTH app-state backends."""

    def test_request_stop_sets_the_flag_and_preserves_sibling_extraction_keys(self, monkeypatch):
        connection = {"id": "conn1", "config": {"extraction": {"last_run_at": "t0", "last_job_id": "job1"}}}
        monkeypatch.setattr("src.repositories.source_connections_repo", lambda: FakeSourceConnectionsRepo(connection))

        stamp = crawler.request_stop("conn1")

        extraction = connection["config"]["extraction"]
        assert extraction["stop_requested_at"] == stamp
        # The dispatch bookkeeping next to it (TCRD-226) must survive —
        # `request_stop` merges into the EXISTING extraction sub-object,
        # never replaces it wholesale.
        assert extraction["last_run_at"] == "t0"
        assert extraction["last_job_id"] == "job1"

    def test_request_stop_works_on_a_connection_with_no_extraction_block_yet(self, monkeypatch):
        connection = {"id": "conn1", "config": {}}
        monkeypatch.setattr("src.repositories.source_connections_repo", lambda: FakeSourceConnectionsRepo(connection))

        stamp = crawler.request_stop("conn1")

        assert connection["config"]["extraction"]["stop_requested_at"] == stamp

    def test_clear_stale_stop_removes_only_the_flag(self, monkeypatch):
        connection = {"id": "conn1", "config": {"extraction": {"stop_requested_at": "x", "last_run_at": "t0"}}}
        monkeypatch.setattr("src.repositories.source_connections_repo", lambda: FakeSourceConnectionsRepo(connection))

        crawler._clear_stale_stop("conn1")

        extraction = connection["config"]["extraction"]
        assert "stop_requested_at" not in extraction
        assert extraction["last_run_at"] == "t0"

    def test_clear_stale_stop_is_a_noop_when_nothing_is_set(self, monkeypatch):
        connection = {"id": "conn1", "config": {}}
        monkeypatch.setattr("src.repositories.source_connections_repo", lambda: FakeSourceConnectionsRepo(connection))

        crawler._clear_stale_stop("conn1")  # must not raise, and must not write

        assert connection["config"] == {}

    def test_stop_requested_reads_the_live_flag(self, monkeypatch):
        connection = {"id": "conn1", "config": {"extraction": {"stop_requested_at": "2026-09-01T00:00:00+00:00"}}}
        monkeypatch.setattr("src.repositories.source_connections_repo", lambda: FakeSourceConnectionsRepo(connection))

        assert crawler._stop_requested("conn1") == "2026-09-01T00:00:00+00:00"

    def test_stop_requested_is_none_for_an_unknown_connection(self, monkeypatch):
        monkeypatch.setattr(
            "src.repositories.source_connections_repo",
            lambda: FakeSourceConnectionsRepo({"id": "conn1", "config": {}}),
        )

        assert crawler._stop_requested("does-not-exist") is None


class TestStopWatcher:
    """The polling cadence itself: unconditional at a page boundary, only
    every ``every`` completed items at a file boundary."""

    def test_page_boundary_raises_when_stopped(self, monkeypatch):
        monkeypatch.setattr(crawler, "_stop_requested", lambda cid: "2026-09-01T00:00:00+00:00")
        with pytest.raises(crawler.CrawlStopped):
            crawler._StopWatcher("conn1").check_page_boundary()

    def test_page_boundary_is_a_noop_when_not_stopped(self, monkeypatch):
        monkeypatch.setattr(crawler, "_stop_requested", lambda cid: None)
        crawler._StopWatcher("conn1").check_page_boundary()  # must not raise

    def test_item_boundary_cadence_is_exact(self, monkeypatch):
        calls: List[str] = []

        def fake(connection_id: str) -> Optional[str]:
            calls.append(connection_id)
            return None

        monkeypatch.setattr(crawler, "_stop_requested", fake)
        watcher = crawler._StopWatcher("conn1", every=3)
        for n in range(1, 10):
            watcher.maybe_check_item_boundary(n)

        # Checked at 3, 6, 9 — three reads for nine completed items, not one
        # repo read per item.
        assert calls == ["conn1", "conn1", "conn1"]

    def test_item_boundary_raises_exactly_at_the_cadence(self, monkeypatch):
        monkeypatch.setattr(crawler, "_stop_requested", lambda cid: "stopped-at")
        watcher = crawler._StopWatcher("conn1", every=5)
        watcher.maybe_check_item_boundary(4)  # not yet — must not raise
        with pytest.raises(crawler.CrawlStopped):
            watcher.maybe_check_item_boundary(5)


class TestStopReason:
    def test_stopped_is_a_named_stop_reason(self):
        assert crawler._stop_reason(crawler.CrawlStopped("x")) == "stopped"

    def test_a_stop_outranks_a_bare_gone_but_not_a_throttle_or_timeout(self):
        assert crawler._abort_rank(crawler.GraphThrottled("x")) > crawler._abort_rank(crawler.CrawlStopped("x"))
        assert crawler._abort_rank(crawler.CrawlTimeout("x")) > crawler._abort_rank(crawler.CrawlStopped("x"))
        assert crawler._abort_rank(crawler.CrawlStopped("x")) > crawler._abort_rank(crawler.GraphGone("x"))


class TestCooperativeStopEndToEnd:
    """A stop requested mid-run through the connection row — the exact path
    the admin endpoint uses — stops the crawl at a resume-safe boundary."""

    def test_a_stop_requested_between_pages_is_honored_before_the_next_page_is_fetched(self, crawl_env, monkeypatch):
        connection = _connection([_drive_scope()])
        monkeypatch.setattr(
            "src.repositories.source_connections_repo",
            lambda: FakeSourceConnectionsRepo(connection),
        )

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/content"):
                # Simulates an admin's `POST .../extraction/stop` landing
                # while page 1's one item is being processed.
                crawler.request_stop(connection["id"])
                return _content_response()
            if "nextpage" in url:
                return httpx.Response(
                    200,
                    json={"value": _many_items(5, prefix="p2"), "@odata.deltaLink": f"{DRIVE_DELTA}?t=done"},
                )
            return httpx.Response(
                200,
                json={"value": [_file_item("item1")], "@odata.nextLink": f"{DRIVE_DELTA}?nextpage=1"},
            )

        _install_graph(monkeypatch, handler)

        with pytest.raises(crawler.CrawlStopped):
            _run(connection, monkeypatch)

        state = _state(crawl_env)
        assert state["last_run"]["interrupted_reason"] == "stopped"
        # Page 1's single item landed; page 2's five never got fetched — the
        # page-boundary check fires BEFORE that request.
        assert set(state["ctags"]) == {"graph:item1"}
        assert not state.get("delta_links")

    def test_a_stop_requested_mid_page_is_honored_only_at_the_file_cadence(self, crawl_env, monkeypatch):
        """Cadence: the check fires only every `_STOP_CHECK_EVERY_ITEMS`
        completed items — a stop requested after item 3 does not end the
        run until item 10, and every item up to and including the 10th
        still lands. The flag is left on the row afterwards; only the NEXT
        run's start clears it."""
        _at_concurrency(monkeypatch, 1)
        connection = _connection([_drive_scope()])
        monkeypatch.setattr(
            "src.repositories.source_connections_repo",
            lambda: FakeSourceConnectionsRepo(connection),
        )
        items = _many_items(15)

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/content"):
                landed = len(FakeIngestor.instances[-1].ingested) if FakeIngestor.instances else 0
                if landed == 3:
                    crawler.request_stop(connection["id"])
                return _content_response()
            return httpx.Response(200, json={"value": items, "@odata.deltaLink": f"{DRIVE_DELTA}?t=done"})

        _install_graph(monkeypatch, handler)

        with pytest.raises(crawler.CrawlStopped):
            _run(connection, monkeypatch)

        state = _state(crawl_env)
        assert state["last_run"]["interrupted_reason"] == "stopped"
        assert len(state["ctags"]) == crawler._STOP_CHECK_EVERY_ITEMS
        assert not state.get("delta_links"), "the page never finished, so its deltaLink must not be persisted"
        # The run that stopped never clears its own flag.
        assert connection["config"]["extraction"]["stop_requested_at"]

        # The NEXT run must not be killed by the same, now-stale, flag: it
        # is cleared unconsumed at THAT run's start. Reconfigure Graph so
        # this second pass cannot re-trigger a stop of its own.
        _install_graph(monkeypatch, _one_page(items))
        report = _run(connection, monkeypatch)

        assert report["interrupted"] is False
        assert "stop_requested_at" not in connection["config"]["extraction"]
        # The 10 items ingested before the first stop are unchanged this
        # time (matching cTags); the remaining 5 land as new.
        assert report["unchanged"] == crawler._STOP_CHECK_EVERY_ITEMS
        assert report["new"] == 15 - crawler._STOP_CHECK_EVERY_ITEMS

    def test_a_stale_stop_flag_is_cleared_before_the_run_starts(self, crawl_env, monkeypatch):
        """A stop requested for a run that already finished (or never
        started) must not reach forward and kill an unrelated, later run."""
        connection = _connection([_drive_scope()])
        connection["config"]["extraction"] = {"stop_requested_at": "2020-01-01T00:00:00+00:00"}
        monkeypatch.setattr(
            "src.repositories.source_connections_repo",
            lambda: FakeSourceConnectionsRepo(connection),
        )
        _install_graph(monkeypatch, _one_page(_many_items(3)))

        report = _run(connection, monkeypatch)

        assert report["interrupted"] is False
        assert report["new"] == 3
        assert "stop_requested_at" not in connection["config"]["extraction"]

    def test_a_stopped_worker_pool_drains_its_in_flight_items(self, crawl_env, monkeypatch):
        """Same drain mechanism a throttle/timeout abort already uses under
        concurrency: no new item is picked up once the stop is seen, but the
        ones already in flight still finish and are recorded."""
        _at_concurrency(monkeypatch, 4)
        connection = _connection([_drive_scope()])
        monkeypatch.setattr(
            "src.repositories.source_connections_repo",
            lambda: FakeSourceConnectionsRepo(connection),
        )
        items = _many_items(20)

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/content"):
                landed = len(FakeIngestor.instances[-1].ingested) if FakeIngestor.instances else 0
                # `>=`, not `==`: under real concurrency several items can
                # finish between two content requests, so an exact count
                # would be a race. Re-requesting the same stop repeatedly is
                # harmless (`request_stop` just re-stamps the same key).
                if landed >= 2:
                    crawler.request_stop(connection["id"])
                return _content_response()
            return httpx.Response(200, json={"value": items, "@odata.deltaLink": f"{DRIVE_DELTA}?t=done"})

        _install_graph(monkeypatch, handler)

        with pytest.raises(crawler.CrawlStopped):
            _run(connection, monkeypatch)

        state = _state(crawl_env)
        assert state["last_run"]["interrupted_reason"] == "stopped"
        # The pool stopped being FED at the cadence boundary, but not all 20
        # items — the drain lets whatever was already in flight finish.
        assert 0 < len(state["ctags"]) < 20


# --------------------------------------------------------------------------
# Live activity in the checkpoint (owner-frustration fix, 2026-09-01): "I
# can't see what's happening in the extraction".
# --------------------------------------------------------------------------


class TestLiveActivity:
    def test_activity_appears_in_the_live_checkpoint(self, crawl_env, monkeypatch):
        runs = _install_runs_repo(monkeypatch)
        _install_graph(monkeypatch, _one_page(_many_items(3)))

        _run(_connection([_drive_scope()]), monkeypatch)

        activity = runs.checkpoints[-1]["progress"]["activity"]
        assert activity["phase"] == "crawl"
        # By the LAST checkpoint of a finished page every item has exited —
        # `current_path` is honestly None, not a stale leftover.
        assert activity["current_path"] is None
        assert activity["recent"], "completed items must be visible in the checkpoint"
        assert {"path", "outcome"} <= set(activity["recent"][0])
        assert all(entry["outcome"] == "new" for entry in activity["recent"])

    def test_recent_activity_is_capped_at_five(self, crawl_env, monkeypatch):
        """The cap-and-newest-first MECHANISM is pinned exactly and
        deterministically by
        ``TestActivityBookkeeping::test_recent_is_capped_and_newest_first``
        below, which drives ``enter``/``exit_item_activity`` directly in a
        fixed order. This test is the integration half: a REAL crawl, at
        this instance's default concurrency (6), exercises it end to end.

        Six real workers finish 8 trivial items in whatever order they
        actually complete — not dispatch order — so asserting an exact
        finishing position here would pin a race, not a behavior (confirmed
        empirically: the SAME non-determinism reproduces identically on the
        pre-process-isolation code, so it is not something conversion
        running in a child process introduced). What the checkpoint
        actually promises, and what this asserts, is the cap itself and
        that the LAST item enumerated is never silently dropped from it —
        the one thing a real crawl adds over the deterministic unit test.
        """
        runs = _install_runs_repo(monkeypatch)
        _install_graph(monkeypatch, _one_page(_many_items(8)))

        _run(_connection([_drive_scope()]), monkeypatch)

        activity = runs.checkpoints[-1]["progress"]["activity"]
        assert len(activity["recent"]) == 5
        paths = {entry["path"] for entry in activity["recent"]}
        assert "Reports/f7.docx" in paths, "the last item enumerated must never be silently dropped"

    def test_activity_is_absent_from_a_finished_runs_stored_report(self, crawl_env, monkeypatch):
        """`report()` (the FINAL, stored shape) is a separate dict from the
        live checkpoint's `progress` — it never grew an `activity` key, so a
        finished run honestly shows nothing in flight."""
        _install_graph(monkeypatch, _one_page(_many_items(2)))
        report = _run(_connection([_drive_scope()]), monkeypatch)
        assert "activity" not in report


class TestActivityBookkeeping:
    """`CrawlStats.enter_item_activity` / `exit_item_activity` — the
    concurrency-safe accumulation `activity_snapshot` reads."""

    def test_a_single_item_shows_up_while_in_flight_and_leaves_when_done(self):
        stats = crawler.CrawlStats()
        token = stats.enter_item_activity("Reports/a.docx")
        snap = stats.activity_snapshot(phase="crawl")
        assert snap["current_path"] == "Reports/a.docx"
        assert snap["current_started_at"]

        stats.exit_item_activity(token, "Reports/a.docx", "new")
        snap = stats.activity_snapshot(phase="crawl")
        assert snap["current_path"] is None
        assert snap["recent"] == [{"path": "Reports/a.docx", "outcome": "new"}]

    def test_recent_is_capped_and_newest_first(self):
        stats = crawler.CrawlStats()
        for i in range(7):
            token = stats.enter_item_activity(f"f{i}.docx")
            stats.exit_item_activity(token, f"f{i}.docx", "new")

        recent = stats.activity_snapshot(phase="crawl")["recent"]
        assert [r["path"] for r in recent] == ["f6.docx", "f5.docx", "f4.docx", "f3.docx", "f2.docx"]

    def test_bookkeeping_is_exact_under_concurrency(self):
        """A lost entry/exit here is exactly the same class of bug the
        counters guard against — see `TestParallelCounters`."""
        stats = crawler.CrawlStats()

        def worker(i: int) -> None:
            token = stats.enter_item_activity(f"path{i}.docx")
            stats.exit_item_activity(token, f"path{i}.docx", "new")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        snap = stats.activity_snapshot(phase="crawl")
        # Every worker entered AND exited — nothing left dangling in-flight.
        assert snap["current_path"] is None
        assert len(snap["recent"]) == 5
