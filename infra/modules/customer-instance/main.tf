terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }
}

locals {
  # Normalize all instances into a single list so for_each is uniform across prod + dev.
  # Note: merge({defaults}, d) — d overrides defaults (fix for v1.3.0 bug where
  # defaults overrode user-supplied values).
  dev_defaults = {
    role         = "dev"
    disk_size_gb = 30
    data_disk_gb = 20
    upgrade_mode = "auto"
    tls_mode     = "none" # dev VMs default to plain HTTP; TLS requires domain
    domain       = ""
    domain_alias = ""
    ui_layout    = ""
    theme        = ""
  }
  all_instances = concat(
    [merge(var.prod_instance, { role = "prod" })],
    [for d in var.dev_instances : merge(local.dev_defaults, d)]
  )

  # Watchdog + DB-backup artifacts shipped to every VM via the startup
  # script (base64 — metadata is a single string, so files can't ride
  # along any other way). fileset() means a new file under files/ is
  # delivered automatically; the install/enable lines in the template
  # stay explicit per artifact.
  watchdog_files_b64 = {
    for f in fileset("${path.module}/files", "agnes-*") :
    f => filebase64("${path.module}/files/${f}")
  }
  # Per-VM OAuth (Sign-in with Google) secret names, derived from
  # var.oauth_secret_name_template. Empty template -> empty map ->
  # startup-script falls back to legacy `google-oauth-client-{id,secret}`.
  #
  # The {kind}/{role}/{name} substitution is done in HCL so the resolved
  # names are visible to the IAM resource (it needs the literal strings to
  # set secretAccessor on each).
  per_vm_oauth = var.oauth_secret_name_template == "" ? {} : {
    for inst in local.all_instances : inst.name => {
      id = replace(replace(replace(
        var.oauth_secret_name_template,
        "{kind}", "id"),
        "{role}", inst.role),
        "{name}", inst.name
      )
      secret = replace(replace(replace(
        var.oauth_secret_name_template,
        "{kind}", "secret"),
        "{role}", inst.role),
        "{name}", inst.name
      )
    }
  }
  # Deduplicated union — multiple VMs with the same role collapse to the
  # same name when the template uses {role}, and the toset() makes the
  # IAM resource for_each idempotent across those collisions.
  per_instance_oauth_secrets = toset(flatten([
    for k, v in local.per_vm_oauth : [v.id, v.secret]
  ]))

  # Opt-in LLM dispatcher (token-arbitrage PoC): secrets the VM SA needs
  # read access to when ANY instance in this module call enables it. The
  # flag is per-VM (dev-first rollouts), the config is module-wide.
  dispatcher_any_enabled = anytrue([for inst in local.all_instances : inst.dispatcher_enabled])
  dispatcher_secrets = local.dispatcher_any_enabled ? toset(compact([
    var.dispatcher_key_secret,
    var.dispatcher_vertex_sa_secret,
  ])) : toset([])

  # Opt-in embedded kai-agent turn engine: same shape as the dispatcher —
  # per-VM flag, module-wide config, secretAccessor only when some instance
  # actually enables it. Secrets already granted through runtime_secret_env
  # OR runtime_secrets are subtracted: a caller reusing one of the app's own
  # secrets for the engine would otherwise declare the same (project, secret,
  # role, member) IAM binding twice and the second apply errors with
  # "already exists" — the same duplicate-binding trap
  # oauth_secret_name_template documents.
  kai_agent_any_enabled = anytrue([for inst in local.all_instances : inst.kai_agent_enabled])
  kai_agent_secrets = local.kai_agent_any_enabled ? setsubtract(toset(compact([
    var.kai_agent_jwt_secret,
    var.kai_agent_e2b_key_secret,
  ])), setunion(toset(keys(var.runtime_secret_env)), toset(var.runtime_secrets))) : toset([])

  # --- Vendor-neutral per-instance branding -> /data/state/instance.yaml ---
  # The startup script seeds instance.yaml on FIRST boot only (it never clobbers
  # an existing file). These locals turn the optional per-VM branding fields into
  # the `instance:` + `theme:` YAML blocks the script appends. Assembled here —
  # not in the shell template — so yamlencode handles the multi-line SVG /
  # custom_scripts HTML escaping + indentation for us, and so an unset caller
  # collapses to "" (no bytes appended -> a byte-for-byte identical file).
  #
  # Each map keeps ONLY the keys the caller actually set (drop null + ""), so
  # partial branding (e.g. a logo but no theme colours) emits exactly its subset.
  #
  # D1 (config-ownership remediation, 2026-08): `theme` (the palette NAME —
  # "blue"/"paper"/etc., not the colour-override `theme:` block below) and
  # `experience` moved into this seed from an always-wins `.env` line the
  # startup script used to rewrite on EVERY boot, which permanently shadowed
  # the admin UI's `/admin/server-config` control of the same knob. Seeded
  # here instead: Terraform still sets the day-1 value, the admin UI owns it
  # from day 2 onward. Per-instance, like the branding fields above.
  # `home_route` is the same handoff but module-wide (one value for every VM,
  # mirroring its historical `var.home_route` env behavior) rather than
  # per-instance.
  instance_brand_scalars = {
    for inst in local.all_instances : inst.name => {
      for k, v in {
        logo_svg    = try(inst.logo_svg, "")
        brand       = try(inst.brand, "")
        brand_short = try(inst.brand_short, "")
        subtitle    = try(inst.subtitle, "")
        copyright   = try(inst.copyright, "")
        favicon     = try(inst.favicon, "")
        theme       = try(inst.theme, "")
        experience  = try(inst.experience, "")
        home_route  = var.home_route
      } : k => v if v != null && v != ""
    }
  }
  instance_theme_colors = {
    for inst in local.all_instances : inst.name => {
      for k, v in {
        primary        = try(inst.theme_colors.primary, null)
        primary_dark   = try(inst.theme_colors.primary_dark, null)
        primary_light  = try(inst.theme_colors.primary_light, null)
        background     = try(inst.theme_colors.background, null)
        surface        = try(inst.theme_colors.surface, null)
        border         = try(inst.theme_colors.border, null)
        text_primary   = try(inst.theme_colors.text_primary, null)
        text_secondary = try(inst.theme_colors.text_secondary, null)
        success        = try(inst.theme_colors.success, null)
        warning        = try(inst.theme_colors.warning, null)
        error          = try(inst.theme_colors.error, null)
        font_primary   = try(inst.theme_colors.font_primary, null)
        font_url       = try(inst.theme_colors.font_url, null)
        radius         = try(inst.theme_colors.radius, null)
      } : k => v if v != null && v != ""
    }
  }
  # D1: studio toggle — same first-boot-seed handoff as theme/experience/
  # home_route above, module-wide (studio_enabled applies uniformly to every
  # VM, mirroring its historical `var.studio_enabled` env behavior). Emitted
  # only when explicitly TRUE, and the variable defaults to false: the app's
  # own default flipped to false when the admin cleanup retired the Studio
  # surface, so a non-customized instance's seed stays empty — the rule is
  # "seed nothing when the request matches the app default", and this
  # condition inverted with that default. Leaving it as
  # `var.studio_enabled ? {} : {enabled = false}` would have seeded nothing
  # for an operator asking for `true` and quietly given them a disabled
  # Studio.
  instance_studio_map = var.studio_enabled ? { enabled = true } : {}
  # D1 residual (2026-08): `data_source.type` — the last knob still rewritten
  # by an always-wins `.env` line (`DATA_SOURCE=...`) on every boot, which
  # permanently shadowed the admin UI's `/admin/server-config` control of the
  # same knob (app/instance_config.py::get_data_source_type() now also flips
  # its own precedence to prefer this overlay over a stale env var). Same
  # first-boot-seed handoff as studio_enabled above — module-wide, one value
  # for every VM, mirroring its historical `var.data_source` env behavior.
  instance_data_source_map = var.data_source != "" ? { type = var.data_source } : {}
  # Assemble the top-level YAML map, dropping empty sub-blocks: `instance:` gets
  # the scalar branding keys plus custom_scripts (verbatim); `theme:` gets the
  # set colours; `studio:` gets the toggle above; `data_source:` gets the
  # connector type. base64(yamlencode(...)) — but only when the map is
  # non-empty, so a no-branding VM resolves to "" and the startup script
  # appends nothing.
  instance_branding_map = {
    for inst in local.all_instances : inst.name => merge(
      (length(local.instance_brand_scalars[inst.name]) > 0 || length(try(inst.custom_scripts, [])) > 0) ? {
        instance = merge(
          local.instance_brand_scalars[inst.name],
          length(try(inst.custom_scripts, [])) > 0 ? { custom_scripts = inst.custom_scripts } : {}
        )
      } : {},
      length(local.instance_theme_colors[inst.name]) > 0 ? { theme = local.instance_theme_colors[inst.name] } : {},
      length(local.instance_studio_map) > 0 ? { studio = local.instance_studio_map } : {},
      length(local.instance_data_source_map) > 0 ? { data_source = local.instance_data_source_map } : {}
    )
  }
  instance_branding_b64 = {
    for name, m in local.instance_branding_map :
    name => length(m) > 0 ? base64encode(yamlencode(m)) : ""
  }
}

