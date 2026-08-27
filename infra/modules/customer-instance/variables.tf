variable "gcp_project_id" {
  description = "GCP project ID where the instance will be deployed."
  type        = string
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "europe-west1"
}

variable "zone" {
  description = "GCP zone"
  type        = string
  default     = "europe-west1-b"
}

variable "customer_name" {
  description = "Short customer identifier (e.g. acme, example). Used as a prefix for created resources."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.customer_name))
    error_message = "customer_name must be lowercase, start with a letter, 2-21 chars."
  }
}

variable "prod_instance" {
  description = <<-EOT
    Production VM configuration.

    `image_tag` MUST point to an image that contains `/opt/agnes-host/`
    (this directory was added in v0.26.0). Older tags will fail at first
    boot with `docker cp: No such file or directory` because the startup
    script extracts host artifacts from the image instead of curling
    them. Existing VMs are unaffected by this constraint — the module
    sets `lifecycle { ignore_changes = [metadata_startup_script] }` so
    the new script only runs on freshly-created VMs.
  EOT
  type = object({
    name         = string
    machine_type = optional(string, "e2-small")
    disk_size_gb = optional(number, 30)
    data_disk_gb = optional(number, 50)
    image_tag    = optional(string, "stable")
    upgrade_mode = optional(string, "auto")
    # Standard 5-field cron expression consumed by startup-script.sh.tpl's
    # crontab install line. Default matches the historical fixed cadence —
    # override to reduce upgrade-triggered blips on a customer-facing
    # instance (e.g. a nightly window) while dev/staging instances stay on fast
    # iteration.
    upgrade_schedule = optional(string, "*/5 * * * *")
    tls_mode         = optional(string, "caddy")
    domain           = optional(string, "")
    # Hostname being migrated AWAY from. When set, Caddy serves it alongside
    # `domain` and 308s every request onto `domain` (see the Caddyfile's
    # second site block), so old bookmarks / `agnes` CLI configs / MCP
    # connector URLs keep resolving through a domain cutover instead of
    # failing the TLS handshake. Clear it once the old DNS record is retired.
    domain_alias = optional(string, "")
    # Chrome the web UI renders in. Per-VM (not module-wide) for the same
    # reason as dispatcher_enabled / data_apps_enabled below: a look is
    # rolled out dev-first, previewed on a dev VM, and promoted to prod only
    # once it looks right. First-boot seed only (D1, 2026-08): a non-empty
    # value is written into instance.yaml's `instance.theme` the FIRST time
    # this VM boots (never on a later apply/recreate) — the admin UI
    # (`/admin/server-config`) owns it from day 2 onward. Empty (the default)
    # seeds nothing, so the instance keeps whatever `instance.theme` already
    # says in instance.yaml — or the app's own default when that is unset too.
    #   theme: "" | "blue" | "navy" | "dark" | "auto" | "paper" (app default)
    #
    # ui_layout is RETIRED (app >= the Wave 0 legacy retirement, 2026-08): the
    # rail is the only chrome the app can render, so there is nothing left to
    # choose. The field stays DECLARED — and rejects the retired "topnav" in
    # the validation below — on purpose. Terraform silently DISCARDS object
    # attributes a type constraint does not declare, so deleting it would let
    # a root that still sets `ui_layout = "topnav"` apply cleanly and get the
    # rail with nothing anywhere saying why: exactly the silent-fallback
    # hazard every validation in this file exists to catch. Declared, it
    # becomes a plan-time error that names the retirement. Nothing is plumbed
    # to the VM from it any more — drop the line from your root when convenient.
    ui_layout = optional(string, "")
    theme     = optional(string, "")
    # Experience preset (app >= 0.83.1). `redesign` flips the app-side
    # DEFAULTS of the coupled knobs (theme -> paper, features.
    # stack_auto_membership -> on); any per-knob setting — the `theme` field
    # above, or instance.yaml — still wins, so don't set both this and
    # `theme` unless you mean to pin a divergence. Chrome layout is NO LONGER
    # part of the coupling: the rail is unconditional.
    # First-boot seed only (D1, 2026-08): a non-empty value is written into
    # instance.yaml's `instance.experience` the FIRST time this VM boots
    # (never on a later apply/recreate) — the admin UI
    # (`/admin/server-config`) owns it from day 2 onward. Empty (the default)
    # seeds nothing, so the instance keeps whatever `instance.experience`
    # already says in instance.yaml, or the app's `redesign` default.
    #   experience: "" | "redesign" (app default)
    # `classic` is RETIRED and rejected below — same reasoning as ui_layout.
    experience = optional(string, "")
    # Container memory caps written to /opt/agnes/.env and read by
    # docker-compose.yml (mem_limit: $${AGNES_APP_MEM_LIMIT:-4g}). Defaults
    # match the compose defaults; raise on a larger VM together with the
    # app's per-connection DuckDB budgets (DuckDB sizes a fresh connection
    # to ~80% of the cgroup limit, so an under-sized cap OOM-kills uvicorn
    # mid-WAL-write).
    app_mem_limit       = optional(string, "4g")
    scheduler_mem_limit = optional(string, "2g")
    # Container CPU caps written to /opt/agnes/.env and read by
    # docker-compose.yml (cpus: $${AGNES_APP_CPUS:-2.0}). Raise app_cpus on
    # hosts with more cores (e.g. "3.0" on a 4-core VM) for headroom under
    # concurrent load; keep app_cpus + scheduler_cpus <= host cores.
    app_cpus       = optional(string, "2.0")
    scheduler_cpus = optional(string, "1.0")
    # Opt-in LLM dispatcher (token-arbitrage PoC) on this VM. Per-VM so a
    # dev-first rollout doesn't touch prod. Requires the module-level
    # dispatcher_* variables to be set — see their docs.
    dispatcher_enabled = optional(bool, false)
    # Opt-in hosted data apps on this VM. Per-VM (like dispatcher_enabled) so a
    # dev-first rollout doesn't touch prod. Brings up the apps-runner sidecar +
    # the AGNES_DATA_APPS_ENABLED env override on that VM's .env only.
    data_apps_enabled = optional(bool, false)
    # Serve each hosted app from its OWN origin, `<slug>.<base>`, instead of
    # `<domain>/apps/<slug>/` (app >= the origin-isolation release). The app
    # REFUSES main-origin serving by default, so a VM with data apps enabled
    # and this unset serves no app at all — that is the intended posture, not
    # a regression: main-origin serving hands an app's user-authored JS the
    # viewer's own session (docs/architecture.md#hosted-data-apps).
    #
    # Setting this requires BOTH, or apps stay unreachable:
    #   - a wildcard DNS record `*.<base>` pointed at this VM, and
    #   - TLS covering those names on the terminating proxy.
    #
    # SECURITY — the value widens the session cookie to the base's PARENT
    # domain (the app's `session_cookie_domain()`), so one login also covers
    # the app subdomains:
    #   "apps.agnes.example.com" -> cookie Domain=.agnes.example.com  (good)
    #   "apps.example.com"       -> cookie Domain=.example.com        (BAD —
    #      the session cookie then rides to every unrelated host under it)
    # Use `apps.<this VM's domain>`, never `apps.<registrable domain>`.
    data_apps_subdomain_base = optional(string, "")
    # Opt-in embedded kai-agent turn engine on this VM (app >= the /api/kai
    # host wiring, app/api/kai.py). Per-VM (like dispatcher_enabled) so a
    # dev-first rollout doesn't touch prod. Brings up the engine + its own
    # Postgres as extra compose services AND writes KAI_HOST_JWT_SECRET into
    # that VM's app .env, enabling the /api/kai/* host surface — both halves
    # of the shared-secret pair come from one Secret Manager secret, so they
    # cannot drift. Requires the module-level kai_agent_* variables.
    kai_agent_enabled = optional(bool, false)
    # Engine container resource ceilings, written to /opt/agnes/.env like
    # app_mem_limit above — per-VM TF fields and not .env hand-edits, because
    # the startup script rewrites .env from scratch on every boot and a
    # hand-raised ceiling would silently drop back to the default. Heavy work
    # happens in the remote E2B sandbox, so the engine itself stays small.
    kai_agent_mem_limit    = optional(string, "2g")
    kai_agent_cpus         = optional(string, "1.0")
    kai_agent_pg_mem_limit = optional(string, "1g")
    # Opt-in: let the engine's sandbox reach this instance's own MCP tool
    # surface. Sets both halves of the pair that only work together — the
    # app-side ticket-scope switch (KAI_BROKER_MCP_ENABLED=true in the app
    # .env) and the engine-side broker URL (HOST_BROKER_MCP_URL derived from
    # the VM's own public origin, the same SERVER_URL the LLM broker line
    # uses, since the E2B sandbox egresses to it from the public internet).
    # One flag rather than two knobs because either half alone is a silent
    # failure: URL without the scope answers 503 kai_mcp_not_enabled on every
    # tool call (`_require_mcp_surface` is declared ahead of the ticket
    # dependency so it decides before any credential is inspected), scope
    # without the URL simply never registers the tool server. Inert unless
    # kai_agent_enabled is also true on this VM.
    kai_agent_broker_mcp_enabled = optional(bool, false)
    # Web-chat provider pin, written as AGNES_CHAT_PROVIDER into the app .env
    # (app >= 0.85: env > instance.yaml > default; "kai-agent" since 0.88).
    # Codifies which engine runs
    # /chat sessions IN TERRAFORM instead of a hand-edited instance.yaml on
    # the data disk — the overlay survives reboots and recreates, but not a
    # fresh data disk, and it is invisible in review. Empty (the default)
    # writes NO env line, so the instance keeps whatever instance.yaml says.
    # "kai-agent" requires kai_agent_enabled on the same VM (validated below):
    # pinning web chat onto an engine this VM does not run refuses every
    # session at boot.
    # "docker" is self-contained: the startup script mints APPS_RUNNER_TOKEN +
    # DOCKER_GID, activates the `apps` compose profile for the sidecar that
    # creates the sandboxes, and builds the sandbox image from the app image's
    # own build context. It does NOT turn on hosted data apps — that stays
    # data_apps_enabled, even though both features share the sidecar.
    chat_provider = optional(string, "")

    # --- Vendor-neutral per-instance branding (all OPTIONAL) ---
    # Written into the VM's /data/state/instance.yaml on FIRST boot only. The
    # startup script never clobbers an existing instance.yaml (so an operator's
    # later theme edits and DB-backend migrations survive a reboot/recreate),
    # which means these values seed a FRESH instance — to restyle a LIVE one,
    # edit instance.yaml through the app/admin surface or recreate the VM.
    # Leaving every field below unset writes a byte-for-byte identical
    # instance.yaml — the branding blocks are emitted only for the keys set.
    #
    # The app reads these back from /data/state/instance.yaml
    # (app/instance_config.py); config/instance.yaml.example documents the full
    # contract. Keep values generic here — nothing customer-specific belongs in
    # a module default or example (use example.com / <your-brand> placeholders).
    #
    #   logo_svg    -> instance.logo_svg   (inline <svg> for header + /login brand slot)
    #   brand       -> instance.brand      (product name in analyst-facing copy)
    #   brand_short -> instance.brand_short (short form used mid-sentence)
    #   subtitle    -> instance.subtitle   (tagline shown under the instance name)
    #   copyright   -> instance.copyright  (footer credit, rendered "Deployed by <this>")
    #   favicon     -> instance.favicon    (favicon href — static path, data: URI, or
    #                                       absolute URL; see get_instance_favicon())
    logo_svg    = optional(string, "")
    brand       = optional(string, "")
    brand_short = optional(string, "")
    subtitle    = optional(string, "")
    copyright   = optional(string, "")
    favicon     = optional(string, "")

    # theme_colors -> the top-level `theme:` block in instance.yaml. Known color
    # keys recolor the design-system --ds-* tokens (primary -> --ds-primary,
    # background -> --ds-bg, surface -> --ds-surface, border -> --ds-border,
    # text_primary / text_secondary -> --ds-text-primary / --ds-text-secondary,
    # success / warning / error -> --ds-accent-{success,warn,danger}-ink); see
    # THEME_CSS_VAR_MAP in app/instance_config.py. font_primary / radius stay
    # legacy-only (no single --ds-* equivalent) and font_url is a stylesheet URL,
    # not a CSS variable. Values are free-form CSS (hex, rgba(), font stacks) —
    # only the keys you set are written. This generic recolor is independent of
    # the named `theme` preset above and composes with it: a preset picks the
    # base palette, theme_colors overrides individual tokens on top.
    theme_colors = optional(object({
      primary        = optional(string)
      primary_dark   = optional(string)
      primary_light  = optional(string)
      background     = optional(string)
      surface        = optional(string)
      border         = optional(string)
      text_primary   = optional(string)
      text_secondary = optional(string)
      success        = optional(string)
      warning        = optional(string)
      error          = optional(string)
      font_primary   = optional(string)
      font_url       = optional(string)
      radius         = optional(string)
    }), {})

    # custom_scripts -> instance.custom_scripts, written verbatim. Each entry is
    # an operator-injected HTML/JS block (feedback widget, analytics, error
    # capture — or the hero/accent CSS the theme_colors block can't express)
    # rendered into every page. Admin-authored, emitted with `| safe`, so this
    # is trusted operator content by contract. Empty list (default) writes
    # nothing.  placement: head_start | head_end | body_end
    custom_scripts = optional(list(object({
      name      = string
      enabled   = bool
      placement = string
      html      = string
    })), [])
  })

  # An alias equal to `domain` produces two Caddy site blocks with the same
  # address, which Caddy refuses to parse — so the next recreate or reload
  # takes the PRIMARY site down too, not just the alias. Rejecting it at plan
  # time turns an outage into an error the operator reads before applying
  # (Devin Review on #1182).
  validation {
    condition     = var.prod_instance.domain_alias == "" || var.prod_instance.domain_alias != var.prod_instance.domain
    error_message = "prod_instance.domain_alias must differ from prod_instance.domain; two site blocks sharing one address stop Caddy from starting."
  }

  # The app resolves an unrecognised layout/theme by SILENTLY falling back to
  # its default (see get_ui_layout / get_instance_theme). So a typo here
  # applies cleanly, reboots cleanly, and simply renders the old chrome with
  # nothing anywhere saying why. Catch it at plan time instead.
  #
  # "topnav" is REJECTED rather than merely ignored: the chrome it names no
  # longer exists in the app, so a root asking for it is stating an intent
  # that cannot be honored, and applying silently would hand back the rail.
  validation {
    condition     = contains(["", "rail"], var.prod_instance.ui_layout)
    error_message = "prod_instance.ui_layout must be \"\" or \"rail\". The \"topnav\" chrome was retired (Wave 0, 2026-08) and the rail is now unconditional — remove the line."
  }

  validation {
    condition     = contains(["", "blue", "navy", "dark", "auto", "paper"], var.prod_instance.theme)
    error_message = "prod_instance.theme must be \"\", \"blue\", \"navy\", \"dark\", \"auto\" or \"paper\"."
  }

  # Same silent-fallback hazard: the app resolves an unrecognised preset
  # value to `redesign` without a word (see get_experience / the `experience`
  # entry in app/switches.py), so a typo here would look applied and do
  # nothing. Catch it at plan time. `classic` is retired for the same reason
  # `topnav` is above — it names a behavior the app no longer has.
  # The app resolves an unknown chat.provider by REFUSING the ChatManager at
  # boot (app/main.py provider allowlist) — loud, but only at runtime on the
  # VM. Catch the typo (and the engine-less kai-agent pin, which would refuse
  # every session at mint time) at plan time instead.
  validation {
    condition     = contains(["", "docker", "kai-agent"], var.prod_instance.chat_provider)
    error_message = "prod_instance.chat_provider must be \"\", \"docker\" or \"kai-agent\". The \"e2b\" provider was removed in app 0.89.0."
  }

  validation {
    condition     = var.prod_instance.chat_provider != "kai-agent" || var.prod_instance.kai_agent_enabled
    error_message = "prod_instance.chat_provider = \"kai-agent\" requires kai_agent_enabled = true on the same VM — web chat pinned onto an engine the VM does not run refuses every session."
  }

  validation {
    condition     = contains(["", "redesign"], var.prod_instance.experience)
    error_message = "prod_instance.experience must be \"\" or \"redesign\". The \"classic\" experience was retired (Wave 0, 2026-08) — remove the line."
  }
}

