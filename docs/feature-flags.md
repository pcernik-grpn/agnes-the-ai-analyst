# Feature flags

Canonical convention for gating a feature on/off in Agnes (#1022). Before this,
gating was heterogeneous — some flags read `instance.yaml` directly, some
consulted an env var only, some duplicated the truthy-string parsing inline.
This doc is the one pattern every new flag follows.

## The convention

**Naming.** A feature that owns a config section uses `<section>.enabled`
(e.g. `chat.enabled`, `guardrails.enabled`, `studio.enabled`,
`data_apps.enabled`). A small or experimental toggle with no section of its
own lives under the reserved `features.<name>` namespace instead of inventing
a top-level key.

**Resolution order**, identical for every flag:

```
env var  >  server-config overlay (DATA_DIR/state/instance.yaml)  >  instance.yaml (static base)  >  default
```

The middle two collapse into one step in code: `config/loader.py` deep-merges
the writable admin overlay over the static `config/instance.yaml` at load
time (see `app/instance_config.py::load_instance_config`), so
`get_value(*keys)` already returns the fully-resolved value. A flag's
resolver is therefore just:

```
env var (if set)  >  get_value(*keys)  >  default
```

**Env var naming**: `AGNES_<SECTION>_ENABLED` (uppercased section name), e.g.
`AGNES_CHAT_ENABLED`, `AGNES_STUDIO_ENABLED`, `AGNES_GUARDRAILS_ENABLED`,
`AGNES_DATA_APPS_ENABLED`. Terraform/infra-friendly — operators can flip a
flag per-deployment without touching the YAML.

**Truthy parsing** is shared across every boolean config/env value in Agnes,
not just feature flags: a Python `bool` passes through unchanged; a string is
false only for `"0"`, `"false"`, `"no"`, `"off"`, or `""` (case-insensitive) —
everything else, including an unrecognized typo, is true. This avoids a
truthy operator intent silently degrading to disabled because of a casing
mismatch.

**Default posture**: **new user-visible features default OFF** (`default=False`).
One flag is grandfathered on (`guardrails`) because it shipped enabled before
this convention existed and flipping it off by default would be a breaking
change for existing instances — don't use it as a precedent for a new flag's
default.

`studio` was the other grandfathered flag and is now `false`. That was a
deliberate behavior change, not a drift: the admin cleanup retired four
surfaces the Library builders had replaced, so `studio` joined the three new
off-by-default flags (`news`, `knowledge_digests`, `contribute_skill`) rather
than staying on for continuity. It is the precedent for *retiring* a surface —
default the flag off, leave the pages and the API in place, and gate the entry
points as well as the routes so nothing on screen links to a redirect.

## The helper

`app/instance_config.py::feature_enabled`:

```python
def feature_enabled(*keys: str, env_var: str | None = None, default: bool = False) -> bool:
    ...

feature_enabled("chat", "enabled", env_var="AGNES_CHAT_ENABLED", default=False)
```

Every flag resolver in the codebase (`get_studio_enabled`,
`get_guardrails_enabled`, the `chat.enabled` / `data_apps.enabled` read
sites) delegates to this function — nothing re-derives the truthy-string
rule or the env-over-yaml order by hand.

Two exceptions, by necessity rather than choice: `app.chat.config.load_chat_config`
parses a *caller-supplied* `instance.yaml` path, not the process-global merged
config `get_value()` reads from. Its `chat.enabled` resolution therefore reuses
only the shared truthy-parsing primitive (`app.instance_config.coerce_flag_value`)
rather than calling `feature_enabled` directly — the value *source* differs, the
resolution *order* and *parsing rule* do not.

This has a production consequence, not just a test one: `app/main.py` boots chat
from `load_chat_config(DATA_DIR/state/instance.yaml)` — the writable
server-config **overlay file alone**. A `chat.enabled` set only in the static
`config/instance.yaml` base is invisible to the chat runtime; enable chat via
the `/admin/server-config` editor (which writes the overlay) or the
`AGNES_CHAT_ENABLED` env var. The `/admin/server-config` flag inventory resolves
its `chat` row from the same overlay-only source the runtime uses, so the panel
always reflects what the app actually does.

`chat.approvals_enabled` (the `chat_approvals` flag) is the second exception,
for the same reason and with the same consequences: the chat gate reads it off
`load_chat_config`, so setting it in the static base config alone has no effect
— use the `/admin/server-config` editor or `AGNES_CHAT_APPROVALS_ENABLED`. Both
flags resolve their panel row through `_chat_flag_runtime_view`
(`app/api/admin.py`), fed by `_CHAT_RUNTIME_FLAGS` — a `{flag name: ChatConfig
attribute}` map derived from each switch's `runtime_view` field (see
`Switch.runtime_view` below), not hand-maintained. A third chat-resolved flag
needs only `runtime_view` set on its `Switch` entry — the map, and the panel
row, follow automatically. `switch_value()` refuses to resolve any switch that
declares `runtime_view` (it raises `ValueError` rather than silently reading
the wrong source) — see the caveat under "How to add a switch" below.

