# Datadog host monitoring for a customer-instance VM — design

**Status:** approved design, 2026-09-03. Implementation plan:
[`../plans/2026-09-03-datadog-host-monitoring.md`](../plans/2026-09-03-datadog-host-monitoring.md).

## Problem

A VM provisioned by `infra/modules/customer-instance/` has no working operational
monitoring:

- The module's GCP uptime check probes `http://<ip>:8000/api/health`, but on every
  `tls_mode = "caddy"` VM port 8000 is firewalled (`web_raw` opens it only for the
  `-rawhttp` tag) and the alert policy ships with `notification_channel_ids = []`. It fails
  permanently and notifies nobody. It stays untouched by this design; a consumer may keep
  or disable it.
- The on-VM watchdog (`files/agnes-watchdog.sh`) knows the incident signatures that matter
  most on this stack — DuckDB crash loops, the invalidated-database "zombie" state in which
  `/api/health` keeps answering 200 while every write fails, WAL salvage, cgroup OOM kills,
  `/data` at 85 % — but ships them only to journald and an optional webhook.
- Nothing watches the boot disk (`/var/lib/docker` images, unrotated `/var/log/agnes-*.log`),
  inodes, host CPU/memory, systemd unit failures, the `postgres`/`caddy`/`kai-agent`
  containers, or TLS certificate expiry.
- `/api/health` is **always HTTP 200**; only the JSON body (`"status": "ok" | "unhealthy"`)
  distinguishes a healthy instance. Any HTTP probe must assert on the body. `/readyz` is
  the only endpoint whose status code (200/503) carries meaning.

Scope: operational parameters of the VM, its Postgres side-cars (`postgres`,
`kai-agent-pg`), Docker containers, disks, TLS, host jobs. Out of scope: APM, traces, log
collection into Datadog (container logs keep going to Cloud Logging via the Ops Agent),
managed databases outside the VM.

## Decisions

1. **Host-installed Datadog Agent (apt, exact version pin + `apt-mark hold`) installed by
   the module startup script**, in a block adjacent to the `google-cloud-ops-agent` block —
   i.e. BEFORE `docker compose up`. The compose section ends in `exit 1` on a failed
   `up`; a block placed after it would be skipped on exactly the boot that needs
   monitoring. Rejected: shipping the Agent as a compose overlay in the app image.
   `CONFIG_FILES` in `scripts/ops/agnes-auto-upgrade.sh` is a hardcoded array (no
   auto-distribution), the overlay resolver deliberately never reads `.env`, `.env` sits
   outside the config-drift hash, the API key would land in a file every container's
   `env_file` reads, and a containerised agent shares fate with dockerd and cannot report
   `docker.service_up`.
2. **The API key is a Secret Manager secret NAME** (`datadog_api_key_secret`). The startup
   script fetches it with the silent form (`… 2>/dev/null || echo ""`): a missing or
   unreadable secret degrades to "no agent this boot" with a WARNING, never a failed boot.
   The value is written only to `/etc/datadog-agent/datadog.yaml` (`root:dd-agent`, `0640`)
   through a heredoc — never into `/opt/agnes/.env`, never on argv, never in Terraform
   state. The module grants the VM service account `secretAccessor` on that one secret and
   adds the binding to the VM's `depends_on`. It is deliberately NOT routed through
   `runtime_secret_env`, which lands in `.env`.
3. **`env` tag = GCP project id.** `datadog_env` defaults to `var.gcp_project_id`, is
   written as the top-level `env:` key of `datadog.yaml` (applied to every series the agent
   emits) and, through a new `extra_labels` module input, as a GCE label on the VM, the
   data disk and the static IP. Consumer roots tag every Datadog resource with the same
   `env:<project-id>` and scope every monitor by it. Never scope by hostname: on GCE the
   agent's hostname is the metadata FQDN (`<instance>.c.<project-id>.internal`), not the
   bare instance name.
4. **Static check configs ship as explicit `filebase64()` locals** from a new
   `files/datadog/` directory. The existing `agnes-*` fileset is decoded inside
   `%{ if enable_watchdog }` and would not deliver them.
5. **Flat `enable_datadog` / `datadog_*` module variables**, defaults off, site default
   `datadoghq.com` (a consumer overrides, e.g. to `datadoghq.eu`). Matches the existing
   module-global toggles (`enable_watchdog`, `enable_gcp_logging`, `alert_webhook_url`).