variable "dev_instances" {
  description = <<-EOT
    List of dev VMs. Empty list = no dev VMs.

    tls_mode + domain are optional and default to plain HTTP on :8000. Set
    tls_mode = "caddy" + domain to enable Caddy + Let's Encrypt (or whatever
    CADDY_TLS env var is configured to in the Caddyfile — see Caddyfile docs).

    Same `image_tag >= v0.26.0` constraint as `prod_instance` — older tags
    lack `/opt/agnes-host/` and the startup `docker cp` fails-loud.
  EOT
  type = list(object({
    name         = string
    machine_type = optional(string, "e2-small")
    image_tag    = optional(string, "dev")
    tls_mode     = optional(string, "none")
    domain       = optional(string, "")
    # Legacy hostname to 308 onto `domain` during a domain migration. Same
    # semantics as prod_instance.domain_alias — see there. MUST be declared on
    # this object type: Terraform silently drops attributes absent from the
    # type, so a bare entry in a caller's list would never reach the module.
    domain_alias = optional(string, "")
    # Per-VM chrome — see prod_instance.ui_layout / .theme. Same "must be on
    # the type" rule: Terraform silently drops attributes absent from the
    # type, so a bare entry in a caller's list would never reach the module.
    ui_layout = optional(string, "")
    theme     = optional(string, "")
    # Experience preset — see prod_instance.experience (one-line redesign
    # adoption; per-knob settings win; reaches the VM on creation/recreate).
    experience = optional(string, "")
    # Role label used by per-VM OAuth secret naming
    # (var.oauth_secret_name_template `{role}` placeholder), VM tagging in
    # downstream cron/log filters, and dev_defaults selection. Defaults to
    # "dev" so existing callers don't have to set it; override per VM to
    # introduce `stage`, `perf`, etc. without any module-side code change
    # (matching Secret Manager entries — `*-stage` / `*-perf` — must exist
    # if the per-VM OAuth template uses {role}). MUST be declared on the
    # object type, not only in dev_defaults: Terraform silently drops
    # attributes that aren't in the object type during conversion, so a
    # caller-supplied `role = "stage"` would never reach the merge() below
    # if the type omits it.
    role = optional(string, "dev")
    # See prod_instance for the rationale; same defaults.
    app_mem_limit       = optional(string, "4g")
    scheduler_mem_limit = optional(string, "2g")
    app_cpus            = optional(string, "2.0")
    scheduler_cpus      = optional(string, "1.0")
    dispatcher_enabled  = optional(bool, false)
    # Per-VM hosted data apps — see prod_instance for the rationale.
    data_apps_enabled = optional(bool, false)
    # Per-VM app origin base — see prod_instance for the DNS/TLS prerequisites
    # and the session-cookie widening this value drives.
    data_apps_subdomain_base = optional(string, "")
    # Per-VM embedded kai-agent turn engine — see prod_instance for the
    # rationale. Same "must be on the type" rule as the fields above.
    kai_agent_enabled = optional(bool, false)
    # Engine resource ceilings — see prod_instance; same defaults, same
    # "must be on the type" rule.
    kai_agent_mem_limit    = optional(string, "2g")
    kai_agent_cpus         = optional(string, "1.0")
    kai_agent_pg_mem_limit = optional(string, "1g")
    # Engine → instance MCP tool surface — see prod_instance for the
    # rationale; same default, inert without kai_agent_enabled.
    kai_agent_broker_mcp_enabled = optional(bool, false)
    # Web-chat provider pin (AGNES_CHAT_PROVIDER) — see prod_instance for the
    # rationale; same default (empty = no env line), same validations below.
    chat_provider = optional(string, "")
    # See prod_instance for the rationale; same default.
    upgrade_schedule = optional(string, "*/5 * * * *")

    # Vendor-neutral per-instance branding — see prod_instance for the full
    # contract (seeded into /data/state/instance.yaml on FIRST boot; unset =
    # byte-for-byte identical file; the app reads them back via
    # app/instance_config.py). MUST be declared on this object type: Terraform
    # silently drops attributes absent from the type, so a bare entry in a
    # caller's dev_instances list would never reach the module.
    logo_svg    = optional(string, "")
    brand       = optional(string, "")
    brand_short = optional(string, "")
    subtitle    = optional(string, "")
    copyright   = optional(string, "")
    favicon     = optional(string, "")
    theme_colors = optional(object({
      primary        = optional(string)
      primary_dark   = optional(string)
      primary_light  = optional(string)
      background     = optional(string)
      surface        = optional(string)
      border         = optional(string)
      text_primary   = optional(string)
      text_secondary = optional(string)
      success        = optional(string)
      warning        = optional(string)
      error          = optional(string)
      font_primary   = optional(string)
      font_url       = optional(string)
      radius         = optional(string)
    }), {})
    custom_scripts = optional(list(object({
      name      = string
      enabled   = bool
      placement = string
      html      = string
    })), [])
  }))
  default = []

  # Same failure as prod_instance: an alias equal to the domain gives Caddy two
  # site blocks with one address and it refuses to start, taking the primary
  # site with it (Devin Review on #1182).
  validation {
    condition = alltrue([
      for i in var.dev_instances : i.domain_alias == "" || i.domain_alias != i.domain
    ])
    error_message = "each dev_instances[].domain_alias must differ from its domain; two site blocks sharing one address stop Caddy from starting."
  }

  # Same silent-fallback hazard as prod_instance.ui_layout / .theme above,
  # and the same retirement of "topnav".
  validation {
    condition = alltrue([
      for i in var.dev_instances : contains(["", "rail"], i.ui_layout)
    ])
    error_message = "each dev_instances[].ui_layout must be \"\" or \"rail\". The \"topnav\" chrome was retired (Wave 0, 2026-08) and the rail is now unconditional — remove the line."
  }

  validation {
    condition = alltrue([
      for i in var.dev_instances : contains(["", "blue", "navy", "dark", "auto", "paper"], i.theme)
    ])
    error_message = "each dev_instances[].theme must be \"\", \"blue\", \"navy\", \"dark\", \"auto\" or \"paper\"."
  }

  # Same silent-fallback hazard as prod_instance.experience above, and the
  # same retirement of "classic".
  validation {
    condition = alltrue([
      for i in var.dev_instances : contains(["", "redesign"], i.experience)
    ])
    error_message = "each dev_instances[].experience must be \"\" or \"redesign\". The \"classic\" experience was retired (Wave 0, 2026-08) — remove the line."
  }

  # Same plan-time guards as prod_instance.chat_provider — see there.
  validation {
    condition = alltrue([
      for i in var.dev_instances : contains(["", "docker", "kai-agent"], i.chat_provider)
    ])
    error_message = "each dev_instances[].chat_provider must be \"\", \"docker\" or \"kai-agent\". The \"e2b\" provider was removed in app 0.89.0."
  }

  validation {
    condition = alltrue([
      for i in var.dev_instances : i.chat_provider != "kai-agent" || i.kai_agent_enabled
    ])
    error_message = "dev_instances[].chat_provider = \"kai-agent\" requires kai_agent_enabled = true on the same VM — web chat pinned onto an engine the VM does not run refuses every session."
  }
}

