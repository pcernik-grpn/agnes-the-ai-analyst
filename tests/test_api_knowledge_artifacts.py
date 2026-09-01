"""Tests for the knowledge_artifacts manifest section + artifact download (K3, #798).

Covers:
- GET /api/sync/manifest gains a ``knowledge_artifacts`` array (always present),
  filtered by the caller's collection grants (fail-closed).
- GET /api/knowledge/artifacts/{corpus_id}/download: 401 unauthenticated, 404
  for an ungranted-or-unknown corpus and for a granted corpus with no built
  artifact, 200 with the exact bytes + ETag for a granted caller, 304 on
  ``If-None-Match`` replay.
"""

from __future__ import annotations

from unittest.mock import patch

from src.knowledge_packaging import artifacts_dir


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_artifact(corpus_id: str, name: str = "Handbook") -> dict:
    """Build+register a knowledge artifact for ``corpus_id`` via a real
    packaging pass (patched data-access seams), so the manifest's
    ``state.json``-backed section actually surfaces it."""
    from src.knowledge_packaging import run_packaging_pass

    chunks = [
        {
            "id": "ck1",
            "corpus_id": corpus_id,
            "file_id": "f1",
            "ordinal": 0,
            "text": "invoices are monthly",
            "embedding": None,
            "section_path": None,
            "page": None,
            "bbox": None,
            "metadata": None,
            "created_at": None,
        },
    ]
    with (
        patch("src.knowledge_packaging._list_chunks", lambda cid: list(chunks)),
        patch("src.knowledge_packaging._list_files", lambda cid: [{"id": "f1", "filename": "billing.md"}]),
        patch("src.knowledge_packaging._list_corpora", lambda: [{"id": corpus_id, "name": name}]),
    ):
        return run_packaging_pass()


def test_manifest_lists_artifact_for_admin(seeded_app):
    c = seeded_app["client"]
    admin = seeded_app["admin_token"]
    col = c.post("/api/collections", json={"name": "KA Col"}, headers=_auth(admin)).json()
    _seed_artifact(col["id"])

    resp = c.get("/api/sync/manifest", headers=_auth(admin))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "knowledge_artifacts" in body
    entries = {e["corpus_id"]: e for e in body["knowledge_artifacts"]}
    assert col["id"] in entries
    entry = entries[col["id"]]
    assert entry["kind"] == "chunks"
    assert entry["md5"]
    assert entry["chunks"] == 1
    assert entry["url"] == f"/api/knowledge/artifacts/{col['id']}/download"


def test_manifest_hides_artifact_from_ungranted_analyst(seeded_app):
    c = seeded_app["client"]
    admin = seeded_app["admin_token"]
    analyst = seeded_app["analyst_token"]
    col = c.post("/api/collections", json={"name": "KA Private"}, headers=_auth(admin)).json()
    _seed_artifact(col["id"])

    resp = c.get("/api/sync/manifest", headers=_auth(analyst))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Key PRESENT even with zero grants — the client's prune gate needs it.
    assert body["knowledge_artifacts"] == []


def test_manifest_key_present_with_no_artifacts_built(seeded_app):
    c = seeded_app["client"]
    admin = seeded_app["admin_token"]
    resp = c.get("/api/sync/manifest", headers=_auth(admin))
    assert resp.status_code == 200, resp.text
    assert resp.json()["knowledge_artifacts"] == []


def test_download_unauthenticated_401(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/api/knowledge/artifacts/col_a/download")
    assert resp.status_code == 401


def test_download_unknown_corpus_404(seeded_app):
    c = seeded_app["client"]
    admin = seeded_app["admin_token"]
    resp = c.get("/api/knowledge/artifacts/col_does_not_exist/download", headers=_auth(admin))
    assert resp.status_code == 404


def test_download_ungranted_analyst_403(seeded_app):
    c = seeded_app["client"]
    admin = seeded_app["admin_token"]
    analyst = seeded_app["analyst_token"]
    col = c.post("/api/collections", json={"name": "KA Priv2"}, headers=_auth(admin)).json()
    _seed_artifact(col["id"])

    resp = c.get(f"/api/knowledge/artifacts/{col['id']}/download", headers=_auth(analyst))
    # require_resource_access gate (standard collection-scoped pattern) → 403;
    # ids are unguessable (col_ + token_hex) so this is not an existence oracle.
    assert resp.status_code == 403


def test_download_granted_corpus_no_artifact_built_404(seeded_app):
    c = seeded_app["client"]
    admin = seeded_app["admin_token"]
    col = c.post("/api/collections", json={"name": "KA NoArtifact"}, headers=_auth(admin)).json()
    # Collection exists (admin is always "granted") but no packaging pass ran.
    resp = c.get(f"/api/knowledge/artifacts/{col['id']}/download", headers=_auth(admin))
    assert resp.status_code == 404


def test_download_soft_deleted_collection_404(seeded_app):
    """A soft-deleted collection must not keep serving its packaged artifact.

    Grants are not revoked on soft-delete (a Library delete, or the SharePoint
    wizard tidying away an emptied scope collection), and this endpoint never
    consulted ``deleted_at`` — so a grant-holder could keep downloading a
    "removed" collection's knowledge.duckdb for as long as the file stayed on
    disk. The admin caller is deliberate: god-mode passes every grant check,
    so a 404 here can only come from the endpoint's own liveness guard.
    """
    c = seeded_app["client"]
    admin = seeded_app["admin_token"]
    col = c.post("/api/collections", json={"name": "KA Deleted"}, headers=_auth(admin)).json()
    _seed_artifact(col["id"])

    # Sanity: downloadable while live.
    assert c.get(f"/api/knowledge/artifacts/{col['id']}/download", headers=_auth(admin)).status_code == 200

    from src.repositories import file_corpora_repo

    file_corpora_repo().soft_delete(col["id"])

    resp = c.get(f"/api/knowledge/artifacts/{col['id']}/download", headers=_auth(admin))
    assert resp.status_code == 404


def test_download_success_bytes_and_etag_then_304(seeded_app):
    c = seeded_app["client"]
    admin = seeded_app["admin_token"]
    col = c.post("/api/collections", json={"name": "KA Dl"}, headers=_auth(admin)).json()
    _seed_artifact(col["id"])

    expected_bytes = (artifacts_dir() / f"{col['id']}.duckdb").read_bytes()

    resp = c.get(f"/api/knowledge/artifacts/{col['id']}/download", headers=_auth(admin))
    assert resp.status_code == 200, resp.text
    assert resp.content == expected_bytes
    etag = resp.headers.get("etag")
    assert etag

    resp2 = c.get(
        f"/api/knowledge/artifacts/{col['id']}/download",
        headers={**_auth(admin), "If-None-Match": etag},
    )
    assert resp2.status_code == 304