6. **Compose-service dimension** via `container_labels_as_tags:
   {com.docker.compose.service: compose_service}` (`docker_labels_as_tags` is deprecated).
   `docker.containers.running` is aggregated per image (app/scheduler share one), so
   per-service signals use `container.uptime` / `container.memory.*`.
   `docker.container_health` does not exist in Agent 7 — app health comes from `/readyz`.
7. **dd-agent joins the `docker` group** (root-equivalent on the host, the same posture the
   module already accepts for `agnes-applier`). Compensating controls in `datadog.yaml`:
   remote configuration, APM, logs, DogStatsD, process/container/process-discovery
   collection, runtime security, compliance, SBOM, container image/lifecycle collection
   and both inventories uploads are OFF; IPC binds to loopback; container env vars are
   never turned into tags.
8. **The feature reaches a RUNNING VM only through `terraform apply -replace`** of the
   instance (`lifecycle { ignore_changes = [metadata_startup_script] }`). Thresholds and
   monitors live in the consumer root and never touch the VM; only check-config changes
   need a recreate.
9. **Postgres side-cars via file-based Autodiscovery** (`ad_identifiers: [postgres]`): one
   template covers every `postgres:*` container of the compose project. A root-owned
   systemd timer (`agnes-datadog-pg-role.timer`) idempotently creates a `datadog` role with
   `pg_monitor` in each side-car, feeding the SQL to `psql` entirely on stdin, and renders
   the password into the check config. Running it as a timer decouples the role bootstrap
   from boot ordering (the agent block runs before compose is up) and converges after a
   side-car volume recreate.
