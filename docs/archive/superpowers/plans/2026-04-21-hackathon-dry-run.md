# Hackathon E2E Dry-Run Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Validate the full developer→dev-VM→merge→prod flow end-to-end the day before a multi-developer hackathon, so any broken link is found and fixed before participants arrive.

**Architecture:** This is an operational dry-run, not a code feature. The executing agent pushes a throwaway feature branch to the public repo, verifies that CI produces a per-branch Docker image tag on GHCR, switches the shared `agnes-dev` VM onto that tag via the existing auto-upgrade cron, verifies that the CI test gate blocks a deliberately-broken PR from reaching `:stable`, and produces a helper script + report. The plan is **strictly non-destructive for prod** — prod-pinning (point 6 of the original outline) is explicitly out of scope and left to the user.

**Tech Stack:** Bash / `gcloud` / `gh` / `git` / `docker` / `curl` / Python (`pytest`) / Terraform (plan only, no apply). No app code changes.

---

## Out of Scope (do NOT do)

- Any `terraform apply` against real infrastructure. TF `plan` is allowed; TF `apply` is forbidden.
- Pinning `prod_instance.image_tag` in `agnes-infra-keboola`. User will do this themselves after the dry-run succeeds.
- Rotating admin passwords, Keboola tokens, or JWT secrets.
- Modifying `main` branch of any repo. All changes happen on throwaway branches, which are deleted at the end.
- Creating new GCP resources (VMs, disks, IPs, secrets, SAs).

If any step would require doing one of the above, **STOP and ask the user**.

---

## Prerequisites

Before starting, the executing agent MUST verify all of the following. If any fails, abort and report which prerequisite is missing — do NOT try to fix it.

- [ ] **Working directory** is the `tmp_oss` checkout at `/Users/zdeneksrotyr/Library/Mobile Documents/com~apple~CloudDocs/Sources/VsCode/component_factory/tmp_oss`. Current branch can be anything; the plan will create a new branch.

- [ ] **`gh auth status`** shows authenticated, with `workflow` scope. Run:

  ```bash
  gh auth status 2>&1 | grep -E "(Logged in|Token scopes)"
  ```

  Expected: line containing `Logged in to github.com` and a line listing scopes that include `workflow`. If `workflow` scope is missing, abort with message: `Run: gh auth refresh -h github.com -s workflow`.

- [ ] **`gcloud` authenticated** to project `<gcp-project>`. Run:

  ```bash
  gcloud config get-value project
  gcloud auth list --filter=status:ACTIVE --format="value(account)"
  ```

  Expected: project is `<gcp-project>`, at least one active account. If not, abort with message: `Run: gcloud config set project <gcp-project> && gcloud auth login`.

- [ ] **SSH to `agnes-dev` works** (OS Login). Run:

  ```bash
  gcloud compute ssh agnes-dev --zone=europe-west1-b --command="echo ok" --quiet
  ```

  Expected: output contains `ok`. First connection may take ~20s while OS Login provisions. If fails with permission error, abort with message: `User needs compute.osLogin role on agnes-dev VM`.

- [ ] **`docker` CLI available** locally (for `docker manifest inspect`). Run: `docker --version`. Expected: version output. If missing, abort.

- [ ] **Public GHCR pull works**. Run:

  ```bash
  docker manifest inspect ghcr.io/keboola/agnes-the-ai-analyst:stable > /dev/null && echo ok
  ```

  Expected: `ok`. If fails, abort — something is wrong with public image visibility.

- [ ] **Clone of `agnes-infra-keboola` exists or can be cloned** at `/tmp/agnes-infra-keboola`. Run:

  ```bash
  if [ ! -d /tmp/agnes-infra-keboola ]; then
    gh repo clone keboola/agnes-infra-keboola /tmp/agnes-infra-keboola
  fi
  cd /tmp/agnes-infra-keboola && git status --short
  ```

  Expected: clone succeeds, `git status` is clean. If clone fails, skip Task 4 (TF plan verification) and note it in the final report.

**Gate:** All 7 prerequisite checks pass, OR the agent has clearly reported which ones failed and reduced scope accordingly. Only then proceed to Task 1.

---

## Task 1: Baseline Snapshot

**Purpose:** Record the current state of both VMs and the TF outputs so the agent can detect drift at the end and prove it left everything as it found it.

**Files:**
- Create: `/tmp/dryrun-baseline/prod-health.json`
- Create: `/tmp/dryrun-baseline/dev-health.json`
- Create: `/tmp/dryrun-baseline/prod-image.txt`
- Create: `/tmp/dryrun-baseline/dev-image.txt`
- Create: `/tmp/dryrun-baseline/dev-env.txt`

- [ ] **Step 1.1: Create baseline directory**

  ```bash
  mkdir -p /tmp/dryrun-baseline
  ```

- [ ] **Step 1.2: Capture prod health**

  ```bash
  curl -sf --max-time 10 http://<prod-vm-ip>:8000/api/health > /tmp/dryrun-baseline/prod-health.json
  cat /tmp/dryrun-baseline/prod-health.json | python3 -m json.tool
  ```

  Expected: JSON with `"status"` field equal to `"healthy"` or `"degraded"`. If `"unhealthy"` or curl times out, abort with message: `Prod is not in acceptable baseline state — investigate before dry-run`.

