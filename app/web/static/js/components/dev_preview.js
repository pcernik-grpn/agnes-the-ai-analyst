/* =====================================================================
 * dev_preview.js — open/close for the local-dev audience switch.
 *
 * Everything else about that switch is links and CSS; this is only the
 * popover. Kept tiny and defensive because it loads on every page: if the
 * markup is not there (any real deployment), it binds nothing and returns.
 * ===================================================================== */

(function () {
  'use strict';

  var root = document.querySelector('[data-devsw]');
  if (!root) return;   // not local dev — the partial rendered nothing

  var toggle = root.querySelector('[data-devsw-toggle]');
  var menu = root.querySelector('[data-devsw-menu]');
  if (!toggle || !menu) return;

  function setOpen(open) {
    menu.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
    if (open) root.setAttribute('data-open', '');
    else root.removeAttribute('data-open');
  }

  toggle.addEventListener('click', function (e) {
    e.stopPropagation();
    setOpen(menu.hidden);
  });

  // Click-away and Escape. Scoped so a page's own handlers are untouched —
  // this is a dev tool sitting on top of someone else's UI, and it should
  // swallow as little as possible.
  document.addEventListener('click', function (e) {
    if (!menu.hidden && !root.contains(e.target)) setOpen(false);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !menu.hidden) { setOpen(false); toggle.focus(); }
  });
})();