variable "oauth_secret_name_template" {
  description = <<-EOT
    Template for per-VM OAuth client (Sign-in with Google) Secret Manager
    secret names. Supports placeholders:
      {kind} -> "id" or "secret" (REQUIRED — otherwise both fetches resolve
                to the same secret, which is broken)
      {role} -> "prod" for the prod VM; for dev VMs, whatever was passed in
                via `dev_instances[*].role` (defaults to "dev"). Set
                `role = "stage"` / "perf" / etc. on a dev_instances entry to
                introduce a new env class — the matching
                <template-expanded-stage> secrets must already exist in SM.
      {name} -> the VM name from prod_instance.name / dev_instances[*].name
                (one OAuth client per VM, regardless of role)

    Empty (default) -> legacy shared `google-oauth-client-{id,secret}`
    (v1.x default — same OAuth client across every VM in the module call).

    Examples:
      "agnes-google-oauth-client-{kind}-{role}"  -> one client per role
                                                    (prod, dev share across
                                                    multiple dev VMs)
      "agnes-oauth-{kind}-{name}"                -> one client per VM
                                                    (every VM isolated; needed
                                                    for per-engineer dev VMs
                                                    on shared OAuth domain)

    Resolved names must already exist in Secret Manager — the module grants
    the VM SA secretAccessor on the resolved set; it does NOT create the
    secret rows themselves (those carry the OAuth credentials issued in
    Cloud Console, which has no public API).

    Caveat: do NOT also list a derived name in `var.runtime_secrets` — the
    same `google_secret_manager_secret_iam_member` would land twice for the
    same (project, secret, role, member) tuple and apply errors with
    "already exists". Keep `runtime_secrets` strictly for OTHER secrets the
    VM needs (e.g. `keboola-storage-token`) when the template is in use.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.oauth_secret_name_template == "" || strcontains(var.oauth_secret_name_template, "{kind}")
    error_message = "oauth_secret_name_template must contain the {kind} placeholder when non-empty (otherwise id and secret resolve to the same Secret Manager name)."
  }

  validation {
    condition = var.oauth_secret_name_template == "" || (
      strcontains(var.oauth_secret_name_template, "{role}") ||
      strcontains(var.oauth_secret_name_template, "{name}")
    )
    error_message = "oauth_secret_name_template should contain {role} or {name} for per-VM differentiation — otherwise every VM resolves to the same Secret Manager name and you've just renamed the legacy shared client (which is fine, but pointless to do via this variable; set runtime_secrets instead)."
  }
}

