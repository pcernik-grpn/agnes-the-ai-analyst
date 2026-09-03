# Datadog Host Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Every decision is already made — there is no "ask the user" step in this plan.

**Goal:** Give a `customer-instance` VM opt-in, host-level Datadog monitoring — host, disks, Docker daemon and containers, the Postgres side-cars, TLS, health endpoints, host jobs and the watchdog's incident signatures — installed by the module, with the alerting and the dashboard owned by the consumer's private Terraform root.

**Architecture:** The public module (`infra/modules/customer-instance/`) installs a pinned Datadog Agent from apt in the startup script (before any docker work), renders `datadog.yaml` + `conf.d/*` from module-shipped artifacts on every boot, and bridges the watchdog through marker files. The consumer root wires the Datadog provider, a customer-agnostic local module `datadog-monitors` (monitors, one synthetic, one dashboard) and the tfvars values. Thresholds never live on the VM.

**Tech Stack:** Terraform (module + consumer root, DataDog/datadog provider ~> 4.20, Terraform ≥ 1.7 for `terraform test` with `mock_provider`), bash startup script, Datadog Agent 7.82.x, pytest for the template/artifact contract tests.

**Spec:** `docs/superpowers/specs/2026-09-03-datadog-host-monitoring-design.md`

## Global Constraints

- **Vendor-agnostic public repo.** Tasks 1–5 must contain no customer name, GCP project id, hostname, Slack channel or private-repo reference. Placeholders in this document (`<customer>`, `<project-id>`, `<domain>`, `<alias-domain>`, `@slack-<channel>`) are filled ONLY in the consumer's private repo (Task 6).
- **Defaults off.** `enable_datadog = false`; a module bump alone must produce an empty diff for every existing consumer (Renovate automerges minor module bumps).
- **The API key never enters `/opt/agnes/.env`, argv, an `echo`, or Terraform state.** Tests assert this on the rendered template.
- **Never fail a boot for monitoring.** Every step of the Datadog blocks is wrapped like the ops-agent block: `( … ) || echo "WARNING: …" >&2`. No `set -x` anywhere near the key.
- **Place the agent block BEFORE `docker compose up`.** The compose section ends in `exit 1` on failure; a tail-placed block is skipped exactly when monitoring matters.
- **Current Datadog key names only:** `container_labels_as_tags` (not `docker_labels_as_tags`), `mount_point_exclude` / `file_system_exclude` (not `excluded_*`), `postgres.can_connect` (not `postgresql.can_connect`), `monitored_resource_configs` (not `host_filters`). `docker.container_health` does not exist in Agent 7.
- **No customer specifics in test fixtures either** — render the template with `customer_name = "acme"`, `gcp_project_id = "example-project"`, `domain = "agnes.example.com"`.
- **CHANGELOG bullet lands in the same change (Task 5).** Quality hook runs ruff + mypy on every edited Python file; fix what it flags before committing.
- **Per-task verification:** run the named test file(s). Final gate: `scripts/verify_syncmap.py`, then `.venv/bin/pytest tests/ connectors/ --lane impacted --tb=short -n auto -q`, then `--lane fast`. The full suite is CI's job.
- **Release:** after merge, `git tag infra-v1.31.0 origin/main && git push origin infra-v1.31.0` (`infra-v1.30.0` already exists — do not reuse it).

---

### Task 1: Module inputs and Terraform plumbing

**Files:**
- Modify: `infra/modules/customer-instance/variables.tf` (append after `enable_gcp_logging`)
- Modify: `infra/modules/customer-instance/main.tf` (locals near `watchdog_files_b64`, a new IAM member next to `vm_kai_agent`, the VM `depends_on`, the labels of `google_compute_instance.vm`, `google_compute_disk.data`, `google_compute_address.ip`, the `templatefile` args, a `lifecycle.precondition`)
- Test: `tests/test_datadog_module_files.py` (new; the Terraform-side assertions of this task, the artifact assertions of Task 2)

**Interfaces (Tasks 2 and 3 rely on these exact names):**
- Variables: `enable_datadog` (bool, `false`), `datadog_api_key_secret` (string, `""`), `datadog_site` (string, `"datadoghq.com"`), `datadog_env` (string, `""` = `var.gcp_project_id`), `datadog_agent_version` (string, `"7.82.3"`), `datadog_extra_tags` (list(string), `[]`), `extra_labels` (map(string), `{}`).
- `local.datadog_env = coalesce(var.datadog_env, var.gcp_project_id)`.
- `local.datadog_files_b64[<instance name>]` = map of relative path → base64 content, `{}` when disabled. Keys: `datadog.yaml`, `conf.d/disk.yaml`, `conf.d/docker.yaml`, `conf.d/systemd.yaml`, `conf.d/directory.yaml`, `conf.d/http_check.yaml`, `conf.d/tls.yaml` (empty string when `tls_mode != "caddy"` or `domain == ""`), `postgres.yaml.tpl`, `agnes-datadog-pg-role.sh`, `agnes-datadog-pg-role.service`, `agnes-datadog-pg-role.timer`.
- Template args added to the `templatefile()` call: `enable_datadog`, `datadog_api_key_secret`, `datadog_site`, `datadog_agent_version`, `datadog_files_b64`.

