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
        assert 'id="spw-search-skipped"' in body
        for opt in ('"prefix"', '"contains"', '"glob"'):
            assert opt in body


class TestStep2SavedScopesAndGuidanceMarkup:
    """Page-shell markers for the two 2026-09-01 fixes below: the "Saved
    scopes" panel (`TestSavedScopesPanel`) and the discovery-forbidden
    guidance box (`TestSavedSiteScopeVisibleOnReopen.
    test_saved_site_renders_when_discovery_is_forbidden`)."""

    def test_saved_scopes_panel_present_above_the_search_box(self, seeded_app):
        body = _page(seeded_app)
        pane2 = body.split('data-spw-pane="2"', 1)[1].split('data-spw-pane="3"', 1)[0]
        assert 'id="spw-saved-scopes"' in pane2
        assert pane2.index('id="spw-saved-scopes"') < pane2.index('id="spw-search-q"'), (
            "reopening the wizard must not look like a blank slate — the saved-scope "
            "panel has to be visible before anything else on the step"
        )

    def test_discovery_guidance_lives_inside_the_by_url_box(self, seeded_app):
        body = _page(seeded_app)
        wrap_idx = body.index('id="spw-site-by-url-wrap"')
        guidance_idx = body.index('id="spw-discovery-guidance"', wrap_idx)
        input_idx = body.index('id="spw-site-by-url"', guidance_idx)
        assert wrap_idx < guidance_idx < input_idx, "guidance sits inside the by-URL box, above the input"
        # It must never share the drawer's generic failure styling.
        guidance_tag = body[guidance_idx - 60 : guidance_idx + 40]
        assert "ds-wizard-error" not in guidance_tag


#: The wizard's own script moved into a dedicated static file wholesale
#: (perf follow-up, 2026-09-03) — the comment on `_sp_step2_slice` below
#: explains why that made the old "slice up to the enclosing `</script>`"
#: technique both unnecessary and unsafe (this file has none of its own).
SHAREPOINT_WIZARD_JS = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "web"
    / "static"
    / "js"
    / "admin"
    / "data_sources_sharepoint_wizard.js"
)


