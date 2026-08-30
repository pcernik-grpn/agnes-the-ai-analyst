# Conversational entity builder — design

**Status:** proposed
**Date:** 2026-08-26
**Scope:** every authoring surface in the Library's **+ New** menu — `/skills`
(skill · plugin · agent template) and the data-package drawer — aligned with
the `/agents` builder shipped in the same wave.

## Why

`/agents` now builds an agent the way a person would describe one: a
conversation on the left writes a configuration on the right, the panel stays
hand-editable and is the source of truth, and a Preview tab opens a real
session with the thing being built. `/skills` — the page behind every "Build
a …" entry in the Library's **+ New** menu — is still a step-wise form with a
card preview showing how the item will *read* in the Library, not what it
*does*.

Two builders, two mental models, one product. This spec closes that.

It is deliberately a spec and not a patch: the three types have genuinely
different shapes, and "preview a skill" turns out to be a real engineering
question rather than a UI one (see [Preview](#preview-chat-with-it-applied)).

## Which surfaces this covers, and which it does not

Every place in the product where a person brings something into existence,
and whether the conversational pattern belongs there:

| Surface | What it authors | Verdict |
|---|---|---|
| `/agents` | An agent profile | **Done** — the reference implementation |
| `/skills?type=skill` | Markdown skill | **In scope** |
| `/skills?type=agent` | Agent template (markdown) | **In scope** |
| `/skills?type=plugin` | `.zip` bundle | **In scope, metadata only** |
| Data-package drawer | Package + tables + group grants | **In scope** — see below |
| "Upload a file" | A file | Metadata only, if at all |
| Data apps | Deployed app | Out — created by `POST /api/data-apps` from a git repo; there is no in-app builder to convert |
| Semantic models | Ossie document | Out — authored in a *source* (git / upload / connection) and synced; the document is owned upstream, and a model owned by a source is read-only through the API (`409 source_owned`) |
| `/me/connections` | An OAuth connection | Out — not authoring |
| Admin config forms (register a table, add an MCP source, a marketplace, a connection spec) | Configuration | Out — see the criterion |

**The criterion.** Make a builder conversational when the input is *intent* the
person can describe but would otherwise have to translate into a form — "an
agent that answers pipeline questions", "a package with our opportunity tables
for the sales team". Do **not** when the input is *exact values from another
system* — a BigQuery dataset path, an OAuth client id, a warehouse HTTP path.
There, prose adds a transcription step and a chance for the model to invent a
plausible wrong string, and the form is already the right shape. This is why
registering a table stays a form while authoring a data package does not.

### The data-package drawer

`app/web/static/js/components/package_drawer.js` (~976 lines) is the fourth
**+ New** entry, admin-only. It is already a *component* rather than a page —
it opens in place on whatever lens you are standing on — and its own header
comment records why: as a centred modal it was "the tallest form in the admin
surface (name, slug, description, lifecycle, category, icon, colour, cover
image, and a group-access matrix)".

That list is the argument for conversation. It is also a clean fit for the
panel pattern: tables and groups are both **pools**, so they get the
only-what's-attached treatment with a `+` picker each, and the drawer stops
being a wall of fields.

Two things make it different from the other three, and both are constraints,
not blockers:

- **It writes RBAC.** Creating a package fires `POST /api/admin/grants`, one
  per chosen group. A conversation that can say "share it with the sales team"
  is a conversation that can widen access, so the sanitizer bar is higher than
  for a skill: group ids validate against *the caller's own* admin-visible
  group list, and — the part worth being strict about — **grants should not be
  applied from a turn at all**. Let the conversation propose them into the
  unsaved panel and require the explicit Save, which is exactly what the
  agent builder's `apply:false` already gives us. An admin should see the
  access matrix they are about to create before it exists.
- **It is a drawer, not a page.** The two-pane shell assumes a full-bleed
  workspace. Either the drawer grows into one when opened from **+ New**
  (keeping the in-place behaviour when opened from a chip input, which is a
  different, narrower job), or package authoring gets a page of its own and
  the drawer stays the quick path. Recommend the former; decide in phase 2.

## What exists today

`app/web/templates/skills.html` (~2,100 lines) serves all three types from one
`TYPES` table. Per-type differences, quoted from its own header comment:

| Type | Wire value | Authoring input | Submit | "Check" |
|---|---|---|---|---|
| Skill | `skill` | markdown body | `POST /api/store/entities/from-markdown` | skill linter (`/entities/dryrun`) |
| Agent template | `agent` | markdown body | same endpoint, `type=agent` | — |
| Plugin | `plugin` | `.zip` bundle (multipart) | `POST /api/store/entities` | `/entities/preview` component rows |

Notes that constrain the design:

- Drafts are **per-type, localStorage only**, never the store. A plugin draft
  keeps its metadata but not its `.zip` (a `File` cannot be serialized).
- Switching type is non-destructive by construction — the outgoing draft is
  persisted to its own slot first.
- The right column is already a sticky `<aside class="sk-preview">`, so the
  two-pane *skeleton* exists; what it holds is the difference.

## The target shape

The same shell as `/agents`, which is now:

- Full-bleed `body.ag-building` breakout from the index column, two panes at
  50/50, one hairline between them.
- Left: `Create` | `Preview` tabs over a conversation.
- Right: `Configuration` — numbered collapsible sections listing **only what
  is set**, with a `+` per pooled section opening a picker modal.
- Header verbs follow the entity's life: a never-saved draft gets *Save as
  draft* and no Delete; an existing one gets *Save*, a status toggle, and
  *Delete*. Leaving asks first and discards.
- Editing is explicit: nothing reaches the server until Save.

Reuse, don't fork. The `ag-*` CSS block and the shell markup should move out
of `agents.html` into a shared partial before the second page adopts them —
copying a 400-line inline `<style>` into `skills.html` is how the two pages
drift back apart. Proposed: `app/web/templates/macros/_builder_shell.html`
plus `app/web/static/css/builder.css`, with `agents.html` migrated onto it
first, as a no-visual-change refactor with the existing guards green.

## The turn endpoint

`POST /api/agents/{agent_id}/builder/turn` (`app/api/agent_builder.py`) is the
model. It already carries the two flags the explicit-save page needs:

- `apply: bool = True` — with `false`, run the turn and return the sanitized
  patch **without** writing.
- `config: dict | None` — the caller's unsaved working copy, narrowed to
  `PATCHABLE` and used only for the prompt.

The entity builder wants the same contract at
`POST /api/store/entities/builder/turn`, differing in three places:

1. **No entity id in the path.** An entity draft lives in localStorage and has
   no server row until submit — unlike an agent, where the row is minted up
   front because the conversation addresses it by id. So the draft travels in
   the request (`type` + `draft`), and the endpoint is stateless.
2. **A per-type prompt and a per-type sanitizer.** One `TYPES`-shaped table
   server-side, mirroring the template's:
   - `skill` / `agent` → patch keys are the frontmatter fields plus `body`
     (markdown). `body` is the large one; cap it the way `MAX_MESSAGE_CHARS`
     caps the message.
   - `plugin` → the conversation must not invent a `.zip`. It patches
     **metadata only** (name, description, category, visibility); the bundle
     stays an upload. Say so in the reply when someone asks for code.
3. **The sanitizer is still the trust boundary.** Same rule as the agent
   builder: unknown keys dropped, enum values validated against the server's
   own list, ids validated against candidate lists the *caller* was offered —
   never against the model's claims. `tests/test_agent_builder_turns.py`
   `TestSanitizerIsTheTrustBoundary` is the template for the test class.

A markdown `body` is the one genuinely new sanitizer problem: it is authored
content, rendered later, and the security playbook's `innerHTML` rule applies
to whatever displays it. The builder panel must render it as **text** (the
agent builder's `.ag-msg-text` precedent) or through `renderMarkdownSafe`,
never raw.

The data package needs its own turn endpoint rather than a fourth row in this
table: it is admin-gated, its writes go to `/api/admin/data-packages` and
`/api/admin/grants`, and its candidate pools are the registered tables and the
caller's admin-visible groups. Same shape, different authority —
`POST /api/admin/data-packages/builder/turn`, gated with `Depends(require_admin)`
like everything else under that prefix, and **`apply` is not merely defaulted
off here but unsupported**: a package turn only ever proposes. See the RBAC
note above.

## Preview: "chat with it applied"

This is the expensive half, and the reason this is a spec.

**Agent template** is nearly free. It is a markdown body that becomes an
agent's behaviour, so Preview can mint a scratch agent from the draft and open
a session against it — exactly what `/agents` Preview does today
(`POST /api/chat/sessions` with `agent_slug`, then the existing WebSocket).
The only new work is the scratch agent's lifecycle: create on first preview,
reuse while the draft is open, delete when the builder closes. It must never
appear in the owner's `/agents` list.

**Skill and plugin are not free**, because of how delivery actually works.
Per `app/chat/skills_catalog.py`, a skill is only invokable if something
materialized it into the session's project scope — server-side, into the
per-user chat workspace for docker (`app/chat/workdir.py`) or into the
workspace tarball for kai-agent (`app/api/kai.py`) — and both walk the
caller's RBAC-filtered *store* set. An unsaved draft is in neither.

So "chat with it applied" needs one more materialization source: the draft
itself. The seam already exists and is the right one —
`WorkdirManager.prepare_ephemeral_session_dir(chat_id, participants,
intersection)` builds a fresh session workspace with **no** symlinks to any
personal workspace, no `CLAUDE.local.md`, and only intersection-filtered
`.claude/skills` copied in. A preview session is exactly that shape plus one
extra directory.

Proposed: `prepare_preview_session_dir(chat_id, user, draft)` alongside it,
which builds the same isolated root and writes the draft into
`.claude/skills/<name>/SKILL.md` (skill) or unpacks the uploaded bundle
(plugin). Constraints, none of them optional:

- **The name is untrusted.** It becomes a directory name, so it must be
  validated *and* realpath-contained under the session root — the security
  playbook's rule, and the exact shape of a real prior finding.
- **The bundle is untrusted.** Zip extraction needs the traversal and
  zip-bomb guards `/entities/preview` already applies; reuse that code path
  rather than writing a second extractor.
- **Ephemeral and owner-scoped.** One preview root per builder session, torn
  down on close, never reachable by another user, never mixed into the
  owner's real workspace.
- **kai-agent needs the parallel change** in the tarball path, or preview
  works on docker and silently does nothing on the other delivery. If only
  one lands, the Preview tab must say which — the `merged_skills`
  `delivery="none"` precedent (#1552: a menu entry for an undelivered skill
  is what produced "Unknown command").

That last point is the main risk. Two delivery paths, and a preview that
works on one is worse than a preview that is honestly unavailable.

## Phasing

Each phase ships on its own and leaves the product coherent.

1. **Extract the shell — CSS. ✅ done** (`app/web/static/css/builder.css`).
   ~230 lines out of `agents.html`'s inline `<style>`, no visual change,
   verified by computed style. Turned up one thing worth keeping in mind for
   every later phase: the design-system guards that ban raw hex and legacy
   `var(--primary)` scan TEMPLATES, so moving rules into a `.css` file escapes
   them silently. `tests/test_builder_css_tokens.py` now follows them, over a
   cohort of sheets that may only grow.

1b. **Extract the shell — markup.** Not done, and not what this spec first
   said. There is no Jinja partial to extract: the shell does not exist in the
   template at all. It is built at runtime by eleven functions
   (`renderBuilder`, `renderConvPane`, `renderCfgPane`, `section`,
   `createPaneHtml`, `composerHtml`, `convHtml`, `msgHtml`, `pickerHtml`,
   `headActionsHtml`, `toolbar`) inside a 1,841-line inline `<script>`. The
   reusable artifact is therefore a **JS module** —
   `app/web/static/js/components/builder_shell.js` — that renders the shell
   from a config object and calls back into the host page for the parts only
   it knows: the section list, each section's body, and what a turn does.

   This is the largest single refactor in the plan and the one that decides
   whether phase 2 is genuinely shared or a second copy. Its hard part is not
   the extraction but the seam: the eleven functions currently close over the
   page's module-scope state (`conv`, `pv`, `picker`, `baseline`, `collapsed`,
   `builderTab`), so the module needs that state passed in or owned, and the
   explicit-save machinery (`touch` / `isDirty` / `saveAgent` / `leaveBuilder`)
   has to move with it or the two pages will diverge on the one behaviour
   users notice most.
2. **`/skills` adopts the shell.** Two panes, the only-what's-set panel with
   `+` pickers, explicit save with the same header verbs and leave
   confirmation. Still form-authored — no conversation yet. Delivers the
   consistency; nothing new is needed server-side. Decide here whether the
   package drawer becomes a page or grows into the shell.
3. **The entity turn endpoint** + the Create tab, agent-template and skill
   first (both are markdown), plugin metadata-only after.
4. **Preview for agent templates** — scratch agent + existing session path.
5. **The data-package builder** — its own admin-gated turn endpoint, tables
   and groups as `+` pickers, proposals only (no `apply`).
6. **Preview for skills, then plugins** — `prepare_preview_session_dir`, both
   delivery paths, the containment guards above.

Phases 1–2 are mechanical and well-guarded. 3 is the agent builder again with
a different table. 4 is small. 5 is small in code and needs care in review —
it is a conversation adjacent to grant-writing, which is why it proposes and
never applies. **6 is the one to schedule deliberately** — it is new sandbox
surface handling untrusted names and untrusted archives, and it deserves its
own review pass against
`.claude/skills/agnes-conventions/references/security.md`.

A package's Preview, if it gets one, is not a chat: it is "what an analyst
will see" — the resolved table list and which groups reach it. Worth doing
precisely because that is the thing an admin currently has to guess at.

## Open questions

- Should an entity draft become a real server row (like an agent's
  placeholder) so the conversation and preview have something to address, or
  stay localStorage-only? A row simplifies phases 3–5 and gives drafts
  cross-device continuity; it also puts unfinished entities in the store,
  which the current design deliberately avoids.
- Is a plugin's conversation worth building at metadata-only scope, or should
  the plugin type keep the form and skip the Create tab?
- Does the agent-template scratch agent count against `token_budget_monthly`,
  and whose budget — the author's, presumably.
- Does the package drawer become a full page, or grow into the shell only
  when opened from **+ New**? It has a second caller (a chip input on
  `/admin/tables`) where in-place is the right behaviour.
- "Upload a file" is an input method, not a builder — but it does produce a
  Library item with a name, description and sharing. Is it worth a one-line
  "describe it and I will fill the metadata in", or is that a gimmick on a
  three-field form? Leaning gimmick.
