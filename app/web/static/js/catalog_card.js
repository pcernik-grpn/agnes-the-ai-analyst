/* Catalog card — shared client runtime for the reusable card component.
   Loaded by both catalog_unified.html and stack_unified.html so the two
   surfaces render + behave identically. Pairs with the server-side
   catalog_card() Jinja macro and static/css/catalog_card.css.

   Exposes window.renderCatalogCard(c) for client-hydrated kinds and
   installs ONE delegated Add/Remove stack-toggle handler covering every
   card on the page (server-rendered or hydrated). Cards inside a
   [data-remove-hides] container are removed from the DOM on successful
   unsubscribe (My Stack behavior); elsewhere the button flips back to
   "Add to stack" (Catalog behavior). */
(function () {
  'use strict';

  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  const KIND_GLYPH = {
    data: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><ellipse cx="12" cy="6" rx="7" ry="3" stroke="currentColor" stroke-width="1.7"/><path d="M5 6v6c0 1.66 3.13 3 7 3s7-1.34 7-3V6" stroke="currentColor" stroke-width="1.7"/><path d="M5 12v6c0 1.66 3.13 3 7 3s7-1.34 7-3v-6" stroke="currentColor" stroke-width="1.7"/></svg>',
    plugins: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M10 4a2 2 0 1 1 4 0v2h3a1 1 0 0 1 1 1v3h2a2 2 0 1 1 0 4h-2v3a1 1 0 0 1-1 1h-3v-2a2 2 0 1 0-4 0v2H7a1 1 0 0 1-1-1v-3H4a2 2 0 1 1 0-4h2V7a1 1 0 0 1 1-1h3V4Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>',
    memory: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 3c-1.7 0-3 1.3-3 3-1.6.2-3 1.5-3 3.2 0 .6.2 1.2.5 1.7-.6.6-1 1.4-1 2.4 0 1.5.9 2.7 2.2 3.2 0 1.7 1.4 3 3.1 3 .8 0 1.5-.3 2-.8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/><path d="M12 3c1.7 0 3 1.3 3 3 1.6.2 3 1.5 3 3.2 0 .6-.2 1.2-.5 1.7.6.6 1 1.4 1 2.4 0 1.5-.9 2.7-2.2 3.2 0 1.7-1.4 3-3.1 3-.8 0-1.5-.3-2-.8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/><path d="M12 3v18" stroke="currentColor" stroke-width="1.6"/></svg>',
    recipes: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M6 3h9l4 4v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M14 3v4h4M8.5 12h7M8.5 16h7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
    library: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M9 7h6l4 4v9a1 1 0 0 1-1 1H9a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M15 7v4h4" stroke="currentColor" stroke-width="1.6"/><path d="M6 16H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h6l1 1" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    app: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="3.5" y="4.5" width="17" height="15" rx="2.5" stroke="currentColor" stroke-width="1.6"/><path d="M3.5 9h17" stroke="currentColor" stroke-width="1.6"/><circle cx="6.6" cy="6.8" r="0.9" fill="currentColor"/></svg>',
  };
  const META_GLYPH = {
    tables: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="3.5" y="4.5" width="17" height="15" rx="2" stroke="currentColor" stroke-width="1.6"/><path d="M3.5 9.5h17M3.5 14.5h17M9 4.5v15" stroke="currentColor" stroke-width="1.6"/></svg>',
    items: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 3 3 7.5 12 12l9-4.5L12 3Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="m3 12 9 4.5L21 12M3 16.5 12 21l9-4.5" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>',
    plugin: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="4" y="4" width="16" height="16" rx="3" stroke="currentColor" stroke-width="1.6"/><path d="M9 9h6v6H9z" stroke="currentColor" stroke-width="1.6"/></svg>',
    doc: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M7 4h7l4 4v12H7z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M13 4v4h4" stroke="currentColor" stroke-width="1.6"/></svg>',
  };
  const ARROW = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  // Verification tick — the ONLY trust glyph on a card. Mirrors the SVG in the
  // Jinja macro's cc-trust--verified branch.
  /* Which trust vocabulary this instance draws. `.ds-trust` (css/trustmark.css)
     is scoped to html[data-theme="paper"], so on a default blue instance it
     would render an unstyled marker — blue keeps the `.cc-trust` chips it has
     always shown. Read once: the theme is fixed for the document's lifetime. */
  const IS_PAPER = document.documentElement.getAttribute('data-theme') === 'paper';

  const CHECK_GLYPH = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  // Two wordings share the same toggle mechanics (POST/DELETE add_url/
  // remove_url, flip in↔add): 'stack' is Marketplace install/uninstall
  // (plugins — untouched by the RBAC auto-membership stack model);
  // 'download' is Data/Memory local-copy state (the resource is ALWAYS
  // already in the caller's stack once granted — this only controls
  // whether `agnes pull` also keeps a local copy on disk).
  const TOGGLE_LABELS = {
    stack: { in: 'In stack', remove: 'Remove', add: 'Add to stack' },
    download: { in: 'Downloaded', remove: 'Remove local copy', add: 'Download locally' },
  };
  function stackBtnClass(state) { return 'cc-btn ' + (state === 'in' ? 'cc-btn--instack' : 'cc-btn--primary'); }
  function stackBtnInner(state, kind) {
    const labels = TOGGLE_LABELS[kind] || TOGGLE_LABELS.stack;
    if (state === 'in') {
      return '<span class="cc-btn-instack"><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg> ' + labels.in + '</span>' +
             '<span class="cc-btn-remove"><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg> ' + labels.remove + '</span>';
    }
    return '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg> ' + labels.add;
  }

  function footerAction(a) {
    a = a || {};
    if (a.mode === 'required') {
      return '<span class="cc-required"><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="5" y="10.5" width="14" height="9" rx="2" stroke="currentColor" stroke-width="1.7"/><path d="M8 10.5V8a4 4 0 0 1 8 0v2.5" stroke="currentColor" stroke-width="1.7"/></svg>Automatic</span>';
    }
    if (a.mode === 'stack' || a.mode === 'download') {
      const kind = a.mode === 'download' ? 'download' : 'stack';
      return '<button type="button" class="' + stackBtnClass(a.state) + '" data-stack-toggle data-state="' + esc(a.state) +
        '" data-toggle-kind="' + kind + '"' +
        ' data-add-url="' + esc(a.add_url) + '" data-remove-url="' + esc(a.remove_url) + '"' +
        (a.rt ? ' data-rt="' + esc(a.rt) + '" data-rid="' + esc(a.rid) + '"' : '') + '>' +
        stackBtnInner(a.state, kind) + '</button>';
    }
    return '<a class="cc-btn" href="' + esc(a.href) + '">' + esc(a.label || 'Open') + ARROW + '</a>';
  }

  // Build one card. MUST match templates/macros/_catalog_card.html.
  function renderCatalogCard(c) {
    const tags = c.tags || [];
    let tagsHtml = '';
    if (tags.length) {
      tagsHtml = '<div class="cc-tags">' +
        tags.slice(0, 3).map(t => '<span class="cc-tag">' + esc(t) + '</span>').join('') +
        (tags.length > 3 ? '<span class="cc-tag cc-tag--more">+' + (tags.length - 3) + '</span>' : '') +
        '</div>';
    }
    let eyebrow = '<span class="cc-kind cc-kind--' + c.kind + '">' + esc(c.kind_label) + '</span>';
    if (c.curator) eyebrow += '<span class="cc-sep">·</span><span>' + esc(c.curator) + '</span>';
    // v104 trust line — keep in lockstep with the Jinja macro's `publisher` /
    // `verified` block. Only the POSITIVE verification state renders; there is
    // deliberately no "Unverified" label (see the macro header).
    const pub = c.publisher || {};
    if (pub.name) {
      eyebrow += '<span class="cc-sep">·</span><span class="cc-pub">' +
        (pub.kind === 'organization' ? '' : 'by ') + esc(pub.name) + '</span>';
      if (pub.kind === 'organization') {
        // v113: match whichever vocabulary this theme renders server-side, so a
        // JS-hydrated card and a Jinja one never disagree WITHIN a theme.
        // `.ds-trust` is scoped to paper in css/trustmark.css, so emitting it on
        // a blue instance would produce an unstyled marker.
        eyebrow += IS_PAPER
          ? '<span class="ds-trust ds-trust--org ds-trust--label">'
            + '<span class="ds-trust__word">Organization</span></span>'
          : '<span class="cc-trust cc-trust--org">Organization</span>';
      } else if (c.verified) {
        eyebrow += IS_PAPER
          ? '<span class="ds-trust ds-trust--verified ds-trust--label" '
            + 'title="Verified by your organization">' + CHECK_GLYPH
            + '<span class="ds-trust__word">Verified</span></span>'
          : '<span class="cc-trust cc-trust--verified" '
            + 'title="Verified by your organization">' + CHECK_GLYPH + 'Verified</span>';
      }
    }
    if (c.category) eyebrow += '<span class="cc-sep">·</span><span>' + esc(c.category) + '</span>';
    return '<article class="cc-card" data-search="' + esc((c.title + ' ' + (c.description || '')).toLowerCase()) + '">' +
      '<div class="cc-head">' +
        '<span class="cc-icon cc-icon--' + c.kind + '">' + (KIND_GLYPH[c.kind] || '') + '</span>' +
        '<div class="cc-titlewrap"><h3 class="cc-title"><a href="' + esc(c.href) + '">' + esc(c.title) + '</a></h3>' +
        '<div class="cc-eyebrow">' + eyebrow + '</div></div>' +
      '</div>' +
      '<p class="cc-desc">' + esc(c.description || 'No description provided yet.') + '</p>' +
      tagsHtml +
      '<div class="cc-foot"><span class="cc-meta">' + (META_GLYPH[c.meta_icon] || META_GLYPH.items) + '<span>' + esc(c.meta_text) + '</span></span>' +
      footerAction(c.action) + '</div></article>';
  }
  window.renderCatalogCard = renderCatalogCard;

  // ── One delegated Add/Remove toggle for every card on the page ──────
  document.addEventListener('click', async (ev) => {
    const btn = ev.target.closest('[data-stack-toggle]');
    if (!btn) return;
    ev.preventDefault();
    if (btn.disabled) return;
    const adding = btn.dataset.state !== 'in';
    const kind = btn.dataset.toggleKind === 'download' ? 'download' : 'stack';
    // Removals need a confirm. Two flavours: a 'stack' remove is a
    // destructive uninstall (the resource leaves the stack), a 'download'
    // remove only drops the on-disk local copy (re-downloadable), so the
    // copy tailors severity/wording per the two cases.
    if (!adding && typeof window.confirmModal === 'function') {
      const card = btn.closest('.cc-card, [data-stack-row]');
      const nameEl = card && card.querySelector('.cc-title');
      const name = nameEl ? nameEl.textContent.trim() : 'this item';
      const opts = kind === 'download'
        ? { title: 'Remove local copy?',
            message: '"' + name + '" stays in your stack — only the downloaded copy on disk is removed. You can download it again anytime.',
            confirmText: 'Remove local copy', danger: false }
        : { title: 'Remove from stack?',
            message: '"' + name + '" will be removed from your stack.',
            confirmText: 'Remove', danger: true };
      if (!await window.confirmModal(opts)) return;
    }
    btn.disabled = true;
    try {
      let resp;
      if (adding) {
        const opts = { method: 'POST', credentials: 'same-origin' };
        if (btn.dataset.rt) {
          opts.headers = { 'Content-Type': 'application/json' };
          opts.body = JSON.stringify({ resource_type: btn.dataset.rt, resource_id: btn.dataset.rid });
        }
        resp = await fetch(btn.dataset.addUrl, opts);
      } else {
        resp = await fetch(btn.dataset.removeUrl, { method: 'DELETE', credentials: 'same-origin' });
      }
      if (!(resp.ok || resp.status === 204)) throw new Error('HTTP ' + resp.status);
      // On My Stack, removal drops the card (grid) or row (inventory
      // table) for a 'stack' toggle (Marketplace uninstall — the resource
      // is genuinely gone); elsewhere it flips to "Add". A 'download'
      // toggle NEVER hides the row: removing the local copy doesn't drop
      // the resource from the stack (auto-membership keeps it visible),
      // it only reverts to "not yet downloaded".
      if (!adding && kind !== 'download' && btn.closest('[data-remove-hides]')) {
        const card = btn.closest('.cc-card, [data-stack-row]');
        if (card) { card.remove(); return; }
      }
      const next = adding ? 'in' : 'add';
      // The same resource can render more than one card on a page (e.g.
      // the Catalog's "Recommended for you" row + its kind grid) — flip
      // every toggle for it, keyed by resource_type/resource_id.
      let targets = [btn];
      if (btn.dataset.rt && btn.dataset.rid && window.CSS && CSS.escape) {
        targets = document.querySelectorAll(
          '[data-stack-toggle][data-rt="' + CSS.escape(btn.dataset.rt) + '"][data-rid="' + CSS.escape(btn.dataset.rid) + '"]'
        );
      }
      targets.forEach(b => {
        b.dataset.state = next;
        b.className = stackBtnClass(next);
        b.innerHTML = stackBtnInner(next, kind);
      });
    } catch (e) {
      console.error('stack toggle failed', e);
    } finally {
      btn.disabled = false;
    }
  });
})();
