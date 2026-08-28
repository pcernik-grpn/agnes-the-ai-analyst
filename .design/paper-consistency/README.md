# Agnes UI consistency — the admin surface still reads as the old product

**Status:** spec, not yet started · **Branch:** `design/paper-consistency` · **Written:** 2026-08-28

Every page in agnes already resolves to `base_ds.html` and the design system is healthy
(`--ds-*` tokens, `ds.*` macros, 37 contract tests, a current binding standard at
`.claude/skills/agnes-conventions/references/design-system.md`).

The inconsistency users see is **not** token drift. It is a layout split: two page shells
that look like two different products.

## What actually differs

Established by looking at rendered pages, then traced to the templates.

| | New — Library, Agents, agent builder | Old — Admin (Marketplaces, Memory Review, Data) |
|---|---|---|
| Layout | rail + full-width content | rail **+ a second sidebar** (`_admin_nav.html`), three columns |
| Page header | large title free on the page, muted description below | small-caps eyebrow ("AGENT EXPERIENCE") + title **inside a bordered gradient panel** (`_page_hero.html`) |
| Density | generous whitespace, soft-bordered cards, floating pill toolbar | dense tables, uppercase column heads, stat blocks, tab rows, amber banners, multi-colour chips |

**The tell is mechanical:** `base_admin.html` and `base_admin_page.html` both
`include "_admin_nav.html"`. `base_index.html` includes nothing. That single include is
what makes a page read as the old product.

So the split is not per-page CSS drift — it is three base templates with different
intentions, all sitting under `base_ds.html`.

## The inventory

**Old shell — 37 pages, all admin.** Both admin bases pull in the second sidebar; because
`base_admin_page.html` extends `base_page.html`, those 19 get the eyebrow hero *as well as*
the sidebar.

| Page | Lines | Base |
|---|---|---|
| `admin_tables` | 9584 | `base_admin.html` |
| `admin_data_sources` | 4616 | `base_admin.html` |
| `admin_corporate_memory` | 4181 | `base_admin.html` |
| `admin_access` | 2453 | `base_admin_page.html` |
| `admin_server_config` | 2162 | `base_admin.html` |
| `admin_users` | 1524 | `base_admin_page.html` |
| `admin_user_detail` | 1305 | `base_admin.html` |
| `admin_package_detail` | 1284 | `base_admin_page.html` |
| `admin_mcp_source_detail` | 1259 | `base_admin.html` |
| `admin_tokens` | 1149 | `base_admin.html` |
| `admin_marketplaces` | 1096 | `base_admin_page.html` |
| `admin_initial_workspace` | 900 | `base_admin_page.html` |
| `activity_center` | 783 | `base_admin_page.html` |
| `admin_store_submission_detail` | 695 | `base_admin.html` |
| `admin_usage` | 677 | `base_admin_page.html` |
| `admin_hub` | 611 | `base_admin_page.html` |
| `admin_datasource_credentials` | 584 | `base_admin.html` |
| `admin_mcp_sources` | 562 | `base_admin.html` |
| `admin_sessions` | 487 | `base_admin_page.html` |
| `admin_semantic_layer` | 449 | `base_admin_page.html` |
| `admin_knowledge_digests` | 441 | `base_admin.html` |
| `admin_linked_apps` | 435 | `base_admin_page.html` |
| `admin_sync` | 389 | `base_admin.html` |
| `admin_store_submissions` | 378 | `base_admin_page.html` |
| `admin_prompts` | 364 | `base_admin_page.html` |
| `news_editor` | 336 | `base_admin.html` |
| `admin_mcp_tool_grants` | 333 | `base_admin.html` |
| `admin_data_packages` | 316 | `base_admin_page.html` |
| `admin_adoption` | 260 | `base_admin_page.html` |
| `admin_session_detail` | 231 | `base_admin_page.html` |
| `contribute_skill` | 226 | `base_admin.html` |
| `admin_adoption_user` | 223 | `base_admin_page.html` |
| `admin_database` | 193 | `base_admin.html` |
| `admin_moderation_hub` | 96 | `base_admin_page.html` |
| `admin_store_lint` | 87 | `base_admin.html` |
| `admin_chat` | 83 | `base_admin_page.html` |
| `admin_studio_suggestions` | 62 | `base_admin.html` |

**New shell — 7 pages on `base_index.html`:** `library` (4331), `skills` (2097),
`agents` (1369), `profile` (744), `me_activity` (621), `chats` (416), `me_connections` (248).

## The decision this needs first

Not a styling task. Someone has to choose:

**A — Restyle the admin chrome.** Bring `_admin_nav.html` and `_page_hero.html` up to the
paper treatment; keep the second sidebar. Cheapest, keeps admin's IA (37 pages across
MANAGE / MAINTAIN with a nested Content group). Two templates change, 37 pages benefit.
Admin still looks structurally unlike the rest of the product.

**B — Fold admin nav into the rail.** Matches the new pages exactly, one navigation model
for the whole app. Much larger: 37 pages of IA have to go somewhere the rail can hold, and
the rail is currently 6 items.

**Recommendation: A first.** It is two templates, it is reversible, and it removes most of
the visible difference for a fraction of B's cost. B is worth its own brief if the
three-column admin layout is judged wrong in principle rather than just dated.

Whichever is chosen, the page-level CSS in the four oversized templates
(`admin_tables` 9584, `admin_data_sources` 4616, `admin_corporate_memory` 4181,
`admin_server_config` 2162) is a separate, later problem. Do not start there.

## What this is not

- Not a new design standard. The binding one exists and is current:
  `.claude/skills/agnes-conventions/references/design-system.md`. Do not write a fourth doc.
- Not the `.design/design-system-unification/` backlog. That folder's status doc was last
  reviewed 2026-05-26 and is now marked superseded; several items in its "Remaining" list
  are long closed.
- Not a token cleanup. Tokens, macros and contract tests are in good shape.

## How to verify

There is no preview build and no screenshots anywhere in this repo — the same gap that
left ~26 design-review tickets unverifiable. Any work here needs a running instance and
before/after captures of at least: `/admin/data-sources`, `/admin/memory`,
`/admin/marketplaces`, against `/library` and `/agents` as the reference look.