# --- Secrets ---

resource "google_secret_manager_secret" "jwt" {
  secret_id = "agnes-${var.customer_name}-jwt-secret"
  project   = var.gcp_project_id
  replication {
    auto {}
  }
}

resource "random_password" "jwt" {
  length  = 48
  special = false
}

resource "google_secret_manager_secret_version" "jwt" {
  secret      = google_secret_manager_secret.jwt.id
  secret_data = random_password.jwt.result
}

# SESSION_SECRET — signs the app's session cookies (app/secrets.py::get_session_secret).
# Single-node deployments fall back to a per-node generated-and-persisted file, but a
# role-split (multi-process) deployment needs every process to agree on the same value,
# so this module provisions it the same way as the JWT secret: minted once here, fetched
# fresh from Secret Manager on every boot (no on-VM fallback generation).
resource "google_secret_manager_secret" "session" {
  secret_id = "agnes-${var.customer_name}-session-secret"
  project   = var.gcp_project_id
  replication {
    auto {}
  }
}

resource "random_password" "session" {
  length  = 48
  special = false
}

resource "google_secret_manager_secret_version" "session" {
  secret      = google_secret_manager_secret.session.id
  secret_data = random_password.session.result
}

# Postgres password for the side-car postgres:16-alpine container.
# Startup-script pulls this and writes POSTGRES_PASSWORD + DATABASE_URL
# into /opt/agnes/.env before docker compose up.
resource "google_secret_manager_secret" "postgres" {
  secret_id = "agnes-${var.customer_name}-postgres-password"
  project   = var.gcp_project_id
  replication {
    auto {}
  }
}