- [ ] **Step 1.3: Capture dev health**

  ```bash
  curl -sf --max-time 10 http://<dev-vm-ip>:8000/api/health > /tmp/dryrun-baseline/dev-health.json
  cat /tmp/dryrun-baseline/dev-health.json | python3 -m json.tool
  ```

  Expected: JSON with `"status"` in `{healthy, degraded}`. Same abort condition as 1.2.

- [ ] **Step 1.4: Capture current image tags on both VMs**

  ```bash
  gcloud compute ssh agnes-prod --zone=europe-west1-b --quiet --command \
    "docker inspect \$(docker ps -qf name=app) --format '{{.Config.Image}}'" \
    > /tmp/dryrun-baseline/prod-image.txt
  gcloud compute ssh agnes-dev --zone=europe-west1-b --quiet --command \
    "docker inspect \$(docker ps -qf name=app) --format '{{.Config.Image}}'" \
    > /tmp/dryrun-baseline/dev-image.txt
  cat /tmp/dryrun-baseline/prod-image.txt /tmp/dryrun-baseline/dev-image.txt
  ```

  Expected: each file contains exactly one line like `ghcr.io/keboola/agnes-the-ai-analyst:stable` or `:stable-2026.04.XX`. Non-empty.

- [ ] **Step 1.5: Capture `agnes-dev` `.env` AGNES_TAG line**

  ```bash
  gcloud compute ssh agnes-dev --zone=europe-west1-b --quiet --command \
    "sudo grep -E '^AGNES_TAG=' /data/.env || echo 'AGNES_TAG_NOT_SET'" \
    > /tmp/dryrun-baseline/dev-env.txt
  cat /tmp/dryrun-baseline/dev-env.txt
  ```

  Expected: output is `AGNES_TAG=dev` or similar. Record exact value for restoration in Task 6. If `AGNES_TAG_NOT_SET`, abort — the VM is in an unknown config state.

- [ ] **Step 1.6: Record baseline to report buffer**

  Append to a running report at `/tmp/dryrun-report.md` (create if not exists):

  ```bash
  cat > /tmp/dryrun-report.md <<EOF
  # Hackathon Dry-Run Report

  **Run at:** $(date -u +"%Y-%m-%dT%H:%M:%SZ")

  ## Baseline (Task 1)

  - Prod health status: $(jq -r '.status' /tmp/dryrun-baseline/prod-health.json)
  - Dev health status: $(jq -r '.status' /tmp/dryrun-baseline/dev-health.json)
  - Prod image: $(cat /tmp/dryrun-baseline/prod-image.txt)
  - Dev image: $(cat /tmp/dryrun-baseline/dev-image.txt)
  - Dev AGNES_TAG: $(cat /tmp/dryrun-baseline/dev-env.txt)

  EOF
  cat /tmp/dryrun-report.md
  ```

  Expected: report file exists, all fields populated (no empty values).

**Task 1 gate:** baseline directory has 5 non-empty files, report has 5 non-empty bullet lines. Proceed.

---

## Task 2: Verify Per-Branch GHCR Build

**Purpose:** Push a throwaway feature branch to the public repo, wait for the release workflow, and confirm that the per-branch `:dev-<slug>` tag appears on GHCR.

**Files:**
- Create (throwaway): branch `feature/hack-dryrun-<timestamp>` in `tmp_oss` + one trivial commit touching `docs/QUICKSTART.md`

**Branch naming:** the agent MUST use `feature/hack-dryrun-<epoch>` (e.g. `feature/hack-dryrun-1745254321`) so the slug is unique per run and cleanup is deterministic.

- [ ] **Step 2.1: Compute branch name and expected slug**

  Per `.github/workflows/release.yml:92-98` logic: strip `feature/` prefix, sanitise `[^a-zA-Z0-9-]` to `-`, lowercase, cut 50 chars.

  ```bash
  EPOCH=$(date +%s)
  BRANCH="feature/hack-dryrun-${EPOCH}"
  SLUG=$(echo "$BRANCH" | sed 's|^feature/||' | sed 's|[^a-zA-Z0-9-]|-|g' | tr '[:upper:]' '[:lower:]' | cut -c1-50)
  echo "BRANCH=$BRANCH"
  echo "SLUG=$SLUG"
  echo "EXPECTED_TAG=ghcr.io/keboola/agnes-the-ai-analyst:dev-$SLUG"
  # Persist for later steps
  echo "$BRANCH" > /tmp/dryrun-baseline/branch-name.txt
  echo "$SLUG" > /tmp/dryrun-baseline/slug.txt
  ```

  Expected: BRANCH like `feature/hack-dryrun-1745254321`, SLUG like `hack-dryrun-1745254321`. Persisted.

- [ ] **Step 2.2: Create branch with trivial commit**

  ```bash
  cd "/Users/zdeneksrotyr/Library/Mobile Documents/com~apple~CloudDocs/Sources/VsCode/component_factory/tmp_oss"
  # Save current branch so we can return
  git rev-parse --abbrev-ref HEAD > /tmp/dryrun-baseline/starting-branch.txt
  BRANCH=$(cat /tmp/dryrun-baseline/branch-name.txt)
  git checkout -b "$BRANCH"
  echo "<!-- dryrun $(date -u +%FT%TZ) -->" >> docs/QUICKSTART.md
  git add docs/QUICKSTART.md
  git commit -m "dryrun: verify per-branch GHCR tag"
  git push -u origin "$BRANCH"
  ```

  Expected: branch created, one commit, push succeeds with upstream tracking. If push is rejected (e.g. protection), abort.

