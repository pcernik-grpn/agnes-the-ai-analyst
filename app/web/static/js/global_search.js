/* Global search — combobox over GET /api/knowledge/search (K2, #797).
   Wires the #global-search input + #globalSearchResults listbox in the rail
   chrome (`_app_rail.html`). The markup shipped on the topnav chrome first;
   when that was retired (Wave 0, 2026-08) the box moved into the rail with
   the same two ids, so this module's binding is unchanged — only the result
   rows' classes carry the rail's prefix now (`.rail-search-*`, rail.css).
   The null-guard below stays: the rail is gated on `session.user`, so an
   unauthenticated page loads this script with nothing to bind to.
   Debounces input, groups results by type (Tables /
   Knowledge / Documents), and links each hit to its detail page:
     - table     -> /catalog/t/<table_id> (falls back to /catalog)
     - metric    -> /semantic-layer?tab=all_metrics   (#1108, retargeted #1707)
     - glossary  -> /semantic-layer?tab=all_glossary  (#1108, retargeted #1707)
     - knowledge -> /corporate-memory
     - chunk     -> /library
   All API-derived strings are set via textContent — never innerHTML — so a
   malicious document title / table name can't inject markup.
   No global hotkey: Cmd/Ctrl-K is already the admin/chat command palette. */
(function () {
    "use strict";

    var input = document.getElementById("global-search");
    var panel = document.getElementById("globalSearchResults");
    if (!input || !panel) return;

    var DEBOUNCE_MS = 250;
    var MIN_CHARS = 2;

    var GROUPS = [
        { type: "table", heading: "Tables" },
        { type: "metric", heading: "Metrics" },
        { type: "glossary", heading: "Glossary" },
        { type: "knowledge", heading: "Knowledge" },
        { type: "chunk", heading: "Documents" },
    ];
    var TYPE_LABELS = { table: "Table", metric: "Metric", glossary: "Term", knowledge: "Knowledge", chunk: "Document" };

    var debounceTimer = null;
    var activeController = null;

    function hrefFor(hit) {
        if (hit.type === "table") {
            return hit.table_id ? "/catalog/t/" + encodeURIComponent(hit.table_id) : "/catalog";
        }
        // The flat registries are tabs of the model list since #1707 N5. The
        // TAB rides the query string, not a fragment: a fragment never reaches
        // the server, so `#metrics` could only ever have been a client-side
        // hint and the old /catalog/semantics 308 cannot read it.
        if (hit.type === "metric") return "/semantic-layer?tab=all_metrics";
        if (hit.type === "glossary") return "/semantic-layer?tab=all_glossary";
        if (hit.type === "knowledge") return "/corporate-memory";
        if (hit.type === "chunk") return "/library";
        return "#";
    }

    function titleFor(hit) {
        if (hit.type === "table") return hit.name || "Untitled table";
        if (hit.type === "metric") return hit.display_name || hit.name || "Metric";
        if (hit.type === "glossary") return hit.term || "Term";
        if (hit.type === "knowledge") return hit.title || "Untitled";
        if (hit.type === "chunk") return hit.filename || "Document";
        return "";
    }

    /* Second line, for the two DEFINITION types only.

       Looking a term up is a complete job in itself — "what do we mean by
       active account?" — and it was the one job this panel could finish on the
       spot but didn't: it showed the word you had just typed, as a link to a
       page you then had to scan for it. The API has carried the answer the
       whole time (`definition` on a glossary hit, `description` on a metric),
       so rendering it here ends the lookup in the dropdown and leaves the
       navigation for people who want the surrounding page.

       Deliberately not extended to the other three types. A table, a knowledge
       item and a document are things you go TO; a snippet under them would be a
       preview of a destination, which is a different (and much longer) piece of
       text. Definitions are short by construction — the server already caps a
       glossary definition at 280 chars — so this line stays one line. */
    function definitionFor(hit) {
        var text = "";
        if (hit.type === "glossary") text = hit.definition || "";
        else if (hit.type === "metric") text = hit.description || "";
        return text.replace(/\s+/g, " ").trim();
    }

    function setExpanded(open) {
        input.setAttribute("aria-expanded", open ? "true" : "false");
    }

    function closePanel() {
        panel.setAttribute("hidden", "");
        panel.textContent = "";
        setExpanded(false);
    }

    function openPanel() {
        panel.removeAttribute("hidden");
        setExpanded(true);
    }

    function renderMessage(message) {
        panel.textContent = "";
        var el = document.createElement("div");
        el.className = "rail-search-empty";
        el.textContent = message;
        panel.appendChild(el);
        openPanel();
    }

    function renderResults(results) {
        panel.textContent = "";
        if (!results || !results.length) {
            renderMessage("No results.");
            return;
        }
        GROUPS.forEach(function (group) {
            var hits = results.filter(function (r) { return r.type === group.type; });
            if (!hits.length) return;
            var heading = document.createElement("div");
            heading.className = "rail-search-group";
            heading.textContent = group.heading;
            panel.appendChild(heading);
            hits.forEach(function (hit) {
                var row = document.createElement("a");
                row.className = "rail-search-result";
                row.setAttribute("role", "option");
                row.href = hrefFor(hit);

                /* Title (+ definition) stack on the left, badge pinned right.
                   The wrapper is what lets a hit be two lines tall without the
                   badge dropping under the title — the row stays one flex line
                   of two children whether or not the second line is there. */
                var mainEl = document.createElement("div");
                mainEl.className = "rail-search-result-main";

                var titleEl = document.createElement("span");
                titleEl.className = "rail-search-result-title";
                titleEl.textContent = titleFor(hit);
                mainEl.appendChild(titleEl);

                var definition = definitionFor(hit);
                if (definition) {
                    var defEl = document.createElement("span");
                    defEl.className = "rail-search-result-def";
                    /* textContent, like every other API-derived string here —
                       a definition is admin/sync-authored text, not markup. */
                    defEl.textContent = definition;
                    mainEl.appendChild(defEl);
                }

                var badgeEl = document.createElement("span");
                badgeEl.className = "rail-search-result-badge";
                badgeEl.textContent = TYPE_LABELS[hit.type] || hit.type;

                row.appendChild(mainEl);
                row.appendChild(badgeEl);
                panel.appendChild(row);
            });
        });
        openPanel();
    }

    function runSearch(query) {
        if (activeController) activeController.abort();
        activeController = ("AbortController" in window) ? new AbortController() : null;
        var url = "/api/knowledge/search?q=" + encodeURIComponent(query) + "&k=8";
        fetch(url, {
            credentials: "same-origin",
            signal: activeController ? activeController.signal : undefined,
        })
            .then(function (r) {
                if (!r.ok) throw new Error("http_" + r.status);
                return r.json();
            })
            .then(function (data) {
                renderResults(data && data.results);
            })
            .catch(function (err) {
                if (err && err.name === "AbortError") return;
                renderMessage("Search failed. Try again.");
            });
    }

    input.addEventListener("input", function () {
        var q = input.value.trim();
        if (debounceTimer) clearTimeout(debounceTimer);
        if (q.length < MIN_CHARS) {
            closePanel();
            return;
        }
        debounceTimer = setTimeout(function () { runSearch(q); }, DEBOUNCE_MS);
    });

    input.addEventListener("keydown", function (e) {
        if (e.key === "Escape") {
            closePanel();
            input.blur();
        }
    });

    document.addEventListener("click", function (e) {
        if (panel.contains(e.target) || e.target === input) return;
        closePanel();
    });
})();
