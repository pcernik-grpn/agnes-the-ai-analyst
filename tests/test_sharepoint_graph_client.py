"""``connectors.sharepoint.graph_client`` — the connect wizard's live
folder-tree browser (spec 2026-08-27 §13.2).

No live network: the ``_http_client()`` seam is monkeypatched to an
``httpx.AsyncClient`` wired to ``httpx.MockTransport``, same idiom as
``tests/test_teams_sigverify.py``. A throwaway self-signed certificate +
RSA key (generated in-process via ``cryptography``) stands in for a real
Entra app-registration certificate — enough to exercise the JWT-assertion
parsing/signing path without any real credential.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import re
import unicodedata
from typing import Any, Dict, List, Optional

import httpx
import jwt as pyjwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from connectors.sharepoint import graph_client as gc


def _self_signed_pem() -> str:
    """A throwaway self-signed certificate + its private key, concatenated —
    exactly the combined-PEM shape ``SharePointSettings.private_key`` is
    documented to hold."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agnes-test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return cert_pem + key_pem


PEM = _self_signed_pem()


def _cert_pem(
    *,
    not_before: datetime.datetime,
    not_after: datetime.datetime,
    subject_cn: str = "agnes-test",
    issuer_cn: str = "agnes-test",
) -> str:
    """Same combined-PEM shape as :func:`_self_signed_pem`, with a caller-
    chosen validity window so ``status``/``expires_in_days`` boundaries are
    testable without waiting on the wall clock."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject_cn)])
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_cn)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return cert_pem + key_pem


class TestBuildClientAssertion:
    def test_signs_a_jwt_with_x5t_header(self):
        token = gc.build_client_assertion("tenant-1", "client-1", PEM)
        header = pyjwt.get_unverified_header(token)
        assert "x5t" in header and header["x5t"]
        claims = pyjwt.decode(token, options={"verify_signature": False})
        assert claims["iss"] == "client-1"
        assert claims["sub"] == "client-1"
        assert claims["aud"] == "https://login.microsoftonline.com/tenant-1/oauth2/v2.0/token"

    def test_missing_certificate_block_raises_typed_error(self):
        key_only = "-----BEGIN PRIVATE KEY-----\nc2VjcmV0\n-----END PRIVATE KEY-----"
        with pytest.raises(gc.SharePointGraphError, match="CERTIFICATE"):
            gc.build_client_assertion("t", "c", key_only)

    def test_missing_private_key_block_raises_typed_error(self):
        cert_only = PEM.split("-----BEGIN PRIVATE KEY-----")[0]
        with pytest.raises(gc.SharePointGraphError, match="PRIVATE KEY"):
            gc.build_client_assertion("t", "c", cert_only)

    def test_unparseable_pem_raises_typed_error_not_a_bare_exception(self):
        garbage = "-----BEGIN CERTIFICATE-----\nnot-real\n-----END CERTIFICATE-----\n" + PEM.split(
            "-----BEGIN PRIVATE KEY-----"
        )[1].join(["-----BEGIN PRIVATE KEY-----", ""])
        with pytest.raises(gc.SharePointGraphError):
            gc.build_client_assertion("t", "c", garbage)


def _install_transport(monkeypatch, handler):
    def _client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)

    monkeypatch.setattr(gc, "_http_client", _client)


class TestGetAppToken:
    def test_returns_access_token_on_200(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/oauth2/v2.0/token")
            return httpx.Response(200, json={"access_token": "graph-token-abc", "expires_in": 3600})

        _install_transport(monkeypatch, handler)
        token = asyncio.run(gc.get_app_token("tenant-1", "client-1", PEM))
        assert token == "graph-token-abc"

    def test_non_200_raises_typed_error(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "invalid_client"})

        _install_transport(monkeypatch, handler)
        with pytest.raises(gc.SharePointGraphError, match="401"):
            asyncio.run(gc.get_app_token("tenant-1", "client-1", PEM))

    def test_missing_access_token_in_body_raises(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"token_type": "Bearer"})

        _install_transport(monkeypatch, handler)
        with pytest.raises(gc.SharePointGraphError, match="access_token"):
            asyncio.run(gc.get_app_token("tenant-1", "client-1", PEM))


class TestBrowseLevels:
    def test_list_sites(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1.0/sites"
            assert request.headers["Authorization"] == "Bearer tok"
            return httpx.Response(
                200,
                json={"value": [{"id": "site1", "displayName": "Corp Site", "webUrl": "https://x/site1"}]},
            )

        _install_transport(monkeypatch, handler)
        sites = asyncio.run(gc.list_sites("tok"))
        assert sites == [{"id": "site1", "name": "Corp Site", "web_url": "https://x/site1"}]

    def test_list_drives(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1.0/sites/site1/drives"
            return httpx.Response(
                200, json={"value": [{"id": "drv1", "name": "Documents", "driveType": "documentLibrary"}]}
            )

        _install_transport(monkeypatch, handler)
        drives = asyncio.run(gc.list_drives("tok", "site1"))
        assert drives == [{"id": "drv1", "name": "Documents", "drive_type": "documentLibrary"}]

    def test_list_root_children(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1.0/drives/drv1/root/children"
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "f1", "name": "Contracts", "folder": {"childCount": 5}},
                        {"id": "f2", "name": "notes.txt", "file": {}},
                    ]
                },
            )

        _install_transport(monkeypatch, handler)
        items = asyncio.run(gc.list_root_children("tok", "drv1"))
        assert items == [
            {"id": "f1", "name": "Contracts", "is_folder": True, "child_count": 5},
            {"id": "f2", "name": "notes.txt", "is_folder": False, "child_count": None},
        ]

    def test_non_200_raises_typed_error(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": {"code": "Forbidden"}})

        _install_transport(monkeypatch, handler)
        with pytest.raises(gc.SharePointGraphError, match="403"):
            asyncio.run(gc.list_sites("tok"))

    def test_non_200_error_carries_the_status_code_for_callers_to_classify(self, monkeypatch):
        """`search_folders` needs to tell "this one site/folder is
        forbidden, skip it" apart from "the whole call is broken, stop" —
        that classification reads ``status_code`` off the raised error."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": {"code": "Forbidden"}})

        _install_transport(monkeypatch, handler)
        with pytest.raises(gc.SharePointGraphError) as excinfo:
            asyncio.run(gc.list_sites("tok"))
        assert excinfo.value.status_code == 403


