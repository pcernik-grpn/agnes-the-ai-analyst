/* =====================================================================
 * share_dialog.js — the ONE owner-facing sharing dialog.
 *
 * Groups come from GET /api/sharing/groups (the caller's memberships +
 * Everyone); the current state from GET /api/sharing/{type}/{id}; saving
 * is one idempotent PUT of the desired group set. The server preserves
 * grants to groups the caller can't share into, so an owner can never
 * revoke an admin's grant from here.
 *
 * Two ways in:
 *
 *   1. Declarative — any element carrying `data-share-open` plus
 *      `data-share-type` / `data-share-id` / `data-share-title` opens the
 *      dialog on click. When that element is a visibility badge
 *      (`.detail-vis`, `.lib-vis`) it is re-rendered from the saved state,
 *      so the badge the caller clicked IS both the control and the
 *      read-out. This is what makes the badge on a detail page work:
 *      before this, the badge was inert and the sharing control was a
 *      "Manage sharing" button that navigated to a dead URL.
 *
 *   2. Programmatic — window.openShareDialog({resourceType, resourceId,
 *      title, note, onSaved}). `onSaved(state, trigger)` opts out of the
 *      default badge refresh, which is what the Library table needs: a
 *      row carries more sharing-derived state than its badge (the sort
 *      label, the ownership facet) and owns updating all of it.
 *
 * The dialog markup is built once, lazily, and appended to <body>; there
 * is no per-page copy to keep in sync. Chrome lives in
 * css/share_dialog.css.
 * ===================================================================== */