resource "random_password" "postgres" {
  length  = 32
  special = false # PG-friendly; avoids shell-quoting in startup-script
}

resource "google_secret_manager_secret_version" "postgres" {
  secret      = google_secret_manager_secret.postgres.id
  secret_data = random_password.postgres.result
}

# --- VM service account (dedicated, read-only on specific secrets only) ---

resource "google_service_account" "vm" {
  account_id   = "agnes-${var.customer_name}-vm"
  display_name = "Agnes VM runtime SA (${var.customer_name})"
  project      = var.gcp_project_id
}

# Grant read access only to the JWT secret this module owns.
# Not project-wide — if the customer adds unrelated secrets (e.g. Stripe key)
# to the same project, Agnes VM must NOT be able to read them.
resource "google_secret_manager_secret_iam_member" "vm_jwt" {
  project   = var.gcp_project_id
  secret_id = google_secret_manager_secret.jwt.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.vm.email}"
}

# Same scoped read access for the session secret — mirrors vm_jwt above.
resource "google_secret_manager_secret_iam_member" "vm_session" {
  project   = var.gcp_project_id
  secret_id = google_secret_manager_secret.session.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.vm.email}"
}

# Same scoped read access for the postgres password secret.
resource "google_secret_manager_secret_iam_member" "vm_postgres" {
  project   = var.gcp_project_id
  secret_id = google_secret_manager_secret.postgres.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.vm.email}"
}