- [ ] **Step 2.3: Wait for release workflow to complete**

  ```bash
  cd "/Users/zdeneksrotyr/Library/Mobile Documents/com~apple~CloudDocs/Sources/VsCode/component_factory/tmp_oss"
  BRANCH=$(cat /tmp/dryrun-baseline/branch-name.txt)
  # Get the most recent run id for this branch + workflow
  sleep 10  # give GH a moment to register the run
  RUN_ID=$(gh run list --branch "$BRANCH" --workflow release.yml --limit 1 --json databaseId --jq '.[0].databaseId')
  echo "Watching run $RUN_ID"
  gh run watch "$RUN_ID" --exit-status --interval 15
  echo "Workflow exit: $?"
  ```

  Expected: exit status 0 after ~3-5 min. If exit != 0, print the logs:

  ```bash
  gh run view "$RUN_ID" --log-failed | tail -100
  ```

  and abort with message: `Release workflow failed for throwaway branch — investigate before hackathon`.

- [ ] **Step 2.4: Verify per-branch tag exists on GHCR**

  ```bash
  SLUG=$(cat /tmp/dryrun-baseline/slug.txt)
  EXPECTED="ghcr.io/keboola/agnes-the-ai-analyst:dev-$SLUG"
  docker manifest inspect "$EXPECTED" > /tmp/dryrun-baseline/ghcr-manifest.json
  DIGEST=$(jq -r '.config.digest // .manifests[0].digest' /tmp/dryrun-baseline/ghcr-manifest.json)
  echo "Tag exists: $EXPECTED"
  echo "Digest: $DIGEST"
  echo "$DIGEST" > /tmp/dryrun-baseline/expected-digest.txt
  ```

  Expected: `docker manifest inspect` returns JSON (exit 0), a non-empty digest is extracted. If the tag is missing, abort with message: `release.yml did not produce :dev-<slug> tag — check build-and-push step logs`.

- [ ] **Step 2.5: Record Task 2 result**

  ```bash
  SLUG=$(cat /tmp/dryrun-baseline/slug.txt)
  cat >> /tmp/dryrun-report.md <<EOF
  ## Task 2: Per-Branch GHCR Build — PASS

  - Branch: $(cat /tmp/dryrun-baseline/branch-name.txt)
  - Slug: $SLUG
  - Tag: ghcr.io/keboola/agnes-the-ai-analyst:dev-$SLUG
  - Digest: $(cat /tmp/dryrun-baseline/expected-digest.txt)

  EOF
  ```

**Task 2 gate:** `:dev-<slug>` manifest exists. Proceed.

---

## Task 3: Dev VM Switch Flow

**Purpose:** Simulate the hackathon developer path — have the shared `agnes-dev` VM pick up the per-branch image via the existing auto-upgrade cron, verify the new image is running, then (in Task 6) roll back.

**Files touched (reversibly):**
- `/data/.env` on `agnes-dev` VM — one-line `AGNES_TAG=` change (rollback is captured in baseline from Step 1.5)

- [ ] **Step 3.1: Switch `agnes-dev` `.env` AGNES_TAG to the per-branch tag**

  ```bash
  SLUG=$(cat /tmp/dryrun-baseline/slug.txt)
  NEW_TAG="dev-$SLUG"
  gcloud compute ssh agnes-dev --zone=europe-west1-b --quiet --command "\
    sudo cp /data/.env /data/.env.dryrun-bak && \
    sudo sed -i 's|^AGNES_TAG=.*|AGNES_TAG=$NEW_TAG|' /data/.env && \
    sudo grep -E '^AGNES_TAG=' /data/.env"
  ```

  Expected: final line is `AGNES_TAG=dev-<slug>`. If sed didn't match (no `AGNES_TAG=` line existed), abort and manually investigate.

- [ ] **Step 3.2: Trigger auto-upgrade cron script immediately**

  ```bash
  gcloud compute ssh agnes-dev --zone=europe-west1-b --quiet --command \
    "sudo /usr/local/bin/agnes-auto-upgrade.sh 2>&1 | tail -30"
  ```

  Expected: output shows `docker compose pull` + `docker compose up -d` activity. If the script doesn't exist or errors, abort with message: `auto-upgrade script missing or broken on agnes-dev`.

- [ ] **Step 3.3: Wait for app container to become healthy**

  ```bash
  # Poll /api/health for up to 90s
  for i in $(seq 1 30); do
    STATUS=$(curl -s --max-time 5 http://<dev-vm-ip>:8000/api/health | jq -r '.status' 2>/dev/null || echo "down")
    echo "[$i/30] status=$STATUS"
    if [ "$STATUS" = "healthy" ] || [ "$STATUS" = "degraded" ]; then
      break
    fi
    sleep 3
  done
  [ "$STATUS" = "healthy" ] || [ "$STATUS" = "degraded" ] || { echo "FAIL: dev never healthy"; exit 1; }
  ```

  Expected: reaches `healthy`/`degraded` within 90s.