variable "seed_admin_email" {
  description = "Email of the initial admin user."
  type        = string
}

variable "enable_seed_password" {
  description = "If true, the seed admin user immediately gets a password_hash from seed_admin_password (dev helper). Keep false in prod — the admin sets a password via /auth/bootstrap or Google OAuth."
  type        = bool
  default     = false
}

variable "seed_admin_password" {
  description = "Plain-text password for the seed admin. Only used when enable_seed_password=true. WARNING: stored in Terraform state."
  type        = string
  default     = ""
  sensitive   = true
}

variable "data_source" {
  description = "Data source type — keboola | bigquery | csv. First-boot seed only (D1 residual, 2026-08): the value is written into instance.yaml's `data_source.type` the FIRST time a VM in this instance boots (never on a later apply/recreate) — the admin UI (`/admin/server-config`) owns it from day 2 onward. **BREAKING** for pinned infra roots: this used to be an always-wins `.env` line (`DATA_SOURCE=...`) rewritten on EVERY boot, silently reverting any UI change; it no longer reaches `.env` at all. Also still threaded to the startup script to gate the one-time boot-side fetch of the keboola-storage-token secret."
  type        = string
  default     = "keboola"
}

variable "keboola_stack_url" {
  description = "Keboola Stack URL (used when data_source = keboola)."
  type        = string
  default     = ""
}

