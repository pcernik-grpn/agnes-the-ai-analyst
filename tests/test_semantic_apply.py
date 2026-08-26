"""``POST /api/semantic-models/apply`` — the one semantic-layer write surface
with outcome branching (spec 2026-08-24-semantic-layer-chat-authoring).

Admin callers apply directly (same validate + upsert + project pipeline as the
admin POST); non-admin callers land in the ``authoring_suggestions`` queue
(domain ``semantic-layer``) — the document must never reach ``semantic_models``
before an admin approves. Both branches share the guards: schema-invalid 422,
slug owned by a non-manual source 409, stale ``expected_content_hash`` 409.
"""

from __future__ import annotations

import hashlib


DOC = (
    "version: '0.2.0.dev0'\n"
    "semantic_model:\n"
    "  - name: support\n"
    "    datasets:\n"
    "      - name: tickets\n"
    "        source: db.public.tickets\n"
    "        fields: []\n"
)

DOC_V2 = DOC + "# revised\n"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _apply(client, token, document=DOC, **extra):
    return client.post(
        "/api/semantic-models/apply",
        headers=_auth(token),
        json={"document": document, **extra},
    )


def _seed_imported_model(slug: str = "support"):
    from src.repositories import semantic_model_repo

    return semantic_model_repo().upsert(
        id=f"ossie_git/src1/{slug}",
        slug=slug,
        name=slug,
        description=None,
        document=DOC,
        document_json={"semantic_model": [{"name": slug}]},
        spec_version="0.2.0.dev0",
        content_hash="h1",
        source="ossie_git",
        source_ref="src1",
        status="valid",
        validation_errors=None,
        validated_at=None,
    )


# ---------------------------------------------------------------------------
# Admin branch
# ---------------------------------------------------------------------------


def test_admin_apply_creates_the_model_directly(seeded_app):
    c = seeded_app["client"]
    r = _apply(c, seeded_app["admin_token"], description="Support tickets model")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "applied"
    assert body["model"]["slug"] == "support"
    assert body["model"]["source"] == "manual"

    from src.repositories import semantic_model_repo

    row = semantic_model_repo().get_by_slug("support")
    assert row is not None and row["source"] == "manual"


def test_admin_apply_projects_the_document(seeded_app):
    """The applied model must reach the flat projections the same way the
    admin POST does — not sit in ``semantic_models`` unread."""
    c = seeded_app["client"]
    doc = (
        "version: '0.2.0.dev0'\n"
        "semantic_model:\n"
        "  - name: support\n"
        "    datasets:\n"
        "      - name: tickets\n"
        "        source: db.public.tickets\n"
        "        fields: []\n"
        "    metrics:\n"
        "      - name: open_tickets\n"
        "        description: Count of unresolved tickets\n"
        "        expression:\n"
        "          dialects:\n"
        "            - dialect: ANSI_SQL\n"
        "              expression: COUNT(*)\n"
    )
    r = _apply(c, seeded_app["admin_token"], document=doc)
    assert r.status_code == 200, r.text

    from src.repositories import metric_repo

    ids = {m["id"] for m in metric_repo().list()}
    assert any("open_tickets" in mid for mid in ids), ids


def test_admin_apply_is_create_or_replace(seeded_app):
    c = seeded_app["client"]
    assert _apply(c, seeded_app["admin_token"]).status_code == 200
    r = _apply(c, seeded_app["admin_token"], document=DOC_V2)
    assert r.status_code == 200, r.text

    from src.repositories import semantic_model_repo

    row = semantic_model_repo().get_by_slug("support")
    assert row["document"] == DOC_V2


