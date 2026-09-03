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

import re

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
        once rendered the Available/Required pair INSTEAD of a Revoke, so those
        three could be granted from this page and never un-granted. An admin
        had to reach for the API to undo a click.

        The two halves now live in two CELLS rather than one: the tier pair is
        the "Access tier" column (`controlCell`) and the act is the "Manage"
        column (`manageCell`), which is what the retired "What they will see"
        column became. So this reads both, and additionally pins the ONE
        deliberate exception: where Revoke is withheld — a grant another
        surface re-asserts, which the API refuses to delete anyway — the cell
        must hand over the way to the surface that CAN undo it. Withholding
        the control silently would be the one-way door this guard exists to
        prevent, just wearing a different face.
        """
        src = self._source()
        tier_cell = src[src.index("const controlCell"):][:1800]
        assert "tierControl(" in tier_cell, "the tier cell must carry the tier pair"

        manage_cell = src[src.index("const manageCell"):][:1800]
        assert "data-revoke" in manage_cell, "the manage cell must carry Revoke"
        # No dead ends: every branch that returns without a Revoke returns a
        # link instead. Both non-revocable branches key off `.href`.
        assert manage_cell.count("data-revoke") >= 2, (
            "Revoke must survive on the ordinary grant AND on a seeded default"
        )
        assert "href" in manage_cell, (
            "a grant this page cannot revoke must still point at the surface that can"
        )

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
        #
        # Matched as an ELEMENT, not as one exact literal. The invariant is
        # that both views emit the same row element — a `div.ax-r` carrying
        # `data-kind` — so the delegated `[data-rid]` handler reaches rows in
        # either view. It says nothing about the class list being a single
        # bare token, and the bundle view now appends a modifier
        # (`ax-r--scope`, marking an audience that is a scope rather than a
        # group). Pinning the literal forbade a modifier the invariant does
        # not care about, which is a guard failing on something it was not
        # written to protect.
        row_element = re.compile(r'<div class="ax-r(?:\$\{[^{}]*\}|[^">])*"[^>]*?data-kind=')
        assert len(row_element.findall(src)) == 2   # one per view
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


class TestAnEveryoneAudienceIsNotARoster:
    """An everyone-scoped grant must not be reported as a group with members.

    The grant is STORED against the seeded `Everyone` group as its carrier
    (the column is NOT NULL, and the unique key on it is what keeps one
    everyone-grant per resource), so `group_id` still names a group on the
    wire. Rendering it as one produced two false statements on the page:
    the audience row read "Everyone · 41 people", and the collapsed line
    read "Everyone · 41 people" above it.

    Both invite the same two wrong conclusions — that a roster decides this,
    and that a member leaving the group would change it. Neither is true:
    the audience is every account, and anyone who joins later. In the
    collapsed line the count is not merely mis-attributed but the wrong
    QUANTITY, because an everyone grant dominates every other group on the
    line rather than adding to it.

    `audience` is the server's own answer (`/api/admin/access-overview`),
    computed with `reaches_everyone`, so it is correct on the frozen DuckDB
    ladder too — where there is no `scope` column and the carrier match is
    the only available signal.
    """

    TEMPLATE = "app/web/templates/admin_access.html"

    def _source(self) -> str:
        from pathlib import Path

        return Path(self.TEMPLATE).read_text(encoding="utf-8")

    def test_both_renderers_branch_on_the_server_s_answer(self):
        src = self._source()
        # The audience row, and the collapsed line above it.
        assert 'grant.audience === "everyone"' in src
        assert '(g) => g.audience === "everyone"' in src

    def _group_row(self) -> str:
        """Just the audience-row renderer.

        Scoped deliberately: a member count is CORRECT in several other
        renderers on this page — the group picker lists real groups and
        their rosters — so a whole-file assertion would fail on code that is
        right, which is the guard being wrong rather than the page.
        """
        src = self._source()
        start = src.index("const groupRow = (r, grant) => {")
        return src[start : src.index("\n    };", start)]

    def test_the_audience_row_does_not_count_before_it_knows_the_kind(self):
        row = self._group_row()
        # It used to take the count first, unconditionally, and had no way to
        # not print it. The kind of audience must be established first.
        assert "const n = g.member_count ?? 0;" not in row
        assert "member_count" in row  # the ordinary-group branch still counts
        assert row.index("isEveryone") < row.index("member_count")

    def test_the_page_says_what_an_everyone_audience_reaches(self):
        src = self._source()
        assert "every account, and anyone who joins" in src   # the audience row
        assert "everyone, and anyone who joins" in src        # the collapsed line
        assert "`every account · ${grants} granted`" in src   # the By group row

    def test_all_three_renderers_are_covered(self):
        """Three renderers made the same claim, in three views.

        By group listed Everyone with a member count, By resource's audience
        row did the same, and By resource's collapsed line said it a third
        time. Fixing one and not the others is the failure mode worth a test:
        each was written at a different time and none of them knew about the
        others.
        """
        src = self._source()
        # By group branches on the flag already on the payload; the two in By
        # resource branch on the server's `audience` field.
        assert "g.is_everyone" in src
        assert 'grant.audience === "everyone"' in src
        assert '(g) => g.audience === "everyone"' in src

    def test_the_row_still_says_people_for_an_ordinary_group(self):
        """The fix must not cost the ordinary case its member count."""
        src = self._source()
        assert '=== 1 ? "person" : "people"' in src


class TestTheGrantListSplitsOnWhatCanBeActedOn:
    """Two sections, named for where the ACTION lives.

    A group's grant list was one flat list mixing rows an admin can revoke
    with rows a revoke cannot remove — a `marketplace_sync` row that comes
    back tonight, a via-Everyone row whose tier is set on Everyone. Nothing
    on the list said which was which except the absence of a Revoke button,
    which reads as a bug rather than a rule.

    The axis is "can the admin act on this row, here" rather than "who
    wrote it" (`src/grant_sources.py::section_for`, keyed on `revocable`):
    nine writers are not the admin but only two produce rows that
    re-assert, so grouping by authorship would file the Library shares an
    admin most often comes here to check under "not yours".
    """

    TEMPLATE = "app/web/templates/admin_access.html"

    def _source(self) -> str:
        from pathlib import Path

        return Path(self.TEMPLATE).read_text(encoding="utf-8")

    def test_the_two_sections_are_named_for_the_action(self):
        src = self._source()
        assert 'sectionLabel("Change here")' in src
        assert 'sectionLabel("Set elsewhere",' in src

    def test_an_inherited_row_is_set_elsewhere(self):
        """The one case the server's `section` field cannot describe.

        A via-Everyone row is a client-side projection of Everyone's own
        grant, so it carries no source of its own — and it fails the axis,
        because the act belongs on Everyone.
        """
        src = self._source()
        assert 'grant.inherited ? "set_elsewhere" : (grant.section || "change_here")' in src

    def test_a_lone_section_is_not_labelled(self):
        """A heading over every row there is categorises nothing.

        The same rule the group list applies to GROUPS / SYSTEM. It holds in
        both directions: an unactionable row is explained by its own Manage
        cell (`via Everyone →`), on the row, not by a band above it.
        """
        src = self._source()
        assert "(changeHere && setElsewhere)" in src

    def test_one_vocabulary_for_the_actionable_side(self):
        """The section and the row's own reason must not use two phrasings."""
        from pathlib import Path

        sources = Path("src/grant_sources.py").read_text(encoding="utf-8")
        assert "Yours to change" not in sources
        assert "Change it here." in sources