- [ ] **Step 1: Write the failing test (Terraform side)**

Create `tests/test_datadog_module_files.py` with a `MODULE = Path("infra/modules/customer-instance")` constant (pattern: `tests/test_watchdog_module_files.py`) and these tests:

```python
def test_variables_declared_with_safe_defaults():
    tf = (MODULE / "variables.tf").read_text()
    for name in ("enable_datadog", "datadog_api_key_secret", "datadog_site",
                 "datadog_env", "datadog_agent_version", "datadog_extra_tags", "extra_labels"):
        assert f'variable "{name}"' in tf, name
    assert re.search(r'variable "enable_datadog"[^}]*default\s*=\s*false', tf, re.S)
    assert re.search(r'variable "datadog_site"[^}]*default\s*=\s*"datadoghq\.com"', tf, re.S)

def test_api_key_secret_is_validated_when_enabled():
    tf = (MODULE / "variables.tf").read_text()
    block = tf[tf.index('variable "datadog_api_key_secret"'):]
    assert "validation" in block[: block.index("\nvariable ")]

def test_main_tf_ships_every_datadog_artifact_and_grants_the_secret():
    main = (MODULE / "main.tf").read_text()
    for f in sorted(p.relative_to(MODULE / "files/datadog").as_posix()
                    for p in (MODULE / "files/datadog").rglob("*") if p.is_file()):
        assert f in main, f"files/datadog/{f} is not referenced by main.tf"
    assert 'google_secret_manager_secret_iam_member" "vm_datadog"' in main
    assert "datadog_files_b64" in main and "enable_datadog" in main
    assert "var.extra_labels" in main
```

- [ ] **Step 2: Run it, watch it fail**

`.venv/bin/pytest tests/test_datadog_module_files.py -q` → fails on the missing variables.

- [ ] **Step 3: Add the variables**