class TestListItemChildren:
    def test_list_item_children(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1.0/drives/drv1/items/it1/children"
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "sub1", "name": "Subfolder", "folder": {"childCount": 2}},
                        {"id": "doc1", "name": "report.pdf", "file": {}},
                    ]
                },
            )

        _install_transport(monkeypatch, handler)
        items = asyncio.run(gc.list_item_children("tok", "drv1", "it1"))
        assert items == [
            {"id": "sub1", "name": "Subfolder", "is_folder": True, "child_count": 2},
            {"id": "doc1", "name": "report.pdf", "is_folder": False, "child_count": None},
        ]

    def test_non_200_raises_typed_error(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": {"code": "itemNotFound"}})

        _install_transport(monkeypatch, handler)
        with pytest.raises(gc.SharePointGraphError, match="404"):
            asyncio.run(gc.list_item_children("tok", "drv1", "missing"))


class TestGetSiteByPath:
    """`get_site_by_path` — the discovery-free, `Sites.Selected`-compatible
    way to reach one site: Graph's by-path addressing
    (``/sites/{hostname}:/{server-relative-path}``) works on a granted site
    even when ``/sites`` enumeration is 403-forbidden."""

    def test_resolves_site_by_hostname_and_path(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1.0/sites/contoso.sharepoint.com:/sites/ProjectHub"
            return httpx.Response(
                200,
                json={
                    "id": "contoso.sharepoint.com,abc,def",
                    "displayName": "Project Hub",
                    "webUrl": "https://contoso.sharepoint.com/sites/ProjectHub",
                },
            )

        _install_transport(monkeypatch, handler)
        site = asyncio.run(gc.get_site_by_path("tok", "contoso.sharepoint.com", "sites/ProjectHub"))
        assert site == {
            "id": "contoso.sharepoint.com,abc,def",
            "name": "Project Hub",
            "web_url": "https://contoso.sharepoint.com/sites/ProjectHub",
        }

    def test_empty_path_addresses_the_root_site(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1.0/sites/contoso.sharepoint.com"
            return httpx.Response(200, json={"id": "root-id", "displayName": "Home", "webUrl": "https://x/"})

        _install_transport(monkeypatch, handler)
        site = asyncio.run(gc.get_site_by_path("tok", "contoso.sharepoint.com", ""))
        assert site["id"] == "root-id"

    def test_path_segments_are_url_quoted(self, monkeypatch):
        """A segment is percent-encoded on the wire — nothing an admin pastes
        can splice extra path components or query syntax into the Graph URL."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert b"/sites/contoso.sharepoint.com:/sites/Team%20Site" in request.url.raw_path
            return httpx.Response(200, json={"id": "s2", "displayName": "Team Site", "webUrl": "https://x/ts"})

        _install_transport(monkeypatch, handler)
        site = asyncio.run(gc.get_site_by_path("tok", "contoso.sharepoint.com", "sites/Team Site"))
        assert site["id"] == "s2"

    def test_non_200_error_carries_the_status_code(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": {"code": "accessDenied"}})

        _install_transport(monkeypatch, handler)
        with pytest.raises(gc.SharePointGraphError) as exc_info:
            asyncio.run(gc.get_site_by_path("tok", "contoso.sharepoint.com", "sites/NotGranted"))
        assert exc_info.value.status_code == 403


class TestBuildFolderMatcher:
    def test_prefix_mode_is_case_insensitive(self):
        matcher = gc.build_folder_matcher("con", "prefix")
        assert matcher("Contracts") is True
        assert matcher("CONTRACTS 2026") is True
        assert matcher("Subcontracts") is False

    def test_contains_mode(self):
        matcher = gc.build_folder_matcher("tract", "contains")
        assert matcher("Contracts") is True
        assert matcher("Subcontracts") is True
        assert matcher("Invoices") is False

    def test_glob_mode(self):
        matcher = gc.build_folder_matcher("Contracts-*", "glob")
        assert matcher("Contracts-2026") is True
        assert matcher("contracts-old") is True  # case-insensitive
        assert matcher("Contracts") is False

    def test_glob_mode_question_mark_and_charclass(self):
        matcher = gc.build_folder_matcher("Q[1-4]-report", "glob")
        assert matcher("Q1-report") is True
        assert matcher("Q9-report") is False

    def test_czech_diacritics_prefix_match_case_and_composition_insensitive(self):
        # Precomposed (NFC) vs. decomposed (NFD: base letter + combining
        # caron) forms of the same visible string must match identically.
        precomposed = "Přehledy"
        decomposed = unicodedata.normalize("NFD", precomposed)
        matcher = gc.build_folder_matcher(decomposed, "prefix")
        assert matcher(precomposed) is True
        assert matcher("PŘEHLEDY archiv") is True

    def test_diacritics_are_not_stripped(self):
        """Distinct from the client-side filter: `e` must NOT match `é` here."""
        matcher = gc.build_folder_matcher("elektrina", "contains")
        assert matcher("elektřina") is False

    def test_unbalanced_brackets_in_glob_is_malformed(self):
        with pytest.raises(ValueError, match="malformed glob"):
            gc.build_folder_matcher("Contracts[2026", "glob")

    def test_unknown_mode_raises(self):
        with pytest.raises(ValueError, match="unknown search mode"):
            gc.build_folder_matcher("x", "fuzzy")


class _FakeGraphTree:
    """A tiny in-memory Graph server for :func:`gc.search_folders` tests —
    routes ``/sites``, ``/sites/{id}/drives``, ``/drives/{id}/root/children``
    and ``/drives/{id}/items/{id}/children`` off one hand-built tree, so the
    BFS walk can be exercised without a live tenant.

    ``fail_drives``/``fail_children`` let a test force one specific site's
    drive listing (or one specific folder's children listing) to answer with
    a non-200 status instead of the tree data — the seam that exercises
    ``search_folders``' skip-vs-propagate classification without a live
    tenant that actually has an inaccessible site.
    """

    def __init__(self):
        self.sites = [{"id": "s1", "displayName": "Corp Site", "webUrl": "https://x/s1"}]
        self.drives = {"s1": [{"id": "d1", "name": "Documents", "driveType": "documentLibrary"}]}
        # (drive_id, item_id_or_None) -> Graph `value` list
        self.children: Dict[Any, List[Dict[str, Any]]] = {}
        self.calls: List[str] = []
        self.fail_drives: Dict[str, int] = {}  # site_id -> status code
        self.fail_children: Dict[Any, int] = {}  # (drive_id, item_id_or_None) -> status code

    def set_children(self, drive_id: str, item_id: Optional[str], items: List[Dict[str, Any]]) -> None:
        self.children[(drive_id, item_id)] = items

    def fail_drives_for(self, site_id: str, status_code: int) -> None:
        self.fail_drives[site_id] = status_code

    def fail_children_for(self, drive_id: str, item_id: Optional[str], status_code: int) -> None:
        self.fail_children[(drive_id, item_id)] = status_code

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if path == "/v1.0/sites":
            return httpx.Response(200, json={"value": self.sites})
        m = re.match(r"^/v1\.0/sites/([^/]+)/drives$", path)
        if m:
            status = self.fail_drives.get(m.group(1))
            if status is not None:
                return httpx.Response(status, json={"error": {"code": "forbidden-or-not-found"}})
            return httpx.Response(200, json={"value": self.drives.get(m.group(1), [])})
        m = re.match(r"^/v1\.0/drives/([^/]+)/root/children$", path)
        if m:
            status = self.fail_children.get((m.group(1), None))
            if status is not None:
                return httpx.Response(status, json={"error": {"code": "forbidden-or-not-found"}})
            return httpx.Response(200, json={"value": self.children.get((m.group(1), None), [])})
        m = re.match(r"^/v1\.0/drives/([^/]+)/items/([^/]+)/children$", path)
        if m:
            status = self.fail_children.get((m.group(1), m.group(2)))
            if status is not None:
                return httpx.Response(status, json={"error": {"code": "forbidden-or-not-found"}})
            return httpx.Response(200, json={"value": self.children.get((m.group(1), m.group(2)), [])})
        raise AssertionError(f"unexpected path in fake Graph tree: {path}")


def _folder(id_: str, name: str) -> Dict[str, Any]:
    return {"id": id_, "name": name, "folder": {"childCount": 0}}


def _file(id_: str, name: str) -> Dict[str, Any]:
    return {"id": id_, "name": name, "file": {}}


class TestSearchFolders:
    def test_matches_within_a_single_drive_scoped_search(self, monkeypatch):
        tree = _FakeGraphTree()
        tree.set_children("d1", None, [_folder("a1", "Contracts"), _folder("a2", "Invoices")])
        tree.set_children("d1", "a1", [_folder("a1x", "Contracts 2026")])
        _install_transport(monkeypatch, tree.handler)

        matcher = gc.build_folder_matcher("contract", "contains")
        result = asyncio.run(gc.search_folders("tok", matcher=matcher, drive_id="d1", max_depth=5, max_visited=500))
        assert result["truncated"] is False
        names = sorted(m["display_path"] for m in result["matches"])
        assert names == ["Contracts", "Contracts / Contracts 2026"]
        assert all(m["drive_id"] == "d1" for m in result["matches"])

    def test_search_scoped_to_a_subtree_via_item_id(self, monkeypatch):
        tree = _FakeGraphTree()
        tree.set_children("d1", None, [_folder("a1", "Contracts"), _folder("a2", "ContractsArchive")])
        tree.set_children("d1", "a2", [_folder("a2x", "Contracts old")])
        _install_transport(monkeypatch, tree.handler)

        matcher = gc.build_folder_matcher("contract", "contains")
        result = asyncio.run(
            gc.search_folders("tok", matcher=matcher, drive_id="d1", item_id="a2", max_depth=5, max_visited=500)
        )
        # Root-level "Contracts" (a1) is OUTSIDE the a2 subtree — must not appear.
        assert [m["display_path"] for m in result["matches"]] == ["Contracts old"]

    def test_max_depth_stops_the_walk_and_reports_truncated(self, monkeypatch):
        tree = _FakeGraphTree()
        tree.set_children("d1", None, [_folder("l0", "Level0")])
        tree.set_children("d1", "l0", [_folder("l1", "Level1")])
        tree.set_children("d1", "l1", [_folder("l2", "Level2")])  # beyond max_depth=1
        _install_transport(monkeypatch, tree.handler)

        matcher = gc.build_folder_matcher("level", "prefix")
        result = asyncio.run(gc.search_folders("tok", matcher=matcher, drive_id="d1", max_depth=1, max_visited=500))
        names = sorted(m["display_path"] for m in result["matches"])
        # Level0 (depth 0) and Level1 (depth 1, its parent's children fetched
        # since depth 0 -> 1 is within max_depth=1) are found; Level2 would
        # require expanding a depth-1 folder past the cap, so it is never
        # fetched and never appears.
        assert names == ["Level0", "Level0 / Level1"]
        assert result["truncated"] is True

    def test_no_truncation_when_everything_reachable_was_covered(self, monkeypatch):
        tree = _FakeGraphTree()
        tree.set_children("d1", None, [_folder("a1", "OnlyFolder")])
        tree.set_children("d1", "a1", [])
        _install_transport(monkeypatch, tree.handler)

        matcher = gc.build_folder_matcher("only", "prefix")
        result = asyncio.run(gc.search_folders("tok", matcher=matcher, drive_id="d1", max_depth=5, max_visited=500))
        assert result["truncated"] is False
        assert [m["display_path"] for m in result["matches"]] == ["OnlyFolder"]

    def test_max_visited_stops_the_walk_and_reports_truncated(self, monkeypatch):
        tree = _FakeGraphTree()
        # A wide root: many sibling folders, each with further children —
        # a small max_visited must stop well before the whole tree is walked.
        root_children = [_folder(f"f{i}", f"Folder{i}") for i in range(10)]
        tree.set_children("d1", None, root_children)
        for i in range(10):
            tree.set_children("d1", f"f{i}", [_folder(f"f{i}x", f"Folder{i}x")])
        _install_transport(monkeypatch, tree.handler)

        matcher = gc.build_folder_matcher("folder", "prefix")
        result = asyncio.run(gc.search_folders("tok", matcher=matcher, drive_id="d1", max_depth=5, max_visited=2))
        assert result["truncated"] is True
        assert result["visited"] <= 2

    def test_files_are_never_matched_or_descended_into(self, monkeypatch):
        tree = _FakeGraphTree()
        tree.set_children("d1", None, [_file("doc1", "Contracts.pdf")])
        _install_transport(monkeypatch, tree.handler)

        matcher = gc.build_folder_matcher("contract", "contains")
        result = asyncio.run(gc.search_folders("tok", matcher=matcher, drive_id="d1", max_depth=5, max_visited=500))
        assert result["matches"] == []
        assert result["truncated"] is False

    def test_global_search_walks_every_site_and_drive_when_none_given(self, monkeypatch):
        tree = _FakeGraphTree()
        tree.sites = [
            {"id": "s1", "displayName": "Site One", "webUrl": "https://x/s1"},
            {"id": "s2", "displayName": "Site Two", "webUrl": "https://x/s2"},
        ]
        tree.drives = {
            "s1": [{"id": "d1", "name": "Docs", "driveType": "documentLibrary"}],
            "s2": [{"id": "d2", "name": "Docs2", "driveType": "documentLibrary"}],
        }
        tree.set_children("d1", None, [_folder("a1", "Contracts")])
        tree.set_children("d2", None, [_folder("b1", "Contracts EU")])
        _install_transport(monkeypatch, tree.handler)

        matcher = gc.build_folder_matcher("contract", "contains")
        result = asyncio.run(gc.search_folders("tok", matcher=matcher, max_depth=5, max_visited=500))
        assert result["truncated"] is False
        paths = sorted(m["display_path"] for m in result["matches"])
        assert paths == ["Site One / Docs / Contracts", "Site Two / Docs2 / Contracts EU"]
        drives_hit = {m["drive_id"] for m in result["matches"]}
        assert drives_hit == {"d1", "d2"}

    def test_one_forbidden_site_is_skipped_and_the_others_still_return_matches(self, monkeypatch):
        """The bug under test: one inaccessible site used to abort the
        entire "search everywhere" walk (`list_drives` raised, unguarded).
        It must instead be skipped, with the other reachable sites still
        searched and their matches returned."""
        tree = _FakeGraphTree()
        tree.sites = [
            {"id": "s1", "displayName": "Site One", "webUrl": "https://x/s1"},
            {"id": "s2", "displayName": "Blocked Site", "webUrl": "https://x/s2"},
            {"id": "s3", "displayName": "Site Three", "webUrl": "https://x/s3"},
        ]
        tree.drives = {
            "s1": [{"id": "d1", "name": "Docs", "driveType": "documentLibrary"}],
            "s3": [{"id": "d3", "name": "Docs3", "driveType": "documentLibrary"}],
        }
        tree.set_children("d1", None, [_folder("a1", "Contracts")])
        tree.set_children("d3", None, [_folder("c1", "Contracts EU")])
        tree.fail_drives_for("s2", 403)
        _install_transport(monkeypatch, tree.handler)

        matcher = gc.build_folder_matcher("contract", "contains")
        result = asyncio.run(gc.search_folders("tok", matcher=matcher, max_depth=5, max_visited=500))

        paths = sorted(m["display_path"] for m in result["matches"])
        assert paths == ["Site One / Docs / Contracts", "Site Three / Docs3 / Contracts EU"]
        assert result["truncated"] is False

    def test_forbidden_site_is_named_in_the_skipped_list(self, monkeypatch):
        tree = _FakeGraphTree()
        tree.sites = [
            {"id": "s1", "displayName": "Site One", "webUrl": "https://x/s1"},
            {"id": "s2", "displayName": "Blocked Site", "webUrl": "https://x/s2"},
        ]
        tree.drives = {"s1": [{"id": "d1", "name": "Docs", "driveType": "documentLibrary"}]}
        tree.set_children("d1", None, [_folder("a1", "Contracts")])
        tree.fail_drives_for("s2", 403)
        _install_transport(monkeypatch, tree.handler)

        matcher = gc.build_folder_matcher("contract", "contains")
        result = asyncio.run(gc.search_folders("tok", matcher=matcher, max_depth=5, max_visited=500))

        assert result["skipped"] == [
            {
                "scope": "site",
                "reason": "forbidden",
                "status_code": 403,
                "site_id": "s2",
                "site_name": "Blocked Site",
                "drive_id": None,
                "item_id": None,
                "display_path": None,
            }
        ]

    def test_a_missing_site_404_is_also_skipped_not_propagated(self, monkeypatch):
        tree = _FakeGraphTree()
        tree.sites = [{"id": "s1", "displayName": "Gone Site", "webUrl": "https://x/s1"}]
        tree.fail_drives_for("s1", 404)
        _install_transport(monkeypatch, tree.handler)

        matcher = gc.build_folder_matcher("contract", "contains")
        result = asyncio.run(gc.search_folders("tok", matcher=matcher, max_depth=5, max_visited=500))

        assert result["matches"] == []
        assert result["skipped"] == [
            {
                "scope": "site",
                "reason": "not_found",
                "status_code": 404,
                "site_id": "s1",
                "site_name": "Gone Site",
                "drive_id": None,
                "item_id": None,
                "display_path": None,
            }
        ]

    def test_a_forbidden_folder_mid_walk_is_skipped_without_losing_earlier_matches(self, monkeypatch):
        """Worse than a whole-site 403: an inaccessible folder discovered
        DURING the walk used to blow up a walk that had already found
        matches, throwing that work away. The match for the forbidden
        folder itself (found as a child of its parent, before descending
        into it) and any sibling matches must both survive."""
        tree = _FakeGraphTree()
        tree.set_children(
            "d1",
            None,
            [_folder("ok", "Contracts OK"), _folder("blocked", "Contracts Blocked")],
        )
        tree.set_children("d1", "ok", [_folder("okx", "Contracts OK sub")])
        tree.fail_children_for("d1", "blocked", 403)
        _install_transport(monkeypatch, tree.handler)

        matcher = gc.build_folder_matcher("contract", "contains")
        result = asyncio.run(gc.search_folders("tok", matcher=matcher, drive_id="d1", max_depth=5, max_visited=500))

        paths = sorted(m["display_path"] for m in result["matches"])
        assert paths == ["Contracts Blocked", "Contracts OK", "Contracts OK / Contracts OK sub"]
        assert result["truncated"] is False
        assert result["skipped"] == [
            {
                "scope": "folder",
                "reason": "forbidden",
                "status_code": 403,
                "site_id": None,
                "site_name": None,
                "drive_id": "d1",
                "item_id": "blocked",
                "display_path": "Contracts Blocked",
            }
        ]

    def test_skipped_permission_gaps_do_not_set_truncated(self, monkeypatch):
        """`truncated` means a depth/visited CAP stopped the walk short of
        covering everything reachable — a permission-caused gap is a
        different fact and must not flip it."""
        tree = _FakeGraphTree()
        tree.sites = [
            {"id": "s1", "displayName": "Site One", "webUrl": "https://x/s1"},
            {"id": "s2", "displayName": "Blocked Site", "webUrl": "https://x/s2"},
        ]
        tree.drives = {"s1": [{"id": "d1", "name": "Docs", "driveType": "documentLibrary"}]}
        tree.set_children("d1", None, [_folder("a1", "Contracts")])
        tree.fail_drives_for("s2", 403)
        _install_transport(monkeypatch, tree.handler)

        matcher = gc.build_folder_matcher("contract", "contains")
        result = asyncio.run(gc.search_folders("tok", matcher=matcher, max_depth=5, max_visited=500))
        assert result["truncated"] is False
        assert len(result["skipped"]) == 1

    def test_unauthorized_401_on_a_site_propagates_instead_of_being_swallowed(self, monkeypatch):
        """A 401 means the token itself is bad — that breaks the WHOLE
        search, not just one site. Swallowing it per-site would turn a
        broken connection into an empty, successful-looking search."""
        tree = _FakeGraphTree()
        tree.sites = [{"id": "s1", "displayName": "Site One", "webUrl": "https://x/s1"}]
        tree.fail_drives_for("s1", 401)
        _install_transport(monkeypatch, tree.handler)

        matcher = gc.build_folder_matcher("contract", "contains")
        with pytest.raises(gc.SharePointGraphError) as excinfo:
            asyncio.run(gc.search_folders("tok", matcher=matcher, max_depth=5, max_visited=500))
        assert excinfo.value.status_code == 401

    def test_rate_limited_429_on_a_site_propagates(self, monkeypatch):
        tree = _FakeGraphTree()
        tree.sites = [{"id": "s1", "displayName": "Site One", "webUrl": "https://x/s1"}]
        tree.fail_drives_for("s1", 429)
        _install_transport(monkeypatch, tree.handler)

        matcher = gc.build_folder_matcher("contract", "contains")
        with pytest.raises(gc.SharePointGraphError) as excinfo:
            asyncio.run(gc.search_folders("tok", matcher=matcher, max_depth=5, max_visited=500))
        assert excinfo.value.status_code == 429

    def test_server_error_500_mid_walk_propagates(self, monkeypatch):
        tree = _FakeGraphTree()
        tree.set_children("d1", None, [_folder("a1", "Contracts")])
        tree.fail_children_for("d1", "a1", 500)
        _install_transport(monkeypatch, tree.handler)

        matcher = gc.build_folder_matcher("contract", "contains")
        with pytest.raises(gc.SharePointGraphError) as excinfo:
            asyncio.run(gc.search_folders("tok", matcher=matcher, drive_id="d1", max_depth=5, max_visited=500))
        assert excinfo.value.status_code == 500


class _FakeGraphBatch:
    """A tiny in-memory ``POST /$batch`` server for
    :func:`gc.probe_unique_permissions` tests. ``items`` maps item id ->
    ``(status, hasUniqueRoleAssignments-or-None)``; ``None`` for the second
    element means "200 but no listItem/hasUniqueRoleAssignments in the
    body" — the malformed-shape case, distinct from a non-200 ``status``."""

    def __init__(self, items: Dict[str, Any]):
        self.items = items
        self.batch_calls: List[List[Dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1.0/$batch"
        payload = json.loads(request.content)
        requests = payload["requests"]
        self.batch_calls.append(requests)
        assert len(requests) <= 20, "one $batch call must never exceed Graph's own cap"
        responses = []
        for req in requests:
            # url shape: /drives/{drive}/items/{item}?$expand=listItem($select=hasUniqueRoleAssignments)&...
            item_id = req["url"].split("/items/")[1].split("?")[0]
            entry = self.items.get(item_id)
            if entry is None:
                responses.append({"id": req["id"], "status": 404, "body": {}})
                continue
            status, flag = entry
            body: Dict[str, Any] = {"id": item_id}
            if flag is not None:
                body["listItem"] = {"hasUniqueRoleAssignments": flag}
            elif status == 200:
                body["listItem"] = {}  # 200 but the field never came back
            responses.append({"id": req["id"], "status": status, "body": body})
        return httpx.Response(200, json={"responses": responses})


class TestProbeUniquePermissions:
    """Batched, best-effort ``hasUniqueRoleAssignments`` probe (module
    docstring on :func:`gc.probe_unique_permissions` names the Graph-shape
    uncertainty) — ADVISORY ONLY. A probe failure must always degrade to
    ``None`` ("unknown"), never raise and never a false ``False``."""

    def test_empty_item_list_returns_empty_dict_without_a_call(self, monkeypatch):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            raise AssertionError("must not call Graph for an empty item list")

        _install_transport(monkeypatch, handler)
        result = asyncio.run(gc.probe_unique_permissions("tok", "d1", []))
        assert result == {}
        assert calls == []

    def test_flags_true_and_false_per_item(self, monkeypatch):
        fake = _FakeGraphBatch({"f1": (200, True), "f2": (200, False)})
        _install_transport(monkeypatch, fake.handler)
        result = asyncio.run(gc.probe_unique_permissions("tok", "d1", ["f1", "f2"]))
        assert result == {"f1": True, "f2": False}
        assert len(fake.batch_calls) == 1

    def test_more_than_graph_batch_cap_splits_into_multiple_batch_calls(self, monkeypatch):
        item_ids = [f"f{i}" for i in range(45)]
        fake = _FakeGraphBatch({iid: (200, True) for iid in item_ids})
        _install_transport(monkeypatch, fake.handler)
        result = asyncio.run(gc.probe_unique_permissions("tok", "d1", item_ids))
        assert result == {iid: True for iid in item_ids}
        # 45 items at a cap of 20 -> 3 batch calls (20, 20, 5), never one call
        # over the cap.
        assert len(fake.batch_calls) == 3
        assert [len(c) for c in fake.batch_calls] == [20, 20, 5]

    def test_non_200_item_status_degrades_to_unknown_not_raise(self, monkeypatch):
        fake = _FakeGraphBatch({"f1": (403, None)})
        _install_transport(monkeypatch, fake.handler)
        result = asyncio.run(gc.probe_unique_permissions("tok", "d1", ["f1"]))
        assert result == {"f1": None}

    def test_missing_item_in_response_degrades_to_unknown(self, monkeypatch):
        fake = _FakeGraphBatch({})  # nothing registered -> handler answers 404 per item
        _install_transport(monkeypatch, fake.handler)
        result = asyncio.run(gc.probe_unique_permissions("tok", "d1", ["ghost"]))
        assert result == {"ghost": None}

    def test_200_but_no_hasuniqueroleassignments_field_degrades_to_unknown(self, monkeypatch):
        fake = _FakeGraphBatch({"f1": (200, None)})
        _install_transport(monkeypatch, fake.handler)
        result = asyncio.run(gc.probe_unique_permissions("tok", "d1", ["f1"]))
        assert result == {"f1": None}

    def test_whole_batch_call_failing_degrades_every_item_to_unknown_without_raising(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="upstream unavailable")

        _install_transport(monkeypatch, handler)
        result = asyncio.run(gc.probe_unique_permissions("tok", "d1", ["f1", "f2"]))
        assert result == {"f1": None, "f2": None}

    def test_network_error_degrades_to_unknown_without_raising(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        _install_transport(monkeypatch, handler)
        result = asyncio.run(gc.probe_unique_permissions("tok", "d1", ["f1"]))
        assert result == {"f1": None}


class TestCertificateMetadata:
    """Certificate-metadata surface (no schema, no new storage — derived at
    request time from the same PEM ``build_client_assertion`` already
    parses). Two real failure modes this closes: a registered certificate
    that does not match what the connection presents (compare the
    thumbprint), and a certificate expiring silently (the status/
    expires_in_days ladder)."""

    def test_derives_subject_issuer_and_matches_the_x5t_actually_sent(self):
        result = gc.certificate_metadata(PEM)
        assert result["reason"] is None
        cert = result["certificate"]
        assert cert["subject"] == "CN=agnes-test"
        assert cert["issuer"] == "CN=agnes-test"
        assert cert["not_before"] and cert["not_after"]

        # The exact value Entra receives in the JWT assertion's x5t header —
        # what an admin actually compares against the app registration.
        token = gc.build_client_assertion("tenant-1", "client-1", PEM)
        header = pyjwt.get_unverified_header(token)
        assert cert["thumbprint_x5t"] == header["x5t"]

        # Conventional uppercase-hex SHA-1 fingerprint (40 hex chars).
        assert len(cert["thumbprint_sha1_hex"]) == 40
        assert cert["thumbprint_sha1_hex"] == cert["thumbprint_sha1_hex"].upper()
        all(c in "0123456789ABCDEF" for c in cert["thumbprint_sha1_hex"])

    def test_status_ok_when_far_from_expiry(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        pem = _cert_pem(not_before=now - datetime.timedelta(days=1), not_after=now + datetime.timedelta(days=90))
        result = gc.certificate_metadata(pem)
        cert = result["certificate"]
        assert cert["status"] == "ok"
        assert cert["expires_in_days"] > 30

    def test_status_expiring_soon_under_30_days(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        pem = _cert_pem(not_before=now - datetime.timedelta(days=1), not_after=now + datetime.timedelta(days=10))
        result = gc.certificate_metadata(pem)
        cert = result["certificate"]
        assert cert["status"] == "expiring_soon"
        assert 0 <= cert["expires_in_days"] <= 30

    def test_status_expired_when_past_not_after(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        pem = _cert_pem(not_before=now - datetime.timedelta(days=60), not_after=now - datetime.timedelta(days=5))
        result = gc.certificate_metadata(pem)
        cert = result["certificate"]
        assert cert["status"] == "expired"
        assert cert["expires_in_days"] < 0

    def test_missing_certificate_block_is_a_clean_typed_absence_not_a_raise(self):
        key_only = "-----BEGIN PRIVATE KEY-----\nc2VjcmV0\n-----END PRIVATE KEY-----"
        result = gc.certificate_metadata(key_only)
        assert result == {"certificate": None, "reason": "no_certificate_configured"}

    def test_empty_string_is_a_clean_typed_absence(self):
        assert gc.certificate_metadata("") == {"certificate": None, "reason": "no_certificate_configured"}

    def test_unparseable_certificate_is_a_clean_typed_absence_not_a_raise(self):
        garbage = "-----BEGIN CERTIFICATE-----\nbm90LXJlYWw=\n-----END CERTIFICATE-----\n"
        result = gc.certificate_metadata(garbage)  # must not raise
        assert result["certificate"] is None
        assert result["reason"].startswith("certificate_unparseable")

    def test_response_never_contains_private_key_material(self):
        """HARD CONSTRAINT: metadata only. Even though ``PEM`` is a combined
        cert+key bundle, nothing derived from the key half may reach the
        returned/serialized response."""
        result = gc.certificate_metadata(PEM)
        serialized = json.dumps(result)
        assert "PRIVATE KEY" not in serialized
        assert "BEGIN CERTIFICATE" not in serialized  # no raw PEM at all — only derived fields


class TestGetAppTokenClientSecret:
    """`get_app_token(..., client_secret=...)` — Entra's plain client-secret
    client-credentials flow. No JWT assertion is built or sent; the secret
    travels in the form body of the same token endpoint."""

    def test_posts_client_secret_and_no_assertion(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["body"] = request.content.decode()
            return httpx.Response(200, json={"access_token": "graph-token-secret", "expires_in": 3600})

        _install_transport(monkeypatch, handler)
        token = asyncio.run(gc.get_app_token("tenant-1", "client-1", "", client_secret="s3cr3t"))
        assert token == "graph-token-secret"
        assert seen["path"].endswith("/oauth2/v2.0/token")
        assert "client_secret=s3cr3t" in seen["body"]
        assert "client_assertion" not in seen["body"]

    def test_no_credential_at_all_is_a_typed_error(self, monkeypatch):
        _install_transport(monkeypatch, lambda request: httpx.Response(200, json={"access_token": "x"}))
        with pytest.raises(gc.SharePointGraphError):
            asyncio.run(gc.get_app_token("tenant-1", "client-1", "", client_secret=""))