10. **Watchdog bridge = marker files.** The watchdog writes `date +%s` to
    `$STATE/markers/<signature>` (= `/var/lib/agnes-watchdog/markers/`) on every alert it
    computes, from inside `add()` and therefore before its hourly anti-spam gate; the Agent
    `directory` check reports `system.disk.directory.file.modified_sec_ago` per marker, and one
    monitor fires while a marker is fresher than 15 minutes. No DogStatsD, no custom metrics.
    A subdirectory of the watchdog's own state dir rather than a sibling of it, for two reasons:
    the markers then hold nothing but signatures (the check's `*` glob would otherwise pick up
    the script's run-to-run delta files), and the existing bash harness sandboxes host paths by
    rewriting the `STATE=` assignment, which relocates the markers for free. The slug is an
    explicit second argument to `add()` rather than derived from the message text, so rewording
    an alert never renames a metric; the `[container]` qualifier is deliberately not part of it,
    which keeps the marker set at 14 signatures instead of 14 x N containers. There are 14 alert
    sites, one more than this design listed: `CONTAINER: no agnes role containers found` shares
    an anti-spam prefix with the per-container down alert but is a materially different incident,
    so it gets its own slug (`fleet-empty`).
11. **Heartbeats as file mtimes**: the auto-upgrade cron line touches
    `/var/lib/agnes/auto-upgrade.tick` on every tick (unconditional, vendor-neutral); the
    state applier already touches `/data/state/agnes-state-applier.tick`; the daily backup
    writes a dated directory. ACLs (`setfacl -m u:dd-agent:rx`) make them readable.
12. **Notification tiers, no paging by default.** "Important" monitors carry the consumer's
    Slack handle, `renotify_interval = 0`; "info" monitors carry no handle and surface only
    on the dashboard's monitor summary. Only `host_down` and the external synthetic notify
    on missing data, so a dead VM produces two messages, not twenty No-Data groups.
13. **The dashboard is the primary deliverable** of the consumer side; monitors are the
    alarm bells, the dashboard is where the answer is read.

## What runs on the VM

| Check | Config | Purpose |
|---|---|---|
| core (cpu, memory, load, uptime, io, network) | default | host parameters |
| `disk` | `use_mount: true`, `service_check_rw: true`, docker overlay/tmpfs excluded | `device:/` and `device:/data` usage + inodes; a read-only remount of `/data` surfaces as a failed RW check |
| `docker` + `container` | events on, `unbundle_events: true` | daemon up, containers running, per-service uptime/cpu/memory/OOM |
| `systemd` | agnes timers + `docker.service` + `cron.service`; `substate_status_mapping` for the oneshot backup unit | failed units; backup failure named by unit |
| `directory` | four instances tagged `agnes_probe:<state_applier\|auto_upgrade\|backup\|watchdog>` | heartbeat ages, newest backup age, watchdog markers. The backup instance is recursive with `pattern: '*/STATUS'`, because the daily backup writes dated SUBDIRECTORIES: a flat walk finds no file at all, and a bare `STATUS` matches nothing either — the check fnmatches the full path and the path relative to `directory`, never the basename |
| `http_check` | `agnes_readyz` (200), `agnes_health_body` (`content_match` on `"status": "ok"`), `agnes_acme_http` (port-80 redirect) | app readiness, health body, ACME HTTP-01 reachability. The templates take a ready-made `base_url` / `acme_hosts` / `hosts` rather than `domain` + `tls_mode`: the "does this VM terminate TLS" policy stays in HCL and the templates stay dumb renderers |
| `tls` | one instance per public hostname (domain + alias) | certificate expiry, incl. the alias whose ACME account has no contact e-mail |
| `postgres` | Autodiscovery on `postgres` images, `dbm: false` | `postgres.can_connect`, connections vs `max_connections`, database size, XID wraparound |

## Consumer-side catalogue (customer-agnostic module `datadog-monitors`)

Names follow `<env> - Agnes - <signal>`; scope `S` = `env:<project-id>`.

| id | tier | query |
|---|---|---|
| host_down | important | `"datadog.agent.up".over("S").by("host").last(2).count_by_status()`, `notify_no_data`, `no_data_timeframe 10` |
| synthetic_health | important | `datadog_synthetics_test` GET `https://<domain>/api/health`, `statusCode is 200` + `body validatesJSONPath $.status is ok`, 2 locations, `min_location_failed = 2`, `retry {2, 5000 ms}`, `min_failure_duration 300` |
| docker_daemon_down | important | `"docker.service_up".over("S").by("host").last(3).count_by_status()` |
| pg_unreachable (per side-car) | important | `"postgres.can_connect".over("S","compose_service:<svc>").by("host").last(3).count_by_status()` |
| pg_xid_wraparound | important | `max(last_30m):max:postgresql.percent_towards_wraparound{S} by {host,compose_service,db} > 70` (warn 50) |
| watchdog_signature | important | `min(last_10m):min:system.disk.directory.file.modified_sec_ago{S,agnes_probe:watchdog} by {host,filename} < 900` |
| containers_below_expected | important | `max(last_10m):max:docker.containers.running.total{S} by {host} < <expected>` |
| container_oom_killed | important | `max(last_15m):diff(max:container.memory.oom_events{S} by {host,compose_service}) > 0` |
| disk_full | important | `avg(last_10m):avg:system.disk.in_use{S AND (device:/ OR device:/data)} by {host,device} > 0.85` (warn 0.75) |
| tls_cert_expiring | important | `min(last_1h):min:tls.days_left{S} by {host,tls_target} < 10` (warn 21) |
| edge_readyz_failed | important | `"http.can_connect".over("S","instance:agnes_readyz").by("host").last(3).count_by_status()` |
| db_backup_failed | important | `"systemd.unit.substate".over("S","unit:agnes-db-backup.service").by("host","unit").last(1).count_by_status()` |
| pg_connections | info | `avg(last_10m):max:postgresql.percent_usage_connections{S} by {host,compose_service} > 0.85` (warn 0.70) |
| container_restart_loop | info | `max(last_30m):max:container.uptime{S} by {host,compose_service} < 600` |
| inodes_exhausted | info | `avg(last_15m):avg:system.fs.inodes.in_use{S AND (device:/ OR device:/data)} by {host,device} > 0.9` |
| memory_low | info | `avg(last_10m):avg:system.mem.pct_usable{S} by {host} < 0.10` (warn 0.15) |
| edge_checks_failed | info | `"http.can_connect".over("S").exclude("instance:agnes_readyz").by("host","instance").last(3).count_by_status()` |
| systemd_units_failed | info | `max(last_15m):max:systemd.units_by_state{S,state:failed} by {host} > 0` |
| heartbeat_stale (×3) | info | `max(last_5m):min:system.disk.directory.file.modified_sec_ago{S,agnes_probe:<k>} by {host} > <crit>` (state_applier 300, auto_upgrade 1800, backup 93600) |
| agent_check_error | info | `"datadog.agent.check_status".over("S").by("host","check").last(3).count_by_status()` |

Deliberately not in the first iteration: load, NTP, host reboot, edge latency, per-table
dead rows, deadlocks, disk forecast (needs a week of data), a second synthetic,
per-service No-Data monitors.

Query rules that were verified against the API: the boolean scope form
`{S AND (device:/ OR device:/data)}` is mandatory (comma-mixed `IN (…)` with `/` values
is rejected); `.as_count()` only on count/rate metrics; `diff()` on cumulative gauges such
as `container.memory.oom_events`; service-check monitors take `notify_no_data`, never
`on_missing_data`; `no_data_timeframe` cannot be combined with `on_missing_data`;
`postgres.can_connect` (not `postgresql.can_connect`); synthetics `retry.interval` is in
milliseconds, max 5000.

Dashboard groups: status (monitor summary + check-status tiles), host, disks, containers,
Postgres side-cars, edge (HTTP response time, TLS days left), ops jobs and watchdog.

## Cost and security notes

- One infrastructure host; containers within the per-host allotment; integration
  metrics only (no custom metrics, no logs, no APM, no DBM). One API synthetic from two
  locations every 5 minutes is on the order of 17k runs a month.
- Three credentials with three blast radii: an agent-only API key (VM service account
  reads it at boot), a Terraform API key and an application key bound to a scoped Datadog
  service account (read by the deploy identity at plan time, like the other Secret
  Manager data sources in a consumer root).
- Outbound HTTPS to the Datadog site only; no inbound listeners beyond loopback IPC.
- A compromised Datadog org has no path back to the VM: remote configuration is off.

## Follow-ups

- logrotate for `/var/log/agnes-*.log` and `json-file` limits in the prod overlay.
- Snapshot-policy health via a GCP log-based alert (no Datadog metric exists).
- Fix or drop the module's GCP uptime check for TLS VMs.
- A Renovate manager for the agent version pin.
- A kai-agent readiness probe once its health path is known.

## Implementation notes (2026-09-03)

Four things the implementation settled differently from the design above, each
verified rather than assumed:

- **No `setfacl`, and nothing else touches a shared directory's mode either.**
  The design assumed dd-agent would need ACLs to reach the heartbeat paths. It
  does not: `/data/state` and `/data/backups` are already mode 0755 (the secrets
  inside them are individually 0600), so the `directory` check can stat the tick
  files with no permission change at all. That removes the `acl` package
  dependency and a whole class of boot-time failure. The corollary took a review
  to surface: the Postgres monitoring password must NOT live in `/data/state`
  either, because `install -d -m 0700` applies its mode to an already-existing
  directory and would have made the shared state dir unreadable to dd-agent —
  silently, since the check's own `exists` probe only needs `+x` on `/data`. It
  lives at `/var/lib/agnes/datadog/pg-password`, a directory this feature owns;
  losing it to a VM recreate is free, because the timer rewrites the role's
  password on its next run anyway.
- **No `%` in a crontab command field.** cron turns an unescaped `%` into a
  newline and hands everything after it to the command as stdin, so the
  heartbeat is a `touch`, not `date +%s > file` — which would have run as
  `date +` and written nothing.
- **The `directory` check's `pattern` is not a basename match.** It fnmatches
  the file's full path and its path relative to `directory`, so the recursive
  backup probe needs `*/STATUS`; a bare `STATUS` matches zero files and reports
  no metric rather than an error.
- **`google_compute_address` does accept `labels`** at `hashicorp/google ~> 5.0`
  — confirmed with `terraform validate` against the pinned provider, which this
  design had flagged as unverified.
- **Two `%{ if }` blocks, not `%{ if } / %{ else }`.** The startup template uses
  no `%{ else ~}` anywhere; a stray whitespace difference from one would show up
  as a diff on every existing consumer, against the "a bump alone is an empty
  diff" invariant.
- **The install steps chain with `&&` inside the tolerance subshell.** `set -e`
  is suppressed inside a command that is part of an `||` list — including a
  subshell, and including one that re-runs `set -e` itself (verified in bash
  5.3) — so `( set -e; step1; step2 ) || warn` would run `step2` after `step1`
  failed. The existing Ops Agent block's `&&` chain is the pattern that works.

The template renderer the new tests use (`tests/_tf_template.py`) reproduces
`terraform console`'s `templatefile()` byte-for-byte on the full 1179-line
startup script, including the `~}` trim rule — it eats the following spaces and
tabs plus at most one newline, not the whole whitespace run. That is what lets
those tests assert on what actually boots rather than on template source text.
