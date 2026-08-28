"""GET /admin/ontology (fact-graph-over-Collections §13.2, "Ontology builder").

Zero new navigation: reachable only via the link on /admin/semantic-layer,
no admin-nav entry of its own. When the `facts` flag is off, or the active
backend is DuckDB (drafts are PG-only), the page renders an explanatory
empty state rather than 404ing or crashing -- same posture as /apps when
data_apps is disabled.
"""

from __future__ import annotations

from pathlib import Path

_TEMPLATE_SRC = Path("app/web/templates/ontology_builder.html").read_text(encoding="utf-8")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_anon_redirects(seeded_app):
    r = seeded_app["client"].get("/admin/ontology", follow_redirects=False)
    assert r.status_code in (302, 303, 307)


def test_non_admin_403(seeded_app):
    r = seeded_app["client"].get("/admin/ontology", headers=_auth(seeded_app["analyst_token"]))
    assert r.status_code == 403


def test_admin_facts_disabled_renders_explanatory_empty_state(seeded_app, monkeypatch):
    monkeypatch.delenv("AGNES_FACTS_ENABLED", raising=False)
    r = seeded_app["client"].get("/admin/ontology", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "facts.enabled" in r.text
    # The builder shell itself must not render when the feature is off.
    assert 'id="ont-app"' not in r.text


def test_admin_facts_enabled_duckdb_backend_renders_postgres_empty_state(seeded_app, monkeypatch):
    """Draft persistence is PG-only (A3 ratchet) -- on a DuckDB-backed
    instance the page must say so, not crash or render a dead-end builder."""
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    r = seeded_app["client"].get("/admin/ontology", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "Postgres" in r.text
    assert 'id="ont-app"' not in r.text


def test_semantic_layer_page_links_to_ontology_builder(seeded_app):
    """The ONLY entry point (spec §13.2: "zero new navigation")."""
    r = seeded_app["client"].get("/admin/semantic-layer", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "/admin/ontology" in r.text


def test_template_extends_admin_page_base():
    assert '{% extends "base_admin_page.html" %}' in _TEMPLATE_SRC


def test_template_save_is_the_only_write_comment_present():
    """Documentation smoke check: the Save-only-write invariant is stated in
    the template itself, not just in code review memory."""
    assert "Save is the only write" in _TEMPLATE_SRC
