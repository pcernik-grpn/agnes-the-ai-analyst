"""The Definitions page's authoring door, and the builder behind it.

Authoring a semantic model had no UI entry point on any surface a reader
reaches by navigating. `POST /api/semantic-models/apply` shipped as the one
write surface, and the only page driving it was the Studio domain
(`/admin/studio/semantic-layer`) — hidden by default since the Studio flag
flipped off, so the endpoint was reachable by URL, CLI and chat and by
nothing a person could click.

These tests pin the door and the rule behind it. The rule is the one the
list's own empty state already states for its import CTA: *never offer a
path that cannot succeed*. Authoring succeeds for an admin always (the
apply endpoint's admin branch is a plain admin write) and for everyone else
only while Studio is on (the non-admin branch queues a suggestion and 403s
`studio_disabled` when the surface is off). So one derived flag —
`can_author_model` — gates the card, the empty-state CTA and the builder
route together; the three can never disagree about who is invited.
"""

from __future__ import annotations

import pytest

from tests.test_web_semantic_layer_browse import _auth, _grant_model, _seed_model


@pytest.fixture
def studio_off(monkeypatch):
    monkeypatch.setenv("AGNES_STUDIO_ENABLED", "0")


@pytest.fixture
def studio_on(monkeypatch):
    monkeypatch.setenv("AGNES_STUDIO_ENABLED", "1")