# Grant read access to additional secrets the app needs (e.g. keboola-storage-token).
# Caller specifies these via var.runtime_secrets. Each secret must already exist.
resource "google_secret_manager_secret_iam_member" "vm_runtime" {
  for_each  = toset(var.runtime_secrets)
  project   = var.gcp_project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.vm.email}"
}

# Grant read access to secrets that get auto-injected as .env entries (Anthropic,
# Anthropic, Slack, etc.) per var.runtime_secret_env. The startup script
# iterates this map and writes one `<env_var>=$(gcloud secrets ...)` line
# per entry to /opt/agnes/.env.
resource "google_secret_manager_secret_iam_member" "vm_runtime_env" {
  for_each  = var.runtime_secret_env
  project   = var.gcp_project_id
  secret_id = each.key
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.vm.email}"
}

# Dispatcher secrets (API key + Vertex SA key JSON) — read-only, and only
# when some instance actually enables the dispatcher. Both secrets must
# already exist (created out-of-band, like the OAuth clients above).
resource "google_secret_manager_secret_iam_member" "vm_dispatcher" {
  for_each  = local.dispatcher_secrets
  project   = var.gcp_project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.vm.email}"
}

# kai-agent engine secrets (shared JWT secret + E2B key) — read-only, and only
# when some instance actually enables the engine. Both secrets must already
# exist (created out-of-band, like the dispatcher's above). A secret also
# mapped in runtime_secret_env is deliberately absent from this for_each (see
# the setsubtract in locals) — the grant already exists there, and declaring
# it twice errors the apply.
resource "google_secret_manager_secret_iam_member" "vm_kai_agent" {
  for_each  = local.kai_agent_secrets
  project   = var.gcp_project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.vm.email}"
}

# Per-VM OAuth client secrets, expanded from var.oauth_secret_name_template.
# Granted to the same shared VM SA (all VMs in this module call use one SA, so
# every per-VM OAuth secret is technically readable from every VM — isolation
# comes from distinct OAuth clients with different redirect URIs at Google's
# end, not from at-rest read scoping). A per-VM SA refactor is tracked
# separately. When the template is empty the local resolves to an empty set
# and this resource is a noop (legacy `google-oauth-client-{id,secret}` are
# still listed by the caller in var.runtime_secrets and bound there).
resource "google_secret_manager_secret_iam_member" "vm_oauth" {
  for_each  = local.per_instance_oauth_secrets
  project   = var.gcp_project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.vm.email}"
}

# Cloud Logging writer for Docker's gcplogs log driver
# (docker-compose.gcp-logging.yml, gated by var.enable_gcp_logging). The
# driver authenticates as the VM's service account — the dedicated SA above,
# attached to every instance below — and without this role it cannot
# initialize. Docker refuses to START a container whose log driver fails to
# initialize, so a VM running the overlay without the role goes fully down
# on its next routine container recreate (the auto-upgrade cron), not at
# provisioning time. The startup script's boot-time driver probe keeps such
# a VM alive by disabling the overlay with a loud warning, but logs then
# never reach Cloud Logging; this binding is what makes the default-on
# feature actually work. Unlike the secret grants above this one is
# project-level: roles/logging.logWriter only permits writing log entries,
# and the driver's log target has no narrower resource to bind on. The
# identity running `terraform apply` must be able to modify project IAM
# policy (e.g. roles/resourcemanager.projectIamAdmin) — if it cannot, the
# apply fails visibly here, which beats a latent VM that dies on a cron
# tick; either grant the role out-of-band or set enable_gcp_logging=false.
resource "google_project_iam_member" "vm_log_writer" {
  count   = var.enable_gcp_logging ? 1 : 0
  project = var.gcp_project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.vm.email}"
}

