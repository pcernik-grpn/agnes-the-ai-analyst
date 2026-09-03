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
    "TIERED",
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
