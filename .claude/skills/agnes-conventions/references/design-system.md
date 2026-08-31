# Reference: Agnes design system (tokens, themes, layouts)

The binding visual standard for ANY agent touching web UI. The look is
token-driven and theme-switched — you never hardcode a palette, and you
never change the default chrome for existing instances.

## Architecture in one paragraph

Every page extends `base_ds.html` (or `base_page.html` on top of it).
The base stamps two attributes on `<html>`: `data-theme` (palette —
`paper` default since Wave 0 (2026-08) | `blue` | `navy` | `dark` |
`auto`) and `data-ui-layout` (always `"rail"` — a hard-wired literal;
the topnav chrome (`_app_header.html`) was deleted in the same wave, so
there is only one chrome left). All colors, type, radii, shadows, and
motion come from `--ds-*` custom properties declared in
`app/web/static/css/design-tokens.css`; each theme is a
`:root[data-theme="…"]` override block there. Operators still pick the
theme via `instance.theme` (env: `AGNES_INSTANCE_THEME`) — an explicit
choice always wins, so `blue` (or any other non-`paper` value) still
renders correctly for an instance that sets it. `instance.ui_layout` /
`AGNES_UI_LAYOUT` are tolerated but inert (ignored with a one-time
startup warning) — there is no second chrome left to opt into.

## The paper theme (issue #896 prototype)

`paper` is the theme half of the prototype look (issue #896), and has
been the default since Wave 0 (2026-08): warm paper canvas (`--ds-bg`),
white panels, ONE emerald accent (`--ds-primary`), Inter-first type
with tight negative headline tracking, pill CTAs, hairline slate
borders, calm shadows. The rail-navigation half of the prototype look
is no longer a separate opt-in — the rail is the only chrome, under
every theme. Shape/typography rules that aren't expressible as color
tokens live in `app/web/static/css/paper-skin.css` — every selector
there is scoped to `[data-theme="paper"]`, so an instance that
explicitly sets `blue` still renders the pre-redesign shapes; the rail
chrome CSS is `app/web/static/css/rail.css`, scoped to
`html[data-ui-layout="rail"]` — an attribute that is now a hard-wired
literal rather than a live switch, so that sheet is effectively always
active.

## Non-negotiable rules for agents

1. **Tokens only.** No raw hex in templates (contract-tested), and in
   CSS reach for an existing `--ds-*` token before inventing a value.
   Legacy `var(--primary)` is banned in new code — use
   `var(--ds-primary)`.
2. **Never break an explicitly-configured theme.** `paper` (rail
   chrome) is what every instance renders by default since Wave 0
   (2026-08); an operator who sets `instance.theme: blue` still gets a
   fully correct blue render. A NEW theme value ships as its own
   opt-in scoped block (`[data-theme="<name>"] …`), never by mutating
   an existing theme's block. There is only one chrome (`rail`) —
   `topnav` was retired in the same wave, so there is no second chrome
   to preserve. `tests/test_ui_layout_theme.py::TestPaperThemeAssets` +
   `TestResourceColourTokens` guard the token/skin contract that
   replaced the old "default page keeps `.app-header`" chrome-parity
   guard.
3. **Scoped skin sheets.** Anything paper-specific goes in
   `paper-skin.css` under a `[data-theme="paper"]` selector; anything
   rail-specific in `rail.css` under `html[data-ui-layout="rail"]`.
   Both sheets are loaded globally on every page and are ACTIVE by
   default (paper + rail is what every instance renders unless
   configured otherwise) — they MUST stay fully inert for an instance
   that explicitly sets `instance.theme: blue` (scoping is
   contract-tested).
4. **One accent vocabulary per meaning:**
   - `--ds-primary` family — the ONE brand action color (primary CTA,
     active nav, selected states). Never for category labels.
   - `--ds-kind-{data,plugin,memory,library,recipe}` + `-soft` — the
     categorical "sticker" palette for entity-kind tags. Never the
     brand primary, so categories can't be mistaken for actions.
   - `--ds-kai` / `--ds-kai-dark` / `--ds-kai-soft` / `--ds-kai-line` —
     the assistant **Kai's** identity accent (sky blue). The ONE accent
     for "Kai said / suggests this / is showing you around" surfaces:
     the guided tour, "Ask Kai" affordances, the "Ask Kai in Agnes"
     card, assistant voice cards, the "Kai is using…" pill. Deliberately
     distinct from `--ds-primary` (green brand) so Kai reads as its own
     voice inside the Agnes platform. Never reused for structural UI.
   - `--ds-agnes` / `--ds-agnes-soft` / `--ds-agnes-line` — the green
     platform/brand accent kept for the user's own chat surfaces (e.g.
     the user's message bubble) and legacy assistant callouts not yet
     migrated to `--ds-kai`. Never reused for structural UI.
   - Status: `--ds-accent-{info,warn,success,danger}-{bg,ink,line}`.
5. **Shape contrast is meaningful.** Every labelled button — primary,
   secondary, ghost, toolbar CTA, detail-header CTA — wears
   `--ds-radius-btn` (9px), the same radius as the toolbar controls and
   inputs it stands beside. Pill radius (`--ds-radius-pill`) is the
   BADGE language: category tags, status chips, counters, avatars, dots,
   and circular icon-only buttons. Never on a labelled button, never on
   an input. Dense per-row actions keep tight ~8px corners. One carve-out:
   the connect-banner CTA (`.cbn-cta`) is fully round under every theme —
   the pill is what marks the product-model banner as marketing surface
   rather than page chrome. Don't extend it further. (It was two CTAs until
   the Knowledge Layer hero and its `.klb-cta` were retired; the carve-out
   did not grow to replace it.)

   **Corollary — "you can change this" is a FORM signal, not a hue.** Where a
   control and a read-out sit in the same strip, the container separates them:
   *filled* = the action, *outlined* = a state you can change, *bare text* = a
   state you cannot. Add a chevron to anything that opens a dialog or menu, so
   the cue survives colour-blindness and greyscale (WCAG 1.4.1), and give it a
   ≥24px box (WCAG 2.2 SC 2.5.8) — an adjacent control of similar size denies it
   the spacing exemption. Do NOT reach for `--ds-primary` at rest to mean
   "interactive" on a surface that already spends primary on hover/focus: on a
   card whose `:hover` warms the title and whose `:focus-within` draws a primary
   ring, primary ink reads as "the pointer is here", not as "this is a control",
   and the local hover then has to out-shout the ambient one. Answer hover with a
   *background* change instead — a different channel from a card's elevation
   hover, so the two can't be confused. Worked example: `.fbar-card__access` in
   `filter_toolbar.css` (grid card) beside `.lib-vis` in `library.html` (table
   badge) — same anatomy and same words, tinted in the table where the chip owns
   a column, outlined on the card where it shares a bar with the primary action.
