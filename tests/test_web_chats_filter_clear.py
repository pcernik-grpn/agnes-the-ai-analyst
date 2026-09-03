"""/chats' Show controls: a lifecycle STATE and independent ATTRIBUTES.

All · Pinned · Shared · Archived were one control, first as the engine's
single-select `segments` (a scope: exactly one on, so "my pinned ones in the
archive" was unaskable, and being a scope rather than a filter also cost them
the removable chip, the badge count and both Clear buttons) and then as one
OR-group of checkboxes, which was worse in a different way: `All` is a superset
of `Shared`, so ticking both said nothing that ticking `All` didn't, and `All`
only existed because a checkbox group already spends "nothing ticked" on "no
filter" — leaving the option named "everything" to actually mean "include the
archive too".

The four are not four of a kind. `Archived` is a lifecycle STATE — live or
archived, never both, hidden until asked for. `Pinned` and `Shared` are
ATTRIBUTES — independent flags a conversation carries on either side of that
state. So: two groups, ANDed.

    Show   ( ) Active  ( ) Archived  ( ) All      ← `status`, exclusive
           [ ] Pinned only  [ ] Shared only       ← independent toggles

Both are ordinary FACETS, which is what makes them behave like every other
filter on the page: chipped, counted, cleared. Three engine options carry what a
plain facet lacks — `exclusive` (radios; selecting replaces), `whenEmpty` (the
resting value, a condition rather than the absence of one, which a selection
REPLACES rather than narrows within), and `spansSearch` (#1974).

The engine really RUNS here — the shipped app/web/static/js/filter_toolbar.js,
node-executed over tests/fixtures/dom_shim.js. Asserting on its source would
prove Clear calls something named `clearFacets`, not that clicking Clear puts the
rows back.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "app" / "web" / "static" / "js" / "filter_toolbar.js"
SHIM = ROOT / "tests" / "fixtures" / "dom_shim.js"
CHATS_HTML = ROOT / "app" / "web" / "templates" / "chats.html"
CHATS_JS = ROOT / "app" / "web" / "static" / "js" / "chats_page.js"

# The /chats toolbar reduced to the parts under test: the Show radios, the two
# attribute toggles, one ordinary facet category, the chip row, the Filter
# button's badge and the two Clear buttons — built with the same ids, classes and
# data-* the template renders, so the engine is wired exactly as the page wires
# it.
_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const { doc, win, dispatch } = require(process.env.SHIM);

function el(tag, attrs, kids) {
  const e = doc.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'text') e.textContent = v;
    else if (k === 'class') e.className = v;
    else e.setAttribute(k, v);
  }
  (kids || []).forEach(k => e.appendChild(k));
  return e;
}
const opt = (type, facet, value, label, n, checked) =>
  el('label', { class: 'fbar-menu__opt' }, [
    el('input', Object.assign({ type: type, 'data-facet': facet, value: value },
                              checked ? { checked: 'checked' } : {})),
    el('span', { class: 'fbar-menu__opt-text', text: label }),
    el('span', { class: 'fbar-menu__opt-n', 'data-opt-count': value === '1' ? facet : value, text: String(n) }),
  ]);

const search = el('input', { id: 'ch-search', type: 'search' });
const chips = el('div', { id: 'ch-chips', class: 'fbar-chips' }); chips.hidden = true;
const badge = el('span', { class: 'fbar-filter__n' });
const filterBtn = el('button', { id: 'ch-filter-btn', class: 'fbar-filter__btn' }, [badge]);

// Group 1: the lifecycle state, as RADIOS with the resting one checked.
const statusGroup = el('div', { class: 'ch-viewsel', role: 'radiogroup' }, [
  opt('radio', 'status', 'active', 'Active', 4, true),
  opt('radio', 'status', 'archived', 'Archived', 3, false),
  opt('radio', 'status', 'all', 'All', 7, false),
]);
// Group 2: the attributes, as independent checkboxes.
const onlys = el('div', { class: 'ch-onlys' }, [
  opt('checkbox', 'pinned', '1', 'Pinned only', 3),
  opt('checkbox', 'shared', '1', 'Shared only', 1),
]);
const cat = el('div', { class: 'fbar-cat', 'data-cat': 'agent' }, [
  el('button', { class: 'fbar-cat__btn' }, [
    el('span', { class: 'fbar-cat__label', text: 'Agent' }),
    el('span', { class: 'fbar-cat__n', 'data-cat-count': 'agent', text: '0' }),
  ]),
  el('div', { class: 'fbar-cat__pop' }, [
    opt('checkbox', 'agent', 'default', 'Default', 4),
    opt('checkbox', 'agent', 'revenue', 'Revenue Analyst', 3),
  ]),
]);
const menuClear = el('button', { 'data-fbar-clear': '', text: 'Clear' });
const menu = el('div', { id: 'ch-filter-menu', class: 'fbar-menu fbar-menu--cats', role: 'menu' }, [
  el('p', { class: 'fbar-menu__title', id: 'ch-status-label', text: 'Show' }),
  statusGroup, onlys, cat,
  el('div', { class: 'fbar-menu__foot' }, [menuClear, el('button', { 'data-fbar-done': '', text: 'Done' })]),
]);
menu.hidden = true;

const count = el('span', { id: 'ch-count' });
const resetBtn = el('button', { 'data-fbar-reset': '', text: 'Clear filters' });
const noRes = el('div', { id: 'ch-noresults' }, [resetBtn]); noRes.hidden = true;

const list = el('div', { id: 'ch-list' });
// Four live, three archived. `arc_pin` is the row the old single control could
// never show: a PINNED conversation inside the archive. `pinned`/`shared` sit
// on the row independently of its state, exactly as the server writes them.
[
  { id: 'live', status: 'all|active', agent: 'default', name: 'live one' },
  { id: 'pin', status: 'all|active|', agent: 'revenue', name: 'pinned one', pinned: 1 },
  { id: 'shd', status: 'all|active', agent: 'default', name: 'shared one', shared: 1 },
  { id: 'pin_shd', status: 'all|active', agent: 'revenue', name: 'pinned shared one', pinned: 1, shared: 1 },
  { id: 'arc', status: 'all|archived', agent: 'default', name: 'archived one' },
  { id: 'arc_pin', status: 'all|archived', agent: 'revenue', name: 'archived pinned one', pinned: 1 },
  { id: 'arc2', status: 'all|archived', agent: 'default', name: 'archived two' },
].forEach(r => {
  const attrs = {
    class: 'ch-row', 'data-item-id': r.id, 'data-status': r.status.replace(/\|$/, ''),
    'data-agent': r.agent, 'data-name': r.name, 'data-search': r.name,
  };
  if (r.pinned) attrs['data-pinned'] = '1';
  if (r.shared) attrs['data-shared'] = '1';
  list.appendChild(el('div', attrs));
});

doc.appendChild(el('body', {}, [search, filterBtn, menu, count, chips, noRes, list]));

// The SHIPPED engine, run for real — no copy and no re-implementation.
const ctx = vm.createContext(Object.assign(win, {
  window: win, document: doc, console, setTimeout, clearTimeout,
}));
vm.runInContext(fs.readFileSync(process.env.ENGINE, 'utf8'), ctx);

// The same facet config chats_page.js passes (minus the page hooks this harness
// has no DOM for) — see the guard at the bottom of this file that pins them to
// each other.
const toolbar = ctx.FilterToolbar.init({
  rows: '#ch-list .ch-row',
  search: { el: '#ch-search', attr: 'data-search' },
  facets: [
    { key: 'status', attr: 'data-status', label: 'Show',
      multi: true, exclusive: true, whenEmpty: ['active'], spansSearch: true },
    { key: 'pinned', attr: 'data-pinned', label: 'Pinned only', toggle: true },
    { key: 'shared', attr: 'data-shared', label: 'Shared only', toggle: true },
    { key: 'agent', attr: 'data-agent', label: 'Agent' },
  ],
  filterBtn: '#ch-filter-btn', menu: '#ch-filter-menu', chips: '#ch-chips',
  count: { el: '#ch-count', noun: 'chat' },
  noResults: '#ch-noresults',
});

function state() {
  return {
    visible: list.children.filter(r => !r.hidden).map(r => r.getAttribute('data-item-id')),
    status: (statusGroup.querySelectorAll('input').filter(i => i.checked)[0] || {}).value || null,
    status_checked: statusGroup.querySelectorAll('input').filter(i => i.checked).length,
    onlys: onlys.querySelectorAll('input').filter(i => i.checked).map(i => i.getAttribute('data-facet')),
    chips_hidden: chips.hidden,
    chips: chips.children.filter(c => c.classList.contains('fbar-chip')).map(c => ({
      key: c.getAttribute('data-chip'),
      label: c.querySelector('.fbar-chip__label').textContent,
      values: (c.querySelector('.fbar-chip__vals') || { textContent: '' }).textContent,
      clear_label: c.querySelector('.fbar-chip__x').getAttribute('aria-label'),
    })),
    clear_all: chips.children.some(c => c.classList.contains('fbar-chips__clear')),
    badge: badge.textContent, badge_hidden: badge.hidden,
    filter_lit: filterBtn.classList.contains('is-active'),
    count: count.textContent,
    search: search.value,
    menu_open: !menu.hidden,
  };
}
const chipFor = key => chips.children.find(c => c.getAttribute('data-chip') === key);
const STEPS = {
  // A radio: the browser unchecks its siblings, so the harness does too before
  // firing change — anything less would test a DOM no browser produces.
  pick: value => {
    statusGroup.querySelectorAll('input').forEach(i => { i.checked = i.value === value; });
    dispatch(menu.querySelector('input[data-facet="status"][value="' + value + '"]'), 'change');
  },
  tick: (facet, on) => {
    const i = menu.querySelector('input[data-facet="' + facet + '"]');
    i.checked = on !== false; dispatch(i, 'change');
  },
  tick_value: (facet, value, on) => {
    const i = menu.querySelector('input[data-facet="' + facet + '"][value="' + value + '"]');
    i.checked = on !== false; dispatch(i, 'change');
  },
  api_set: (facet, value) => toolbar.setFacet(facet, value, true),
  menu_clear: () => dispatch(menuClear, 'click'),
  clear_all: () => dispatch(chips.children.find(c => c.classList.contains('fbar-chips__clear')), 'click'),
  chip_x: key => dispatch(chipFor(key).querySelector('.fbar-chip__x'), 'click'),
  chip_edit: key => dispatch(chipFor(key).querySelector('.fbar-chip__edit'), 'click'),
  reset: () => dispatch(resetBtn, 'click'),
  search: q => { search.value = q; dispatch(search, 'input'); },
};

const out = [state()];
for (const [name, ...args] of JSON.parse(process.env.STEPS)) {
  if (!STEPS[name]) throw new Error('unknown step: ' + name);
  STEPS[name](...args);
  out.push(state());
}
process.stdout.write(JSON.stringify(out));
"""


