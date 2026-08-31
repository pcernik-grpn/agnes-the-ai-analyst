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

from app.web import vocabulary


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

    def test_the_labels_are_automatic_and_optional(self, page):
        """Reversed from Required / Available on 2026-08-30 (TCRD-208; the
        reversal note is section 2 of the access-page spec).

        The rule did not change — the Library's words win — but the Library's
        words moved: *In stack* / *Add to stack* became **Keep a local copy**,
        so "Required by your admin" stopped being the sentence this control
        mirrors. What was left was *Available*'s own flaw, which the first pass
        did not weigh: BOTH tiers are available. Both are granted, both are
        reachable, both are queryable the moment the grant exists — the
        tooltip two tests down says exactly that. Offering *Available* as the
        opposite of *Required* draws a distinction the system does not make.
        """
        assert ">{}<".format(vocabulary.TIER_AUTOMATIC) in page
        assert ">{}<".format(vocabulary.TIER_OPTIONAL) in page

    @pytest.mark.parametrize("retired", ["Available"])
    def test_the_retired_label_is_gone(self, page, retired):
        """`>Required<` is deliberately NOT checked here. A memory item's
        `is_required` means *required reading* — the agent must always load it
        — a different axis that shares only the word, and one this page may
        legitimately name. Banning the substring would have renamed those
        badges too and said something false about downloads."""
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

    @pytest.mark.parametrize("retired", ["· Optional —", "· Automatic —"])
    def test_the_retired_words_are_gone_from_prose_too(self, retired):
        """The element-shaped sweep above missed these for months.

        The person lens wrote its tier as a chip SUFFIX — "· Automatic — in
        their stack" — which is prose, not a label, so the `>Automatic<`
        check never saw it. The result was one Required package described
        two ways on one screen: "Automatic" in the chip and "Required by
        your admin" in the Library panel directly beneath it. Same
        vocabulary, matched in the shape it actually appears in.
        """
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
        # match `tierControl(` — this counts CALL SITES only. There is now
        # exactly ONE: neither lens renders the tier pair directly any more.
        # Both go through `controlCell`, the whole ADMIN CONTROL cell — tier
        # pair plus Revoke — so a grant of any kind can be undone from the
        # page that made it. Holding this at one is what stops a lens quietly
        # growing its own copy of the labels again.
        assert src.count("tierControl(") == 1
        assert src.count("const controlCell") == 1
        assert src.count("controlCell(") == 2      # the group lens and the bundle lens

    def test_a_grant_of_any_kind_can_be_revoked(self):
        """A control that grants and cannot revoke is a one-way door.

        The tiered kinds — data package, memory domain, marketplace plugin —
        used to render the Available/Required pair INSTEAD of a Revoke, so
        those three could be granted from this page and never un-granted, in
        either lens. An admin had to reach for the API to undo a click. The
        cell carries both now, for every kind, which is also why there is a
        single `controlCell` for the guard above to count.
        """
        src = self._source()
        start = src.index("const controlCell")
        cell = src[start:start + 1800]
        assert "data-revoke" in cell, "the shared control cell must carry Revoke"
        assert "tierControl(" in cell, "…and the tier pair, so both live in one place"

    def test_simulate_speaks_the_person_s_words(self):
        src = self._source()
        assert "Required by your admin" in src
        assert "In their Library" in src


class TestRowsAndTheirHandlerAgree:
    """A row is identified by `data-rid`, never by its tag.

    The group view's rows were `<tr>` until the table was flattened into a
    CSS grid, and the click handler kept matching `tr[data-rid]` — so
    Available / Required and Revoke silently did nothing on the one surface
    people actually use, while continuing to work in the bundle view, whose
    rows are still `<tr>`. Nothing failed and nothing logged; the control
    just had no effect.
    """

    TEMPLATE = "app/web/templates/admin_access.html"

    def _source(self) -> str:
        from pathlib import Path

        return Path(self.TEMPLATE).read_text(encoding="utf-8")

    def test_the_row_handler_is_not_tag_specific(self):
        src = self._source()
        assert 'closest("tr[data-rid]")' not in src
        assert 'closest("[data-rid]")' in src

    def test_both_views_emit_the_same_row(self):
        """Both views render the SAME row element now.

        By group and By bundle sat on one page looking like two products —
        one a collapsible row with counts, the other a permanently-open block
        with a table under it. They share `.ax-r` and `.ax-gs`, so switching
        changes what the list is about, not what a list is. The bundle view's
        `<tr>`s are gone, which is also why the tag-specific handler could
        never have been caught by using that view.
        """
        src = self._source()
        # Rows now carry their kind too (the keyboard rail wears it), so the
        # attribute order differs — match on the class plus the row id.
        assert src.count('<div class="ax-r" data-kind=') == 2   # one per view
        assert "<tr data-type=" not in src
        assert 'class="ax-gs ax-gs--bb' in src             # a bundle is a group-shaped row


class TestActingOnARowDoesNotCloseIt:
    """A write from inside an open row must not shut the row.

    The group view survives a repaint because the selected group is state and
    is re-emitted with `open`. The bundle view had no equivalent: every
    repaint rebuilt the list closed, so changing a tier or revoking from
    inside an open bundle shut the thing you were working in, immediately
    after acting on it — the one moment you are most certain to still be
    looking at it.
    """

    TEMPLATE = "app/web/templates/admin_access.html"

    def _source(self) -> str:
        from pathlib import Path

        return Path(self.TEMPLATE).read_text(encoding="utf-8")

    def test_open_bundles_are_remembered(self):
        src = self._source()
        assert "const openBundles = new Set()" in src
        assert "openBundles.has(bkey)" in src        # re-emitted open on render
        assert "openBundles.add(key)" in src         # …and recorded on toggle
        assert "openBundles.delete(key)" in src

    def test_the_group_view_re_emits_its_open_group(self):
        """The same property, by a different mechanism: `selectedGroup`."""
        src = self._source()
        assert "const open = selectedGroup === g.id;" in src
        assert '${open ? "open" : ""}' in src
