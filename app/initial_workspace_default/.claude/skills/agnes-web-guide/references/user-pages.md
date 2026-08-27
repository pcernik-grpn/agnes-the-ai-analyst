# User-facing pages

Every page a signed-in (non-admin) user can open. Format: path — what the
user sees there, and when to send them.

## Rail destinations

- `/chat` — a new conversation with you. The empty state is the landing
  dashboard: greeting, composer, task-starter suggestions.
- `/chats` — every conversation the user has had: search, filter, sort,
  rename, pin, bulk cleanup. "Where did our conversation from last week go?"
  → here.
- `/library` — the one browse surface for everything the user can have or
  already has: uploaded files & artefacts, data packages, corporate-memory
  domains, marketplace plugins, recipes, skills, agents, and items shared
  with them. A scope toggle switches *Available* (everything their groups
  are granted) vs *My Stack* (what they subscribed to); the *+ Add* menu
  holds the install/subscribe/upload paths. Rows open detail pages:
  a collection at `/library/{slug}`, a single file at
  `/library/{slug}/f/{file_id}`, a table at `/catalog/t/{table_id}`
  (schema, sample rows, query mode), a data package at
  `/catalog/p/{slug}`, a recipe at `/catalog/r/{slug}`, a memory domain at
  `/memory/d/{slug}`, a hosted data app at `/apps/detail/{slug}`.
- `/agents` — the agent builder: create a named, scoped agent over the
  user's own stack, pin its model, set its memory mode, issue a PAT for
  calling it as an API.
- `/home` — the landing hub. For a user whose workspace is not set up yet
  it walks through the install; once onboarded it is a shortcut hub.

## Data & semantics

- `/semantic-layer` — the semantic models: datasets, fields, relationships,
  metrics, glossary. Each model opens at `/semantic-layer/{slug}` and each
  object at `/semantic-layer/{slug}/{object_id}`.
- `/catalog/semantics` — browse the catalog's metric definitions and
  glossary terms in one place. "What is our canonical MRR?" → here (or
  `agnes catalog --metrics` in chat).

## Skills, plugins & the store

- `/skills` — the Builder: author a skill, plugin, or agent template in the
  browser and publish it to the store.
- `/store/new` — upload a ready-made plugin ZIP to the store.
- `/store/examples` — example plugins to copy the shape from.
- `/marketplace/guide/curated` — authoring guide for curated marketplace
  repositories.
- `/marketplace/guide/flea` — guide for community (flea-market) submissions.
- `/marketplace/format-guide` — reference for the marketplace metadata
  format.
- `/admin/studio` — the Studio authoring surface for corporate-memory
  content. Despite the URL prefix it is open to every signed-in user when
  the instance has Studio enabled.

## Account & help (user menu)

- `/me/profile` — profile, API tokens, group memberships, password change,
  and (for admins) the admin-mode toggle.
- `/me/connections` — the user's own data-source and MCP connections.
- `/me/activity` — their personal activity feed.
- `/me/memory-mining` — opt in/out of memory mining over their sessions.
- `/mcp-connect` — mint a token and connect an external AI client over MCP.
- `/how-it-works` — the product explainer; owns the "connect an AI client"
  walkthrough and links the setup pages.
- `/setup-advanced` — advanced install paths for the local `agnes` CLI
  workspace.
- `/news` — in-app news and announcements.
- `/documentation/api` — the REST API guide; interactive Swagger lives at
  `/docs` and the reference at `/redoc`.
