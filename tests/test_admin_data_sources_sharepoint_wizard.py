"""The SharePoint connect wizard on /admin/data-sources (spec 2026-08-27
§13.2): a source *type* on the existing picker, three steps
(connect -> scope -> share) in its own drawer — no new nav item, no new
page. Depth here is page-shell markers the JS hangs off + the verbatim
copy the spec pins; the endpoints themselves are covered in
tests/test_admin_sharepoint.py.

TCRD-240 (subfolder browsing + client filter + server search) adds a
second layer: `TestSharePointWizardStep2Behavior` runs the SHIPPED step-2
JS under node against stubbed `document`/`fetch` — same idiom as
`tests/test_chat_files_drawer_ui.py` (no DOM harness in CI; assertions are
about the handlers' observable calls and the raw rendered HTML string,
not simulated pixels)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def _page(seeded_app) -> str:
    c = seeded_app["client"]
    return c.get(
        "/admin/data-sources",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
    ).text


class TestSharePointIsASourceType:
    def test_picker_offers_sharepoint_alongside_the_others(self, seeded_app):
        body = _page(seeded_app)
        assert 'data-wsrc="sharepoint"' in body
        for src in ("keboola", "bigquery", "csv", "jira"):
            assert f'data-wsrc="{src}"' in body

    def test_no_new_nav_item(self, seeded_app):
        """Reached only from the existing /admin/data-sources picker — no
        sidebar/nav link anywhere else points at a SharePoint-only page."""
        c = seeded_app["client"]
        nav = c.get("/dashboard", headers={"Authorization": f"Bearer {seeded_app['admin_token']}"}).text
        assert "sharepoint" not in nav.lower()


class TestThreeStepDrawer:
    def test_drawer_has_exactly_connect_scope_share(self, seeded_app):
        body = _page(seeded_app)
        assert 'id="sp-wizard-overlay"' in body
        assert 'data-spw-step="1"' in body and ">Connect<" in body
        assert 'data-spw-step="2"' in body and ">Scope<" in body
        assert 'data-spw-step="3"' in body and ">Share<" in body
        # The table wizard's fourth ("Bundle") step does not appear in this drawer.
        sp_drawer = body.split('id="sp-wizard-overlay"', 1)[1].split("</script>", 1)[0]
        assert "Bundle" not in sp_drawer

    def test_step1_fields_are_tenant_and_client_id(self, seeded_app):
        """Exact foreign values as FIELDS, never through conversation."""
        body = _page(seeded_app)
        assert 'id="spw-tenant"' in body
        assert 'id="spw-client"' in body
        assert "Tenant ID" in body
        assert "Client (application) ID" in body

    def test_step1_certificate_choice_vault_or_env(self, seeded_app):
        body = _page(seeded_app)
        assert 'id="spw-cert-pem"' in body  # upload-to-vault path
        assert 'id="spw-cert-env-name"' in body  # server env-name path
        assert "never echoed back" in body

    def test_step2_has_anonymize_column_and_the_verbatim_note(self, seeded_app):
        body = _page(seeded_app)
        assert "anonymize" in body
        # The exact note text the spec requires (§13.2).
        assert "original file is not copied" in body
        assert "stores the extracted markdown" in body
        assert "open in the source" in body.lower()

    def test_step3_has_group_badges_and_no_group_warning(self, seeded_app):
        body = _page(seeded_app)
        assert "indexed but invisible" in body
        assert 'id="spw-share-rows"' in body

    def test_step3_has_corpus_map_download(self, seeded_app):
        body = _page(seeded_app)
        assert 'id="spw-corpus-map-link"' in body
        assert "corpus-map" in body


class TestStep2MarkupTCRD240:
    """Step-2 page-shell markers for subfolder browsing (#1), the
    client-side filter (#2) and the server search box (#3/#4). Behavior is
    covered by `TestSharePointWizardStep2Behavior` below."""

    def test_filter_input_present_above_the_tree(self, seeded_app):
        body = _page(seeded_app)
        assert 'id="spw-tree-filter"' in body
        pane2 = body.split('data-spw-pane="2"', 1)[1].split('data-spw-pane="3"', 1)[0]
        # The filter sits ABOVE the tree host in document order.
        assert pane2.index('id="spw-tree-filter"') < pane2.index('id="spw-tree"')

    def test_search_box_present_with_mode_select(self, seeded_app):
        body = _page(seeded_app)
        assert 'id="spw-search-q"' in body
        assert 'id="spw-search-mode"' in body
        assert 'id="spw-search-btn"' in body
        assert 'id="spw-search-results"' in body
        assert 'id="spw-search-truncated"' in body
        for opt in ('"prefix"', '"contains"', '"glob"'):
            assert opt in body


TEMPLATE = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html"


def _node_run(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _sp_step2_slice() -> str:
    """The shipped SharePoint wizard JS (state, step-1 wiring, step-2 tree
    render/filter/server search, and step-3 share-preview render), sliced
    from the template so these tests run the REAL shipped functions rather
    than a copy that can drift. The slice runs through the END of the
    wizard's own ``<script>`` block — step 1 and step 3 wiring ride along
    even for a test that only exercises step 2 (it's all one contiguous
    block) but every DOM id any of it reaches for resolves to a safe no-op
    stub (see ``_HARNESS_PREAMBLE``), so it costs nothing to include and
    keeps the slice a single honest contiguous range rather than a
    hand-picked patchwork.
    """
    html = TEMPLATE.read_text(encoding="utf-8")
    start = html.index('const SP_CONN_API = "/api/admin/source-connections";')
    end = html.index("</script>", start)
    return html[start:end]


#: Stubs for everything the sliced block reaches out to. Any DOM id NOT
#: explicitly given special behavior below resolves to a safe generic
#: element (so the many `document.getElementById(...).addEventListener(...)`
#: top-level registrations in the slice never throw) — same idiom as
#: `tests/test_chat_files_drawer_ui.py`'s `_HARNESS_PREAMBLE`.
_HARNESS_PREAMBLE = """
function genericEl() {
  const el = {
    value: "", disabled: false, checked: false, textContent: "",
    dataset: {}, style: {},
    classList: { add(){}, remove(){}, toggle(){} },
    _handlers: {},
    addEventListener(evt, fn) { (el._handlers[evt] = el._handlers[evt] || []).push(fn); },
    dispatchEvent(e) {
      ((el._handlers[(e && e.type) || "change"]) || []).slice().forEach((fn) => fn(e));
    },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    focus() {}, click() {},
  };
  Object.defineProperty(el, "innerHTML", {
    get() { return el._html || ""; },
    set(v) { el._html = v; },
  });
  return el;
}

const _elements = {};
function el(id) { return _elements[id] || (_elements[id] = genericEl()); }
// The tail of the sliced script (step-3 close/cancel wiring) registers a
// document-level keydown handler and does two `document.querySelectorAll(
// ...).forEach(...)` close-button sweeps — no-op stubs so the slice can
// extend through the end of the wizard's <script> block without throwing
// at load.
const document = {
  getElementById(id) { return el(id); },
  addEventListener() {},
  querySelectorAll() { return []; },
};

let _fetchCalls = [];
let _nextSearchResponse = null;
global.fetch = async (url, opts) => {
  opts = opts || {};
  _fetchCalls.push({ url: String(url), opts });
  if (opts.method === "DELETE") return { ok: true, status: 204, json: async () => null };
  if (String(url).indexOf("/tree/search") !== -1) {
    const body = _nextSearchResponse || { matches: [], visited: 0, truncated: false };
    return { ok: true, status: 200, json: async () => body };
  }
  if (opts.method !== "POST") {
    // A plain GET .../tree call (e.g. the drill handler's own spLoadTree())
    // that this test file has no reason to assert on — an empty, harmless
    // level so the caller's `.then((body) => { spItems = body.items || [];
    // spRenderTree(body.level); })` never throws or dangles an unhandled
    // rejection.
    return { ok: true, status: 200, json: async () => ({ level: "items", items: [] }) };
  }
  const reqBody = JSON.parse(opts.body);
  const scope = {
    source_scope_id: reqBody.source_scope_id,
    display_path: reqBody.display_path,
    anonymize: !!reqBody.anonymize,
    collection_id: "coll-" + reqBody.source_scope_id,
    collection: { id: "coll-" + reqBody.source_scope_id, slug: "slug-" + reqBody.source_scope_id, name: "N" },
    group_ids: [],
    no_group_warning: true,
  };
  return { ok: true, status: 201, json: async () => scope };
};

async function _settle() {
  // Let spConfirmScope's fetch().then(...).then(...) chain fully resolve.
  await new Promise((r) => setTimeout(r, 20));
}
"""


def _run(body: str) -> dict:
    script = _HARNESS_PREAMBLE + _sp_step2_slice() + "\n(async () => {\n" + body + "\n})();\n"
    return json.loads(_node_run(script))


class TestSharePointWizardStep2Behavior:
    """The SHIPPED step-2 functions executed for real under node — a future
    edit that keeps every string literal `TestStep2MarkupTCRD240` pins
    intact while breaking the actual filter/highlight/search/select-all
    logic would pass those tests and fail these."""

    def test_current_level_follows_site_drive_item_precedence(self):
        result = _run(
            """
            spLevel = { site_id: null, drive_id: null, item_id: null };
            const sites = spCurrentLevel();
            spLevel = { site_id: "s1", drive_id: null, item_id: null };
            const drives = spCurrentLevel();
            spLevel = { site_id: "s1", drive_id: "d1", item_id: null };
            const driveRoot = spCurrentLevel();
            spLevel = { site_id: "s1", drive_id: "d1", item_id: "f1" };
            const subfolder = spCurrentLevel();
            process.stdout.write(JSON.stringify({ sites, drives, driveRoot, subfolder }));
            """
        )
        assert result == {"sites": "sites", "drives": "drives", "driveRoot": "items", "subfolder": "items"}

    def test_filter_is_case_and_diacritics_insensitive(self):
        result = _run(
            """
            spTreeFilterQuery = "Contracts";
            const exactMatch = spRowMatchesFilter("Contracts");
            const noMatch = spRowMatchesFilter("Invoices");
            spTreeFilterQuery = "CONTRACTS";
            const caseInsensitive = spRowMatchesFilter("contracts 2026");
            spTreeFilterQuery = "elektrina";
            const diacriticsInsensitive = spRowMatchesFilter("Elektřina");
            spTreeFilterQuery = "";
            const emptyQueryMatchesEverything = spRowMatchesFilter("anything");
            process.stdout.write(JSON.stringify({
              exactMatch, noMatch, caseInsensitive, diacriticsInsensitive, emptyQueryMatchesEverything,
            }));
            """
        )
        assert result == {
            "exactMatch": True,
            "noMatch": False,
            "caseInsensitive": True,
            "diacriticsInsensitive": True,
            "emptyQueryMatchesEverything": True,
        }

    def test_highlight_wraps_the_matched_substring_in_mark(self):
        result = _run(
            """
            spTreeFilterQuery = "conf";
            const highlighted = spHighlight("Configs");
            spTreeFilterQuery = "";
            const plainWhenNoFilter = spHighlight("Configs");
            spTreeFilterQuery = "zzz";
            const plainWhenNoMatch = spHighlight("Configs");
            process.stdout.write(JSON.stringify({ highlighted, plainWhenNoFilter, plainWhenNoMatch }));
            """
        )
        assert result["highlighted"] == "<mark>Conf</mark>igs"
        assert result["plainWhenNoFilter"] == "Configs"
        assert result["plainWhenNoMatch"] == "Configs"

    def test_filter_narrows_rendered_tree_rows_and_restores_when_cleared(self):
        """`spRenderTree` — the function the filter input's own `input`
        handler re-invokes — must mark non-matching rows `hidden` and
        highlight the match in the ones it keeps, and clearing the query
        must show everything again with no leftover `hidden`/`<mark>`."""
        result = _run(
            """
            spItems = [
              { id: "f1", name: "Contracts", is_folder: true, child_count: 2 },
              { id: "f2", name: "Invoices", is_folder: true, child_count: 0 },
              { id: "f3", name: "readme.txt", is_folder: false, child_count: null },
            ];
            spLevel = { site_id: "s1", drive_id: "d1", item_id: null };
            spCrumbs = [];
            spScopes = {};

            spTreeFilterQuery = "";
            spRenderTree("items");
            const unfiltered = document.getElementById("spw-tree").innerHTML;

            spTreeFilterQuery = "con";
            spRenderTree("items");
            const filtered = document.getElementById("spw-tree").innerHTML;

            spTreeFilterQuery = "";
            spRenderTree("items");
            const cleared = document.getElementById("spw-tree").innerHTML;

            process.stdout.write(JSON.stringify({ unfiltered, filtered, cleared }));
            """
        )
        assert "hidden" not in result["unfiltered"]
        assert "Contracts" in result["unfiltered"] and "Invoices" in result["unfiltered"]

        filtered = result["filtered"]
        assert "<mark>Con</mark>tracts" in filtered
        # "Invoices" and "readme.txt" don't match "con" — their rows carry `hidden`.
        row_f2 = filtered[filtered.index('data-spw-item="f2"') - 40 : filtered.index('data-spw-item="f2"') + 60]
        row_f3 = filtered[filtered.index('data-spw-item="f3"') - 40 : filtered.index('data-spw-item="f3"') + 60]
        assert "hidden" in row_f2
        assert "hidden" in row_f3
        # The still-matching "Contracts" row is NOT hidden.
        row_f1 = filtered[filtered.index('data-spw-item="f1"') - 40 : filtered.index('data-spw-item="f1"') + 200]
        assert "hidden" not in row_f1.split("</div>")[0]

        assert "hidden" not in result["cleared"]
        assert "<mark>" not in result["cleared"]

    def test_subfolder_drill_uses_item_id_at_the_items_level(self):
        """TCRD-240: drilling into a FOLDER at the "items" level (not just
        sites/drives) must set `item_id` on the next tree call and push a
        crumb carrying it — the pre-existing contract stopped navigation at
        the drive root. `host.querySelectorAll` is overridden with a fake
        drill button (constructed the same way the search-results tests
        build fake checkboxes) so the REAL click handler `spRenderTree`
        registers actually runs."""
        result = _run(
            """
            spConnId = "conn-1";
            spItems = [{ id: "sub1", name: "Subfolder", is_folder: true, child_count: 1 }];
            spLevel = { site_id: "s1", drive_id: "d1", item_id: "parent1" };
            spCrumbs = [{ label: "Site", site_id: "s1", drive_id: null, item_id: null },
                        { label: "Docs", site_id: "s1", drive_id: "d1", item_id: null },
                        { label: "Parent", site_id: "s1", drive_id: "d1", item_id: "parent1" }];
            spScopes = {};
            spTreeFilterQuery = "";

            const drillBtn = genericEl();
            drillBtn.dataset = { spwDrill: "sub1", spwName: "Subfolder" };
            const host = document.getElementById("spw-tree");
            host.querySelectorAll = (sel) => (sel === "[data-spw-drill]" ? [drillBtn] : []);

            spRenderTree("items");
            drillBtn.dispatchEvent({ type: "click" });

            process.stdout.write(JSON.stringify({
              level: spLevel,
              lastCrumb: spCrumbs[spCrumbs.length - 1],
              crumbCount: spCrumbs.length,
            }));
            """
        )
        assert result["level"] == {"site_id": "s1", "drive_id": "d1", "item_id": "sub1"}
        assert result["lastCrumb"] == {"label": "Subfolder", "site_id": "s1", "drive_id": "d1", "item_id": "sub1"}
        assert result["crumbCount"] == 4

    def test_search_results_render_with_select_all_and_it_populates_the_basket(self):
        """The headline behavior (#3/#4): search results render with a
        "Select all (N)" control, and checking it fires an ordinary
        `spConfirmScope` (a POST to `.../scopes`) for every result — bulk
        select IS just N ordinary confirms, never a separate bulk endpoint."""
        result = _run(
            """
            spConnId = "conn-1";
            spScopes = {};
            const matches = [
              { item_id: "f1", drive_id: "d1", display_path: "Site / Docs / Contracts" },
              { item_id: "f2", drive_id: "d1", display_path: "Site / Docs / Invoices" },
            ];

            function mkRowFake(m) {
              const row = { dataset: { spwSrPath: m.display_path } };
              const selectCb = genericEl();
              selectCb.dataset = { spwSrSelect: m.item_id };
              selectCb.closest = (sel) => (sel === "[data-spw-sr]" ? row : null);
              const anonCb = genericEl();
              anonCb.dataset = { spwSrAnon: m.item_id };
              anonCb.closest = (sel) => (sel === "[data-spw-sr]" ? row : null);
              row.querySelector = (sel) => (sel === "[data-spw-sr-anon]" ? anonCb : null);
              return { selectCb, anonCb };
            }
            const fakes = matches.map(mkRowFake);
            const selectCbs = fakes.map((f) => f.selectCb);
            const anonCbs = fakes.map((f) => f.anonCb);

            const host = document.getElementById("spw-search-results");
            host.querySelectorAll = (sel) => {
              if (sel === "[data-spw-sr-select]") return selectCbs;
              if (sel === "[data-spw-sr-anon]") return anonCbs;
              return [];
            };

            spRenderSearchResults(matches);
            const html = host.innerHTML;
            const selectAllLabelPresent = html.indexOf("Select all (2)") !== -1;
            const initiallyUnchecked = selectCbs.every((c) => c.checked === false);

            const selectAllEl = document.getElementById("spw-search-select-all");
            selectAllEl.checked = true;
            selectAllEl.dispatchEvent({ type: "change", target: selectAllEl });
            await _settle();

            process.stdout.write(JSON.stringify({
              selectAllLabelPresent,
              initiallyUnchecked,
              selectCbsCheckedAfter: selectCbs.map((c) => c.checked),
              fetchCalls: _fetchCalls.map((c) => ({
                method: c.opts.method || "GET",
                body: c.opts.body ? JSON.parse(c.opts.body) : null,
              })),
            }));
            """
        )
        assert result["selectAllLabelPresent"] is True
        assert result["initiallyUnchecked"] is True
        assert result["selectCbsCheckedAfter"] == [True, True]
        posts = [c for c in result["fetchCalls"] if c["method"] == "POST"]
        assert len(posts) == 2, "select-all must confirm EVERY result — N ordinary confirms"
        confirmed_ids = sorted(p["body"]["source_scope_id"] for p in posts)
        assert confirmed_ids == ["f1", "f2"]
        confirmed_paths = {p["body"]["source_scope_id"]: p["body"]["display_path"] for p in posts}
        assert confirmed_paths == {"f1": "Site / Docs / Contracts", "f2": "Site / Docs / Invoices"}

    def test_search_results_reflect_already_confirmed_scopes(self):
        result = _run(
            """
            spConnId = "conn-1";
            spScopes = {
              f1: { source_scope_id: "f1", display_path: "X", anonymize: true,
                    collection: { id: "c1", slug: "my-slug", name: "My" } },
            };
            const matches = [{ item_id: "f1", drive_id: "d1", display_path: "Site / Docs / Contracts" }];
            const host = document.getElementById("spw-search-results");
            spRenderSearchResults(matches);
            process.stdout.write(JSON.stringify({ html: host.innerHTML }));
            """
        )
        assert "checked" in result["html"]
        assert "my-slug" in result["html"]
        assert "anon" in result["html"]

    def test_no_matches_shows_an_empty_state(self):
        result = _run(
            """
            const host = document.getElementById("spw-search-results");
            spRenderSearchResults([]);
            process.stdout.write(JSON.stringify({ html: host.innerHTML, displayed: host.style.display }));
            """
        )
        assert "No folders matched" in result["html"]

    def test_query_shorter_than_two_chars_never_calls_fetch(self):
        result = _run(
            """
            document.getElementById("spw-search-q").value = "a";
            spRunSearch();
            await _settle();
            const errEl = document.getElementById("spw-search-error");
            process.stdout.write(JSON.stringify({
              fetchCalls: _fetchCalls.length,
              errorShown: errEl.style.display === "block",
              errorText: errEl.textContent,
            }));
            """
        )
        assert result["fetchCalls"] == 0
        assert result["errorShown"] is True
        assert "2 characters" in result["errorText"]

    def test_truncation_banner_shows_exactly_when_the_response_says_truncated(self):
        result = _run(
            """
            spConnId = "conn-1";
            document.getElementById("spw-search-q").value = "ab";
            document.getElementById("spw-search-mode").value = "prefix";

            _nextSearchResponse = {
              matches: [], visited: 500, truncated: true,
              hint: "Scope the search to a site or folder, or narrow the pattern.",
            };
            spRunSearch();
            await _settle();
            const truncEl = document.getElementById("spw-search-truncated");
            const truncatedShown = truncEl.style.display === "block";
            const truncatedText = truncEl.textContent;

            _nextSearchResponse = { matches: [], visited: 3, truncated: false, hint: null };
            spRunSearch();
            await _settle();
            const notTruncatedShown = truncEl.style.display === "block";

            process.stdout.write(JSON.stringify({ truncatedShown, truncatedText, notTruncatedShown }));
            """
        )
        assert result["truncatedShown"] is True
        assert "500" in result["truncatedText"]
        # The enriched banner: the visited count AND the server's own hint,
        # verbatim — not a client-side guess at what to do next.
        assert "Scope the search to a site or folder, or narrow the pattern." in result["truncatedText"]
        assert result["notTruncatedShown"] is False

    def test_truncation_banner_falls_back_to_generic_hint_when_response_omits_it(self):
        """Defensive fallback for a response shape this build has never
        seen (e.g. an older server) — the banner must still say SOMETHING
        actionable, never blank."""
        result = _run(
            """
            spConnId = "conn-1";
            document.getElementById("spw-search-q").value = "ab";
            document.getElementById("spw-search-mode").value = "prefix";
            _nextSearchResponse = { matches: [], visited: 20000, truncated: true };
            spRunSearch();
            await _settle();
            const truncEl = document.getElementById("spw-search-truncated");
            process.stdout.write(JSON.stringify({ text: truncEl.textContent }));
            """
        )
        assert "20000" in result["text"]
        assert len(result["text"]) > len("Stopped after 20000 folder(s) visited. ")


class TestUniquePermissionsBadgeUI:
    """ADVISORY-ONLY badge (Decision #2) rendered by the SHIPPED
    `spRenderTree` — present only for a bare `true`, absent for `false` AND
    for "unknown" (unset/`null`) alike, since a probe failure must never
    read as a reassurance."""

    def test_badge_present_only_for_true_absent_for_false_and_unknown(self):
        result = _run(
            """
            spItems = [
              { id: "f1", name: "Legal", is_folder: true, child_count: 0 },
              { id: "f2", name: "Ordinary", is_folder: true, child_count: 0 },
              { id: "f3", name: "NeverProbed", is_folder: true, child_count: 0 },
            ];
            spLevel = { site_id: "s1", drive_id: "d1", item_id: null };
            spCrumbs = [];
            spScopes = {};
            spUniquePerms = { f1: true, f2: false };
            // f3 deliberately absent from spUniquePerms — the "never even
            // attempted" unknown, distinct from f2's explicit `false`.

            spRenderTree("items");
            const html = document.getElementById("spw-tree").innerHTML;
            process.stdout.write(JSON.stringify({ html }));
            """
        )
        html = result["html"]
        row_f1 = html[html.index('data-spw-item="f1"') : html.index('data-spw-item="f2"')]
        row_f2 = html[html.index('data-spw-item="f2"') : html.index('data-spw-item="f3"')]
        row_f3 = html[html.index('data-spw-item="f3"') :]
        assert "sp-badge--unique-perms" in row_f1
        assert "unique permissions" in row_f1
        # Honesty requirement: the badge text never claims enforcement.
        assert "enforce" not in row_f1.lower()
        assert "sp-badge--unique-perms" not in row_f2
        assert "sp-badge--unique-perms" not in row_f3

    def test_badge_tooltip_text_is_advisory_not_an_enforcement_claim(self):
        result = _run(
            """
            spItems = [{ id: "f1", name: "Legal", is_folder: true, child_count: 0 }];
            spLevel = { site_id: "s1", drive_id: "d1", item_id: null };
            spCrumbs = [];
            spScopes = {};
            spUniquePerms = { f1: true };
            spRenderTree("items");
            process.stdout.write(JSON.stringify({ html: document.getElementById("spw-tree").innerHTML }));
            """
        )
        html = result["html"]
        assert "people who can't open it there may still see its content once this scope is shared here" in html


class TestSharePointWizardStep3AdvisorySummary:
    """The share step's one-line advisory summary — shown only when at
    least one confirmed scope was flagged `true` while browsing THIS
    session (spUniquePerms); silent otherwise. Runs the shipped
    `spRenderShare` for real."""

    def test_summary_shown_when_a_selected_scope_was_flagged(self):
        result = _run(
            """
            spGroups = [];
            spPendingGroups = {};
            spUniquePerms = { "drive:legal": true };
            const items = [
              { source_scope_id: "drive:legal", display_path: "Legal", anonymize: false,
                anonymization_declared: false, collection: { id: "c1", slug: "legal", name: "Legal" },
                group_ids: [], no_group_warning: true },
            ];
            spRenderShare(items);
            process.stdout.write(JSON.stringify({ html: document.getElementById("spw-share-rows").innerHTML }));
            """
        )
        html = result["html"]
        assert "apg-strip--warn" in html
        assert "unique permissions in the source" in html
        assert "does not read or enforce" in html

    def test_summary_absent_when_no_selected_scope_was_flagged(self):
        result = _run(
            """
            spGroups = [];
            spPendingGroups = {};
            spUniquePerms = {};
            const items = [
              { source_scope_id: "drive:ordinary", display_path: "Ordinary", anonymize: false,
                anonymization_declared: false, collection: { id: "c2", slug: "ordinary", name: "Ordinary" },
                group_ids: [], no_group_warning: true },
            ];
            spRenderShare(items);
            process.stdout.write(JSON.stringify({ html: document.getElementById("spw-share-rows").innerHTML }));
            """
        )
        assert "apg-strip--warn" not in result["html"]

    def test_summary_counts_only_flagged_scopes_not_all_selected(self):
        result = _run(
            """
            spGroups = [];
            spPendingGroups = {};
            spUniquePerms = { "drive:legal": true, "drive:ordinary": false };
            const items = [
              { source_scope_id: "drive:legal", display_path: "Legal", anonymize: false,
                anonymization_declared: false, collection: { id: "c1", slug: "legal", name: "Legal" },
                group_ids: [], no_group_warning: true },
              { source_scope_id: "drive:ordinary", display_path: "Ordinary", anonymize: false,
                anonymization_declared: false, collection: { id: "c2", slug: "ordinary", name: "Ordinary" },
                group_ids: [], no_group_warning: true },
            ];
            spRenderShare(items);
            process.stdout.write(JSON.stringify({ html: document.getElementById("spw-share-rows").innerHTML }));
            """
        )
        html = result["html"]
        assert "1 of your selected scope" in html
