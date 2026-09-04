/* Global UI helpers loaded by base.html (not by base_login.html — login pages
   have no nav and no toasts, so the helpers aren't reachable there).
   Two responsibilities for now:
   - wireDropdown: open/close + click-outside + Escape for the user menu and
     the Admin nav dropdown. Used by _app_rail.html.
   - More helpers (window.appToast, etc.) added later as the design system
     primitives need JS sidecars. */
(function () {
    "use strict";

    function wireDropdown(triggerId, panelId) {
        var trigger = document.getElementById(triggerId);
        var panel = document.getElementById(panelId);
        if (!trigger || !panel) return;
        // The primary nav's #navMore panel lives inside `.app-header-nav
        // .is-priority`, which sets `overflow: hidden` so the priority-plus
        // JS can measure available width. That same clip crops the open
        // dropdown to the nav's own (thin) height. Toggle it off for the
        // duration the panel is open — a no-op for dropdowns (user/admin
        // menu) that aren't nested inside that overflow-clipped ancestor.
        var clippedAncestor = panel.closest(".is-priority");
        function setOpen(open) {
            trigger.setAttribute("aria-expanded", open ? "true" : "false");
            if (open) {
                panel.removeAttribute("hidden");
                if (clippedAncestor) clippedAncestor.style.overflow = "visible";
            } else {
                panel.setAttribute("hidden", "");
                if (clippedAncestor) clippedAncestor.style.overflow = "";
            }
        }
        trigger.addEventListener("click", function () {
            // No stopPropagation — let the click bubble to the document
            // handler so any OTHER open dropdown's handler can close
            // itself ("only one menu open at a time" behaviour).
            setOpen(trigger.getAttribute("aria-expanded") !== "true");
        });
        document.addEventListener("click", function (e) {
            // Use trigger.contains(target) instead of strict equality —
            // clicking the chevron <svg> inside the button reports the
            // svg / path as e.target, which would otherwise trip the
            // close branch immediately after opening.
            if (!panel.contains(e.target) && !trigger.contains(e.target)) {
                setOpen(false);
            }
        });
        document.addEventListener("keydown", function (e) {
            if (e.key !== "Escape") return;
            // Only react when THIS dropdown is actually open. The handler used
            // to close-and-focus unconditionally, so ANY Escape anywhere on the
            // page pulled focus onto this trigger — Escaping out of an
            // unrelated menu or dialog dumped the user on the profile row, and
            // any component that returns focus to its own trigger on Escape had
            // it stolen straight back (this is registered on `document`, so it
            // runs after the component's own handler).
            if (trigger.getAttribute("aria-expanded") !== "true") return;
            setOpen(false);
            trigger.focus();
        });
    }

    // Priority-plus navigation — keeps the primary nav on a single row and
    // moves the lowest-priority links (from the end: Memory first, back toward
    // Dashboard) into a "More" overflow menu when they'd otherwise overflow,
    // BEFORE anything shrinks or clips. Progressive enhancement: without this
    // the nav simply wraps (CSS fallback). Markup: #primaryNav > a.app-nav-link
    // items + a trailing #navMore (button #navMoreTrigger, panel #navMorePanel).
    function initPriorityNav() {
        var nav = document.getElementById("primaryNav");
        var more = document.getElementById("navMore");
        var panel = document.getElementById("navMorePanel");
        var trigger = document.getElementById("navMoreTrigger");
        if (!nav || !more || !panel || !trigger) return;

        // Managed links in priority order (DOM order; last = lowest priority).
        var items = Array.prototype.slice.call(
            nav.querySelectorAll(":scope > a.app-nav-link")
        );
        if (!items.length) return;

        nav.classList.add("is-priority");   // CSS: single measured row, no wrap

        function reflectActive() {
            // Keep the selected state visible when the active page's link is
            // tucked inside the overflow menu.
            trigger.classList.toggle(
                "is-active", panel.querySelector(".is-active") != null
            );
        }

        var laying = false;   // re-entrancy guard (layout mutates the DOM, which
                              // can re-trigger the ResizeObserver below).
        function layout() {
            if (laying) return;
            laying = true;

            // 1. Reset — every managed link back inline, just before #navMore.
            items.forEach(function (a) { nav.insertBefore(a, more); });
            more.hidden = true;

            // 2. Fits as-is? Done.
            if (nav.scrollWidth <= nav.clientWidth) {
                reflectActive();
                laying = false;
                return;
            }

            // 3. Reveal More (it consumes row width) and move items from the
            //    end into the panel until the inline row fits.
            more.hidden = false;
            var i = items.length - 1;
            while (i >= 0 && nav.scrollWidth > nav.clientWidth) {
                panel.insertBefore(items[i], panel.firstChild);
                i--;
            }
            if (!panel.children.length) more.hidden = true;
            reflectActive();
            laying = false;
        }

        // Re-run whenever the header's width changes. ResizeObserver fires
        // reliably (unlike a resize+rAF path in backgrounded tabs) and also
        // catches container-driven width changes that aren't window resizes.
        var host = nav.closest(".app-header") || nav;
        if (typeof ResizeObserver !== "undefined") {
            new ResizeObserver(function () { layout(); }).observe(host);
        } else {
            window.addEventListener("resize", layout);
        }
        layout();
    }

    // Toast helper — paired with .toast / .toast-container CSS in style-custom.css.
    // Usage: window.appToast({kind: "success", msg: "Saved", timeout: 4000})
    //        window.appToast("Saved")   // shorthand: message only, kind=info
    //
    // The toast is the channel for RECEIPTS — "saved", "removed", "loaded" —
    // outcomes the reader acknowledges and moves on from. Anything the reader
    // has to act on (a validation failure naming a field) belongs on the page,
    // next to the thing that has to change, not in a strip that times out.
    // ── the shared inline-status element ────────────────────────────────
    // One builder for every surface that says "this happened": the global
    // toast below, chat's transcript notes, and the upload dialogs' error
    // slots. Lives here rather than in a module because app.js loads on every
    // page as a classic script, so both a module (chat.js) and inline page
    // scripts can reach it — the same reason window.appToast and
    // window.confirmModal live here.
    //
    // Tone is carried by the glyph alone; see the `.notice` block in
    // style-custom.css for why the surface no longer changes colour.
    var NOTICE_GLYPHS = {
        // A clock: come back in a moment. The default for anything transient.
        wait: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 1.8"/></svg>',
        // A triangle: we failed. The only tone that spends a colour.
        error: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4 2.7 20h18.6z"/><path d="M12 10v4"/><path d="M12 17.2h.01"/></svg>',
        info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 7.8h.01"/></svg>',
        ok: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M8.2 12.4l2.6 2.6 5-5.4"/></svg>',
    };
    // Kinds callers already pass, mapped onto the four glyphs above.
    var NOTICE_GLYPH_FOR = { warn: "wait", wait: "wait", error: "error", ok: "ok", success: "ok", info: "info" };

    /**
     * Build a .notice element. `placement` is "inline" (in the content) or
     * "floating" (over it). Returns the element; the caller decides where it
     * goes and whether it auto-dismisses.
     */
    function agnesNotice(text, kind, opts) {
        opts = opts || {};
        var k = NOTICE_GLYPH_FOR[kind] ? kind : "info";
        var glyph = NOTICE_GLYPHS[NOTICE_GLYPH_FOR[k]];
        var el = document.createElement("div");
        el.className = "notice notice--" + (opts.placement === "floating" ? "floating" : "inline") + " is-" + k;
        if (opts.extraClass) el.className += " " + opts.extraClass;
        var icon = document.createElement("span");
        icon.className = "notice__icon";
        icon.setAttribute("aria-hidden", "true");
        // Static, author-controlled markup from the table above — never
        // caller text, which goes through textContent below.
        icon.innerHTML = glyph;
        el.appendChild(icon);
        var msg = document.createElement("span");
        msg.className = "notice__msg";
        msg.textContent = String(text == null ? "" : text);
        el.appendChild(msg);
        return el;
    }
    window.agnesNotice = agnesNotice;

    var TOAST_MAX = 4;   // a repeated action must not paper over the page
    function ensureToastContainer() {
        var c = document.getElementById("appToastContainer");
        if (c) return c;
        c = document.createElement("div");
        c.id = "appToastContainer";
        c.className = "toast-container";
        // Announced, or a screen-reader user gets no receipt at all.
        c.setAttribute("role", "status");
        c.setAttribute("aria-live", "polite");
        document.body.appendChild(c);
        return c;
    }
    function appToast(opts) {
        // Eight existing call sites pass a bare string (`appToast("Dismissed.")`),
        // which read `opts.msg` as undefined and showed an EMPTY toast. Accept
        // both shapes rather than chase the callers.
        if (typeof opts === "string") opts = { msg: opts };
        opts = opts || {};
        var kind = opts.kind || "info";
        var msg = String(opts.msg || "");
        var timeout = opts.timeout == null ? 4000 : opts.timeout;

        // Composes the shared .notice (icon + message) and keeps the .toast
        // hooks the container's positioning and the existing guards use.
        var el = agnesNotice(msg, kind, { placement: "floating", extraClass: "toast" });
        el.querySelector(".notice__msg").classList.add("toast-msg");
        var text = el.querySelector(".toast-msg");
        // A real dismiss control: click-anywhere stays, but an affordance that
        // only exists as `cursor: pointer` is invisible to anyone not already
        // hovering it — and unreachable by keyboard.
        var close = document.createElement("button");
        close.type = "button";
        close.className = "toast-x";
        close.setAttribute("aria-label", "Dismiss");
        close.textContent = "✕";
        el.appendChild(close);

        var timer = null;
        function dismiss() { if (timer) clearTimeout(timer); el.remove(); }
        function arm() { if (timeout > 0) timer = setTimeout(dismiss, timeout); }
        close.addEventListener("click", function (e) { e.stopPropagation(); dismiss(); });
        el.addEventListener("click", dismiss);
        // Hovering is how someone reads a long receipt — don't yank it away
        // mid-sentence.
        el.addEventListener("mouseenter", function () { if (timer) { clearTimeout(timer); timer = null; } });
        el.addEventListener("mouseleave", arm);

        var host = ensureToastContainer();
        host.appendChild(el);
        while (host.children.length > TOAST_MAX) host.removeChild(host.firstChild);
        arm();
        return el;
    }

    // Theme toggle (Light / Dark / System) — the segmented control in the user
    // menu (_app_rail.html). The pre-paint resolver in _theme_resolve.html owns
    // theme application + the single OS listener and exposes window.__agnesTheme;
    // this just drives it from clicks/keys and reflects the active choice. Clicks
    // stop propagation so the user menu (wired above) stays open while switching.
    function wireThemeToggle() {
        var group = document.getElementById("themeToggle");
        var theme = window.__agnesTheme;
        if (!group || !theme) return;
        var btns = Array.prototype.slice.call(
            group.querySelectorAll("[data-theme-choice]")
        );
        if (!btns.length) return;

        function reflect(choice) {
            btns.forEach(function (b) {
                var on = b.getAttribute("data-theme-choice") === choice;
                b.setAttribute("aria-checked", on ? "true" : "false");
                b.setAttribute("tabindex", on ? "0" : "-1");
                b.classList.toggle("is-active", on);
            });
        }
        function choose(choice, moveFocus) {
            theme.apply(choice, true);
            reflect(choice);
            if (moveFocus) {
                var sel = group.querySelector('[data-theme-choice="' + choice + '"]');
                if (sel) sel.focus();
            }
        }
        group.addEventListener("click", function (e) {
            var btn = e.target.closest && e.target.closest("[data-theme-choice]");
            if (!btn) return;
            e.stopPropagation();   // keep the user menu open while switching
            choose(btn.getAttribute("data-theme-choice"), false);
        });
        // Roving radiogroup: arrows move + activate the selection.
        group.addEventListener("keydown", function (e) {
            var i = btns.indexOf(document.activeElement);
            if (i === -1) return;
            var to = -1;
            if (e.key === "ArrowRight" || e.key === "ArrowDown") to = (i + 1) % btns.length;
            else if (e.key === "ArrowLeft" || e.key === "ArrowUp") to = (i - 1 + btns.length) % btns.length;
            if (to === -1) return;
            e.preventDefault();
            e.stopPropagation();
            choose(btns[to].getAttribute("data-theme-choice"), true);
        });
        reflect(theme.current());
    }

    window.appUI = { wireDropdown: wireDropdown };
    window.appToast = appToast;

    // Auto-wire the dropdowns + theme toggle shipped by the nav chrome
    // (_app_rail.html). adminMenuTrigger/navMoreTrigger were the topnav's
    // mega-menu + overflow menu (retired, Wave 0 2026-08) — the rail has no
    // matching elements, so those two calls are permanently no-ops now;
    // left in place since wireDropdown() already guards a missing trigger.
    function init() {
        wireDropdown("userMenuTrigger", "userMenuPanel");
        wireDropdown("adminMenuTrigger", "adminMenuPanel");
        wireDropdown("navMoreTrigger", "navMorePanel");
        initPriorityNav();
        wireThemeToggle();
        // Drain any cross-navigation flash toast written to sessionStorage
        // before a redirect (e.g. store upload success). Written via
        // sessionStorage.setItem('agnes.flash.toast', JSON.stringify({kind,msg})).
        try {
            var flash = sessionStorage.getItem("agnes.flash.toast");
            if (flash) {
                sessionStorage.removeItem("agnes.flash.toast");
                appToast(JSON.parse(flash));
            }
        } catch (_) {}
    }
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