class TestGivingSomethingToEveryoneIsAnExplicitChoice:
    """Decision 04: reaching every account is an act, not a membership fact.

    Before, an admin reached everybody two ways, neither of them a visible
    decision: by granting to a group that happened to hold every account, or
    by marking a plugin "system" on a different page. The audience picker now
    offers the scope beside the groups, and picking it writes `scope` on the
    row.

    The carrier is filtered out as a GROUP in the same move. An everyone
    grant is stored against the seeded group either way — the column is NOT
    NULL, and the unique key on it is what keeps one everyone-grant per
    resource — so offering both would let an admin pick "everyone" and "the
    Everyone group" as if they were different audiences, and then collide on
    that key.
    """

    TEMPLATE = "app/web/templates/admin_access.html"

    def _source(self) -> str:
        from pathlib import Path

        return Path(self.TEMPLATE).read_text(encoding="utf-8")

    def test_the_scope_is_offered_and_the_carrier_is_not(self):
        src = self._source()
        assert 'EVERYONE_AUDIENCE = { id: "everyone"' in src
        # The candidate list drops the carrier as a group...
        assert "if (g.is_everyone) return false;" in src
        # ...and re-offers it as the scope.
        assert "offerScope ? [EVERYONE_AUDIENCE, ...groups] : groups" in src

    def test_the_scope_is_withheld_where_a_grant_does_not_mean_audience(self):
        """Decision 07's four types, which the server also refuses with 422.

        The page must not offer a choice the API would reject — an admin who
        picks it and gets an error learns nothing about why.
        """
        src = self._source()
        assert "SCOPE_WITHHELD_TYPES = new Set([" in src
        for t in ("slack_channel", "table", "memory_domain", "memory_item"):
            assert f'"{t}"' in src
        assert "!SCOPE_WITHHELD_TYPES.has(b.type)" in src

    def test_it_is_not_offered_twice_for_the_same_resource(self):
        """The unique key allows one everyone-grant per resource."""
        src = self._source()
        assert "const held = everyoneHeld(b.type, b.id);" in src
        assert "&& !held" in src

    def test_picking_it_writes_a_scope_not_a_group(self):
        src = self._source()
        # The tier argument became the admin's answer (see
        # TestThePickerAsksAboutTheTierInsteadOfDeciding); what this test
        # pins is the SCOPE argument beside it.
        assert 'await writeGrant(type, rid, tier, gid, asScope ? "everyone" : undefined);' in src
        assert "...(scope ? { scope } : {})" in src

    def test_the_optimistic_row_carries_its_audience(self):
        """Or the renderers make the false claim again until the next repaint.

        `overview.grants` is appended locally after a successful POST so the
        page repaints without a refetch. A row missing `audience` is read as
        an ordinary group grant by all three renderers that were fixed to
        stop counting a scope as a roster.
        """
        src = self._source()
        assert 'audience: scope === "everyone" ? "everyone" : gid,' in src

    def test_the_sentinel_counts_as_every_account(self):
        """It is not in `overview.groups`, so a lookup silently drops it.

        Dropped, the picker's reach line counts only the real groups chosen
        alongside it — reporting everyone as fewer people than a two-person
        team.
        """
        src = self._source()
        assert "if ((groupIds || []).includes(EVERYONE_AUDIENCE.id)) {" in src