- [ ] **Step 3.4: Verify the running image is the per-branch one**

  ```bash
  SLUG=$(cat /tmp/dryrun-baseline/slug.txt)
  EXPECTED_DIGEST=$(cat /tmp/dryrun-baseline/expected-digest.txt)
  RUNNING_IMAGE=$(gcloud compute ssh agnes-dev --zone=europe-west1-b --quiet --command \
    "docker inspect \$(docker ps -qf name=app) --format '{{.Image}}'")
  echo "Running image digest: $RUNNING_IMAGE"
  # The running image line will be sha256:xxxxx. Compare to the manifest digest we recorded.
  # They should match (or differ only by multi-arch manifest indirection — compare via docker inspect on remote)
  gcloud compute ssh agnes-dev --zone=europe-west1-b --quiet --command \
    "docker inspect \$(docker ps -qf name=app) --format '{{.Config.Image}}' && \
     docker image inspect \$(docker ps -qf name=app --format '{{.Image}}' | head -1) --format '{{.RepoTags}}{{.RepoDigests}}'"
  ```

  Expected: `RepoTags` or `RepoDigests` output includes either `:dev-$SLUG` or the digest from Step 2.4. If neither matches, the cron didn't pull the new tag — record as FAIL and continue (cleanup is still required).

- [ ] **Step 3.5: Record Task 3 result**

  The agent must judge PASS/FAIL based on Step 3.4 output: PASS iff `RepoTags` or `RepoDigests` contained `:dev-$SLUG` or the digest captured in Step 2.4.

  ```bash
  SLUG=$(cat /tmp/dryrun-baseline/slug.txt)
  # Replace <RESULT> with PASS or FAIL based on the Step 3.4 output the agent observed.
  # Replace <IMAGE_OUTPUT> with the RepoTags/RepoDigests line from Step 3.4.
  # Replace <SECONDS> with the loop iteration count from Step 3.3 × 3.
  cat >> /tmp/dryrun-report.md <<EOF
  ## Task 3: Dev VM Switch — <RESULT>

  - Switched agnes-dev to AGNES_TAG=dev-$SLUG
  - Health after switch: reached healthy/degraded within 90s
  - Running image: <IMAGE_OUTPUT>
  - Time from cron trigger to healthy: <SECONDS>s

  EOF
  ```

**Task 3 gate:** health reached OK state; running image verified. Proceed even if image verification was inconclusive — rollback still required.

---

## Task 4: Terraform Plan Verification (Private Repo)

**Purpose:** Validate that adding a new entry to `dev_instances` produces a clean `terraform plan` (not apply) in `agnes-infra-keboola`. This proves the TF module accepts the variable shape the hackathon docs will recommend.

**Skip condition:** If prerequisites check found that `/tmp/agnes-infra-keboola` clone failed, skip this entire task and record `SKIPPED — repo unavailable` in the report.

**Files touched (throwaway branch only):**
- `/tmp/agnes-infra-keboola/terraform/terraform.tfvars` (throwaway edit)

- [ ] **Step 4.1: Create throwaway branch in private repo**

  ```bash
  cd /tmp/agnes-infra-keboola
  git checkout main
  git pull
  EPOCH=$(date +%s)
  BRANCH="dryrun-tfplan-${EPOCH}"
  echo "$BRANCH" > /tmp/dryrun-baseline/tf-branch.txt
  git checkout -b "$BRANCH"
  ```

  Expected: clean checkout of main, new branch created.

- [ ] **Step 4.2: Add throwaway dev_instance entry**

  Read `terraform/terraform.tfvars` first to understand the current `dev_instances` shape. Then append a new entry.

  The `dev_instances` variable schema (from `infra/modules/customer-instance/variables.tf:41-49`) is:
  ```hcl
  list(object({
    name         = string
    machine_type = optional(string, "e2-small")
    image_tag    = optional(string, "dev")
  }))
  ```

  Modify the `dev_instances` list to append:
  ```hcl
  { name = "agnes-hack-dryrun", image_tag = "dev-<slug-from-task-2>" }
  ```

  The agent should detect the current tfvars format and insert accordingly. If the file does not already contain `dev_instances`, abort and report format-mismatch.

  ```bash
  SLUG=$(cat /tmp/dryrun-baseline/slug.txt)
  # Show current tfvars for context
  cat /tmp/agnes-infra-keboola/terraform/terraform.tfvars | grep -A 20 "dev_instances"
  # Agent must edit the file to add the new entry — use the Edit tool rather than sed to be safe.
  ```

  After editing, show the diff:
  ```bash
  cd /tmp/agnes-infra-keboola
  git diff terraform/terraform.tfvars
  ```

  Expected: diff adds exactly one new entry to `dev_instances` list with `name = "agnes-hack-dryrun"` and `image_tag = "dev-<slug>"`.

- [ ] **Step 4.3: Run `terraform plan` locally (no apply)**

  ```bash
  cd /tmp/agnes-infra-keboola/terraform
  export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.agnes-keys/agnes-deploy-<gcp-project>-key.json"
  [ -f "$GOOGLE_APPLICATION_CREDENTIALS" ] || { echo "SA key not found — skipping plan"; exit 2; }
  terraform init -input=false -upgrade=false
  terraform plan -input=false -no-color -out=/tmp/dryrun-tfplan.bin > /tmp/dryrun-tfplan.txt 2>&1
  RC=$?
  echo "terraform plan exit: $RC"
  tail -40 /tmp/dryrun-tfplan.txt
  ```

  Expected:
  - exit 0 or 2 (2 = changes detected, which is what we want)
  - output ends with `Plan: N to add, M to change, K to destroy.` where `N >= 1` (at least the new VM + disk + IP) and `K == 0` (we must NOT be destroying anything)

  If `K > 0` or `terraform plan` errors, abort and DO NOT proceed to Step 4.4. Report the plan output verbatim in the final report.

