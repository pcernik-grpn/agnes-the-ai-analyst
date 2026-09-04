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
    # GCE-side guard: while true, neither `terraform destroy` nor
    # `gcloud compute instances delete` can remove this VM until someone
    # clears the flag. Worth turning on for any instance carrying customer
    # data. It does NOT block an in-place update, so a deployment that sets it
    # can still be reconfigured normally — and note that clearing it is itself
    # an ordinary apply, so it guards against accident rather than intent.
    #
    # Left at false so existing roots see no diff. A root that sets it out of
    # band with gcloud, without this field, gets the flag reverted on the next
    # apply: the provider's own default is false, and a module that never
    # declares the attribute hands the provider that default every time.
    deletion_protection = optional(bool, false)
    image_tag           = optional(string, "stable")
    upgrade_mode        = optional(string, "auto")
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
    # docker-compose.yml (mem_limit: $${AGNES_APP_MEM_LIMIT:-4g}). "auto"
    # (the default, TCRD-296) defers sizing to the box the startup script
    # actually boots on: app = clamp(RAM/8, 4 GiB, 32 GiB), scheduler =
    # 2 GiB fixed — re-derived from /proc/meminfo on EVERY boot, so a VM
    # recreate never regresses to a laptop-sized literal. A live 64-vCPU/
    # 251GB VM ran a fixed 4g app cap and was OOM-killed four times serving
    # DuckDB queries before this existed. Set an explicit value ("8g") to
    # override "auto" outright; do so together with the app's per-connection
    # DuckDB budgets (DuckDB sizes a fresh connection to ~80% of the cgroup
    # limit, so an under-sized cap OOM-kills uvicorn mid-WAL-write).
    app_mem_limit       = optional(string, "auto")
    scheduler_mem_limit = optional(string, "auto")
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
    # Opt-in: let the engine's sandbox export its OWN spans (turn → step →
    # tool) through this instance's OTLP broker route. Derives
    # HOST_BROKER_OTLP_URL=<origin>/api/broker/otlp into the engine env, the
    # same way kai_agent_broker_mcp_enabled derives the MCP URL. A deliberate
    # flag rather than a side effect of otlp_endpoint, because the URL is a
    # two-sided switch on the engine: its presence makes the sandbox
    # initialize OTel AND makes `otlp` an active relay scope every turn needs
    # a ticket for — set against an app older than the route that mints it
    # (app >= 0.100, `POST /api/broker/otlp/v1/{signal}`) every turn fails
    # before the prompt is sent. So: app first, then this flag. Requires
    # kai_agent_enabled and otlp_endpoint on the same VM (validated below);
    # startup-script-owned, lands on VM recreate.
    kai_agent_broker_otlp_enabled = optional(bool, false)
    # Opt-in OpenTelemetry (OTLP/HTTP) export of this VM's LLM completions
    # (docs/observability.md → "OpenTelemetry export"; app >= 0.98).
    # `otlp_endpoint` is the collector's BASE URL — the SDK appends
    # /v1/traces — and is not a secret. `otlp_headers_secret` names a Secret
    # Manager secret whose value is the ENTIRE `OTEL_EXPORTER_OTLP_HEADERS`
    # string (e.g. `Authorization=Bearer%20<token>`, W3C-baggage encoded);
    # the module grants secretAccessor and the startup script fetches it at
    # boot into /opt/agnes/.env with the runtime_secret_env escape set, so
    # the credential never sits in Terraform state, VM metadata or a command
    # line. `otlp_capture_content` also exports prompt and completion text
    # (customer data — off by default; turn it on only where the collector
    # may hold it). Per-VM so a dev pilot never reaches prod. All three are
    # startup-script-owned: they land on VM recreate, not on apply.
    otlp_endpoint        = optional(string, "")
    otlp_headers_secret  = optional(string, "")
    otlp_capture_content = optional(bool, false)
    # The instance label every log line (`env`) and every exported span
    # (`deployment.environment`) carries — written as AGNES_DEPLOYMENT_ENV.
    # Empty = this VM's name, which is what an operator filters on anyway
    # (until now the line was absent and the logs said `unknown`).
    deployment_env = optional(string, "")
    # Opt-in extraction lane on this VM: a Redis coordination backend + the
    # `extraction-worker` compose service (AGNES_ROLE=worker), which makes the
    # deployment role-split. Per-VM (like dispatcher_enabled) and OFF by
    # default so a module bump alone never moves the existing fleet. Turning
    # it on writes AGNES_COORDINATION_BACKEND=redis + AGNES_REDIS_URL +
    # AGNES_SHAREPOINT_ENABLED=1 into the VM's app .env (env overrides
    # instance.yaml for every one of these — app/coordination/factory.py's
    # posture, mirrored by app/instance_config.py::feature_enabled — so the
    # applier-owned /data/state/instance.yaml is never touched) and engages a
    # module-owned docker-compose.extraction.yml overlay carrying the `redis`
    # service and an always-on `extraction-worker`. By DEFAULT the worker
    # carries no image override: the built-in document pipeline (owner
    # decision 2026-08-31 — connectors/sharepoint/crawler.py, one pipeline)
    # needs nothing bundled that the app image does not already carry, so
    # the service falls through to docker-compose.prod.yml's own pin and
    # simply follows AGNES_IMAGE_REPO/AGNES_TAG like app/scheduler — this is
    # what stops pin rot. The module-level extraction_worker_image can still
    # pin the worker away from that deliberately (a canary, holding the
    # worker back mid-rollout); see that variable's own description for why
    # a pin left in place risks a crash loop. This is what makes the flag
    # alone activate the `corpus-extraction` job kind end to end — no per-VM
    # SSH edit of instance.yaml required. AGNES_SHAREPOINT_ENABLED gates the
    # WHOLE SharePoint connector (2026-09-01 flag consolidation), not just
    # extraction, so this also turns on the connect wizard, admin routes, and
    # ACL mirroring on this VM. The multi-process
    # startup guard (app/startup_guards.py) then requires the instance to
    # already run the Postgres app-state backend and boots refuse loudly on a
    # DuckDB instance — deliberate: migrate the backend first, then flip this.
    extraction_worker_enabled = optional(bool, false)
    # Worker container resource ceiling, written to /opt/agnes/.env like
    # kai_agent_mem_limit above (TF field, not a .env hand-edit — the startup
    # script rewrites .env from scratch on every boot). "auto" (the default,
    # TCRD-296) derives RAM * 0.6, capped so app + worker + an 8 GiB
    # headroom (Postgres + host) never exceeds the VM's actual RAM, floored
    # at 4 GiB — the worker runs the extraction pipeline in-process (document
    # download + conversion + LLM calls), hence beefier than the kai
    # engine's. Set an explicit value ("8g") to override outright.
    extraction_worker_mem_limit = optional(string, "auto")
    extraction_worker_cpus      = optional(string, "2.0")
    # Number of `extraction-worker` compose replicas on this VM
    # (`docker compose up -d --scale extraction-worker=N`). 1 (the default)
    # reproduces today's behaviour byte-for-byte: the startup script renders
    # `--scale extraction-worker=1`, a no-op next to a plain `up -d`, and
    # writes no Postgres pool override — the extraction-worker process keeps
    # sizing its own connection pool from `extraction.concurrency` /
    # `extraction.facts.concurrency` (src/db_pg.py's per-process hint, which
    # assumes exactly ONE replica).
    #
    # TCRD-296 gap #76: an operator ran SIX replicas by hand
    # (`docker compose up -d --scale extraction-worker=6`) to keep up with a
    # facts-extraction backlog on a 64-vCPU/252 GiB VM. Nothing here
    # remembered that scale — the next recreate silently dropped back to
    # one — and six replicas each sizing a pool from the SAME per-process
    # hint exhausted the Postgres side-car's stock 100-connection cap
    # ("FATAL: sorry, too many clients already"), worked around by hand with
    # `AGNES_PG_POOL_SIZE`/`AGNES_PG_MAX_OVERFLOW` in `.env` (which — set
    # there — also shrank app's and scheduler's pools, not just the
    # worker's) and a manual `ALTER SYSTEM SET max_connections`.
    #
    # Setting this > 1 makes the startup script (a) thread `--scale
    # extraction-worker=N` through every `docker compose up` that would
    # otherwise recreate the stack at N=1 (the boot sequence AND the
    # recurring agnes-auto-upgrade tick — see
    # docs/DEPLOYMENT.md#sizing-an-extraction-instance), (b) pin
    # `AGNES_PG_POOL_SIZE`/`AGNES_PG_MAX_OVERFLOW` on the extraction-worker
    # service ONLY (app/scheduler keep their own defaults), and (c) size the
    # Postgres side-car's `max_connections` to hold app + scheduler + every
    # replica's pool, plus headroom — see the `agnes_pg_max_connections`
    # sizing note next to `agnes_pg_shared_buffers_mb` in
    # startup-script.sh.tpl. Raising this without also raising the host's
    # RAM (or lowering `extraction_worker_mem_limit`) can overcommit memory —
    # each replica gets the SAME per-replica ceiling, not a shrunk share of
    # it.
    extraction_worker_replicas = optional(number, 1)
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

  validation {
    condition     = var.prod_instance.otlp_headers_secret == "" || var.prod_instance.otlp_endpoint != ""
    error_message = "prod_instance.otlp_headers_secret is set but otlp_endpoint is empty — the headers are the collector's credential and mean nothing without a collector to send to."
  }

  validation {
    condition     = var.prod_instance.otlp_endpoint == "" || can(regex("^https?://", var.prod_instance.otlp_endpoint))
    error_message = "prod_instance.otlp_endpoint must be an http(s) URL — the collector's BASE URL, without the /v1/traces suffix the SDK appends itself."
  }

  validation {
    condition     = var.prod_instance.extraction_worker_replicas >= 1 && var.prod_instance.extraction_worker_replicas <= 32
    error_message = "prod_instance.extraction_worker_replicas must be between 1 and 32."
  }

  validation {
    condition     = !var.prod_instance.kai_agent_broker_otlp_enabled || (var.prod_instance.kai_agent_enabled && var.prod_instance.otlp_endpoint != "")
    error_message = "prod_instance.kai_agent_broker_otlp_enabled requires kai_agent_enabled = true and a non-empty otlp_endpoint on the same VM — the engine would arm an otlp relay scope against a broker route with no collector to forward to."
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
    # Disk sizes, per dev VM. MUST be declared on the object type — Terraform
    # silently drops attributes absent from the type during conversion, so a
    # caller's `data_disk_gb = 60` never reached the merge with dev_defaults
    # and every dev VM was stuck at the default. Same defaults as
    # dev_defaults in main.tf. Growing `data_disk_gb` is an in-place GCE
    # resize (no VM change); the filesystem still needs an online
    # `resize2fs` on the VM afterwards — the startup script only formats a
    # fresh disk. Shrinking is refused by GCE.
    disk_size_gb = optional(number, 30)
    data_disk_gb = optional(number, 20)
    image_tag    = optional(string, "dev")
    tls_mode     = optional(string, "none")
    domain       = optional(string, "")
    # See prod_instance.deletion_protection. Declared here too because
    # Terraform silently drops attributes absent from the type — a dev entry
    # setting it against an object type that does not declare it would be
    # discarded without an error, which is the failure mode this file keeps
    # calling out.
    deletion_protection = optional(bool, false)
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
    # See prod_instance for the rationale; same defaults ("auto").
    app_mem_limit       = optional(string, "auto")
    scheduler_mem_limit = optional(string, "auto")
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
    # Engine sandbox → this instance's OTLP broker route — see prod_instance
    # for the ordering contract; same default, inert without kai_agent_enabled
    # and otlp_endpoint.
    kai_agent_broker_otlp_enabled = optional(bool, false)
    # Per-VM opt-in OTLP export + deployment label — see prod_instance for
    # the contract; same defaults, same "must be on the type" rule.
    otlp_endpoint        = optional(string, "")
    otlp_headers_secret  = optional(string, "")
    otlp_capture_content = optional(bool, false)
    deployment_env       = optional(string, "")
    # Opt-in extraction lane (Redis coordination + extraction-worker) — see
    # prod_instance for the full contract; same defaults ("auto" mem limit),
    # OFF by default so a module bump alone never moves existing VMs.
    extraction_worker_enabled   = optional(bool, false)
    extraction_worker_mem_limit = optional(string, "auto")
    extraction_worker_cpus      = optional(string, "2.0")
    # Per-VM extraction-worker replica count — see prod_instance for the full
    # rationale (TCRD-296 gap #76); same default (1, byte-identical to
    # today), same "must be on the type" rule as the fields above.
    extraction_worker_replicas = optional(number, 1)
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

  validation {
    condition     = alltrue([for d in var.dev_instances : try(d.otlp_headers_secret, "") == "" || try(d.otlp_endpoint, "") != ""])
    error_message = "dev_instances[*].otlp_headers_secret is set but otlp_endpoint is empty — the headers are the collector's credential and mean nothing without a collector to send to."
  }

  validation {
    condition     = alltrue([for d in var.dev_instances : try(d.otlp_endpoint, "") == "" || can(regex("^https?://", d.otlp_endpoint))])
    error_message = "dev_instances[*].otlp_endpoint must be an http(s) URL — the collector's BASE URL, without the /v1/traces suffix the SDK appends itself."
  }

  validation {
    condition = alltrue([
      for i in var.dev_instances : i.extraction_worker_replicas >= 1 && i.extraction_worker_replicas <= 32
    ])
    error_message = "each dev_instances[].extraction_worker_replicas must be between 1 and 32."
  }

  validation {
    condition     = alltrue([for d in var.dev_instances : !try(d.kai_agent_broker_otlp_enabled, false) || (try(d.kai_agent_enabled, false) && try(d.otlp_endpoint, "") != "")])
    error_message = "dev_instances[*].kai_agent_broker_otlp_enabled requires kai_agent_enabled = true and a non-empty otlp_endpoint on the same VM — the engine would arm an otlp relay scope against a broker route with no collector to forward to."
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
  description = "Map of Secret Manager secret name to env var name to inject into /opt/agnes/.env. Module auto-grants secretAccessor and the startup script fetches each via gcloud secrets versions access latest --secret=<key> and writes a line <env_var>=<fetched> to .env. Missing/403 -> empty string (silent), so production deploys can roll out a secret name before the value lands. Example map: anthropic-api-key -> ANTHROPIC_API_KEY, slack-bot-token -> SLACK_BOT_TOKEN. Values are written double-quoted with shell escaping, so a single-line value may safely contain spaces, quotes, `$` or backticks. Single-line values ONLY — the startup script refuses a multiline value (a PEM certificate, an SA key JSON) with a boot-log warning and writes an empty string, because raw it would corrupt the .env for both of its consumers (the startup script's own bash source and compose's dotenv parser); map those through `runtime_secret_env_multiline` instead."
  type        = map(string)
  default     = {}
}

variable "runtime_secret_env_multiline" {
  description = "Like `runtime_secret_env`, for MULTILINE secret values — e.g. the SharePoint combined cert+key PEM under SHAREPOINT_CERT_PRIVATE_KEY. The startup script base64-encodes the fetched value so it rides /opt/agnes/.env as a single line; the app decodes it on read (connectors/sharepoint/settings resolves either form). Store the secret in Secret Manager as the RAW value (the module does the encoding). Do not map the same secret name here and in `runtime_secret_env` — the module suppresses the duplicate IAM binding, but the two .env lines would fight."
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
  description = "Expose the authoring Studio (/admin/studio, its per-domain builders and the suggestions moderation queue). First-boot seed only (D1, 2026-08): `true` is written into instance.yaml's `studio.enabled` the FIRST time a VM in this instance boots (never on a later apply/recreate) — the admin UI (`/admin/server-config`) owns it from day 2 onward. `false` (default) seeds nothing, matching the app's own default, which is now OFF: the admin cleanup retired the Studio in favour of the Library builders (/library, '+ New'). Set true only for an instance that wants the second authoring surface back."
  type        = bool
  default     = false
}

variable "enable_watchdog" {
  description = "Install the host-side watchdog + daily DB backup on every VM. The watchdog (5-min systemd timer) greps container logs for known incident signatures — DuckDB fatal crash loops, the invalidated-database \"zombie\" state (app answers /api/health 200 while every write 500s), WAL salvage data-loss events, index-desync errors — plus container restart bursts, cgroup OOM kills, scheduler failure streaks and /data disk pressure. The backup (daily systemd timer) copies system.duckdb+WAL to /data/backups/system-duckdb/ with 7-day retention and proves each copy restorable via a canary open+replay. Complements enable_monitoring: uptime checks see the VM from outside; the watchdog sees failure states the health endpoint cannot express, and PD snapshots preserve a corrupted file faithfully while the canary verify catches the corruption."
  type        = bool
  default     = true
}

variable "enable_gcp_logging" {
  description = <<-EOT
    Permit and provision the Cloud Logging pipeline: every container's
    stdout/stderr forwarded over Docker's `fluentd` driver to a
    Google Cloud Ops Agent on loopback, which parses the JSON line back into
    fields and writes it to Cloud Logging (docs/gcp-logging.md). The local
    dual-logging cache `docker logs` reads from is unaffected.

    PERMIT, not select: which collector actually gets the logs is
    container_logs_destination below. This variable grants
    roles/logging.logWriter and roles/monitoring.metricWriter on the project
    to the VM service account, and makes Cloud Logging an eligible
    destination; leaving it true while the destination resolves to `datadog`
    keeps the grants in place and installs no Ops Agent, so flipping back is
    a recreate and not an IAM change.

    When Cloud Logging IS the destination, the startup script extracts
    docker-compose.gcp-logging.yml (baked into the image) into the app
    directory, probes that something is listening on the Ops Agent's forward
    port, and only then arms the overlay for the COMPOSE_FILE resolver
    (scripts/ops/agnes-compose-file.sh) to include on every `docker compose`
    invocation — so logs survive the container recreates the auto-upgrade
    cron performs when an image digest or a config file moves, which
    otherwise destroy the Docker json-file log history. The driver is
    ASYNC (`fluentd-async: true`), so a collector that is down costs log
    lines and cannot keep a container in `created` (#1557).

    The metric role is not about metrics this module wants: the Ops Agent
    that collects the logs runs an OpenTelemetry sub-agent which cannot be
    switched off, and it exports the agent's own free
    agent.googleapis.com/agent/* self-metrics whatever its config says. The
    host metrics Cloud Monitoring charges for are turned off in
    files/ops-agent-config.yaml, since enable_datadog is the path that
    collects those; without the role, though, every export cycle fails and
    floods the serial console with monitoring.timeSeries.create
    PermissionDenied. Order matters when upgrading an already-provisioned VM:
    the grant lands on `terraform apply`, while the agent config only reaches
    the VM on instance replacement, so until you recreate it that VM keeps
    the built-in hostmetrics receiver and now exports it successfully, billed
    by ingested bytes. Recreating closes the window and is the same step
    every other startup-script change from this module needs anyway.

    The IAM grants mean the identity running `terraform apply` must be
    allowed to modify project IAM policy (e.g.
    roles/resourcemanager.projectIamAdmin); if yours cannot, grant both
    roles to the VM service account out-of-band or set this to false — a VM
    without the logging role stays up either way (the probe disables the
    overlay with a warning) but ships no logs. Off: the script removes the
    file instead, keeping the instance on the default json-file driver
    (rotated by /etc/docker/daemon.json) — the only supported choice for a
    non-GCE / non-GCP deployment, since the Ops Agent authenticates with
    Google ADC from the GCE metadata server and there is nothing to receive
    the logs without it.
  EOT
  type        = bool
  default     = true
}

variable "container_logs_destination" {
  description = <<-EOT
    Which collector receives the containers' stdout/stderr. Empty (default)
    resolves automatically: `datadog` when enable_datadog is on, else
    `cloud_logging` when enable_gcp_logging is on, else `none`. Explicit
    values are `cloud_logging`, `datadog` and `none`.

    ONE destination per VM, because Docker allows exactly one log driver per
    container and the two collectors want different ones. Cloud Logging needs
    the `fluentd` driver (the Ops Agent is what parses the JSON line back into
    fields; Docker's own gcplogs driver parses nothing). Datadog needs the
    default `json-file` driver: it reads containers through the Docker API,
    and under a remote driver that API serves Docker's dual-logging cache —
    which happens to work but is not a path Datadog documents or supports.
    On json-file the same API serves the driver's own logs and the path is
    supported. So `datadog` leaves docker-compose.gcp-logging.yml off the
    disk, which disarms the COMPOSE_FILE resolver's gate for free.

    Running both is therefore not offered. It is not physically impossible —
    it would just mean building a production pipeline on undocumented
    behaviour, which this module does not do.

    ** BEHAVIOUR CHANGE ON A MODULE BUMP. ** A VM that already has
    enable_datadog = true resolves to `datadog` the next time it is recreated,
    and that has two halves — state both when reviewing a bump:

      * it STOPS shipping to Cloud Logging, so any log-based alert, dashboard
        or saved query pointed there goes quiet rather than red; and
      * it STARTS exporting every container log line to a third-party SaaS in
        whatever region datadog_site names. On a VM with enable_gcp_logging =
        false this is not a redirect at all: nothing left the host before, and
        now everything does. Data residency is a decision to make before the
        redaction rules, which bound only WHAT is sent, never WHERE.

    That is the intended default — metrics, monitors and logs belong in one
    console — but it is the one diff a bump alone carries. Set
    container_logs_destination = "cloud_logging" (or "none") to keep the old
    behaviour explicitly. See docs/datadog-logging.md.

    Startup-script-owned like every other setting here: it reaches a running
    VM only through `terraform apply -replace` of the instance.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = contains(["", "auto", "cloud_logging", "datadog", "none"], var.container_logs_destination)
    error_message = "container_logs_destination must be \"\" (auto), \"auto\", \"cloud_logging\", \"datadog\" or \"none\"."
  }
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

variable "extraction_worker_image" {
  description = <<-EOT
    OPTIONAL override for the `extraction-worker` service's image/tag — a
    deliberate, TEMPORARY divergence from the app, NOT the normal way to run
    this lane.

    Empty (the default) is normal: the module renders no `image:` key on the
    service at all, so it falls through to docker-compose.prod.yml's own
    AGNES_IMAGE_REPO/AGNES_TAG pin — the SAME ref `app`/`scheduler` run. This
    is what stops pin rot: the worker follows the app by construction, with
    nothing separate to fall behind. The built-in extraction pipeline needs
    nothing bundled that the app image does not already carry (the
    `extraction` optional extra ships in the DEFAULT image build), so this
    is also the right default on pure "does it work" grounds, not just
    safety.

    Set it only for a genuine, SHORT-LIVED reason to run the worker on a
    different tag than the app — a canary (rebuild the worker while the app
    stays on `:stable`), or holding the worker back mid-rollout. WARNING: a
    pin left in place is a liability, not a feature. The app keeps
    auto-upgrading and migrating the database forward
    (src/db_pg.py::assert_pg_at_head refuses to boot an image whose
    migrations trail the live schema) — once the database moves past what
    THIS pinned image's Alembic head knows, the worker refuses to start and
    crash-loops under `restart: unless-stopped` forever, silently killing
    extraction. This happened on a live deployment and is the reason this
    variable is optional rather than mandatory. Clear it again as soon as
    the divergence is no longer needed.

    Registry access: same rule as kai_agent_image — `gcloud auth
    configure-docker` is run for a *-docker.pkg.dev host, any other private
    registry needs pre-authenticated pull access on the VM.
  EOT
  type        = string
  default     = ""
}

variable "extraction_producer_command" {
  description = <<-EOT
    Deprecated, ignored — the module no longer reads this variable.

    It used to be written verbatim as AGNES_EXTRACTION_PRODUCER_COMMAND
    into the app .env of every instance with `extraction_worker_enabled =
    true`, naming the external producer binary's invocation command. That
    external-producer mode was removed (owner decision 2026-08-31); nothing
    in the app reads AGNES_EXTRACTION_PRODUCER_COMMAND any more, and the
    module no longer writes the line.

    Kept declared, accepted and unused only so a root module that still
    sets it does not fail `terraform plan` with "unsupported argument".
    Slated for removal in a later cleanup once known consumers have
    dropped it from their own configuration.
  EOT
  type        = string
  default     = ""
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

# --- Opt-in Datadog host monitoring --------------------------------------
#
# Module-global, like enable_watchdog / enable_gcp_logging: monitoring is an
# instance-wide posture, not a per-VM presentation choice. The module installs
# and configures the agent; WHAT is alerted on — thresholds, monitors,
# dashboards — belongs to the caller's own Datadog Terraform, never to the VM.

variable "enable_datadog" {
  description = <<-EOT
    Install the Datadog Agent as a pinned HOST package (apt, `apt-mark hold`)
    on every VM and ship host, disk, Docker, systemd, TLS, HTTP-health and
    Postgres side-car checks to Datadog. Off by default: leaving it unset
    installs nothing, grants nothing and starts nothing.

    One caveat, so nobody is promised an empty apply: picking up this module
    version labels the data disk and the static IP with the module's own four
    keys even with Datadog off, because those two resources carried no labels
    at all before `extra_labels` existed. Metadata-only, in place, nothing
    recreated — but it is a diff, and it is the only one an opt-out bump has.

    Three things worth knowing before turning it on:

      * The agent is installed by the startup script, which only runs on boot
        (`lifecycle.ignore_changes = [metadata_startup_script]`). Enabling this
        on a RUNNING VM produces the IAM binding and the labels but no agent —
        the instance must be recreated:
        `terraform apply -replace='module.<name>.google_compute_instance.vm["<vm>"]'`.

      * `dd-agent` joins the `docker` group so the agent can read the daemon's
        container metrics. That is root-equivalent on this host — the same
        posture the module already accepts for `agnes-applier`. The rendered
        `datadog.yaml` compensates: remote configuration, APM, DogStatsD,
        process/container/discovery collection, runtime security, compliance,
        SBOM, image and lifecycle collection and both inventory uploads are all
        off, and the IPC endpoint binds to loopback. Log collection is NOT on
        that list any more — it is off unless container_logs_destination
        resolves to `datadog`, which it does by default whenever this variable
        is true. That is an egress decision rather than a privilege one; see
        that variable and docs/datadog-logging.md.

      * No thresholds live on the VM. The checks report; the caller's monitors
        decide what is an incident.
  EOT
  type        = bool
  default     = false
}

variable "datadog_api_key_secret" {
  description = <<-EOT
    Secret Manager secret NAME (not the value) holding a Datadog API key used
    ONLY by the agent. Required when enable_datadog is true; the module grants
    the VM service account secretAccessor on exactly this one secret.

    Deliberately NOT routed through runtime_secret_env: that path writes the
    value into /opt/agnes/.env, which every container reads via env_file. This
    key is fetched at boot and written only into /etc/datadog-agent/datadog.yaml
    (root:dd-agent, 0640). It never enters .env, argv, or Terraform state.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.datadog_api_key_secret == "" || can(regex("^[A-Za-z0-9_-]{1,255}$", var.datadog_api_key_secret))
    error_message = "datadog_api_key_secret must be a Secret Manager secret NAME (letters, digits, _ and -), not a secret value or a full resource path."
  }
}

variable "datadog_site" {
  description = "Datadog site the agent reports to. Must match the site the caller's API key belongs to — a key from a different site authenticates but the metrics land in an org nobody is looking at."
  type        = string
  default     = "datadoghq.com"

  validation {
    condition = contains([
      "datadoghq.com",
      "datadoghq.eu",
      "us3.datadoghq.com",
      "us5.datadoghq.com",
      "ap1.datadoghq.com",
      "ap2.datadoghq.com",
      "ddog-gov.com",
    ], var.datadog_site)
    error_message = "datadog_site must be one of the documented Datadog sites (datadoghq.com, datadoghq.eu, us3/us5/ap1/ap2.datadoghq.com, ddog-gov.com)."
  }
}

variable "datadog_env" {
  description = "Value of the `env` tag applied to everything the agent emits, and the dimension every caller-side monitor scopes on. Empty (default) = the GCP project id, which is unique per deployment and is what the module's GCE labels can carry too. Never scope monitors by hostname: on GCE the agent reports the metadata FQDN, not the bare instance name."
  type        = string
  default     = ""

  validation {
    condition     = var.datadog_env == "" || can(regex("^[a-z0-9][a-z0-9._:/-]{0,199}$", var.datadog_env))
    error_message = "datadog_env must be a lowercase Datadog tag value."
  }
}

variable "datadog_agent_version" {
  description = "Exact datadog-agent package version to install and hold. Pinned rather than tracking `latest` so a boot never silently changes what is collecting; a newer version reaches a running VM only through a recreate."
  type        = string
  default     = "7.82.3"

  validation {
    condition     = can(regex("^7\\.[0-9]+\\.[0-9]+$", var.datadog_agent_version))
    error_message = "datadog_agent_version must be an exact Agent 7 version, e.g. 7.82.3."
  }
}

variable "datadog_extra_tags" {
  description = "Additional host tags (key:value) applied to everything the agent emits, on top of the module's own customer/app/service/role/agnes_instance/managed set."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for t in var.datadog_extra_tags : can(regex("^[a-z][a-z0-9._/-]*:[a-z0-9][a-z0-9._:/-]*$", t))])
    error_message = "Each entry of datadog_extra_tags must be a lowercase key:value tag."
  }

  validation {
    condition     = alltrue([for t in var.datadog_extra_tags : length(t) <= 200])
    error_message = "A Datadog tag is capped at 200 characters."
  }

  validation {
    # The rendered datadog.yaml is base64'd into the single startup-script
    # metadata value, which GCE caps at 256 KiB. A bound here fails the plan
    # instead of failing instance creation.
    condition     = length(var.datadog_extra_tags) <= 50
    error_message = "datadog_extra_tags is capped at 50 entries."
  }

  validation {
    # `env` is the top-level key of datadog.yaml and the dimension every monitor
    # scopes on; the rest are the module's own identity tags. A second value for
    # one of them does not error in Datadog — it silently gives the host two,
    # which is worse than an error.
    condition = alltrue([
      for t in var.datadog_extra_tags :
      !contains(["env", "customer", "app", "service", "role", "agnes_instance", "managed"], split(":", t)[0])
    ])
    error_message = "datadog_extra_tags must not redefine a module-owned tag key (env, customer, app, service, role, agnes_instance, managed) — use datadog_env for the env dimension."
  }
}

variable "extra_labels" {
  description = <<-EOT
    Additional GCE labels merged into the VM, the data disk and the static IP.
    The module's own keys (app, customer, role, managed) always win, so a
    caller cannot accidentally re-label a VM out from under the log filters and
    cron selectors that key off them.

    Independent of Datadog, but this is what makes the Datadog `env` dimension
    reachable from the GCP side too: pass `{ env = var.gcp_project_id }` and
    the same string identifies the deployment in both consoles.

    NOTE: the data disk and the static IP carried NO labels before this input
    existed, so the first apply after picking up this module version labels
    them with the module's four keys even when this map is empty. That is a
    metadata-only, in-place update on both resources — nothing is recreated —
    but it is a non-empty plan on a bump that otherwise has none.
  EOT
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for k in keys(var.extra_labels) : can(regex("^[a-z][a-z0-9_-]{0,62}$", k))])
    error_message = "GCE label KEYS must start with a lowercase letter and contain only lowercase letters, digits, - and _ (max 63 chars)."
  }

  validation {
    condition     = alltrue([for v in values(var.extra_labels) : can(regex("^[a-z0-9_-]{0,63}$", v))])
    error_message = "GCE label VALUES may contain only lowercase letters, digits, - and _ (max 63 chars) — a value with a dot or an uppercase letter is rejected by the GCE API at apply time, not at plan time."
  }

  validation {
    # GCE caps a resource at 64 labels and the module spends four of them, so
    # more than 60 here plans cleanly and then fails on the VM, the disk or the
    # address.
    condition     = length(var.extra_labels) <= 60
    error_message = "extra_labels is capped at 60 entries: GCE allows 64 labels per resource and the module adds four of its own."
  }
}
