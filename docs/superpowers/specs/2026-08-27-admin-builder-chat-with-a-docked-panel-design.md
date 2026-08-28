# Admin Builder — a conversation with a docked panel

**Status:** proposed
**Date:** 2026-08-27
**Scope:** setting an instance up by talking to it — chat in the middle, the
relevant admin surface docked on the right — for the three admin jobs that have
no conversational path today: connecting a source, registering tables, and
granting access.

## Two questions this spec does not answer

Both are decisions for the owner, and the rest of the design changes shape
depending on them. They are stated first so a reader does not have to infer
which parts are settled.

1. **What goes in the pane: the real admin pages, or components?** Framing
   `/admin/data-sources?add=bigquery` verbatim is far less work and stays
   correct as those pages evolve, but needs a per-route relaxation of the
   app-wide `X-Frame-Options: DENY`. Mounting each form as a panel component
   needs no header change and no security review, and costs a refactor per
   form. See [The pane](#the-pane).
2. **Does the conversation ever apply a write, or only propose?** The entity
   builder already answers this for packages and grants — propose, never apply
   (`2026-08-26-conversational-entity-builder-design.md`). This spec argues the
   same rule should hold for connections and grants, and that "apply" is only
   ever the human clicking Save in the pane. Confirm or overrule.

## Why

An admin setting up a fresh instance hits a chain of dependencies, and today
every link in it costs them their place. The concrete case that prompted this:
open the Library, click **+ New → data package**, and the table picker says

> No table matches that.

There are no tables. The picker is reporting an unsatisfiable precondition as a
search miss, and the only way forward is to leave the builder, find
`/admin/data-sources`, connect a source, register tables, navigate back, and
start the package over — with the half-formed intent ("a package with the
opportunity tables for sales") gone.

The expensive part is not the number of clicks. It is that **the intent does not
survive the detour**. That is what this design is for, and it is worth stating
plainly because it also bounds the work: the goal is continuity across a
dependency boundary, not conversational admin for its own sake.

## What already exists

Three quarters of the mechanism ships today. This is the main argument for the
design being tractable.

**The dock is real.** `.cloud-chat-shell.has-preview-pane` adds a third grid
track (`css/chat.css`) and `js/chat.js` builds an `<aside
class="cloud-chat-preview-pane">` with a header, a close button and an iframe.
Data-app previews use it.

**The conversation already drives it.** Four chat-surface MCP tools
(`agnes_data_app_preview` / `_refresh` / `_close` / `_credentials`, defined in
`app/api/mcp/foundation_tools.py`) return a **render directive** on a fixed JSON
contract, which `chat.js` routes to the pane instead of rendering as a generic
tool block:

```
{render:"data_app_preview", slug, url: null|"/apps/<slug>/"}
```

`url:null` opens the pane immediately with a placeholder so the reader sees
something happening before the container is up; a follow-up call swaps in the
live frame. An admin equivalent is the same shape with a different directive.

**The agent can already act.** The chat sandbox is spawned with the `agnes` CLI
pointed at the server and a session JWT minted for the calling user
(`app/chat/manager.py::_spawn_runner`), and `cli/commands/admin_connection.py`
and `admin_data_package.py` exist. An admin talking to Agnes can already reach
the same admin API their browser can.

**The two-pane builder shell ships.** `js/components/builder_shell.js` +
`css/builder.css` render a conversation beside a configuration panel, now used
by `/agents`, `/skills` and the package builder.

## What is genuinely new

**A conversation that holds a stack of tasks.** This is the only real
architectural addition, and the part most worth getting right on paper.

The data-app pane is driven within one topic: the conversation is about the app,
and the pane shows the app. The builder shell is one entity per page. Neither
models "I was building a package, I am now connecting a source, and afterwards I
am back on the package."

So the session needs a **task stack**, and the two things that make it hard are
both about honesty rather than data structure:

- The pane's subject and the thread's subject must never disagree. If the pane
  shows the connection form while the model believes it is still discussing the
  package, every subsequent turn is reasoning about the wrong object. The
  directive must therefore *set* the conversation's current task, not merely
  open a frame beside it.
- Returning must be explicit and visible. "You connected `sales-bq`. Back to the
  package — which of its 40 tables do you want?" is the whole point; silently
  resuming loses the thread for the reader even when the model kept it.

Proposed shape: the directive carries `task` (`connect_source`,
`register_tables`, `grant_access`) plus `resume` — the task to return to when
this one completes, or `null` at the root. The server holds the stack on the
session; the pane renders the top of it; a `..._done` directive pops it. The
stack is the model's *scaffolding*, not its memory: the transcript remains the
record, and a lost stack degrades to "ask what they were doing", never to a
wrong subject.

## The pane

The fork from the top of this document, with what each option actually costs.

### Option A — frame the real admin pages

`/admin/data-sources?add=bigquery` already works: `openWizard(connector)`
pre-selects its source from the argument, and the URL bootstrap now honours
`?add=<type>` (shipped with the landing-page work). Framing it means the pane
shows the real wizard, and it keeps working as that wizard changes.

The cost is a security decision. `app/middleware/security_headers.py` sets
`frame-ancestors 'none'` + `X-Frame-Options: DENY` app-wide. There is precedent
for a narrow exception — `app/api/collections.py` serves the PDF modal with
`SAMEORIGIN` + `frame-ancestors 'self'` — so the mechanism is established. But
admin forms are the highest-value clickjacking target in the product, which is
presumably why the default is `DENY`, and an exception for them deserves a real
review rather than a wave-through. If taken: scope it to the specific admin
routes the pane can open, never the `/admin` prefix.

### Option B — mount the forms as panel components

No header change, no review. Each form becomes something the pane can render
directly, the way the builder shell renders its configuration sections. Cost is
a refactor per form, and a standing obligation to keep the panel version and
the page version from drifting — the failure mode the entity-builder spec
already worried about when it insisted the shell be shared rather than copied.

**Recommendation: A for the first version, if the header exception is reviewed
and scoped.** It gets a working end-to-end loop without refactoring anything,
which is what tells us whether admins actually reach for this. B is the better
end state and can replace A form by form, because the directive contract does
not care what renders the pane.

## What the conversation may and may not do

Three rules, each with a reason that has already bitten something in this
codebase.

**Exact values from another system stay in the form.** The conversational-entity
-builder spec's criterion applies unchanged: make a builder conversational when
the input is *intent* the person could describe, not when it is a BigQuery
dataset path or a warehouse HTTP path, where "prose adds a transcription step
and a chance for the model to invent a plausible wrong string". This is why the
pane exists at all — the conversation handles which tables and which team, the
form handles the dataset path.

**A credential never travels through a turn.** Not in the message, not in the
patch, and not in a directive. The precedent is explicit: the data-app preview
tool mints its cookie server-side and deliberately does *not* return the token,
because "a tool result is archived in the session transcript, and this is a live
bearer credential". A connection's token goes into the form in the pane, over
the normal admin path, and the conversation never sees it.

**Grants propose, never apply.** Same rule the package builder already follows.
A turn may fill the access matrix in the pane; only the human's Save writes it.
An admin should see the access they are about to create before it exists.

## Phases

1. **The directive + the task stack**, with one task: `connect_source`. Proves
   the loop end to end — ask, pane opens on the right wizard, finish, return.
2. **`register_tables`**, which is the step that actually unblocks the package
   builder and the one the current dead end runs into.
3. **The dependency handoff.** The package builder detects its unmet
   precondition, offers the upstream task, and the draft survives the round
   trip. (This is the tier-one fix from the same investigation; it is worth
   doing before this spec lands, and it becomes the entry point once this does.)
4. **`grant_access`**, propose-only, with the access matrix rendered in the pane.
5. **Option B migration**, form by form, if the framed version proves the value.

## Open questions

- Where does the admin Builder live? A mode of `/chat`, or its own page that
  embeds the chat component? `/chat` matches "chat stays in the middle" but
  makes the landing page carry an admin mode; a page keeps them separate at the
  cost of a second conversation surface.
- What happens to the stack when the session ends mid-task? Resuming a
  half-finished connection later is a nice property, but a stale stack that
  outlives the reader's intent is worse than none.
- Does a non-admin ever see this? Everything here is admin-gated, so the
  honest answer is probably no — but the same task-stack machinery would serve
  an analyst hitting a *grant* they lack, where the resolution is asking someone
  rather than doing it.