6. **Heroes:** content pages use the canonical `.page-header--hero` /
   `.stack-hero` (light card under paper, dark gradient elsewhere).
   The `--ds-hero-*` family stays DARK under paper (the one "night"
   moment: /home install hero, terminal mockups). Don't hand-build
   heroes; don't assume ink color — use tokens.
6a. **A page head is `.page-title` over `.page-lede` — one size each,
   every page.** Both are defined once in `style-custom.css`: the `<h1>`
   at 22px, the sentence under it at 13px `--ds-text-muted` capped at
   68ch. A plain-variant header gets both for free through
   `page_hero_title` / `page_hero_subtitle`; a hand-built head writes
   `<h1 class="page-title">` and `<p class="page-lede">` — that exact
   lede class, not the bare `.lede` /home's marketing hero still uses.
   Never set a head's `font-size`/`color` in a page's own `<style>` —
   that is exactly how the lede split three ways before. A head's own
   LAYOUT (flex row for a badge, `overflow-wrap` for a long name) does
   stay page-local; only the type scale is shared.

6b. **Resource detail pages are ONE template, never a family of
   lookalikes.** Every entity with a detail page — data package, plugin,
   skill, agent, file, collection, upload, memory domain, recipe, table —
   renders through `templates/macros/_detail.html` + `css/detail-page.css`.
   The reference implementation is `catalog_package_detail.html`; read it
   before building a new one. The contract:

   - **Header:** `detail.hero(...)` and nothing hand-written. It carries
     the title, the `type_label` badge, the trust marker, and exactly ONE
     prominent action; everything else goes in `detail.menu(...)`. A
     client-hydrated page uses `name_id` / `icon_id` / `body_slot` for its
     JS hooks — the marketplace pages hand-wrote their header for exactly
     four hooks and lost the badge, the rail and the menu doing it.
   - **Container language:** `detail--panels` is the shared DEFAULT
     (`hero(panels=False)` to escape it, and only for a surface designed
     against the borderless rhythm). One rule: *a section is a panel;
     everything inside a panel is whitespace.* No cards inside panels.
   - **Main column:** the subject — what it is / does, then the objects it
     contains (`detail.objects(...)` for any "what is inside this" list),
     then examples (`detail.questions(...)`).
   - **Rail:** the facts, always in this order, each skipped when empty —
     About prose → the metadata read-out (`side_rows`, **unlabelled**: the
     rows name their own values) → Owner/Publisher → Sharing →
     Availability → Versions. Put an entity-specific block where it belongs
     in that order; don't invent a new position for it.
   - **Owner/admin tools are not a block in the flow.** The action ladder is
     `store_menu()` in the header's overflow menu, history is
     `version_timeline()` in the rail, and only an alert about the page
     (the quarantine banner) sits above the content.
   - **Shared concepts use the shared component**, always:
     `visibility_chip` / `side_sharing`, `version_timeline`, `store_menu`,
     `owner`, `status`, `side_panel_*`, `related`, `objects`, `empty`. If
     you are about to write page-local CSS for one of these, the component
     is missing a parameter — add it there instead.
   - **Gating.** The whole redesign is opt-in. `cols_open` / `aside_open` /
     `cols_close` emit nothing on the legacy path, but any content that
     MOVED (the rail's blocks, a re-worded or relocated main section) must
     sit behind `{% if detail.redesign %}` with the legacy markup kept
     verbatim in the `{% else %}`. Guarded by
     `tests/test_ui_layout_theme.py::TestDetailPageTemplateIsShared`.

7. **Motion:** use `--ds-motion-{fast,med,slow}` +
   `--ds-ease-{standard,enter}`; honor `prefers-reduced-motion` on
   anything that moves.
8. **The rail is the only chrome.** `_app_header.html` (topnav) was
   deleted in Wave 0 (2026-08); every page renders `_app_rail.html`
   unconditionally, so grant gating (`can_chat`), the admin entry, and
   the JS id contract (`#global-search` + `#globalSearchResults`,
   `#userMenu`, `#themeToggle`) live in that one file — no second
   chrome left to mirror them into
   (`tests/test_ui_layout_theme.py::TestRailOptIn` asserts the rail
   side directly). There are **no `data-tour` anchors** anywhere in the
   templates: the guided tour was retired with the topnav, and
   `js/tour.js` keeps `[data-tour=…]` only as a dead fallback in one
   selector — do not add new ones expecting anything to read them.
   The rail's IA is **two fixed zones with the
   conversation list between them** — top: global search, then New chat
   and Chats (a destination row of its own; the old `View all chats`
   link at the foot of the list is retired, because a way OUT cannot
   live inside the one region collapse hides), with the conversations
   under them scrolling inside `.rail-history-body`; bottom: Library ·
   Agents, then Admin behind the nav's only divider, then the
   onboarding row (`Set up Agnes` → `Continue setup`, a circular
   progress ring, gone at 5/5), then the profile. Neither zone may move
   when the list grows. **Every row carries an icon** — the rail
   collapses to a glyph strip, so a text-only row is one that
   disappears.
   Admin is ONE destination (`/admin`), not a menu: the hand-written
   flyout was retired as a second, drifting copy of the admin
   inventory. That inventory now lives once in `app/web/admin_nav.py`,
   rendered by `_admin_nav.html` as the admin sidebar on every
   `/admin/*` page, and guarded by `tests/test_web_admin_nav.py`. Add an
   admin page there — never by growing the rail.
   Every row shares one height (`--rail-row-h`) and the active
   destination is the ONLY tinted row — never add a standing CTA tint.
   The Studio dropdown, the Marketplace entry and the `.rail-sub-i`
   subcategory tree under Catalog are all retired, and My Stack is
   demoted out of the rail (#1088) — `/stack` stays live, reached from
   the Library header, the chat hero counts and the `/catalog` lede. A
   new content surface reaches the caller through an existing
   destination (the Library's "+ New" menu, chat suggestions, search),
   not by growing the rail.
9. **Verify visually.** After any UI change, run the app with both
   configs and screenshot: the default (nothing set — now `paper` on
   the rail chrome) and an explicit `AGNES_INSTANCE_THEME=blue`
   override. A page that only looks right in one mode is not done.
   (Chrome context: routes must spread `_chrome_ctx(request, user)` or
   the page renders bare.)

## Where things live

| Concern | File |
|---|---|
| Token palettes (all themes) | `app/web/static/css/design-tokens.css` |
| Paper shape/type skin | `app/web/static/css/paper-skin.css` |
| Rail chrome CSS | `app/web/static/css/rail.css` |
| Rail chrome markup (the only chrome) | `app/web/templates/_app_rail.html` |
| Theme/layout resolvers | `app/instance_config.py` (`get_instance_theme`, `get_ui_layout` — the latter always returns `"rail"`) |
| Config surface | `app/api/config_surface.py`; docs `docs/CONFIGURATION.md` |
| Guards | `tests/test_design_system_contract.py`, `tests/test_ui_layout_theme.py` |

## Adding a new theme

1. Add the value to the whitelist in `get_instance_theme()`.
2. Add `:root[data-theme="<name>"]` in `design-tokens.css` overriding
   BOTH families: the `--ds-*` set AND the legacy compat shims
   (`--primary`, `--background`, `--surface`, `--text-*` …) — follow
   the `paper`/`dark` blocks as the template.
3. If the theme needs shape/type changes, add a scoped skin sheet and
   load it from BOTH bases (`base_ds.html`, `base.html`).
4. Extend `tests/test_ui_layout_theme.py` with the new value.
5. Document in `docs/CONFIGURATION.md` + `config/instance.yaml.example`.
