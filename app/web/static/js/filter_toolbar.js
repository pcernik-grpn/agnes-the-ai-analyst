/* filter_toolbar.js — reusable client-side filter/sort engine for the shared
   .fbar toolbar. One instance drives a table/grid entirely in the browser
   (no round-trips): a search box, an optional single-select segmented control
   (scope), any number of multi-select facet menus (Filter button → grouped
   popover → removable chips on a second row), and a sort <select>. Facet
   values are read off each row's data-* attributes.

   Design intent (see filter_toolbar.css): ownership/scope segments and Sort
   stay inline; only optional refinements live behind the Filter menu, and each
   applied CATEGORY shows as one chip ("Source: Generated, Uploaded") so the
   resting bar stays a single clean row however many values are selected. A chip
   is the edit affordance for its category — clicking it reopens that category's
   popover, its × clears the category, and it disappears when the category
   empties. Chips are updated in place, never rebuilt per value.

   Usage:
     FilterToolbar.init({
       rows: '#af-tbody tr',
       search:  { el: '#af-search', attr: 'data-search' },
       segments:{ container: '#af-own', attr: 'data-ownership',
                  expand: { mine: ['mine', 'shared_by_me'] } },
       // …or, when the segments are NOT mutually exclusive (a chat can be
       // both pinned and shared), `multi: true` reads the attribute as a
       // pipe-separated SET and keeps a row whose set contains the selected
       // segment — the same shape `facets[].multi` uses. `all` then stops
       // being a wildcard and becomes an ordinary token every row that
       // belongs in the default view carries, which is what lets a bucket
       // (archived) be excluded from it without a special case:
       //   segments:{ container: '#ch-seg', attr: 'data-buckets', multi: true }
       //   <tr data-buckets="all|pinned">  <tr data-buckets="archived">
       segments:{ container: '#ch-seg', attr: 'data-buckets', multi: true },
       facets:  [ { key: 'type', attr: 'data-type', label: 'Type' },
                  { key: 'origin', attr: 'data-origin', label: 'Source' },
                  // A binary condition, not a category: on = keep only rows
                  // whose `attr` equals the facet's value, off = keep
                  // everything. Rendered either as one checkbox sitting
                  // directly in the menu (no submenu), or — with `control` —
                  // as a button in the toolbar itself (see below).
                  { key: 'stack', attr: 'data-stack', label: 'In stack only',
                    toggle: true, control: '#lib-stack-toggle' } ],
       filterBtn: '#af-filter-btn', menu: '#af-filter-menu',
       chips: '#af-chips',
       sort:    { el: '#af-sort',
                  keys: { added: 'data-added', name: 'data-name', files: 'data-files' },
                  pinFirst: 'data-folder',     // optional: rows carrying this
                                               // attribute sort above the rest,
                                               // whatever the chosen order
                  headers: '.lib-sort',        // optional: clickable column
                                               // headers (see below)
                  wrap: '#lib-sortwrap' },     // optional: the toolbar
                                               // control's wrapper, hidden in
                                               // table view when `headers` are
                                               // present
       count:   { el: '#af-upload-count', noun: 'artefact' },
       noResults: '#af-noresults',
       view:    { buttons: '.fbar-view__btn', tableWrap: '#lib-tablewrap',
                  grid: '#lib-grid', storageKey: 'lib-view',
                  card: (row) => HTMLElement },
     });

   A facet may name a `control` — a selector for a pressed-state BUTTON living
   in the toolbar rather than inside the Filter menu. Use it for the one or two
   conditions worth surfacing at rest (the Library's "In stack only"): the
   control shows its own state, so such a facet is deliberately left out of the
   Filter button's badge count and grows no chip — both would be a second
   readout of a switch already visible on the bar. It is still cleared by
   "Clear all" and by reset, which sync the button back. The facet's value comes
   from the control's `data-facet-value`, mirroring how an in-menu checkbox
   carries it in `value`.

   `sort.headers` moves sorting onto the TABLE, where a column header is the
   natural place to ask for an order: each button carries `data-sort-key` (a key
   of `sort.keys`) and `data-sort-first` (the direction its first click applies —
   `asc` for names, `desc` for dates). Clicking the active column flips it,
   clicking another takes over; the engine drives `aria-sort` on the enclosing
   `<th>`, an `is-sorted` class and a `data-sort-dir` hook for the caret. Only
   columns with a meaningful order should get one — a categorical column the
   Filter menu already slices is a filter, not a sort. On a sectioned page every
   group has its own `<thead>`; all of them are kept in step. Pair it with
   `sort.wrap` so the toolbar `<select>` yields to the headers in table view and
   returns in grid view, where there are no headers.

   The optional `view` config adds a table ⇄ grid switch: cards are projected
   from the (filtered, sorted) rows by the caller's `card(row)` builder, so the
   table stays the single source of truth and grid view inherits filtering,
   sorting and the no-results state for free. The choice persists in
   localStorage under `storageKey`.

   The handle `init()` returns carries `destroy()`, for a page whose ROWS AND
   MENU are both rendered by a fetch (the admin table registry): the option set
   changes with the data, so the menu has to be rebuilt, and rebuilding it
   throws away the listeners this engine attached to it. `destroy()` releases
   everything init took — including the document/window listeners, which
   outlive any re-render — so `destroy(); rebuildMenu(); init()` is a clean
   swap rather than a slow accumulation of duplicate handlers on the shared
   surfaces (a second engine still holding the old menu would keep filtering
   the same rows against stale state).
*/
(function (global) {
  'use strict';

  function qs(sel, root) { return (root || document).querySelector(sel); }
  function qsa(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function init(cfg) {
    // Every listener this instance attaches, so `destroy()` can take them all
    // back. Routed through one helper rather than remembered case by case:
    // the ones that matter are on `document` and `window`, which no re-render
    // ever clears, and those are exactly the ones easiest to forget.
    var bound = [];
    function on(target, type, fn, opts) {
      if (!target) return;
      target.addEventListener(type, fn, opts);
      bound.push([target, type, fn, opts]);
    }
    function destroy() {
      bound.forEach(function (b) { b[0].removeEventListener(b[1], b[2], b[3]); });
      bound = [];
    }

    var rows = qsa(cfg.rows);
    if (!rows.length && !cfg.always) { /* still wire controls so empty page is inert-safe */ }
    var total = rows.length;

    var searchEl = cfg.search ? qs(cfg.search.el) : null;
    var searchAttr = cfg.search ? cfg.search.attr : null;
    var sortEl = cfg.sort ? qs(cfg.sort.el) : null;
    var sortKeys = (cfg.sort && cfg.sort.keys) || {};
    // Column headers as a second way to set the order (`sort.headers`), and the
    // toolbar control's wrapper (`sort.wrap`) so the two never both show.
    var sortBtns = (cfg.sort && cfg.sort.headers) ? qsa(cfg.sort.headers) : [];
    var sortWrapEl = (cfg.sort && cfg.sort.wrap) ? qs(cfg.sort.wrap) : null;
    var countEl = cfg.count ? qs(cfg.count.el) : null;
    var noun = (cfg.count && cfg.count.noun) || 'item';
    var noResultsEl = cfg.noResults ? qs(cfg.noResults) : null;
    var chipsEl = cfg.chips ? qs(cfg.chips) : null;
    var filterBtn = cfg.filterBtn ? qs(cfg.filterBtn) : null;
    var menuEl = cfg.menu ? qs(cfg.menu) : null;
    var facets = cfg.facets || [];

    // ── state ──
    var segValue = 'all';
    var segExpand = (cfg.segments && cfg.segments.expand) || {};
    // Non-exclusive segments: the row's attribute is a pipe-separated SET
    // rather than one value (see the usage block above).
    var segMulti = !!(cfg.segments && cfg.segments.multi);
    var facetState = {};                 // key -> Set(values)
    facets.forEach(function (f) { facetState[f.key] = new Set(); });

    // ── matching ──
    function segMatch(row) {
      if (!cfg.segments) return true;
      // `all` is a wildcard only for exclusive segments. With `multi` it is a
      // real token, so a row that doesn't carry it (an archived chat) stays out
      // of the default view — which is the whole point of the mode.
      if (segValue === 'all' && !segMulti) return true;
      var raw = row.getAttribute(cfg.segments.attr) || '';
      var allowed = segExpand[segValue] || [segValue];
      if (!segMulti) return allowed.indexOf(raw) !== -1;
      var have = raw.split('|');
      for (var i = 0; i < have.length; i++) {
        if (have[i] && allowed.indexOf(have[i]) !== -1) return true;
      }
      return false;
    }
    function facetMatch(row) {
      // AND across facets, OR within a facet's selected values.
      for (var i = 0; i < facets.length; i++) {
        var f = facets[i];
        var sel = facetState[f.key];
        if (sel.size === 0) continue;
        var raw = row.getAttribute(f.attr) || '';
        if (f.multi) {
          // Multi-valued attribute (e.g. data-tags="a|b"): match if ANY of the
          // row's values is selected.
          var vals = raw.split('|');
          var hit = false;
          for (var v = 0; v < vals.length; v++) {
            if (vals[v] && sel.has(vals[v])) { hit = true; break; }
          }
          if (!hit) return false;
        } else if (!sel.has(raw)) {
          return false;
        }
      }
      return true;
    }
    function searchMatch(row) {
      if (!searchEl) return true;
      var q = (searchEl.value || '').trim().toLowerCase();
      if (!q) return true;
      return (row.getAttribute(searchAttr) || '').indexOf(q) !== -1;
    }

    // ── Facets whose control sits OUTSIDE the Filter menu ───────────────────
    //    A facet with `control` is driven by a button on the toolbar itself, so
    //    it is visible at rest and needs no second readout: it counts toward
    //    neither the Filter button's badge (that badge stands for what is hidden
    //    behind the button) nor the chip row (a chip would restate a switch the
    //    user can already see). It stays a normal facet in every other respect —
    //    same state, same matching, same clearing.
    function externalEl(f) { return f.control ? qs(f.control) : null; }
    function externalValue(f) {
      var el = externalEl(f);
      return (el && el.getAttribute('data-facet-value')) || f.value || 'on';
    }
    //: Push facet state back onto the external controls, so every path that can
    //: change it (the control itself, Clear all, a chip's ×, reset) leaves the
    //: button showing the truth. Driven from apply(), the one funnel they share.
    function syncExternalControls() {
      facets.forEach(function (f) {
        var el = externalEl(f);
        if (!el) return;
        var on = facetState[f.key].has(externalValue(f));
        el.setAttribute('aria-pressed', on ? 'true' : 'false');
        el.classList.toggle('is-active', on);
      });
    }

    function activeFacetCount() {
      return facets.reduce(function (n, f) {
        return f.control ? n : n + facetState[f.key].size;
      }, 0);
    }

    //: The badge on a CATEGORY row, saying how many of its values are picked.
    //: The Filter button's own badge is a total, and a total cannot say which
    //: category to open — with the options one hop behind a submenu, that is
    //: the question the menu has to answer at a glance. Driven from apply()
    //: like every other readout, so the chip row, the button badge and these
    //: can never disagree. A category that renders no badge (or a page with no
    //: `.fbar-cat` markup at all) is simply skipped.
    function syncCatCounts() {
      if (!menuEl) return;
      facets.forEach(function (f) {
        var el = qs('[data-cat-count="' + f.key + '"]', menuEl);
        if (!el) return;
        var n = facetState[f.key].size;
        el.textContent = n;
        el.hidden = n === 0;
      });
    }

    // ── chips (row 2) ──
    function labelFor(facet, value) {
      // Prefer the menu option's own text so labels never drift from the DOM.
      if (menuEl) {
        var input = qs('input[data-facet="' + facet.key + '"][value="' + value + '"]', menuEl);
        if (input) {
          var opt = input.closest('.fbar-menu__opt');
          if (opt) {
            // The label element when the option has one; otherwise the whole
            // row, whose text ends in the tally.
            var span = opt.querySelector('.fbar-menu__opt-text');
            var txt = (span || opt).textContent || '';
            // Strip the trailing tally ONLY when it is actually in the string —
            // i.e. when we fell back to the row. `.fbar-menu__opt-text` holds
            // the name alone (the count is its sibling `.fbar-menu__opt-n`), so
            // stripping there ate any label that legitimately ends in a number:
            // a data package called "Customer 360" chipped as "Customer", and a
            // tag "Q3 2026" as "Q3".
            if (!span) txt = txt.replace(/\s+\d+\s*$/, '');
            return txt.trim() || value;
          }
        }
      }
      return value;
    }
    // ONE chip per facet CATEGORY, not one per selected value: a chip reads
    //   "Source: Generated, Uploaded"
    // and is the edit affordance for that whole category — clicking it reopens
    // the category's options popover, its × clears the category, and it vanishes
    // when nothing is left selected there. Chips persist across renders and are
    // UPDATED in place (keyed by facet), so picking a second value grows the
    // existing chip instead of spawning another, and the element the user just
    // clicked is still under the popover that opens over it.
    var chipEls = {};          // facet key -> chip element
    var chipClearEl = null;    // the trailing "Clear all"
    //: Values past this collapse into "+N"; the chip's CSS ellipsis is the final
    //: guard. Either way `title` carries the complete selection.
    var CHIP_MAX_VALUES = 3;

    function buildChip(f) {
      var chip = document.createElement('span');
      chip.className = 'fbar-chip';
      chip.setAttribute('data-chip', f.key);
      var edit = document.createElement('button');
      edit.type = 'button';
      edit.className = 'fbar-chip__edit';
      var lab = document.createElement('span');
      lab.className = 'fbar-chip__label';
      // A toggle is one condition, so its chip STATES the condition ("In stack
      // only") instead of naming a category and listing values after a colon.
      lab.textContent = f.toggle ? (f.label || f.key) : (f.label || f.key) + ':';
      var vals = null;
      edit.appendChild(lab);
      if (!f.toggle) {
        vals = document.createElement('span');
        vals.className = 'fbar-chip__vals';
        edit.appendChild(vals);
      }
      // stopPropagation: the document-level "click outside closes the menu"
      // listener would otherwise fire on this same event and shut the popover
      // being opened (the chip row sits outside the menu).
      edit.addEventListener('click', function (e) { e.stopPropagation(); openCategory(f.key); });
      var x = document.createElement('button');
      x.type = 'button';
      x.className = 'fbar-chip__x';
      x.textContent = '×';
      x.addEventListener('click', function (e) { e.stopPropagation(); clearFacet(f.key); });
      chip.appendChild(edit);
      chip.appendChild(x);
      chip._edit = edit; chip._vals = vals; chip._x = x;
      return chip;
    }

    function updateChip(chip, f, labels) {
      if (f.toggle) {
        // Nothing to summarise — the chip's presence IS the state.
        var only = f.label || f.key;
        chip._edit.title = only;
        chip._edit.setAttribute('aria-label', 'Edit filter — ' + only);
        chip._x.setAttribute('aria-label', 'Clear ' + only + ' filter');
        return;
      }
      var full = labels.join(', ');
      var shown = labels.length > CHIP_MAX_VALUES
        ? labels.slice(0, CHIP_MAX_VALUES).join(', ') + ' +' + (labels.length - CHIP_MAX_VALUES)
        : full;
      if (chip._vals.textContent !== shown) chip._vals.textContent = shown;
      var name = (f.label || f.key) + ': ' + full;
      chip._edit.title = name;
      chip._edit.setAttribute('aria-label', 'Edit filter — ' + name);
      chip._x.setAttribute('aria-label', 'Clear ' + (f.label || f.key) + ' filter');
    }

    // ── Category submenu placement ──────────────────────────────────────────
    //    ONE placer for every category, present and future (it is driven off the
    //    `.fbar-cat` query, so a new facet inherits it with no extra code): the
    //    submenu opens to the RIGHT of the menu with a small consistent gap,
    //    flips to the left when the viewport has no room there, and is nudged
    //    vertically so the whole popover stays visible. Every coordinate is
    //    measured from live rects — none is hardcoded, in CSS or here.
    //
    //    The popover is `position: fixed` (see filter_toolbar.css) for two
    //    reasons: it escapes every ancestor's clipping box, so a scrolling menu
    //    can't swallow it, and collision detection becomes plain viewport
    //    arithmetic instead of offset-parent bookkeeping.
    var SUBMENU_GAP = 6;     //: the gap between menu and submenu, both sides
    var SUBMENU_EDGE = 8;    //: keep this clear of the viewport edges

    function placeSubmenu(cat) {
      var pop = qs('.fbar-cat__pop', cat);
      if (!pop || pop.hidden) return;
      // On a phone the options stack underneath the row instead (the popover is
      // `position: static` there), so there is nothing to place.
      if (global.getComputedStyle(pop).position !== 'fixed') {
        pop.style.top = '';
        pop.style.left = '';
        return;
      }
      var host = (menuEl || cat).getBoundingClientRect();   // beside the MENU panel
      var row = cat.getBoundingClientRect();
      var w = pop.offsetWidth;
      var h = pop.offsetHeight;
      var vw = global.innerWidth;
      var vh = global.innerHeight;

      // Horizontal: right by default, flipped left on collision. If neither side
      // can hold it (a very narrow window), clamp to the nearest edge rather than
      // leaving it half off-screen.
      var left = host.right + SUBMENU_GAP;
      if (left + w > vw - SUBMENU_EDGE) {
        var flipped = host.left - SUBMENU_GAP - w;
        left = flipped >= SUBMENU_EDGE
          ? flipped
          : Math.max(SUBMENU_EDGE, vw - SUBMENU_EDGE - w);
      }
      // Vertical: aligned with its own row, then pulled back inside the viewport
      // so a category near the bottom still shows all of its options.
      var top = row.top - 6;
      if (top + h > vh - SUBMENU_EDGE) top = vh - SUBMENU_EDGE - h;
      if (top < SUBMENU_EDGE) top = SUBMENU_EDGE;

      pop.style.left = Math.round(left) + 'px';
      pop.style.top = Math.round(top) + 'px';
    }

    // ── Searching WITHIN a category ─────────────────────────────────────────
    //    Past a handful of options a category stops being a list you read and
    //    becomes one you scroll (a tag vocabulary runs to dozens), so finding a
    //    value you can already name costs a scan. Categories longer than
    //    CAT_SEARCH_MIN therefore grow a filter field at the top of their own
    //    popover, injected here rather than in the templates so every page — and
    //    every future facet — inherits it from the row count alone.
    //
    //    It narrows the OPTIONS, never the rows: checking a hidden-by-search box
    //    is impossible anyway, and the field is scoped to one popover, so the
    //    toolbar's own search box keeps its single meaning (search the list).
    var CAT_SEARCH_MIN = 10;

    function setupCatSearch(cat) {
      var pop = qs('.fbar-cat__pop', cat);
      if (!pop) return;
      var opts = qsa('.fbar-menu__opt', pop);
      if (opts.length <= CAT_SEARCH_MIN) return;

      // The text each option is matched on, read once off the DOM: the option's
      // own label minus the trailing tally, which is a count and not part of any
      // name (searching "1" must not match every option on the page).
      var entries = opts.map(function (opt) {
        var txt = (opt.querySelector('.fbar-menu__opt-text') || opt).textContent || '';
        return { el: opt, text: txt.toLowerCase().trim() };
      });

      var label = qs('.fbar-cat__label', cat);
      var name = (label && (label.textContent || '').trim()) || cat.getAttribute('data-cat') || '';
      var wrap = document.createElement('div');
      wrap.className = 'fbar-cat__search';
      var input = document.createElement('input');
      input.type = 'search';
      input.className = 'fbar-cat__searchinput';
      input.placeholder = 'Search…';
      input.autocomplete = 'off';
      //: The placeholder stays generic (a category name doesn't always read as
      //: an object — "Search optional / required…"), so the accessible name is
      //: where the field says WHICH options it filters.
      input.setAttribute('aria-label', 'Search ' + (name || 'filter') + ' options');
      wrap.appendChild(input);
      var none = document.createElement('div');
      none.className = 'fbar-cat__none';
      //: A sighted reader sees the list empty out; `status` is how a screen
      //: reader is told the same thing, since narrowing to nothing is otherwise
      //: silent — options just stop being there.
      none.setAttribute('role', 'status');
      none.textContent = 'No matches';
      none.hidden = true;

      function filterOpts() {
        var q = (input.value || '').trim().toLowerCase();
        var shown = 0;
        entries.forEach(function (e) {
          var hit = !q || e.text.indexOf(q) !== -1;
          e.el.hidden = !hit;
          if (hit) shown++;
        });
        none.hidden = shown !== 0;
        // The popover just changed height, and it is placed against the viewport
        // — re-run the placer or a category near the bottom would keep the top it
        // was measured for and hang off the screen as it shrinks and grows.
        placeSubmenu(cat);
      }
      input.addEventListener('input', filterOpts);
      // Escape with a query in the field means "undo the narrowing", not "close
      // the menu" — the outer Escape handler (which closes it) only gets the key
      // once there is nothing left here to clear.
      input.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && input.value) {
          e.stopPropagation();
          input.value = '';
          filterOpts();
        }
      });

      pop.insertBefore(wrap, pop.firstChild);
      pop.appendChild(none);
      cat._search = input;
    }

    // The one way to open or close a category, so the placer runs however the
    // submenu was reached — hover, click, keyboard focus, or a chip.
    function setCatOpen(cat, open) {
      var pop = qs('.fbar-cat__pop', cat);
      var btn = qs('.fbar-cat__btn', cat);
      if (pop) pop.hidden = !open;
      if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
      cat.classList.toggle('is-open', open);
      if (open) placeSubmenu(cat);
    }

    // `position: fixed` does not follow the anchor, so re-place the open submenu
    // when the page moves or resizes under it. Capture phase, to catch scrolling
    // inside an ancestor as well as the window.
    function replaceOpenSubmenu() {
      if (!menuEl) return;
      var open = qs('.fbar-cat.is-open', menuEl);
      if (open) placeSubmenu(open);
    }
    on(global, 'resize', replaceOpenSubmenu);
    on(global, 'scroll', replaceOpenSubmenu, true);

    // Open the Filter menu with ONE category's options showing — what a chip
    // click does, so a filter is edited where it was applied. A TOGGLE facet has
    // no category row and no submenu: it sits in the menu itself, so opening the
    // menu and focusing its checkbox is the whole affordance.
    function openCategory(key) {
      if (!menuEl) return;
      openMenu(true);
      var cat = qs('.fbar-cat[data-cat="' + key + '"]', menuEl);
      qsa('.fbar-cat', menuEl).forEach(function (c) { setCatOpen(c, c === cat); });
      // A long category opens with the caret in its search field: the reader came
      // here to change a selection they can name, and on that list finding the
      // value is the work. Short ones focus the first option, as before.
      var first = (cat && cat._search) || qs('input[data-facet="' + key + '"]', cat || menuEl);
      if (first) first.focus();
    }

    function renderChips() {
      if (!chipsEl) return;
      var order = [];
      facets.forEach(function (f) {
        // Its own toolbar button is the readout — see externalEl().
        if (f.control) return;
        var labels = [];
        facetState[f.key].forEach(function (val) { labels.push(labelFor(f, val)); });
        var chip = chipEls[f.key];
        if (!labels.length) {
          // Nothing selected in this category any more — the chip goes.
          if (chip) { chip.remove(); delete chipEls[f.key]; }
          return;
        }
        if (!chip) { chip = chipEls[f.key] = buildChip(f); }
        updateChip(chip, f, labels);
        order.push(chip);
      });
      // "Clear all" trails the chips, and ONLY while there is a chip to clear.
      // It is cached across renders (so it keeps its listener), but pushing a
      // cached one unconditionally kept `order` non-empty forever — the row then
      // never hid again and a lone "Clear all" sat under an unfiltered list.
      if (order.length) {
        if (!chipClearEl) {
          chipClearEl = document.createElement('button');
          chipClearEl.type = 'button';
          chipClearEl.className = 'fbar-chips__clear';
          chipClearEl.textContent = 'Clear all';
          chipClearEl.addEventListener('click', function () { clearFacets(); });
        }
        order.push(chipClearEl);
      }
      // Move only the nodes actually out of place, so an untouched chip is never
      // detached and re-inserted (which would drop focus and hover mid-edit).
      order.forEach(function (el, i) {
        if (chipsEl.children[i] !== el) chipsEl.insertBefore(el, chipsEl.children[i] || null);
      });
      while (chipsEl.children.length > order.length) chipsEl.removeChild(chipsEl.lastChild);
      chipsEl.hidden = order.length === 0;
    }

    // ── mutations ──
    function setFacet(key, value, on) {
      var sel = facetState[key];
      if (on) sel.add(value); else sel.delete(value);
      if (menuEl) {
        var input = qs('input[data-facet="' + key + '"][value="' + value + '"]', menuEl);
        if (input) input.checked = on;
      }
      apply();
    }
    // Clear ONE category — what a chip's × does, now that a chip stands for the
    // whole category rather than a single value.
    function clearFacet(key) {
      if (!facetState[key]) return;
      facetState[key].clear();
      if (menuEl) {
        qsa('input[data-facet="' + key + '"]', menuEl).forEach(function (i) { i.checked = false; });
      }
      apply();
    }
    function clearFacets() {
      facets.forEach(function (f) { facetState[f.key].clear(); });
      if (menuEl) qsa('input[data-facet]', menuEl).forEach(function (i) { i.checked = false; });
      apply();
    }
    function resetAll() {
      if (searchEl) searchEl.value = '';
      setSegment('all');
      clearFacets();  // also applies
    }

    // ── table ⇄ grid view ──
    // Cards are rebuilt from the visible rows on every apply()/sort while grid
    // view is active, so filtering + sorting need no grid-specific code.
    var lastVisible = rows.length;

    // ── Grouped sections (one collapsible block per type) ──
    // Each section owns its rows; a section whose rows are all filtered out
    // hides entirely rather than leaving an empty heading behind.
    var sectionsSel = cfg.sections || null;
    function refreshSections() {
      if (!sectionsSel) return;
      qsa(sectionsSel).forEach(function (sec) {
        // Counted off the engine's OWN row set, not a fresh `[data-item-id]`
        // query: the caller's selector already excludes a folder's child rows
        // (they don't earn a section badge, nor keep an empty section alive),
        // and in grid view that query would also match the projected CARDS —
        // which are rebuilt after this runs, so their stale copy inflated every
        // badge (a filtered section read "11" over two visible rows).
        var vis = 0;
        rows.forEach(function (r) { if (!r.hidden && sec.contains(r)) vis++; });
        var badge = qs('[data-sec-count]', sec);
        if (badge) badge.textContent = String(vis);
        sec.hidden = vis === 0;
      });
    }

    var viewCfg = cfg.view || null;
    var viewBtns = viewCfg ? qsa(viewCfg.buttons) : [];
    var currentView = 'table';

    function renderGrid() {
      if (!viewCfg.card) return;
      // Sectioned pages have one grid per section, so project each section's
      // own visible rows into its own grid; flat pages have a single pair.
      qsa(viewCfg.grid).forEach(function (g) {
        g.innerHTML = '';
        var scope = sectionsSel ? g.closest(sectionsSel) : document;
        var scopeRows = scope ? qsa('[data-item-id]:not([data-parent-id])', scope) : rows;
        scopeRows.forEach(function (row) {
          if (row.hidden) return;
          var card = viewCfg.card(row);
          if (card) g.appendChild(card);
        });
      });
    }
    function applyView() {
      if (!viewCfg) return;
      var grid = currentView === 'grid';
      // With nothing left after filtering, hide BOTH containers so the
      // no-results panel stands alone (an empty table head reads as a bug).
      var empty = lastVisible === 0;
      qsa(viewCfg.tableWrap).forEach(function (el) { el.hidden = grid || empty; });
      qsa(viewCfg.grid).forEach(function (el) { el.hidden = !grid || empty; });
      viewBtns.forEach(function (b) {
        var on = b.getAttribute('data-view') === currentView;
        b.classList.toggle('is-active', on);
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      // Where the columns can be clicked, the HEADERS are the sort control and
      // the toolbar one would be a second readout of the same state — the same
      // reason a facet with its own toolbar button grows no chip. So it shows
      // only in grid view, where there are no headers to click. A page with no
      // sortable headers keeps its control in both views.
      if (sortWrapEl && sortBtns.length) sortWrapEl.hidden = !grid;
      if (grid) renderGrid();
    }
    function setView(value) {
      currentView = value === 'grid' ? 'grid' : 'table';
      if (viewCfg && viewCfg.storageKey) {
        try { localStorage.setItem(viewCfg.storageKey, currentView); } catch (e) { /* private mode */ }
      }
      applyView();
    }

    // ── apply (visibility + count + chrome) ──
    function apply() {
      var visible = 0;
      rows.forEach(function (row) {
        var show = searchMatch(row) && segMatch(row) && facetMatch(row);
        row.hidden = !show;
        if (show) visible++;
      });
      if (countEl) {
        countEl.textContent = (visible === total)
          ? total + ' ' + noun + (total === 1 ? '' : 's')
          : visible + ' of ' + total + ' ' + noun + (total === 1 ? '' : 's');
      }
      lastVisible = visible;
      refreshSections();
      if (noResultsEl) noResultsEl.hidden = visible !== 0 || total === 0;
      var n = activeFacetCount();
      if (filterBtn) {
        var badge = filterBtn.querySelector('.fbar-filter__n');
        filterBtn.classList.toggle('is-active', n > 0);
        if (badge) { badge.textContent = n; badge.hidden = n === 0; }
      }
      renderChips();
      syncCatCounts();
      syncExternalControls();
      applyView();  // re-project the grid from the new visible set
      if (cfg.onApply) {
        try { cfg.onApply(); } catch (e) { /* a page hook must never break filtering */ }
      }
    }

    // ── sort ─────────────────────────────────────────────────────────────────
    //    ONE order, up to two ways to set it: the toolbar <select> and, on a
    //    table, clickable column headers. Both write `sortValue` and both are
    //    re-synced from it, so the order survives switching control and
    //    switching view — and a page can offer either, or both.
    var sortValue = (sortEl && sortEl.value) || (cfg.sort && cfg.sort.initial) || '';

    //: A sort value is `<key>_<dir>` ("added_desc"). Split on the LAST
    //: underscore so a multi-word key ("file_size_desc") still parses.
    function sortParts(v) {
      var i = String(v).lastIndexOf('_');
      return i === -1 ? { key: v, dir: 'asc' } : { key: v.slice(0, i), dir: v.slice(i + 1) };
    }
    //: The direction a column opens in on its FIRST click — names read A→Z,
    //: dates read newest-first, and a blanket "ascending" would open a date
    //: column at the oldest row, which is never what "sort by date" means.
    function firstDir(btn) { return btn.getAttribute('data-sort-first') || 'asc'; }

    //: Push `sortValue` back onto every control that can display it. The
    //: headers live in one <thead> PER GROUP on a sectioned page, so all of
    //: them are updated — a group showing a stale arrow would be claiming a
    //: different order for its own rows.
    //: ds_dropdown.js (#1055) only pushes a selection FROM its own menu ONTO
    //: the paired native <select> — it never reads the select back. Sortable
    //: column headers write `sortEl.value` directly (above), which a paired
    //: dropdown under the paper theme would otherwise never see, so re-sync
    //: its button label + aria-checked state here too.
    function syncPairedDropdown() {
      if (!sortEl) return;
      var dd = document.querySelector('.ds-dropdown[data-ds-dropdown-target="' + sortEl.id + '"]');
      if (!dd) return;
      var label = dd.querySelector('.ds-dropdown-btn-label');
      var opt = sortEl.options[sortEl.selectedIndex];
      dd.querySelectorAll('.ds-dropdown-menu [role="menuitemradio"]').forEach(function (item) {
        var isSelected = item.dataset.value === sortEl.value;
        item.classList.toggle('is-selected', isSelected);
        item.setAttribute('aria-checked', isSelected ? 'true' : 'false');
      });
      if (label && opt) label.textContent = opt.textContent.trim();
    }

    function syncSortControls() {
      var cur = sortParts(sortValue);
      if (sortEl && sortEl.value !== sortValue) sortEl.value = sortValue;
      syncPairedDropdown();
      sortBtns.forEach(function (btn) {
        var on = btn.getAttribute('data-sort-key') === cur.key;
        var dir = on ? cur.dir : firstDir(btn);
        btn.classList.toggle('is-sorted', on);
        //: The caret always shows the direction it stands for: the ACTIVE one
        //: on the sorted column, the one a first click would apply on the rest
        //: (revealed on hover, so the header row isn't a wall of arrows).
        btn.setAttribute('data-sort-dir', dir);
        var th = btn.closest ? btn.closest('th') : null;
        //: aria-sort is what a screen reader announces the column BY, so it
        //: must name the applied state, not the affordance.
        if (th) th.setAttribute('aria-sort', on ? (dir === 'asc' ? 'ascending' : 'descending') : 'none');
        var label = btn.getAttribute('data-sort-label') || (btn.textContent || '').trim();
        var next = on ? (cur.dir === 'asc' ? 'desc' : 'asc') : dir;
        btn.setAttribute('aria-label',
          'Sort by ' + label + ', ' + (next === 'asc' ? 'ascending' : 'descending'));
      });
    }

    function setSort(value) {
      sortValue = value;
      syncSortControls();
      applySort();
    }

    function applySort() {
      if (!cfg.sort || !rows.length) return;
      // Any key in `sort.keys` sorts, in either direction — the set of orders is
      // whatever the page declares, not a fixed list baked in here, so adding a
      // sortable column is one entry in `keys` and one header button. Keys named
      // in `sort.numeric` compare as numbers; everything else compares as a
      // string, which is right for names, owners, labels AND ISO timestamps
      // alike (ISO 8601 sorts lexicographically, by design).
      var cur = sortParts(sortValue);
      var attr = sortKeys[cur.key];
      if (!attr) return;                 //: unknown key — leave the order alone
      var flip = cur.dir === 'desc' ? -1 : 1;
      var numeric = ((cfg.sort && cfg.sort.numeric) || []).indexOf(cur.key) !== -1;
      // Optional structural grouping that OUTRANKS the chosen order: rows
      // carrying `sort.pinFirst` stay above the rest (e.g. the Library's
      // collections above its loose files). The group is part of the table's
      // shape, not one sort order among several.
      var pinK = (cfg.sort && cfg.sort.pinFirst) || null;
      function pinned(row) { return pinK && row.getAttribute(pinK) ? 0 : 1; }
      var sorted = rows.slice().sort(function (a, b) {
        if (pinK) {
          var pd = pinned(a) - pinned(b);
          if (pd !== 0) return pd;
        }
        var av = a.getAttribute(attr) || '';
        var bv = b.getAttribute(attr) || '';
        var d = numeric
          ? (parseInt(av, 10) || 0) - (parseInt(bv, 10) || 0)
          : av.localeCompare(bv);
        return flip * d;
      });
      // Re-attach each row to ITS OWN parent, not to rows[0]'s: on a sectioned
      // page the rows span one tbody per section, and appending them all to the
      // first one would empty every other section into it. A row's child rows
      // (`data-parent-id`, e.g. the files inside a collection) follow it, so
      // sorting can't separate a container from its contents.
      sorted.forEach(function (row) {
        var parent = row.parentNode;
        if (!parent) return;
        parent.appendChild(row);
        var id = row.getAttribute('data-item-id');
        if (!id) return;
        var ref = row;
        qsa('[data-parent-id="' + id + '"]', parent).forEach(function (kid) {
          parent.insertBefore(kid, ref.nextSibling);
          ref = kid;
        });
      });
      // `rows` is the engine's own ordered list — keep it in sync so grid
      // projection reflects the new order (it iterates `rows`, not the DOM).
      rows = sorted;
      applyView();
      // Sorting rearranged the rows, so any page chrome derived from row ORDER
      // (the Library's collections/files divider) has to be recomputed — the
      // same hook `apply()` calls for the same reason.
      if (cfg.onApply) {
        try { cfg.onApply(); } catch (e) { /* a page hook must never break sorting */ }
      }
    }

    // ── segmented control ──
    function setSegment(value) {
      segValue = value;
      if (cfg.segments) {
        qsa('.fbar-seg__btn', qs(cfg.segments.container)).forEach(function (btn) {
          var on = btn.getAttribute('data-' + (cfg.segments.name || 'own')) === value
                || btn.getAttribute('data-own') === value;
          btn.classList.toggle('is-active', on);
          // The state attribute has to be the one the element's ROLE defines,
          // and every caller of this engine is a `role="radiogroup"` of
          // `role="radio"` buttons (library.html, chats.html,
          // _profile_tokens.html) — where the state is `aria-checked`;
          // `aria-selected` belongs to tabs and options and means nothing on a
          // radio. Writing only `aria-selected` left the server-rendered
          // `aria-checked="true"` on the DEFAULT segment forever, and
          // components.css styles an active tab off BOTH the class and
          // `[aria-checked="true"]` — so selecting the second tab lit them both
          // (#1898 item 2, the half `.tab-strip` alone could not fix). A screen
          // reader heard the same stale answer.
          btn.setAttribute(btn.getAttribute('role') === 'radio' ? 'aria-checked' : 'aria-selected',
                           on ? 'true' : 'false');
        });
      }
      apply();
    }

    // ── menu open/close ──
    function openMenu(open) {
      if (!menuEl || !filterBtn) return;
      menuEl.hidden = !open;
      filterBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    // ── wiring ──
    on(searchEl, 'input', apply);
    on(sortEl, 'change', function () { setSort(sortEl.value); });
    // Clicking a column header: the SAME column flips direction, a different
    // one takes over in the direction that column is read in. There is no
    // third "unsorted" state — the list always has an order, so a click that
    // removed it would only ever leave the reader somewhere arbitrary.
    sortBtns.forEach(function (btn) {
      on(btn, 'click', function () {
        var key = btn.getAttribute('data-sort-key');
        var cur = sortParts(sortValue);
        var dir = cur.key === key ? (cur.dir === 'asc' ? 'desc' : 'asc') : firstDir(btn);
        setSort(key + '_' + dir);
      });
    });
    if (cfg.segments) {
      qsa('.fbar-seg__btn', qs(cfg.segments.container)).forEach(function (btn) {
        on(btn, 'click', function () {
          setSegment(btn.getAttribute('data-' + (cfg.segments.name || 'own')) || btn.getAttribute('data-own'));
        });
      });
    }
    // ── Filter categories: each menu item reveals its options in a secondary
    //    popover on hover OR keyboard focus (hover alone would be unusable on
    //    touch and by keyboard). Only one category is open at a time. ──
    if (menuEl) {
      var cats = qsa('.fbar-cat', menuEl);
      // The submenu now sits a gap away from the menu, so the pointer travelling
      // to it leaves the category for a frame. A short grace period — cancelled
      // by entering the submenu — is what keeps hover usable across that gap.
      var closeTimer = null;
      function cancelClose() {
        if (closeTimer) { clearTimeout(closeTimer); closeTimer = null; }
      }
      function scheduleClose(cat) {
        cancelClose();
        closeTimer = setTimeout(function () {
          // Focus outranks hover: a category being TYPED IN (its search field) or
          // tabbed through must not disappear because the pointer drifted off it.
          // Without this, reaching for a long category's options with the mouse
          // and then typing was a race against the 140ms grace period.
          if (cat.contains(document.activeElement)) return;
          setCatOpen(cat, false);
        }, 140);
      }
      function openOnly(cat) {
        cancelClose();
        cats.forEach(function (c) { if (c !== cat) setCatOpen(c, false); });
        setCatOpen(cat, true);
      }
      cats.forEach(function (cat) {
        setupCatSearch(cat);
        on(cat, 'mouseenter', function () { openOnly(cat); });
        on(cat, 'mouseleave', function () { scheduleClose(cat); });
        var pop = qs('.fbar-cat__pop', cat);
        if (pop) {
          on(pop, 'mouseenter', cancelClose);
          on(pop, 'mouseleave', function () { scheduleClose(cat); });
        }
        var btn = qs('.fbar-cat__btn', cat);
        if (btn) {
          // Click/Enter toggles — the keyboard and touch path.
          on(btn, 'click', function (e) {
            e.stopPropagation();
            var willOpen = pop ? pop.hidden : true;
            cancelClose();
            cats.forEach(function (c) { setCatOpen(c, false); });
            setCatOpen(cat, willOpen);
          });
          on(btn, 'focus', function () { openOnly(cat); });
        }
      });
    }

    // Toolbar-level facet buttons (facet.control). aria-pressed is the state, so
    // read the NEXT state off it rather than keeping a parallel flag.
    facets.forEach(function (f) {
      var el = externalEl(f);
      if (!el) return;
      on(el, 'click', function () {
        setFacet(f.key, externalValue(f), el.getAttribute('aria-pressed') !== 'true');
      });
    });

    if (menuEl) {
      qsa('input[data-facet]', menuEl).forEach(function (input) {
        on(input, 'change', function () {
          setFacet(input.getAttribute('data-facet'), input.value, input.checked);
        });
      });
      on(qs('[data-fbar-clear]', menuEl), 'click', clearFacets);
      on(qs('[data-fbar-done]', menuEl), 'click', function () { openMenu(false); });
    }
    if (filterBtn) {
      on(filterBtn, 'click', function (e) {
        e.stopPropagation();
        openMenu(menuEl && menuEl.hidden);
      });
      on(document, 'click', function (e) {
        if (menuEl && !menuEl.hidden && !menuEl.contains(e.target) && e.target !== filterBtn && !filterBtn.contains(e.target)) {
          openMenu(false);
        }
      });
      on(document, 'keydown', function (e) {
        if (e.key === 'Escape' && menuEl && !menuEl.hidden) openMenu(false);
      });
    }
    qsa('[data-fbar-reset]').forEach(function (btn) {
      on(btn, 'click', resetAll);
    });
    viewBtns.forEach(function (btn) {
      on(btn, 'click', function () { setView(btn.getAttribute('data-view')); });
    });

    // Restore the persisted view BEFORE the first apply so the page never
    // flashes the table on its way to the grid.
    if (viewCfg && viewCfg.storageKey) {
      try {
        if (localStorage.getItem(viewCfg.storageKey) === 'grid') currentView = 'grid';
      } catch (e) { /* private mode — table default */ }
    }

    // Re-read the row set from the DOM, then re-apply. A page that MUTATES its
    // rows (the Library moves a file between collections in place) changes which
    // rows are top-level, and `rows` was captured at init — without this the
    // engine would keep counting a row that is no longer part of the set.
    function refresh() {
      rows = qsa(cfg.rows);
      total = rows.length;
      apply();
    }

    // The server renders the rows in the default order already, so the first
    // paint needs no re-sort — only the controls have to be told which order
    // that is, or the active column would start with no arrow on it.
    syncSortControls();

    apply();
    return {
      apply: apply, refresh: refresh, reset: resetAll,
      setView: setView, renderGrid: renderGrid, setSort: setSort,
      destroy: destroy,
    };
  }

  global.FilterToolbar = { init: init };
})(window);
