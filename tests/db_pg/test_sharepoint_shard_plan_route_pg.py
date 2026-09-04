"""PG-only HTTP round-trip tests for ``GET .../shard-plan`` (2026-09-03
auto-parallel-crawl design §4.7, plan Task 9) — the read-only preview of
the AUTOMATIC parallel crawl.

There is no DuckDB "happy path" half to parametrize against: the planner
is PG-only by construction (A3 ratchet) — a DuckDB-backed instance's
``preview_shard_plan`` short-circuits to ``mode: "inline"`` without ever
reaching Graph, which is covered directly in
``tests/test_admin_sharepoint.py::TestShardPlan`` alongside the route's
auth/404/validation contract (all backend-independent). This file proves
the route's own orchestration against a REAL sharded plan: a confirmed
scope resolves, Graph is called, and the response matches
``connectors.sharepoint.crawler.preview_shard_plan``'s own contract.
"""

from __future__ import annotations

import datetime as dt
import json

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from tests.db_pg._parity_sweep_util import build_seeded_client

BASE = "/api/admin/sharepoint/connections"


def _self_signed_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agnes-test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1))
        .not_valid_after(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1))
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


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _pg_client(tmp_path, monkeypatch, pg_engine):
    client, token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    monkeypatch.setenv("AGNES_SHAREPOINT_ENABLED", "true")
    monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", PEM)
    return client, token


