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
- `/admin/store` — store moderation: approve or reject published items.
- `/admin/store/submissions` — community submissions queue.
- `/admin/store/lint` — lint results for store content.
- `/admin/studio` — the Studio authoring surface (also open to non-admins
  when enabled — see the user pages).
- `/admin/studio/suggestions` — suggestions harvested for Studio curation.
- `/admin/corporate-memory` — corporate-memory domains and their content.
- `/admin/knowledge-digests` — periodic knowledge digests.
- `/admin/news` — author the in-app news.
- `/admin/contribute-skill` — contribute a skill directly into the
  instance's own marketplace.

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
