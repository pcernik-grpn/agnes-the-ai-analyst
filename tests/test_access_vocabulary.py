"""The words on /admin/access, pinned.

Nothing else in the repo tests language. The design-system contract tests
police colour, tokens and layout; a rename that leaves the admin saying one
thing and the person on the other end reading another passes every check we
have. Sixteen of the twenty-six open `DR(agnes)` reviews carry a naming
collision, and this control — the one deciding whether a person can stop
worrying about a resource — carried three names at once: `available` /
`required` on the wire, *Optional* / *Automatic* in the admin UI, and
*Required by your admin* / *Keep a local copy* in the Library.

The Library's words win: they are the ones a human reads. Design:
`docs/superpowers/specs/2026-08-28-access-page-definition.md`.
"""

from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def page(seeded_app):
    r = seeded_app["client"].get("/admin/access", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200, r.text
    return r.text


class TestTheTierSaysWhatItDoes:
    def test_the_wire_words_are_unchanged(self, page):
        """`available` / `required` are persisted in `resource_grants`.
        Only the labels move; renaming these would be a data migration."""
        assert 'data-tier="available"' in page
        assert 'data-tier="required"' in page

    def test_the_labels_are_the_library_s_words(self, page):
        assert ">Available<" in page
        assert ">Required<" in page

    @pytest.mark.parametrize("retired", ["Optional", "Automatic"])
    def test_the_retired_labels_are_gone(self, page, retired):
        """They named neither state and matched nothing the person sees."""
        assert f">{retired}<" not in page

    def test_the_tooltip_stops_calling_it_an_access_control(self, page):
        """The grant is already the membership — `stack_auto_membership` is
        default-on since Wave 0 — so the tier only decides the copy."""
        assert "The grant already gave access" in page

    def test_the_tooltip_still_states_admin_god_mode(self, page):
        """A page about access that does not say admins bypass it invites an
        admin to conclude their grants are what let them in."""
        assert "Admins can always reach everything" in page


class TestEveryRowSaysWhatThePersonReads:
    """The admin sets a tier while looking at the sentence it writes in
    someone else's Library. That column is the only place the two halves of
    this vocabulary can be kept in step."""

    @pytest.mark.parametrize(
        "sentence",
        [
            "Required by your admin",
            "Keep a local copy",
            "Install",
            "Ask Agnes",
            "Open",
            "Use as template",
        ],
    )
    def test_the_library_sentences_are_present(self, page, sentence):
        assert sentence in page

    def test_a_person_with_no_control_is_said_so_not_left_blank(self, page):
        """A grant-scoped document has no control at all: the person's agent
        can cite it, and there is nothing for them to press. Blank would read
        as a missing feature."""
        assert "citable" in page
        assert "not granted" in page


class TestNoSurfaceKeepsTheOldWordsInSource:
    """The rendered page is not the whole page.

    Simulate's Library-preview chips are built in JS from a fetch, so they
    never appear in the first byte and a test that reads the response body
    cannot see them — which is exactly how they kept saying *In stack ·
    Automatic* while the control above them had been renamed. This one reads
    the template source instead, and is the reason the drift was found at
    all.
    """

    TEMPLATE = "app/web/templates/admin_access.html"

    def _source(self) -> str:
        from pathlib import Path

        return Path(self.TEMPLATE).read_text(encoding="utf-8")

    @pytest.mark.parametrize("retired", ["In stack · Automatic", "Not in stack yet · Optional"])
    def test_retired_chip_labels_are_gone(self, retired):
        assert retired not in self._source()

    def test_the_tier_control_has_exactly_one_definition(self):
        """Three surfaces render this control — the group rows, the bundle
        rows, and Advanced. Each keeping its own copy of the labels is
        precisely how one control ended up with three names; this pins it to
        a single `tierControl` definition, so a rename cannot land on two of
        the three."""
        src = self._source()
        assert src.count('data-tier="available"') == 1
        assert src.count('data-tier="required"') == 1
        assert src.count("const tierControl") == 1
        # The definition is `const tierControl = (…) =>`, so it does not
        # match `tierControl(` — this counts call sites only. Two now: the
        # group's own rows and the bundle view's. Advanced was the third and
        # was removed with the browsing tree.
        assert src.count("tierControl(") == 2

    def test_simulate_speaks_the_person_s_words(self):
        src = self._source()
        assert "Required by your admin" in src
        assert "In their Library" in src