## The registry

`app/switches.py::SWITCHES` is a tuple of `Switch` entries, one per
operator-facing toggle. Each entry declares:

- `name`, `config_keys`, `env_var` — identity and where the value can come
  from, in the resolution order above.
- `kind`, `default`, `options` — every switch today is `kind="bool"`;
  `options` is for the `select` switches (`theme`, `ui_layout`, …) landing in
  a later PR.
- `on_invalid` — what a `select` switch does with a token not in `options`:
  `"default"` (the default) falls back to `default` silently, `"raise"`
  fails loudly at read time instead of guessing wrong (use for a switch
  where a bad value is worse than a crash, e.g. a backend selector). Ignored
  for non-`select` kinds.
- `effect` — what the running system can do with a new value: `live` (read
  per request — a save takes effect immediately), `restart` (read at boot —
  a save is stored and applies after a restart), or `deploy` (not a section
  of `instance.yaml` at all; it's what the container was started with, so
  there is nothing to write).
- `category` — the settings-panel display group (`product` / `operations` /
  `locked`), independent of `editable`: a `product` row can still be
  read-only.
- `editable` / `lock_reason` — whether the admin UI offers a write path for
  this switch, and, when it doesn't, the operator-facing reason: nothing to
  write (`effect="deploy"`), a deliberate security lock, or an unmet
  dependency. `editable` has no default — every entry states it explicitly,
  because `POST /api/admin/server-config` validates only the section name
  and then deep-merges the patch, so `editable=True` on one switch exposes
  every key in that switch's section, not just the switch's own key. Most
  switches in the table below are editable; the locked rows (`data_apps`,
  `data_apps_allow_same_origin`, `kai_broker_mcp_enabled`, `agent_profiles`,
  `extraction`) each state their reason in the table — e.g. `data_apps` is read per
  request, but the `apps_runner` sidecar it gates sits behind the `apps`
  Compose profile, so flipping it live would surface a feature with no
  backend running.
- `danger` — marks a switch whose flip is high-risk enough to warrant its
  own confirmation copy in the panel — the per-switch analog of the
  section-level `_DANGER_SECTIONS` gate in `app/api/admin.py` (`auth`,
  `server`). Not consumed anywhere yet; reserved for a future per-switch
  confirmation dialog. Every switch today leaves it at the default `False`.
- `runtime_view` — non-`None` for a switch whose *running* value is not read
  from the merged config, per the "Two exceptions" above: the `ChatConfig`
  attribute name holding the resolved flag (`chat` and `chat_approvals` are
  the current two). `switch_value()` raises `ValueError` for any switch that
  sets this rather than risk returning a value the runtime does not
  actually use — see "How to add a switch" below.
- `description` — the operator-facing summary shown in the panel and in the
  table below.

`app.instance_config.FEATURE_FLAGS` is the same tuple under its historical
name (`FeatureFlag` is an alias for `Switch`), so existing imports and the
resolution helper above keep working unchanged.