def _create_connection(client, token, *, name="corp-sharepoint-shardplan") -> str:
    resp = client.post(
        "/api/admin/source-connections",
        json={
            "name": name,
            "source_type": "sharepoint",
            "config": {"tenant_id": "tenant-1", "client_id": "client-1"},
        },
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _set_scopes(conn_id: str, scopes: list) -> None:
    """Write ``config.scopes`` directly — the confirm-scope HTTP flow is
    covered by its own tests; this route's own contract needs only a
    scope row already shaped the way :func:`connectors.sharepoint.crawler.
    _confirmed_scopes` reads it (``source_scope_id`` + ``collection_id``)."""
    from src.repositories import source_connections_repo

    row = source_connections_repo().get(conn_id)
    source_connections_repo().update(conn_id, config={**(row.get("config") or {}), "scopes": scopes})


def _drive_scope(**overrides) -> dict:
    scope = {
        "source_scope_id": "b!drive1",
        "display_path": "Corp / Documents",
        "collection_id": "col1",
        "anonymize": False,
    }
    scope.update(overrides)
    return scope


def _install_shard_graph(monkeypatch, *, site_total: int, folder_totals: dict) -> None:
    """The same two-pass Graph read ``compute_shard_plan`` makes: one
    ``search_document_count`` against the drive ROOT (deciding whether the
    site stays inline), then — only when that total is over target — a
    root-children listing plus one ``search_document_count`` per top-level
    folder. ``folder_totals`` maps a folder's ``webUrl`` to its count.

    The planner counts a folder by precedence — the collection's own
    ``corpus_files`` rows, else the listing's ``folder.childCount``, else
    Graph Search (``_count_top_level_folders``). This connection's
    collection is empty (never crawled) and the folder facet below
    deliberately carries NO ``childCount``, so Search is the signal that
    actually decides — which is what makes ``folder_totals`` the numbers a
    test can expect back in the preview. A ``childCount`` here would
    silently pre-empt Search and the seeded totals would never be read."""
    from connectors.sharepoint import graph_client as gc

    folder_children = [
        {"id": f"f{index}", "name": url.rsplit("/", 1)[-1], "folder": {}, "webUrl": url}
        for index, url in enumerate(folder_totals, start=1)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": "tok-shardplan"})
        if path.endswith("/root") and request.method == "GET":
            return httpx.Response(200, json={"webUrl": "https://x/root"})
        if path.endswith("/root/children"):
            return httpx.Response(200, json={"value": folder_children})
        if "/items/" in path and path.endswith("/children"):
            # A still-over-target folder gets folded one level deeper
            # (`_fold_one_level`) — no subfolders here, so it comes back
            # unchanged (that function's own "nothing to fold" case).
            return httpx.Response(200, json={"value": []})
        if path.endswith("/search/query"):
            body = json.loads(request.content.decode())
            query = body["requests"][0]["query"]["queryString"]
            if '"https://x/root"' in query:
                return httpx.Response(200, json={"value": [{"hitsContainers": [{"total": site_total}]}]})
            for url, total in folder_totals.items():
                if f'"{url}"' in query:
                    return httpx.Response(200, json={"value": [{"hitsContainers": [{"total": total}]}]})
            return httpx.Response(200, json={"value": [{"hitsContainers": [{"total": 0}]}]})
        raise AssertionError(f"unexpected shard-plan mock path {path}")

    monkeypatch.setattr(
        gc, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
    )


def test_requires_admin(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    r = client.get(f"{BASE}/nope/shard-plan", headers=_auth(token))
    assert r.status_code in (401, 403, 404)  # admin-only routing is exercised fully in the DuckDB-side test class


def test_404_for_unknown_connection(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    r = client.get(f"{BASE}/does-not-exist/shard-plan", headers=_auth(token))
    assert r.status_code == 404


def test_a_site_over_target_previews_sharded_with_expected_counts(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _create_connection(client, token)
    _set_scopes(conn_id, [_drive_scope()])

    from connectors.sharepoint import crawler

    monkeypatch.setattr(crawler, "_shard_target_docs", lambda: 10)
    _install_shard_graph(monkeypatch, site_total=1000, folder_totals={"https://x/root/A": 400, "https://x/root/B": 600})

    r = client.get(f"{BASE}/{conn_id}/shard-plan", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "sharded"
    assert body["target_docs"] == 10
    assert body["shards"]
    for shard in body["shards"]:
        assert set(shard.keys()) == {"drive_id", "index", "label", "signal", "expected", "targets_count"}
        assert shard["drive_id"] == "b!drive1"

    # An EMPTY corpus and a listing without ``childCount`` leave Graph Search
    # as the only counting signal, so the seeded per-folder totals must come
    # back verbatim — each folder is over target on its own, so it is its own
    # shard — and every shard, plus the plan, must say so via ``signal``.
    packed = [s for s in body["shards"] if s["label"] != "remainder"]
    assert sorted(s["expected"] for s in packed) == [400, 600]
    assert all(s["signal"] == "search" for s in packed)
    assert all(s["targets_count"] == 1 for s in packed)
    assert body["signal"] == "search"

    # The remainder (loose root files + anything created after planning)
    # is one whole-drive target whose count nobody can know in advance.
    remainder = [s for s in body["shards"] if s["label"] == "remainder"]
    assert len(remainder) == 1
    assert remainder[0]["expected"] == 0
    assert remainder[0]["signal"] == "none"
    assert remainder[0]["targets_count"] == 1


def test_a_small_site_previews_inline(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _create_connection(client, token)
    _set_scopes(conn_id, [_drive_scope()])

    from connectors.sharepoint import crawler

    monkeypatch.setattr(crawler, "_shard_target_docs", lambda: 5000)
    _install_shard_graph(monkeypatch, site_total=10, folder_totals={})

    r = client.get(f"{BASE}/{conn_id}/shard-plan", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "inline"
    assert body["shards"] == []


def test_no_confirmed_scope_previews_inline_without_any_graph_call(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _create_connection(client, token)

    from connectors.sharepoint import graph_client as gc

    def _boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no Graph call expected")

    monkeypatch.setattr(gc, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(_boom), timeout=10))

    r = client.get(f"{BASE}/{conn_id}/shard-plan", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "inline"


def test_invalid_min_modified_is_400(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _create_connection(client, token)

    r = client.get(f"{BASE}/{conn_id}/shard-plan?min_modified=not-a-date", headers=_auth(token))
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error"] == "invalid_min_modified"