variable "image_repo" {
  description = <<-EOT
    Docker image repo the instance runs (and extracts host artifacts from).
    Threaded into /opt/agnes/.env as AGNES_IMAGE_REPO, so the compose files
    and the recurring host scripts (agnes-auto-upgrade.sh,
    agnes-state-applier.sh) all resolve the same repository.

    Registry access: when the image lives in GCP Artifact Registry
    (*-docker.pkg.dev) the startup script runs `gcloud auth
    configure-docker` for that host, so the VM's own service account
    authenticates every pull — grant it artifactregistry.reader on the
    repository. Any other private registry needs pre-authenticated pull
    access on the VM (not provided by this module).
  EOT
  type        = string
  default     = "ghcr.io/keboola/agnes-the-ai-analyst"
}

variable "compose_ref" {
  # RETIRED. This never pinned anything: it was threaded to the startup script
  # as COMPOSE_REF and then never read. Compose files are extracted from the
  # image the operator pinned with `image_tag` (agnes-auto-upgrade.sh
  # refreshes them from that same image's /opt/agnes-host/ on every tick) —
  # so a root setting `compose_ref = "stable-YYYY.MM.N"` believed it had
  # pinned its compose files and had not.
  #
  # Kept DECLARED, like `ui_layout` above, so that belief fails loudly instead
  # of quietly: a root that still sets it gets a plan-time error naming the
  # retirement, rather than an apply that succeeds and pins nothing. Delete the
  # line from your root when convenient. To pin, pin `image_tag`.
  description = "RETIRED — never had any effect. Pin `image_tag` instead; setting this is now a plan-time error."
  type        = string
  default     = ""

  validation {
    condition     = var.compose_ref == ""
    error_message = "compose_ref is retired and never pinned anything — it was passed to the VM and never read. Compose files come from the image `image_tag` selects. Remove the compose_ref line from your root module and pin `image_tag` instead."
  }
}