# --- Network ---

# Web firewall: 80/443 for Caddy (TLS), 8000 only when TLS is disabled (direct HTTP).
# Separate rule for SSH (port 22) — default restricted to IAP tunnel range.
locals {
  # Security audit F12: raw :8000 must be exposed PER-INSTANCE, gated on THAT
  # VM's own tls_mode — never fleet-wide. The previous `anytrue(...)` over a
  # shared tag opened :8000 on the TLS-fronted prod VM as soon as any dev VM
  # defaulted to tls_mode="none", letting an on-path attacker reach the prod
  # app in cleartext on :8000. The shared web rule now only carries 80/443; a
  # separate rule opens 8000 solely for VMs tagged raw-http (see below).
  raw_http_instance_names = [for inst in local.all_instances : inst.name if inst.tls_mode != "caddy"]
  expose_raw_http_port    = length(local.raw_http_instance_names) > 0
  raw_http_tag            = "agnes-${var.customer_name}-rawhttp"
}

resource "google_compute_firewall" "web" {
  name    = "agnes-${var.customer_name}-allow-web"
  project = var.gcp_project_id
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["agnes-${var.customer_name}"]
}

# Raw :8000 — only VMs whose OWN tls_mode != "caddy" carry the raw-http tag, so
# a TLS-fronted (caddy) VM never exposes plaintext :8000 even when a sibling dev
# VM does. Created only when at least one instance needs it.
resource "google_compute_firewall" "web_raw" {
  count   = local.expose_raw_http_port ? 1 : 0
  name    = "agnes-${var.customer_name}-allow-web-raw"
  project = var.gcp_project_id
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["8000"]
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = [local.raw_http_tag]
}

resource "google_compute_firewall" "ssh" {
  name    = "agnes-${var.customer_name}-allow-ssh"
  project = var.gcp_project_id
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  source_ranges = var.firewall_ssh_source_ranges
  target_tags   = ["agnes-${var.customer_name}"]
}

# --- Backup policy: daily snapshot with 30-day retention ---

resource "google_compute_resource_policy" "daily_backup" {
  name    = "agnes-${var.customer_name}-daily-backup"
  project = var.gcp_project_id
  region  = var.region

  snapshot_schedule_policy {
    schedule {
      daily_schedule {
        days_in_cycle = 1
        start_time    = "02:00"
      }
    }
    retention_policy {
      max_retention_days    = 30
      on_source_disk_delete = "KEEP_AUTO_SNAPSHOTS"
    }
    snapshot_properties {
      labels = {
        app      = "agnes"
        customer = var.customer_name
      }
    }
  }
}

# --- Persistent data disks + VMs (prod + dev) ---

resource "google_compute_disk" "data" {
  for_each = { for inst in local.all_instances : inst.name => inst }

  name    = "${each.value.name}-data"
  project = var.gcp_project_id
  zone    = var.zone
  size    = each.value.data_disk_gb
  type    = "pd-ssd"
}

# Attach daily backup policy to data disks (boot disks are ephemeral,
# app code lives in the image so no need to snapshot them)
resource "google_compute_disk_resource_policy_attachment" "data_backup" {
  for_each = { for inst in local.all_instances : inst.name => inst }

  project = var.gcp_project_id
  zone    = var.zone
  disk    = google_compute_disk.data[each.key].name
  name    = google_compute_resource_policy.daily_backup.name
}

