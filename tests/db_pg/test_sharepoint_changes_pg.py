"""PG-only tests for the SharePoint observed-changes feed
(``GET /api/admin/sharepoint/connections/{id}/changes``) and its backing
``corpus_file_events`` log.

There is no DuckDB half to parametrize against (PG-first ratchet, A3) — see
``docs/migrations.md`` -> "Adding a PG-only feature". The DuckDB-side typed
501 (route reachable, repo unavailable) lives in
``tests/test_admin_sharepoint.py::TestChangesFeedFailsCleanOnDuckDB``.

Three layers, mirroring ``tests/db_pg/test_collections_upsert_pg.py``:

* Direct repository tests (``CorpusFileEventsPgRepository``) — pagination,
  since/until filtering, cursor round-trip, malformed cursor.
* Direct calls into ``app.api.collections._upsert_corpus_file`` /
  ``delete_file`` — precise classification (added/updated/renamed),
  sidestepping FastAPI's ``BackgroundTasks``.
* One full HTTP round-trip via ``build_seeded_client("pg", ...)`` proving
  the wizard-to-feed wiring end-to-end on a realistic fixture: upload ->
  update via re-upload with a new sha -> rename -> delete.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_ID = "col_test"
BASE = "/api/admin/sharepoint/connections"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Repository layer
# ---------------------------------------------------------------------------


def _setup_pg(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    with pg_engine.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO file_corpora (id, slug, name, created_by) VALUES (:id, :slug, :name, :by)"),
            {"id": CORPUS_ID, "slug": "test", "name": "Test", "by": "u"},
        )

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()

    from src.repositories.corpus_file_events_pg import CorpusFileEventsPgRepository

    return CorpusFileEventsPgRepository(db_pg.get_engine())


def test_record_rejects_unknown_change_kind(pg_engine, monkeypatch):
    repo = _setup_pg(pg_engine, monkeypatch)
    with pytest.raises(ValueError):
        repo.record(corpus_id=CORPUS_ID, file_id="f1", change="archived", name="a.md")


def test_list_for_empty_corpus_ids_short_circuits(pg_engine, monkeypatch):
    repo = _setup_pg(pg_engine, monkeypatch)
    items, next_cursor = repo.list_for_corpus_ids([])
    assert items == []
    assert next_cursor is None


def test_since_until_filtering_boundaries(pg_engine, monkeypatch):
    repo = _setup_pg(pg_engine, monkeypatch)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(5):
        repo.record(
            corpus_id=CORPUS_ID,
            file_id=f"f{i}",
            change="added",
            name=f"file{i}.md",
            observed_at=base + timedelta(minutes=i),
        )

    # Inclusive on both ends: since=t1, until=t3 -> f1, f2, f3.
    items, _ = repo.list_for_corpus_ids(
        [CORPUS_ID], since=base + timedelta(minutes=1), until=base + timedelta(minutes=3)
    )
    assert [i["file_id"] for i in items] == ["f1", "f2", "f3"]

    # Empty window entirely after the last event.
    items, _ = repo.list_for_corpus_ids([CORPUS_ID], since=base + timedelta(days=1))
    assert items == []


def test_pagination_is_deterministic_and_exhaustive(pg_engine, monkeypatch):
    repo = _setup_pg(pg_engine, monkeypatch)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    expected_ids = [f"f{i}" for i in range(5)]
    for i, file_id in enumerate(expected_ids):
        repo.record(
            corpus_id=CORPUS_ID,
            file_id=file_id,
            change="added",
            name=f"{file_id}.md",
            observed_at=base + timedelta(minutes=i),
        )

    seen: list[str] = []
    cursor = None
    for _ in range(10):  # generous upper bound; the loop breaks on next_cursor is None
        page, cursor = repo.list_for_corpus_ids([CORPUS_ID], limit=2, cursor=cursor)
        seen.extend(item["file_id"] for item in page)
        if cursor is None:
            break
    assert seen == expected_ids


def test_malformed_cursor_raises_value_error(pg_engine, monkeypatch):
    repo = _setup_pg(pg_engine, monkeypatch)
    with pytest.raises(ValueError):
        repo.list_for_corpus_ids([CORPUS_ID], cursor="not-a-real-cursor")


def test_scoped_per_corpus_id(pg_engine, monkeypatch):
    repo = _setup_pg(pg_engine, monkeypatch)
    repo.record(corpus_id=CORPUS_ID, file_id="f1", change="added", name="a.md")
    repo.record(corpus_id="col_other", file_id="f2", change="added", name="b.md")
    items, _ = repo.list_for_corpus_ids([CORPUS_ID])
    assert [i["file_id"] for i in items] == ["f1"]


# ---------------------------------------------------------------------------
# Classification — direct calls into app.api.collections
# ---------------------------------------------------------------------------


@pytest.fixture
def pg_repos(pg_engine, monkeypatch):
    _setup_pg(pg_engine, monkeypatch)
    import src.repositories as factory

    return factory


def _upsert(pg_repos, sources_repo, *, filename, sha256, path, stable_id="graph:item1"):
    from app.api.collections import _upsert_corpus_file

    return _upsert_corpus_file(
        CORPUS_ID,
        path=path,
        stable_id=stable_id,
        source_doc_id=None,
        source_sha256_meta=None,
        filename=filename,
        sha256=sha256,
        file_type="pdf",
        size_bytes=3,
        storage_path=f"/blobs/{sha256}.pdf",
        sources_repo=sources_repo,
    )


def test_upsert_classification_added_updated_renamed(pg_repos):
    """The realistic fixture: upload -> update via re-upload with a new
    sha -> rename (same identity, same content, new name/path)."""
    sources_repo = pg_repos.corpus_file_sources_repo()
    events_repo = pg_repos.corpus_file_events_repo()

    file_id, *_ = _upsert(pg_repos, sources_repo, filename="report.pdf", sha256="sha-a", path="docs/report.pdf")
    file_id2, *_ = _upsert(pg_repos, sources_repo, filename="report.pdf", sha256="sha-b", path="docs/report.pdf")
    file_id3, *_ = _upsert(pg_repos, sources_repo, filename="renamed.pdf", sha256="sha-b", path="docs/renamed.pdf")

    assert file_id == file_id2 == file_id3  # same identity throughout

    items, _ = events_repo.list_for_corpus_ids([CORPUS_ID])
    assert [i["change"] for i in items] == ["added", "updated", "renamed"]
    assert all(i["file_id"] == file_id for i in items)
    assert all(i["source_stable_id"] == "graph:item1" for i in items)
    assert items[0]["name"] == "report.pdf"
    assert items[0]["path"] == "docs/report.pdf"
    assert items[2]["name"] == "renamed.pdf"
    assert items[2]["path"] == "docs/renamed.pdf"


def test_byte_identical_resync_is_not_a_change(pg_repos):
    """A retry/resync that changes neither content nor path/filename is not
    a reportable change — only the genuine transitions above are."""
    sources_repo = pg_repos.corpus_file_sources_repo()
    events_repo = pg_repos.corpus_file_events_repo()

    _upsert(pg_repos, sources_repo, filename="a.md", sha256="same-sha", path="docs/a.md")
    _upsert(pg_repos, sources_repo, filename="a.md", sha256="same-sha", path="docs/a.md")

    items, _ = events_repo.list_for_corpus_ids([CORPUS_ID])
    assert [i["change"] for i in items] == ["added"]


def test_delete_records_a_deleted_event_with_name_path_and_stable_id(pg_repos):
    from app.api.collections import _resolve_source_stable_id, _record_corpus_file_event, _purge_file_row

    sources_repo = pg_repos.corpus_file_sources_repo()
    events_repo = pg_repos.corpus_file_events_repo()
    cf_repo = pg_repos.corpus_files_repo()

    file_id, *_ = _upsert(pg_repos, sources_repo, filename="a.md", sha256="sha-a", path="docs/a.md")
    row = cf_repo.get(file_id)

    # Mirrors app/api/collections.py::delete_file's own ordering: resolve the
    # stable id BEFORE the purge cascades corpus_file_sources away.
    stable_id = _resolve_source_stable_id(file_id)
    _purge_file_row(CORPUS_ID, row)
    _record_corpus_file_event(
        corpus_id=CORPUS_ID,
        file_id=file_id,
        change="deleted",
        name=row["filename"],
        path=row.get("path"),
        source_stable_id=stable_id,
    )

    assert cf_repo.get(file_id) is None  # row genuinely gone
    items, _ = events_repo.list_for_corpus_ids([CORPUS_ID])
    assert [i["change"] for i in items] == ["added", "deleted"]
    deleted = items[1]
    assert deleted["name"] == "a.md"
    assert deleted["path"] == "docs/a.md"
    assert deleted["source_stable_id"] == "graph:item1"
    assert deleted["file_id"] == file_id


# ---------------------------------------------------------------------------
# HTTP round-trip — the wizard-to-feed wiring end-to-end
# ---------------------------------------------------------------------------


def _pg_client(tmp_path, monkeypatch, pg_engine):
    from tests.db_pg._parity_sweep_util import build_seeded_client

    return build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)


def _create_connection(client, token, *, name="corp-sharepoint"):
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


def test_changes_404_for_unknown_connection(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    r = client.get(f"{BASE}/does-not-exist/changes", headers=_auth(token))
    assert r.status_code == 404
    assert r.json()["detail"] == "connection_not_found"


def test_changes_empty_page_when_no_scopes_confirmed(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _create_connection(client, token)
    r = client.get(f"{BASE}/{conn_id}/changes", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["next_cursor"] is None


def test_full_fixture_added_updated_renamed_deleted(tmp_path, monkeypatch, pg_engine):
    import io

    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _create_connection(client, token)

    scope = client.post(
        f"{BASE}/{conn_id}/scopes",
        json={"source_scope_id": "drive:a", "display_path": "A"},
        headers=_auth(token),
    )
    assert scope.status_code == 201, scope.text
    collection_id = scope.json()["collection_id"]

    upload_kwargs = dict(headers=_auth(token))

    # 1. added
    r1 = client.post(
        f"/api/collections/{collection_id}/files",
        files=[("files", ("report.pdf", io.BytesIO(b"AAA"), "application/pdf"))],
        data={"paths": "docs/report.pdf", "source_stable_ids": "graph:item1"},
        **upload_kwargs,
    )
    assert r1.status_code == 201, r1.text
    file_id = r1.json()[0]["file_id"]

    # 2. updated — same identity/path, new content.
    r2 = client.post(
        f"/api/collections/{collection_id}/files",
        files=[("files", ("report.pdf", io.BytesIO(b"BBB"), "application/pdf"))],
        data={"paths": "docs/report.pdf", "source_stable_ids": "graph:item1"},
        **upload_kwargs,
    )
    assert r2.status_code == 201, r2.text
    assert r2.json()[0]["file_id"] == file_id

    # 3. renamed — same identity/content, new name/path.
    r3 = client.post(
        f"/api/collections/{collection_id}/files",
        files=[("files", ("renamed.pdf", io.BytesIO(b"BBB"), "application/pdf"))],
        data={"paths": "docs/renamed.pdf", "source_stable_ids": "graph:item1"},
        **upload_kwargs,
    )
    assert r3.status_code == 201, r3.text
    assert r3.json()[0]["file_id"] == file_id

    # 4. deleted
    r4 = client.delete(f"/api/collections/{collection_id}/files/{file_id}", headers=_auth(token))
    assert r4.status_code == 204, r4.text

    changes = client.get(f"{BASE}/{conn_id}/changes", headers=_auth(token))
    assert changes.status_code == 200, changes.text
    body = changes.json()
    assert body["connection_id"] == conn_id
    items = body["items"]
    assert [i["change"] for i in items] == ["added", "updated", "renamed", "deleted"]
    for item in items:
        assert item["file_id"] == file_id
        assert item["collection_id"] == collection_id
        assert item["source_stable_id"] == "graph:item1"
        assert item["source_modified"] is None
        assert item["ingest_run_id"] is None
        assert item["observed_at"] is not None
    assert items[0]["name"] == "report.pdf"
    assert items[0]["path"] == "docs/report.pdf"
    assert items[2]["name"] == "renamed.pdf"
    assert items[3]["name"] == "renamed.pdf"  # last known name/path, captured at delete time
    assert body["next_cursor"] is None


def test_changes_limit_and_cursor_paginate_the_http_endpoint(tmp_path, monkeypatch, pg_engine):
    import io

    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _create_connection(client, token)
    scope = client.post(
        f"{BASE}/{conn_id}/scopes",
        json={"source_scope_id": "drive:a", "display_path": "A"},
        headers=_auth(token),
    )
    collection_id = scope.json()["collection_id"]

    for i in range(3):
        r = client.post(
            f"/api/collections/{collection_id}/files",
            files=[("files", (f"f{i}.md", io.BytesIO(f"content-{i}".encode()), "text/markdown"))],
            data={"paths": f"docs/f{i}.md"},
            headers=_auth(token),
        )
        assert r.status_code == 201, r.text

    page1 = client.get(f"{BASE}/{conn_id}/changes", params={"limit": 2}, headers=_auth(token))
    assert page1.status_code == 200
    body1 = page1.json()
    assert len(body1["items"]) == 2
    assert body1["next_cursor"] is not None

    page2 = client.get(
        f"{BASE}/{conn_id}/changes", params={"limit": 2, "cursor": body1["next_cursor"]}, headers=_auth(token)
    )
    assert page2.status_code == 200
    body2 = page2.json()
    assert len(body2["items"]) == 1
    assert body2["next_cursor"] is None

    all_names = [i["name"] for i in body1["items"]] + [i["name"] for i in body2["items"]]
    assert all_names == ["f0.md", "f1.md", "f2.md"]


def test_malformed_cursor_is_typed_400(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _create_connection(client, token)
    r = client.get(f"{BASE}/{conn_id}/changes", params={"cursor": "garbage"}, headers=_auth(token))
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_cursor"
