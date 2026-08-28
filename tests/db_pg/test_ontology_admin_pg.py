"""Write-path tests for the ontology builder (fact-graph-over-Collections
§13.2, "Ontology builder").

PG-only, no DuckDB half to parametrize against (A3 ratchet) -- see
``docs/migrations.md`` -> "Adding a PG-only feature". Two layers, mirroring
``tests/db_pg/test_facts_ingest_pg.py``:

* Direct :class:`OntologyDraftsPgRepository` calls -- precise, fast.
* HTTP round-trips via ``build_seeded_client("pg", ...)`` -- draft CRUD,
  import, and the Save-only-write invariant end to end: section edits never
  create a semantic model; Save creates exactly one.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# repository — direct
# ---------------------------------------------------------------------------


def _repo(pg_engine):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    from src.repositories.ontology_drafts_pg import OntologyDraftsPgRepository

    return OntologyDraftsPgRepository(pg_engine)


def test_create_get_update_delete_round_trip(pg_engine):
    repo = _repo(pg_engine)
    row = repo.create(name="my ontology", created_by="admin1")
    assert row["name"] == "my ontology"
    assert row["node_types"] == {}
    assert row["edge_types"] == {}
    assert row["saved_model_slug"] is None

    fetched = repo.get(row["id"])
    assert fetched["id"] == row["id"]

    updated = repo.update(row["id"], node_types={"client": {"attrs": {}}})
    assert updated["node_types"] == {"client": {"attrs": {}}}
    # Untouched fields survive a partial update.
    assert updated["name"] == "my ontology"

    repo.mark_saved(row["id"], saved_model_slug="my_ontology")
    assert repo.get(row["id"])["saved_model_slug"] == "my_ontology"

    assert row["id"] in [r["id"] for r in repo.list()]

    repo.delete(row["id"])
    assert repo.get(row["id"]) is None


def test_update_unknown_draft_returns_none(pg_engine):
    repo = _repo(pg_engine)
    assert repo.update("does-not-exist", name="x") is None


# ---------------------------------------------------------------------------
# HTTP round-trip
# ---------------------------------------------------------------------------


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _pg_client(tmp_path, monkeypatch, pg_engine):
    from tests.db_pg._parity_sweep_util import build_seeded_client

    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    return client, admin_token


def test_http_draft_crud(tmp_path, monkeypatch, pg_engine):
    client, admin_token = _pg_client(tmp_path, monkeypatch, pg_engine)
    headers = _auth(admin_token)

    created = client.post("/api/admin/ontology/drafts", json={"name": "acme"}, headers=headers)
    assert created.status_code == 201, created.text
    draft_id = created.json()["id"]

    listed = client.get("/api/admin/ontology/drafts", headers=headers)
    assert any(d["id"] == draft_id for d in listed.json())

    got = client.get(f"/api/admin/ontology/drafts/{draft_id}", headers=headers)
    assert got.status_code == 200
    assert got.json()["name"] == "acme"

    deleted = client.delete(f"/api/admin/ontology/drafts/{draft_id}", headers=headers)
    assert deleted.status_code == 204

    missing = client.get(f"/api/admin/ontology/drafts/{draft_id}", headers=headers)
    assert missing.status_code == 404


def test_http_import_fills_draft_and_never_creates_a_semantic_model(tmp_path, monkeypatch, pg_engine):
    client, admin_token = _pg_client(tmp_path, monkeypatch, pg_engine)
    headers = _auth(admin_token)

    draft_id = client.post("/api/admin/ontology/drafts", json={"name": "x"}, headers=headers).json()["id"]

    yaml_text = (
        "name: acme_ontology\n"
        "node_types:\n"
        "  client:\n"
        "    description: A client company.\n"
        "    attrs:\n"
        "      name: {type: string, required: true}\n"
        "edge_types: {}\n"
    )
    r = client.post(f"/api/admin/ontology/drafts/{draft_id}/import", json={"text": yaml_text}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "client" in body["draft"]["node_types"]
    assert "node types" in body["report"].lower()
    assert "datasets" in body["report"].lower()

    models = client.get("/api/admin/semantic-models", headers=headers).json()
    assert not any(m.get("slug") == "acme_ontology" for m in models)


def test_http_save_only_write_editing_never_writes_until_save(tmp_path, monkeypatch, pg_engine):
    """The core invariant (spec §13.2): section edits (PUT) never touch
    semantic_models; Save creates exactly one model, exactly once."""
    client, admin_token = _pg_client(tmp_path, monkeypatch, pg_engine)
    headers = _auth(admin_token)

    draft_id = client.post("/api/admin/ontology/drafts", json={"name": "widgets_co"}, headers=headers).json()["id"]

    def _model_slugs():
        return {m.get("slug") for m in client.get("/api/admin/semantic-models", headers=headers).json()}

    before = _model_slugs()

    # Several section edits — none of these is a write to the live ontology.
    for i in range(3):
        r = client.put(
            f"/api/admin/ontology/drafts/{draft_id}",
            json={"node_types": {f"entity_{i}": {"attrs": {"name": {"type": "string"}}}}},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert _model_slugs() == before

    r = client.put(
        f"/api/admin/ontology/drafts/{draft_id}",
        json={
            "node_types": {"client": {"attrs": {"name": {"type": "string"}}}},
            "edge_types": {"owned_by": {"src": "client", "dst": "client"}},
        },
        headers=headers,
    )
    assert r.status_code == 200
    assert _model_slugs() == before

    # Save — the ONE write.
    saved = client.post(f"/api/admin/ontology/drafts/{draft_id}/save", headers=headers)
    assert saved.status_code == 200, saved.text
    slug = saved.json()["model"]["slug"]
    assert slug == "widgets_co"
    assert _model_slugs() == before | {slug}

    # The draft itself records what it became.
    draft = client.get(f"/api/admin/ontology/drafts/{draft_id}", headers=headers).json()
    assert draft["saved_model_slug"] == slug

    # Saving again (Save posts once per click, but the button can be pressed
    # twice) upserts the same slug rather than creating a second row.
    saved_again = client.post(f"/api/admin/ontology/drafts/{draft_id}/save", headers=headers)
    assert saved_again.status_code == 200
    assert saved_again.json()["model"]["slug"] == slug
    all_models = client.get("/api/admin/semantic-models", headers=headers).json()
    assert sum(1 for m in all_models if m.get("slug") == slug) == 1


def test_http_save_empty_draft_is_422(tmp_path, monkeypatch, pg_engine):
    client, admin_token = _pg_client(tmp_path, monkeypatch, pg_engine)
    headers = _auth(admin_token)
    draft_id = client.post("/api/admin/ontology/drafts", json={"name": "empty"}, headers=headers).json()["id"]

    r = client.post(f"/api/admin/ontology/drafts/{draft_id}/save", headers=headers)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "empty_draft"


def test_http_save_evidence_required_false_survives_into_the_model(tmp_path, monkeypatch, pg_engine):
    client, admin_token = _pg_client(tmp_path, monkeypatch, pg_engine)
    headers = _auth(admin_token)
    draft_id = client.post("/api/admin/ontology/drafts", json={"name": "ev_test"}, headers=headers).json()["id"]
    client.put(
        f"/api/admin/ontology/drafts/{draft_id}",
        json={
            "node_types": {"a": {"attrs": {}}, "b": {"attrs": {}}},
            "edge_types": {"maybe_related": {"src": "a", "dst": "b", "evidence_required": False}},
        },
        headers=headers,
    )
    saved = client.post(f"/api/admin/ontology/drafts/{draft_id}/save", headers=headers)
    assert saved.status_code == 200, saved.text
    model = client.get(f"/api/admin/semantic-models/{saved.json()['model']['slug']}", headers=headers).json()
    document = model["document"]
    assert "NOT required" in document
