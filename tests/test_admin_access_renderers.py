"""The Access page's renderers, executed — not grepped.

The page's script was ~4,500 lines inside `admin_access.html`. Nothing could
import or run it, so every guard over its behaviour was a text scan: a test
asserted that a *string* appeared in the template and inferred that the right
thing therefore rendered. That inference is what the 2026-09 access audit
kept catching out. Three renderers independently printed the same false claim
about what a tier does (U1) and the scans passed on all three, because each
one contained the string it was searching for. Other scans broke on unrelated
edits, and one fired on a CSS class being added.

The script is now `app/web/static/js/admin_access.js`. These tests slice its
declarations out, run them under node with a stub `document`/`window`, and
assert on the HTML the functions actually return.

The slice runs from the top of the IIFE to the first DOM renderer, so it
executes the module's real top-level declarations in their real order — the
same thing the browser does — rather than lifting one function out of its
context. `esc`, the vocabulary, the tier sets and the per-kind maps are all
the production ones. What is stubbed is only the browser.

Static-source guards keep their place: `tests/test_access_vocabulary.py` pins
the words, and this file pins what is done with them.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from app.web import vocabulary
from tests.helpers.access_page import access_js

#: What the template's `<script type="application/json" id="ax-boot-data">`
#: blob carries. Built from `app.web.vocabulary` for the same reason the
#: template reads `words`: a test that spells the labels itself stops being
#: able to notice a rename.
BOOT = {
    "viewer_user_id": "admin1",
    "google_group_prefix": "grp_acme_",
    "invite_domains": ["example.com"],
    "words": {
        "tier_optional": vocabulary.TIER_OPTIONAL,
        "tier_automatic": vocabulary.TIER_AUTOMATIC,
        "tier_optional_help": vocabulary.TIER_OPTIONAL_HELP,
        "tier_automatic_help": vocabulary.TIER_AUTOMATIC_HELP,
    },
}

#: Distinguishes "the page sent no blob" from "the test did not name one".
ABSENT = object()

#: The module's declarations, up to the first function that touches the DOM.
_SLICE_END = "  function renderGroups() {"

#: Names the tests reach for. Anything not listed here stays private, which
#: keeps this file from quietly becoming the module's public API.
_EXPORTS = (
    "esc",
    "deriveDisplayName",
    "titleOf",
    "subtitleOf",
    "isEditable",
    "itemName",
    "itemProvenance",
    "tierSentence",
    "tierControl",
    "controlCell",
    "ACT_WORD",
    "manageCell",
    "whoGranted",
    "addScopeFor",
    "addableAtScope",
    "memberLabel",
    "memberVerb",
    "SCOPE_WITHHELD_TYPES",
    "notSharedWith",
    "holdingHeadsRows",
    "sharePickerCount",
    "sharePickerApply",
    "TIERED",
    "facets",
    "rowPassesFacets",
    "reachOfRow",
    "originOfGrant",
    "VIEWER_USER_ID",
    "GOOGLE_GROUP_PREFIX",
    "INVITE_DOMAINS",
)


def _declarations() -> str:
    js = access_js()
    start = js.index('"use strict";') + len('"use strict";')
    end = js.index(_SLICE_END)
    return js[start:end]


def _harness(body: str, boot=None) -> str:
    """The module's declarations under a stub browser, then `body`.

    `body` sets `OUT` to whatever the test wants read back; the harness
    prints it as JSON. Pass `boot=ABSENT` to serve a page with no blob at all.
    """
    node_js = (
        "null" if boot is ABSENT else "{ textContent: " + json.dumps(json.dumps(BOOT if boot is None else boot)) + " }"
    )
    return f"""