- [ ] **Step 4.4: Discard throwaway branch (no push, no apply)**

  ```bash
  cd /tmp/agnes-infra-keboola
  git checkout main
  BRANCH=$(cat /tmp/dryrun-baseline/tf-branch.txt)
  git branch -D "$BRANCH"
  # Branch was never pushed, so nothing to clean up remotely.
  ```

  Expected: branch deleted locally, main is current, working tree clean.

- [ ] **Step 4.5: Record Task 4 result**

  ```bash
  ADDS=$(grep -E "Plan:" /tmp/dryrun-tfplan.txt | head -1)
  DESTROYS_OK=$(grep -E "Plan:.*0 to destroy" /tmp/dryrun-tfplan.txt && echo yes || echo no)
  cat >> /tmp/dryrun-report.md <<EOF
  ## Task 4: TF Plan for New Dev VM — <PASS|SKIPPED|FAIL>

  - Plan summary: $ADDS
  - Zero destroys: $DESTROYS_OK
  - Full plan output: see /tmp/dryrun-tfplan.txt

  EOF
  ```

**Task 4 gate:** plan produced with 0 destroys and ≥1 add. Proceed.

---

## Task 5: Verify Smoke-Test Gate Blocks Broken PR

**Purpose:** Confirm that a pull request with a deliberately-failing test does NOT produce a passing CI — which is the safety net that keeps `:stable` from auto-promoting broken images to prod.

**Files touched (throwaway branch only):**
- `tests/test_dryrun_should_fail.py` (new file on throwaway branch)

**Important:** This task creates a PR (not a merge). The PR is closed without merging in Step 5.5.

- [ ] **Step 5.1: Create throwaway branch with failing test**

  ```bash
  cd "/Users/zdeneksrotyr/Library/Mobile Documents/com~apple~CloudDocs/Sources/VsCode/component_factory/tmp_oss"
  git checkout main
  git pull
  EPOCH=$(date +%s)
  BRANCH="dryrun-break-smoke-${EPOCH}"
  echo "$BRANCH" > /tmp/dryrun-baseline/smoke-branch.txt
  git checkout -b "$BRANCH"
  cat > tests/test_dryrun_should_fail.py <<'PYEOF'
  def test_intentional_fail_for_dryrun():
      """Intentional failure to verify CI gate blocks broken PRs. Remove after dryrun."""
      assert False, "dryrun: this test is supposed to fail"
  PYEOF
  git add tests/test_dryrun_should_fail.py
  git commit -m "dryrun: intentional failing test (will be reverted)"
  git push -u origin "$BRANCH"
  ```

  Expected: push succeeds.

- [ ] **Step 5.2: Open PR**

  ```bash
  cd "/Users/zdeneksrotyr/Library/Mobile Documents/com~apple~CloudDocs/Sources/VsCode/component_factory/tmp_oss"
  PR_URL=$(gh pr create --title "dryrun: verify CI gate (DO NOT MERGE)" \
    --body "Intentionally failing test to verify CI blocks bad merges. Will be closed immediately after CI result." \
    --base main)
  echo "$PR_URL" > /tmp/dryrun-baseline/pr-url.txt
  echo "Opened: $PR_URL"
  ```

  Expected: PR URL returned.

- [ ] **Step 5.3: Wait for CI `test` job to complete (expected: FAIL)**

  ```bash
  cd "/Users/zdeneksrotyr/Library/Mobile Documents/com~apple~CloudDocs/Sources/VsCode/component_factory/tmp_oss"
  BRANCH=$(cat /tmp/dryrun-baseline/smoke-branch.txt)
  sleep 15
  RUN_ID=$(gh run list --branch "$BRANCH" --workflow release.yml --limit 1 --json databaseId --jq '.[0].databaseId')
  echo "Watching run $RUN_ID (expected to FAIL)"
  # Use --exit-status WITHOUT `set -e`; we expect non-zero
  set +e
  gh run watch "$RUN_ID" --exit-status --interval 15
  EXIT=$?
  set -e
  echo "Exit code: $EXIT (non-zero is EXPECTED here)"
  ```

  Expected: exit code != 0. If exit code IS 0, that means CI passed despite `assert False` → the test suite is not being run, or the file was excluded → record as **FAIL — CI gate broken**.

- [ ] **Step 5.4: Verify PR mergeability check shows failure**

  ```bash
  PR_URL=$(cat /tmp/dryrun-baseline/pr-url.txt)
  PR_NUM=$(basename "$PR_URL")
  STATE=$(gh pr view "$PR_NUM" --json statusCheckRollup --jq '.statusCheckRollup[] | select(.name=="test") | .conclusion')
  echo "test job conclusion: $STATE"
  ```

  Expected: `FAILURE`. If `SUCCESS`, the gate is broken.

