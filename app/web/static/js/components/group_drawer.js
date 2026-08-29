/* =====================================================================
 * group_drawer.js — naming a group, and nothing else.
 *
 * This drawer used to be a four-step flow: Name → People → Access →
 * Review. It grew that way for a good reason — creating a group used to
 * drop you on a table with the two things that MAKE a group (its people,
 * what it can use) on two other pages you had to know to look for — but
 * it fixed that by carrying its own copy of both editors. The product
 * then had three member editors and three grant editors over one pair of
 * tables, and the copies could disagree.
 *
 * The Access workspace (/admin/access) is now the single editor for
 * both, and creating a group SELECTS it there. So the drawer is back to
 * the one job nothing else does — bringing a group into existence, or
 * changing its name and description — and steps 2–4 are gone rather than
 * duplicated. What taught the lesson those steps taught (a group is not
 * just a name) is now the pane you land in, which opens on an empty
 * audience and an empty grant list saying exactly that.
 *
 * One thing did come back, deliberately narrowed: a NEW group can be
 * given its first people here. That is not the member editor returning —
 * there is no roster, no remove, no source column, and the field is gone
 * the moment the group exists. It is seeding, which is the half of "a
 * group is not just a name" that only has a natural moment at creation;
 * everything after that moment is still /admin/access's job.
 *
 *   window.AgnesGroupDrawer.open({ onSaved: (group) => … })   // create
 *   window.AgnesGroupDrawer.open({ group: row, onSaved: … })  // rename
 *
 * `onSaved` fires on close, with the group, only if something was
 * written. Storage is the existing admin API — no endpoint is new here:
 *   POST /api/admin/groups                    (create)
 *   PATCH /api/admin/groups/{id}              (rename / describe)
 *   GET  /api/users?search=                   (find someone to seed)
 *   POST /api/admin/groups/{id}/members       (seed them)
 *
 * Chrome: css/drawer.css (shared) + css/group_drawer.css. Every control
 * inside is a shared component — `.btn`, the drawer's own field rows.
 * ===================================================================== */
