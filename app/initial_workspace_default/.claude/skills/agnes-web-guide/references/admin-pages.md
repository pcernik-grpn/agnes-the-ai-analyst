# Admin pages

Admin-only (the rail shows the Admin row only to admins). Organized exactly
like the admin sidebar: Overview, then the three MANAGE sections, then the
three MAINTAIN sections. When a non-admin needs one of these, tell them to
ask their admin and name the page.

- `/admin` — Overview: the admin dashboard — what needs attention (failing
  syncs, pending submissions, signals), not a menu.

## Manage

### People

- `/admin/users` — accounts: who has signed in, their groups, per-user
  detail pages.
- `/admin/tokens` — API tokens (PATs) across the instance.

### Data

One page with tabs, in pipeline order — where data comes in, what there is,
who receives it:

- `/admin/data-sources` — connected sources and their sync status; each
  source card's SYNC cell links to the run log.
- `/admin/tables` — the table registry: register a table, set its query
  mode and schedule. "How do I add a table?" lands here (or
  `POST /api/admin/register-table`).
- `/admin/data-packages` — bundle tables into packages and grant them to
  groups; this is what fills analysts' stacks.
- `/admin/semantic-layer` — the instance-wide metric/glossary registry, and
  whether what was imported is complete and healthy: coverage, health
  checks, mutes, feedback.
- `/admin/semantic-sources` — where the semantic model comes FROM, upstream
  of `/admin/semantic-layer`: register a git / upload / connection source
  (any adapter — native, Keboola, Snowflake, Databricks), sync one now,
  remove one.
- `/admin/sync` — sync status dashboard: per-table extraction state and a
  manual trigger (reached from the SYNC cell on `/admin/data-sources`).
- `/admin/extraction` — the SharePoint extraction FLEET dashboard: one row
  per connection's crawl + facts pass — phase, files done/seen, a derived
  files/min, facts done/pending, token spend, estimated cost, and a "stuck?"
  flag on a checkpoint stale past 10 minutes. Defaults to connections with a
  run active right now; `?all=1` shows every connection (reached from the
  *All connections* button in a SharePoint source card's Run row on
  `/admin/data-sources`).

### Access

- `/admin/access` — the groups workspace: members and grants side by side.
  The *Simulate a person* tab (`/admin/access?lens=simulate`) answers "what
  exactly does this user see?". Switching to the by-resource lens lists every
  grantable thing on the instance including *Collections* and *Files in
  collections* — each naming its owner and, for a collection, its file count,
  so "what files exist here and whose are they" is answerable without opening
  any of them. Names, formats and sizes only; nothing on this page shows file
  contents. Its **View a page as them** button goes one step further and
  opens Agnes with that person's access, read-only, behind a banner with a
  one-click exit — every write is refused while it is on, and it never
  confers admin authority (viewing as another admin included).

## Maintain

### Content

- `/admin/marketplaces` — register/sync the marketplace repositories the
  instance ingests.
- `/admin/store` — store moderation: approve or reject published items. Hidden
  by default (`features.store_moderation_enabled`); redirects home when off.
- `/admin/store/submissions` — community submissions queue.
- `/admin/store/lint` — advisory quality findings on published skills (body
  size, weak trigger phrasing, likely duplicates). Never blocks publication.
  No sidebar row — reached from the Submissions queue's toolbar.
- `/admin/corporate-memory` — corporate-memory domains and their content.

Four pages in this section are **hidden by default** — they redirect home and
have no sidebar row unless the instance turned them back on, so do not send
anyone to one without checking `/admin/server-config` first. Each is one flag
(the pages themselves are intact):

- `/admin/studio` and `/admin/studio/suggestions` — the Studio authoring
  surface and its moderation queue (`studio.enabled`). The Library's builders
  (`/library` → "+ New") do the same authoring jobs; go there instead.
- `/admin/knowledge-digests` — the maintained-digests page
  (`features.knowledge_digests_enabled`). Only the PAGE is hidden: `agnes admin
  digest` and the nightly digest job keep working, so an instance can be
  running digests with nothing to show for it here.
- `/admin/news` — author the in-app news (`features.news_enabled`, which also
  hides the `/news` reader).
- `/admin/contribute-skill` — publish a pasted SKILL.md into the instance's own
  marketplace (`features.contribute_skill_enabled`). The Library's skill
  builder is the supported path.

### Instance

- `/admin/server-config` — server configuration switches (feature flags,
  caps, providers).
- `/admin/database` — app-state database backend status and the
  DuckDB↔Postgres migration controls. No sidebar row: reached from the command
  palette (`g d`) or from the DuckDB-only notices that link it in context.
- `/admin/prompts` — the managed prompts (install, workspace CLAUDE.md,
  facts extraction) AND, on its "Template repository" tab
  (`/admin/prompts?tab=repo`), the Git repo every chat/analyst workspace is
  initialized from. One page: a prompt can bind to a file in that repo, so
  the repo and the prompts that read it are the same job.
- `/admin/datasource-credentials` — instance secrets for data sources.
- `/admin/mcp-sources` — external MCP servers offered to users, and
  per-tool grants. "+ Add MCP source" walks the connection, reads the server's
  tool list, and grants it in one pass. A saved source with `auth_method=oauth`
  needs one more step on its own detail page before any analyst can connect
  their own account: an admin registers the OAuth client there (auto-discovery
  or manual entry for a server without dynamic client registration).
- `/admin/linked-apps` — apps a connected MCP server lists, catalogued into
  the Library and granted to groups. The same three steps are offered while
  connecting the server; this is the door for one connected earlier.

### Activity

- `/admin/activity` — the audit log; also the cross-source "what failed
  today?" view.
- `/admin/telemetry` — usage telemetry.
- `/admin/sessions` — analyst (CLI) session transcripts.
- `/admin/chat` — web-chat session transcripts.
- `/admin/adoption` — adoption overview per user.

## API documentation (sidebar footer)

- `/documentation/api` — the API guide.
- `/docs` — interactive Swagger.
- `/redoc` — the ReDoc reference.
