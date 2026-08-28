> [!IMPORTANT]
> **Last reviewed 2026-05-26 — this document is stale and its "Remaining" list is
> no longer accurate.** Since it was written, the `paper` theme shipped and became
> the default (Wave 0, 2026-08), the topnav chrome was retired, and the base-template
> tree was unified: every page now resolves to `base_ds.html` except four catalog
> pages and the login pages.
>
> Verify against the code before treating any item below as open. Several were closed
> long ago; a design-review sweep in Aug 2026 re-filed items from this list as new
> defects because nobody had updated it.
>
> Current standard: [`.claude/skills/agnes-conventions/references/design-system.md`](../../.claude/skills/agnes-conventions/references/design-system.md).

# Agnes UI — Design System Status & Handoff

Sources: `.design/design-system-unification/` + `.interface-design/system.md`
Last reviewed: 2026-05-26 (code-only, no screenshots)

---

## Summary

40 templates converted to shared macros (`_components.html`). ~200 button instances converted, ~150 lines of duplicate CSS removed. All test runs passed green.

The goal is a unified `--ds-*` component vocabulary across all page families — one way to do each thing. The work is mostly class renames + CSS consolidation, not a redesign.

---

## Styles (design tokens + palette)

### Done ✅
- Unified `--ds-*` palette: green primary (`#2ea877`), navy hero (`#0f1b3a`), white card surfaces
- Compat shim: legacy `--primary` (blue) aliased to `--ds-primary` (green) — all `var(--primary)` consumers render green without markup changes
- Accent vocabulary: `--ds-accent-{info,success,warn,danger}-{bg,ink,line}` — one token feeds flashes, badges, cards, callouts
- Focus outline: `--ds-focus-outline` (2px solid green, 2px offset) — every interactive element
- Spacing, radius, type, shadow, motion scales defined

### Remaining ❌
- **~122 raw hex values** remain in converted templates (profile, setup, me_activity, home_not_onboarded) — most are chip colors with `--ds-accent-*` equivalents; sweep not yet done
- **Legacy `var(--primary)` references** still in corporate_memory, me_activity, memory_domain_detail, dashboard — shim works, but explicit `--ds-*` references are self-documenting
- **Dark mode**: `[data-theme="dark"]` scaffold exists in `design-tokens.css` but no overrides defined — future brief

---

## CTA (buttons)

### Done ✅
- 4 semantic variants: `btn-primary` (green fill), `btn-secondary` (border), `btn-ghost` (no border), `btn-danger` (red)
- Macro `ds.button(variant, size, icon_only, type, href, ...)` — single entry point for all 40 converted templates
- `.btn-sm` size modifier + `.btn--icon` square icon-only modifier
- Hover: 120ms transition, calm — no scale or translate
- Rejected variants removed and documented: `.btn-warning`, `.btn-lg`, `.btn-copy*`, `.btn-sm-primary/secondary`, `.btn-primary/secondary-v2`, `.btn-ghost-v2`

### Remaining ❌
- **`.btn-required`** (amber disabled state) — used by 2 templates (catalog_package_detail, memory_domain_detail) with no canonical definition; needs to be promoted to `style-custom.css` or absorbed as `.btn-secondary[disabled][data-required="true"]`
- **`.btn-warning`** — one instance in `admin_marketplaces.html` (~line 386) for a system-confirm modal; either add as 5th canonical variant or replace with `.btn-danger`
- **4 large templates deferred** to dedicated PRs:
  - `admin_corporate_memory.html` (3951 lines) — custom `.btn-mandate/.btn-approve/.btn-reject/.btn-revoke` family
  - `admin_tables.html` (5748 lines) — 66 buttons across bespoke modals
  - `dashboard.html` (1539 lines) — `.btn-setup`, `.notif-link`, `.btn-copy-term`, `.btn-register`, terminal chrome
  - `admin_server_config.html` (1462 lines) — 0 canonical `.btn-*` buttons, entirely bespoke `.cfg-btn` chrome
- **JS-generated buttons** inside `<script>` template literals — Jinja macros can't reach them: `admin_access.html`, `install.html` — remain page-local

---

## Modals

### Done ✅
- Cancel + Confirm pattern consistent across converted templates: `ds.button(variant='secondary')` Cancel + variant-typed confirm
- Converted in: admin_groups, admin_users, admin_marketplaces, admin_welcome, admin_workspace_prompt, catalog, corporate_memory, marketplace_guide
- `data-close-modal` passed via `attrs=` escape hatch
- `--shadow-lg` reserved for modal surfaces (heaviest depth level)
- Disabled anchors now emit `aria-disabled="true" tabindex="-1"` (previously stayed keyboard-focusable)