(function () {
  'use strict';

  /* One vocabulary for every surface — the same words `VISIBILITY_LABELS`
     (app/services/artefact_access.py) renders server-side, so a badge that
     said "Private" before the save and a badge re-rendered here after it
     are never two different dialects. */
  var LABELS = { private: 'Private', shared: 'Specific groups', workspace: 'Everyone' };
  var TITLES = {
    private: 'Only you can see this',
    shared: 'Shared with specific groups',
    workspace: 'Everyone in the organization can see this',
  };
  var LOCK_SVG = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="5" y="10.5" width="14" height="9" rx="2" stroke="currentColor" stroke-width="1.9"/><path d="M8 10.5V8a4 4 0 0 1 8 0v2.5" stroke="currentColor" stroke-width="1.9"/></svg>';
  var SHARE_SVG = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="18" cy="6" r="2.6" stroke="currentColor" stroke-width="1.9"/><circle cx="6" cy="12" r="2.6" stroke="currentColor" stroke-width="1.9"/><circle cx="18" cy="18" r="2.6" stroke="currentColor" stroke-width="1.9"/><path d="M8.4 10.8l7.2-3.6M8.4 13.2l7.2 3.6" stroke="currentColor" stroke-width="1.9"/></svg>';
  var CARET_SVG = '<svg class="detail-vis__caret" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m6 9 6 6 6-6" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  var els = null;          // built lazily on first open
  var groupsCache = null;  // /api/sharing/groups is per-caller, not per-item
  var current = null;      // {resourceType, resourceId, title, onSaved, trigger}

  function build() {
    if (els) return els;
    var root = document.createElement('div');
    root.className = 'share-dialog';
    root.hidden = true;
    // This dialog owns its own Escape + backdrop handling; opt out of the
    // global handler in _app_scripts.html, which would hide it with an
    // inline `display: none` and leave `hidden` unset.
    root.dataset.noEscClose = '1';
    root.innerHTML =
      '<div class="share-dialog__backdrop" data-share-close></div>' +
      '<div class="share-dialog__card" role="dialog" aria-modal="true" aria-labelledby="share-dialog-title">' +
      '  <div class="share-dialog__head">' +
      '    <h2 id="share-dialog-title">Share</h2>' +
      '    <button type="button" class="share-dialog__x" data-share-close aria-label="Close">&times;</button>' +
      '  </div>' +
      '  <p class="share-dialog__subject"></p>' +
      '  <div class="share-dialog__loading">Loading groups…</div>' +
      '  <div class="share-dialog__groups"></div>' +
      '  <p class="share-dialog__note" hidden></p>' +
      '  <div class="share-dialog__err" hidden></div>' +
      '  <div class="share-dialog__foot">' +
      '    <button type="button" class="btn btn-secondary" data-share-close>Cancel</button>' +
      '    <button type="button" class="btn btn-primary" data-share-save>Save</button>' +
      '  </div>' +
      '</div>';
    document.body.appendChild(root);
    els = {
      root: root,
      subject: root.querySelector('.share-dialog__subject'),
      loading: root.querySelector('.share-dialog__loading'),
      groups: root.querySelector('.share-dialog__groups'),
      note: root.querySelector('.share-dialog__note'),
      err: root.querySelector('.share-dialog__err'),
      save: root.querySelector('[data-share-save]'),
    };
    root.querySelectorAll('[data-share-close]').forEach(function (b) {
      b.addEventListener('click', close);
    });
    els.save.addEventListener('click', save);
    return els;
  }

  // Closing returns focus to whatever opened the dialog — the badge, which is
  // where a keyboard caller was and where the state they just changed is shown.
  function close() {
    if (!els || els.root.hidden) return;
    els.root.hidden = true;
    var back = current && current.trigger;
    if (back && back.isConnected && typeof back.focus === 'function') back.focus();
  }
  function isOpen() { return !!els && !els.root.hidden; }
  function showErr(msg) { els.err.textContent = msg; els.err.hidden = false; }

  function loadGroups() {
    if (groupsCache) return Promise.resolve(groupsCache);
    return fetch('/api/sharing/groups', { credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (gs) { groupsCache = gs || []; return groupsCache; });
  }

  function open(opts) {
    opts = opts || {};
    if (!opts.resourceType || !opts.resourceId) return;
    build();
    current = opts;
    els.root.hidden = false;
    els.err.hidden = true;
    els.note.hidden = true;
    els.groups.innerHTML = '';
    els.loading.hidden = false;
    els.save.disabled = true;
    els.subject.innerHTML = 'Choose who can use <b></b>.';
    els.subject.querySelector('b').textContent = opts.title || 'this item';

    var type = encodeURIComponent(opts.resourceType);
    var id = encodeURIComponent(opts.resourceId);
    Promise.all([
      loadGroups(),
      fetch('/api/sharing/' + type + '/' + id, { credentials: 'same-origin' })
        .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }),
    ]).then(function (out) {
      var groups = out[0];
      var selected = {};
      (out[1].group_ids || []).forEach(function (g) { selected[g] = true; });
      // Track C6: an agent share an owner requested may be queued rather
      // than granted yet — checked (it reflects what the owner asked for)
      // but flagged, so the dialog never silently implies access exists.
      var pending = {};
      (out[1].pending_group_ids || []).forEach(function (g) { pending[g] = true; });
      els.loading.hidden = true;
      if (!groups.length) {
        els.note.textContent = "You're not in any groups yet, so there's nobody to share with. "
          + 'Ask an admin to add you to a group.';
        els.note.hidden = false;
        return;
      }
      groups.forEach(function (g) {
        var label = document.createElement('label');
        label.className = 'share-dialog__grp';
        var cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.value = g.id;
        cb.checked = !!selected[g.id] || !!pending[g.id];
        var name = document.createElement('span');
        name.textContent = g.name;
        label.appendChild(cb);
        // The mark says GROUP. This list sits beside "Everyone" and the
        // item's own name, and a bare word here read as neither.
        if (window.AgnesKindGlyph) {
          var mark = document.createElement('span');
          mark.innerHTML = window.AgnesKindGlyph.groupGlyph();
          label.appendChild(mark.firstChild);
        }
        label.appendChild(name);
        if (g.is_everyone) {
          var hint = document.createElement('span');
          hint.className = 'share-dialog__grp-hint';
          hint.textContent = 'everyone here';
          label.appendChild(hint);
        } else if (pending[g.id]) {
          var pendingHint = document.createElement('span');
          pendingHint.className = 'share-dialog__grp-hint';
          pendingHint.textContent = 'pending admin approval';
          label.appendChild(pendingHint);
        }
        els.groups.appendChild(label);
      });
      // The item's own sentence when the caller passed one (a file's sharing
      // is not its folder's), otherwise the rule that always applies.
      els.note.textContent = opts.note
        || 'Unchecking every group makes this private to you again.';
      els.note.hidden = false;
      els.save.disabled = false;
      // The checklist IS the dialog, so land the caller on it rather than
      // leaving focus behind on the badge they opened it from.
      var first = els.groups.querySelector('input[type=checkbox]');
      if (first) first.focus();
    }).catch(function (e) {
      els.loading.hidden = true;
      showErr('Could not load sharing: ' + (e && e.message ? e.message : e));
    });
  }

  function save() {
    if (!current) return;
    var ids = Array.prototype.slice
      .call(els.groups.querySelectorAll('input[type=checkbox]'))
      .filter(function (cb) { return cb.checked; })
      .map(function (cb) { return cb.value; });
    els.save.disabled = true;
    els.save.textContent = 'Saving…';
    els.err.hidden = true;
    var opts = current;
    fetch('/api/sharing/' + encodeURIComponent(opts.resourceType) + '/'
          + encodeURIComponent(opts.resourceId), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ group_ids: ids }),
    }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      // Track C6: 202 means at least one group was queued for admin
      // approval rather than granted — carry the status through so the
      // toast can say so instead of claiming sharing is already updated.
      var queued = r.status === 202;
      return r.json().then(function (state) { return { state: state, queued: queued }; });
    }).then(function (result) {
      var state = result.state;
      if (typeof opts.onSaved === 'function') opts.onSaved(state, opts.trigger);
      else refreshBadge(opts.trigger, state.visibility);
      close();
      if (window.appToast) {
        var msg = result.queued
          ? 'Share requested — waiting for admin approval'
          : (state.visibility === 'private' ? 'Sharing turned off' : 'Sharing updated');
        window.appToast({ kind: 'success', msg: msg });
      }
    }).catch(function (e) {
      showErr('Could not save: ' + (e && e.message ? e.message : e));
    }).then(function () {
      els.save.disabled = false;
      els.save.textContent = 'Save';
    });
  }

  /* Re-render the badge that opened the dialog, in place. The state word,
     the glyph and the tint all follow the saved visibility — a badge left
     saying "Private" after the caller shared the item is the bug this
     replaces the old page-reload with. The badge KEEPS its control nature
     (button, caret, tooltip): it is still the way to change this. */
  function refreshBadge(el, visibility) {
    if (!el) return;
    var base = el.classList.contains('lib-vis') ? 'lib-vis' : 'detail-vis';
    if (!el.classList.contains(base)) return;
    var label = LABELS[visibility] || 'Private';
    el.className = base + ' ' + base + '--' + visibility + ' ' + base + '--editable';
    el.innerHTML = (visibility === 'private' ? LOCK_SVG : SHARE_SVG) + ' ' + label
      + CARET_SVG.replace('detail-vis__caret', base + '__caret');
    var title = label + ' — ' + (TITLES[visibility] || '').toLowerCase()
      + '. Change who can see ' + (el.dataset.shareTitle || 'this item') + '.';
    el.setAttribute('title', title);
    el.setAttribute('aria-label', 'Sharing: ' + title);
  }

  // Declarative entry point. preventDefault/stopPropagation so using the
  // control never doubles as "open this item" (a badge can sit inside a link).
  document.addEventListener('click', function (ev) {
    var el = ev.target.closest ? ev.target.closest('[data-share-open]') : null;
    if (!el) return;
    ev.preventDefault();
    ev.stopPropagation();
    open({
      resourceType: el.dataset.shareType,
      resourceId: el.dataset.shareId,
      title: el.dataset.shareTitle,
      note: el.dataset.shareNote || '',
      trigger: el,
    });
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && isOpen()) close();
  });

  window.openShareDialog = open;
  window.closeShareDialog = close;
})();
