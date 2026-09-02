/* Dismissible "connect anywhere" banner (#1956 item 2).

   Opt-in per call site: the card only carries a close button when its macro
   was given a `dismiss_key` (macros/_connect_banner.html), and this module
   binds whatever it finds. Two jobs, in this order:

     1. hide an already-dismissed card;
     2. bind the close button.

   Deferred rather than inlined, so the hide lands on DOM-ready and not before
   first paint. That is a deliberate trade: the only card with a key today
   sits at the FOOT of a long page, well below the fold on arrival, so there
   is nothing to see flashing. A dismissible banner placed above the fold
   should take the rail's approach instead — an inline bootstrap that reads
   the store before the element paints (see `_app_rail.html`).

   State is per-key in localStorage, not on the user: it is a display
   preference for this reader on this browser, and the app-state schema is
   frozen (CLAUDE.md, A3). Every access is wrapped — Safari's private mode
   throws on localStorage rather than returning null, and a banner is never
   worth taking a page down for. A throwing store degrades to "not dismissed",
   which shows the invitation rather than hiding something the reader wanted. */
(function () {
    "use strict";

    var PREFIX = "agnes.connect_banner.dismissed.";

    function isDismissed(key) {
        try {
            return window.localStorage.getItem(PREFIX + key) === "1";
        } catch (e) {
            return false;
        }
    }

    function remember(key) {
        try {
            window.localStorage.setItem(PREFIX + key, "1");
        } catch (e) {
            /* private mode / quota — the card still closes for this page view */
        }
    }

    function card(btn) {
        return btn.closest(".cbn");
    }

    function apply() {
        var buttons = document.querySelectorAll("[data-cbn-dismiss]");
        Array.prototype.forEach.call(buttons, function (btn) {
            var key = btn.getAttribute("data-cbn-dismiss");
            var el = card(btn);
            if (!key || !el) return;
            if (isDismissed(key)) {
                // The whole wrapper, not just the card: the Library's foot
                // banner sits in a `.lib-connect` box with its own spacing,
                // and hiding only the card would leave that gap behind.
                (el.parentElement && el.parentElement.classList.contains("lib-connect")
                    ? el.parentElement
                    : el
                ).hidden = true;
                return;
            }
            btn.addEventListener("click", function () {
                remember(key);
                var box =
                    el.parentElement && el.parentElement.classList.contains("lib-connect")
                        ? el.parentElement
                        : el;
                box.hidden = true;
            });
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", apply, { once: true });
    } else {
        apply();
    }
})();
