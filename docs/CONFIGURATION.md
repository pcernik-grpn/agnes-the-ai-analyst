# Configuration Reference

This is the single authoritative map of everything an operator can customize
per instance. Three independent tiers feed an instance's behavior; the knob
table below names every resolver, its env override, its `instance.yaml` path,
and its default.

> The per-instance knob table is guarded by
> `tests/test_config_reference_coverage.py`: every `get_*` resolver in
> `app/instance_config.py` must appear here, so this doc cannot silently drift
> behind the code.

## How configuration resolves

Most knobs resolve in this order — **first non-empty wins**:

1. **Environment variable** (e.g. `AGNES_HOME_ROUTE`, `DATA_SOURCE`,
   `SLACK_TRANSPORT`) — set in `.env` / by Terraform. **Overrides everything.**
2. **`instance.yaml`** — the static base at `config/instance.yaml` deep-merged
   with the admin overlay written to `${DATA_DIR}/state/instance.yaml` by
   `/admin/server-config`. The overlay wins per-leaf.
3. **Built-in default** — baked into the resolver in `app/instance_config.py`.

> **Footgun:** because the env var wins over `instance.yaml`, an instance that
> pins a knob via an env var **cannot** later change it through the
> `/admin/server-config` UI — the UI writes the YAML tier, which the env tier
> shadows. Pin via env **or** manage via YAML, not both. (This is exactly why
> the Terraform `home_route` knob below writes its env line *only* when set to a
> non-empty value — see [Infra patterns](#infra-patterns-and-knob-reachability).)

A few structural knobs (lists/objects that don't round-trip through env vars —
`datasets`, `theme`, `custom_scripts`, …) are **YAML-only**; their rows show
`—` in the env column.

### A separate tier: the Initial Workspace Template

The **analyst workspace payload** and the **init prompt** rendered on `/home`
are NOT in this file's tier system — they come from a registered *Initial
Workspace Template* (IWT) seed repo, resolved as **operator IWT clone > bundled
snapshot in the wheel**. See
[`initial-workspace-override.md`](initial-workspace-override.md) and
[`seed-repo-contract.md`](seed-repo-contract.md). Configure it at
`/admin/server-config` → *Initial Workspace Template*.

### Infra patterns and knob reachability

Whether the **env-var tier** is reachable at deploy time depends on the infra
pattern serving the instance:

- **Self-contained infra** (the deployment writes its own `/opt/agnes/.env`):
  can set any `AGNES_*` env knob directly.
- **Upstream `infra/modules/customer-instance` module** (consumed via an
  `infra-vX.Y.Z` tag): can only set the env knobs the module exposes as
  Terraform variables. If the module doesn't expose a knob, that instance falls
  through to the `instance.yaml` tier (admin UI) for it.

The module's `dispatcher_image` / `runtime_secret_env` family are the
canonical always-wins-env example — those write real `.env` lines every boot,
by design (they're bootstrap secrets, not admin-owned presentation choices).

A separate, THIRD path exists for knobs that are UI-owned but still need a
day-1 value from Terraform: the **first-boot instance.yaml seed** (see
[Config ownership map](#config-ownership-map) below). `home_route`, `theme`,
`experience`, `studio_enabled` and (D1 residual, 2026-08) `data_source.type`
are the canonical examples — the module writes them into
`/data/state/instance.yaml` the very first time a VM boots and never again,
so `/admin/server-config` is the sole owner from day 2 onward. This is
deliberately NOT the env-var tier: it never touches `.env` and a later
Terraform apply/recreate cannot silently re-assert a value an admin already
changed. `data_source.type` additionally flips its own app-side precedence
(overlay wins over the `DATA_SOURCE` env var, not the reverse) — belt and
braces so a stale `.env` left over from before this change, or an
already-running container's baked-in environment, can't shadow a UI edit
either. Because a VM provisioned *before* this seed existed already has an
`instance.yaml` (so the "only when absent" seed above never fires on it),
`data_source.type` also carries a one-time, idempotent boot-time **backfill**:
on the first boot with the new startup script, it writes `data_source.type`
into the existing overlay from the still-available `$DATA_SOURCE` Terraform
variable — but only if the key is not already present, so it can never
overwrite a later admin edit. This is what actually migrates an
already-deployed VM; the precedence flip above only decides which value wins
once one exists.

---

## Config ownership map

Every knob has exactly one process that writes it after an instance is up and
running — this table names it, so "why doesn't my `/admin/server-config`
change stick" has one place to check. Four owner shapes:

- **bootstrap-env** — a real `.env` line, rewritten on every boot by the
  provisioning script or set once by a self-contained deployment. Always
  wins over `instance.yaml` (see [How configuration resolves](#how-configuration-resolves));
  an admin cannot override it without hand-editing `.env` (or, on the
  upstream module, changing the Terraform variable and re-applying).
- **secret-manager** — fetched fresh from GCP Secret Manager at boot and
  written into `.env` as a bootstrap-env line; same reachability rule as
  bootstrap-env, plus rotation requires a new Secret Manager version (see
  `docs/RELEASING.md`'s Secret Manager gotcha).
- **first-boot-seed** — written into the writable `instance.yaml` overlay
  (`${DATA_DIR}/state/instance.yaml`) only when that file does not exist yet
  (a brand-new VM). Every later boot (recreate, apply, auto-upgrade) leaves
  the file alone. This is the D1 (2026-08) pattern: Terraform states the
  day-1 value, the admin UI owns everything after.
- **UI** — the admin overlay (`${DATA_DIR}/state/instance.yaml`), written
  exclusively through `/admin/server-config` (or `agnes admin config` /
  hand-editing the file). Agnes has no DB-native config store yet — "the UI"
  means this YAML overlay, not a database table.

| Knob | Owner | Change it via | What wins |
|------|-------|----------------|-----------|
| `instance.theme` (UI palette) | first-boot-seed (was bootstrap-env before D1) | `/admin/server-config` → Branding & UI (after day 1); `prod_instance.theme` / `dev_instances[].theme` (day-1 seed only) | `AGNES_INSTANCE_THEME` env (if hand-set) > `instance.yaml` > default |
| `instance.home_route` | first-boot-seed (was bootstrap-env before D1) | `/admin/server-config` (after day 1); `var.home_route` (day-1 seed only, module-wide) | `AGNES_HOME_ROUTE` env (if hand-set) > `instance.yaml` > default |
| `studio.enabled` | first-boot-seed (was bootstrap-env before D1) | `/admin/server-config` (after day 1); `var.studio_enabled` (day-1 seed only, module-wide) | `AGNES_STUDIO_ENABLED` env (if hand-set) > `instance.yaml` > default |
| `instance.experience` | first-boot-seed (was bootstrap-env before D1) | `/admin/server-config` (after day 1); `prod_instance.experience` / `dev_instances[].experience` (day-1 seed only) | `AGNES_INSTANCE_EXPERIENCE` env (if hand-set) > `instance.yaml` > default |
| `instance.{brand,brand_short,subtitle,copyright,logo_svg,favicon,custom_scripts}` | first-boot-seed (unchanged — the pattern D1 extends) | `/admin/server-config` (after day 1); the matching `prod_instance`/`dev_instances[]` fields (day-1 seed only) | env (per-field, if hand-set) > `instance.yaml` > default |
| `theme:` colour overrides (`theme.primary`, etc. — distinct from `instance.theme` above) | first-boot-seed (unchanged) | `/admin/server-config` (after day 1); `prod_instance.theme_colors` / `dev_instances[].theme_colors` (day-1 seed only) | `instance.yaml` > default (YAML-only, no env override) |
| `database.backend` | first-boot-seed (unchanged — the A1 pattern D1 follows) | the DB backend state machine / migration UI (after day 1); seeded `side_car` on a fresh VM | state-machine-managed; not a plain env/YAML precedence |
| `data_source.type` (the connector: `keboola`/`bigquery`/`local`) | first-boot-seed (was bootstrap-env before D1 residual) | `/admin/server-config` (after day 1); `var.data_source` (day-1 seed only, module-wide) | **`instance.yaml` > `DATA_SOURCE` env > default** — the one knob in this doc where the overlay outranks env, not the other way around (see `get_data_source_type()`); protects against a stale `.env`/already-running-container env from before this change |
| Per-connection `data_source.{keboola,bigquery,snowflake,databricks}.*` settings (credentials, stack URL, etc.) | bootstrap-env / `instance.yaml` (mixed) | `.env` (self-contained infra) or the relevant Terraform variable + re-apply; `/admin/server-config` also writes `instance.yaml` | env > `instance.yaml` > default — **unchanged, see D2** (a connection model for derived sources) |
| `SERVER_URL` / `AGNES_BASE_URL` / `DOMAIN` | bootstrap-env | `.env` (self-contained infra) or the module's `domain`/TLS variables + re-apply | env only (no `instance.yaml` path) — **unchanged, out of scope for D1** |
| `tls_mode` / Caddy TLS | bootstrap-env | the module's `tls_mode` variable + re-apply | Terraform-driven compose overlay selection — **unchanged, out of scope for D1** |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`, `KEBOOLA_STORAGE_TOKEN`, `JWT_SECRET_KEY`, `SESSION_SECRET`, `POSTGRES_PASSWORD` | secret-manager | Secret Manager version + re-apply (or hand-edit `.env` for self-contained infra) | env only — **unchanged, out of scope for D1** |
| `chat.provider` (`AGNES_CHAT_PROVIDER`) | bootstrap-env | `.env` or the module's per-VM `chat_provider` field + re-apply; `/admin/server-config` also writes `instance.yaml`, but env still wins | env > `instance.yaml` > default — **deliberately excluded from D1**: it pins deployment-provisioned backing (the kai-agent sidecar / apps-runner), not a pure presentation choice |
| Per-connection settings for derived sources (Snowflake / BigQuery / Databricks rows) | bootstrap-env / `instance.yaml` (mixed, no single connection model yet) | `/admin/server-config` writes `instance.yaml`; some credentials are env-only | see `docs/DATA_SOURCES.md` — **unchanged, see D2** |

---

## Per-instance knob reference

Every resolver lives in [`app/instance_config.py`](../app/instance_config.py).
Set the env var in `.env`/Terraform, or the YAML path in `instance.yaml`.

### Branding & UI

| Knob | Env override | `instance.yaml` path | Default | Resolver |
|------|--------------|----------------------|---------|----------|
| Deployment display name (page titles, email subjects) | — | `instance.name` | `AI Harness` | `get_instance_name()` |
| Header subtitle | — | `instance.subtitle` | `""` | `get_instance_subtitle()` |
| Operator credit in the page footer, rendered as "Deployed by {value}". Unset = the footer omits the line entirely (the product name + build on the left always render) | `AGNES_INSTANCE_COPYRIGHT` | `instance.copyright` | `""` (no attribution) | `get_instance_copyright()` |
| The organization's own privacy policy URL. When set, the public, unauthenticated `/privacy` route redirects there instead of rendering the built-in page — Agnes is self-hosted, so the operator running the instance is the data controller, not the vendor | `AGNES_PRIVACY_POLICY_URL` | `instance.privacy_policy_url` | unset (built-in page renders) | `get_privacy_policy_url()` |
| Product brand string (hero copy, CTAs, setup script) | `AGNES_INSTANCE_BRAND` | `instance.brand` | `Agnes` | `get_instance_brand()` |
| Short brand for mid-sentence body copy; when it differs from the full brand, the `/home` hero appends "Call me {short}." | `AGNES_INSTANCE_BRAND_SHORT` | `instance.brand_short` | derived (= `instance.brand`) | `get_instance_brand_short()` |
| Inline `<svg>` logo for the header brand slot | `AGNES_INSTANCE_LOGO_SVG` | `instance.logo_svg` | `""` (text brand) | `get_instance_logo_svg()` |
| Favicon href (`<link rel="icon">`). A `data:` URI or absolute URL is used as-is; anything else is resolved as a static asset under `app/web/static/` (same cache-busting `static_url()` every other asset gets) | `AGNES_INSTANCE_FAVICON` | `instance.favicon` | `img/agnes-orb.png` (resolved via `static_url()`) | `get_instance_favicon()` |
| Experience preset — retired as a choice (Wave 0, 2026-08): `redesign` is the only valid value and the default; the old `classic` value (or any other unrecognised string) falls back to `redesign`, with a one-time startup warning naming the ignored setting. Historically flipped the DEFAULTS of `instance.theme` and `features.stack_auto_membership`; per-knob settings still win | `AGNES_INSTANCE_EXPERIENCE` | `instance.experience` | `redesign` | `get_experience()` |
| UI theme/palette (`blue`/`navy`/`dark`/`auto`/`paper`) — still a live, independent axis; an explicit choice always wins | `AGNES_INSTANCE_THEME` | `instance.theme` | `paper` (explicit `blue`/`navy`/`dark`/`auto` still wins) | `get_instance_theme()` |
| Chrome layout — retired (Wave 0, 2026-08): the rail chrome (fixed left sidebar) is the only chrome; `topnav` no longer exists. A configured value is tolerated but inert — ignored with a one-time startup warning | `AGNES_UI_LAYOUT` (ignored) | `instance.ui_layout` (ignored) | `rail` (always) | `get_ui_layout()` |
| Stack membership mode (auto-membership vs. the classic subscribe model); an explicit `false` still wins over the default | `AGNES_STACK_AUTO_MEMBERSHIP` | `features.stack_auto_membership` | `true` (explicit `false` still wins) | `get_stack_auto_membership()` |
| Analyst workspace folder name (`~/<name>`) | `AGNES_WORKSPACE_DIR_NAME` | `instance.workspace_dir` | derived from brand (non-alphanumerics stripped) | `get_workspace_dir_name()` |
| The one word an analyst types to open that workspace — what `agnes init` installs as a launcher and what the install guide tells them to type. Derived, not configurable: the folder name stripped to lowercase alphanumerics, plus an `ai` suffix when the word would shadow a shell built-in or the `agnes`/`claude` binaries. Shared with the CLI via `src/launcher_word.py` | — (derived) | — (derived) | derived from the workspace folder name | `get_workspace_launcher_word()` |
| Operator-injected HTML/JS blocks (analytics, widgets) | — | `instance.custom_scripts` | `[]` | `get_custom_scripts()` |
| Hide individual `/login` feature cards (keys: `data`, `marketplace`, `mcp`, `memory`, `anywhere`; list or comma-string) | `AGNES_INSTANCE_HIDE_LOGIN_FEATURES` | `instance.hide_login_features` | `""` (nothing hidden) | `get_hidden_login_features()` |
| Expose the authoring Studio (`/admin/studio*` incl. the admin moderation queue, plus the public suggestion API). `false` hides the nav/palette entries, redirects the routes home, and 403s the suggestion API | `AGNES_STUDIO_ENABLED` | `studio.enabled` | `true` | `get_studio_enabled()` |
| Expose agent profiles (`/agents` builder, `/api/v1/agents*` management + runtime API, `agnes agent`/`agnes chat` CLI). `false` hides the nav/palette entries, redirects `/agents` home, and 403s the API with `agent_profiles_disabled`; internal mechanisms (default-agent seeding, chat attribution, broker agent policy) keep running and data survives re-enabling. Deliberately not writable via the `/admin/server-config` editor — set the env var (or hand-edit the static `instance.yaml`) and restart | `AGNES_AGENT_PROFILES_ENABLED` | `agent_profiles.enabled` | `true` | `get_agent_profiles_enabled()` |
| Expose the user-facing MCP connector surface (`/me/ai-connector`, `/mcp-connect`, and the MCP tab of `/how-it-works#connect`, plus their nav/palette entries). `false` hides all of them — for a VPN/intranet-only instance whose cloud-side MCP clients can never reach the endpoint. UI only: the MCP protocol endpoints (`/api/mcp/http`, `/api/mcp/sse`) keep serving in-network clients regardless. See [`DEPLOYMENT.md`](DEPLOYMENT.md) | `AGNES_MCP_CONNECTOR_UI_ENABLED` | `mcp.connector_ui_enabled` | `true` | `get_mcp_connector_ui_enabled()` |
| Colour/font override block. Recolors both the legacy `--*` variable family AND, for known keys (`primary`, `background`, `surface`, `border`, `text_primary`, `text_secondary`, `success`, `warning`, `error`), the matching design-system `--ds-*` token — so it rebrands the "paper"/"navy"/"dark" surfaces too, not just the pre-redesign chrome. See `THEME_CSS_VAR_MAP` in `app/instance_config.py` for the full key-by-key mapping | — | `theme` | `{}` | `get_theme()` / `get_theme_css_overrides()` |

### Onboarding & `/home`

| Knob | Env override | `instance.yaml` path | Default | Resolver |
|------|--------------|----------------------|---------|----------|
| Landing route after auth (`/home` vs `/dashboard`) | `AGNES_HOME_ROUTE` | `instance.home_route` | `/dashboard` | `get_home_route()` |
| Offer the org-verification axis on the store/marketplace (Verify / Request changes / Request verification + the Verified marker). Off means the whole vocabulary is hidden — publisher attribution still carries accountability — and every user-authored item stays at Community with no admin action able to move it. | `AGNES_STORE_VERIFICATION_ENABLED` | `store.verification_enabled` | `true` | `get_store_verification_enabled()` |
| Require an MCP source's `url` to be https to a public, resolvable address. Off by default, which is **not** unguarded: link-local/metadata, multicast and reserved addresses, and cleartext http to a public address, are refused either way. What the default permits is a source on an internal address — an organization's own tool server, a developer's localhost — because those are ordinary deployments. Turn on for instances that only ever talk to third-party MCP services; it makes an intranet source unconfigurable. | `AGNES_MCP_SOURCE_URL_STRICT` | `mcp.source_url_strict` | `false` | `get_mcp_source_url_strict()` |
| Enforce the DNS-free half of the MCP source url policy at the two credentialed forward seams too (#1216), not only when a source is configured. Off by default — an already-enabled legacy source keeps forwarding exactly as it does today. Before turning this on, review the `url_policy_verdict` column on the admin MCP source list for any `would_refuse` row and fix its url first — this switch converts each one into a refused call with no other warning. | `AGNES_MCP_SOURCE_URL_RUNTIME_ENFORCE` | `mcp.source_url_runtime_enforce` | `false` | `get_mcp_source_url_runtime_enforce()` |
| Show the "turn on auto-accept mode" install block | `AGNES_HOME_SHOW_AUTOMODE` | `instance.home.show_automode` | `true` | `get_home_automode_visibility()` |
| Show the homepage status frame (sync/sessions/tokens) | `AGNES_HOME_SHOW_STATUS_FRAME` | `instance.home.show_status_frame` | `true` | `get_home_status_frame_visibility()` |
| Operator-authored Overview HTML on `/home` | `AGNES_INSTANCE_OVERVIEW` | `instance.overview` | `""` (hidden) | `get_instance_overview()` |
| Operator-authored Support HTML on `/home` | `AGNES_INSTANCE_SUPPORT` | `instance.support` | `""` (hidden) | `get_instance_support()` |
| Operator-authored preamble injected at the TOP of the `agnes init` install prompt (above `Set up the … CLI`). Empty/unset emits zero lines (default prompt byte-identical). `{instance_brand}` and the other server-side placeholders are substituted, but it must NOT contain a literal `{server_url}` (resolves at click time, not in the preamble) and must NOT reference `{token}` at all (no longer a prompt placeholder — the token is handed off via /home step 4 into `~/.agnes/token`). | `AGNES_INSTANCE_CUSTOM_PREAMBLE` | `instance.custom_preamble` | `""` (no extra lines) | `get_instance_custom_preamble()` |
| Admin contact address for user-side "email admin" prompts | `AGNES_INSTANCE_ADMIN_EMAIL` | `instance.admin_email` | `""` | `get_instance_admin_email()` |
| Infrastructure/provisioning repo URL (used by operator plugin to name the concrete infra repo for this instance; empty = vendor-neutral OSS default) | `AGNES_INFRA_REPO_URL` | `instance.infra_repo_url` | `""` (unset) | `get_infra_repo_url()` |
| Refresh-cadence string shown in the welcome prompt | — | `instance.sync_interval` | `1 hour` | `get_sync_interval()` |

### Connector pre-provisioning

| Knob | Env override | `instance.yaml` path | Default | Resolver |
|------|--------------|----------------------|---------|----------|
| Shared Google Workspace CLI OAuth client (id/secret/project/insecure-transport) | `AGNES_GWS_CLIENT_ID`, `AGNES_GWS_CLIENT_SECRET`, `AGNES_GWS_PROJECT_ID` (legacy — ignored by the bundled `connector-gws` seed; forces a per-analyst `serviceUsageConsumer` IAM grant when consumed), `AGNES_GWS_OAUTHLIB_INSECURE_TRANSPORT` | `instance.gws.{client_id,client_secret,project_id,oauthlib_insecure_transport}` | unset / `1` | `get_gws_oauth_credentials()` |
| Atlassian Cloud site URL baked into the connector prompt | `AGNES_ATLASSIAN_BASE_URL` | `instance.atlassian.base_url` | `""` (ask user) | `get_atlassian_base_url()` |

### Data source, auth & structural sections

| Knob | Env override | `instance.yaml` path | Default | Resolver |
|------|--------------|----------------------|---------|----------|
| Data source type (`keboola`/`bigquery`/`local`) — the one knob in this table where the overlay wins over env, not the reverse (D1 residual, 2026-08) | `DATA_SOURCE` (fallback only, consulted when `data_source.type` is unset) | `data_source.type` | `local` | `get_data_source_type()` |
| Public base URL (used by Slack bot to mint **absolute** `/slack/bind` magic-link + `/chat` deep links — request-less code paths can't synthesize a base URL otherwise) | `PUBLIC_URL` | `server.public_url` | unset (links degrade to root-relative) | `get_public_url()` |
| Inbound Slack transport (`http`/`socket`) | `SLACK_TRANSPORT` | `chat.slack.transport` | `http` | `get_slack_transport()` |
| Chat LLM platform (`anthropic`/`vertex`) — vertex runs Claude through Google Vertex AI with Google ADC signing at the broker; see [`cloud-chat.md`](cloud-chat.md#llm-provider-google-vertex-ai) | — | `chat.llm.provider` | `anthropic` | `load_chat_config()` |
| Vertex GCP project for chat (required when `chat.llm.provider: vertex`; pinned server-side by the broker) | — | `chat.llm.vertex.project_id` | unset | `load_chat_config()` |
| Vertex region for chat (`global` or a specific region; required when `chat.llm.provider: vertex`) | — | `chat.llm.vertex.region` | unset | `load_chat_config()` |
| Allowed login email domains | — | `auth.allowed_domain` | `[]` | `get_allowed_domains()` |
| Full auth block | — | `auth` | `{}` | `get_auth_config()` |
| SSRF allowlist — hostnames exempt from the private/reserved-network guard on **all** admin URLs routed through the shared validator (marketplace + initial-workspace clone URLs, Keboola `stack_url`, server-config URL fields), not just clone URLs; use for an internal git host on a private network (e.g. on-prem GitHub Enterprise). List or comma-string. Empty = guard fail-closed. | `AGNES_SSRF_ALLOWED_HOSTS` | `security.ssrf_allowed_hosts` | `""` (fail-closed) | `get_ssrf_allowed_hosts()` |
| Dataset registry | — | `datasets` | `{}` | `get_datasets()` |
| Corporate Memory block | — | `corporate_memory` | `{}` | `get_corporate_memory_config()` |
| Hosted data apps block (`enabled`, `runtime_image`, `subdomain_base`, `default_idle_timeout_s`, `default_sleep_mode`, `default_mem_limit`, `default_cpus`, `max_apps_per_user`, `container_pids_limit`, `container_read_only`) — see [`DEPLOYMENT.md`](DEPLOYMENT.md#data-apps) | — | `data_apps` | `{}` (feature off) | `get_data_apps_config()` |

### Connection ownership (`source_connections` vs `instance.yaml`)

Each of `keboola`/`bigquery`/`snowflake`/`databricks` has a row in the
`source_connections` table (`/api/admin/source-connections`, `agnes admin
connection …`) that is the live source of truth, resolved fresh on every
call — the `data_source.{bigquery,snowflake,databricks}` blocks above are
only consulted as a fallback on an un-migrated instance with no row yet.
`app/connections_seed.py` seeds one such row per type from whatever
`instance.yaml` / env vars are already configured on first boot — a
one-time copy, not a live sync — after which further edits to
`instance.yaml` for a *seeded* type log a deprecation warning and are
otherwise ignored (the row wins). The "Add data source" wizard's Snowflake/
Databricks panes write the row directly, so a saved connection is visible to
every process on the very next call — no restart. See
[`DATA_SOURCES.md`](DATA_SOURCES.md#connection-ownership-source_connections-vs-instanceyaml)
for the per-source table.

### Flea-market upload guardrails

See [`STORE_GUARDRAILS.md`](STORE_GUARDRAILS.md) for the pipeline these tune.

| Knob | `instance.yaml` path | Default | Resolver |
|------|----------------------|---------|----------|
| Guardrail block | `guardrails` | `{}` | `get_guardrails_config()` |
| Pipeline enabled (operator intent) | `guardrails.enabled` | `true` | `get_guardrails_enabled()` |
| LLM review model tier | `guardrails.review_model` | `haiku` | `get_guardrails_review_model()` |
| Per-submitter blocked-row quota / day | `guardrails.blocked_quota_per_day` | `50` | `get_guardrails_blocked_quota_per_day()` |
| Blocked-bundle byte TTL (days) | `guardrails.blocked_bundle_ttl_days` | `30` | `get_guardrails_blocked_bundle_ttl_days()` |
| Stuck-review reaper grace (seconds) | `guardrails.stuck_review_grace_seconds` | `1800` | `get_guardrails_stuck_review_grace_seconds()` |
| Min description chars | `guardrails.min_description_chars` | `60` | `get_guardrails_min_description_chars()` |
| Min slash-command description chars | `guardrails.min_command_description_chars` | `25` | `get_guardrails_min_command_description_chars()` |
| Min distinct words in a description | `guardrails.min_distinct_words` | `5` | `get_guardrails_min_distinct_words()` |
| Min skill/agent body chars | `guardrails.min_body_chars` | `200` | `get_guardrails_min_body_chars()` |
| Skill-lint bloat threshold (chars) | `guardrails.lint_max_body_chars` | `8000` | `get_lint_max_body_chars()` |
| Skill-lint duplicate candidate count | `guardrails.lint_duplicate_top_n` | `5` | `get_lint_duplicate_top_n()` |
| Skill-lint audit min interval (hours) | `guardrails.lint_audit_min_interval_hours` | `144` | `get_lint_audit_min_interval_hours()` |

### Audit trail

See [`observability.md`](observability.md) for the full audit/activity-trail
inventory and which trails have a retention policy.

`audit_log` keeps its own knob and its own daily job (`audit-prune`). The
other trails below are pruned by the daily `retention-prune` sweep
(`POST /api/admin/run-retention-prune`) and all default to `0` — **keep
forever**, so the sweep deletes nothing until an operator opts a trail in.
Pruning a trail never touches the live state beside it: `sync_history` is
pruned while `sync_state` (what the manifest and `agnes pull` read) is not,
and `agent_scope_snapshots` is pruned while the `agents` rows are not.

| Knob | `instance.yaml` path | Default | Resolver |
|------|----------------------|---------|----------|
| `audit_log` retention (days, `0` = keep forever) | `audit.retention_days` | `365` | `get_audit_retention_days()` |
| `sync_history` retention (days, `0` = keep forever) | `retention.sync_history_days` | `0` | `get_sync_history_retention_days()` |
| `llm_usage` retention (days, `0` = keep forever) | `retention.llm_usage_days` | `0` | `get_llm_usage_retention_days()` |
| `agent_scope_snapshots` retention (days, `0` = keep forever) | `retention.agent_scope_snapshots_days` | `0` | `get_agent_scope_snapshots_retention_days()` |
| `usage_events` retention (days, `0` = keep forever) — `USAGE_EVENTS_RETENTION_DAYS` env var wins when set; pruned by its own `POST /api/admin/usage/prune` job, not the sweep | `retention.usage_events_days` | `0` | `get_usage_events_retention_days()` |

---

## Annotated `instance.yaml` examples

The main configuration file lives at `config/instance.yaml`. See
`config/instance.yaml.example` for the full annotated template.

### Instance branding

```yaml
instance:
  name: "AI Harness"        # UI title, email subjects (get_instance_name)
  subtitle: "Acme Corp"          # Header subtitle (get_instance_subtitle)
  copyright: "Acme Corp"         # Footer credit, "Deployed by …" (get_instance_copyright)
  brand: "Acme Analyst"          # Product brand string (get_instance_brand)
  brand_short: "Acme"            # Short brand for body copy (get_instance_brand_short)
  favicon: "img/my-icon.png"     # Favicon href — static path, data: URI, or absolute URL (get_instance_favicon)
  theme: "blue"                  # UI palette (get_instance_theme); default is "paper" since Wave 0 (2026-08); "blue" opts out explicitly
  # ui_layout is retired (Wave 0, 2026-08) — rail is the only chrome; a configured value is ignored
  home_route: "/home"            # Landing after auth (get_home_route)
```

### Authentication

```yaml
auth:
  allowed_domain: "acme.com"     # Email domain restriction for login
```

Only emails from this domain can log in via Google OAuth or email magic link
(when offered — see `auth.providers` in `config/instance.yaml.example`).
Google OAuth is optional — if not configured, only password sign-in is
available by default; the email magic link is opt-in only
(`auth.providers: [..., email]`), since its single-use verify link can be
silently burned by a corporate mail scanner before the human clicks.

### Email

```yaml
email:
  from_address: "noreply@acme.com"
  smtp_host: "${SMTP_HOST}"
  smtp_port: 587
  smtp_user: "${SMTP_USER}"
  smtp_password: "${SMTP_PASSWORD}"
```

Used for magic link authentication. Without SMTP configured, magic links are
shown directly in the browser (development mode). Compatible with any SMTP relay
(Gmail, Mailgun, SendGrid SMTP, etc.).

SMTP is the **only** mail transport. Providers with an HTTP API are used
through their SMTP relay — e.g. for SendGrid set `SMTP_HOST=smtp.sendgrid.net`
with your API key as `SMTP_PASSWORD` (user `apikey`). The former SendGrid SDK
integration (`SENDGRID_API_KEY`) was removed: the SDK was never installed, so
that path only ever failed, and the key no longer counts as a configured mail
transport. The sender address comes from `SMTP_FROM` (the legacy
`EMAIL_FROM_ADDRESS` is still honored as a fallback).

### Server

```yaml
server:
  host: "10.0.0.1"              # Server IP
  hostname: "data.acme.com"     # Server DNS name
```

### Data Source

```yaml
data_source:
  type: "keboola"               # keboola, bigquery, local (get_data_source_type)
```

### Users

```yaml
users:
  admin@acme.com:
    display_name: "John Doe"
    km_admin: true              # Corporate Memory admin (optional)
```

### Datasets

```yaml
datasets:
  jira:
    label: "Jira Tickets"
    description: "Support tickets"
    size_hint: "~50 MB"
    requires: null
  jira_attachments:
    label: "Jira Attachments"
    description: "File attachments"
    size_hint: "~500 MB+"
    requires: "jira"
```

### Catalog

```yaml
catalog:
  categories:
    sales:
      label: "Sales"
      icon: "sales"
    hr:
      label: "HR"
      icon: "hr"
  order: ["sales", "hr"]
```

---

## .env infrastructure variables

These are deployment secrets and infrastructure paths — distinct from the
per-instance knobs above. Copy `config/.env.template` to `.env` and fill in
values. Never commit `.env`.

### Required

| Variable | Description |
|----------|-------------|
| `JWT_SECRET_KEY` | FastAPI JWT token secret (generate with `secrets.token_hex(32)`) |
| `SESSION_SECRET` | Session cookie secret (generate with `secrets.token_hex(32)`) |
| `GOOGLE_CLIENT_ID` | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Google OAuth client secret |

### Data Source (Keboola)

| Variable | Description |
|----------|-------------|
| `KEBOOLA_STORAGE_TOKEN` | Keboola Storage API token |
| `KEBOOLA_STACK_URL` | Keboola stack URL |
| `DATA_DIR` | Data directory path (default: `/data` in Docker, `./data` locally) |

### Data Source (BigQuery)

| Variable | Description |
|----------|-------------|
| `BIGQUERY_PROJECT` | GCP project for job execution/billing |
| `BIGQUERY_LOCATION` | BigQuery location (e.g., `US`, `us-central1`) |

### Optional

| Variable | Description |
|----------|-------------|
| `MICROSOFT_TENANT_ID` | Microsoft Entra ID directory (tenant) ID — a GUID or a verified domain. Multi-tenant endpoints (`common` / `organizations` / `consumers`) are refused; see [`auth-microsoft-oauth.md`](auth-microsoft-oauth.md) |
| `MICROSOFT_CLIENT_ID` | Microsoft Entra ID application (client) ID |
| `MICROSOFT_CLIENT_SECRET` | Microsoft Entra ID client secret value. All three are required for Microsoft sign-in and are read at process start |
| `SMTP_HOST` | SMTP relay host for magic link / password-reset / invite emails. The only mail transport — for SendGrid use `smtp.sendgrid.net` |
| `SMTP_PORT` | SMTP port (587 for STARTTLS, 465 for SSL) |
| `SMTP_USER` | SMTP username |
| `SMTP_PASSWORD` | SMTP password |
| `SMTP_FROM` | Sender address for outgoing auth mail (default `noreply@example.com`). Legacy `EMAIL_FROM_ADDRESS` is honored as a fallback |
| `TELEGRAM_BOT_TOKEN` | For Telegram notifications |
| `ANTHROPIC_API_KEY` | For Corporate Memory AI extraction AND `agnes admin ask` (LLM text-to-SQL on telemetry). Without this, both features show a clear 503 error and skip silently. Not needed when the instance runs LLM calls through Vertex (`ai.provider: vertex` / `chat.llm.provider: vertex`). |
| `LLM_API_KEY` | API key for LLM proxy (LiteLLM, OpenRouter, etc.) |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to a GCP service-account JSON — one way to provide Application Default Credentials for the Vertex LLM provider (the others: gcloud user ADC, or a GCE/GKE attached service account) |
| `ANTHROPIC_VERTEX_PROJECT_ID` | GCP project hosting Claude on Vertex AI — env fallback for `ai.vertex.project_id` (server-side utility calls). Chat uses `chat.llm.vertex.project_id` in `instance.yaml` |
| `CLOUD_ML_REGION` | Vertex AI region (`global`, `us-east5`, `europe-west1`, …) — env fallback for `ai.vertex.region`; defaults to `global` |
| `JIRA_DOMAIN` | Jira Cloud site domain (e.g. `acme.atlassian.net`) |
| `JIRA_EMAIL` | Jira account email paired with `JIRA_API_TOKEN` |
| `JIRA_WEBHOOK_SECRET` | For Jira webhook integration |
| `JIRA_API_TOKEN` | For Jira REST API access |
| `JIRA_REFRESH_FIELDS` | Custom fields to refresh onto tickets — `field_id` or `field_id:column`, comma-separated. Discover with `python -m connectors.jira.scripts.verify_sla_access --list-fields` |
| `JIRA_CLOUD_ID` | Only for a scoped API token (gateway URL) |
| `DESKTOP_JWT_SECRET` | HS256 secret the notifications WebSocket (`/api/notifications/ws`) validates client tokens against. Unset = every connection fails auth (fail-closed). No in-repo flow mints these tokens yet — see issue #412. |
| `CONFIG_DIR` | Override config directory path |
| `LOG_LEVEL` | Logging level: `debug`, `info`, `warning`, `error` |
| `DOMAIN` | Public hostname for Caddy TLS (production profile) |
| `AGNES_BASE_URL` | Operator-pinned public origin (see below). Wins over `SERVER_URL`. |
| `SERVER_URL` | Deployment's public URL (see below). |
| `AGNES_INTERNAL_URL` | Data-rails-only server URL for the chat sandbox + workspace seed (see below). |

### Public origin & data-rails URLs

Three URL variables with distinct jobs:

- **`AGNES_BASE_URL`**, then **`SERVER_URL`** — the *public origin* pin
  (`app/auth/public_url.py`). First non-empty wins; feeds MCP OAuth issuer +
  discovery metadata, connector/Cowork bundles, and external links. When both
  are unset, the origin is derived per-request from the incoming host
  (proxy-aware), so most TLS-proxied deployments don't need either.
- **`SERVER_URL`**, then **`AGNES_INTERNAL_URL`** — the *data rails* chain
  (`agnes_server_url()` in `app/chat/manager.py`): the URL the cloud-chat
  sandbox (`AGNES_SERVER` for the agnes CLI) and the seeded analyst workspace
  use to reach the server. Falls back to loopback for local dev.

> **Plain-HTTP deployments** (`tls_mode=none`, no TLS proxy): an `http://`
> non-localhost URL cannot serve as an MCP OAuth issuer (RFC 8414 requires
> HTTPS), so setting `SERVER_URL`/`AGNES_BASE_URL` to one disables the
> streamable MCP connector at `/api/mcp/http` — the app boots and logs a loud
> ERROR, everything else keeps working. To get cloud-chat data rails without
> touching the public origin (and without the ERROR), set `AGNES_INTERNAL_URL`
> instead — it feeds *only* the rails chain, never OAuth metadata.

---

## Related docs

- [`initial-workspace-override.md`](initial-workspace-override.md) — analyst
  workspace payload + init prompt (the IWT tier).
- [`seed-repo-contract.md`](seed-repo-contract.md) — seed-repo layout +
  install-prompt placeholders.
- [`STORE_GUARDRAILS.md`](STORE_GUARDRAILS.md) — flea-market guardrail pipeline.
- `infra/modules/customer-instance/variables.tf` — Terraform knobs the upstream
  module exposes (incl. `home_route`).
