/* Collection detail — "In the documents" (Band 2 of the Files search).
 *
 * The Files section's search box does two genuinely different things:
 *   Band 1 (server-rendered, ?q=)  — substring match over FILE NAMES, cheap,
 *                                     works with no JS, URL shareable.
 *   Band 2 (this file)             — whole-word match over the CONTENTS of
 *                                     every indexed file, via the same
 *                                     GET /api/collections/search the chat
 *                                     agent uses.
 *
 * Band 2 is deliberately NOT part of the page's own request: content search
 * scores every chunk of the corpus in Python (src/ingest/retrieval.py), so a
 * reader who only typed a filename would otherwise pay for a full-corpus
 * scan on every page load. Instead this script fires ONE fetch after the
 * page has already painted, targeting the `#content-search` container the
 * route renders only when `?q=` is set (a blank query has nothing to search
 * — see `library_detail.html`).
 *
 * Every server/API string lands via textContent — never innerHTML — since a
 * snippet is a fragment of a user-uploaded document and is untrusted text,
 * exactly like `global_search.js` and `file_preview.js`.
 */
(function () {
  "use strict";

  var root = document.getElementById("content-search");
  if (!root) return; // no ?q= on this load — nothing to search

  var body = root.querySelector('[data-role="content-search-body"]');
  if (!body) return;

  var corpusId = root.getAttribute("data-corpus-id") || "";
  var slug = root.getAttribute("data-collection-slug") || "";
  var q = root.getAttribute("data-q") || "";
  if (!corpusId || !q) return;

  var SNIPPET_MAX = 220;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function clear() {
    while (body.firstChild) body.removeChild(body.firstChild);
  }

  function snippetOf(text) {
    var t = (text || "").replace(/\s+/g, " ").trim();
    if (t.length <= SNIPPET_MAX) return t;
    return t.slice(0, SNIPPET_MAX).trim() + "…";
  }

  function renderLoading() {
    clear();
    body.appendChild(el("p", "content-search__loading", "Searching document contents…"));
  }

  function renderError() {
    clear();
    body.appendChild(el("p", "content-search__error", "Could not search inside documents right now. Try again in a moment."));
  }

  function renderEmpty(hint) {
    clear();
    body.appendChild(el("p", "content-search__empty", "No document contents matched “" + q + "”."));
    // The API's own `hint` names the engine's real limits (whole-word match,
    // no wildcard) — worth showing verbatim rather than a generic apology.
    if (hint) body.appendChild(el("p", "content-search__hint", hint));
  }

  function resultRow(hit) {
    var row = el("div", "cs-hit");
    var head = el("div", "cs-hit__head");

    var name = el("span", "cs-hit__name");
    if (hit.file_id && slug) {
      var a = el("a", null, hit.filename || "Untitled file");
      a.href = "/library/" + encodeURIComponent(slug) + "/f/" + encodeURIComponent(hit.file_id);
      name.appendChild(a);
    } else {
      name.textContent = hit.filename || "Untitled file";
    }
    head.appendChild(name);

    // A name-only hit is a weaker signal than a real body match — the search
    // API marks it `matched_on: "filename"` for exactly this reason
    // (src/ingest/retrieval.py), and hiding that here would make it look as
    // strong as a passage that actually contains the words.
    if (hit.matched_on === "filename") {
      head.appendChild(el("span", "cs-hit__badge", "name match"));
    }
    row.appendChild(head);

    var snippet = snippetOf(hit.text);
    if (snippet) row.appendChild(el("p", "cs-hit__snippet", snippet));

    return row;
  }

  function renderResults(data) {
    var results = (data && data.results) || [];
    if (!results.length) {
      renderEmpty(data && data.hint);
      return;
    }
    clear();
    // Lexical-only degradation (no `agnes[embeddings]` extra installed) still
    // returns ranked results — but the ranking signal is weaker, and the
    // reader deserves to know which mode answered them.
    if (data.retrieval === "lexical_only") {
      body.appendChild(
        el("p", "content-search__note", "Word-match only for this search (semantic ranking is not installed).")
      );
    }
    results.forEach(function (hit) {
      body.appendChild(resultRow(hit));
    });
  }

  renderLoading();
  fetch(
    "/api/collections/search?q=" + encodeURIComponent(q) + "&corpus_id=" + encodeURIComponent(corpusId) + "&k=10",
    { credentials: "same-origin" }
  )
    .then(function (r) {
      if (!r.ok) throw new Error("http_" + r.status);
      return r.json();
    })
    .then(renderResults)
    .catch(renderError);
})();