class TestTheDoorOnDefinitions:
    def test_an_admin_gets_a_new_model_card_in_the_grid(self, seeded_app, studio_off):
        """The agents pattern: "New" is the FIRST CARD of the collection, not
        a button in a toolbar above it. Studio off — an admin's authoring does
        not depend on that flag."""
        _seed_model()
        c = seeded_app["client"]
        r = c.get("/semantic-layer", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        grid = r.text.split('id="models-list"', 1)[1]
        card = grid.split("</a>", 1)[0]
        assert 'href="/semantic-layer/new"' in card
        assert "New model" in card

    def test_the_card_is_not_a_search_row(self, seeded_app, studio_off):
        """The filter engine counts `.fbar-card` inside the active bucket and
        hides what does not match. A New card wearing either hook would inflate
        "3 of 12 models" and vanish mid-search, so it carries neither."""
        _seed_model()
        c = seeded_app["client"]
        r = c.get("/semantic-layer", headers=_auth(seeded_app["admin_token"]))
        grid = r.text.split('id="models-list"', 1)[1]
        card = grid.split("</a>", 1)[0]
        assert "fbar-card" not in card
        assert "data-ft" not in card
        assert 'data-tab="models"' not in card

    def test_a_non_admin_is_not_offered_a_door_that_403s(self, seeded_app, studio_off):
        """Studio off: the non-admin branch of the apply endpoint answers
        `403 studio_disabled`, so an analyst gets no card. This is the empty
        state's own rule applied to the populated state."""
        model = _seed_model()
        _grant_model(model["id"])
        c = seeded_app["client"]
        r = c.get("/semantic-layer", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 200
        assert 'href="/semantic-layer/new"' not in r.text

    def test_a_non_admin_is_offered_the_door_when_studio_is_on(self, seeded_app, studio_on):
        """Studio on: the same analyst's document reaches the moderation queue,
        which is a real outcome, so the card appears. What it does NOT do is
        promise publication — see the builder's own labelling test."""
        model = _seed_model()
        _grant_model(model["id"])
        c = seeded_app["client"]
        r = c.get("/semantic-layer", headers=_auth(seeded_app["analyst_token"]))
        assert 'href="/semantic-layer/new"' in r.text

    def test_the_empty_collection_offers_authoring_beside_importing(self, seeded_app, studio_off):
        """With no models the grid does not render at all (`{% if models %}`),
        so the New card cannot carry this state — the empty panel does, exactly
        as the agents list keeps a primary button in its own empty state.

        Before the builder existed this panel could only send an admin to
        register a SOURCE, and told a non-admin to go ask one. Authoring is now
        a second real path, so the panel names both."""
        c = seeded_app["client"]
        r = c.get("/semantic-layer", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert "No semantic model available" in r.text
        assert "/semantic-layer/new" in r.text


class TestTheBuilderRoute:
    def test_it_renders_for_an_admin(self, seeded_app, studio_off):
        c = seeded_app["client"]
        r = c.get("/semantic-layer/new", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert "New semantic model" in r.text

    def test_it_says_publish_for_an_admin(self, seeded_app, studio_off):
        """The apply endpoint labels its outcome and so must the button that
        calls it: an admin's Save publishes, and the page says so before the
        click rather than reporting it afterwards."""
        c = seeded_app["client"]
        r = c.get("/semantic-layer/new", headers=_auth(seeded_app["admin_token"]))
        assert "Publish" in r.text
        assert "Submit for review" not in r.text

    def test_it_says_submit_for_review_for_a_non_admin(self, seeded_app, studio_on):
        """The Studio builder got this right and it is the half worth keeping:
        never let a queued proposal look like a live model."""
        c = seeded_app["client"]
        r = c.get("/semantic-layer/new", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 200
        assert "Submit for review" in r.text

    def test_a_non_admin_with_studio_off_is_explained_not_404ed(self, seeded_app, studio_off):
        """Same posture as /admin/ontology with the facts flag off: a reachable
        URL whose prerequisite is unmet explains itself. A 404 would read as the
        feature not existing, and a redirect home loses the reason."""
        c = seeded_app["client"]
        r = c.get("/semantic-layer/new", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 200
        assert "Publish" not in r.text
        assert "an admin" in r.text.lower()

    def test_new_does_not_shadow_a_model_called_new(self, seeded_app, studio_off):
        """`/semantic-layer/{slug}` is registered after this route, so a model
        whose slug is literally `new` would be unreachable if the static path
        were declared second. It is declared first, and this is the test that
        notices if someone reorders them."""
        model = _seed_model(id="manual/_/new", slug="new")
        _grant_model(model["id"])
        c = seeded_app["client"]
        r = c.get("/semantic-layer/new", headers=_auth(seeded_app["admin_token"]))
        assert "New semantic model" in r.text
        detail = c.get("/semantic-layer/new/", headers=_auth(seeded_app["admin_token"]))
        assert detail.status_code in (200, 307, 404)


class TestTheBuilderConversation:
    """The Start tab's paste/import UI (PR #2148) was replaced by a builder
    chat — the increment PR #2148's own comment said would come next."""

    def test_the_start_tab_renders_the_conversation_composer(self, seeded_app, studio_off):
        """The page's script is INLINE, not an external file, so its source
        text is part of the HTTP response even though a plain GET never runs
        it — what to assert on is the JS SOURCE, not an HTML attribute that
        only exists after the browser builds it (`BuilderShell.conversation`/
        `.composer` render those at runtime). An earlier version of this test
        asserted `'data-ag-comp="chat"' in r.text`, which happened to pass
        only because that exact string also appears, coincidentally, inside
        this page's own `querySelector('[data-ag-comp="chat"]')` call site —
        a false-positive that would have stayed green even with the
        composer never wired up."""
        c = seeded_app["client"]
        r = c.get("/semantic-layer/new", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert "BuilderShell.conversation(" in r.text
        assert "BuilderShell.composer(" in r.text
        assert "'smb-conv'" in r.text

    def test_the_old_paste_import_ui_is_gone(self, seeded_app, studio_off):
        c = seeded_app["client"]
        r = c.get("/semantic-layer/new", headers=_auth(seeded_app["admin_token"]))
        assert "smb-paste" not in r.text
        assert "smb-import" not in r.text

    def test_the_page_calls_the_semantic_model_builder_turn_endpoint(self, seeded_app, studio_off):
        c = seeded_app["client"]
        r = c.get("/semantic-layer/new", headers=_auth(seeded_app["admin_token"]))
        assert "/api/semantic-models/builder/turn" in r.text

    def test_the_engine_notice_is_rendered_via_the_shared_shell(self, seeded_app, studio_off):
        """Pins the fifth builder into the invariant
        tests/test_every_builder_names_its_engine.py enforces across all five."""
        c = seeded_app["client"]
        r = c.get("/semantic-layer/new", headers=_auth(seeded_app["admin_token"]))
        assert "BuilderShell.engineNotice(" in r.text