variable "enable_monitoring" {
  description = "Create uptime checks + alert policies for each VM. Requires notification_channel_ids to be useful."
  type        = bool
  default     = true
}

variable "notification_channel_ids" {
  description = "Full resource IDs of GCP Monitoring notification channels (create in customer project via gcloud alpha monitoring channels create). Empty list = alerts fire but nothing is notified."
  type        = list(string)
  default     = []
}

variable "runtime_secrets" {
  description = "Names of existing Secret Manager secrets the VM needs to read at runtime (e.g. Keboola Storage token). VM SA gets scoped secretAccessor on each. Use this for secrets the startup script handles explicitly (KEBOOLA_STORAGE_TOKEN, GOOGLE_CLIENT_ID/SECRET — names are hardcoded in startup-script.sh.tpl). For new app-level secrets (ANTHROPIC_API_KEY, SLACK_*), prefer `runtime_secret_env` below."
  type        = list(string)
  default     = ["keboola-storage-token"]
}

variable "runtime_secret_env" {
  description = "Map of Secret Manager secret name to env var name to inject into /opt/agnes/.env. Module auto-grants secretAccessor and the startup script fetches each via gcloud secrets versions access latest --secret=<key> and writes a line <env_var>=<fetched> to .env. Missing/403 -> empty string (silent), so production deploys can roll out a secret name before the value lands. Example map: anthropic-api-key -> ANTHROPIC_API_KEY, slack-bot-token -> SLACK_BOT_TOKEN."
  type        = map(string)
  default     = {}
}

variable "firewall_ssh_source_ranges" {
  description = "CIDR ranges allowed to reach SSH (port 22). Default is IAP tunnel range only (use `gcloud compute ssh --tunnel-through-iap`). Override to `[\"0.0.0.0/0\"]` for unrestricted (not recommended)."
  type        = list(string)
  default     = ["35.235.240.0/20"]
}

variable "acme_email" {
  description = "Email for Let's Encrypt account (used when tls_mode=caddy). Defaults to seed_admin_email if empty."
  type        = string
  default     = ""
}

variable "home_route" {
  description = "Landing page after auth. One of /home (state-aware onboarding), /dashboard (legacy table inventory), /setup, /catalog. First-boot seed only (D1, 2026-08): a non-empty value is written into instance.yaml's `instance.home_route` the FIRST time a VM in this instance boots (never on a later apply/recreate) — the admin UI (`/admin/server-config`) owns it from day 2 onward. Empty (default) seeds nothing, so the app falls through to its built-in /dashboard default. Applies to prod + all dev VMs in the instance (module-wide, not per-VM)."
  type        = string
  default     = ""
  validation {
    condition     = var.home_route == "" || contains(["/home", "/dashboard", "/setup", "/catalog"], var.home_route)
    error_message = "home_route must be empty or one of: /home, /dashboard, /setup, /catalog"
  }
}