resource "google_compute_address" "ip" {
  for_each = { for inst in local.all_instances : inst.name => inst }

  name    = "${each.value.name}-ip"
  project = var.gcp_project_id
  region  = var.region
}

resource "google_compute_instance" "vm" {
  for_each = { for inst in local.all_instances : inst.name => inst }

  name         = each.value.name
  project      = var.gcp_project_id
  machine_type = each.value.machine_type
  zone         = var.zone
  # F12: the raw-http tag (opens :8000) is attached ONLY to VMs whose own
  # tls_mode != "caddy", so a caddy/TLS VM never exposes plaintext :8000.
  tags = concat(
    ["agnes-${var.customer_name}"],
    each.value.tls_mode != "caddy" ? [local.raw_http_tag] : [],
  )

  # Without this, a `machine_type` change in TF triggers a full
  # ForceNew (destroy + recreate) of the VM. The data disk would
  # survive (it's a separate `attached_disk`), but VM-local state
  # — fingerprints, journald, ephemeral caches — would not. With
  # `true`, the provider stops the VM, mutates the field, and
  # restarts it in place, which is what an operator resizing a
  # running deployment actually wants.
  allow_stopping_for_update = true

  # Opt-in per VM. Until this was wired, a deployment could only set the flag
  # out of band, and the provider's own `false` default silently reverted it on
  # the next apply — the module never sent the attribute, so every plan
  # proposed turning the protection back off.
  deletion_protection = each.value.deletion_protection

  boot_disk {
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
      size  = each.value.disk_size_gb
      type  = "pd-ssd"
    }
  }

  attached_disk {
    source      = google_compute_disk.data[each.key].self_link
    device_name = "data"
  }

  network_interface {
    network = "default"
    access_config {
      nat_ip = google_compute_address.ip[each.key].address
    }
  }

  metadata = {
    enable-oslogin = "TRUE"
  }

  metadata_startup_script = templatefile("${path.module}/startup-script.sh.tpl", {
    customer_name       = var.customer_name
    image_repo          = var.image_repo
    image_tag           = each.value.image_tag
    app_mem_limit       = each.value.app_mem_limit
    scheduler_mem_limit = each.value.scheduler_mem_limit
    app_cpus            = each.value.app_cpus
    scheduler_cpus      = each.value.scheduler_cpus
    upgrade_mode        = each.value.upgrade_mode
    upgrade_schedule    = each.value.upgrade_schedule
    tls_mode            = each.value.tls_mode
    domain              = each.value.domain
    domain_alias        = each.value.domain_alias
    # ui_layout is deliberately NOT forwarded: the rail chrome is
    # unconditional in the app (Wave 0, 2026-08), so there is no env line to
    # write. The variable stays declared + validated in variables.tf so a root
    # still asking for "topnav" fails the plan instead of silently getting rail.
    #
    # theme / experience are likewise NOT forwarded as separate template vars
    # (D1, 2026-08): they ride the `instance_branding_b64` first-boot seed
    # blob below instead of an always-wins `.env` line — see
    # `instance_brand_scalars` above.
    # Web-chat provider pin (AGNES_CHAT_PROVIDER) — "" writes no env line and
    # the instance follows instance.yaml / the app default. Unlike theme/
    # experience above, this ONE stays an env line: it pins deployment-
    # provisioned backing (the kai-agent sidecar / apps-runner), not a pure
    # presentation choice, so it is out of scope for the D1 handoff.
    chat_provider = each.value.chat_provider
    # Vendor-neutral branding for the FIRST-boot instance.yaml (logo/brand/
    # theme colours/custom_scripts), pre-rendered to a base64'd YAML fragment —
    # "" when the caller set no branding on this VM.
    instance_branding_b64 = local.instance_branding_b64[each.value.name]
    acme_email            = var.acme_email != "" ? var.acme_email : var.seed_admin_email
    # `data_source` still reaches the startup script (used ONLY for the
    # boot-time "do we need the keboola-storage-token secret?" gate) but no
    # longer writes a `DATA_SOURCE=...` line into `.env` (D1 residual,
    # 2026-08) — the app-visible value rides `instance_branding_b64`'s
    # `data_source.type` first-boot seed instead. See
    # `instance_data_source_map` above.
    data_source                     = var.data_source
    keboola_stack_url               = var.keboola_stack_url
    seed_admin_email                = var.seed_admin_email
    seed_admin_password             = var.enable_seed_password ? var.seed_admin_password : ""
    role                            = each.value.role
    oauth_client_id_secret_name     = try(local.per_vm_oauth[each.value.name].id, "")
    oauth_client_secret_secret_name = try(local.per_vm_oauth[each.value.name].secret, "")
    runtime_secret_env              = var.runtime_secret_env
    # home_route / studio_enabled are likewise NOT forwarded as separate
    # template vars (D1, 2026-08) — see the theme/experience note above; both
    # ride instance_branding_b64 now.
    data_apps_enabled            = each.value.data_apps_enabled
    data_apps_subdomain_base     = each.value.data_apps_subdomain_base
    data_apps_runtime_image      = var.data_apps_runtime_image
    enable_watchdog              = var.enable_watchdog
    enable_gcp_logging           = var.enable_gcp_logging
    alert_webhook_url            = var.alert_webhook_url
    watchdog_files_b64           = local.watchdog_files_b64
    dispatcher_enabled           = each.value.dispatcher_enabled
    dispatcher_image             = var.dispatcher_image
    dispatcher_key_secret        = var.dispatcher_key_secret
    dispatcher_vertex_sa_secret  = var.dispatcher_vertex_sa_secret
    dispatcher_policies_b64      = base64encode(var.dispatcher_policies)
    kai_agent_enabled            = each.value.kai_agent_enabled
    kai_agent_mem_limit          = each.value.kai_agent_mem_limit
    kai_agent_cpus               = each.value.kai_agent_cpus
    kai_agent_pg_mem_limit       = each.value.kai_agent_pg_mem_limit
    kai_agent_broker_mcp_enabled = each.value.kai_agent_broker_mcp_enabled
    kai_agent_image              = var.kai_agent_image
    kai_agent_jwt_secret         = var.kai_agent_jwt_secret
    kai_agent_e2b_key_secret     = var.kai_agent_e2b_key_secret
    # Rendered to KEY=VALUE lines, base64'd like dispatcher_policies so no
    # value can break the template or the shell heredoc quoting.
    kai_agent_env_b64 = base64encode(join("\n", [
      for k, v in var.kai_agent_env : "${k}=${v}"
    ]))
  })

  service_account {
    email  = google_service_account.vm.email
    scopes = ["cloud-platform"]
  }

  labels = {
    app      = "agnes"
    customer = var.customer_name
    role     = each.value.role
    managed  = "terraform"
  }

  # Startup script changes do not modify running VMs (script only runs on boot).
  # To propagate module changes, use:
  #   terraform apply -replace='module.agnes.google_compute_instance.vm["agnes-prod"]'
  lifecycle {
    ignore_changes = [metadata_startup_script]

    # Enabling the dispatcher without its module-wide config would boot a VM
    # whose startup script fails loudly halfway (missing secret name renders
    # an empty --secret= arg). Catch it at plan time instead.
    precondition {
      condition = !each.value.dispatcher_enabled || (
        var.dispatcher_image != "" &&
        var.dispatcher_policies != "" &&
        var.dispatcher_key_secret != "" &&
        var.dispatcher_vertex_sa_secret != ""
      )
      error_message = "dispatcher_enabled=true on instance ${each.value.name} requires dispatcher_image, dispatcher_policies, dispatcher_key_secret and dispatcher_vertex_sa_secret to be set on the module."
    }

    # Same plan-time catch for the kai-agent engine: an enabled VM without
    # the module-wide config would fail loudly halfway through boot — and an
    # enabled VM whose kai_agent_env is missing the keys the engine's own
    # env validation requires (HOST_AGENT_IDENTITY always; the
    # ANTHROPIC_UPSTREAM_* pair when CLOUD_LLM_PROVIDER=anthropic) would
    # plan and apply cleanly and then crash-loop at runtime — the same
    # silent-fallback class the dispatcher's policies check exists to catch.
    precondition {
      condition = !each.value.kai_agent_enabled || (
        var.kai_agent_image != "" &&
        var.kai_agent_jwt_secret != "" &&
        var.kai_agent_e2b_key_secret != "" &&
        contains(keys(var.kai_agent_env), "HOST_AGENT_IDENTITY") &&
        contains(keys(var.kai_agent_env), "CLOUD_LLM_PROVIDER") &&
        (lookup(var.kai_agent_env, "CLOUD_LLM_PROVIDER", "") != "anthropic" || (
          contains(keys(var.kai_agent_env), "ANTHROPIC_UPSTREAM_URL") &&
          contains(keys(var.kai_agent_env), "ANTHROPIC_UPSTREAM_API_KEY")
        ))
      )
      error_message = "kai_agent_enabled=true on instance ${each.value.name} requires kai_agent_image, kai_agent_jwt_secret and kai_agent_e2b_key_secret on the module, plus kai_agent_env carrying HOST_AGENT_IDENTITY and CLOUD_LLM_PROVIDER (and the ANTHROPIC_UPSTREAM_URL/ANTHROPIC_UPSTREAM_API_KEY pair when the provider is anthropic) — the engine's env validation refuses to boot without them."
    }
  }

  # Ensure VM SA has read access to required secrets BEFORE the VM boots — otherwise
  # the startup script's `gcloud secrets versions access` can 403 due to IAM lag.
  depends_on = [
    google_secret_manager_secret_iam_member.vm_jwt,
    google_secret_manager_secret_iam_member.vm_session,
    google_secret_manager_secret_iam_member.vm_runtime,
    google_secret_manager_secret_iam_member.vm_runtime_env,
    google_secret_manager_secret_iam_member.vm_oauth,
    google_secret_manager_secret_iam_member.vm_dispatcher,
    google_secret_manager_secret_iam_member.vm_kai_agent,
    google_secret_manager_secret_version.jwt,
    google_secret_manager_secret_version.session,
  ]
}

