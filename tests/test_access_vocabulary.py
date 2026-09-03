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
from tests.helpers.access_page import access_js, access_page_source


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def page(seeded_app):
    """What a browser ends up with: the rendered page AND the module it loads.

    The script is a static asset now, so the response body alone no longer
    contains the words these tests are about. Fetching the page is still the
    part that matters — it proves the route renders and that the vocabulary
    reached the boot blob — and the module is appended because that is where
    the same browser reads the rest of them from.
    """
    r = seeded_app["client"].get("/admin/access", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200, r.text
    return f"{r.text}\n{access_js()}"


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


    def _source(self) -> str:

        return access_page_source()

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
        # Sliced to the FUNCTION, not to a character count. A 1800-char window
        # was long enough until the cell grew a comment explaining why twelve
        # kinds render nothing there, at which point the call it looks for
        # sat past the window and this failed on code that still carried the
        # pair. A guard should stop reading where the function does.
        c0 = src.index("const controlCell")
        tier_cell = src[c0 : src.index("\n  };", c0)]
        assert "tierControl(" in tier_cell, "the tier cell must carry the tier pair"

        m0 = src.index("const manageCell")
        manage_cell = src[m0 : src.index("\n  };", m0)]
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


    def _source(self) -> str:

        return access_page_source()

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


    def _source(self) -> str:

        return access_page_source()

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


    def _source(self) -> str:

        return access_page_source()

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


    def _source(self) -> str:

        return access_page_source()

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


    def _source(self) -> str:

        return access_page_source()

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
        # The optimistic row is now the SERVER's row made renderable
        # (`_rowFromResponse`, audit E2), so the audience is derived there —
        # from the row's own scope, or from the carrier group it was stored
        # against — rather than hand-built in writeGrant. Same guarantee,
        # one home.
        assert "overview.grants.push(_rowFromResponse(created));" in src
        f = src[src.index("function _rowFromResponse(g)"):]
        f = f[: f.index("\n  }", 0) + 4]
        assert 'audience: g.audience ?? ((g.scope === "everyone" || (cid && g.group_id === cid)) ? "everyone" : g.group_id),' in f

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


    def _source(self) -> str:

        return access_page_source()

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


    def _source(self) -> str:

        return access_page_source()

    def test_the_footer_carries_the_question(self):
        src = self._source()
        assert 'data-pk="tier"' in src
        assert 'data-pk-tier="available"' in src
        assert 'data-pk-tier="required"' in src

    def test_the_answer_is_honoured_on_apply(self):
        src = self._source()
        # The footer's answer is what gets written for a tiered kind. The line
        # grew a `&& !userSkill` clause when store_entity joined TIERED (a
        # user-published skill has one legal tier, whatever the footer says —
        # see TestAStoreEntityIsATieredKind), so this pins the invariant
        # rather than the whole expression.
        assert '? (pickerState.tier || "available") : "available";' in src
        assert "TIERED.has(type) && !userSkill" in src
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


    def _source(self) -> str:

        return access_page_source()

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


class TestATierControlIsDrawnOnlyWhereItCanAct:
    """Render a control where it can act; say nothing where it cannot.

    Audit findings U5 and F8. Twelve of the sixteen grantable kinds have no
    Optional/Automatic choice, and the page drew the pair on all of them —
    disabled, with the reason in a tooltip — to keep the column uniform.
    Uniformity bought with a dead control teaches the reader that the page
    cannot be trusted about which of its controls work, and it disagreed with
    itself: a table granted to everyone printed "Optional · to everyone" one
    row above a table showing the tier greyed out.

    A store entity is the subtler case: it IS tiered, but Automatic is
    admissible only when the organization is the publisher, and the API
    refuses anything else with a 422. Drawing the pair on a user-published
    row offers a choice that fails on click. Optional is the one legal value
    and already the value, so it is stated as a fact with its reason, not
    offered as a one-button control.
    """


    def _source(self) -> str:

        return access_page_source()

    def _control_cell(self) -> str:
        src = self._source()
        start = src.index("const controlCell = (typeKey, tier, opts) => {")
        return src[start : src.index("\n  };", start)]

    def test_an_untiered_kind_renders_nothing(self):
        cell = self._control_cell()
        assert '? ""' in cell, "an untiered kind must render an empty control, not a greyed pair"
        # The disabled pair is gone from the page entirely, not merely from
        # this branch: nothing else may draw a control that cannot act.
        assert 'aria-disabled="true" role="group"' not in self._source()
        assert "not applicable to this kind" not in self._source()

    def test_a_user_published_store_entity_states_its_tier_rather_than_offering_it(self):
        cell = self._control_cell()
        assert 'typeKey === "store_entity" && !orgPublished' in cell
        assert "Automatic needs an organization-published item" in cell
        # The reason is the server's own rule, so the page and the 422 agree.
        assert "publish it as the organization first" in cell

    def test_an_unknown_publisher_is_treated_as_a_user(self):
        """The server defaults `publisher_kind` to "user"; so must the page.

        An item whose publisher is unknown must not be granted the wider
        permission by omission — the same direction the server errs in.
        """
        cell = self._control_cell()
        assert '(o.publisherKind || "user") === "organization"' in cell

    def test_both_row_renderers_pass_the_publisher_through(self):
        """The rule lives in one function, so both call sites must feed it.

        Written at different times, neither renderer knew about the other —
        the same way three renderers came to make one false claim about
        Everyone. Pinning both call sites is what stops that recurring here.
        """
        src = self._source()
        assert src.count("publisherKind: i.publisher_kind") == 1      # the group's grant list
        assert src.count("publisherKind: r.i.publisher_kind") == 1    # By resource's audience row


class TestTheNameColumnHasAFloor:
    """Audit V1. The By resource name track was `minmax(0, 1fr)` — the ONLY
    flexible column, so on a narrow pane it absorbed the entire shortfall
    and measured 41px, clipping every audience name to two letters. The
    reach column yields first now, and its text wraps rather than truncates.
    """


    def _source(self) -> str:

        return access_page_source()

    def test_the_by_resource_name_track_has_a_minimum(self):
        src = self._source()
        rule = src[src.index(".ax-gs--bb .ax-colhd,\n  .ax-gs--bb .ax-r {"):]
        rule = rule[: rule.index("}")]
        assert "minmax(7rem, 1fr)" in rule, "the name track must not be allowed to collapse to zero"
        assert "minmax(6rem, 10rem)" in rule, "the reach track is the one that yields"

    def test_the_reach_text_wraps_instead_of_truncating(self):
        assert ".ax-gs--bb .ax-r .ax-r__d { white-space: normal; overflow: visible; }" in self._source()


class TestThePersonTabSaysWhatKindOfTabItIs:
    """Audit I3. By group and By resource are one list pivoted two ways; By
    person is a simulator. Presented as three peers, the page's most
    distinctive capability read as a third sort order. The decision was to
    keep it in the strip — demoting it would trade discoverability for a
    tidier model — so the strip says what makes it a different kind.
    """


    def _source(self) -> str:

        return access_page_source()

    def test_the_person_tab_is_still_a_tab(self):
        """It stays in the strip, with the same role and pane wiring."""
        src = self._source()
        tab = src[src.index('id="ax-tab-person"') - 40 :][:400]
        assert 'role="tab"' in tab and 'aria-controls="ax-pane-sim"' in tab

    def test_and_says_what_it_does(self):
        src = self._source()
        assert '<span class="ax-by__kind">view as someone</span>' in src
        assert 'title="Pick a person and see the product as they see it"' in src


class TestTheAdminGroupNamesTheModeItsGrantsDependOn:
    """Audit F7, reduced. As written the finding had an admin pause their
    elevation and then edit grants — but a paused admin is not an admin
    (`is_admin` is False) and every /admin page answers 403, this one
    included. What is left: the Admin group's grants are inert in the mode
    an admin is in while reading this page and load-bearing in the one they
    cannot be in while reading it, and nothing said so. One sentence where
    the tier is chosen; not a banner.
    """


    def _source(self) -> str:

        return access_page_source()

    def test_the_access_sub_line_is_addressable(self):
        assert 'id="ax-access-sub"' in self._source()

    def test_the_admin_group_gets_the_mode_sentence_and_nobody_else_does(self):
        src = self._source()
        assert "What admins can use with Admin mode paused." in src
        # Keyed on the same test addMember uses for the god-mode confirm, so
        # the two surfaces cannot disagree about which group is Admin.
        assert '(_selGrp.is_admin === true || _selGrp.name === "Admin")' in src
        # The other two audiences keep their own sentences.
        assert '"What every account can use."' in src
        assert '"What everyone above can use."' in src

    def test_the_route_really_does_lock_a_paused_admin_out(self):
        """The reduction rests on this. If /admin/access ever stops being
        admin-gated, the original scenario becomes reachable and a sentence
        is no longer enough."""
        from pathlib import Path

        router = Path("app/web/router.py").read_text(encoding="utf-8")
        i = router.index('@router.get("/admin/access"')
        assert "Depends(require_admin)" in router[i : i + 300]


class TestInheritedRowsCollapseToOneLine:
    """Audit S3. Every everyone-wide grant appeared in every group's list as
    its own row, each labelled with the reason it was there — a production
    screenshot read "5 granted · 86 via Everyone" over page after page of rows
    the group did not hold. The explanation had become the noise, and what
    the group itself holds — the reason the admin opened it — was buried
    under what everyone holds.

    They are one line now, at the end of Set elsewhere, pointing at the
    Everyone audience where they can actually be acted on.
    """


    def _source(self) -> str:

        return access_page_source()

    def _grant_list(self) -> str:
        src = self._source()
        start = src.index("let inheritedN = 0;")
        return src[start : src.index("const sections = (changeHere && setElsewhere)", start)]

    def test_an_inherited_grant_is_counted_not_rendered(self):
        body = self._grant_list()
        assert "if (grant.inherited) { inheritedN += 1; continue; }" in body
        # And the count is taken AFTER the kind and search filters, so the
        # summary never claims rows a filter hid.
        assert body.index("if (!hits(t, i)) continue;") < body.index("if (grant.inherited)")

    def test_the_summary_line_points_where_they_can_be_changed(self):
        body = self._grant_list()
        assert 'data-inherited-summary="${inheritedN}"' in body
        assert "and everything Everyone has" in body
        # The same deep link the per-row `via Everyone →` used.
        assert 'href="?group=${esc(everyoneGroupId() || "")}">Everyone →</a>' in body

    def test_it_lands_in_set_elsewhere(self):
        """It is a fact about rows the admin cannot act on here."""
        body = self._grant_list()
        assert 'const setElsewhere = inSection("set_elsewhere") + inheritedLine;' in body

    def test_the_everyone_entry_never_shows_it(self):
        """Zero by construction: `grantsFor` returns only direct rows for the
        carrier, so nothing is inherited there to summarise."""
        src = self._source()
        assert "if (!evId || groupId === evId) return direct;" in src


class TestReachIsTheServersNumber:
    """Audit E3. The picker's "N groups · M people" is read at the moment of
    deciding to share, and it was computed in the browser by unioning member
    ids — with a fallback to a group's own count when its roster was absent,
    which double-counted anyone in two such groups, and a clamp to the
    account total to hide the overshoot. A figure precise enough to be
    trusted and not always right.

    The server answers now (`GET /api/admin/groups/reach`), where the
    memberships are. The local estimate still paints first so the footer
    never blanks, and it does not get the last word.
    """


    def _source(self) -> str:

        return access_page_source()

    def _picker_footer(self) -> str:
        src = self._source()
        start = src.index("const chosen = [...pickerState.chosen];")
        return src[start : src.index("els.apply.disabled = !n;", start)]

    def test_the_estimate_paints_first_and_the_server_paints_last(self):
        foot = self._picker_footer()
        assert 'els.count.textContent = n ? line(reachOf(chosen)) : "No group selected";' in foot
        assert "fetchReach(chosen).then((count) =>" in foot
        assert foot.index("line(reachOf(chosen))") < foot.index("fetchReach(chosen)")

    def test_a_stale_answer_is_dropped(self):
        """The selection can change while a request is in flight."""
        foot = self._picker_footer()
        assert 'if ([...pickerState.chosen].sort().join(",") !== asked) return;' in foot

    def test_an_unreachable_server_leaves_the_estimate(self):
        src = self._source()
        assert "if (count == null) return;" in src
        assert ".catch(() => null);" in src[src.index("function fetchReach"):]

    def test_the_same_set_is_asked_once(self):
        src = self._source()
        f = src[src.index("function fetchReach"):]
        f = f[: f.index("\n  }", f.index("return p;"))]
        assert "_reachCache.has(key)" in f and "_reachCache.set(key, p);" in f
        assert 'const key = groupIds.slice().sort().join(",");' in f


class TestTheRosterNoLongerShipsToTheBrowser:
    """Audit S2. Every group's full `member_ids` travelled in the overview
    payload to support two lookups — reach (E3) and the group list's
    search-by-member. Both are answered by the server now, and the payload no
    longer grows with headcount.
    """


    def _source(self) -> str:

        return access_page_source()

    def test_nothing_matches_against_a_local_roster_any_more(self):
        src = self._source()
        # The only remaining reader is reachOf's fallback, which now says it is
        # an estimate the server paints over where the number decides anything.
        readers = [i for i in range(len(src)) if src.startswith("member_ids", i)]
        assert len(readers) <= 3, f"{len(readers)} mentions of member_ids; expected reachOf's fallback only"
        assert "new Set(g.member_ids || [])" not in src

    def test_the_member_search_asks_the_server_once_per_query(self):
        src = self._source()
        f = src[src.index("function fetchMemberGroups(q)"):]
        f = f[: f.index("\n  }\n", f.index("return _memberCache"))]
        assert "_memberCache.has(q)" in f and "_memberCache.set(q," in f
        assert "if (!q || q.length < 2)" in f, "the two-character floor is applied before asking"

    def test_a_hit_is_stored_with_the_query_it_answers(self):
        """A repaint for a different query must not read a stale hit."""
        src = self._source()
        assert "const mm = (memberMatches.q === q) ? memberMatches : null;" in src
        assert "memberMatches = { q, byGroup: new Map(" in src

    def test_everyone_is_a_hit_whenever_anyone_matched(self):
        src = self._source()
        assert "if (g.is_everyone) return mm.matched_people ?" in src

    def test_the_search_trigger_no_longer_loads_a_roster(self):
        src = self._source()
        assert "loadUsers().then(() => { if (groupFilter.trim()) repaintQuery(); });" not in src
        assert "fetchMemberGroups(groupFilter.trim().toLowerCase()).then(" in src

    def test_the_trigger_is_not_gated_on_a_roster_that_no_longer_loads(self):
        """`!users.length` belonged to the roster load this replaced. Kept, the
        member search silently never fires on a visit where the person lens
        already filled `users` — a search that works until you open the other
        tab. Found by driving the page: zero requests, list unchanged."""
        src = self._source()
        i = src.index("fetchMemberGroups(groupFilter.trim().toLowerCase())")
        cond = src[src.rfind("if (", 0, i) : i]
        assert "users.length" not in cond, cond
        assert "groupFilter.trim().length >= 2" in cond


class TestAnMcpSourceRowStatesItsSecondCondition:
    """Audit F6. An `mcp_source` grant is necessary but not sufficient — it is
    ANDed with per-tool grants set on the source's own page. The row implied
    completeness it could not deliver; it now says "N of M tools granted →"
    and where that is set. "0 of 12" is the state worth seeing most: a
    visible server with nothing usable in it.
    """


    def _source(self) -> str:

        return access_page_source()

    def test_the_row_reads_the_servers_count_and_links_to_where_tools_are_set(self):
        src = self._source()
        assert 'if (t.type_key === "mcp_source" && !grant.inherited) {' in src
        assert "(overview.mcp_tool_grants || {})[i.resource_id]" in src
        assert 'href="/admin/mcp-sources/${encodeURIComponent(i.resource_id)}"' in src

    def test_a_source_with_no_tools_says_so_rather_than_printing_a_fraction(self):
        assert ': "no tools registered yet";' in self._source()

    def test_zero_of_many_is_the_warned_state(self):
        src = self._source()
        assert 'tc.total && !n ? " ax-r__tools--none" : ""' in src
        assert ".ax-r__tools--none { color: var(--ds-accent-warn-ink" in src


class TestTheLocalCopyKnowsWhenItIsStale:
    """Audit E2. The model this page holds is a copy, and two admins can hold
    two. Every write was applied to the copy optimistically — the requested
    tier stored as if it were the server's, a deleted row's 404 swallowed as
    success — so two people could each see their own answer until a reload
    and confidently report contradictory access states.

    Full conflict detection is not needed; knowing the copy is stale is. A
    write takes the server's row back, a 404 on a row still shown means
    someone else changed it and the page refetches and says so, and
    returning to the tab refetches a copy old enough to matter.
    """


    def _source(self) -> str:

        return access_page_source()

    def test_there_is_one_way_to_refresh_the_model(self):
        src = self._source()
        assert "async function refetchOverview() {" in src
        # The group-delete path's private copy of the fetch is gone.
        assert "const fresh = await fetch(OVERVIEW_API" not in src

    def test_a_write_takes_the_servers_row_not_the_request(self):
        src = self._source()
        assert "overview.grants.push(_rowFromResponse(created));" in src
        assert "grant.requirement = saved && saved.requirement ? saved.requirement : requirement;" in src

    def test_a_404_on_a_shown_row_is_news_on_both_write_paths(self):
        src = self._source()
        assert src.count('await changedElsewhere("That grant");') == 2   # PUT and DELETE
        assert src.count('throw new Error("changed_elsewhere");') == 2

    def test_no_caller_paints_over_the_sentence(self):
        """The first live run showed "Could not save: changed_elsewhere" — the
        tier handler's generic catch repainting a machine token over the
        sentence changedElsewhere had just shown. Every caller must swallow
        the sentinel."""
        src = self._source()
        assert src.count('if (err.message === "changed_elsewhere") return;') == 2   # tier click, checkbox
        assert 'if (err.message === "changed_elsewhere") { done++; continue; }' in src   # the bulk loop
        # And the bulk loop still counts a real failure — the repair that put
        # `failed++` back after a one-line catch was mangled into a comment.
        i = src.index('if (err.message === "changed_elsewhere") { done++; continue; }')
        assert "failed++;" in src[i : i + 200]

    def test_returning_to_the_tab_refreshes_a_stale_copy(self):
        src = self._source()
        assert 'document.addEventListener("visibilitychange"' in src
        assert "const STALE_AFTER_MS = 30000;" in src
        assert "if (Date.now() - _overviewFetchedAt < STALE_AFTER_MS) return;" in src


class TestRemovingAMemoryDomainGrantIsNotCalledRevoke:
    """Audit F2, the most dangerous finding: a memory_domain grant is ADDITIVE
    — it reveals the domain's items to a group and hides them from nobody
    else — so removing it takes no access away, which is precisely what a
    button labelled Revoke implies it does. An admin would click Revoke, read
    the success toast, and report a removal that had not happened.

    Decision recorded: rename honestly now; whether the grant should actually
    restrict, or leave this page, is a permission-model question deferred.
    """


    def _source(self) -> str:

        return access_page_source()

    def test_the_act_is_named_for_what_it_does(self):
        src = self._source()
        assert 'const ACT_WORD = (typeKey) => (typeKey === "memory_domain" ? "Stop revealing" : "Revoke");' in src
        cell = src[src.index("const manageCell = (o) => {"): src.index("\n  };", src.index("const manageCell = (o) => {"))]
        assert cell.count("data-revoke>${act}</button>") == 2
        assert "data-revoke>Revoke</button>" not in cell

    def test_both_row_renderers_tell_the_cell_the_type(self):
        src = self._source()
        # The call grew `hrefLabel` when an agent's link learned to say "owner ↗"
        # (U7); what this pins is that the TYPE still travels with it.
        assert "hrefLabel: ownLabel, typeKey: t.type_key," in src
        assert "manageCell({ managedBy: grant.managed_by, typeKey: r.t.type_key })" in src

    def test_the_confirm_does_not_claim_anyone_loses_anything(self):
        src = self._source()
        assert 'const reveals = type === "memory_domain";' in src
        assert "It hides nothing from anyone else — a memory-domain grant only reveals; it never restricts." in src
        assert 'confirmText: "Stop revealing",' in src

    def test_the_toast_agrees(self):
        assert "No longer revealed to this group — nothing was hidden from anyone else" in self._source()

    def test_the_person_tab_stays_one_line(self):
        """A first pass stacked the tab's children in a column, which dropped
        the count badge under the label and made this one tab three lines tall
        beside two one-line siblings — the active underline no longer lined
        up. Seen in the preview, not in a test. The kind is inline after the
        count, behind a separator, and yields first on a narrow strip."""
        src = self._source()
        assert ".ax-by__tool { flex-direction: column" not in src
        assert '.ax-by__kind::before { content: "·";' in src
        assert ".ax-by__kind { display: none; }" in src   # inside the narrow-strip media query


class TestAStoreEntityIsATieredKind:
    """`store_entity` was missing from TIERED — in this page and in the mock it
    was built from — while the API has always accepted `required` on it. So
    the F8 branch in controlCell was dead code behind `!tiered`: an
    organization-published skill rendered no tier control at all, and a
    user-published one rendered nothing instead of stating its one legal
    tier. Found by seeding both kinds and looking, not by any test — which is
    why this one exists.
    """


    def _source(self) -> str:

        return access_page_source()

    def test_store_entity_is_in_the_tiered_set(self):
        src = self._source()
        line = src[src.index("const TIERED = new Set(["):]
        line = line[: line.index("]);")]
        assert '"store_entity"' in line, "without this the whole F8 branch is unreachable"

    def test_the_picker_writes_a_user_published_skill_at_the_only_legal_tier(self):
        """The row's rule, applied at write time too — otherwise a batch with
        Automatic chosen fails on click with the server's 422."""
        src = self._source()
        assert 'const userSkill = type === "store_entity" && ((itemOf(type, rid) || {}).publisher_kind || "user") !== "organization";' in src
        assert 'const tier = TIERED.has(type) && !userSkill ? (pickerState.tier || "available") : "available";' in src


class TestASharedRowNamesTheSharer:
    """A Library share records the sharer's user id (`library_sharing` writes
    `assigned_by=actor_id`), and the page resolved that id only against a user
    list the group list never loads — so an owner-shared row read
    "shared by <uuid>" on the one page whose job is to say who. Decision 09
    said the label names the sharer; a uuid does not. Found by seeding a real
    share and looking. The server resolves the name now, once per page, with
    the same batch reader the collection projection already uses for owners.
    """


    def _source(self) -> str:

        return access_page_source()

    def test_the_page_prefers_the_resolved_name(self):
        src = self._source()
        f = src[src.index("function whoGranted(grant) {"):]
        f = f[: f.index("\n  }", 0)]
        assert "grant.assigned_by_name" in f
        assert f.index("assigned_by_name") < f.index("const raw = grant && grant.assigned_by;")


class TestAnOwnerSharedRowSaysWhoAndGoesSomewhereReal:
    """Audit U7, the rest of it — found by the owner looking at a seeded row.

    An agent's "where it lives" went to /agents?agent=<id>: the OWNER's
    builder, fed by /api/v1/agents (the caller's own agents), so for an admin
    looking at a colleague's agent it opened on nothing. There is no admin
    page for an agent; it lives with its owner, so the link goes there.

    "shared by <name>" sat last in the detail line, in grey, after the
    description and the file count — the one fact the row exists to carry,
    placed as a footnote. It leads now, as a name. And the sharer is compared
    to the owner by id, not by guessing from names, so "owned by" is said
    once, or not at all when they are the same person.
    """


    def _source(self) -> str:

        return access_page_source()

    def test_an_agents_link_lands_on_its_owners_shares(self):
        """Two destinations failed the owner's look — the owner's builder
        (shows an admin nothing) and the owner's People page (showed nothing
        about sharing). The People page now has a Shares section, so the link
        goes there, anchored, and says what it is."""
        src = self._source()
        assert "? `/admin/users/${encodeURIComponent(item.owner_user_id)}#shares`" in src
        assert 'const ownLabel = t.type_key === "agent" ? "owner ↗" : "where it lives ↗";' in src

    def test_the_sharer_leads_the_row(self):
        src = self._source()
        i = src.index('bits.push(`<span class="ax-r__shared">Shared by <b>${esc(who)}</b></span>`);')
        j = src.index("if (i.description) bits.push(esc(String(i.description).slice(0, 120)));", i - 400)
        assert i < j, "the sharer must be pushed before the description"

    def test_shared_by_is_the_owners_act_and_granted_by_is_anyone_elses(self):
        """An admin granting Ada's agent to a group is not a share — Ada was
        not in the loop — so that row says who granted it AND whose it is.
        The first cut said "Shared by <admin>" for every owner-shared kind,
        crediting the owner with a decision they did not make."""
        src = self._source()
        assert "if (owned && who && sharerIsOwner) {" in src
        assert "Shared by <b>${esc(who)}</b>" in src
        assert "} else if (owned && who) {" in src
        assert "Granted by <b>${esc(who)}</b>" in src
        assert "owned by ${esc(i.owner_email)}" in src[src.index("Granted by <b>"):][:200]

    def test_sharer_and_owner_are_compared_by_id(self):
        src = self._source()
        assert "grant.assigned_by === i.owner_user_id" in src
        assert 'itemProvenance(i, { ownerNamed: !!(owned && who) })' in src
        assert "if (i.owner_email && !(opts && opts.ownerNamed))" in src
        assert 'split("@")[0]' not in src[src.index("const sharerIsOwner"): src.index("const sharerIsOwner") + 600]


class TestSharingBesideAnEveryoneGrantSaysWhatItWouldDo:
    """Audit U8. A resource Everyone already had still offered "Share with
    another group · 7 other groups could have it" — false, since every group
    already had it. A group grant beside an everyone grant does exactly one
    thing: it wins for that group and can carry a different tier. So on a
    tiered kind that is the offer, named; on an untiered kind a group grant
    would change nothing, and nothing is offered.
    """


    def _source(self) -> str:

        return access_page_source()

    def test_untiered_kinds_offer_nothing_beside_an_everyone_grant(self):
        src = self._source()
        assert 'const evGrant = r.held.find((g) => g.audience === "everyone");' in src
        assert 'if (evGrant && !tieredKind) return "";' in src

    def test_tiered_kinds_offer_a_different_tier_and_say_so(self):
        src = self._source()
        assert '${evGrant ? "Set a different tier for a group" : nobody ? "Share it with a group" : "Share with another group"}' in src
        assert "if (evGrant) return `everyone already has it as ${evTier} — a group can get it as ${otherTier} instead`;" in src

    def test_the_old_copy_survives_where_no_everyone_grant_exists(self):
        src = self._source()
        assert '? `${left} other ${left === 1 ? "group" : "groups"} could have it`' in src