- [ ] **Step 5.5: Close PR and delete branch**

  ```bash
  cd "/Users/zdeneksrotyr/Library/Mobile Documents/com~apple~CloudDocs/Sources/VsCode/component_factory/tmp_oss"
  PR_URL=$(cat /tmp/dryrun-baseline/pr-url.txt)
  PR_NUM=$(basename "$PR_URL")
  gh pr close "$PR_NUM" --delete-branch --comment "dryrun complete — CI gate verified, closing without merge"
  # Also delete locally
  git checkout main
  BRANCH=$(cat /tmp/dryrun-baseline/smoke-branch.txt)
  git branch -D "$BRANCH" 2>/dev/null || true
  ```

  Expected: PR closed, local branch gone.

- [ ] **Step 5.6: Check whether `main` has required status checks configured**

  ```bash
  gh api repos/keboola/agnes-the-ai-analyst/branches/main/protection 2>/tmp/dryrun-protection-err.txt > /tmp/dryrun-protection.json
  RC=$?
  if [ $RC -ne 0 ]; then
    echo "No branch protection on main (or insufficient permissions to read it)"
    cat /tmp/dryrun-protection-err.txt
    PROTECTION_NOTE="NONE — branch is unprotected; broken PRs can be merged. Recommend adding 'test' as required status check."
  else
    REQUIRED=$(jq -r '.required_status_checks.contexts[]?' /tmp/dryrun-protection.json 2>/dev/null | tr '\n' ',')
    echo "Required checks: $REQUIRED"
    if echo "$REQUIRED" | grep -q "test"; then
      PROTECTION_NOTE="OK — 'test' is required."
    else
      PROTECTION_NOTE="PARTIAL — protection exists but 'test' is not required. Contexts: $REQUIRED"
    fi
  fi
  echo "$PROTECTION_NOTE" > /tmp/dryrun-baseline/protection-note.txt
  ```

  Expected: note written. Does not abort — informational only.

- [ ] **Step 5.7: Record Task 5 result**

  ```bash
  cat >> /tmp/dryrun-report.md <<EOF
  ## Task 5: CI Gate — <PASS|FAIL>

  - Throwaway PR: $(cat /tmp/dryrun-baseline/pr-url.txt) (closed)
  - CI 'test' job result on broken code: <FAILURE expected>
  - Branch protection on main: $(cat /tmp/dryrun-baseline/protection-note.txt)

  EOF
  ```

**Task 5 gate:** broken PR's CI status is FAILURE. Proceed. If `PROTECTION_NOTE` says NONE/PARTIAL, the final report must flag this as a **hackathon-blocking recommendation**.

---

## Task 6: Cleanup and Baseline Restoration

**Purpose:** Leave the system in exactly the state recorded in Task 1. This is the most important task — a dirty dry-run poisons the hackathon.

- [ ] **Step 6.1: Restore `agnes-dev` AGNES_TAG**

  ```bash
  ORIG_LINE=$(cat /tmp/dryrun-baseline/dev-env.txt)
  # ORIG_LINE looks like: AGNES_TAG=dev
  ORIG_VALUE=$(echo "$ORIG_LINE" | cut -d= -f2-)
  gcloud compute ssh agnes-dev --zone=europe-west1-b --quiet --command "\
    sudo sed -i 's|^AGNES_TAG=.*|AGNES_TAG=$ORIG_VALUE|' /data/.env && \
    sudo rm -f /data/.env.dryrun-bak && \
    sudo grep -E '^AGNES_TAG=' /data/.env && \
    sudo /usr/local/bin/agnes-auto-upgrade.sh 2>&1 | tail -20"
  ```

  Expected: AGNES_TAG line matches original, auto-upgrade pulls back to the original tag.

- [ ] **Step 6.2: Wait for dev VM to return to healthy state on original tag**

  ```bash
  for i in $(seq 1 30); do
    STATUS=$(curl -s --max-time 5 http://<dev-vm-ip>:8000/api/health | jq -r '.status' 2>/dev/null || echo down)
    echo "[$i/30] status=$STATUS"
    [ "$STATUS" = "healthy" ] || [ "$STATUS" = "degraded" ] && break
    sleep 3
  done
  ```

  Expected: reaches healthy/degraded within 90s.

- [ ] **Step 6.3: Verify running image matches baseline**

  ```bash
  RESTORED=$(gcloud compute ssh agnes-dev --zone=europe-west1-b --quiet --command \
    "docker inspect \$(docker ps -qf name=app) --format '{{.Config.Image}}'")
  ORIG=$(cat /tmp/dryrun-baseline/dev-image.txt)
  echo "Restored: $RESTORED"
  echo "Original: $ORIG"
  [ "$RESTORED" = "$ORIG" ] && echo MATCH || echo "MISMATCH — investigate"
  ```

  Expected: MATCH. If MISMATCH, the baseline-tag digest may have advanced (auto-upgrade pulled newer `:stable`/`:dev` floating image during the run) — that is acceptable as long as the `.Config.Image` *tag* matches. Record exact difference in report.