### Remaining ❌
- **`admin_tables.html`** — 66 buttons mostly in bespoke inline-edit panels and modals; not converted
- **`admin_corporate_memory.html`** — custom approve/reject/revoke modals; not converted
- **`.btn-warning` in system-confirm modal** (`admin_marketplaces.html`) — still outside the canonical vocabulary

---

## Confirmation dialogs

### Done ✅
- Confirm-delete pattern unified: `ds.button(variant='danger')` consistently for Delete / Hard delete / Revoke / Remove from stack
- Keyboard navigation preserved — modals reachable via Tab
- Escape key to close modal preserved (JS handler in `base.html`, not yet migrated but functional)

### Remaining ❌
- **`base_ds.html` adoption = zero** — opt-in layout built but no page has migrated; blocked by extraction of inline JS (undo toast, modal-Esc, cmd palette, admin shortcuts) into `_app_scripts.html` partial — tracked in `base_ds.html` doc-comment
- **`admin_tables.html`** — bespoke confirmation dialogs for inline editing; not converted

---

## Dropdowns

### Done ✅
- No canonical dropdown component was in scope for this brief (scope: buttons + panels + tables + tabs + code surfaces)
- Existing dropdowns remain page-local

### Remaining ❌
- **No `ds.dropdown` macro exists** — every page has its own implementation
- Candidates for unification: filter dropdowns (marketplace, catalog), action dropdowns (admin pages), select elements in forms
- **Recommendation**: create a dropdown brief as a follow-up after the admin template sweep; define canonical class, keyboard navigation (arrow keys), focus trap, close-on-outside-click, ARIA (`role="listbox"` or `role="menu"` depending on use case)

---

## Tables

### Done ✅
- `.ds-table` canonical class + 15 legacy aliases via `:is()` (data-table, obs-table, sched-table, members-table, etc.)
- `.ds-table--dense`, `.ds-table--zebra` modifiers
- Sticky header on by default

### Remaining ❌
- Page-local `.obs-table` overrides in admin_sessions, admin_usage, activity_center (sort arrows, selection state, error rows) — extract into shared `.ds-table--sortable` / `.ds-table tbody tr.is-selected` rules

---

## Cards / Panels

### Done ✅
- `.ds-card` canonical family: `__title`, `--accent`, `--info/success/warn/danger`, `[data-clickable]:hover/focus`
- `.card-error`, `.card-highlight`, `.card-ai` re-skinned onto `--ds-accent-*-line` (4px left bar, no background tints)
- `ds.panel()` macro available

### Remaining ❌
- **One-off cards** waiting for follow-up (each has complex sub-element layout): `.cc-setup-card`, `.ai-setup-card`, `.next-step-card`, `.data-link-card`, `.notifications-card`, `.memory-widget`
- **`.auth-tabs`** — waiting to be absorbed into `.tab-strip--heavy` modifier when login flow is refactored

---

## Recent fixes (2026-05-26)

- **`fix: center modal dialogs vertically on admin/tables`** — modal dialogs on the admin/tables page were not vertically centered; fixed.
- **`feat(web): extract inline CSS from news, profile, error pages + token migration`** — inline styles extracted from news, profile, and error pages into shared CSS files; tokens migrated to the `--ds-*` system.

---

## TODO

- [ ] Design tests — visually verify component consistency in browser (modals, CTAs, dropdowns, confirmation dialogs) across pages

---

## Recommended next steps

1. Resolve `.btn-required` and `.btn-warning` (2 small decisions, unblock consistency)
2. Extract `_app_scripts.html` partial → unblocks `base_ds.html` adoption
3. Hex-literal sweep across converted templates (swap raw `#fef3c7` etc. for `var(--ds-accent-warn-bg)`)
4. `admin_server_config` → dedicated PR
5. `admin_tables` → dedicated PR (largest)
6. `admin_corporate_memory` → dedicated PR (decision: promote `.btn-approve` family or keep bespoke)
7. `dashboard.html` → requires macro extensions first
8. **Dropdown brief** — new component after admin sweep is done

Playbook for each page: `.design/design-system-unification/REFACTOR_PLAYBOOK.md`
Component contract: `.interface-design/system.md`
