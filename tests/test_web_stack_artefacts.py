"""Add Artefacts to My Stack.

Artefacts (file_corpora uploads) are available to the *user* the moment
they're owned or shared with them; they only become available to the
*default agent* once added to My Stack. This reuses the generic
``user_stack_subscriptions`` model (resource_type='collection') — no new
table, no StackResolver — via three dedicated endpoints
(POST/DELETE/GET /api/stack/artefacts*) and stack-awareness on /library
(the renamed /artefacts).

The standalone My Stack page — including its own dedicated Artefacts tab —
is retired (#1088; /stack now 302s to /library?stack=in_stack). The
``/api/stack/artefacts/*`` endpoints stay live and untouched (below,
``TestArtefactStackApi``); the Library's inline "Add to stack" / "In stack"
badge on each artefact row (``TestArtefactsPageStackAwareness``) is the
surviving UI for the same membership. One piece of the retired tab's UI has
no like-for-like replacement: it kept listing an artefact as "Unavailable"
after the caller's access to it was revoked (the membership row outlives the
grant). The Library's row set is grant-scoped (owned ∪ shared right now), not
membership-scoped, so a revoked artefact simply stops appearing there —
consistent with the issue's own resolution ("no migration of functionality
is required"), but worth stating plainly rather than silently dropping the
assertion that used to pin it.
"""

from __future__ import annotations
import pytest

import io


@pytest.fixture(autouse=True)
def _rail_layout(monkeypatch):
    """This file exercises the RAIL redesign's surfaces (one-shelf marketplace /
    unified Library). Topnav keeps the pre-redesign pages (the /catalog
    pattern) — guarded by tests/test_ui_layout_theme.py::TestDefaultContentParity."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create(seeded_app, name: str, token: str) -> dict:
    r = seeded_app["client"].post("/api/collections", json={"name": name}, headers=_auth(token))
    assert r.status_code == 201, r.text
    return r.json()


def _upload(seeded_app, cid: str, filename: str, content: bytes, ctype: str, token: str):
    return seeded_app["client"].post(
        f"/api/collections/{cid}/files",
        files={"files": (filename, io.BytesIO(content), ctype)},
        headers=_auth(token),
    )


class TestArtefactStackApi:
    def test_add_and_remove_roundtrip(self, seeded_app):
        col = _create(seeded_app, "Roadmap", seeded_app["analyst_token"])
        c = seeded_app["client"]

        r = c.post(f"/api/stack/artefacts/{col['id']}", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["added"] is True
        assert body["card"]["title"] == "Roadmap"
        assert body["card"]["action"]["mode"] == "stack"
        assert body["card"]["action"]["remove_url"] == f"/api/stack/artefacts/{col['id']}"

        r = c.delete(f"/api/stack/artefacts/{col['id']}", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 204

    def test_add_is_idempotent_no_duplicate_membership(self, seeded_app):
        col = _create(seeded_app, "Dup Test", seeded_app["analyst_token"])
        c = seeded_app["client"]
        for _ in range(2):
            r = c.post(f"/api/stack/artefacts/{col['id']}", headers=_auth(seeded_app["analyst_token"]))
            assert r.status_code == 200

        from src.repositories import user_stack_subscriptions_repo

        ids = user_stack_subscriptions_repo().list_for_user("analyst1", "collection")
        assert ids.count(col["id"]) == 1

    def test_add_requires_access(self, seeded_app):
        col = _create(seeded_app, "Admin Private", seeded_app["admin_token"])
        c = seeded_app["client"]
        r = c.post(f"/api/stack/artefacts/{col['id']}", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 403

    def test_add_nonexistent_404(self, seeded_app):
        c = seeded_app["client"]
        r = c.post("/api/stack/artefacts/col_doesnotexist", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 404

    def test_remove_does_not_delete_the_artefact(self, seeded_app):
        col = _create(seeded_app, "Keep Me", seeded_app["analyst_token"])
        c = seeded_app["client"]
        c.post(f"/api/stack/artefacts/{col['id']}", headers=_auth(seeded_app["analyst_token"]))
        r = c.delete(f"/api/stack/artefacts/{col['id']}", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 204

        # Still owned, still reachable on /artefacts — only Stack access dropped.
        get_r = c.get(f"/api/collections/{col['id']}", headers=_auth(seeded_app["analyst_token"]))
        assert get_r.status_code == 200
        artefacts_text = c.get("/library", headers=_auth(seeded_app["analyst_token"])).text
        assert "Keep Me" in artefacts_text

    def test_candidates_excludes_inaccessible_and_already_in_stack(self, seeded_app):
        mine = _create(seeded_app, "Mine Candidate", seeded_app["analyst_token"])
        _create(seeded_app, "Not Mine", seeded_app["admin_token"])
        c = seeded_app["client"]

        r = c.get("/api/stack/artefacts/candidates", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 200
        body = r.json()
        titles = {it["title"] for it in body["items"]}
        assert "Mine Candidate" in titles
        assert "Not Mine" not in titles  # not owned, not shared → invisible

        # Add it → no longer a candidate; total_accessible stays stable.
        c.post(f"/api/stack/artefacts/{mine['id']}", headers=_auth(seeded_app["analyst_token"]))
        r2 = c.get("/api/stack/artefacts/candidates", headers=_auth(seeded_app["analyst_token"]))
        titles2 = {it["title"] for it in r2.json()["items"]}
        assert "Mine Candidate" not in titles2


class TestArtefactsPageStackAwareness:
    def test_not_in_stack_shows_add_button(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        col = _create(seeded_app, "Fresh Upload", seeded_app["analyst_token"])
        text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
        assert f'data-add-to-stack="{col["id"]}"' in text
        assert f'data-stack-badge="{col["id"]}"' not in text

    def test_in_stack_shows_quiet_badge_not_button(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        col = _create(seeded_app, "Already Added", seeded_app["analyst_token"])
        c = seeded_app["client"]
        c.post(f"/api/stack/artefacts/{col['id']}", headers=_auth(seeded_app["analyst_token"]))

        text = c.get("/library", headers=_auth(seeded_app["analyst_token"])).text
        assert f'data-stack-badge="{col["id"]}"' in text
        assert f'data-add-to-stack="{col["id"]}"' not in text
        assert "In stack" in text