Append to `variables.tf`, in the style of `enable_gcp_logging` (heredoc description that states: opt-in; the agent is a host package; dd-agent joins the docker group; the block lives in the startup script so a running VM picks it up only through `terraform apply -replace`; thresholds belong to the caller's monitors, not to the VM):

```hcl
variable "enable_datadog" {
  description = <<-EOT
    Install the Datadog Agent (host package, pinned version) on every VM and ship
    host, disk, Docker, systemd, TLS, HTTP-health and Postgres side-car checks to
    Datadog. Off by default. … (recreate note, docker-group note, thresholds note)
  EOT
  type    = bool
  default = false
}

variable "datadog_api_key_secret" {
  description = "Secret Manager secret NAME holding a Datadog API key used only by the agent. Required when enable_datadog is true; the module grants the VM service account secretAccessor on it. The value never enters /opt/agnes/.env."
  type        = string
  default     = ""

  validation {
    condition     = var.datadog_api_key_secret == "" || can(regex("^[A-Za-z0-9_-]+$", var.datadog_api_key_secret))
    error_message = "datadog_api_key_secret must be a Secret Manager secret name."
  }
}

variable "datadog_site" {
  description = "Datadog site the agent reports to."
  type        = string
  default     = "datadoghq.com"

  validation {
    condition     = contains(["datadoghq.com", "datadoghq.eu", "us3.datadoghq.com", "us5.datadoghq.com", "ap1.datadoghq.com", "ddog-gov.com"], var.datadog_site)
    error_message = "datadog_site must be one of the documented Datadog sites."
  }
}

variable "datadog_env" {
  description = "Value of the `env` tag applied to everything the agent emits. Empty (default) = the GCP project id."
  type        = string
  default     = ""
}

variable "datadog_agent_version" {
  description = "Exact datadog-agent package version to install and hold. A newer version reaches a running VM only through a recreate."
  type        = string
  default     = "7.82.3"

  validation {
    condition     = can(regex("^7\\.[0-9]+\\.[0-9]+$", var.datadog_agent_version))
    error_message = "datadog_agent_version must be a 7.x.y version."
  }
}

variable "datadog_extra_tags" {
  description = "Additional host tags (key:value) applied to everything the agent emits."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for t in var.datadog_extra_tags : can(regex("^[a-z0-9_.:/-]+$", t))])
    error_message = "Tags must be lowercase key:value strings."
  }
}

variable "extra_labels" {
  description = "Additional GCE labels merged into the VM, data disk and static IP labels (module-owned keys app/customer/role/managed win)."
  type        = map(string)
  default     = {}
}
```

- [ ] **Step 4: Plumb `main.tf`**

1. Locals (next to `watchdog_files_b64`):
   ```hcl
   datadog_env = coalesce(var.datadog_env, var.gcp_project_id)
   datadog_secret_names = var.enable_datadog ? setsubtract(
     toset(compact([var.datadog_api_key_secret])),
     setunion(toset(keys(var.runtime_secret_env)), toset(keys(var.runtime_secret_env_multiline)), toset(var.runtime_secrets)),
   ) : toset([])
   datadog_static_files = ["conf.d/disk.yaml", "conf.d/docker.yaml", "conf.d/systemd.yaml", "conf.d/directory.yaml", "postgres.yaml.tpl", "agnes-datadog-pg-role.sh", "agnes-datadog-pg-role.service", "agnes-datadog-pg-role.timer"]
   datadog_files_b64 = {
     for inst in local.all_instances : inst.name => var.enable_datadog ? merge(
       { for f in local.datadog_static_files : f => filebase64("${path.module}/files/datadog/${f}") },
       {
         "datadog.yaml" = base64encode(templatefile("${path.module}/files/datadog/datadog.yaml.tpl", {
           site = var.datadog_site
           env  = local.datadog_env
           tags = concat(["customer:${var.customer_name}", "app:agnes", "service:agnes", "role:${inst.role}", "agnes_instance:${inst.name}", "managed:terraform"], var.datadog_extra_tags)
         }))
         "conf.d/http_check.yaml" = base64encode(templatefile("${path.module}/files/datadog/http_check.yaml.tpl", { domain = inst.domain, tls_mode = inst.tls_mode }))
         "conf.d/tls.yaml" = (inst.tls_mode == "caddy" && inst.domain != "") ? base64encode(templatefile("${path.module}/files/datadog/tls.yaml.tpl", { hosts = compact([inst.domain, try(inst.domain_alias, "")]) })) : ""
       },
     ) : {}
   }
   ```
   (`local.all_instances` already exists — reuse its field names; check how `role`, `domain`, `domain_alias`, `tls_mode` are spelled there.)
2. `resource "google_secret_manager_secret_iam_member" "vm_datadog"` — `for_each = local.datadog_secret_names`, `roles/secretmanager.secretAccessor`, VM service account member; append `google_secret_manager_secret_iam_member.vm_datadog` to the VM `depends_on` list.
3. Labels on the VM, `google_compute_disk.data`, `google_compute_address.ip`: `merge(var.extra_labels, { app = "agnes", customer = var.customer_name, role = …, managed = "terraform" })` (keep the existing module keys exactly; module keys win).
4. `templatefile()` args: `enable_datadog = var.enable_datadog`, `datadog_api_key_secret = var.datadog_api_key_secret`, `datadog_site = var.datadog_site`, `datadog_agent_version = var.datadog_agent_version`, `datadog_files_b64 = local.datadog_files_b64[each.value.name]`.
5. `lifecycle { precondition { condition = !var.enable_datadog || var.datadog_api_key_secret != "" ; error_message = "enable_datadog requires datadog_api_key_secret." } }` next to the existing kai-agent precondition.

- [ ] **Step 5: Run the test and `terraform validate`**

`.venv/bin/pytest tests/test_datadog_module_files.py -q` (the artifact-reference test still fails until Task 2 — that is expected; the variable tests pass). Then in a scratch dir that calls the module with `enable_datadog = true`, `datadog_api_key_secret = "example-secret"` and a minimal `prod_instance`: `terraform init -backend=false && terraform validate`. Also validate `infra/examples/minimal`.

- [ ] **Step 6: Commit**

`git commit -m "feat(infra): opt-in Datadog agent inputs and plumbing in customer-instance"`

---

### Task 2: Agent artifacts under `files/datadog/`

**Files:**
- Create: `infra/modules/customer-instance/files/datadog/datadog.yaml.tpl`, `conf.d/disk.yaml`, `conf.d/docker.yaml`, `conf.d/systemd.yaml`, `conf.d/directory.yaml`, `http_check.yaml.tpl`, `tls.yaml.tpl`, `postgres.yaml.tpl`, `agnes-datadog-pg-role.sh`, `agnes-datadog-pg-role.service`, `agnes-datadog-pg-role.timer`
- Test: `tests/test_datadog_module_files.py` (extend)

- [ ] **Step 1: Extend the failing tests**

```python
HARDENING = ("remote_configuration", "apm_config", "logs_enabled: false", "use_dogstatsd: false",
             "process_collection", "container_collection", "process_discovery",
             "runtime_security_config", "compliance_config", "sbom", "container_image",
             "container_lifecycle", "inventories_configuration_enabled: false",
             "inventories_checks_configuration_enabled: false", "bind_host: 127.0.0.1",
             "container_env_as_tags", "container_labels_as_tags", "exclude_pause_container: true")

def test_static_conf_files_parse():
    for p in (FILES / "conf.d").glob("*.yaml"):
        assert yaml.safe_load(p.read_text()), p

def test_datadog_yaml_template_is_hardened():
    rendered = _render_tpl("datadog.yaml.tpl", site="datadoghq.com", env="example-project",
                           tags=["customer:acme", "app:agnes"])
    doc = yaml.safe_load(rendered.replace("@@DD_API_KEY@@", "x"))
    assert doc["env"] == "example-project" and doc["site"] == "datadoghq.com"
    assert doc["container_labels_as_tags"]["com.docker.compose.service"] == "compose_service"
    assert doc["remote_configuration"]["enabled"] is False
    assert doc["apm_config"]["enabled"] is False and doc["logs_enabled"] is False
    for key in HARDENING:
        assert key in rendered, key
    assert "docker_labels_as_tags" not in rendered

def test_disk_check_uses_mount_tags_and_current_keys():
    doc = yaml.safe_load((FILES / "conf.d/disk.yaml").read_text())
    inst = doc["instances"][0]
    assert inst["use_mount"] is True and inst["service_check_rw"] is True
    assert "mount_point_exclude" in inst and "file_system_exclude" in inst
    assert "excluded_filesystems" not in inst and "excluded_mountpoint_re" not in inst

def test_systemd_check_maps_the_backup_oneshot_and_never_forces_the_private_socket():
    doc = yaml.safe_load((FILES / "conf.d/systemd.yaml").read_text())
    inst = doc["instances"][0]
    assert "agnes-db-backup.service" in inst["unit_names"]
    assert inst["substate_status_mapping"]["agnes-db-backup.service"]["failed"] == "critical"
    assert "private_socket" not in inst

def test_directory_check_has_the_four_probes():
    doc = yaml.safe_load((FILES / "conf.d/directory.yaml").read_text())
    probes = {t.split(":", 1)[1] for i in doc["instances"] for t in i["tags"] if t.startswith("agnes_probe:")}
    assert probes == {"state_applier", "auto_upgrade", "backup", "watchdog"}
    assert all(i.get("filegauges") is True and i.get("ignore_missing") is True for i in doc["instances"])

def test_postgres_template_is_autodiscovery_on_the_image_and_uses_the_current_check_name():
    tpl = (FILES / "postgres.yaml.tpl").read_text()
    assert "ad_identifiers" in tpl and "- postgres" in tpl and "%%host%%" in tpl
    assert "@@DD_PG_PASSWORD@@" in tpl and "dbm: false" in tpl and "ssl: disable" in tpl

def test_pg_role_script_never_puts_the_password_on_argv():
    sh = (FILES / "agnes-datadog-pg-role.sh").read_text()
    assert "PGPASSWORD=" not in sh and "-v pw=" not in sh and "set -x" not in sh
    assert "psql" in sh and "<<" in sh          # SQL fed on stdin
    assert "GRANT pg_monitor TO datadog" in sh
    assert sh.rstrip().endswith("exit 0")

def test_http_and_tls_templates_render_for_a_caddy_vm():
    http = _render_tpl("http_check.yaml.tpl", domain="agnes.example.com", tls_mode="caddy")
    doc = yaml.safe_load(http)
    names = {i["name"] for i in doc["instances"]}
    assert names == {"agnes_readyz", "agnes_health_body", "agnes_acme_http"}
    assert all(i.get("include_content") is False for i in doc["instances"])
    body = next(i for i in doc["instances"] if i["name"] == "agnes_health_body")
    assert body["content_match"]           # /api/health is always 200 — the body is the signal
    tls = yaml.safe_load(_render_tpl("tls.yaml.tpl", hosts=["agnes.example.com", "alias.example.com"]))
    assert {i["server"] for i in tls["instances"]} == {"agnes.example.com", "alias.example.com"}
```

`_render_tpl` is a mini-templatefile: substitute `${name}` and expand the `%{ for … }` loops used in these three templates (pattern: `_render` in `tests/test_infra_runtime_secret_env_hardening.py`). Keep the templates simple enough for it (one `%{ for }` per template, `${var}` interpolation only).

- [ ] **Step 2: Run, watch it fail**

- [ ] **Step 3: Create the artifacts**

`datadog.yaml.tpl`:
```yaml
api_key: "@@DD_API_KEY@@"
site: ${site}
env: ${env}
tags:
%{ for t in tags ~}
  - ${t}
%{ endfor ~}
container_labels_as_tags:
  com.docker.compose.service: compose_service
  com.docker.compose.project: compose_project
container_exclude: "name:^agnes-(migrate|data-migrate|kai-agent-migrate|extract)-[0-9]+$"
exclude_pause_container: true
container_env_as_tags: {}
bind_host: 127.0.0.1
logs_enabled: false
use_dogstatsd: false
remote_configuration:
  enabled: false
apm_config:
  enabled: false
process_config:
  process_collection:
    enabled: false
  container_collection:
    enabled: false
  process_discovery:
    enabled: false
runtime_security_config:
  enabled: false
compliance_config:
  enabled: false
sbom:
  enabled: false
container_image:
  enabled: false
container_lifecycle:
  enabled: false
inventories_configuration_enabled: false
inventories_checks_configuration_enabled: false
```

`conf.d/disk.yaml`:
```yaml
init_config: {}
instances:
  - use_mount: true
    service_check_rw: true
    mount_point_exclude:
      - '^/(sys|proc|dev|run|snap|boot/efi)($|/)'
      - '^/var/lib/docker/(overlay2|containers)'
    file_system_exclude:
      - 'tmpfs$'
      - 'devtmpfs$'
      - 'overlay$'
      - 'squashfs$'
      - 'nsfs$'
```

`conf.d/docker.yaml`:
```yaml
init_config: {}
instances:
  - collect_events: true
    unbundle_events: true
    collect_container_size: false
    collect_images_stats: false
```

`conf.d/systemd.yaml`:
```yaml
init_config: {}
instances:
  - unit_names:
      - docker.service
      - cron.service
      - agnes-state-applier.timer
      - agnes-watchdog.timer
      - agnes-db-backup.timer
      - agnes-db-backup.service
      - agnes-datadog-pg-role.timer
    substate_status_mapping:
      agnes-db-backup.service:
        failed: critical
        dead: ok
        exited: ok
        running: ok
        start: ok
```

`conf.d/directory.yaml` — four instances (`directory`, `pattern`, `filegauges: true`, `ignore_missing: true`, `recursive: false`, `tags: [agnes_probe:<key>]`): `/data/state` + `agnes-state-applier.tick` (`state_applier`); `/var/lib/agnes` + `auto-upgrade.tick` (`auto_upgrade`); `/data/backups/system-duckdb` + `*` (`backup`); `/var/lib/agnes/watchdog` + `*` (`watchdog`).

`http_check.yaml.tpl` — base URL `https://${domain}` when `tls_mode == "caddy"` and `domain != ""`, otherwise `http://127.0.0.1:8000`; instances `agnes_readyz` (`/readyz`, `http_response_status_code: 200`), `agnes_health_body` (`/api/health`, `content_match: '"status":\s*"ok"'`), and — only in the caddy+domain case — `agnes_acme_http` (`http://${domain}/`, `allow_redirects: false`, `http_response_status_code: (301|308)`). Every instance: `timeout: 10`, `include_content: false`, `tls_verify: true`, `min_collection_interval: 60`, `tags: [instance:<name>]`.

`tls.yaml.tpl` — `%{ for h in hosts }` → `server: ${h}`, `port: 443`, `days_warning: 30`, `days_critical: 14`, `tags: [tls_target:${h}]`.

`postgres.yaml.tpl`:
```yaml
ad_identifiers:
  - postgres
init_config: {}
instances:
  - host: "%%host%%"
    port: 5432
    username: datadog
    password: "@@DD_PG_PASSWORD@@"
    dbname: postgres
    ssl: disable
    dbm: false
    collect_database_size_metrics: true
    collect_activity_metrics: false
    collect_wal_metrics: false
```

`agnes-datadog-pg-role.sh` (root; idempotent; always `exit 0`; warnings via `logger -t agnes-datadog-pg-role`):
```bash
#!/usr/bin/env bash
# Creates a pg_monitor role for the Datadog Agent in every postgres container of the
# compose project and renders the agent's postgres check config. Safe to re-run.
set -uo pipefail
PW_FILE=/data/state/datadog-pg-password
TPL=/etc/datadog-agent/agnes-postgres.yaml.tpl
OUT=/etc/datadog-agent/conf.d/postgres.d/conf.yaml
[ -s "$PW_FILE" ] || (umask 077; openssl rand -hex 24 > "$PW_FILE")
PW=$(cat "$PW_FILE")
for cid in $(docker ps -q --filter label=com.docker.compose.project=agnes); do
    image=$(docker inspect -f '{{.Config.Image}}' "$cid")
    case "$image" in postgres:*|*/postgres:*) ;; *) continue ;; esac
    user=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$cid" | sed -n 's/^POSTGRES_USER=//p' | head -1)
    user=${user:-postgres}
    docker exec -i "$cid" psql -v ON_ERROR_STOP=1 -U "$user" -d postgres >/dev/null <<SQL || logger -t agnes-datadog-pg-role -p user.warning "role bootstrap failed in $cid"
DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'datadog') THEN CREATE ROLE datadog LOGIN; END IF; END \$\$;
ALTER ROLE datadog WITH LOGIN PASSWORD '$PW';
GRANT pg_monitor TO datadog;
GRANT SELECT ON pg_stat_database TO datadog;
SQL
done
if [ -f "$TPL" ]; then
    install -d -o dd-agent -g dd-agent -m 0750 "$(dirname "$OUT")"
    tmp=$(mktemp)
    rendered=$(<"$TPL"); printf '%s\n' "${rendered//@@DD_PG_PASSWORD@@/$PW}" > "$tmp"
    if ! cmp -s "$tmp" "$OUT"; then
        install -o dd-agent -g dd-agent -m 0640 "$tmp" "$OUT" && systemctl restart datadog-agent || true
    fi
    rm -f "$tmp"
fi
exit 0
```
(The heredoc is unquoted on purpose so `$PW` expands; the `\$\$` keep the PL/pgSQL block. The password never appears on a command line.)

`agnes-datadog-pg-role.service`: `[Unit] Description=Datadog pg_monitor role bootstrap for Agnes postgres side-cars`, `After=docker.service`; `[Service] Type=oneshot`, `ExecStart=/usr/local/bin/agnes-datadog-pg-role.sh`. `agnes-datadog-pg-role.timer`: `OnBootSec=2min`, `OnUnitActiveSec=15min`, `[Install] WantedBy=timers.target`.

- [ ] **Step 4: Run the tests until green**

`.venv/bin/pytest tests/test_datadog_module_files.py -q`

- [ ] **Step 5: Commit**

`git commit -m "feat(infra): Datadog agent check configs and pg_monitor bootstrap for customer-instance"`

---

### Task 3: Startup script — agent install (Block A), role timer (Block B), cron heartbeat

**Files:**
- Modify: `infra/modules/customer-instance/startup-script.sh.tpl`
- Test: `tests/test_startup_datadog_toggle.py` (new)

- [ ] **Step 1: Write the failing tests**

Render the template twice with a mini-templatefile (pattern: `tests/test_startup_kai_agent_toggle.py`): `enable_datadog = true` / `false`, `datadog_api_key_secret = "example-secret"`, `datadog_site = "datadoghq.com"`, `datadog_agent_version = "7.82.3"`, `datadog_files_b64 = {"datadog.yaml": "<b64>", "conf.d/disk.yaml": "<b64>", "conf.d/tls.yaml": ""}`. Assert:

1. enabled: the text `DATADOG AGENT` block exists and its index is smaller than the index of the first `docker compose` line, and smaller than the index of `OPS AGENT`'s closing `%{ endif }` successor — i.e. it sits right after the ops-agent block.
2. enabled: `gcloud secrets versions access latest --secret=example-secret 2>/dev/null || echo ""` present (silent form).
3. enabled: the variable holding the key (`DD_API_KEY_VALUE`) appears ONLY in the fetch line, an emptiness test, and the datadog.yaml render; never inside the `cat > "$APP_DIR/.env"` heredoc; never after `echo`; no `set -x` anywhere in the template.
4. enabled: `apt-get install -y datadog-agent=1:7.82.3-1`, `apt-mark hold datadog-agent`, `signed-by=/usr/share/keyrings/datadog-archive-keyring.gpg`, `usermod -aG docker dd-agent`, `install -o root -g dd-agent -m 0640` (datadog.yaml), `setfacl -m u:dd-agent:rx /data/state`, `install -d -m 0755 /var/lib/agnes/watchdog`, `systemctl enable --now datadog-agent`.
5. enabled: Block B (`agnes-datadog-pg-role.timer`) appears AFTER the `docker compose up` line.
6. disabled: no `apt-get install -y datadog-agent`, and `systemctl disable --now datadog-agent` present (self-heal branch).
7. both: the auto-upgrade crontab line ends with `; date +%s > /var/lib/agnes/auto-upgrade.tick` and `install -d -m 0755 /var/lib/agnes` precedes it.

- [ ] **Step 2: Run, watch it fail**

- [ ] **Step 3: Block A** — insert immediately after the ops-agent block's `%{ endif ~}` (before the gcplogs probe), wrapped in `%{ if enable_datadog ~}` … `%{ else ~}` … `%{ endif ~}`:

```bash
# --- DATADOG AGENT (opt-in host monitoring) -------------------------------
# Runs BEFORE any docker work: a boot whose compose never converges must still
# ship host metrics. Every step is failure-tolerant — monitoring never fails a boot.
DD_API_KEY_VALUE=$(gcloud secrets versions access latest --secret=${datadog_api_key_secret} 2>/dev/null || echo "")
if [ -z "$DD_API_KEY_VALUE" ]; then
    echo "WARNING: Datadog API key secret '${datadog_api_key_secret}' is unreadable or empty — agent not configured this boot" >&2
else
    if [ "$(dpkg-query -W -f='$${Version}' datadog-agent 2>/dev/null)" != "1:${datadog_agent_version}-1" ]; then
        (
            install -d -m 0755 /usr/share/keyrings
            for k in DATADOG_APT_KEY_CURRENT DATADOG_APT_KEY_06462314 DATADOG_APT_KEY_C0962C7D DATADOG_APT_KEY_F14F620E DATADOG_APT_KEY_382E94DE; do curl -fsSL "https://keys.datadoghq.com/$$k.public"; done | gpg --dearmor --yes -o /usr/share/keyrings/datadog-archive-keyring.gpg
            echo "deb [signed-by=/usr/share/keyrings/datadog-archive-keyring.gpg] https://apt.datadoghq.com/ stable 7" > /etc/apt/sources.list.d/datadog.list
            apt-get update -qq
            apt-mark unhold datadog-agent >/dev/null 2>&1 || true
            DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --allow-downgrades "datadog-agent=1:${datadog_agent_version}-1" datadog-signing-keys
            apt-mark hold datadog-agent
        ) || echo "WARNING: Datadog Agent install failed — host monitoring unavailable this boot" >&2
    fi
    if id dd-agent >/dev/null 2>&1; then
        usermod -aG docker dd-agent || echo "WARNING: could not add dd-agent to the docker group" >&2
        (
            install -d -m 0755 /var/lib/agnes /var/lib/agnes/watchdog
            setfacl -m u:dd-agent:rx /data/state
            install -d /data/backups/system-duckdb
            setfacl -m u:dd-agent:rx /data/backups /data/backups/system-duckdb
            setfacl -d -m u:dd-agent:rx /data/backups/system-duckdb
        ) || echo "WARNING: could not make heartbeat paths readable by dd-agent" >&2
%{ for path, content in datadog_files_b64 ~}
        _dd_install_artifact "${path}" "${content}"
%{ endfor ~}
        systemctl enable --now datadog-agent >/dev/null 2>&1 && systemctl restart datadog-agent \
            || echo "WARNING: could not start datadog-agent" >&2
    fi
fi
unset DD_API_KEY_VALUE
```

Define `_dd_install_artifact <relative path> <base64>` above the block: an empty base64 removes the target (`conf.d/<check>.yaml` → `/etc/datadog-agent/conf.d/<check>.d/conf.yaml`, `datadog.yaml` → `/etc/datadog-agent/datadog.yaml`, `postgres.yaml.tpl` → `/etc/datadog-agent/agnes-postgres.yaml.tpl` (root, 0600), `agnes-datadog-pg-role.sh` → `/usr/local/bin/` (0755), `*.service`/`*.timer` → `/etc/systemd/system/`); a non-empty one decodes into a temp file, substitutes `@@DD_API_KEY@@` via bash parameter expansion (`"$${content//@@DD_API_KEY@@/$DD_API_KEY_VALUE}"` — read the decoded text into a variable, never `sed` with the key on argv), and `install -o root -g dd-agent -m 0640` (datadog.yaml) or `install -o dd-agent -g dd-agent -m 0640` (conf.d). Remember the template escapes: `$${…}` for shell expansions, `${…}` for Terraform.

`%{ else ~}` branch: `if systemctl is-enabled --quiet datadog-agent 2>/dev/null; then systemctl disable --now datadog-agent || true; fi`.

- [ ] **Step 4: Block B** — after the compose-up + kai-agent section and before the watchdog block, inside `%{ if enable_datadog ~}`: `systemctl daemon-reload; systemctl enable --now agnes-datadog-pg-role.timer; systemctl start agnes-datadog-pg-role.service || true` (units were installed by `_dd_install_artifact` in Block A; guard on `[ -x /usr/local/bin/agnes-datadog-pg-role.sh ]`).

- [ ] **Step 5: Cron heartbeat** (unconditional): change the crontab line to `$UPGRADE_SCHEDULE /usr/local/bin/agnes-auto-upgrade.sh >> /var/log/agnes-auto-upgrade.log 2>&1; date +%s > /var/lib/agnes/auto-upgrade.tick` and add `install -d -m 0755 /var/lib/agnes` just before it. Grep `tests/` for the old crontab line and update any assertion (`grep -rn "agnes-auto-upgrade.log" tests/`).

- [ ] **Step 6: Run** `.venv/bin/pytest tests/test_startup_datadog_toggle.py tests/test_startup_guards.py tests/test_auto_upgrade_role_split.py -q`, then `bash -n` on a rendered copy of the template (render with the mini-templatefile, write to a temp file, `bash -n`).

- [ ] **Step 7: Commit**

`git commit -m "feat(infra): install and configure the Datadog agent from the customer-instance startup script"`

---

### Task 4: Watchdog marker bridge

**Files:**
- Modify: `infra/modules/customer-instance/files/agnes-watchdog.sh`
- Test: `tests/test_watchdog_module_files.py` (extend)

- [ ] **Step 1: Failing test** — assert `MARK_DIR=/var/lib/agnes/watchdog` and a `mark_signature()` function exist, and that every alert site (each `alert "…"`/`add_alert` call — read the script to find the exact helper) has a `mark_signature <slug>` companion with slugs from `{crash, zombie, wal-salvage, index-desync, index-append-fatal, coordination, restarts, container-down, oom, health, discarded-wal, scheduler, disk}`; assert the call happens before the anti-spam check (`alert_hash`).

- [ ] **Step 2: Implement** — `mark_signature() { install -d -m 0755 "$MARK_DIR" 2>/dev/null; date +%s > "$MARK_DIR/$1" 2>/dev/null || true; }`; call it next to each signature alert. Markers are never deleted.

- [ ] **Step 3: Run** `.venv/bin/pytest tests/test_watchdog_module_files.py -q`; `bash -n files/agnes-watchdog.sh`.

- [ ] **Step 4: Commit** `git commit -m "feat(infra): watchdog writes per-signature marker files for external monitoring"`

---

### Task 5: Docs, CHANGELOG, final gates

**Files:**
- Modify: `CHANGELOG.md` (`## [Unreleased]` → `### Added`)
- Modify: `docs/DEPLOYMENT.md` ("Health checks & external monitoring")
- Modify: `docs/README.md` (index already links the spec + this plan)

- [ ] **Step 1:** CHANGELOG bullet: opt-in Datadog host agent (`enable_datadog` + `datadog_*` inputs, `extra_labels`) in the customer-instance module; what it collects; the recreate requirement; watchdog marker files under `/var/lib/agnes/watchdog/`; the auto-upgrade heartbeat file.
- [ ] **Step 2:** DEPLOYMENT.md paragraph: how to enable, which secret to create, what runs on the host (checks table from the spec), that dd-agent joins the docker group, key rotation (`gcloud secrets versions add` + reboot), that thresholds/monitors belong to the consumer root (see the spec's consumer catalogue).
- [ ] **Step 3:** Gates: `scripts/verify_syncmap.py`; `.venv/bin/pytest tests/ connectors/ --lane impacted --tb=short -n auto -q`; `.venv/bin/pytest tests/ connectors/ --lane fast --tb=short -n auto -q`; `terraform validate` on `infra/examples/minimal` and on the scratch root from Task 1 with `enable_datadog = true`.
- [ ] **Step 4:** Commit `docs(infra): document the opt-in Datadog host agent`; open the PR; after merge tag `infra-v1.31.0`.

---

### Task 6: Consumer root recipe (executed in the consumer's PRIVATE infra repo — never in this repo)

This task is the generic contract; concrete values (`<customer>`, `<project-id>`, `<domain>`, `@slack-<channel>`, secret names) live only in the consumer repo.

**Prerequisites (operator, out of band):** a Slack channel added to the Datadog Slack integration (handle `@slack-<channel>`); a Datadog API key for the agent; a Datadog API key + an application key bound to a Datadog service account with Monitors/Dashboards/Synthetics write; three Secret Manager secrets holding them (`<customer>-datadog-agent-api-key`, `<customer>-datadog-terraform-api-key`, `<customer>-datadog-terraform-app-key`); the deploy identity may read the two terraform-* secrets.

**Root wiring:**
- `required_providers` += `datadog = { source = "DataDog/datadog", version = "~> 4.20" }`; `terraform providers lock -platform=linux_amd64 -platform=darwin_arm64 -platform=darwin_amd64`.
- `provider "datadog" { api_key = <secret data source>, app_key = <secret data source>, api_url = "https://api.${var.datadog_site}/" }`.
- Module call: `enable_datadog = true`, `datadog_api_key_secret = "<customer>-datadog-agent-api-key"`, `datadog_site = "<site>"`, `datadog_extra_tags = ["repo:<consumer-repo>"]`, `extra_labels = { env = var.gcp_project_id }`, module ref `infra-v1.31.0`.
- Variables: `datadog_site`, the three secret names, `datadog_notify_slack` (list(string), NO default, `validation length > 0`), `datadog_agent_monitors_enabled` (bool, `false` until the recreate is verified), `datadog_synthetics_locations` (default two EU or US locations), `datadog_thresholds` (object, all `optional()`, default `{}`).
- Local `expected_running_containers = 4 + (kai_agent_enabled ? 2 : 0) + (extraction_worker_enabled ? 2 : 0) + (dispatcher_enabled ? 2 : 0) + (data_apps_enabled || chat_provider == "docker" ? 1 : 0)` from `var.prod_instance` via `try()`.
- CI: add `terraform test` for `modules/datadog-monitors` to the validate job.

**Local module `modules/datadog-monitors/`** (inputs: `env`, `customer_name`, `tags`, `notify_important`, `notify_info`, `domain`, `expected_running_containers`, `postgres_services = {postgres = 1, "kai-agent-pg" = 2}`, `synthetics {enabled, locations, tick_every}`, `agent_monitors_enabled`, `thresholds`): the monitor catalogue, the synthetic and the dashboard exactly as listed in the spec (names `"${var.env} - Agnes - <signal>"`, every resource tagged with `var.tags` which includes `env:<project-id>`, `renotify_interval = 0`, `count = var.agent_monitors_enabled ? 1 : 0` on agent-sourced monitors, messages ending in `{{#is_alert}}<handles>{{/is_alert}}{{#is_recovery}}<handles>{{/is_recovery}}`). `tests/monitors.tftest.hcl` with `mock_provider "datadog" {}` asserts: expected resource count; important messages contain the handle and info messages do not; only `host_down` sets `notify_no_data`; every resource carries `env:` in tags; no literal customer name in any query; `agent_monitors_enabled = false` leaves only the synthetic + dashboard.

**Rollout:** PR 1 (provider, secrets, module with `agent_monitors_enabled = false`) → PR 2 (module bump + `enable_datadog = true` + labels; only the IAM binding and labels change) → VM recreate via the consumer's `recreate_targets` dispatch (bundle with other pending startup-script changes; 5–10 min outage; `/data` and the static IP survive; Let's Encrypt re-issues) → verify on the VM (`sudo datadog-agent status` shows disk, docker, container, systemd, directory, tls, http_check and postgres for BOTH side-cars OK; `datadog.yaml` is `root dd-agent 0640`; `grep -c api_key /var/log/agnes-startup.log` is 0; the host carries `env:<project-id>`) → PR 3 (`agent_monitors_enabled = true`) → game-day (`docker stop` a container, stop the agent for 12 min, break the backup target, touch a watchdog marker).