The registry backs the read-only **Feature flags** panel on
`/admin/server-config` (fed by the `feature_flags` block in
`GET /api/admin/server-config`), so an operator can see every switch's
effective value, where it came from (`env` / `config` / `default`), and
whether they can change it — and why not, when they can't — without
grepping the codebase. `app/api/admin.py::_EDITABLE_SECTIONS` is derived
from the same registry: any config section holding at least one
`editable=True` switch is automatically writable, so shipping a new
editable switch can no longer leave its section rejecting saves — the gap
that shipped `mcp.allow_query_param_token` without a write path.

## How to add a switch

1. Pick a name and config key: does the switch own a config section
   (`<section>.enabled`), or is it small enough for `features.<name>`? Also
   decide its `effect` (`live` / `restart` / `deploy`) and whether it's
   `editable` — most switches are; give the others a `lock_reason`.
2. At the read site, call `switch_value("<name>")` instead of hand-rolling
   `os.environ.get(...)` / `get_value(...)`. **Caveat:** this only works if
   the switch's runtime actually reads the merged config `switch_value()`
   resolves from. If your read site instead boots from its own
   caller-supplied config — as `chat` and `chat_approvals` do, from
   `load_chat_config(DATA_DIR/state/instance.yaml)`, the writable overlay
   file alone — declare `runtime_view` on the `Switch` entry instead (step 3)
   and give the panel its own resolver alongside `_chat_flag_runtime_view`
   in `app/api/admin.py`. `switch_value()` raises `ValueError` for any
   switch that sets `runtime_view`, so this cannot be discovered by a quiet
   wrong answer — only by the exception.
3. Append a `Switch` entry to `SWITCHES` in `app/switches.py` with a short
   operator-facing `description`.
4. Add a row to this doc's flag list below (or update the section it belongs
   to) so operators reading `docs/feature-flags.md` see it without reading
   the registry source.
5. See `CONTRIBUTING.md`'s sync-map — a new user-visible switch is a
   tracked row there too.

## Current flags