def test_stale_expected_hash_409s_for_admin(seeded_app):
    c = seeded_app["client"]
    assert _apply(c, seeded_app["admin_token"]).status_code == 200
    r = _apply(
        c,
        seeded_app["admin_token"],
        document=DOC_V2,
        expected_content_hash="not-the-current-hash",
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "stale_document"


def test_matching_expected_hash_applies(seeded_app):
    c = seeded_app["client"]
    assert _apply(c, seeded_app["admin_token"]).status_code == 200
    r = _apply(
        c,
        seeded_app["admin_token"],
        document=DOC_V2,
        expected_content_hash=hashlib.sha256(DOC.encode()).hexdigest(),
    )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Shared guards
# ---------------------------------------------------------------------------


def test_invalid_document_422s(seeded_app):
    c = seeded_app["client"]
    r = _apply(c, seeded_app["admin_token"], document="semantic_model: [oops")
    assert r.status_code == 422


def test_source_owned_slug_409s_for_admin(seeded_app):
    """Unlike the raw admin POST (which would create a shadow ``manual/_/…``
    row next to the imported one), apply refuses to touch a slug an imported
    source owns — for admins too."""
    c = seeded_app["client"]
    _seed_imported_model()
    r = _apply(c, seeded_app["admin_token"])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "source_owned"


def test_source_owned_slug_409s_for_non_admin(seeded_app):
    c = seeded_app["client"]
    _seed_imported_model()
    r = _apply(c, seeded_app["analyst_token"])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "source_owned"


def test_apply_requires_auth(seeded_app):
    c = seeded_app["client"]
    r = c.post("/api/semantic-models/apply", json={"document": DOC})
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Non-admin branch — the moderation queue
# ---------------------------------------------------------------------------


def test_non_admin_apply_submits_for_review(seeded_app):
    c = seeded_app["client"]
    r = _apply(c, seeded_app["analyst_token"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "submitted_for_review"
    assert body["suggestion_id"]

    # The document must NOT be live before approval.
    from src.repositories import authoring_suggestions_repo, semantic_model_repo

    assert semantic_model_repo().get_by_slug("support") is None
    sug = authoring_suggestions_repo().get(body["suggestion_id"])
    assert sug["domain"] == "semantic-layer"
    assert sug["status"] == "pending"
    assert sug["payload"]["document"] == DOC


def test_duplicate_pending_slug_409s(seeded_app):
    c = seeded_app["client"]
    first = _apply(c, seeded_app["analyst_token"]).json()
    r = _apply(c, seeded_app["analyst_token"], document=DOC_V2)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["kind"] == "duplicate_pending"
    assert detail["suggestion_id"] == first["suggestion_id"]


def test_approve_replays_into_a_live_model(seeded_app):
    c = seeded_app["client"]
    sid = _apply(c, seeded_app["analyst_token"]).json()["suggestion_id"]
    r = c.post(
        f"/api/admin/authoring-suggestions/{sid}/approve",
        headers=_auth(seeded_app["admin_token"]),
        json={},
    )
    assert r.status_code == 200, r.text
    assert r.json()["created_resource_id"]

    from src.repositories import semantic_model_repo

    row = semantic_model_repo().get_by_slug("support")
    assert row is not None and row["source"] == "manual"


def test_approve_refuses_a_since_shadowed_slug(seeded_app):
    """An imported model claiming the slug while the proposal sat pending must
    fail the approve (claim reopened), not shadow the imported model."""
    c = seeded_app["client"]
    sid = _apply(c, seeded_app["analyst_token"]).json()["suggestion_id"]
    _seed_imported_model()
    r = c.post(
        f"/api/admin/authoring-suggestions/{sid}/approve",
        headers=_auth(seeded_app["admin_token"]),
        json={},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["kind"] == "create_failed"

    from src.repositories import authoring_suggestions_repo

    assert authoring_suggestions_repo().get(sid)["status"] == "pending"


def test_approve_clears_semantic_draft_pending_flag(seeded_app):
    """semantic-phase5 wave 2: an approved suggestion clears the dedup flag
    for every table its document covers — approve or reject alike."""
    from src.repositories import table_registry_repo

    table_registry_repo().register(id="db.public.tickets", name="db.public.tickets", source_type="local")
    table_registry_repo().mark_semantic_draft_pending("db.public.tickets")

    c = seeded_app["client"]
    sid = _apply(c, seeded_app["analyst_token"]).json()["suggestion_id"]
    r = c.post(
        f"/api/admin/authoring-suggestions/{sid}/approve",
        headers=_auth(seeded_app["admin_token"]),
        json={},
    )
    assert r.status_code == 200, r.text

    row = table_registry_repo().get("db.public.tickets")
    assert row["semantic_draft_pending_at"] is None


def test_reject_also_clears_semantic_draft_pending_flag(seeded_app):
    """The reject path must clear the flag too — a rejected draft is just as
    eligible for a fresh sweep as an approved one."""
    from src.repositories import table_registry_repo

    table_registry_repo().register(id="db.public.tickets", name="db.public.tickets", source_type="local")
    table_registry_repo().mark_semantic_draft_pending("db.public.tickets")

    c = seeded_app["client"]
    sid = _apply(c, seeded_app["analyst_token"]).json()["suggestion_id"]
    r = c.post(
        f"/api/admin/authoring-suggestions/{sid}/reject",
        headers=_auth(seeded_app["admin_token"]),
        json={"note": "not good enough"},
    )
    assert r.status_code == 200, r.text

    row = table_registry_repo().get("db.public.tickets")
    assert row["semantic_draft_pending_at"] is None


def test_non_admin_branch_respects_studio_toggle(seeded_app, monkeypatch):
    import app.api.semantic_models as sm

    monkeypatch.setattr(sm, "get_studio_enabled", lambda: False)
    c = seeded_app["client"]
    # Non-admin: the queue is closed.
    r = _apply(c, seeded_app["analyst_token"])
    assert r.status_code == 403
    assert r.json()["detail"]["kind"] == "studio_disabled"
    # Admin: a plain admin write, not Studio-gated.
    r = _apply(c, seeded_app["admin_token"])
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Studio domain registration
# ---------------------------------------------------------------------------


def test_semantic_layer_studio_domain_is_registered():
    from app.api.authoring_suggestions import _SAFE_REPLAY
    from app.web.studio import get_domain

    spec = get_domain("semantic-layer")
    assert spec is not None and not spec.submit_directly
    assert spec.endpoint == "/api/semantic-models/apply"
    assert "semantic-layer" in _SAFE_REPLAY


def test_apply_is_a_foundation_tool():
    from app.api.mcp.foundation_tools import FOUNDATION_TOOL_NAMES

    assert "apply_semantic_model" in FOUNDATION_TOOL_NAMES