class TestEveryoneIsNotInTheGroupList:
    """Decision 04's other half: the list holds groups someone made.

    The carrier stays in `overview.groups` — every id lookup on the page
    resolves through it, and the grant it carries is stored against it — but
    it is not offered as a row among real groups. A group named Everyone
    invites the assumption it can be narrowed like any other, which is the
    ambiguity the scope exists to remove.

    It gets its own entry above the list instead, carrying the carrier's id
    in `data-gs` / `data-gsbody` so the existing machinery works untouched:
    the disclosure handler sets the selection from `data-gs` like any row,
    and the single Access panel is moved into `data-gsbody`. Selecting it
    lists exactly the grants that reach every account, with their real tier
    control and Revoke — which is how an admin sees and changes what has
    been given to everyone — without a second rendering path to keep in step
    with the first.
    """

    TEMPLATE = "app/web/templates/admin_access.html"

    def _source(self) -> str:
        from pathlib import Path

        return Path(self.TEMPLATE).read_text(encoding="utf-8")

    def test_the_carrier_is_filtered_out_of_the_list(self):
        src = self._source()
        assert "(overview.groups || []).filter((g) => !g.is_everyone).slice().sort(" in src

    def test_it_reuses_the_selection_and_panel_machinery(self):
        """A second rendering path is the thing this avoids.

        Both attributes are required: `data-gs` is what the disclosure
        handler reads to set the selection, `data-gsbody` is where
        `homeWork` moves the one Access panel. With either missing the entry
        opens onto nothing.
        """
        src = self._source()
        assert '<details class="ax-gs ax-gs--scope" data-gs="${esc(cid)}"' in src
        assert '<div class="ax-gs__body" data-gsbody="${esc(cid)}"></div>' in src

    def test_the_entry_carries_no_roster(self):
        src = self._source()
        entry = src[src.index("const everyoneEntry = cid"):]
        entry = entry[: entry.index("host.innerHTML")]
        assert "member_count" not in entry
        assert "data-gmenu" not in entry     # no rename/delete: it is neither
        assert "every account ·" in entry

    def test_the_copy_beside_it_does_not_say_group(self):
        """Three strings in the grant list name their subject.

        Saying "group" over the everyone entry is the same category error the
        row treatments were fixed for, one level up: it tells the reader the
        thing they are editing has members. A real group must keep the
        group wording.
        """
        src = self._source()
        assert "const scopeSelected = !!selectedGroup && selectedGroup === everyoneGroupId();" in src
        assert '${scopeSelected ? "Add for everyone" : "Add to this group"}' in src
        assert '${scopeSelected ? "What every account gets" : "What the group gets"}' in src


