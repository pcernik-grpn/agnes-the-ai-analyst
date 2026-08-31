"""REST-layer tests for the ontology builder admin API (`app/api/ontology.py`,
fact-graph-over-Collections §13.2).

DuckDB backend (the default ``seeded_app`` fixture) proves: the router-level
feature-flag gate, RBAC (admin only), and the PG-only typed-501 fail-clean
shape for every draft-persistence route (``ontology_drafts_repo()`` is
PG-only, A3 ratchet). The dry-run route needs neither Postgres nor a
persisted draft -- its ontology types and document reference are inline, and
Collections repos are a frozen full pair -- so it is fully exercised here
against a real seeded collection/file/chunk with a MOCKED LLM extractor.
Draft CRUD round-trips + Save-only-write against a real Postgres backend
live in ``tests/db_pg/test_ontology_admin_pg.py``.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def ontology_client(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    return seeded_app


def _auth(app, token_key="admin_token"):
    return {"Authorization": f"Bearer {app[token_key]}"}


def _seed_document(seeded_app, *, text="Acme Corp is owned by Litware Capital."):
    from src.repositories import corpus_chunks_repo, corpus_files_repo, file_corpora_repo

    corpus_id = file_corpora_repo().create(name="ontology-src", slug="ontology-src", description=None, created_by="u1")
    file_id = corpus_files_repo().add(
        corpus_id=corpus_id,
        filename="doc.txt",
        sha256="s",
        file_type="txt",
        size_bytes=len(text),
        storage_path="/x",
    )
    corpus_chunks_repo().add_many([{"corpus_id": corpus_id, "file_id": file_id, "ordinal": 0, "text": text}])
    return corpus_id, file_id


# ---------------------------------------------------------------------------
# feature-flag gate — the whole /api/admin/ontology* surface 404s when off.
# ---------------------------------------------------------------------------


def test_drafts_list_404s_when_flag_off(seeded_app):
    r = seeded_app["client"].get("/api/admin/ontology/drafts", headers=_auth(seeded_app))
    assert r.status_code == 404


def test_dry_run_404s_when_flag_off(seeded_app):
    r = seeded_app["client"].post(
        "/api/admin/ontology/dry-run",
        json={"node_types": {}, "edge_types": {}, "collection_id": "x", "file_id": "y"},
        headers=_auth(seeded_app),
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# RBAC — admin only.
# ---------------------------------------------------------------------------


def test_drafts_list_requires_admin_not_just_any_caller(ontology_client):
    r = ontology_client["client"].get("/api/admin/ontology/drafts", headers=_auth(ontology_client, "analyst_token"))
    assert r.status_code == 403


def test_drafts_create_requires_admin(ontology_client):
    r = ontology_client["client"].post(
        "/api/admin/ontology/drafts", json={"name": "x"}, headers=_auth(ontology_client, "analyst_token")
    )
    assert r.status_code == 403


def test_dry_run_requires_admin(ontology_client):
    r = ontology_client["client"].post(
        "/api/admin/ontology/dry-run",
        json={"node_types": {}, "edge_types": {}, "collection_id": "x", "file_id": "y"},
        headers=_auth(ontology_client, "analyst_token"),
    )
    assert r.status_code == 403


def test_drafts_list_anon_401(ontology_client):
    r = ontology_client["client"].get("/api/admin/ontology/drafts")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# PG-only typed-501 fail-clean — draft persistence has no DuckDB half.
# ---------------------------------------------------------------------------


def test_drafts_list_fails_clean_on_duckdb(ontology_client):
    r = ontology_client["client"].get("/api/admin/ontology/drafts", headers=_auth(ontology_client))
    assert r.status_code == 501, r.text
    assert r.json()["error"] == "requires_postgres_backend"


def test_drafts_create_fails_clean_on_duckdb(ontology_client):
    r = ontology_client["client"].post("/api/admin/ontology/drafts", json={"name": "x"}, headers=_auth(ontology_client))
    assert r.status_code == 501, r.text
    assert r.json()["error"] == "requires_postgres_backend"


def test_draft_get_fails_clean_on_duckdb(ontology_client):
    r = ontology_client["client"].get("/api/admin/ontology/drafts/some-id", headers=_auth(ontology_client))
    assert r.status_code == 501, r.text
    assert r.json()["error"] == "requires_postgres_backend"


def test_draft_save_fails_clean_on_duckdb(ontology_client):
    r = ontology_client["client"].post("/api/admin/ontology/drafts/some-id/save", headers=_auth(ontology_client))
    assert r.status_code == 501, r.text
    assert r.json()["error"] == "requires_postgres_backend"


# ---------------------------------------------------------------------------
# dry-run — no Postgres dependency, exercised end-to-end with a mocked LLM.
# ---------------------------------------------------------------------------


def test_dry_run_unknown_collection_404s(ontology_client):
    r = ontology_client["client"].post(
        "/api/admin/ontology/dry-run",
        json={"node_types": {}, "edge_types": {}, "collection_id": "nope", "file_id": "nope"},
        headers=_auth(ontology_client),
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "collection_not_found"


def test_dry_run_llm_not_configured_is_typed_501(ontology_client, monkeypatch):
    corpus_id, file_id = _seed_document(ontology_client)

    def _raise(*a, **k):
        raise ValueError("no ai: block, no ANTHROPIC_API_KEY")

    import app.api.ontology as ontology_mod

    monkeypatch.setattr(ontology_mod, "_make_extractor", _raise)

    r = ontology_client["client"].post(
        "/api/admin/ontology/dry-run",
        json={"node_types": {}, "edge_types": {}, "collection_id": corpus_id, "file_id": file_id},
        headers=_auth(ontology_client),
    )
    assert r.status_code == 501, r.text
    assert r.json()["detail"]["error"] == "llm_not_configured"


def test_dry_run_returns_structured_facts_edges_and_not_captured(ontology_client, monkeypatch):
    corpus_id, file_id = _seed_document(ontology_client)

    captured = {}

    class _FakeExtractor:
        def extract_json(self, prompt, max_tokens, json_schema, schema_name, system=None):
            captured["prompt"] = prompt
            captured["system"] = system
            captured["schema_name"] = schema_name
            return {
                "facts": [
                    {
                        "type": "client",
                        "attrs": {"name": "Acme Corp"},
                        "quote": "Acme Corp is owned by Litware Capital.",
                    }
                ],
                "edges": [
                    {
                        "type": "owned_by",
                        "src_type": "client",
                        "dst_type": "sponsor",
                        "quote": "Acme Corp is owned by Litware Capital.",
                    }
                ],
                "not_captured": [{"sentence": "founded in 1990", "reason": "no 'founded_year' attribute"}],
            }

    import app.api.ontology as ontology_mod

    monkeypatch.setattr(ontology_mod, "_make_extractor", lambda: _FakeExtractor())

    r = ontology_client["client"].post(
        "/api/admin/ontology/dry-run",
        json={
            "node_types": {"client": {"description": "A client company."}},
            "edge_types": {"owned_by": {"src": "client", "dst": "sponsor"}},
            "collection_id": corpus_id,
            "file_id": file_id,
        },
        headers=_auth(ontology_client),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["facts"][0]["type"] == "client"
    assert body["edges"][0]["type"] == "owned_by"
    assert body["not_captured"][0]["sentence"] == "founded in 1990"

    # The document text reached the model as fenced, labeled untrusted data —
    # not folded into the (admin-authored, trusted) system prompt.
    assert "Acme Corp is owned by Litware Capital." in captured["prompt"]
    assert "UNTRUSTED_SOURCE_DATA" in captured["prompt"]
    assert "client" in captured["system"]
    assert "owned_by" in captured["system"]
    assert captured["schema_name"] == "ontology_dry_run"


def test_dry_run_empty_document_text_is_422(ontology_client, monkeypatch):
    corpus_id, file_id = _seed_document(ontology_client, text="")
    # add_many with empty text still creates a chunk row with blank text --
    # _resolve_document_text joins non-empty stripped chunks, so this ends
    # up empty either way; assert the endpoint's own guard fires.
    r = ontology_client["client"].post(
        "/api/admin/ontology/dry-run",
        json={"node_types": {}, "edge_types": {}, "collection_id": corpus_id, "file_id": file_id},
        headers=_auth(ontology_client),
    )
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "document_has_no_text"