def _run(steps: list[list]) -> list[dict]:
    """Every state the toolbar passed through: index 0 is at rest, then one per
    step."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    out = subprocess.run(
        [node, "-e", _HARNESS],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "SHIM": str(SHIM),
            "ENGINE": str(ENGINE),
            "STEPS": json.dumps(steps),
        },
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# ── the two dimensions compose ────────────────────────────────────────────


def test_a_state_and_an_attribute_compose_into_the_question_that_was_unaskable():
    """"My pinned conversations inside the archive" — impossible while the two
    dimensions shared one control: as exclusive segments, Pinned and Archived
    could not both be on; as one OR-group, ticking both asked for their UNION
    instead."""
    _, archived, both = _run([["pick", "archived"], ["tick", "pinned"]])
    assert archived["visible"] == ["arc", "arc_pin", "arc2"]
    assert both["visible"] == ["arc_pin"], "AND across the dimensions, not OR"
    assert both["status"] == "archived" and both["onlys"] == ["pinned"]
    assert both["badge"] == "2"


def test_an_attribute_reaches_across_the_state_when_asked():
    """`All + Pinned only` is every pinned conversation on either side of the
    archive — the combination `All` now earns by being one of three states
    rather than a fourth filter."""
    _, _, live_only, everywhere = _run([["tick", "pinned"], ["pick", "active"], ["pick", "all"]])
    assert live_only["visible"] == ["pin", "pin_shd"], "the resting state is the live ones"
    assert everywhere["visible"] == ["pin", "pin_shd", "arc_pin"]


def test_the_two_attributes_are_independent_of_each_other():
    """Pinned and Shared are separate facets, so they AND — "pinned AND shared",
    which is a real set. In the old OR-group they unioned, and `All` swallowed
    them both."""
    _, pinned, both = _run([["tick", "pinned"], ["tick", "shared"]])
    assert pinned["visible"] == ["pin", "pin_shd"]
    assert both["visible"] == ["pin_shd"]
    assert both["badge"] == "2"


def test_the_state_is_exclusive_so_a_second_choice_replaces_the_first():
    """A conversation is live or archived, never both — so these are radios and
    there is no combination of them to offer. This is the one dimension where
    the old segmented control was RIGHT, which is why it stays exclusive."""
    _, archived, everything = _run([["pick", "archived"], ["pick", "all"]])
    assert archived["status"] == "archived"
    assert everything["status"] == "all" and everything["status_checked"] == 1
    assert everything["visible"] == ["live", "pin", "shd", "pin_shd", "arc", "arc_pin", "arc2"]
    assert everything["count"] == "7 chats", "nothing filtered out"
    assert everything["badge"] == "1", "one applied filter, not two"


# ── each is an ordinary filter: chipped, counted, cleared ─────────────────


def test_each_group_chips_in_its_own_right():
    """The state chips as a category ("Show: Archived"); a toggle chips by
    STATING its condition ("Pinned only"), since it has no values to list."""
    _, _, both = _run([["pick", "archived"], ["tick", "pinned"]])
    assert [c["key"] for c in both["chips"]] == ["status", "pinned"]
    assert both["chips"][0]["label"] == "Show:" and both["chips"][0]["values"] == "Archived"
    assert both["chips"][0]["clear_label"] == "Clear Show filter"
    assert both["chips"][1]["label"] == "Pinned only" and both["chips"][1]["values"] == ""
    assert both["chips"][1]["clear_label"] == "Clear Pinned only filter"


def test_choosing_the_resting_state_explicitly_is_not_an_applied_filter():
    """An exclusive group always has one option chosen, so "Active" is checked
    at rest and after Clear. It must not chip or count — a chip saying
    "Show: Active" over an unfiltered list is noise, and the badge would read 1
    on a page with nothing applied."""
    rest, archived, back = _run([["pick", "archived"], ["pick", "active"]])
    assert rest["status"] == "active" and rest["chips"] == [] and rest["badge"] == "0"
    assert archived["badge"] == "1"
    assert back["status"] == "active"
    assert back["chips"] == [] and back["chips_hidden"] is True
    assert back["badge"] == "0" and back["badge_hidden"] is True
    assert back["filter_lit"] is False
    assert back["visible"] == rest["visible"]


def test_clear_returns_the_state_to_resting_and_unticks_the_attributes():
    """The original report: with Archived chosen and nothing else applied, Clear
    was the only control on screen claiming it would undo that, and it did
    nothing at all."""
    rest, _, _, cleared = _run([["pick", "archived"], ["tick", "pinned"], ["menu_clear"]])
    assert cleared["status"] == "active", "the radio goes back to resting, not to no-choice"
    assert cleared["status_checked"] == 1
    assert cleared["onlys"] == []
    assert cleared["chips"] == [] and cleared["badge"] == "0"
    assert cleared["visible"] == rest["visible"]
    assert cleared["count"] == rest["count"]


def test_clear_all_beside_the_chips_does_the_same():
    _, _, _, cleared = _run([["pick", "all"], ["tick", "shared"], ["clear_all"]])
    assert cleared["status"] == "active" and cleared["onlys"] == []
    assert cleared["clear_all"] is False, "the row hides with its last chip"


def test_clear_is_not_a_no_op_when_nothing_is_applied():
    rest, cleared = _run([["menu_clear"]])
    assert cleared == rest


def test_a_chips_x_clears_only_its_own_condition():
    _, _, _, no_state, no_pin = _run(
        [["pick", "archived"], ["tick", "pinned"], ["chip_x", "status"], ["chip_x", "pinned"]]
    )
    assert no_state["status"] == "active", "back to resting"
    assert no_state["onlys"] == ["pinned"], "the attribute survives"
    assert no_state["visible"] == ["pin", "pin_shd"]
    assert no_pin["onlys"] == [] and no_pin["chips"] == []


def test_the_chips_edit_reopens_the_menu_where_the_filter_was_set():
    """Both groups sit in the menu body rather than in a `.fbar-cat` popover, so
    editing either is: open the menu, with no category popover covering it."""
    _, _, opened = _run([["pick", "archived"], ["chip_edit", "status"]])
    assert opened["menu_open"] is True
    assert opened["status"] == "archived", "editing must not change the filter"


def test_it_all_composes_with_the_ordinary_facet_categories():
    _, _, _, with_agent = _run([["pick", "all"], ["tick", "pinned"], ["tick_value", "agent", "revenue"]])
    assert [c["key"] for c in with_agent["chips"]] == ["status", "pinned", "agent"]
    assert with_agent["visible"] == ["pin", "pin_shd", "arc_pin"]
    assert with_agent["badge"] == "3"


# ── the resting condition (`whenEmpty`) ───────────────────────────────────


def test_the_resting_state_hides_the_archive_without_a_special_case():
    """A plain facet's empty state means "don't filter", which would put the
    archive in the default list. `whenEmpty` is the resting CONDITION."""
    (rest,) = _run([])
    assert rest["visible"] == ["live", "pin", "shd", "pin_shd"]
    assert rest["count"] == "4 of 7 chats"


def test_a_choice_replaces_the_resting_value_rather_than_narrowing_within_it():
    """If `whenEmpty` were ANDed instead of replaced, Archived would intersect
    the live set and come back empty — the control would be unusable."""
    _, archived = _run([["pick", "archived"]])
    assert archived["visible"] == ["arc", "arc_pin", "arc2"]


# ── search (#1974) ────────────────────────────────────────────────────────


def test_an_active_search_looks_past_the_state_it_is_in():
    """A search is a request for one named thing, so it outranks the state that
    would hide it — an archived conversation absent from the list AND invisible
    to a search for its own title is simply lost."""
    rest, searching = _run([["search", "archived two"]])
    assert "arc2" not in rest["visible"]
    assert searching["visible"] == ["arc2"]


def test_a_search_still_respects_the_conditions_the_reader_set():
    """`spansSearch` is per-facet and only the state carries it: an attribute or
    an Agent is a condition the reader chose on purpose, so a search narrows
    within it."""
    _, _, searching = _run([["tick", "pinned"], ["search", "archived"]])
    assert searching["visible"] == ["arc_pin"], "not the unpinned archived rows"


def test_the_no_results_reset_clears_the_search_as_well_as_the_filters():
    """`data-fbar-reset` is a wider control than Clear and stays that way: it is
    offered when NOTHING matches, where the search box is as likely to be the
    cause as the filters."""
    *_, reset = _run(
        [["pick", "archived"], ["tick", "pinned"], ["search", "nothing at all"], ["reset"]]
    )
    assert reset["search"] == ""
    assert reset["status"] == "active" and reset["onlys"] == []
    assert reset["visible"] == ["live", "pin", "shd", "pin_shd"]


# ── the "Show N archived" control beside the count ────────────────────────


def test_the_archived_link_goes_through_the_engine():
    """The page offers a second route to the archive next to the count that says
    rows are hidden. It uses the engine's own `setFacet`, so it is the same act
    as choosing Archived in the menu — including moving the radio and growing
    the chip."""
    _, viaapi = _run([["api_set", "status", "archived"]])
    assert viaapi["status"] == "archived" and viaapi["status_checked"] == 1
    assert viaapi["visible"] == ["arc", "arc_pin", "arc2"]
    assert [c["key"] for c in viaapi["chips"]] == ["status"]


# ── the page and the harness stay in step ─────────────────────────────────


def test_the_page_ships_the_facets_this_harness_hard_codes():
    """The harness above declares the config; this pins it to the config the page
    actually ships, so the two cannot drift."""
    js = CHATS_JS.read_text(encoding="utf-8")
    assert 'key: "status", attr: "data-status", label: "Show"' in js
    assert "exclusive: true" in js and 'whenEmpty: ["active"]' in js and "spansSearch: true" in js
    assert 'key: "pinned", attr: "data-pinned", label: "Pinned only", toggle: true' in js
    assert 'key: "shared", attr: "data-shared", label: "Shared only", toggle: true' in js
    assert "segments:" not in js, "the state is a facet now, not a segmented control"


def test_the_template_renders_two_groups_the_right_way_round():
    """State first (the primary cut), attributes under it, categories below —
    and the state as RADIOS, the attributes as CHECKBOXES, which is the whole
    distinction made visible."""
    html = CHATS_HTML.read_text(encoding="utf-8")
    menu = html.split('id="ch-filter-menu"', 1)[1]
    assert '<input type="radio" name="ch-status" data-facet="status" value="{{ key }}"' in menu
    assert "('active', 'Active'), ('archived', 'Archived'), ('all', 'All')" in menu
    assert '<input type="checkbox" data-facet="{{ key }}" value="1">' in menu
    assert "('pinned', 'Pinned only'" in menu and "('shared', 'Shared only'" in menu
    assert menu.index('class="ch-viewsel"') < menu.index('class="ch-onlys"') < menu.index('class="fbar-cat"')
    # An option that cannot change the list is not rendered, and NEITHER IS THE
    # WRAPPER once its last option goes — the rule the categories already follow,
    # which is what "Shared 0" was breaking. `selectattr(2)` keeps only the
    # options with a non-zero tally; `{% if _onlys %}` drops the group with them.
    assert "| selectattr(2) | list %}" in menu
    assert "{% if _onlys %}" in menu
    assert "fbar-seg__btn" not in html and 'data-own="' not in html


def test_the_retired_view_label_is_gone_from_every_file():
    """A chip that says "Show: Archived" plus a button that says
    "Filter · Archived" is the same fact twice, only one of which can be
    clicked off."""
    for f in (CHATS_HTML, CHATS_JS, ROOT / "app" / "web" / "static" / "css" / "chats.css"):
        text = f.read_text(encoding="utf-8")
        assert "ch-filter-view" not in text, f"{f.name} still carries the retired view label"
        assert "ch-filter__view" not in text


def test_segments_survives_in_the_engine_for_librarys_tabs():
    """The fix is not "delete segments": /library's top-level tabs are a real
    mutually-exclusive scope and still ride it. What moved is /chats, which was
    using one scope control for two different dimensions."""
    engine = ENGINE.read_text(encoding="utf-8")
    assert "function setSegment(" in engine
    lib = (ROOT / "app" / "web" / "templates" / "library.html").read_text(encoding="utf-8")
    assert "segments: { container: '#lib-tabs'" in lib