class TestThePickerAsksAboutTheTierInsteadOfDeciding:
    """The tier decides whether anyone actually RECEIVES the thing.

    Optional leaves it for a person to take; Automatic puts it in every
    workspace in the audience. Both pickers chose that silently — they wrote
    `available` and mentioned it in grey in the subtitle ("Anything with a
    tier is added as Available") — so the one field with a real consequence
    was set by the surface on the admin's behalf, and the sentence announcing
    it was also false on the twelve kinds that have no tier at all.

    It is a question in the footer now, at the far left, away from
    Cancel/Apply so it does not read as a third action. Shown only when the
    selection contains something the tier can act on: a control that cannot
    act is what this effort keeps removing.
    """

    TEMPLATE = "app/web/templates/admin_access.html"

    def _source(self) -> str:
        from pathlib import Path

        return Path(self.TEMPLATE).read_text(encoding="utf-8")

    def test_the_footer_carries_the_question(self):
        src = self._source()
        assert 'data-pk="tier"' in src
        assert 'data-pk-tier="available"' in src
        assert 'data-pk-tier="required"' in src

    def test_the_answer_is_honoured_on_apply(self):
        src = self._source()
        assert 'const tier = TIERED.has(type) ? (pickerState.tier || "available") : "available";' in src
        assert 'await writeGrant(type, rid, tier, gid, asScope ? "everyone" : undefined);' in src

    def test_it_is_shown_only_where_the_tier_can_act(self):
        """Both modes, and they ask it differently.

        In bundle mode the subject is one resource and the choices are
        audiences, so whether the tier applies is a property of that
        resource. In resources mode the subject is a group and the choices
        are resources of every kind, so it applies as soon as one tiered
        kind is ticked — and stops applying if it is unticked again.
        """
        src = self._source()
        assert "function paintPickerTier() {" in src
        assert "? (chosen.length > 0 && TIERED.has(pickerState.bundle.type))" in src
        assert ": chosen.some((k) => TIERED.has(k.slice(0, k.indexOf(\":\"))));" in src

    def test_neither_subtitle_still_announces_the_answer(self):
        """Checked on the ASSIGNMENTS, not on the whole file.

        Both strings still appear in the file, inside the comments that
        record why they went — which is worth keeping, and is not the page
        saying them. A guard that cannot tell prose about a string from the
        string being used fires on its own documentation.
        """
        import re

        src = self._source()
        assigns = re.findall(r"els\.sub\.textContent\s*=[^;]*;", src, re.S)
        assert assigns, "the subtitle assignments moved; this guard needs rewriting"
        joined = "\n".join(assigns)
        assert "added as Available" not in joined
        assert "Added as Available" not in joined

    def test_the_choice_resets_with_the_rest_of_the_state(self):
        """Every field, every time — a literal that forgets one leaves the
        painter reading a stale answer into the next write."""
        src = self._source()
        assert src.count('tier: "available",') == 2   # one per picker mode


class TestTheAddControlsAreButtons:
    """Five `.ax-add` blocks, and the width kept coming back.

    The action was full-width because a late block still said
    `display: grid` + `grid-template-columns: inherit` + `width: 100%`,
    left from when it literally was a row in the table's column grid. It
    silently undid the base rule several hundred lines above — a cascade
    collision, not a missed rule.

    Fixed at both origins, plus one consolidating rule stated last so no
    later block can undo it again. Filled and bordered, the control has
    nothing left to align to, so riding the table's columns bought nothing.
    """

    TEMPLATE = "app/web/templates/admin_access.html"

    def _source(self) -> str:
        from pathlib import Path

        return Path(self.TEMPLATE).read_text(encoding="utf-8")

    def test_no_add_rule_sets_a_full_width(self):
        """The regression is textual and it has happened twice."""
        import re

        src = self._source()
        for m in re.finditer(r"\.ax-(?:add|newrow)[^{}]*\{([^{}]*)\}", src):
            assert "width: 100%" not in m.group(1), m.group(0)[:120]

    def test_the_grid_that_originated_it_is_gone(self):
        src = self._source()
        assert "grid-template-columns: inherit;\n    gap: inherit;" not in src

    def test_the_last_width_declaration_wins_as_auto(self):
        """The cascade's answer, not any single rule's.

        Asserting that ONE rule has the last word was the wrong shape: two
        rules legitimately share these selectors — the tile (display, width,
        padding) and a later one carrying only box-sizing and margin. What
        matters is what the cascade resolves to, which is what broke twice:
        a later block said `100%` and the tile set a hundred lines above
        lost, so the width appeared to keep coming back on its own.
        """
        import re

        src = self._source()
        widths = []
        for m in re.finditer(r"(\.ax-(?:add|newrow)[^{}]*)\{([^{}]*)\}", src):
            selector, body = m.group(1), m.group(2)
            if "__" in selector or ":hover" in selector or ":focus" in selector:
                continue
            for w in re.finditer(r"(?<!max-)(?<!min-)width:\s*([^;]+);", body):
                widths.append((selector.strip(), w.group(1).strip()))
        assert widths, "no width declarations found; this guard needs rewriting"
        assert widths[-1][1] == "auto", f"last word is {widths[-1]}"
        # And nothing earlier fights it, so the next edit here does not have
        # to know the cascade order to be safe.
        assert all(v == "auto" for _, v in widths), widths