# --- Monitoring: uptime check on each VM's /api/health endpoint ---

resource "google_monitoring_uptime_check_config" "health" {
  for_each = var.enable_monitoring ? { for inst in local.all_instances : inst.name => inst } : {}

  project      = var.gcp_project_id
  display_name = "agnes-${var.customer_name}-${each.value.name}-health"
  timeout      = "10s"
  period       = "60s"

  http_check {
    path         = "/api/health"
    port         = "8000"
    use_ssl      = false
    validate_ssl = false
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.gcp_project_id
      host       = google_compute_address.ip[each.key].address
    }
  }
}

# --- Monitoring: alert when health fails for > 5 min ---

resource "google_monitoring_alert_policy" "health_failure" {
  for_each = var.enable_monitoring ? { for inst in local.all_instances : inst.name => inst } : {}

  project      = var.gcp_project_id
  display_name = "agnes-${var.customer_name}-${each.value.name}-health-failure"
  combiner     = "OR"

  conditions {
    display_name = "Uptime check failed > 5 min"
    condition_threshold {
      filter   = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND metric.labels.check_id=\"${google_monitoring_uptime_check_config.health[each.key].uptime_check_id}\" AND resource.type=\"uptime_url\""
      duration = "300s"
      # ALIGN_FRACTION_TRUE yields fraction of checks that returned true.
      # If the fraction stays < 1 (i.e. any probe failed) for 5 min → alert.
      comparison      = "COMPARISON_LT"
      threshold_value = 1

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_FRACTION_TRUE"
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = var.notification_channel_ids
  enabled               = true
}
