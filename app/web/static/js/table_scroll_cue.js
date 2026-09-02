/* "There is more table this way" — the edge cue for `.data-table-wrap`.

   The wrap scrolls its own overflow (`.data-table-wrap` in style-custom.css)
   and paints an 8px scrollbar so the affordance is not an overlay bar macOS
   hides. That scrollbar sits under the LAST ROW, though — with a dozen rows
   it is several hundred pixels below where the reader is looking, and on a
   long list it is off-screen entirely. So a table could be cut mid-column
   with nothing anywhere near the cut to say so: on /admin/data-packages at a
   900px window, "Shared with" was simply absent and the page looked complete.

   This adds the cue AT the cut, which is the only place it can be seen:
   `is-cue-left` / `is-cue-right` on the wrap, for whichever side still has
   content behind it. The CSS decides what to draw with them (a fade, or a
   shadow under a pinned actions column) — this module only ever answers "is
   there more that way", and answers it for every wrap on the page without
   the page having to opt in.

   No IntersectionObserver, no rAF loop: the state changes on scroll, on
   resize, and when rows are added or removed (four admin tables hydrate from
   JSON after first paint, and /chats and the Library filter rows in and out
   of an already-rendered table). A ResizeObserver on the wrap catches the
   first two — a scroll container resizes when its content does — and a
   MutationObserver on the wrap's own subtree catches the third. Both are cheap and both
   are per-wrap, so a page with no `.data-table-wrap` installs nothing.

   Degrades to today's behaviour, which is why nothing here is guarded by a
   feature test beyond existence: with the script blocked, neither class is
   ever set, no cue is drawn, and the scrollbar still scrolls. */
(function () {
    "use strict";

    /* 1px, not 0: a fractional scroll offset (a zoomed page, a hidpi
       scrollbar) leaves scrollLeft at 0.5 with nothing actually hidden, and
       a cue that draws over an un-cut edge is worse than none — it says
       "scroll" where scrolling does nothing. */
    var EPSILON = 1;

    function update(wrap) {
        var hidden = wrap.scrollWidth - wrap.clientWidth;
        var left = wrap.scrollLeft;
        wrap.classList.toggle("is-cue-left", hidden > EPSILON && left > EPSILON);
        wrap.classList.toggle(
            "is-cue-right",
            hidden > EPSILON && left < hidden - EPSILON,
        );
    }

    function watch(wrap) {
        if (wrap.dataset.cueBound === "1") {
            return;
        }
        wrap.dataset.cueBound = "1";

        wrap.addEventListener("scroll", function () {
            update(wrap);
        }, { passive: true });

        /* The observers are parked ON the element rather than dropped after
           `observe()`: an observer whose only reference is its own
           registration is a shape engines have been known to collect, and a
           cue that stops updating after a garbage collection is the kind of
           bug that reproduces for nobody. */
        var kept = [];
        wrap._agnesCueObservers = kept;

        if (typeof window.ResizeObserver === "function") {
            var ro = new window.ResizeObserver(function () {
                update(wrap);
            });
            ro.observe(wrap);
            kept.push(ro);
        } else {
            window.addEventListener("resize", function () {
                update(wrap);
            });
        }

        /* Rows arriving, leaving, or being hidden by a filter all change
           whether anything is cut, and none of them resize the wrap. */
        if (typeof window.MutationObserver === "function") {
            var mo = new window.MutationObserver(function () {
                update(wrap);
            });
            kept.push(mo);
            mo.observe(wrap, {
                childList: true,
                subtree: true,
                attributes: true,
                attributeFilter: ["hidden", "style", "class"],
            });
        }

        update(wrap);
    }

    function scan() {
        var wraps = document.querySelectorAll(".data-table-wrap");
        for (var i = 0; i < wraps.length; i++) {
            watch(wraps[i]);
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", scan);
    } else {
        scan();
    }

    /* A wrap can also be built after load — /admin/tables hydrates its whole
       layout host from `/api/admin/registry`. Exposed rather than watched
       globally, so a page that injects one can say so; the DOM-ready scan
       covers every server-rendered wrap. */
    window.AgnesTableScrollCue = { scan: scan, update: update };
})();
