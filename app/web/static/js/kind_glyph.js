/* kind_glyph — THE per-kind glyph set, for anything that renders a resource
   kind client-side.
 *
 * The server-side twin is the `kind_glyph(kind)` macro in
 * macros/_catalog_card.html, and this file is a transcription of it rather
 * than a second drawing of the same shapes. It exists because three
 * surfaces render rows in JS (the catalog card runtime, /admin/access, the
 * Library's hydrated drafts) and each was otherwise going to keep its own
 * copy — catalog_card.js already carried one that had fallen six kinds
 * behind the macro, which is the failure this replaces.
 *
 * Keys are the `--ds-kind-*` token names, so a caller that already resolved
 * a kind to its COLOUR has, by construction, resolved it to its GLYPH:
 *
 *     el.dataset.kind = kind;                       // colour, via CSS
 *     el.innerHTML = AgnesKindGlyph.get(kind);      // glyph, via here
 *
 * `get()` folds the macro's own aliases (recipe/recipes, plugin/plugins,
 * table/data) and answers the neutral square for anything unknown, so a
 * newly registered resource type renders a shape rather than a hole.
 *
 * One key is NOT a resource kind and has no colour token: `group`. It lives
 * here because it is drawn on as many pages as any kind is and the whole
 * point of this file is that a shape used everywhere has one source. See
 * its own note below.
 */
(function () {
  'use strict';

  var GLYPH = {
  agent: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="4" y="8" width="16" height="11" rx="2.5" stroke="currentColor" stroke-width="1.6"/><path d="M12 4v4M8.5 13h.01M15.5 13h.01" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><circle cx="12" cy="4" r="1.2" fill="currentColor"/></svg>',
  app: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="3.5" y="4.5" width="17" height="15" rx="2.5" stroke="currentColor" stroke-width="1.6"/><path d="M3.5 9h17" stroke="currentColor" stroke-width="1.6"/><circle cx="6.6" cy="6.8" r="0.9" fill="currentColor"/></svg>',
  data: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><ellipse cx="12" cy="6" rx="7" ry="3" stroke="currentColor" stroke-width="1.7"/><path d="M5 6v6c0 1.66 3.13 3 7 3s7-1.34 7-3V6" stroke="currentColor" stroke-width="1.7"/><path d="M5 12v6c0 1.66 3.13 3 7 3s7-1.34 7-3v-6" stroke="currentColor" stroke-width="1.7"/></svg>',
  doc: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M7 4h7l4 4v12H7z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M13 4v4h4" stroke="currentColor" stroke-width="1.6"/></svg>',
  /* A GROUP — an audience, not a resource. Here because it is drawn on as
     many pages as any kind is (access, people, sharing, an agent's scope)
     and a second drawing of it would drift the way the kinds did. It has NO
     --ds-kind-group colour on purpose: the categorical palette names types
     of CONTENT, and a group is who the content reaches — so it is marked in
     neutral ink, which is also what keeps it legible beside a coloured kind
     in a mixed row. */
  group: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M16 20v-1.6a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4V20" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/><circle cx="9.5" cy="7.5" r="3.5" stroke="currentColor" stroke-width="1.6"/><path d="M16.5 4.4a3.5 3.5 0 0 1 0 6.7M21 20v-1.6a4 4 0 0 0-3-3.87" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  folder: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 7.5A1.5 1.5 0 0 1 5.5 6h3.2a1.5 1.5 0 0 1 1.06.44L11 7.5h7.5A1.5 1.5 0 0 1 20 9v8.5a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 17.5Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M4 10.5h16" stroke="currentColor" stroke-width="1.6"/></svg>',
  library: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M9 7h6l4 4v9a1 1 0 0 1-1 1H9a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M15 7v4h4" stroke="currentColor" stroke-width="1.6"/><path d="M6 16H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h6l1 1" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  memory: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 3c-1.7 0-3 1.3-3 3-1.6.2-3 1.5-3 3.2 0 .6.2 1.2.5 1.7-.6.6-1 1.4-1 2.4 0 1.5.9 2.7 2.2 3.2 0 1.7 1.4 3 3.1 3 .8 0 1.5-.3 2-.8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/><path d="M12 3c1.7 0 3 1.3 3 3 1.6.2 3 1.5 3 3.2 0 .6-.2 1.2-.5 1.7.6.6 1 1.4 1 2.4 0 1.5-.9 2.7-2.2 3.2 0 1.7-1.4 3-3.1 3-.8 0-1.5-.3-2-.8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/><path d="M12 3v18" stroke="currentColor" stroke-width="1.6"/></svg>',
  plugins: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M10 4a2 2 0 1 1 4 0v2h3a1 1 0 0 1 1 1v3h2a2 2 0 1 1 0 4h-2v3a1 1 0 0 1-1 1h-3v-2a2 2 0 1 0-4 0v2H7a1 1 0 0 1-1-1v-3H4a2 2 0 1 1 0-4h2V7a1 1 0 0 1 1-1h3V4Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>',
  recipes: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M6 3h9l4 4v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M14 3v4h4M8.5 12h7M8.5 16h7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
  skill: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M13 3 4 14h6l-1 7 9-11h-6l1-7Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>',
  };

  // The macro's own aliases, plus `file` — the --ds-kind-* token name for a
  // single document, which the macro spells `doc`.
  var ALIAS = { recipe: 'recipes', plugin: 'plugins', table: 'data', file: 'doc', groups: 'group' };
  var FALLBACK = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="4" y="4" width="16" height="16" rx="3" stroke="currentColor" stroke-width="1.7"/></svg>';

  /* The group mark, in the two densities a group actually gets rendered at,
     so the twelve JS surfaces that name a group stop each writing their own
     wrapper around the same shape.

     `groupTile()`  — the leading marker of a ROW, the same 26px neutral
                      square the Library gives a kind. Use it where a kind
                      tile would sit, so a list of mixed things lines up.
     `groupGlyph()` — the bare shape, for INSIDE something that already has
                      a shell: an origin chip, a "via <b>Delivery</b>", a
                      link. A tile inside a pill is a box in a box. */
  var GROUP_LABEL = 'Group';
  window.AgnesKindGlyph = {
    get: function (kind) {
      var k = ALIAS[kind] || kind;
      return GLYPH[k] || FALLBACK;
    },
    has: function (kind) { return !!GLYPH[ALIAS[kind] || kind]; },
    groupTile: function () {
      return '<span class="ds-kindtag ds-kindtag--glyph" data-kind="group"'
        + ' role="img" aria-label="' + GROUP_LABEL + '" data-tip="' + GROUP_LABEL + '">'
        + GLYPH.group + '</span>';
    },
    groupGlyph: function () {
      return '<span class="ds-groupglyph" role="img" aria-label="' + GROUP_LABEL + '">'
        + GLYPH.group + '</span>';
    },
  };
})();