variable "studio_enabled" {
  description = "Expose the authoring Studio (/admin/studio). First-boot seed only (D1, 2026-08): `false` is written into instance.yaml's `studio.enabled` the FIRST time a VM in this instance boots (never on a later apply/recreate) — the admin UI (`/admin/server-config`) owns it from day 2 onward. `true` (default) seeds nothing, matching the app's own default."
  type        = bool
  default     = true
}

variable "enable_watchdog" {
  description = "Install the host-side watchdog + daily DB backup on every VM. The watchdog (5-min systemd timer) greps container logs for known incident signatures — DuckDB fatal crash loops, the invalidated-database \"zombie\" state (app answers /api/health 200 while every write 500s), WAL salvage data-loss events, index-desync errors — plus container restart bursts, cgroup OOM kills, scheduler failure streaks and /data disk pressure. The backup (daily systemd timer) copies system.duckdb+WAL to /data/backups/system-duckdb/ with 7-day retention and proves each copy restorable via a canary open+replay. Complements enable_monitoring: uptime checks see the VM from outside; the watchdog sees failure states the health endpoint cannot express, and PD snapshots preserve a corrupted file faithfully while the canary verify catches the corruption."
  type        = bool
  default     = true
}

variable "enable_gcp_logging" {
  description = <<-EOT
    Ship every container's stdout/stderr to Google Cloud Logging via Docker's
    built-in gcplogs driver, in addition to the local dual-logging cache
    `docker logs` reads from. On: the module grants roles/logging.logWriter
    on the project to the VM service account (the gcplogs driver
    authenticates as that account, and Docker refuses to START a container
    whose log driver cannot initialize — without the role, any container
    recreate takes the instance down), and the startup script extracts
    docker-compose.gcp-logging.yml (baked into the image) into the app
    directory, probes that the driver actually initializes, and only then
    arms the overlay for the COMPOSE_FILE resolver
    (scripts/ops/agnes-compose-file.sh) to include on every `docker compose`
    invocation — so logs survive the routine container recreates the
    auto-upgrade cron performs every 5 minutes, which otherwise destroy the
    Docker json-file log history. The IAM grant means the identity running
    `terraform apply` must be allowed to modify project IAM policy (e.g.
    roles/resourcemanager.projectIamAdmin); if yours cannot, grant
    roles/logging.logWriter to the VM service account out-of-band or set
    this to false — a VM without the role stays up either way (the probe
    disables the overlay with a warning) but ships no logs. Off: the script
    removes the file instead, keeping the instance on the default json-file
    driver (rotated by /etc/docker/daemon.json) — the only supported choice
    for a non-GCE / non-GCP deployment, since gcplogs needs GCE
    metadata-server credentials.
  EOT
  type        = bool
  default     = true
}

variable "dispatcher_image" {
  description = <<-EOT
    Image for the opt-in LLM dispatcher (token-arbitrage PoC), e.g.
    "ghcr.io/keboola/token-arbitrage-dispatcher:<sha>". Pin to a commit-sha
    tag — CI publishes :<sha> and :latest on every token-arbitrage main push;
    deploying :latest makes rollouts non-reproducible.

    The dispatcher runs as an extra compose service (overlay written by the
    startup script) on any VM whose instance object sets
    `dispatcher_enabled = true`; the Agnes chat broker then routes chat
    completions to it via LLM_DISPATCHER_URL (see app/api/broker.py).
    Required (with the other dispatcher_* variables) when any instance
    enables the dispatcher.
  EOT
  type        = string
  default     = ""
}

variable "dispatcher_policies" {
  description = <<-EOT
    Routing-policy YAML CONTENT for the dispatcher (pass file(...) from the
    deployment repo — policy is deployment-owned). Delivered to the VM as
    /opt/agnes/dispatcher/policies.yaml via the startup script. Must parse
    under the dispatcher's PolicyEngine (top-level `default:` route is
    mandatory). Required when any instance enables the dispatcher.
  EOT
  type        = string
  default     = ""
}

variable "dispatcher_key_secret" {
  description = <<-EOT
    Secret Manager secret name holding the dispatcher API key (the value the
    Agnes broker sends as x-api-key; doubles as the cost-ledger team
    identity). The startup script builds /opt/agnes/dispatcher/keys.yaml
    mapping this key to team "agnes" and writes LLM_DISPATCHER_API_KEY into
    /opt/agnes/.env. Module grants the VM SA secretAccessor. Fetch fails
    LOUDLY at boot when the dispatcher is enabled — a dispatcher without its
    key would 401 every chat request. Required when any instance enables the
    dispatcher.
  EOT
  type        = string
  default     = ""
}

variable "dispatcher_vertex_sa_secret" {
  description = <<-EOT
    Secret Manager secret name holding a GCP service-account KEY JSON with
    Vertex AI access in the project the routing policy targets (the VM's own
    SA usually lives in a different project, so ADC is not enough). Written
    to /opt/agnes/dispatcher/vertex-sa.json and mounted into the dispatcher
    container as GOOGLE_APPLICATION_CREDENTIALS. Module grants the VM SA
    secretAccessor; fetch fails loudly at boot when the dispatcher is
    enabled. Required when any instance enables the dispatcher.
  EOT
  type        = string
  default     = ""
}

