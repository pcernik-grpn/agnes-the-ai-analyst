// app/web/static/js/icon_sprite.js
//
// Fetches the vendored Lucide sprite (see vendor/LICENSES.md) once and
// injects it inline at the top of <body>, so icon consumers can reference
// symbols SAME-DOCUMENT: <svg class="ds-icon"><use href="#name"/></svg>.
//
// Why not <use href="/static/vendor/lucide-sprite.svg#name"> directly?
// WebKit only gained external-document <use> resolution in Safari 17.1 —
// every earlier Safari renders such an icon as an empty box. Same-document
// references work everywhere, and <use> resolution is live: an icon element
// created BEFORE this fetch settles renders the moment the sprite lands, so
// nothing needs to wait for it.
//
// Loaded from _app_scripts.html (both base layouts). Idempotent; the sprite
// is a trusted same-origin static asset, which is what makes the innerHTML
// injection below safe.
(function () {
  var ID = "agnes-icon-sprite";
  if (document.getElementById(ID)) return;
  fetch("/static/vendor/lucide-sprite.svg")
    .then(function (r) { return r.ok ? r.text() : Promise.reject(new Error("HTTP " + r.status)); })
    .then(function (svg) {
      if (document.getElementById(ID)) return;
      var holder = document.createElement("div");
      holder.id = ID;
      holder.hidden = true;
      holder.setAttribute("aria-hidden", "true");
      holder.innerHTML = svg;
      document.body.insertBefore(holder, document.body.firstChild);
    })
    .catch(function () { /* icons degrade to empty 1em boxes — never block the page */ });
})();