- [ ] **Step 6.4: Delete throwaway branches in public repo**

  ```bash
  cd "/Users/zdeneksrotyr/Library/Mobile Documents/com~apple~CloudDocs/Sources/VsCode/component_factory/tmp_oss"
  STARTING=$(cat /tmp/dryrun-baseline/starting-branch.txt)
  git checkout "$STARTING"
  FEAT_BRANCH=$(cat /tmp/dryrun-baseline/branch-name.txt)
  SMOKE_BRANCH=$(cat /tmp/dryrun-baseline/smoke-branch.txt 2>/dev/null || echo "")
  # Local delete
  git branch -D "$FEAT_BRANCH" 2>/dev/null || true
  [ -n "$SMOKE_BRANCH" ] && git branch -D "$SMOKE_BRANCH" 2>/dev/null || true
  # Remote delete (smoke branch was already deleted via `gh pr close --delete-branch` in Step 5.5)
  git push origin --delete "$FEAT_BRANCH" 2>/dev/null || echo "(feature branch already gone)"
  ```

  Expected: local branches gone, remote feature branch deleted. QUICKSTART.md commit on throwaway branch vanishes from origin.

- [ ] **Step 6.5: Final health check on prod (must match baseline)**

  ```bash
  curl -sf --max-time 10 http://<prod-vm-ip>:8000/api/health > /tmp/dryrun-baseline/prod-health-after.json
  BEFORE=$(jq -r '.status' /tmp/dryrun-baseline/prod-health.json)
  AFTER=$(jq -r '.status' /tmp/dryrun-baseline/prod-health-after.json)
  echo "Prod status before: $BEFORE / after: $AFTER"
  [ "$BEFORE" = "$AFTER" ] && echo UNCHANGED || echo DRIFT
  ```

  Expected: UNCHANGED. (Note: prod was never touched, so this is sanity only.)

- [ ] **Step 6.6: Record Task 6 result**

  ```bash
  cat >> /tmp/dryrun-report.md <<EOF
  ## Task 6: Cleanup — <PASS|FAIL>

  - agnes-dev AGNES_TAG restored to: $(cat /tmp/dryrun-baseline/dev-env.txt)
  - agnes-dev health after restore: $(curl -s --max-time 5 http://<dev-vm-ip>:8000/api/health | jq -r '.status')
  - agnes-dev image: matches baseline? <MATCH|MISMATCH — paste both>
  - Throwaway branches deleted: feature, smoke
  - Prod status unchanged: <UNCHANGED|DRIFT>

  EOF
  ```

**Task 6 gate:** dev VM back on its baseline tag, branches gone, prod untouched.

---

## Task 7: Generate Deliverables

**Purpose:** Produce the artefacts the user needs tomorrow: a helper script for the hackathon team and a consolidated report.

**Files:**
- Create: `scripts/switch-dev-vm.sh` (new)
- Create (already being built): `/tmp/dryrun-report.md`

- [ ] **Step 7.1: Write `scripts/switch-dev-vm.sh`**

  Create file at `scripts/switch-dev-vm.sh`:

  ```bash
  #!/usr/bin/env bash
  # switch-dev-vm.sh — point the shared hackathon dev VM at the caller's branch image.
  #
  # Usage:
  #   scripts/switch-dev-vm.sh <branch-slug>
  #   scripts/switch-dev-vm.sh hack-zs-metrics
  #
  # Prerequisite: your branch has been pushed and the release.yml workflow has completed,
  # producing ghcr.io/keboola/agnes-the-ai-analyst:dev-<slug>.
  #
  # The slug is derived from your branch name by stripping the leading "feature/" and
  # replacing non-alphanumeric chars with "-". For branch "feature/hack-zs-metrics" the slug
  # is "hack-zs-metrics".
  set -euo pipefail

  if [ $# -ne 1 ]; then
    echo "Usage: $0 <branch-slug>" >&2
    echo "Example: $0 hack-zs-metrics" >&2
    exit 2
  fi

  SLUG="$1"
  VM="agnes-dev"
  ZONE="europe-west1-b"
  TAG="dev-$SLUG"
  IMAGE="ghcr.io/keboola/agnes-the-ai-analyst:$TAG"

  echo "[1/4] Verifying $IMAGE exists on GHCR..."
  docker manifest inspect "$IMAGE" > /dev/null || {
    echo "ERROR: $IMAGE not found on GHCR. Did your release.yml run finish?" >&2
    echo "Check: gh run list --branch feature/$SLUG --workflow release.yml" >&2
    exit 1
  }

  echo "[2/4] Updating AGNES_TAG on $VM to $TAG..."
  gcloud compute ssh "$VM" --zone="$ZONE" --quiet --command "\
    sudo sed -i 's|^AGNES_TAG=.*|AGNES_TAG=$TAG|' /data/.env && \
    sudo grep -E '^AGNES_TAG=' /data/.env"

  echo "[3/4] Triggering auto-upgrade..."
  gcloud compute ssh "$VM" --zone="$ZONE" --quiet --command \
    "sudo /usr/local/bin/agnes-auto-upgrade.sh 2>&1 | tail -10"

  echo "[4/4] Waiting for app to become healthy..."
  for i in $(seq 1 30); do
    STATUS=$(curl -s --max-time 5 http://<dev-vm-ip>:8000/api/health | python3 -c 'import sys,json; print(json.load(sys.stdin).get("status","down"))' 2>/dev/null || echo down)
    echo "  [$i/30] status=$STATUS"
    if [ "$STATUS" = "healthy" ] || [ "$STATUS" = "degraded" ]; then
      echo "OK — agnes-dev now running $TAG. Open http://<dev-vm-ip>:8000"
      exit 0
    fi
    sleep 3
  done
  echo "ERROR: agnes-dev did not become healthy in 90s. SSH in and check: docker compose logs" >&2
  exit 1
  ```

  ```bash
  chmod +x scripts/switch-dev-vm.sh
  bash -n scripts/switch-dev-vm.sh  # syntax check
  ```

  Expected: syntax-check passes, file executable.