variable "kai_agent_image" {
  description = <<-EOT
    Full image ref (with tag) of the kai-agent turn engine, e.g.
    "<region>-docker.pkg.dev/<project>/<repo>/kai-agent:<tag>". Pin to an
    immutable tag — the engine runs as an extra compose service on any VM
    whose instance object sets `kai_agent_enabled = true`, and the
    agnes-auto-upgrade tick re-pulls it every cycle, so a floating tag makes
    rollouts non-reproducible.

    Registry access: when the image lives in GCP Artifact Registry
    (*-docker.pkg.dev) the startup script runs `gcloud auth configure-docker`
    for that host, so the VM's own service account authenticates the pull —
    grant it artifactregistry.reader on the repository. Any other private
    registry needs pre-authenticated pull access on the VM (not provided by
    this module).

    Required (with the other kai_agent_* variables) when any instance enables
    the engine.
  EOT
  type        = string
  default     = ""
}

variable "kai_agent_jwt_secret" {
  description = <<-EOT
    Secret Manager secret name holding the HS256 secret shared between the
    Agnes host surface and the engine (>= 32 chars — the engine refuses
    shorter; mint with `openssl rand -hex 32`). On every VM with
    `kai_agent_enabled = true` the startup script writes the SAME fetched
    value to both halves of the pair: KAI_HOST_JWT_SECRET in the app's .env
    (enabling the /api/kai/* host routes — unset, they answer 503) and
    HOST_JWT_SECRET in the engine's env, so the two can never drift. Module
    grants the VM SA secretAccessor; the fetch fails LOUDLY at boot when the
    engine is enabled. Required when any instance enables the engine.
  EOT
  type        = string
  default     = ""
}

variable "kai_agent_e2b_key_secret" {
  description = <<-EOT
    Secret Manager secret name holding the E2B API key the engine spawns its
    sandboxes with (the engine's E2B_API_KEY — may name the same secret the
    app's own cloud chat uses via runtime_secret_env). Module grants the VM SA
    secretAccessor; fetch fails loudly at boot when the engine is enabled.
    Required when any instance enables the engine.
  EOT
  type        = string
  default     = ""
}

variable "kai_agent_env" {
  description = <<-EOT
    Extra environment for the engine container, written verbatim into its env
    file AFTER the derived lines — env_file gives later duplicate keys
    precedence, so entries here can also override a derived value (e.g. a
    split-horizon HOST_BROKER_TICKET_URL). Deployment-owned, like
    dispatcher_policies. NON-SENSITIVE values only: the map lands in Terraform
    state and on the VM in plaintext.

    The module derives HOST_MODULE/HOST_JWT_*/HOST_BROKER_LLM_URL/
    HOST_BROKER_TICKET_URL/HOST_WORKSPACE_URL/POSTGRES_URL/E2B_API_KEY; the
    engine additionally requires from this map at minimum:
      HOST_AGENT_IDENTITY  — the agent's persona line (host copy)
      CLOUD_LLM_PROVIDER   — "anthropic" for a broker-fronted engine, plus its
      ANTHROPIC_UPSTREAM_URL / ANTHROPIC_UPSTREAM_API_KEY — required by the
        engine's env validation even though the jwt host path never reads
        them (all LLM traffic transits the Agnes broker); placeholders are
        fine and expected.
    Optional extras: LLM_MODEL_NAME, LOG_LEVEL, ...

    HOST_BROKER_MCP_URL is normally NOT set here: the per-VM
    kai_agent_broker_mcp_enabled flag derives it from this instance's own
    origin AND sets the app-side switch that makes it work, which is the
    pairing this key alone cannot complete. Set it here only to override the
    derived value (split-horizon, say).

    Values must be SINGLE-LINE: the map is rendered as KEY=VALUE lines into
    the engine's env_file, where an embedded line break truncates the value
    and turns its remainder into a garbage line — the engine then never
    starts, with only a generic warning in the boot log. Rejected at plan
    time below.
  EOT
  type        = map(string)
  default     = {}

  validation {
    condition = alltrue([
      for k, v in var.kai_agent_env :
      !strcontains(v, "\n") && !strcontains(k, "\n") && !strcontains(k, "=")
    ])
    error_message = "kai_agent_env keys and values must be single-line (and keys must not contain '='): the map becomes KEY=VALUE lines in the engine's env_file, where an embedded newline corrupts the file and the engine silently never starts."
  }
}

variable "alert_webhook_url" {
  description = "Webhook for watchdog + backup-verify alerts (Slack / Google Chat compatible: POST {\"text\": ...}). Empty (default) = alerts go to journald + /var/log/agnes-watchdog.log only. Lands on the VM in /etc/agnes-watchdog.env (root, 0600). An operator may hand-edit that file; the startup script preserves a hand-edited value when this variable is empty and overwrites it when set — same precedence pattern as AGNES_TAG."
  type        = string
  default     = ""
  sensitive   = true
}

# data_apps_enabled is a PER-VM field on prod_instance / dev_instances[*]
# (like dispatcher_enabled), not a module-global — enabling it here would flip
# every VM including prod. See those object types above.

variable "data_apps_runtime_image" {
  description = "Full runtime image (with tag) the data-app containers run, instance-wide. The registry prefix (everything before the last `:`) is also handed to the apps-runner as APPS_RUNNER_IMAGE_PREFIX to gate which images it may pull. Only consulted on VMs with data_apps_enabled = true."
  type        = string
  default     = "keboolapublic.azurecr.io/data-app-python-js:1.6.2_python-3.13_node-24"
}