(function () {
  'use strict';

  var GROUPS_API = '/api/admin/groups';
  var USERS_API = '/api/users';
  var FIND_LIMIT = 8;

  var els = null;          // built lazily on first open
  var st = null;           // per-open state

  function api(url, opts) {
    opts = opts || {};
    opts.credentials = 'include';
    if (opts.body) opts.headers = { 'Content-Type': 'application/json' };
    return fetch(url, opts).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (b) {
          var d = b && b.detail;
          throw new Error(typeof d === 'string' ? d : 'HTTP ' + r.status);
        });
      }
      return r.status === 204 ? null : r.json();
    });
  }

  /* ── Scaffold ─────────────────────────────────────────────────────── */

  function build() {
    if (els) return els;
    var root = document.createElement('div');
    root.className = 'ds-drawer';
    root.hidden = true;
    // This drawer owns its own Escape / backdrop handling; opt out of the
    // global handler in _app_scripts.html, which hides overlays with an
    // inline display:none and would leave our state half-open.
    root.dataset.noEscClose = '1';
    root.innerHTML =
      '<div class="ds-drawer__backdrop" data-gdw-close></div>' +
      '<div class="ds-drawer__panel" role="dialog" aria-modal="true" aria-labelledby="gdw-title">' +
      '  <div class="ds-drawer__head">' +
      '    <div class="ds-drawer__head-main">' +
      '      <h2 class="ds-drawer__title" id="gdw-title">New group</h2>' +
      '      <p class="ds-drawer__sub" data-gdw="sub"></p>' +
      '    </div>' +
      '    <button type="button" class="ds-drawer__x" data-gdw-close aria-label="Close">&times;</button>' +
      '  </div>' +
      '  <div class="ds-drawer__body" data-gdw="body">' +
      '    <section class="ds-drawer__pane is-on" data-gdw-pane="1">' +
      '      <p class="ds-drawer__lede">A group is the audience every grant is written against.' +
      '        Name it after who they are — <code>data-team</code>, <code>finance</code> — not after' +
      '        what they get.</p>' +
      '      <div class="ds-drawer__field">' +
      '        <label for="gdw-name">Name</label>' +
      '        <input type="text" id="gdw-name" autocomplete="off" placeholder="data-team">' +
      '      </div>' +
      '      <div class="ds-drawer__field">' +
      '        <label for="gdw-desc">Description <span class="gdw-optional">(optional)</span></label>' +
      '        <textarea id="gdw-desc" autocomplete="off" placeholder="Who is in here, and why they exist as a group."></textarea>' +
      '      </div>' +
      '      <div class="ds-drawer__field gdw-people" data-gdw="people">' +
      '        <label for="gdw-find">People <span class="gdw-optional">(optional)</span></label>' +
      '        <p class="gdw-people__lede">Who is in it. You can add the rest later —' +
      '          this is here so a new group does not start out reaching nobody.</p>' +
      '        <div class="gdw-chips" data-gdw="chips"></div>' +
      '        <input type="text" id="gdw-find" autocomplete="off" role="combobox"' +
      '               aria-expanded="false" aria-controls="gdw-found" aria-autocomplete="list"' +
      '               placeholder="Search people by name or address…">' +
      '        <div class="gdw-found" id="gdw-found" role="listbox" hidden></div>' +
      '      </div>' +
      '      <p class="gdw-locked" data-gdw="locked" hidden></p>' +
      // `role="alert"` because this is the only feedback the form gives and
      // it appears without focus moving to it — without the role, a screen
      // reader user presses Create and is told nothing at all.
      '      <div class="ds-drawer__err" data-gdw="err1" id="gdw-err1" role="alert" hidden></div>' +
      '    </section>' +
      '  </div>' +
      '  <div class="ds-drawer__foot">' +
      '    <span class="ds-drawer__foot-gap"></span>' +
      '    <button type="button" class="btn btn-secondary" data-gdw="finish">Cancel</button>' +
      '    <button type="button" class="btn btn-primary" data-gdw="next">Create group</button>' +
      '  </div>' +
      '</div>';
    document.body.appendChild(root);

    els = {
      root: root,
      title: root.querySelector('#gdw-title'),
      sub: root.querySelector('[data-gdw="sub"]'),
      body: root.querySelector('[data-gdw="body"]'),
      name: root.querySelector('#gdw-name'),
      desc: root.querySelector('#gdw-desc'),
      err1: root.querySelector('[data-gdw="err1"]'),
      locked: root.querySelector('[data-gdw="locked"]'),
      people: root.querySelector('[data-gdw="people"]'),
      chips: root.querySelector('[data-gdw="chips"]'),
      find: root.querySelector('#gdw-find'),
      found: root.querySelector('#gdw-found'),
      finish: root.querySelector('[data-gdw="finish"]'),
      next: root.querySelector('[data-gdw="next"]'),
    };

    root.addEventListener('click', onClick);
    // Document-level, not panel-level: focus can legitimately sit outside
    // the panel (a click on the backdrop, the browser's own chrome), and
    // Escape has to close the drawer from there too.
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && st) { e.stopPropagation(); close(); }
    });
    els.name.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); els.next.click(); }
    });
    // Typing a name is the fix for "a name is required", so the complaint
    // goes as soon as there is one.
    els.name.addEventListener('input', function () {
      if (els.name.value.trim() && !els.err1.hidden) clearNameError();
    });
    els.next.addEventListener('click', onNext);
    bindFind();
    els.finish.addEventListener('click', function () { close(); });
    return els;
  }

  /* ── Open / close ─────────────────────────────────────────────────── */

  function open(opts) {
    opts = opts || {};
    build();
    var g = opts.group || null;
    st = {
      group: g,
      mode: g ? 'edit' : 'create',
      changed: false,     // did anything at all get written?
      onSaved: opts.onSaved || function () {},
      restoreFocus: document.activeElement,
    };
    els.name.value = g ? (g.name || '') : '';
    els.desc.value = g ? (g.description || '') : '';
    els.err1.hidden = true;
    // Seeding is a creation-time affordance only. On an existing group the
    // editor behind this drawer is the one that owns membership, and on a
    // Google-synced group nothing here owns it at all.
    st.picked = [];
    els.find.value = '';
    closeFound();
    renderChips();
    els.people.hidden = !!g;
    // A system group's name is fixed and a Google-synced group's fields are
    // Workspace's — the API rejects both edits, so the controls say so
    // rather than collecting a change whose save will 409.
    var managed = !!(g && g.is_google_managed);
    els.name.disabled = !!(g && (g.is_system || managed));
    els.desc.disabled = managed;
    els.locked.hidden = !els.name.disabled;
    els.locked.textContent = managed
      ? 'Synced from Google Workspace — its name, description and members are managed there. What it can use is still yours to set.'
      : (els.name.disabled
          ? 'A built-in group: the name is fixed, but the description, its people and what it can use are yours to set.'
          : '');
    els.root.hidden = false;
    els.root.classList.add('is-open');
    document.body.style.overflow = 'hidden';
    renderHead();
    renderFoot();
    els.body.scrollTop = 0;
    setTimeout(function () {
      if (!els.name.disabled) els.name.focus();
      else els.desc.focus();
    }, 60);
  }

  function close() {
    if (!els) return;
    els.root.classList.remove('is-open');
    els.root.hidden = true;
    document.body.style.overflow = '';
    var s = st;
    st = null;
    if (s) {
      if (s.changed) s.onSaved(s.group);
      if (s.restoreFocus && s.restoreFocus.focus) s.restoreFocus.focus();
    }
  }

  function onClick(e) {
    if (e.target.closest('[data-gdw-close]')) { close(); return; }
    var pick = e.target.closest('[data-gdw-pick]');
    if (pick) {
      st.picked.push({ email: pick.dataset.email, name: pick.dataset.name });
      renderChips();
      els.find.value = '';
      closeFound();
      els.find.focus();
      return;
    }
    var drop = e.target.closest('[data-gdw-unpick]');
    if (drop) {
      var mail = drop.getAttribute('data-gdw-unpick');
      st.picked = st.picked.filter(function (m) { return m.email !== mail; });
      renderChips();
    }
  }

  /* ── Chrome ───────────────────────────────────────────────────────── */

  function renderHead() {
    var g = st.group;
    els.title.textContent = g ? 'Rename group' : 'New group';
    els.sub.textContent = g
      ? 'The group itself. Its people and what it can use are on the page behind this.'
      : 'Name it — its people and what it can use are the next thing you will see.';
  }

  function renderFoot() {
    els.finish.textContent = 'Cancel';
    els.next.textContent = st.group ? 'Save' : 'Create group';
    els.next.disabled = false;
  }

  function onNext() {
    saveNameThen(function () { seedMembers(close); });
  }

  /* ── Seeding the new group ─────────────────────────────────────────
     Runs once, after the group exists, against the same endpoint the
     Access page uses. Failures are reported and the drawer STAYS OPEN
     with the ones that failed still in the field: the group itself was
     created either way, so closing on a partial result would hide the
     half that did not happen behind a success. */
  function seedMembers(done) {
    var picked = (st && st.picked) || [];
    var g = st && st.group;
    if (!picked.length || !g || !g.id) { done(); return; }
    els.next.disabled = true;
    var base = GROUPS_API + '/' + encodeURIComponent(g.id) + '/members';
    var failed = [];
    var chain = picked.reduce(function (acc, person) {
      return acc.then(function () {
        return api(base, { method: 'POST', body: JSON.stringify({ email: person.email }) })
          .catch(function () { failed.push(person); });
      });
    }, Promise.resolve());
    chain.then(function () {
      els.next.disabled = false;
      if (!failed.length) { done(); return; }
      st.picked = failed;
      renderChips();
      els.err1.textContent = failed.length === picked.length
        ? 'The group was created, but nobody could be added to it. Try again, or add them on the Access page.'
        : 'The group was created. ' + failed.length + ' of these could not be added — they are still listed above.';
      els.err1.hidden = false;
    });
  }

  /* ── Finding someone to seed ───────────────────────────────────────
     The query goes to the server, like the Access page's own person
     search, for the same reason: a prefetched copy of the org silently
     stops finding people at whatever limit it was given, which reads as
     "that person has no account". */
  var findTimer = null;
  var findSeq = 0;

  function closeFound() {
    if (!els) return;
    els.found.hidden = true;
    els.found.innerHTML = '';
    els.find.setAttribute('aria-expanded', 'false');
  }

  function bindFind() {
    els.find.addEventListener('input', function () {
      clearTimeout(findTimer);
      var q = els.find.value;
      findTimer = setTimeout(function () { runFind(q); }, 200);
    });
    els.find.addEventListener('keydown', function (e) {
      // Enter inside the search must not reach the panel's Enter-to-create.
      if (e.key === 'Enter') {
        e.preventDefault();
        e.stopPropagation();
        var first = els.found.querySelector('[data-gdw-pick]');
        if (first) first.click();
        return;
      }
      if (e.key === 'Escape' && !els.found.hidden) { e.stopPropagation(); closeFound(); }
    });
  }

  function runFind(raw) {
    var q = String(raw || '').trim();
    if (!q) { closeFound(); return; }
    var seq = ++findSeq;
    api(USERS_API + '?search=' + encodeURIComponent(q) + '&limit=' + FIND_LIMIT)
      .catch(function () { return []; })
      .then(function (people) {
        if (seq !== findSeq || !st) return;
        if (!Array.isArray(people)) people = (people && people.users) || [];
        var taken = {};
        (st.picked || []).forEach(function (m) { taken[m.email] = true; });
        var rows = people.filter(function (u) { return !taken[u.email]; }).map(function (u) {
          return '<button type="button" class="gdw-found__row" role="option"' +
                 ' data-gdw-pick data-email="' + esc(u.email || '') + '"' +
                 ' data-name="' + esc(u.name || '') + '">' +
                 '<span class="gdw-found__who">' + esc(u.name || u.email || '') + '</span>' +
                 (u.name ? '<span class="gdw-found__mail">' + esc(u.email || '') + '</span>' : '') +
                 '</button>';
        }).join('');
        els.found.innerHTML = rows ||
          '<p class="gdw-found__none">No account matches that. People without an account' +
          ' are invited on the Access page once this group exists.</p>';
        els.found.hidden = false;
        els.find.setAttribute('aria-expanded', 'true');
      });
  }

  function renderChips() {
    var picked = (st && st.picked) || [];
    els.chips.innerHTML = picked.map(function (m) {
      return '<span class="gdw-chip">' + esc(m.name || m.email) +
             '<button type="button" class="gdw-chip__x" data-gdw-unpick="' + esc(m.email) + '"' +
             ' aria-label="Remove ' + esc(m.email) + '">&times;</button></span>';
    }).join('');
    els.chips.hidden = !picked.length;
  }

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /* ── Step 1: the group itself ─────────────────────────────────────── */

  /* The validation message used to sit there while you typed a perfectly
     good name, and only cleared on the NEXT submit — so the form told you it
     was wrong the whole time you were fixing it. */
  function clearNameError() {
    els.err1.hidden = true;
    els.name.removeAttribute('aria-invalid');
    els.name.removeAttribute('aria-describedby');
  }

  function saveNameThen(done) {
    var name = els.name.value.trim();
    clearNameError();
    var description = els.desc.value.trim();
    els.err1.hidden = true;
    if (!name) {
      els.err1.textContent = 'A name is required — it is what every grant is written against.';
      els.err1.hidden = false;
      els.name.setAttribute('aria-invalid', 'true');
      els.name.setAttribute('aria-describedby', 'gdw-err1');
      els.name.focus();
      return;
    }
    var g = st.group;
    var unchanged = g && g.name === name && (g.description || '') === description;
    if (unchanged) { done(); return; }

    els.next.disabled = true;
    var req = g
      ? api(GROUPS_API + '/' + encodeURIComponent(g.id), {
          method: 'PATCH',
          body: JSON.stringify({
            // A system/google-managed name cannot change; send only what may.
            name: els.name.disabled ? undefined : name,
            description: description || null,
          }),
        })
      : api(GROUPS_API, {
          method: 'POST',
          body: JSON.stringify({ name: name, description: description || null }),
        });

    req.then(function (saved) {
      st.group = saved;
      st.changed = true;
      renderHead();
      done();
    }).catch(function (e) {
      els.err1.textContent = (g ? 'Could not save: ' : 'Could not create the group: ') + e.message;
      els.err1.hidden = false;
    }).then(function () { els.next.disabled = false; });
  }

  window.AgnesGroupDrawer = { open: open, close: close };
})();