const document = {{
  getElementById: (id) => (id === "ax-boot-data" ? {node_js} : null),
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener: () => {{}},
}};
const window = {{ location: {{ search: "", hash: "" }}, AgnesTime: null, AgnesKindGlyph: null }};
const sessionStorage = {{ getItem: () => null, setItem: () => {{}} }};
const api = (function () {{
{_declarations()}
  return {{ {", ".join(_EXPORTS)} }};
}})();
let OUT;
{body}
console.log(JSON.stringify(OUT));
"""


def _run(body: str, boot=None):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    script = _harness(body, boot)
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


class TestTheModuleReadsWhatTheTemplateSends:
    """The boot blob is the whole server boundary now, so it is worth a test
    of its own: the extraction is only sound if the values the template
    writes are the values the script ends up with."""

    def test_the_boot_blob_is_where_the_server_values_come_from(self):
        got = _run(
            "OUT = { viewer: api.VIEWER_USER_ID, prefix: api.GOOGLE_GROUP_PREFIX, domains: api.INVITE_DOMAINS };"
        )
        assert got == {
            "viewer": "admin1",
            "prefix": "grp_acme_",
            "domains": ["example.com"],
        }

    def test_a_missing_blob_does_not_take_the_page_down(self):
        """Every reader has a default, so a page served without the blob
        renders rather than throwing during module evaluation — which on a
        module is silent and leaves the page on "Loading groups…"."""
        got = _run(
            "OUT = { viewer: api.VIEWER_USER_ID, prefix: api.GOOGLE_GROUP_PREFIX,"
            " domains: api.INVITE_DOMAINS, tier: api.tierControl('available', true) };",
            boot=ABSENT,
        )
        assert got["viewer"] is None
        assert got["prefix"] == ""
        assert got["domains"] == []
        assert "<button" in got["tier"]


class TestAGroupsCountSaysMembersNotPeople:
    """The one function eight renderers now call, executed.

    `member_count` counts MEMBERSHIPS: a group may hold a service account
    (#1534) or a seeded system identity, and `is_person` — which
    `/api/admin/groups/reach` counts by — counts neither. So the group row
    said "3 people" two words from the reach line's people-only "2 people",
    on one screen, and both numbers were right.

    Asserted by running it rather than by grepping for the ternary: the
    label was hand-rolled in seven places before this, and a scan for the
    string passed on every one of them while they disagreed.
    """

    @pytest.mark.parametrize(
        ("n", "expected"),
        [(0, "0 members"), (1, "1 member"), (2, "2 members"), (41, "41 members")],
    )
    def test_the_count_is_said_in_members(self, n, expected):
        assert _run(f"OUT = api.memberLabel({n});") == expected

    @pytest.mark.parametrize(("n", "expected"), [(0, "lose"), (1, "loses"), (2, "lose"), (41, "lose")])
    def test_the_verb_agrees_with_the_count(self, n, expected):
        """The two confirmations read "N members lose …". The count was
        always the variable and the verb never was, so one member "lose"
        it — and before the relabel, one person did too."""
        assert _run(f"OUT = api.memberVerb({n}, 'loses', 'lose');") == expected

    def test_the_singular_is_the_only_special_case(self):
        """One member is "1 member" — not "1 members", and not "1 person".

        The plural boundary is the whole reason this was a ternary in seven
        places, which is the reason it is one function now.
        """
        got = _run("OUT = [0, 1, 2].map((n) => api.memberLabel(n));")
        assert got == ["0 members", "1 member", "2 members"]
        assert "person" not in " ".join(got)


class TestTheTierControlSaysTheVocabularysWords:
    def test_both_labels_come_from_the_vocabulary_module(self):
        html = _run("OUT = api.tierControl('required', true);")
        assert vocabulary.TIER_OPTIONAL in html
        assert vocabulary.TIER_AUTOMATIC in html
        assert 'data-tier="available"' in html
        assert 'data-tier="required"' in html

    def test_the_active_button_is_the_grants_own_tier(self):
        available = _run("OUT = api.tierControl('available', true);")
        required = _run("OUT = api.tierControl('required', true);")
        assert 'data-tier="available">' in available
        assert available.index("is-active") < available.index('data-tier="required"')
        assert required.index('data-tier="available"') < required.index("is-active")

    def test_an_ungranted_row_marks_the_control_disabled(self):
        assert 'aria-disabled="true"' in _run("OUT = api.tierControl('available', false);")
        assert 'aria-disabled="true"' not in _run("OUT = api.tierControl('available', true);")


class TestAControlIsDrawnOnlyWhereItCanAct:
    """Audit finding U5. Twelve of the sixteen kinds have no tier, and the
    page used to draw the pair on all of them, greyed, with the reason in a
    tooltip — a dead control on three quarters of the rows."""

    @pytest.mark.parametrize("kind", ["data_package", "memory_domain", "marketplace_plugin"])
    def test_a_tiered_kind_gets_the_pair(self, kind):
        html = _run(f"OUT = api.controlCell({json.dumps(kind)}, 'available', {{}});")
        assert 'data-tier="required"' in html

    @pytest.mark.parametrize("kind", ["table", "slack_channel", "agent", "data_app"])
    def test_an_untiered_kind_gets_nothing_at_all(self, kind):
        html = _run(f"OUT = api.controlCell({json.dumps(kind)}, 'available', {{}});")
        assert "data-tier" not in html
        assert "aria-disabled" not in html, "a greyed pair is the thing U5 removed"

    def test_the_tiered_set_and_the_rendering_agree(self):
        """The set is the single source; a kind added to one and not the
        other is how `store_entity` came to render nothing (F8)."""
        tiered = _run("OUT = [...api.TIERED];")
        for kind in tiered:
            if kind == "store_entity":
                continue  # its own rule, pinned below
            html = _run(f"OUT = api.controlCell({json.dumps(kind)}, 'available', {{}});")
            assert 'data-tier="required"' in html, kind


class TestAUserPublishedStoreEntityStatesItsTierInsteadOfOfferingIt:
    """F8. The API refuses `required` on a user-published item with a 422, so
    drawing the pair there offers a choice that fails on click."""

    def test_an_organization_published_item_gets_the_real_control(self):
        html = _run("OUT = api.controlCell('store_entity', 'available', { publisherKind: 'organization' });")
        assert 'data-tier="required"' in html

    def test_a_user_published_item_states_the_one_legal_tier(self):
        html = _run("OUT = api.controlCell('store_entity', 'available', { publisherKind: 'user' });")
        assert 'data-tier="required"' not in html
        assert vocabulary.TIER_OPTIONAL in html
        assert "organization-published" in html

    def test_an_unknown_publisher_is_treated_as_the_narrower_one(self):
        """`publisher_kind` absent must not widen the permission by omission."""
        html = _run("OUT = api.controlCell('store_entity', 'available', {});")
        assert 'data-tier="required"' not in html


class TestANonRevocableGrantOffersNeitherHalf:
    """#1956 item 13: a Required plugin's grant is re-asserted elsewhere and
    the API refuses to delete it (409). The page drew Revoke and a tier pair
    on it anyway, over a confirm promising it could be granted again here."""

    MANAGED = (
        "{ managedBy: { revocable: false, label: 'Marketplaces',"
        " reason: 'Set on /admin/marketplaces', surface: 'Marketplaces',"
        " href: '/admin/marketplaces' } }"
    )

    def test_the_control_states_where_it_is_owned(self):
        html = _run(f"OUT = api.controlCell('marketplace_plugin', 'required', {self.MANAGED});")
        assert "data-tier" not in html
        assert "Marketplaces" in html

    def test_the_act_becomes_the_way_to_the_owning_surface(self):
        html = _run(f"OUT = api.manageCell(Object.assign({{ typeKey: 'marketplace_plugin' }}, {self.MANAGED}));")
        assert "Revoke" not in html
        assert 'href="/admin/marketplaces"' in html

    def test_a_seeded_default_keeps_its_control(self):
        """`revocable` is the split, not authorship: several writers seed a
        default an admin is expected to override."""
        html = _run(
            "OUT = api.controlCell('data_package', 'available', { managedBy: { revocable: true, label: 'Seeded' } });"
        )
        assert 'data-tier="required"' in html


class TestRemovingAMemoryDomainGrantIsNamedForWhatItDoes:
    """Audit F2, the first live finding: a memory_domain grant is additive,
    so removing it takes access from nobody. An admin clicked Revoke, read
    the success toast, and reported a removal that had not happened."""

    def test_the_memory_domain_act_is_not_called_revoke(self):
        assert _run("OUT = api.ACT_WORD('memory_domain');") == "Stop revealing"

    @pytest.mark.parametrize("kind", ["data_package", "table", "agent", "marketplace_plugin"])
    def test_every_other_kind_still_revokes(self, kind):
        assert _run(f"OUT = api.ACT_WORD({json.dumps(kind)});") == "Revoke"


class TestTheTierSentenceIsPerKind:
    """U1: one sentence written for data packages — "permanent, and always
    downloaded" — fired verbatim on memory domains and marketplace plugins,
    neither of which is downloaded in that sense. Three renderers said it and
    three string-scanning guards passed."""

    def test_a_kinds_sentence_names_what_that_kind_does(self):
        pkg = _run("OUT = api.tierSentence('data_package', 'required');")
        plugin = _run("OUT = api.tierSentence('marketplace_plugin', 'required');")
        assert pkg != plugin, "one sentence for every kind is exactly finding U1"

    def test_a_kind_with_no_sentence_falls_back_without_claiming_a_download(self):
        text = _run("OUT = api.tierSentence('slack_channel', 'required');")
        assert "download" not in text.lower()

    def test_the_two_tiers_do_not_say_the_same_thing(self):
        req = _run("OUT = api.tierSentence('data_package', 'required');")
        avail = _run("OUT = api.tierSentence('data_package', 'available');")
        assert req != avail
        assert "cannot opt out" in req
        assert "choice" in avail


class TestARowNamesWhoseThingItIs:
    """U7. A chat file-drop becomes a one-file collection named after the
    file, and an admin — who sees every collection by god-mode — got a flat
    list of filenames with nothing saying they belonged to someone else."""

    def test_the_owner_and_the_size_are_both_stated(self):
        out = _run("OUT = api.itemProvenance({ owner_email: 'ada@example.com', file_count: 3 }, {});")
        assert "ada@example.com" in out and "3 files" in out

    def test_one_file_is_not_pluralised(self):
        out = _run("OUT = api.itemProvenance({ file_count: 1 }, {});")
        assert out == "1 file"

    def test_the_owner_is_not_said_twice_when_the_row_already_named_them(self):
        """ "Shared by Ada Lovelace · owned by ada@example.com" states one
        fact twice."""
        out = _run("OUT = api.itemProvenance({ owner_email: 'ada@example.com' }, { ownerNamed: true });")
        assert out == ""

    def test_a_kind_carrying_neither_field_renders_exactly_as_before(self):
        assert _run("OUT = api.itemProvenance({ name: 'Finance' }, {});") == ""

    def test_the_owner_email_is_escaped(self):
        out = _run("OUT = api.itemProvenance({ owner_email: '<img src=x onerror=1>' }, {});")
        assert "<img" not in out and "&lt;img" in out


class TestASharerIsNamedNotIdentified:
    """A Library share read "shared by <uuid>" on the one page whose job is
    to say who."""

    def test_the_servers_resolved_name_wins(self):
        out = _run("OUT = api.whoGranted({ assigned_by: 'u_123', assigned_by_name: 'Ada Lovelace' });")
        assert out == "Ada Lovelace"

    def test_an_unresolved_address_falls_back_to_its_local_part_not_the_raw_id(self):
        assert _run("OUT = api.whoGranted({ assigned_by: 'ada@example.com' });") == "ada"

    def test_a_name_equal_to_the_id_is_not_treated_as_resolved(self):
        assert _run("OUT = api.whoGranted({ assigned_by: 'u_123', assigned_by_name: 'u_123' });") == "u_123"

    def test_nothing_assigned_says_nothing(self):
        assert _run("OUT = api.whoGranted({});") == ""


class TestAWorkspaceGroupIsCalledWhatPeopleCallIt:
    """A Workspace group is STORED under its full email, so without the
    configured prefix the left column read `grp_acme_finance@example.com`
    where the retired list read `Finance`."""

    def test_the_configured_prefix_is_stripped_and_the_name_capitalised(self):
        assert _run("OUT = api.deriveDisplayName('grp_acme_finance@example.com');") == "Finance"

    def test_an_address_without_the_prefix_keeps_its_local_part(self):
        assert _run("OUT = api.deriveDisplayName('finance@example.com');") == "Finance"

    def test_no_configured_prefix_is_the_normal_case_not_a_crash(self):
        boot = {**BOOT, "google_group_prefix": ""}
        assert _run("OUT = api.deriveDisplayName('grp_acme_finance@example.com');", boot=boot) == ("Grp_acme_finance")

    def test_an_address_that_is_only_the_prefix_keeps_something_to_read(self):
        assert _run("OUT = api.deriveDisplayName('grp_acme_@example.com');") == "grp_acme_"

    def test_a_mapped_system_row_shows_its_canonical_name_over_the_email(self):
        got = _run(
            "const g = { name: 'Everyone', mapped_email: 'all@example.com',"
            " is_google_managed: true };"
            "OUT = { title: api.titleOf(g), subtitle: api.subtitleOf(g) };"
        )
        assert got == {"title": "Everyone", "subtitle": "all@example.com"}

    def test_a_synced_row_derives_its_title_and_keeps_the_address_below(self):
        got = _run(
            "const g = { name: 'grp_acme_finance@example.com', is_google_managed: true };"
            "OUT = { title: api.titleOf(g), subtitle: api.subtitleOf(g) };"
        )
        assert got == {"title": "Finance", "subtitle": "grp_acme_finance@example.com"}

    def test_a_row_owned_elsewhere_is_not_renameable_here(self):
        got = _run(
            "OUT = { plain: api.isEditable({ name: 'Finance' }),"
            " system: api.isEditable({ is_system: true }),"
            " synced: api.isEditable({ is_google_managed: true }) };"
        )
        assert got == {"plain": True, "system": False, "synced": False}


class TestAThingWithoutATitleIsStillNamed:
    """An agent saved without a title rendered as 32 hex characters — as a row
    title, and as 12 of the 13 options in one group's picker."""

    def test_the_name_wins_then_the_slug_then_the_id(self):
        got = _run(
            "OUT = [api.itemName({ name: 'N', slug: 's', resource_id: 'r' }),"
            " api.itemName({ slug: 's', resource_id: 'r' }),"
            " api.itemName({ resource_id: 'r' }),"
            " api.itemName(null)];"
        )
        assert got == ["N", "s", "r", ""]


class TestTheFiltersAreFacets:
    """Four multi-select facets where there was one single-select radio.

    The page could answer "show me the plugins" and nothing else. Kind kept
    its place; Reach, Tier and Where-it-came-from are the questions an admin
    opens this page with, and each reads a field the payload already carries.

    The matching rules are pure and live in the sliced region, so these run
    the production functions rather than reading them.
    """

    HELD_EVERYONE = "[{audience: 'everyone', requirement: 'required', source: null}]"
    HELD_GROUP = "[{audience: 'g1', requirement: 'available', source: null}]"
    HELD_OWNER = "[{audience: 'g1', requirement: 'available', source: 'library_sharing'}]"
    HELD_MANAGED = "[{audience: 'g1', requirement: 'required', source: 'marketplace', managed_by: {revocable: false}}]"

    def test_reach_is_derived_from_the_grants_not_stored_beside_them(self):
        got = _run(
            f"OUT = [api.reachOfRow({self.HELD_EVERYONE}), api.reachOfRow({self.HELD_GROUP}),"
            " api.reachOfRow([]), api.reachOfRow(null)];"
        )
        assert got == ["everyone", "group", "nobody", "nobody"]

    def test_an_everyone_grant_outranks_a_group_grant_on_the_same_row(self):
        """A row held by both reaches everyone; saying "specific groups"
        would be the smaller claim and the false one."""
        mixed = "[{audience: 'g1'}, {audience: 'everyone'}]"
        assert _run(f"OUT = api.reachOfRow({mixed});") == "everyone"

    def test_origin_splits_on_whether_the_admin_can_act_not_on_who_wrote_it(self):
        """Ticket 10's axis. Nine writers are not the admin; only the ones
        that re-assert produce a row a revoke here cannot remove."""
        got = _run(
            "OUT = [api.originOfGrant({source: null}),"
            " api.originOfGrant({source: 'library_sharing'}),"
            " api.originOfGrant({source: 'marketplace', managed_by: {revocable: false}}),"
            " api.originOfGrant({source: 'chat_seed', managed_by: {revocable: true}})];"
        )
        assert got == ["admin", "owner", "managed", "admin"]

    def test_a_facet_with_nothing_picked_filters_nothing(self):
        """Empty is off — not "every value ticked", which a value matching
        nothing would silently turn into an empty list."""
        assert _run("OUT = api.rowPassesFacets('agent', []);") is True

    def test_values_inside_one_facet_are_or(self):
        got = _run(
            "api.facets.get('kind').add('agent'); api.facets.get('kind').add('data_package');"
            "OUT = [api.rowPassesFacets('agent', []), api.rowPassesFacets('data_package', []),"
            " api.rowPassesFacets('chat', [])];"
        )
        assert got == [True, True, False]

    def test_facets_are_and_with_each_other(self):
        got = _run(
            "api.facets.get('kind').add('agent'); api.facets.get('reach').add('nobody');"
            f"OUT = [api.rowPassesFacets('agent', []), api.rowPassesFacets('agent', {self.HELD_EVERYONE}),"
            " api.rowPassesFacets('chat', [])];"
        )
        assert got == [True, False, False], "kind AND reach, not kind OR reach"

    def test_a_tier_filter_keeps_a_row_where_any_grant_carries_that_tier(self):
        got = _run(
            "api.facets.get('tier').add('required');"
            f"OUT = [api.rowPassesFacets('data_package', {self.HELD_EVERYONE}),"
            f" api.rowPassesFacets('data_package', {self.HELD_GROUP})];"
        )
        assert got == [True, False]

    def test_an_ungranted_row_survives_no_tier_or_origin_filter(self):
        """It has no grant to carry either property, so a filter on one is a
        question it cannot answer yes to."""
        got = _run("api.facets.get('tier').add('available');OUT = api.rowPassesFacets('agent', []);")
        assert got is False

    def test_an_owner_shared_row_is_findable_by_where_it_came_from(self):
        """The fastest way to the rows an admin most often opens this page
        to check."""
        got = _run(
            "api.facets.get('origin').add('owner');"
            f"OUT = [api.rowPassesFacets('agent', {self.HELD_OWNER}),"
            f" api.rowPassesFacets('agent', {self.HELD_GROUP}),"
            f" api.rowPassesFacets('agent', {self.HELD_MANAGED})];"
        )
        assert got == [True, False, False]

    def test_the_url_carries_every_facet_so_a_filtered_view_is_a_link(self):
        from tests.helpers.access_page import access_js

        js = access_js()
        assert "for (const k of FACET_KEYS) set(k, st[k]);" in js
        assert 'kind: [...facets.get("kind")].join(",")' in js, (
            "?kind=agent — the single-value shape in existing links — must read back as the one-element case"
        )


class TestAnAddStartedOnEveryoneReachesEveryAccount:
    """The Everyone audience is selected by the CARRIER group's id, so the
    picker's "which audience is this" question cannot be answered from its
    mode. It was: `asScope` keyed on `bundleMode`, so the Everyone block's own
    `+ Add for everyone` wrote a grant with no scope — reach is the carrier's
    members, under a heading that says every account, and an account in no
    group (the case the scope exists for) was left out.

    Run rather than scanned: the decision is a pure function now precisely so
    a guard can call it.
    """

    GROUPS = "[{ id: 'ev1', is_everyone: true }, { id: 'g1' }, { id: 'g2' }]"

    def test_the_everyone_carrier_resolves_to_the_scope(self):
        assert _run(f"OUT = api.addScopeFor('ev1', {self.GROUPS});") == "everyone"

    def test_an_ordinary_group_does_not(self):
        assert _run(f"OUT = api.addScopeFor('g1', {self.GROUPS});") is None

    def test_no_audience_at_all_does_not(self):
        assert _run(f"OUT = api.addScopeFor(null, {self.GROUPS});") is None
        assert _run("OUT = api.addScopeFor('ev1', []);") is None

    def test_an_instance_with_no_carrier_falls_back_to_the_group(self):
        """A grant on a group is always writable; a scope with no carrier is
        not (the API answers 409 `everyone_carrier_group_missing`). Guessing
        the scope here would turn every add on such an instance into a
        failure."""
        assert _run("OUT = api.addScopeFor('g1', [{ id: 'g1' }, { id: 'g2' }]);") is None


class TestAScopeModePickerOffersOnlyWhatTheApiAccepts:
    """`SCOPE_WITHHELD_TYPES` mirrors `src.grant_scopes.SCOPE_WITHHELD_TYPES`:
    four kinds for which "give this to everyone" is not a coherent choice, and
    which the API refuses with a 422. The scope-mode picker listed them, so the
    fix for the silent wrong write would have produced a visible batch failure
    instead."""

    def test_a_withheld_kind_is_not_addable_at_the_everyone_scope(self):
        got = _run("OUT = [...api.SCOPE_WITHHELD_TYPES].map((k) => api.addableAtScope(k, 'everyone'));")
        assert got == [False] * len(got) and got, got

    def test_the_same_kinds_stay_addable_to_a_group(self):
        got = _run("OUT = [...api.SCOPE_WITHHELD_TYPES].map((k) => api.addableAtScope(k, null));")
        assert got == [True] * len(got) and got, got

    def test_a_kind_that_takes_the_scope_is_addable_either_way(self):
        got = _run(
            "OUT = ['marketplace_plugin', 'data_package', 'agent', 'chat', 'collection']"
            ".map((k) => [api.addableAtScope(k, 'everyone'), api.addableAtScope(k, null)]);"
        )
        assert got == [[True, True]] * 5, got

    def test_the_mirror_still_matches_the_server(self):
        """Two copies of one list, so the drift is worth a guard: the page's
        set is the reason a choice is not offered, the server's is the reason
        it is refused."""
        from src.grant_scopes import SCOPE_WITHHELD_TYPES

        assert set(_run("OUT = [...api.SCOPE_WITHHELD_TYPES];")) == set(SCOPE_WITHHELD_TYPES)


class TestOneAnswerPerResourceInThePersonLens:
    """#2255. The By-person lens rendered one package twice, in two sections
    that contradicted each other: "In their Library" (the library preview,
    which resolves the everyone scope) AND "Not shared with them · No group of
    theirs" (this list, which asked which of the person's GROUPS grants it).

    The reader's fix is #2254 — effective-access now resolves the scope too —
    but the two lists must not be able to disagree again, so the gap list is
    derived by SUBTRACTING both what the person reaches and what the panel
    above it already shows. It also dedupes: the band's "N of M" denominator
    is `shown + not-shared`, and an instance with one package and two grants
    on it read "1 of 2".
    """

    ALL = "[{ resource_id: 'p1', name: 'One' }, { resource_id: 'p2', name: 'Two' }]"

    def test_a_package_reached_by_a_grant_is_not_also_not_shared(self):
        got = _run(f"OUT = api.notSharedWith({self.ALL}, new Set(['p1']), new Set()).map((p) => p.resource_id);")
        assert got == ["p2"]

    def test_a_package_the_library_panel_shows_is_not_also_not_shared(self):
        """The panel resolves the everyone scope through `library-preview`. A
        row it lists is shared with them by definition, whatever the grant
        read says — this is the belt to #2254's braces."""
        got = _run(f"OUT = api.notSharedWith({self.ALL}, new Set(), new Set(['p1'])).map((p) => p.resource_id);")
        assert got == ["p2"]

    def test_the_denominator_counts_resources_not_grants(self):
        """Two grants on one package is one package. The list is keyed on
        `resource_id`, so a projection that lists the same package in two
        blocks cannot inflate the count the band prints."""
        dupes = "[{ resource_id: 'p1' }, { resource_id: 'p1' }, { resource_id: 'p2' }]"
        got = _run(f"OUT = api.notSharedWith({dupes}, new Set(), new Set()).map((p) => p.resource_id);")
        assert got == ["p1", "p2"]

    def test_nothing_reached_leaves_every_resource_in_the_gap(self):
        got = _run(f"OUT = api.notSharedWith({self.ALL}, new Set(), new Set()).length;")
        assert got == 2


class TestThePickerCountsTheScopeAsAScope:
    """#2257 item 1. Selecting `Everyone` plus one group read "2 groups · 10
    people" and offered "Share with 2 groups" — in the one control where the
    group/scope choice is actually made, and a few hundred pixels from the
    page's own words for the distinction ("Not a group — a scope")."""

    def test_the_scope_is_counted_beside_the_groups_not_among_them(self):
        got = _run("OUT = api.sharePickerCount(['everyone', 'g1'], 10);")
        assert got == "Everyone + 1 group · 10 people"

    def test_the_scope_alone_is_not_a_group_count(self):
        got = _run("OUT = api.sharePickerCount(['everyone'], 10);")
        assert got == "Everyone · 10 people"

    def test_groups_alone_read_exactly_as_before(self):
        assert _run("OUT = api.sharePickerCount(['g1', 'g2'], 10);") == "2 groups · 10 people"
        assert _run("OUT = api.sharePickerCount(['g1'], 1);") == "1 group · 1 person"

    def test_nothing_selected_says_so(self):
        assert _run("OUT = api.sharePickerCount([], 0);") == "No group selected"

    def test_the_apply_button_names_the_same_two_things(self):
        assert _run("OUT = api.sharePickerApply(['everyone', 'g1']);") == "Share with everyone and 1 group"
        assert _run("OUT = api.sharePickerApply(['everyone', 'g1', 'g2']);") == "Share with everyone and 2 groups"
        assert _run("OUT = api.sharePickerApply(['everyone']);") == "Share with everyone"
        assert _run("OUT = api.sharePickerApply(['g1', 'g2']);") == "Share with 2 groups"
        assert _run("OUT = api.sharePickerApply([]);") == "Apply"


class TestTheColumnHeaderHasRowsToHead:
    """#2257 item 3. The holding table's column header — Kind / What the group
    gets / Access tier / Manage — labels the columns of GRANT ROWS. A group
    with no grants of its own still renders the one-line "and everything
    Everyone has" summary, which is a sentence rather than a row of those
    columns, and the header sat over a band label and nothing else."""

    def test_a_group_holding_nothing_of_its_own_gets_no_header(self):
        assert _run('OUT = api.holdingHeadsRows("", "");') is False

    def test_its_own_rows_earn_the_header(self):
        assert _run('OUT = api.holdingHeadsRows("<div class=\\"ax-r\\"></div>", "");') is True

    def test_rows_set_elsewhere_earn_it_too(self):
        """The `Admin` case: every row is unactionable here, and each is still
        a row with a kind, a tier and a Manage cell."""
        assert _run('OUT = api.holdingHeadsRows("", "<div class=\\"ax-r\\"></div>");') is True