def _node_run(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _sp_step2_slice() -> str:
    """The shipped SharePoint wizard JS (state, step-1 wiring, step-2 tree
    render/filter/server search, and step-3 share-preview render) — the
    REAL shipped functions, not a copy that can drift.

    Was a slice from the template up to the enclosing `</script>` — the
    wizard's script is now its own dedicated static file (perf follow-up,
    2026-09-03: `app/web/static/js/admin/data_sources_sharepoint_wizard.js`,
    extracted wholesale since the wizard was ALREADY self-contained, per this
    file's own module docstring), so the whole file IS the slice: no
    `</script>` marker to hunt for, and no risk of accidentally running past
    it into whatever static asset happens to load next. Step 1 and step 3
    wiring ride along even for a test that only exercises step 2 (it's all
    one contiguous file) but every DOM id any of it reaches for resolves to
    a safe no-op stub (see ``_HARNESS_PREAMBLE``), so it costs nothing to
    include.
    """
    return SHAREPOINT_WIZARD_JS.read_text(encoding="utf-8")


#: Stubs for everything the sliced block reaches out to. Any DOM id NOT
#: explicitly given special behavior below resolves to a safe generic
#: element (so the many `document.getElementById(...).addEventListener(...)`
#: top-level registrations in the slice never throw) — same idiom as
#: `tests/test_chat_files_drawer_ui.py`'s `_HARNESS_PREAMBLE`.
_HARNESS_PREAMBLE = """
function genericEl() {
  // Real (not no-op) class tracking — `sp-search--emphasis` (the
  // discovery-forbidden guidance) needs a `contains` check a test can
  // assert on, not a stub that silently drops every call.
  const _classes = new Set();
  const el = {
    value: "", disabled: false, checked: false, textContent: "",
    dataset: {}, style: {},
    classList: {
      add(...cls) { cls.forEach((c) => _classes.add(c)); },
      remove(...cls) { cls.forEach((c) => _classes.delete(c)); },
      toggle(c, force) {
        const on = force === undefined ? !_classes.has(c) : !!force;
        if (on) _classes.add(c); else _classes.delete(c);
      },
      contains(c) { return _classes.has(c); },
    },
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

// The page-level connection cache `spSeedManualSitesFromConnectionConfig`
// reads (`loadConnections()` populates it, in an EARLIER <script> block not
// part of this slice) — declared here, empty by default, so a test that
// wants to exercise the seeding just assigns to it before calling
// `spLoadScopesThenTree`.
let _connections = [];
// Same story for the page-level refresh functions the "Share & finish"
// success path calls (`refreshSourcePipelines().then(loadConnections)`) —
// defined in an EARLIER <script> block, not part of this slice.
global.refreshSourcePipelines = async () => {};
global.loadConnections = async () => {};

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
  if (String(url).indexOf("/manual-sites") !== -1) {
    const reqBody = JSON.parse(opts.body);
    const site = { id: "site-" + reqBody.site_url, name: reqBody.site_url, web_url: reqBody.site_url };
    return { ok: true, status: 201, json: async () => site };
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

    def test_skipped_sites_and_folders_are_reported_quietly_alongside_matches(self):
        """A site/folder this app registration cannot read (TCRD-240 skip
        hardening) must be visible to the admin, distinct from a real error
        AND from the truncation banner — matches from the readable sites
        still render normally."""
        result = _run(
            """
            spConnId = "conn-1";
            document.getElementById("spw-search-q").value = "contract";
            document.getElementById("spw-search-mode").value = "contains";

            _nextSearchResponse = {
              matches: [{ item_id: "c1", drive_id: "d1", display_path: "Open Site / Docs / Contracts" }],
              visited: 3, truncated: false,
              skipped: [
                { scope: "site", reason: "forbidden", status_code: 403,
                  site_id: "s2", site_name: "Blocked Site", drive_id: null, item_id: null, display_path: null },
              ],
            };
            spRunSearch();
            await _settle();
            const skipEl = document.getElementById("spw-search-skipped");
            const truncEl = document.getElementById("spw-search-truncated");
            const resultsHtml = document.getElementById("spw-search-results").innerHTML;

            process.stdout.write(JSON.stringify({
              skippedShown: skipEl.style.display === "block",
              skippedText: skipEl.textContent,
              truncatedShown: truncEl.style.display === "block",
              resultsHtml,
            }));
            """
        )
        assert result["skippedShown"] is True
        assert "Blocked Site" in result["skippedText"]
        # Quiet and factual, not an alarm — never labeled "error"/"failed".
        assert "error" not in result["skippedText"].lower()
        assert "fail" not in result["skippedText"].lower()
        # A permission gap is not a cap-truncated walk.
        assert result["truncatedShown"] is False
        assert "Open Site / Docs / Contracts" in result["resultsHtml"]

    def test_skipped_notice_is_absent_when_nothing_was_skipped(self):
        result = _run(
            """
            spConnId = "conn-1";
            document.getElementById("spw-search-q").value = "contract";
            document.getElementById("spw-search-mode").value = "contains";
            _nextSearchResponse = { matches: [], visited: 3, truncated: false, skipped: [] };
            spRunSearch();
            await _settle();
            const skipEl = document.getElementById("spw-search-skipped");
            process.stdout.write(JSON.stringify({ skippedShown: skipEl.style.display === "block" }));
            """
        )
        assert result["skippedShown"] is False


class TestSavedSiteScopeVisibleOnReopen:
    """Reopening "Manage scopes" on a connection with a saved SITE scope
    must show that site at the sites level even when the live listing
    cannot include it — under ``Sites.Selected`` discovery is 403-forbidden,
    and ``list_sites`` is first-page-only anyway. A site scope's
    ``source_scope_id`` IS the Graph site id ("host,siteCol,web" — the only
    scope id with commas)
    and its ``display_path`` IS the site name, so the row is rebuildable
    from the scope alone. Regression: the row only rendered when live
    discovery happened to list it, so a reopened wizard showed
    "Nothing here." with the saved site invisible."""

    _SITE_SCOPE = (
        '{ source_scope_id: "contoso.sharepoint.com,11111111-aaaa,22222222-bbbb",'
        ' display_path: "My Site", anonymize: false,'
        ' collection: { id: "c1", slug: "my-site", name: "My Site" }, group_ids: [] }'
    )

    def test_saved_site_renders_when_discovery_is_forbidden(self):
        result = _run(
            """
            spConnId = "conn-1";
            global.fetch = async (url) => {
              if (String(url).indexOf("/scopes") !== -1) {
                return { ok: true, status: 200, json: async () => ({ items: [%s] }) };
              }
              return { ok: false, status: 502, json: async () => ({
                detail: { error: "sharepoint_discovery_forbidden",
                          message: "Graph refused to list sites (HTTP 403)." } }) };
            };
            spLoadScopesThenTree();
            await _settle();
            process.stdout.write(JSON.stringify({
              html: document.getElementById("spw-tree").innerHTML,
              errShown: document.getElementById("spw-tree-error").style.display === "block",
              guidanceText: document.getElementById("spw-discovery-guidance").textContent,
              guidanceShown: document.getElementById("spw-discovery-guidance").style.display === "block",
              emphasis: el("spw-site-by-url-wrap").classList.contains("sp-search--emphasis"),
            }));
            """
            % self._SITE_SCOPE
        )
        html = result["html"]
        assert "My Site" in html, "the saved site must render at the sites level"
        assert "checked" in html, "the saved site's checkbox must reflect its confirmed state"
        assert "data-spw-drill" in html, "the rebuilt site row must stay navigable (id IS the site id)"
        assert "my-site" in html, "the scope's collection badge must ride along"
        # A Sites.Selected 403 is the intended least-privilege posture, not
        # a fault: it must read as guidance toward "Add a site by URL",
        # never as `.ds-wizard-error`'s red failure banner.
        assert result["errShown"] is False, "discovery-forbidden must never use the failure banner"
        assert "403" in result["guidanceText"]
        assert result["guidanceShown"] is True
        assert result["emphasis"] is True, "the by-URL box is the primary action in this state"

    def test_saved_site_merges_into_a_listing_that_omits_it(self):
        result = _run(
            """
            spConnId = "conn-1";
            global.fetch = async (url) => {
              if (String(url).indexOf("/scopes") !== -1) {
                return { ok: true, status: 200, json: async () => ({ items: [%s] }) };
              }
              return { ok: true, status: 200, json: async () => ({
                level: "sites", items: [{ id: "other.sharepoint.com,x,y", name: "Other Site" }] }) };
            };
            spLoadScopesThenTree();
            await _settle();
            process.stdout.write(JSON.stringify({ html: document.getElementById("spw-tree").innerHTML }));
            """
            % self._SITE_SCOPE
        )
        assert "Other Site" in result["html"]
        assert "My Site" in result["html"]

    def test_saved_site_already_listed_is_not_duplicated(self):
        result = _run(
            """
            spConnId = "conn-1";
            global.fetch = async (url) => {
              if (String(url).indexOf("/scopes") !== -1) {
                return { ok: true, status: 200, json: async () => ({ items: [%s] }) };
              }
              return { ok: true, status: 200, json: async () => ({
                level: "sites",
                items: [{ id: "contoso.sharepoint.com,11111111-aaaa,22222222-bbbb", name: "My Site" }] }) };
            };
            spLoadScopesThenTree();
            await _settle();
            process.stdout.write(JSON.stringify({ html: document.getElementById("spw-tree").innerHTML }));
            """
            % self._SITE_SCOPE
        )
        assert result["html"].count("data-spw-item=") == 1

    #: Every way the tree call can fail, as (error code, HTTP status). A
    #: saved site is local state — rebuilt from the scope row, never read
    #: from Graph — so NONE of these can be a reason to hide it. Live run
    #: 2026-09-01 found the original fix wired the rescue into the
    #: `discovery_forbidden` branch alone: a real instance failing with
    #: `cert_unresolved` still showed an empty Sites list next to a card
    #: reading "1 scope".
    _TREE_FAILURES = [
        ("sharepoint_cert_unresolved", 409),
        ("sharepoint_graph_error", 502),
        ("feature_disabled", 409),
    ]

    @pytest.mark.parametrize("error_code,status", _TREE_FAILURES)
    def test_saved_site_survives_every_tree_failure(self, error_code, status):
        result = _run(
            """
            spConnId = "conn-1";
            global.fetch = async (url) => {
              if (String(url).indexOf("/scopes") !== -1) {
                return { ok: true, status: 200, json: async () => ({ items: [%s] }) };
              }
              return { ok: false, status: %d, json: async () => ({
                detail: { error: "%s", message: "the listing failed" } }) };
            };
            spLoadScopesThenTree();
            await _settle();
            process.stdout.write(JSON.stringify({
              html: document.getElementById("spw-tree").innerHTML,
              errText: document.getElementById("spw-tree-error").textContent,
              errShown: document.getElementById("spw-tree-error").style.display === "block",
            }));
            """
            % (self._SITE_SCOPE, status, error_code)
        )
        assert "My Site" in result["html"], f"{error_code} must not hide the saved site"
        assert "checked" in result["html"]
        assert "data-spw-drill" in result["html"], "the rebuilt row stays navigable"
        # The row being real and the listing having failed are both true —
        # rescuing the row must never swallow the error that explains why
        # the rest of the tree is missing.
        assert result["errShown"] is True, f"{error_code} must still be reported"

    def test_a_failure_below_the_sites_level_shows_no_site_rows(self):
        """The rescue belongs to the sites level only. Failing while browsing
        a drive lists drives/folders — splicing site rows in there would
        answer a different question than the one the breadcrumb asks."""
        result = _run(
            """
            spConnId = "conn-1";
            global.fetch = async (url) => {
              if (String(url).indexOf("/scopes") !== -1) {
                return { ok: true, status: 200, json: async () => ({ items: [%s] }) };
              }
              return { ok: false, status: 502, json: async () => ({
                detail: { error: "sharepoint_graph_error", message: "boom" } }) };
            };
            spApi(`/api/admin/sharepoint/connections/conn-1/scopes`).then((body) => {
              spScopes = {};
              (body.items || []).forEach((s) => { spScopes[s.source_scope_id] = s; });
              spSeedManualSitesFromScopes();
            });
            await _settle();
            // Drilled into a drive: the level lists folders, not sites.
            spLevel = { site_id: "s1", drive_id: "d1", item_id: null };
            spCrumbs = [{ label: "Some Site", site_id: "s1", drive_id: null, item_id: null },
                        { label: "Documents", site_id: "s1", drive_id: "d1", item_id: null }];
            spLoadTree();
            await _settle();
            process.stdout.write(JSON.stringify({
              html: document.getElementById("spw-tree").innerHTML,
              errShown: document.getElementById("spw-tree-error").style.display === "block",
            }));
            """
            % self._SITE_SCOPE
        )
        assert "My Site" not in result["html"]
        assert "data-spw-item=" not in result["html"]
        assert result["errShown"] is True

    def test_folder_scope_never_fabricates_a_site_row(self):
        """A folder scope's id (a Graph item id, no commas) names no site —
        the sites level must stay honestly empty rather than invent an
        un-navigable row."""
        result = _run(
            """
            spConnId = "conn-1";
            global.fetch = async (url) => {
              if (String(url).indexOf("/scopes") !== -1) {
                return { ok: true, status: 200, json: async () => ({ items: [
                  { source_scope_id: "01ABCDEF123", display_path: "Site / Docs / X",
                    anonymize: false, collection: null, group_ids: [] },
                ] }) };
              }
              return { ok: false, status: 502, json: async () => ({
                detail: { error: "sharepoint_discovery_forbidden",
                          message: "Graph refused to list sites (HTTP 403)." } }) };
            };
            spLoadScopesThenTree();
            await _settle();
            process.stdout.write(JSON.stringify({ html: document.getElementById("spw-tree").innerHTML }));
            """
        )
        assert "01ABCDEF123" not in result["html"]
        assert "data-spw-item=" not in result["html"]


class TestSavedScopesPanel:
    """Live report, 2026-09-01: a confirmed FOLDER scope several levels
    deep in a site is invisible to every tree-rescue above — a folder
    scope's id names no site, so `spSeedManualSitesFromScopes` cannot
    rebuild a row for it (`test_folder_scope_never_fabricates_a_site_row`),
    and under `Sites.Selected` the live listing cannot reach it either. An
    operator who wanted to change its `anonymize` flag had to know the site
    URL from elsewhere, re-paste it, and re-walk the whole tree back down
    to the same folder. `spw-saved-scopes` renders every confirmed scope
    from the scope rows alone — independent of tree navigation entirely —
    with an anonymize checkbox wired to the same idempotent confirm path."""

    _FOLDER_SCOPE = (
        '{ source_scope_id: "01ABCDEF123", display_path: "Site / Docs / Contracts / 2026",'
        " anonymize: true, anonymization_declared: false,"
        ' collection: { id: "c1", slug: "contracts-2026", name: "Contracts 2026" }, group_ids: [] }'
    )

    def test_renders_a_folder_scope_the_tree_cannot_show_at_all(self):
        result = _run(
            """
            spConnId = "conn-1";
            global.fetch = async (url) => {
              if (String(url).indexOf("/scopes") !== -1) {
                return { ok: true, status: 200, json: async () => ({ items: [%s] }) };
              }
              return { ok: false, status: 502, json: async () => ({
                detail: { error: "sharepoint_discovery_forbidden",
                          message: "Graph refused to list sites (HTTP 403)." } }) };
            };
            spLoadScopesThenTree();
            await _settle();
            process.stdout.write(JSON.stringify({
              html: document.getElementById("spw-saved-scopes").innerHTML,
              shown: document.getElementById("spw-saved-scopes").style.display === "block",
              treeHtml: document.getElementById("spw-tree").innerHTML,
            }));
            """
            % self._FOLDER_SCOPE
        )
        assert result["shown"] is True
        html = result["html"]
        assert "Site / Docs / Contracts / 2026" in html, "the saved path must be visible without navigating the tree"
        assert "contracts-2026" in html, "the collection it maps to must ride along"
        assert "checked" in html, "the anonymize checkbox must reflect the saved state"
        # The tree itself still cannot show it (unchanged, pinned by
        # test_folder_scope_never_fabricates_a_site_row) — the panel is an
        # ADDITION, not a replacement for that honesty.
        assert "01ABCDEF123" not in result["treeHtml"]

    def test_hidden_when_the_connection_has_no_saved_scopes(self):
        result = _run(
            """
            spConnId = "conn-1";
            global.fetch = async (url) => {
              if (String(url).indexOf("/scopes") !== -1) {
                return { ok: true, status: 200, json: async () => ({ items: [] }) };
              }
              return { ok: true, status: 200, json: async () => ({ level: "sites", items: [] }) };
            };
            spLoadScopesThenTree();
            await _settle();
            process.stdout.write(JSON.stringify({
              shown: document.getElementById("spw-saved-scopes").style.display === "block",
              html: document.getElementById("spw-saved-scopes").innerHTML,
            }));
            """
        )
        assert result["shown"] is False
        assert result["html"] == ""

    def test_toggling_the_saved_anonymize_checkbox_confirms_without_group_ids(self):
        """Same idempotent path the tree's own anonymize checkbox uses
        (`spConfirmScope`, source_scope_id-keyed) — and the same contract:
        `group_ids` is never sent, so editing anonymize here can never
        silently revoke this scope's collection grants."""
        result = _run(
            """
            spConnId = "conn-1";
            spScopes = { "01ABCDEF123": %s };

            global.fetch = async (url, opts) => {
              _fetchCalls.push({ url: String(url), opts });
              const body = JSON.parse(opts.body);
              return { ok: true, status: 201, json: async () => (
                { source_scope_id: body.source_scope_id, display_path: body.display_path,
                  anonymize: !!body.anonymize, collection: null, group_ids: [] }
              ) };
            };

            spOnSavedAnonToggle({ dataset: { spwSavedAnon: "01ABCDEF123" }, checked: true });
            await _settle();
            process.stdout.write(JSON.stringify({ call: _fetchCalls[_fetchCalls.length - 1] }));
            """
            % self._FOLDER_SCOPE
        )
        call = result["call"]
        assert call["url"].endswith("/connections/conn-1/scopes")
        assert call["opts"]["method"] == "POST"
        body = json.loads(call["opts"]["body"])
        assert body == {
            "source_scope_id": "01ABCDEF123",
            "display_path": "Site / Docs / Contracts / 2026",
            "anonymize": True,
            # Round-tripped from the scope's own last-known values (this
            # fixture never set them, so they read as the server's own
            # defaults) — see TestReconfirmRoundTripsAlwaysPersistedFields
            # for the case where an existing drive_id must survive.
            "access_mode": "manual",
            "include_excluded_subtrees": False,
        }
        assert "group_ids" not in body, "an anonymize-only edit must never touch this scope's sharing"


class TestReconfirmRoundTripsAlwaysPersistedFields:
    """Live report, 2026-09-01: `ConfirmScopeBody`'s own docstring calls
    `access_mode`/`drive_id`/`include_excluded_subtrees` "always persisted
    on confirm" server-side — NOT "omitted means unchanged" like
    `group_ids`/`audience_classes` — so a caller that omits them is telling
    the server to blank them. The wizard's confirm calls only ever sent
    `source_scope_id`/`display_path`/`anonymize`(/`group_ids`), which
    silently wiped `drive_id` — and would have reverted a `mirrored` scope
    to `manual` — on every anonymize toggle AND on every single "Share &
    finish" click, not just the row an admin meant to touch. Without
    `drive_id` the built-in crawler cannot address a folder scope on Graph
    at all: the next crawl enumerated nothing for it while the run still
    reported `done`, with no visible error."""

    _EXISTING_FOLDER_SCOPE = (
        '{ source_scope_id: "01ABCDEF123", display_path: "Site / Docs / Contracts / 2026",'
        ' anonymize: false, access_mode: "manual", drive_id: "b!existingDriveId",'
        " include_excluded_subtrees: false,"
        ' collection: { id: "c1", slug: "contracts-2026", name: "Contracts 2026" }, group_ids: [] }'
    )

    def test_an_anonymize_only_edit_still_sends_the_scopes_existing_drive_id(self):
        result = _run(
            """
            spConnId = "conn-1";
            spScopes = { "01ABCDEF123": %s };

            global.fetch = async (url, opts) => {
              _fetchCalls.push({ url: String(url), opts });
              // Mirrors the real handler's idempotent-update contract:
              // echoes back exactly what was sent, plus the collection the
              // server resolves from the (unchanged) source_scope_id —
              // never taken from the request body at all.
              const body = JSON.parse(opts.body);
              return { ok: true, status: 201, json: async () => Object.assign(
                {}, body, { collection: { id: "c1", slug: "contracts-2026", name: "Contracts 2026" } }
              ) };
            };

            spConfirmScope("01ABCDEF123", "Site / Docs / Contracts / 2026", false);
            await _settle();
            process.stdout.write(JSON.stringify({
              call: _fetchCalls[_fetchCalls.length - 1],
              scopeAfter: spScopes["01ABCDEF123"],
            }));
            """
            % self._EXISTING_FOLDER_SCOPE
        )
        body = json.loads(result["call"]["opts"]["body"])
        assert body["drive_id"] == "b!existingDriveId", "an anonymize-only edit must not blank the scope's drive_id"
        assert body["access_mode"] == "manual"
        assert body["anonymize"] is False
        assert "group_ids" not in body
        # The server hands back a scope whose drive_id/access_mode/
        # collection are exactly what they were before this edit.
        after = result["scopeAfter"]
        assert after["drive_id"] == "b!existingDriveId"
        assert after["access_mode"] == "manual"
        assert after["collection"]["id"] == "c1"

    def test_share_and_finish_round_trips_every_scopes_own_drive_id(self):
        """ "Share & finish" re-confirms EVERY scope on the connection in one
        pass — the same round-trip must apply there too, or completing the
        wizard silently blanks `drive_id` on scopes nobody touched."""
        result = _run(
            """
            spConnId = "conn-1";
            spScopes = { "01ABCDEF123": %s };
            spPendingGroups = {};

            global.fetch = async (url, opts) => {
              _fetchCalls.push({ url: String(url), opts });
              return { ok: true, status: 201, json: async () => JSON.parse(opts.body) };
            };

            el("spw-finish-btn").dispatchEvent({ type: "click" });
            await _settle();
            process.stdout.write(JSON.stringify({ call: _fetchCalls[_fetchCalls.length - 1] }));
            """
            % self._EXISTING_FOLDER_SCOPE
        )
        body = json.loads(result["call"]["opts"]["body"])
        assert body["drive_id"] == "b!existingDriveId"
        assert body["access_mode"] == "manual"

    def test_a_brand_new_scope_omits_drive_id_and_access_mode(self):
        """A first-time pick has no prior state to round-trip — the
        server's own defaults (manual/null/false) still apply, exactly as
        before this fix."""
        result = _run(
            """
            spConnId = "conn-1";
            spScopes = {};

            global.fetch = async (url, opts) => {
              _fetchCalls.push({ url: String(url), opts });
              return { ok: true, status: 201, json: async () => JSON.parse(opts.body) };
            };

            spConfirmScope("new-item-id", "Site / Docs / New Folder", false);
            await _settle();
            process.stdout.write(JSON.stringify({ call: _fetchCalls[_fetchCalls.length - 1] }));
            """
        )
        body = json.loads(result["call"]["opts"]["body"])
        assert "drive_id" not in body
        assert "access_mode" not in body
        assert "include_excluded_subtrees" not in body


#: Enough of a DOM for the step-1 code paths the other harness blocks never
#: reach: `openSpWizard` writes `document.body.style`, and `spEnableStep`
#: uses `querySelector`, neither of which the shared preamble stubs.
_STEP1_DOM = """
document.body = { style: {} };
document.querySelector = (sel) => el("qs:" + sel);
global._syncDropdownRebuild = () => {};
const _CONN = {
  id: "conn-1", name: "SharePoint — test tenant",
  config: { tenant_id: "b6386aaa-b5c5-4d24-a9a9-337fb28d6d4f",
            client_id: "66a7be5c-3664-4e3e-8649-e4779da0706e" },
};
global.fetch = async (url) => {
  if (String(url).indexOf("/source-connections") !== -1) {
    return { ok: true, status: 200, json: async () => [_CONN] };
  }
  if (String(url).indexOf("/scopes") !== -1) {
    return { ok: true, status: 200, json: async () => ({ items: [] }) };
  }
  return { ok: true, status: 200, json: async () => ({ level: "sites", items: [] }) };
};
function step1State() {
  return {
    name: el("spw-name").value,
    tenant: el("spw-tenant").value,
    client: el("spw-client").value,
    nameReadOnly: !!el("spw-name").readOnly,
    tenantReadOnly: !!el("spw-tenant").readOnly,
    clientReadOnly: !!el("spw-client").readOnly,
    credentialShown: el("spw-credential-field").style.display !== "none",
    connectBtnShown: el("spw-connect-btn").style.display !== "none",
    pickerShown: el("spw-existing-picker").style.display !== "none",
  };
}
"""


class TestStep1ShowsTheBoundConnection:
    """Opening the wizard on an EXISTING connection must load that
    connection's saved values into step 1. Reported from a live instance
    (2026-09-01): managing scopes and then looking at the Connect step
    showed an empty new-tenant form — no connection name, no tenant, no
    client id — so nothing on screen said which connection was being
    edited. The values are already in the `/api/admin/source-connections`
    payload the card renders from, so step 1 was simply never told.

    They load read-only: the SharePoint card offers no connection editor,
    and a prefilled form whose button POSTs would create a duplicate
    rather than save an edit. Showing the truth is the fix; an editor is
    a separate feature.
    """

    def test_bound_wizard_loads_the_saved_values(self):
        result = _run(
            _STEP1_DOM
            + """
            openSpWizardForConnection("conn-1");
            await _settle();
            process.stdout.write(JSON.stringify(step1State()));
            """
        )
        assert result["name"] == "SharePoint — test tenant"
        assert result["tenant"] == "b6386aaa-b5c5-4d24-a9a9-337fb28d6d4f"
        assert result["client"] == "66a7be5c-3664-4e3e-8649-e4779da0706e"
        assert result["nameReadOnly"] is True
        assert result["tenantReadOnly"] is True
        assert result["clientReadOnly"] is True
        # The create-a-new-tenant affordances have no meaning here, and a
        # prefilled form under a "Connect & validate" button is a trap.
        assert result["credentialShown"] is False
        assert result["connectBtnShown"] is False
        # "Continue an existing connection" asks a question this drawer
        # already answered by being opened from that connection.
        assert result["pickerShown"] is False

    def test_new_connection_flow_is_untouched(self):
        result = _run(
            _STEP1_DOM
            + """
            openSpWizard();
            await _settle();
            process.stdout.write(JSON.stringify(step1State()));
            """
        )
        assert result["name"] == ""
        assert result["tenant"] == ""
        assert result["client"] == ""
        assert result["nameReadOnly"] is False
        assert result["credentialShown"] is True
        assert result["connectBtnShown"] is True
        assert result["pickerShown"] is True

    def test_bound_continue_to_scope_uses_the_bound_connection(self):
        """The picker is hidden while bound, so its value is whatever the
        listing preselected — "Continue to scope" must follow spConnId, not
        that. With several connections the preselection is another one
        entirely, which would silently scope the wrong source."""
        result = _run(
            _STEP1_DOM
            + """
            openSpWizardForConnection("conn-1");
            await _settle();
            el("spw-existing-select").value = "some-other-connection";
            const calls = [];
            spLoadScopesThenTree = () => calls.push("loadScopes");
            el("spw-existing-btn").dispatchEvent({ type: "click" });
            await _settle();
            process.stdout.write(JSON.stringify({ spConnId: spConnId, calls: calls }));
            """
        )
        assert result["spConnId"] == "conn-1"
        assert result["calls"] == ["loadScopes"]

    def test_reopening_for_a_new_connection_clears_the_bound_state(self):
        """The bound presentation must not leak into the next open — the
        drawer is reused, so a stale read-only prefilled form would make
        connecting a new tenant impossible."""
        result = _run(
            _STEP1_DOM
            + """
            openSpWizardForConnection("conn-1");
            await _settle();
            const bound = step1State();
            openSpWizard();
            await _settle();
            process.stdout.write(JSON.stringify({ bound: bound, after: step1State() }));
            """
        )
        assert result["bound"]["name"] == "SharePoint — test tenant"
        after = result["after"]
        assert after["name"] == ""
        assert after["tenant"] == ""
        assert after["nameReadOnly"] is False
        assert after["credentialShown"] is True
        assert after["connectBtnShown"] is True
        assert after["pickerShown"] is True


class TestManualSitePersistence:
    """2026-09-01 bug fix: a site added by URL used to live ONLY in the
    client-side `spManualSites` map (reset on every `openSpWizard()`),
    forcing a re-paste on every reopen. `spAddSiteByUrl` now POSTs to the
    persisting endpoint, and `spSeedManualSitesFromConnectionConfig` reads it
    back from the connection's own `config.manual_sites` — the same
    page-level `_connections` cache the rest of the page already fetches."""

    def test_add_site_by_url_posts_to_the_persisting_endpoint(self):
        result = _run(
            """
            spConnId = "conn-1";
            document.getElementById("spw-site-by-url").value = "https://contoso.sharepoint.com/sites/ProjectHub";
            spLevel = { site_id: null, drive_id: null, item_id: null };
            spItems = [];
            spScopes = {};

            global.fetch = async (url, opts) => {
              _fetchCalls.push({ url: String(url), opts });
              const body = JSON.parse(opts.body);
              return { ok: true, status: 201, json: async () => (
                { id: "s-by-url", name: "Project Hub", web_url: "https://contoso/x" }
              ) };
            };

            spAddSiteByUrl();
            await _settle();
            process.stdout.write(JSON.stringify({
              call: _fetchCalls[_fetchCalls.length - 1],
              manualSites: spManualSites,
              html: document.getElementById("spw-tree").innerHTML,
            }));
            """
        )
        call = result["call"]
        assert call["url"].endswith("/connections/conn-1/manual-sites")
        assert call["opts"]["method"] == "POST"
        assert json.loads(call["opts"]["body"]) == {"site_url": "https://contoso.sharepoint.com/sites/ProjectHub"}
        assert result["manualSites"]["s-by-url"] == {
            "id": "s-by-url",
            "name": "Project Hub",
            "web_url": "https://contoso/x",
        }
        assert "Project Hub" in result["html"]

    def test_reopening_seeds_manual_sites_from_the_connections_cache(self):
        """The persistence half: `_connections` (the SAME cache
        `loadConnections()` populated before the wizard could have been
        opened) carries `config.manual_sites` forward across a reopen — no
        re-paste needed."""
        result = _run(
            """
            spConnId = "conn-1";
            _connections = [
              { id: "conn-1", config: { manual_sites: [
                { id: "s-persisted", name: "Persisted Site", web_url: "https://contoso/p" },
              ] } },
            ];
            global.fetch = async (url) => {
              if (String(url).indexOf("/scopes") !== -1) {
                return { ok: true, status: 200, json: async () => ({ items: [] }) };
              }
              return { ok: true, status: 200, json: async () => ({ level: "sites", items: [] }) };
            };

            spLoadScopesThenTree();
            await _settle();
            process.stdout.write(JSON.stringify({
              manualSites: spManualSites,
              html: document.getElementById("spw-tree").innerHTML,
            }));
            """
        )
        assert result["manualSites"]["s-persisted"] == {
            "id": "s-persisted",
            "name": "Persisted Site",
            "web_url": "https://contoso/p",
        }
        assert "Persisted Site" in result["html"]

    def test_seeding_never_overwrites_a_row_already_placed_by_a_scope(self):
        """`spSeedManualSitesFromScopes` runs first and owns any id it
        placed — a stale `manual_sites` name for the SAME site id must never
        clobber the live, scope-derived one."""
        result = _run(
            """
            spConnId = "conn-1";
            _connections = [
              { id: "conn-1", config: { manual_sites: [
                { id: "contoso.sharepoint.com,aaa,bbb", name: "Stale Name", web_url: "https://x" },
              ] } },
            ];
            global.fetch = async (url) => {
              if (String(url).indexOf("/scopes") !== -1) {
                return { ok: true, status: 200, json: async () => ({ items: [
                  { source_scope_id: "contoso.sharepoint.com,aaa,bbb", display_path: "Live Name",
                    anonymize: false, collection: null, group_ids: [] },
                ] }) };
              }
              return { ok: true, status: 200, json: async () => ({ level: "sites", items: [] }) };
            };

            spLoadScopesThenTree();
            await _settle();
            process.stdout.write(JSON.stringify({ manualSites: spManualSites }));
            """
        )
        assert result["manualSites"]["contoso.sharepoint.com,aaa,bbb"]["name"] == "Live Name"

    def test_a_manual_unconfirmed_site_shows_a_forget_control(self):
        result = _run(
            """
            spItems = [{ id: "s1", name: "Manual Site" }];
            spScopes = {};
            spManualSites = { s1: { id: "s1", name: "Manual Site" } };
            spRenderTree("sites");
            process.stdout.write(JSON.stringify({ html: document.getElementById("spw-tree").innerHTML }));
            """
        )
        assert 'data-spw-unlink="s1"' in result["html"]

    def test_a_confirmed_scope_never_shows_the_forget_control(self):
        """A confirmed scope already has its own removal path — unticking
        the checkbox — so offering a second one here would only be
        confusing about which one an admin just used."""
        result = _run(
            """
            spItems = [{ id: "s1", name: "Confirmed Site" }];
            spScopes = { s1: { source_scope_id: "s1", display_path: "Confirmed Site", anonymize: false,
                               collection: { id: "c1", slug: "confirmed-site", name: "Confirmed Site" } } };
            spManualSites = { s1: { id: "s1", name: "Confirmed Site" } };
            spRenderTree("sites");
            process.stdout.write(JSON.stringify({ html: document.getElementById("spw-tree").innerHTML }));
            """
        )
        assert 'data-spw-unlink="s1"' not in result["html"]

    def test_the_forget_control_is_never_shown_below_the_sites_level(self):
        result = _run(
            """
            spItems = [{ id: "f1", name: "Folder", is_folder: true, child_count: 0 }];
            spScopes = {};
            spManualSites = { f1: { id: "f1", name: "Folder" } };
            spLevel = { site_id: "s1", drive_id: "d1", item_id: null };
            spCrumbs = [];
            spRenderTree("items");
            process.stdout.write(JSON.stringify({ html: document.getElementById("spw-tree").innerHTML }));
            """
        )
        assert "data-spw-unlink" not in result["html"]

    def test_clicking_forget_deletes_and_removes_the_row(self):
        result = _run(
            """
            spConnId = "conn-1";
            spItems = [{ id: "s1", name: "Manual Site" }];
            spScopes = {};
            spManualSites = { s1: { id: "s1", name: "Manual Site" } };

            const unlinkBtn = genericEl();
            unlinkBtn.dataset = { spwUnlink: "s1" };
            const host = document.getElementById("spw-tree");
            host.querySelectorAll = (sel) => (sel === "[data-spw-unlink]" ? [unlinkBtn] : []);

            spRenderTree("sites");
            unlinkBtn.dispatchEvent({ type: "click" });
            await _settle();

            process.stdout.write(JSON.stringify({
              call: _fetchCalls[_fetchCalls.length - 1],
              manualSites: spManualSites,
              items: spItems,
            }));
            """
        )
        call = result["call"]
        assert call["opts"]["method"] == "DELETE"
        assert call["url"].endswith("/manual-sites?site_id=s1")
        assert "s1" not in result["manualSites"]
        assert result["items"] == []


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


class TestCertificateFileUploadMarkup:
    """A real file picker next to the PEM textarea — pasting a multiline PEM
    is error-prone, and admins have the material as files. Both credential
    surfaces get one: wizard step 1 and the source card's rotate row."""

    def test_wizard_has_a_hidden_multi_file_input_and_a_button(self, seeded_app):
        body = _page(seeded_app)
        assert 'id="spw-cert-file"' in body
        tag = body.split('id="spw-cert-file"', 1)[1][:250]
        assert 'type="file"' in body.split('id="spw-cert-file"', 1)[0][-250:] + tag
        assert "multiple" in tag
        assert ".pem" in tag
        assert "Upload PEM file" in body

    def test_card_rotate_row_has_the_same_picker(self, seeded_app):
        # The card is a JS template literal — in the shipped PAGE SCRIPT
        # (perf follow-up, 2026-09-03: extracted to its own static asset),
        # not the HTML response itself. Fetched through the same client, the
        # way a browser loading the page would.
        c = seeded_app["client"]
        page_js = c.get(
            "/static/js/admin/data_sources_page.js",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        ).text
        assert "ds-sp-cert-file-" in page_js
        card_js = page_js.split("ds-sp-cert-row-", 1)[1]
        assert "spCertFilePicked(" in card_js


class TestPemFormatCheckAndFilePick:
    """The SHIPPED client-side format check + file-pick handler under node.
    Advisory only — the PUT endpoint's `validate_certificate_material` stays
    the authority — but it answers before any network call."""

    def test_format_check_verdicts(self):
        cases = _run(
            """
            process.stdout.write(JSON.stringify({
              ok: spPemFormatCheck("-----BEGIN CERTIFICATE-----\\nA\\n-----END CERTIFICATE-----\\n-----BEGIN PRIVATE KEY-----\\nB\\n-----END PRIVATE KEY-----"),
              missing_key: spPemFormatCheck("-----BEGIN CERTIFICATE-----\\nA\\n-----END CERTIFICATE-----"),
              missing_cert: spPemFormatCheck("-----BEGIN RSA PRIVATE KEY-----\\nB\\n-----END RSA PRIVATE KEY-----"),
              garbage: spPemFormatCheck("hello"),
              encrypted: spPemFormatCheck("-----BEGIN CERTIFICATE-----\\nA\\n-----END CERTIFICATE-----\\n-----BEGIN ENCRYPTED PRIVATE KEY-----\\nB\\n-----END ENCRYPTED PRIVATE KEY-----"),
            }));
            """
        )
        assert cases["ok"]["ok"] is True
        assert cases["missing_key"]["ok"] is False
        assert "PRIVATE KEY" in cases["missing_key"]["message"]
        assert cases["missing_cert"]["ok"] is False
        assert "CERTIFICATE" in cases["missing_cert"]["message"]
        assert cases["garbage"]["ok"] is False
        assert cases["encrypted"]["ok"] is False
        assert "unencrypted" in cases["encrypted"]["message"]

    def test_file_pick_concatenates_files_and_fills_the_textarea(self):
        result = _run(
            """
            const input = { files: [
              { text: async () => "-----BEGIN CERTIFICATE-----\\nA\\n-----END CERTIFICATE-----" },
              { text: async () => "-----BEGIN PRIVATE KEY-----\\nB\\n-----END PRIVATE KEY-----" },
            ] };
            await spCertFilePicked(input, "spw-cert-pem", "spw-cert-status");
            process.stdout.write(JSON.stringify({
              value: el("spw-cert-pem").value,
              status: el("spw-cert-status").textContent,
              display: el("spw-cert-status").style.display,
            }));
            """
        )
        assert "BEGIN CERTIFICATE" in result["value"]
        assert "BEGIN PRIVATE KEY" in result["value"]
        assert result["display"] != "none"
        assert "found" in result["status"]

    def test_file_pick_with_only_the_certificate_names_the_missing_key(self):
        result = _run(
            """
            const input = { files: [
              { text: async () => "-----BEGIN CERTIFICATE-----\\nA\\n-----END CERTIFICATE-----" },
            ] };
            await spCertFilePicked(input, "spw-cert-pem", "spw-cert-status");
            process.stdout.write(JSON.stringify({
              value: el("spw-cert-pem").value,
              status: el("spw-cert-status").textContent,
            }));
            """
        )
        # The textarea still shows what was read — the admin can add the key
        # file with a second pick; the status names what is missing.
        assert "BEGIN CERTIFICATE" in result["value"]
        assert "PRIVATE KEY" in result["status"]


class TestClientSecretWizardOption:
    def test_certificate_choice_offers_client_secret(self, seeded_app):
        body = _page(seeded_app)
        assert 'value="secret"' in body
        assert "Client secret" in body
        assert 'id="spw-client-secret"' in body
        secret_tag = (
            body.split('id="spw-client-secret"', 1)[0][-200:] + body.split('id="spw-client-secret"', 1)[1][:200]
        )
        assert 'type="password"' in secret_tag