| Flag | Config key | Env var | Default | Editable | Notes |
|---|---|---|---|---|---|
| `studio` | `studio.enabled` | `AGNES_STUDIO_ENABLED` | `false` | yes | Authoring Studio: `/admin/studio`, its per-domain builders, the `/admin/studio/suggestions` moderation queue, their nav + command-palette entries, and the public suggestion API (403 when off). Was grandfathered ON; **off by default since the admin cleanup** — the Library builders (`/library`, "+ New") do the same authoring jobs, so two surfaces offered one job. Nothing is deleted: set it true to restore the whole surface. |
| `news` | `features.news_enabled` | `AGNES_NEWS_ENABLED` | `false` | yes | In-product news: the `/admin/news` editor, the `/news` reader, the `/home` "What's new" strip, and the rail + palette entries. Off by default since the admin cleanup. Hides UI only — `/api/admin/news/*` keeps serving and a published version stays in the table, so turning it on restores the surface with its content intact. |
| `knowledge_digests` | `features.knowledge_digests_enabled` | `AGNES_KNOWLEDGE_DIGESTS_ENABLED` | `false` | yes | The `/admin/knowledge-digests` admin PAGE and its nav row. Off by default since the admin cleanup. Deliberately narrow — gates the page, not the feature: `/api/admin/knowledge-digests/*`, `agnes admin digest`, the digest scheduler job and `agnes pull`'s digest delivery are untouched, so an instance already running digests keeps running them headlessly. |
| `contribute_skill` | `features.contribute_skill_enabled` | `AGNES_CONTRIBUTE_SKILL_ENABLED` | `false` | yes | The paste-a-SKILL.md publish page (`/admin/contribute-skill`), the landing target for an external "Load skill to Agnes" button. Off by default since the admin cleanup — the Library's skill builder is the supported path. Both POST handlers carry the gate too, so a stale external button gets a redirect home rather than a silent publish. |
| `guardrails` | `guardrails.enabled` | `AGNES_GUARDRAILS_ENABLED` | `true` | yes | Grandfathered. Env override added in #1022 (new, additive). |
| `chat` | `chat.enabled` | `AGNES_CHAT_ENABLED` | `false` | yes | New feature — off by default. |
| `chat_provider` | `chat.provider` | `AGNES_CHAT_PROVIDER` | `kai-agent` | yes (applies after restart) | Which engine runs web/Slack chat sessions: `kai-agent` (the embedded kai-agent turn engine, the default — see docs/cloud-chat.md) or `docker` (self-hosted container per session). Resolved by `load_chat_config` (env > instance.yaml > default) at boot; every provider rides deployment-provisioned backing (kai-agent sidecar + `KAI_HOST_JWT_SECRET`, or the apps-runner sidecar), and app/main.py's boot gates refuse a provider whose backing is absent, loudly. Pin it in infrastructure via the customer-instance module's per-VM `chat_provider` field (`AGNES_CHAT_PROVIDER`) so a fresh data disk boots into the right engine. |
| `chat_approvals` | `chat.approvals_enabled` | `AGNES_CHAT_APPROVALS_ENABLED` | `true` | yes | Operator kill-switch for interactive approval prompts. Off denies ask-flagged tool calls instantly instead of waiting for a human. |
| `chat_bootstrap_marketplace` | `chat.bootstrap_marketplace` | `AGNES_CHAT_BOOTSTRAP_MARKETPLACE` | `true` | yes (applies after restart) | Deliver the caller's RBAC-filtered marketplace PLUGINS into chat sessions — skills, agents, slash commands, hooks and MCP servers, not just skills. Agnes ships the marketplace from the server: on `docker` the sandbox's own CLI installs real plugins from it offline; on `kai-agent` (whose sandbox Agnes never enters) the components ride the workspace tarball as project files. ON by default since 0.87.1: with it off the composer's slash menu still listed those skills while nothing delivered them, so picking one answered "Unknown command" (#1552). Turning it off now also removes them from the menu — the menu never offers what nothing delivers. See docs/cloud-chat.md → *Marketplace plugins in a chat session* for the per-component invocation tokens, which differ between the two shapes. |
| `chat_broker_admin_reads` | `chat.broker_admin_reads` | `AGNES_CHAT_BROKER_ADMIN_READS` | `true` | yes (live — read per request) | Replay read-only (GET/HEAD) admin API routes through the chat secret broker, so `agnes admin list-users`/`list-tables`/… work inside a chat sandbox. The replay runs under the session user's own identity and the route's live `require_admin` still decides — non-admin users and agent principals get the route's own 403 regardless, so the switch only widens what an actual admin's interactive session may read. Admin MUTATIONS are always refused from sandboxes (403 `admin_mutations_require_interactive_auth`), independent of this switch. Unlike the other `chat.*` flags this one is NOT resolved by `load_chat_config` at boot; `app/api/broker.py` reads it live per request, so flipping it needs no restart. |
| `data_apps` | `data_apps.enabled` | `AGNES_DATA_APPS_ENABLED` | `false` | no — apps_runner sidecar needs the `apps` Compose profile | New feature — off by default. |
| `data_apps_allow_same_origin` | `data_apps.allow_same_origin` | `AGNES_DATA_APPS_ALLOW_SAME_ORIGIN` | `false` | no — deliberate security lock: a deployment decision, set in instance.yaml/env, not a live panel toggle | Serve hosted data apps on the MAIN origin (same origin as the Agnes `/api`) for ALL apps and callers. Off by default: a hosted app runs user-authored JS, and same-origin that JS shares the viewer's session and can read `/api` — no response header closes a same-origin read, so the supported isolation is `data_apps.subdomain_base` (per-app origins). Requests arriving on a data-app subdomain are always served regardless of this flag, and the in-chat preview works without it via its per-app `data-app-preview:<slug>` token. Turn on only when every app author is trusted with every viewer's session. See `docs/architecture.md#hosted-data-apps`. |
| `library_show_unverified_trust` | `library.show_unverified_trust` | `AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST` | `true` | yes | 'Community' trust marker for unverified Store items in the Library, so every row states its provenance (Organization / Verified / Community) and none is left silently unlabelled. On by default: the whole trust vocabulary is gated to the paper theme, so upgrade parity for a default blue instance comes from that gate, not from this flag. Set `false` for the older reading, where an unverified item is marked by the ABSENCE of a marker. |
| `experience` | `instance.experience` | `AGNES_INSTANCE_EXPERIENCE` | `redesign` | yes | Retired as a choice (Wave 0, 2026-08) — see the dedicated section below. `redesign` is the only valid value and the default; the old `classic` value (or any other unrecognised string) falls back to `redesign`. Changes only the DEFAULTS of the coupled knobs; any per-knob setting wins. |
| `stack_auto_membership` | `features.stack_auto_membership` | `AGNES_STACK_AUTO_MEMBERSHIP` | `true` | yes | Stack membership mode. On (the default since Wave 0, 2026-08 — `experience` is always `redesign` now): auto-membership — every granted resource is in the caller's stack immediately; subscribe/unsubscribe only control the downloaded local copy, and `agnes pull` manifests list granted-but-unsubscribed tables as `server_only`. Off: the classic subscribe model — required plus subscribed grants — with the grant-downgrade subscription fan-out, exactly the pre-redesign behavior; still fully supported, and an explicit `false` always wins over the default. Flipping OFF after running ON: users lose visibility of granted-but-unsubscribed resources until they subscribe (no data loss — subscriptions are interpreted, never rewritten); flipping ON later only widens visibility. |
| `mcp_query_param_token` | `mcp.allow_query_param_token` | `AGNES_MCP_ALLOW_QUERY_PARAM_TOKEN` | `true` | yes | Grandfathered on. Accepts the MCP bearer token as `?token=` on SSE GET for header-incapable clients; the token then appears in every request log (CWE-598). Turn off when all MCP clients send the `Authorization` header. **Unlike the other flags here its default is the permissive state, so an unrecognized value — `disabled`, `n` — silently leaves it on; use `false`/`off`/`no`/`0` and verify via `effective`.** |
| `kai_broker_mcp_enabled` | `kai.broker_mcp_enabled` | `KAI_BROKER_MCP_ENABLED` | `false` | no | Lets the embedded kai-agent turn engine's sandbox reach this instance's own MCP server, by issuing the `kai_mcp` ticket scope alongside `llm`. Off by default: the engine registers no host MCP server and runs on its built-in tools only. On, the agent gets the CALLER's tool surface — resolved through a short-lived `mcp-oauth` access token minted for that user, so it follows their stack and grants and never inherits an admin's catalog. Not admin-editable: it is only meaningful on an instance that embeds the engine at all, which is gated on the `KAI_HOST_JWT_SECRET` deployment secret — a dependency the settings panel cannot satisfy. |
| `mcp_source_url_strict` | `mcp.source_url_strict` | `AGNES_MCP_SOURCE_URL_STRICT` | `false` | yes | Holds a registered MCP source's own url to the same bar as its OAuth endpoints (https, public address). Off by default, which is **not** unguarded: the baseline always refuses link-local / metadata / multicast / reserved addresses and cleartext http to a public one. The default only permits a source on an *internal* address — an organization's own tool server, a developer's localhost — because those are ordinary deployments. Turn on for instances that talk only to third-party MCP services; it makes an intranet source unconfigurable, hence opt-in. |
| `library_auto_share_admin_uploads` | `library.auto_share_admin_uploads` | `AGNES_LIBRARY_AUTO_SHARE_ADMIN_UPLOADS` | `false` | yes | Auto-share collections an ADMIN creates in the Library to the `Everyone` group at creation, so admin uploads are workspace-visible — Library, the chat agent's collection tools, and `agnes pull` knowledge artifacts — with no manual share step. Writes an ordinary revocable Everyone grant (visible in `/admin/access`, removable per collection in the Share dialog). Off by default so an upgrade never changes who sees data. Scope is the Library/API creation path only: non-admin uploads and chat file drops stay private. |
| `keboola_token_header` | `auth.keboola.allow_token_header` | `AGNES_KEBOOLA_ALLOW_TOKEN_HEADER` | `false` | yes | Accept a Keboola Storage API token in the X-StorageApi-Token header as API authentication (a master token for the bound project; on a wildcard multi-project instance — `project_id` `"*"`/unset with `multi_project_mode` select/auto — from any project on the stack; existing users only). Off by default: a plain Storage token carries no interactive factor, so enabling this bypasses any MFA/SSO the organization enforces on web logins, and it grants the mapped user's full Agnes authority (PAT-equivalent) — including admin mutation endpoints if that user is an admin; the narrowing is on the data-read surface, not the admin gate. Credential-minting endpoints (PAT/MCP/agent/data-app) stay blocked. |
| `microsoft_group_sync` | `auth.microsoft.group_sync_enabled` | `AGNES_MICROSOFT_GROUP_SYNC_ENABLED` | `false` | yes | Mirror the signed-in user's Entra ID group memberships (Microsoft Graph `GET /me/memberOf`) into `user_group_members` (`source='microsoft_sync'`) on every Microsoft sign-in — the group-sync counterpart of `google_sync`. New feature — off by default: turning it on also widens the OAuth consent scope requested at `/auth/microsoft/login` to the delegated Graph permission `GroupMember.Read.All`, which needs its own admin-consent grant in the Entra app registration (see `docs/auth-microsoft-oauth.md`) and only takes effect after a restart — the sync gate itself is read live, so a stale token scope degrades to a fail-soft no-op rather than a login failure. `AGNES_MICROSOFT_GROUP_PREFIX` (env-only, no `instance.yaml` key — mirrors `AGNES_GOOGLE_GROUP_PREFIX`) narrows which fetched groups are mirrored / required to sign in. |
| `keboola_multi_project_mode` | `auth.keboola.multi_project_mode` | `AGNES_KEBOOLA_MULTI_PROJECT_MODE` | `disabled` | yes | What a Keboola OAuth sign-in does with the OTHER projects the user can reach. `disabled` (default): the single-project login only — gated on `auth.keboola.project_id`, nothing provisioned (a `single` value from older configs falls back here, same behavior). `select`: Agnes discovers the user's projects at login (`/v1/auth/token/introspect` on the OAuth host, narrowed by `allowed_roles`) and stashes the list, vault-encrypted with a 15-minute TTL, for a user-driven import via `/api/auth/keboola/projects`. `auto`: trusted auto-provisioning — on every login each allowed project gets a project-scoped PAT (minted via `/v1/auth/pat/exchange`, writable for `admin` role, read-only otherwise) vaulted as its connection's storage token (master slot too when it verifies as a master token, which is what the semantic-layer sync enumerates), a `source_connections` row per `(stack_url, project_id)`, chat tools, and `kbc-<project>-<role>` group membership synced (`source='keboola_sync'` rows only — lost upstream access removes membership; group `tool_grants` stay, membership is the revocation lever). Requires `AGNES_VAULT_KEY`. With `auth.keboola.project_id: "*"` (or unset) the login gate widens to "reaches ≥1 allowed-role project" — in BOTH active modes, and when `allow_token_header` is also on, the `X-StorageApi-Token` API-auth binding widens with it (an existing user's master token from any project on the stack authenticates); a concrete `project_id` keeps the single-project gate and narrows discovery to it. **Wildcard on a shared stack:** a Keboola role is per-project — every user is `admin` of their own project — so on a multi-tenant stack the wildcard admits ANY user of the stack no matter what `allowed_roles` says (it narrows which of a user's projects take part, never which organization signs in); reserve `"*"` for dedicated single-organization stacks and pin a concrete `project_id` on shared ones. Auto-provisioning never overwrites a credential a human stored — it rotates only rows it created for the same user (`config.user_email`), and fills empty slots. |
| `mcp_session_pool` | `mcp.session_pool` | `AGNES_MCP_SESSION_POOL` | `true` | yes | Keeps a **stdio** MCP server's process warm between tool calls instead of starting one per call — the upstream's own import tree costs ~6 s every time, so an agent reaching for five tools spent half a minute in startup. On by default as a **deliberate, documented exception** to the default-off posture: that rule is for new user-visible features, and this adds no surface — same tool calls, faster — while the flag exists as its kill switch (same family as `chat_approvals`); default-off would keep every upgraded instance on the ~6 s-per-call path unless each operator discovered the flag. The trade: reuse is a semantic change for an upstream that keeps per-process state — that upstream is exactly what the switch is for. Read per call, so a save applies to the next tool call; already-warm sessions age out on their own. Turn off for a process per call: the debugging shape, and the answer for an upstream that cannot survive being reused. `http`/`sse` sources are unaffected — they have no spawn to amortize. Four process-level tuning knobs sit beside it and are deliberately **not** registry switches: `AGNES_MCP_SESSION_IDLE_S` (default `180`, how long an unused session is kept), `AGNES_MCP_SESSION_POOL_MAX` (default `8`, live sessions per process), `AGNES_MCP_SESSION_SPAWN_TIMEOUT_S` (default `60`, how long a starting server may take to answer `initialize` before the half-started process is cancelled and the next call spawns fresh; `0` disables), and `AGNES_MCP_SESSION_CALL_TIMEOUT_S` (default `300`, ceiling on one pooled tool call — calls on a warm session are serialized, so past it the call errors, the session is closed, and callers queued behind it get a clear busy error instead of waiting forever; `0` disables both bounds). They gate nothing on or off, they are read once at import — so a live save would display a value the running process is not using — and their unit is a latency/lifetime trade-off per deployment rather than a product decision. Set them in the environment and restart. A non-numeric or non-finite value falls back to the default with a warning rather than failing the connector at import. |
| `mcp_connector_ui` | `mcp.connector_ui_enabled` | `AGNES_MCP_CONNECTOR_UI_ENABLED` | `true` | yes | User-facing MCP connector surface: the `/me/ai-connector` and `/mcp-connect` install-instruction pages, the MCP tab of `/how-it-works#connect`, and their nav / command-palette entries. On by default (current behavior unchanged). Turn off on a VPN/intranet-only instance where cloud-side MCP clients (e.g. a hosted connector resolved from outside the network) can never reach the endpoint — so users are not shown a setup path that cannot work for them. Hides UI ONLY: the MCP protocol endpoints (`/api/mcp/http`, `/api/mcp/sse`) keep serving in-network clients regardless of this flag. |
| `mcp_source_url_runtime_enforce` | `mcp.source_url_runtime_enforce` | `AGNES_MCP_SOURCE_URL_RUNTIME_ENFORCE` | `false` | yes | Enforces the DNS-free half of the url policy (scheme + literal-IP checks) at the two credentialed forward seams too (#1216), not only when a source is configured. Off by default: an already-enabled legacy source keeps forwarding exactly as it does today. **Before turning this on**, check the `url_policy_verdict` column on the admin MCP source list (`GET /api/admin/mcp-sources` / `agnes admin mcp source list`) for any `would_refuse` row and fix its url first — this switch turns each one into a refused call, with no other warning. |
| `agent_profiles` | `agent_profiles.enabled` | `AGNES_AGENT_PROFILES_ENABLED` | `true` | no — deliberately env-var-only kill switch (owner decision on #1186) | Grandfathered — shipped enabled before this flag existed. Gates the `/agents` builder and the `/api/v1/agents*` management + runtime API (and its CLI clients, `agnes agent`/`agnes chat`). Does not gate default-agent seeding, chat attribution, or the broker's agent policy — those are internal mechanisms, not HTTP surface. Set the env var, or hand-edit the static `instance.yaml`, and restart. |
| `access_policies` | `access_policies.enabled` | `AGNES_ACCESS_POLICIES_ENABLED` | `false` | yes | Table access policies (row filtering + column masking via one admin-authored SQL policy per non-distributed table). New feature — off by default. Gates *attaching* a policy at `PUT /api/admin/registry/{id}` only; a table that already carries one stays protected — and the distribution interlock (a policied table can't be made distributable, and no other row may point at its physical source while distributable) stays enforced — regardless of this flag's later state. |
| `facts` | `facts.enabled` | `AGNES_FACTS_ENABLED` | `false` | yes | Fact graph over Collections (design doc `2026-08-27-fact-graph-over-collections-design.md`) — typed subjects (facts/edges) extracted from Collections documents, each claim carrying its evidencing document, verbatim quote and date. Gates the whole `/api/facts*` router (`404` when off — see `app.auth.access.require_facts_enabled`). Postgres-only (A3 ratchet): a DuckDB-backed instance answers a typed `501` regardless of this flag. New feature — off by default. Read surface (`search`/`neighbors`/`claims`, any authenticated caller) and write surface (`ingest` + corrections CRUD/export, scheduler-token-or-admin) both live behind this one flag. The read surface is available across all three surfaces — REST, `agnes facts search\|neighbors\|claims`, and the `fact_search`/`fact_neighbors`/`fact_claims` MCP foundation tools; the write surface is REST-only by design (a producer/admin contract, not an analyst command). |
| `facts_visibility_mode` | `facts.visibility_mode` | `AGNES_FACTS_VISIBILITY_MODE` | `any_evidence` | yes | Fact-graph subject existence rule (design doc §4): `any_evidence` — a subject is visible if AT LEAST ONE of its claims is in a readable collection (default). `all_evidence` — visible only if ALL of its claims are readable; hides strictly more within one grant snapshot (not a general monotonic guarantee across grant changes). Facts and edges use the same rule. |
| `extraction` | `extraction.enabled` | `AGNES_EXTRACTION_ENABLED` | `false` | no — needs a configured `extraction.producer` command AND a worker process polling the `extraction` lane (the `extraction-worker` Compose profile) | Document extraction (spec §7.5 "Extraction inside Agnes (later)") as its own worker lane — gates the `corpus-extraction` job kind's handler, which shells out to the operator-configured producer command/module. New feature — off by default. |

## The `instance.experience` preset

Originally a one-line switch that flipped the DEFAULTS of every
experience-coupled knob between a `classic` and a `redesign` world (spec
`docs/superpowers/specs/2026-08-07-default-chrome-ux-parity.md`). Wave 0
(2026-08, `docs/superpowers/specs/2026-08-13-grounded-analyst-workspace-design.md`)
retired the `classic` side of that choice — the classic experience (topnav
chrome, pre-redesign pages, legacy chat) no longer exists, so there is
nothing left for the preset to switch between:

| | |
|---|---|
| Config key | `instance.experience` |
| Values | `redesign` (the only valid value, and the default) |
| Env | `AGNES_INSTANCE_EXPERIENCE` |

The old `classic` value (or any other unrecognised string) falls back to
`redesign` with a startup warning; the key is kept only so an older
`instance.yaml` still boots. `redesign` still defaults `instance.theme` to
`paper` and `features.stack_auto_membership` to `true` — any per-knob
env/yaml setting still wins, and per-knob precedence is unchanged
(`env(knob) > yaml(knob) > preset-implied default > built-in default`).
`instance.ui_layout` is no longer part of the coupling: the rail chrome is
now the only chrome, hard-wired independently of this preset (a configured
`ui_layout`/`AGNES_UI_LAYOUT` is tolerated but inert — ignored with its own
startup warning; see `docs/CONFIGURATION.md`). Deliberately NOT coupled:
`store.verification_enabled` (a governance opt-in — it needs a reviewer, not
a theme) and `library.show_unverified_trust` (the trust vocabulary is
already gated to the paper theme itself). The `/admin/server-config` flag
inventory leads with the preset's resolved value and labels preset-sourced
flag defaults with a `preset` badge.