- [ ] **Step 7.2: Commit the script on a fresh branch and open PR**

  ```bash
  cd "/Users/zdeneksrotyr/Library/Mobile Documents/com~apple~CloudDocs/Sources/VsCode/component_factory/tmp_oss"
  git checkout -b feature/hackathon-dryrun-deliverables
  git add scripts/switch-dev-vm.sh
  git commit -m "chore: add switch-dev-vm.sh helper for hackathon"
  git push -u origin HEAD
  gh pr create --title "chore: add switch-dev-vm.sh helper for hackathon" \
    --body "Adds scripts/switch-dev-vm.sh. Produced by the 2026-04-21 hackathon dry-run. Reviewed by user before merge." \
    --base main > /tmp/dryrun-baseline/deliverable-pr.txt
  cat /tmp/dryrun-baseline/deliverable-pr.txt
  ```

  Expected: PR URL. **Do not merge** — leave for user review.

- [ ] **Step 7.3: Finalise report with overall verdict**

  Determine overall verdict by inspecting each Task's PASS/FAIL line in `/tmp/dryrun-report.md`. Overall is PASS only if all tasks PASS (SKIPPED Task 4 is acceptable — note it).

  Append to report:

  ```bash
  cat >> /tmp/dryrun-report.md <<EOF
  ---

  ## Overall Verdict

  <PASS | PASS WITH GAPS | FAIL>

  ## Recommendations for the User Before Hackathon Starts

  1. <If protection-note said NONE/PARTIAL:> Configure required status check 'test' on main branch of keboola/agnes-the-ai-analyst.
  2. Pin prod image_tag in agnes-infra-keboola/terraform/terraform.tfvars from "stable" to "stable-2026.04.XX" (current running version). Revert after hackathon.
  3. Rotate admin password '<password>' on prod (<prod-vm-ip>:8000/login) and dev (<dev-vm-ip>:8000/login).
  4. Wire notification_channel_ids in tfvars so uptime alerts actually notify someone.
  5. Share the hackathon 1-pager + switch-dev-vm.sh via the team Slack channel.
  6. Review PR $(cat /tmp/dryrun-baseline/deliverable-pr.txt) and merge if switch-dev-vm.sh looks good.

  ## Artefacts

  - Full report: /tmp/dryrun-report.md (this file)
  - Baseline snapshots: /tmp/dryrun-baseline/*.{json,txt}
  - TF plan output: /tmp/dryrun-tfplan.txt (if Task 4 ran)
  - Deliverable PR: $(cat /tmp/dryrun-baseline/deliverable-pr.txt)

  EOF
  cat /tmp/dryrun-report.md
  ```

  Expected: full report printed.

- [ ] **Step 7.4: Print final summary to chat**

  Agent should output, in its final message to the user:
  - Overall verdict (one line)
  - Each task's result (one line each)
  - Any unresolved anomalies
  - Link to deliverable PR
  - Path to full report

**Task 7 gate:** report complete, PR open, all artefacts listed.

---

## Abort / Rollback Procedures

If any task fails mid-execution, the agent must still perform Task 6 cleanup before reporting failure. Specifically:

- If Task 2 push succeeded but Task 3 failed → still run Task 6 Steps 6.1-6.4 to restore dev VM and delete the branch.
- If Task 5 PR was opened but workflow didn't finish → close the PR with `gh pr close --delete-branch` and log it.
- If Task 4 TF plan showed destroys → abort immediately, do NOT attempt apply, record in report, continue to Task 6.

If Task 6 itself fails (dev VM won't come back healthy on original tag), the agent must:
1. Print the baseline values (from `/tmp/dryrun-baseline/dev-env.txt`, `/tmp/dryrun-baseline/dev-image.txt`) so the user can manually SSH and fix.
2. Attempt `gcloud compute ssh agnes-dev --zone=europe-west1-b --command "docker compose -f /opt/agnes/docker-compose.yml logs --tail 100"` and include output in the report.
3. Mark overall verdict as FAIL and stop.

## What a Successful Run Looks Like

- Task 1 baseline: captured with prod+dev healthy/degraded
- Task 2: GHCR manifest exists for `:dev-hack-dryrun-<epoch>`
- Task 3: agnes-dev briefly running the per-branch image, healthy within 90s
- Task 4: `terraform plan` showed `1+ to add, 0 to destroy` (or SKIPPED)
- Task 5: CI `test` job reported FAILURE on the broken PR, PR closed
- Task 6: agnes-dev back on its baseline AGNES_TAG, healthy, branches gone
- Task 7: `scripts/switch-dev-vm.sh` committed on PR for user review, full report in `/tmp/dryrun-report.md`
- Final agent message: verdict + 6 bullet results + deliverable PR link

Duration: ~45-75 minutes, bounded primarily by CI workflow runs (~3-5 min each, two runs) and TF init (~30s-2min cold).
