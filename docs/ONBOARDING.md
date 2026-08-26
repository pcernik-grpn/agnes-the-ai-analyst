> Companion: [docs/PLATFORM_SETUP.md](./PLATFORM_SETUP.md) is the day-2 operator playbook — marketplaces, scheduler cadence, telemetry, privacy posture, daily routine. It complements this doc rather than replacing it.

# Onboarding a new Agnes instance

End-to-end guide for deploying Agnes into a new GCP project. Target time: **under 1 hour**.

The target reader is a Keboola ops engineer or a customer with GCP Owner access.

## Overview

Every Agnes instance lives in **one GCP project per customer**, driven by a **private infra repo** cloned from [keboola/agnes-infra-template](https://github.com/keboola/agnes-infra-template). The upstream app + TF module is in [keboola/agnes-the-ai-analyst](https://github.com/keboola/agnes-the-ai-analyst); customers do not fork it.

## Prerequisites

- GCP project with billing linked (you / customer owns it)
- `gcloud` CLI authenticated as project Owner
- `terraform` ≥ 1.5
- `gh` CLI authenticated
- (optional) `docker` for local smoke tests

## 1. Bootstrap GCP

Run the bootstrap script from a clone of this repository (a raw-URL fetch
only works while the repository is public — the clone works either way):

```bash
git clone https://github.com/keboola/agnes-the-ai-analyst.git
./agnes-the-ai-analyst/scripts/bootstrap-gcp.sh <GCP_PROJECT_ID>
```

Outputs:
- `agnes-deploy@<project>.iam.gserviceaccount.com` (Terraform SA with scoped roles)
- `gs://agnes-<project>-tfstate` (versioned, uniform bucket-level access)
- `./agnes-deploy-<project>-key.json` (SA JSON key — store in `~/.agnes-keys/` or password manager, **not git**)

Idempotent — safe to re-run.

## 2. Customer's data source secrets

If `data_source = "keboola"`:

```bash
echo -n "<KEBOOLA_STORAGE_TOKEN>" | gcloud secrets create keboola-storage-token \
    --data-file=- --replication-policy=automatic --project=<GCP_PROJECT_ID>
```

## 3. Create private infra repo from template

Create and clone in one step (the `--clone` flag waits for the template copy to finish; cloning in two steps can race):

```bash
gh repo create <customer-org>/agnes-infra-<customer> \
    --template keboola/agnes-infra-template \
    --private \
    --clone
cd agnes-infra-<customer>
```

Upload the SA key to GitHub secrets:

```bash
gh secret set GCP_SA_KEY < ~/.agnes-keys/agnes-deploy-<project>-key.json
```

Create GitHub environments `dev` (no protection) and `prod` (required reviewer, wait timer 5 min, branch `main` only):

```bash
gh api -X PUT repos/<customer-org>/agnes-infra-<customer>/environments/dev
echo '{"wait_timer":300,"deployment_branch_policy":{"protected_branches":true,"custom_branch_policies":false}}' \
  | gh api -X PUT repos/<customer-org>/agnes-infra-<customer>/environments/prod --input -
```

Add reviewers via GitHub UI (Settings → Environments → prod).

## 4. Configure tfvars and backend

Edit `terraform/main.tf`:

```hcl
backend "gcs" {
  bucket = "agnes-<GCP_PROJECT_ID>-tfstate"
  prefix = "<customer>"
}
```

Copy the example and fill it in:

```bash
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
# Required:
#   gcp_project_id    = "<GCP_PROJECT_ID>"
#   customer_name     = "<customer>"
#   seed_admin_email  = "...@customer.com"
#   keboola_stack_url = "https://connection.<region>.gcp.keboola.com/"
#
# Optional:
#   runtime_secrets            = ["keboola-storage-token"]  # empty if non-keboola data_source
#   firewall_ssh_source_ranges = ["35.235.240.0/20"]        # IAP range; "0.0.0.0/0" if public SSH
#   notification_channel_ids   = ["projects/<p>/notificationChannels/<id>"]
```

See the [module README](https://github.com/keboola/agnes-the-ai-analyst/tree/main/infra/modules/customer-instance) for the full variable schema.

## 5. First apply

```bash
cd terraform
export GOOGLE_APPLICATION_CREDENTIALS=~/.agnes-keys/agnes-deploy-<project>-key.json
terraform init
terraform plan
terraform apply
```

Or push `terraform.tfvars` committed path and let GitHub Actions do it:

```bash
git add . && git commit -m "initial: <customer> deployment" && git push origin main
# CI runs apply-dev, waits for prod reviewer, then apply-prod
```

Output: `prod_ip` = external IP.

## 6. Bootstrap admin user

On first boot the app auto-seeds an admin user from `SEED_ADMIN_EMAIL` — but *without a password*, which means nobody can log in yet. Activate it via `POST /auth/bootstrap`:

```bash
PROD_IP=$(terraform output -raw prod_ip)
curl -X POST "http://$PROD_IP:8000/auth/bootstrap" \
    -H "Content-Type: application/json" \
    -d '{"email":"<seed_admin_email from tfvars>","password":"<STRONG_PASSWORD>"}'
```

If the email matches the seed user, the endpoint sets its password and promotes to admin. If it doesn't match, a new admin is created. The endpoint self-deactivates once any user has a password — **so do this before exposing the URL**.

Log in: `http://<prod_ip>:8000/login` with the email + password you just set.

**Security:** The bootstrap endpoint is only disabled by a real password being set. Running `terraform destroy` + `apply` recreates the seed user and re-opens bootstrap — so if you destroy/recreate, a new attacker window opens until you re-run bootstrap.

## 7. DNS + TLS (optional)

For HTTPS, set in `terraform.tfvars`:

```hcl
prod_instance = {
  ...
  tls_mode = "caddy"
  domain   = "agnes.<customer>.com"
}
```

Then create a DNS A-record pointing `agnes.<customer>.com` → `prod_ip`. Caddy will auto-issue a Let's Encrypt cert (`CADDY_TLS=tls <ops-email>`; the A-record must exist and ports 80 + 443 must be open *before* first start, or issuance loops). Corporate-CA certs and the self-signed lab mode are the other two regimes — see [DEPLOYMENT.md § TLS](DEPLOYMENT.md#tls-optional) for the full matrix, including why a publicly-trusted cert makes the analyst install prompt drop its TLS-trust step automatically.

## 8. Smoke test

Run the deploy gate **on the VM** — it checks the public API (health, schema,
CLI wheel), calls the new-instance doctor (`POST /api/admin/doctor/new-instance`:
a usable login door exists, email really sends, chat is granted to a group,
agent scopes intersect owner grants, branding reaches the login page), and
verifies host-side consistency (`COMPOSE_FILE` ↔ `instance.yaml` database
backend, TLS predicate agreement). Each doctor check exists because that exact
thing silently failed on a real deployment.

```bash
gcloud compute ssh agnes-prod --zone=<zone> --project=<project> --command \
    'sudo DOCTOR_EMAIL_TO=you@example.com bash /opt/agnes/post-deploy-smoke-test.sh'
```

The script ships in the image (extracted to `/opt/agnes/` on boot); on a VM
deployed from an older image, fetch it raw from the repo first. On the VM no
token is needed — it falls back to `SCHEDULER_API_TOKEN` from
`/opt/agnes/.env`; run against a remote URL by passing
`https://agnes.<customer>.com` and an admin PAT as arguments (host-side
checks are then skipped). Set `DOCTOR_EMAIL_TO` to receive a real test email
(an HTTP 200 from the send path does NOT prove delivery). The same
server-side checks are also available as
`agnes admin doctor --new-instance [--email-to you@example.com]`.

Then trigger the first sync (populates data from Keboola / other source):

```bash
PROD_IP=$(cd terraform && terraform output -raw prod_ip)
curl -X POST "http://$PROD_IP:8000/api/sync/trigger" \
     -H "Authorization: Bearer $ADMIN_JWT"
```

## 9. Monitoring + backup

The `customer-instance` module already provisions:
- **Per-VM uptime check + alert policy** — wire a `notification_channel_ids` tfvar to get paged (see § Monitoring alerts below).
- **Daily snapshot of `/data` PD** — 30-day retention. See § Restoring from backup.
- **Host-side watchdog + daily DB backup with restore-verification** (module ≥ the tag introducing `enable_watchdog`; on by default). The watchdog checks container logs every 5 minutes for incident signatures the uptime check cannot see — crash loops, the "zombie" state where `/api/health` stays 200 while every write 500s, WAL-salvage data-loss events — and the daily backup copies `system.duckdb` to `/data/backups/system-duckdb/` and proves the copy restorable. Set `alert_webhook_url` (Slack / Google Chat compatible incoming webhook) to receive alerts; left empty, alerts go to journald + `/var/log/agnes-watchdog.log` on the VM only.

Optional add-on: Slack webhook from Cloud Monitoring for alerts.

## 10. First analyst

Nothing instance-specific has to be handed to analysts — no scripts, no
credentials over chat. Grant their group its data packages and plugin grants
first (`/admin/access`), otherwise setup completes correctly but against an empty
catalog. Then point them at the instance URL and let `/home` do the rest:

1. Sign in with the company email (whichever identity provider `instance.yaml`
   enables).
2. Follow the guided steps on `/home` — install Claude Code, create the
   workspace folder, open a terminal in it, save the login token to
   `~/.agnes/token`, launch Claude Code there.
3. Paste the install prompt from the final step. It is deliberately thin: it
   installs the `agnes` CLI and then runs `agnes onboard --workspace .`, which
   is the actual setup — workspace-directory check → `agnes init` (workspace
   files, Claude Code hooks, first `agnes pull`) → catalog smoke test → `git` /
   `claude` preflight → marketplace registration → `agnes diagnose` → a summary
   ending in a `NEXT:` block. Idempotent, so a re-run repairs a half-finished
   workspace instead of duplicating it.
4. Restart Claude Code as the summary instructs, then start asking questions.

Two things this deliberately does *not* do: it never embeds the analyst's token
in the prompt text (the token is written to `~/.agnes/token` in step 2 and read
via `--token-file`), and it does not set up data-source connectors. Connectors
are conversational and come later — an analyst asks Claude Code to "set up Jira"
in the workspace, or lists what is available with `agnes connectors list` /
`agnes connectors show <slug>`.

To verify the path end-to-end yourself, run it once on your own machine against
the fresh instance: the run should end with `agnes diagnose` healthy, and — on a
Let's Encrypt instance — with no TLS-trust step in the pasted prompt at all
(see [DEPLOYMENT.md § TLS](DEPLOYMENT.md#tls-optional)).

## Ongoing maintenance

- **App auto-upgrades** (cron every 5 min) to latest `:stable` if `upgrade_mode = "auto"`. Else Renovate will open PR on new `stable-YYYY.MM.N`.
- **Infra module upgrade:** change `ref=infra-vX.Y.Z` in `terraform/main.tf`, PR → plan → merge → apply. (Renovate opens these PRs automatically when enabled.)
- **Add dev VM for a branch:** add entry to `dev_instances` list with `image_tag = "dev-feature-xyz"`, PR, merge, apply.
- **Token rotation:** `gcloud secrets versions add keboola-storage-token --data-file=-` then run the auto-upgrade script on each VM:
  ```bash
  gcloud compute ssh agnes-prod --zone=... --project=... --command="sudo /usr/local/bin/agnes-auto-upgrade.sh"
  ```
  Or restart containers directly: `sudo docker compose -f ... restart app`.

## Propagating module (startup-script) changes

**Important gotcha:** The `customer-instance` module has `lifecycle { ignore_changes = [metadata_startup_script] }` on VMs — intentional, so `terraform apply` doesn't reboot VMs on every rerun. The consequence is that **startup-script changes are not picked up on a normal `terraform apply`**.

After bumping the module ref (e.g. `ref=infra-v1.5.0` → `infra-v1.6.0`), do one of:

### Option A — Workflow dispatch with `recreate_targets` (recommended)

`apply.yml` has a `workflow_dispatch` input `recreate_targets` that takes a comma-separated list of TF resource addresses and passes each as `-replace=` to `terraform apply`. Use this to destroy + recreate VMs with the new startup script, without any SSH.

```
Actions → Terraform Apply → Run workflow → recreate_targets:
  module.agnes.google_compute_instance.vm["agnes-dev"],module.agnes.google_compute_instance.vm["agnes-prod"]
```

The workflow routes dev targets to `apply-dev` and prod targets to `apply-prod`, so the usual dev-first + prod-reviewer gate still applies. Persistent data disks and static IPs are separate resources and are **preserved** across replacement — only the VM (and its fresh boot disk) is recreated.

Downtime: ~2 min per VM, sequential. Data loss: none (persistent disk keeps `/data`; static IP keeps URL stable).

**Secrets across recreate:** the fresh boot disk gets a re-minted `SCHEDULER_API_TOKEN` (harmless — it is only a shared secret between the app and scheduler containers on the same VM). `AGNES_VAULT_KEY` (module ≥ `infra-v1.10.0`) is generated on first boot and persisted at `/data/state/agnes-vault.key` on the data disk, so admin-stored secrets (datasource, Slack, MCP) stay decryptable across recreates. Never delete or rotate that file casually — losing the key permanently orphans every vault-encrypted secret.

### Option B — Local terraform (emergency)

```bash
export GOOGLE_APPLICATION_CREDENTIALS=~/.agnes-keys/agnes-deploy-<project>-key.json
cd terraform
terraform apply -replace='module.agnes.google_compute_instance.vm["agnes-prod"]'
```

Same semantics as Option A, but no CI audit trail. Use only when CI is broken.

### Do NOT

Do not manually edit `/opt/agnes/.env` or the docker-compose overlay files on a running VM. Any such change is lost on the next VM recreate, and it drifts from Terraform state. If a value needs changing, route it through a module variable or a module upgrade.

## Restoring from backup

Daily snapshots of each data disk are created automatically (module ≥ `infra-v1.3.0`). Retention: 30 days.

To restore:

```bash
# List snapshots for a specific disk
gcloud compute snapshots list --project=<GCP_PROJECT_ID> \
    --filter="sourceDisk~agnes-prod-data"

# Create a new disk from a snapshot
gcloud compute disks create agnes-prod-data-restored \
    --source-snapshot=<SNAPSHOT_NAME> \
    --zone=europe-west1-b \
    --type=pd-ssd \
    --project=<GCP_PROJECT_ID>

# Stop the VM, swap disks:
gcloud compute instances stop agnes-prod --zone=...
gcloud compute instances detach-disk agnes-prod --disk=agnes-prod-data --zone=...
gcloud compute instances attach-disk agnes-prod --disk=agnes-prod-data-restored --device-name=data --zone=...
gcloud compute instances start agnes-prod --zone=...

# Verify /api/health, then optionally delete the old disk
```

For Terraform state consistency after manual disk swap, you may need `terraform state rm` + `terraform import` for the disk resource.

## Monitoring alerts

Module ≥ `infra-v1.3.0` creates per-VM uptime checks + alert policies. To receive notifications, wire a Monitoring notification channel:

```bash
# Email channel
gcloud alpha monitoring channels create \
    --display-name="Agnes ops email" \
    --type=email \
    --channel-labels=email_address=ops@<customer>.com \
    --project=<GCP_PROJECT_ID>

# Get the channel ID, then in terraform.tfvars:
#   notification_channel_ids = ["projects/<project>/notificationChannels/<id>"]
# terraform apply
```

For Slack integrations, use type `slack` with a webhook URL.

## Keeping the template up-to-date (maintainer note)

New customers clone `keboola/agnes-infra-template` — so the template's `terraform/main.tf` must always point at the **latest stable** `infra-v*` tag. Two cooperating mechanisms keep it current:

1. **Upstream release hook** (`.github/workflows/propagate-infra-tag.yml` in `keboola/agnes-the-ai-analyst`): on push of any `infra-v*` tag, opens a PR in the template repo that bumps the module ref. Requires a repository secret `TEMPLATE_REPO_TOKEN` (fine-grained PAT or GitHub App token with `Contents:write` + `Pull requests:write` on the template repo). Without the secret, the job is skipped — fail-soft.

2. **Renovate on the template repo**: tracks `infra-v*` tags on polling cycles as a fallback when the release hook is unavailable. Config is already in `renovate.json`.

For both to land automatically (no human clicks needed):

- **`allow_auto_merge: true`** on the template repo (set via `gh api -X PATCH repos/keboola/agnes-infra-template -f allow_auto_merge=true`)
- **`automerge: true`** in `renovate.json` for minor+patch (already configured)
- **CI validate gate** (`.github/workflows/validate.yml` in the template repo — runs `terraform init -backend=false` + `terraform validate` on the PR). Renovate's `platformAutomerge` waits for this check to pass before merging.
- **Major bumps stay manual** (labeled `breaking`, `automerge: false`).

Customer-owned infra repos (e.g. `keboola/agnes-infra-keboola`) share the same Renovate config but typically leave patch/minor auto-merge **disabled** (because `terraform apply` touches live infrastructure; customers want a human to approve each bump). The template repo is different — it holds no state and doesn't touch GCP.

### One-time setup checklist

- [ ] Install Renovate GitHub App on `keboola/agnes-infra-template` and on each `keboola/agnes-infra-<customer>` repo
- [ ] Create a fine-grained PAT with `Contents:write` + `Pull requests:write` on the template repo
- [ ] Add it as `TEMPLATE_REPO_TOKEN` secret on `keboola/agnes-the-ai-analyst`
- [ ] Verify: tag a test `infra-vX.Y.Z` in upstream → PR appears in template → CI validates → auto-merges

## Decommission

```bash
cd terraform
terraform destroy
```

Then delete:
- GCS bucket `gs://agnes-<project>-tfstate` (or keep for audit)
- Service account `agnes-deploy@...`
- Secret Manager secrets (`keboola-storage-token`, `agnes-<customer>-jwt-secret`)
- GitHub private repo `<customer-org>/agnes-infra-<customer>`

## Troubleshooting

See [keboola/agnes-the-ai-analyst](https://github.com/keboola/agnes-the-ai-analyst) issues and docs.
