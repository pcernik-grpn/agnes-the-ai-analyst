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
- `/admin/semantic-layer` — the instance-wide metric/glossary registry and
  semantic-model sources.
- `/admin/sync` — sync status dashboard: per-table extraction state and a
  manual trigger (reached from the SYNC cell on `/admin/data-sources`).

### Access

- `/admin/access` — the groups workspace: members and grants side by side.
  The *Simulate a person* tab (`/admin/access?lens=simulate`) answers "what
  exactly does this user see?".

## Maintain

### Content

- `/admin/marketplaces` — register/sync the marketplace repositories the
  instance ingests.
- `/admin/store` — store moderation: approve or reject published items. Hidden
  by default (`features.store_moderation_enabled`); redirects home when off.
- `/admin/store/submissions` — community submissions queue.
- `/admin/store/lint` — lint results for store content.
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
- `/admin/database` — app-state database backend status.
- `/admin/initial-workspace` — the template every chat/analyst workspace is
  initialized from.
- `/admin/prompts` — the agent and workspace prompt overrides.
- `/admin/datasource-credentials` — instance secrets for data sources.
- `/admin/mcp-sources` — external MCP servers offered to users, and
  per-tool grants.
- `/admin/linked-apps` — linked external applications.

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
