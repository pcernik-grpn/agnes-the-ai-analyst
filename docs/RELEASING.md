# Releasing & deploying

The full release process for Agnes. CLAUDE.md carries the short version; this
doc is the operational reference. Read it linearly the first few times — once
internalized, the order matters less, but the **non-obvious gotchas never go
away**.

## Changelog discipline — non-negotiable

**Every PR that adds, removes, or changes user-visible behavior MUST update
`CHANGELOG.md` in the same PR.** No exceptions, no follow-ups, no "I'll do it
after merge". User-visible = anything an operator, end-user, or downstream
integrator can observe: CLI flags / output / exit codes, REST endpoints /
payloads / status codes, web UI, `instance.yaml` schema, env vars,
`extract.duckdb` contract, Docker / compose / Caddyfile knobs, default
behaviors, breaking changes, security fixes.

**How:**
- Add a bullet under the topmost `## [Unreleased]` heading (create one if
  missing — it sits above the latest released version).
- Group by `### Added` / `### Changed` / `### Fixed` / `### Removed` /
  `### Internal` (Keep-a-Changelog sections).
- Mark breaking changes with `**BREAKING**` at the start of the bullet —
  operators grep for that string before bumping the pin.
- Reference the relevant doc/runbook if one exists (e.g.
  `see docs/auth-groups.md`), don't restate it.
- Internal-only changes (refactors, test additions, dependency bumps without
  behavior change) go under `### Internal` — still log them, just keep them
  terse.

Reviewers should bounce PRs that touch user-visible behavior without a
changelog update — same way they'd bounce a PR with no test changes for new
logic.

## Release-cut is a dedicated cut PR — non-negotiable

**Feature PRs never bump `pyproject.toml`, rename `## [Unreleased]`, or touch
`server.json`'s version field. They only ever add a bullet under
`## [Unreleased]`** (see the Changelog discipline section above — unchanged).
The version bump + CHANGELOG rename is cut once a day by
`.github/workflows/daily-cut.yml`, which opens a PR labeled `release-cut` for
a human to review and merge. It never merges or tags anything itself.

This replaces the old rule ("the release-cut ships in the same PR that earns
the version"), which made whichever PR happened to land last-with-content in
`[Unreleased]` also own the version bump — in practice, whichever PR won the
race to merge. Two PRs racing for the same version number produced a
duplicated `## [X.Y.Z]` heading once merged (see § CHANGELOG merge hazards
below, kept for the rare manual-cut case), a recurring failure mode across
15–25 hand-cut releases/day. Centralizing the cut removes the race at the
root: there is exactly one writer of the version number and the rename, once
a day.

### The three version segments

- **Minor (`X.Y+1.0`) — the daily batch.** `daily-cut.yml`'s default and its
  scheduled (cron) run always use this. Every bullet accumulated under
  `[Unreleased]` since the last cut ships together.
- **Patch (`X.Y.Z+1`) — emergency hotfix only.** Dispatch `daily-cut.yml`
  manually with `bump: patch` (`gh workflow run daily-cut.yml -f bump=patch`)
  to ship immediately, outside the daily cadence. It still ships the
  *entire* current `[Unreleased]` content — if unrelated in-flight work has
  already merged, prefer waiting for the next scheduled minor cut instead.
- **Major (`X+1.0.0`) — milestone, human decision.** Dispatch with `bump:
  major` only when the team has actually decided this cut is a milestone
  boundary; never automatic.

### The train-driver role

A human (or an agent session acting for one) merges the ready PR queue
completely *first*, then reviews and merges the cut PR — in that order. Once
a cut PR is open, treat it as a short lock on `main`: merging more feature
PRs while it sits open means `main`'s `[Unreleased]` keeps growing
underneath a cut branch that already snapshotted an older state of that same
section, and merging the stale cut PR on top risks the same kind of
same-region collision this workflow exists to avoid. `daily-cut.yml` won't
open a second cut PR while one is already open (it checks for an open PR
carrying the `release-cut` label first), so the practical rule is: **flush
the queue, then merge the cut PR, in that order, before resuming feature
merges.** If the cut PR ends up showing a merge conflict against a newer
`main` (the queue wasn't fully flushed before someone merged past it), close
it and re-dispatch `daily-cut.yml` rather than resolving the conflict by
hand — a hand-resolved overlap on `[Unreleased]` is exactly the collision
class this exists to prevent. Bullets that land after a cut PR opened simply
ride into the next day's cut; that is expected, not a bug.

### Post-merge: tag + Release

Merging the cut PR to `main` triggers `release.yml`'s ordinary push-to-main
build (`:stable`, `:stable-YYYY.MM.N`) — no extra step needed for the image.
The semver tag + GitHub Release are **not** created automatically on merge;
the cut PR's body carries the exact command to run afterward:

```bash
gh workflow run tag-release.yml -f tag=vX.Y.Z
```

(`target` defaults to `main`'s current HEAD, which is correct immediately
after merging the cut PR, as long as nothing else merged in between.)
`tag-release.yml` validates the tag against `pyproject.toml` and the
`CHANGELOG.md` section at the target commit before creating anything
server-side; re-dispatching is safe (see the workflow's own comments for the
repair semantics). This is a deliberate choice over tag-on-merge automation:
a merge-commit-triggered auto-tag would need to re-derive "is this push a
cut" from the diff to avoid tagging every `main` push — more moving parts
than a copy-pasteable command in a PR body a human is already reading. The
rest of the post-merge sequence (rollback-on-smoke-fail, manual rollback,
weekly pruning) is unchanged — see below.

### Emergency path when Actions dispatch isn't available

`scripts/release_cut.py` (unit-tested in `tests/test_release_cut.py`) is the
same pure logic `daily-cut.yml` calls — run it directly when you need a cut
from an environment that can push branches but can't dispatch a workflow:

```bash
python3 scripts/release_cut.py --bump patch   # or --bump minor / --bump major
git checkout -b release-cut/v<version>
git add CHANGELOG.md pyproject.toml server.json
git commit -m "release: <version>"
git push -u origin HEAD
gh pr create --label release-cut --title "release: <version>" --body "..."
```

`--dry-run --json` prints the computed plan (next version, the bullets it
would ship) without writing anything — use it to sanity-check before
committing. An empty `[Unreleased]` is a no-op (exit 0, nothing written), so
running it speculatively is always safe.

## Release workflow — concrete recipe

### Happy path — feature PR (5 steps, no cut)

```bash
# 1. Branch from a fresh checkout. iCloud Drive worktrees randomly hang
#    on git operations — use a fresh shallow clone in /tmp instead.
cd /tmp && git clone --depth 50 --branch main \
  https://github.com/keboola/agnes-the-ai-analyst.git agnes-<topic>
cd agnes-<topic> && git checkout -b zs/<branch-name>

# 2. Make the change + tests. Run the AREA pytest while iterating
#    (e.g. `pytest tests/test_X.py -p no:xdist -q`).

# 3. Add a CHANGELOG bullet under [Unreleased].
#    Group: Added | Changed | Fixed | Removed | Internal
#    Mark BREAKING with **BREAKING** prefix.
#    Do NOT touch pyproject.toml, server.json, or the [Unreleased] heading
#    itself — that is the cut PR's job, not this one's.

# 4. Commit the change(s).

# 5. Run the full pytest suite locally:
#    `pytest tests/ -p no:xdist -q` (or `-n auto` if xdist works).
#    Pre-existing fails (e.g. test_readers_in_pre_init_dir under
#    subprocess timeout) are OK to ignore; verify by reverting your
#    diff and reproducing on bare main.

# 6. Push branch + open PR + enable auto-merge SQUASH:
#    git push -u origin HEAD
#    gh pr create --repo keboola/agnes-the-ai-analyst \
#      --head <branch> --title "<...>" --body "<...>"
#    gh pr merge <N> --repo keboola/agnes-the-ai-analyst \
#      --squash --auto --delete-branch
```

That's it for a feature PR — no version bump, no tag, no Release. The cut
(and everything after it) is a separate PR; see § Release-cut is a dedicated
cut PR above.

### Picking the version segment for a cut

See § The three version segments above: minor is the default for the daily
batch, patch is emergency-hotfix-only, major is a human milestone decision.
`scripts/release_cut.py` reads the CURRENT version straight out of
`pyproject.toml` and bumps from there — since only the cut PR itself ever
writes that field, there is no drift to reconcile and no need to cross-check
`git tag -l` before naming (the old failure mode this used to guard against).

### Authoring expectations on the PR

- **Self-PRs** (you're both author and reviewer): GitHub forbids self-approve.
  If branch protection requires N approving reviews (we don't today —
  `required_approving_review_count = 0`), you need someone else to approve. With
  our current 0-review setup, self-PRs can still merge automatically once
  required CI passes.
- **Other people's PRs you're taking over**: dismiss any prior
  CHANGES_REQUESTED reviews (yours or someone else's) before auto-merge can
  fire. `gh pr review <N> --approve --body "..."` after pushing your fixes.
- **Devin Review**: not a required check today; runs in parallel and posts a
  comment. Don't wait on it for merge unless the human reviewer explicitly asks.

### CI quirks you WILL hit

- **`gh pr checks` glosses CANCELLED as `fail`.** When you force-push (rebase,
  amend), GitHub auto-cancels the in-flight `Release` workflow run on the older
  SHA. Those cancelled jobs show up as "fail" in the PR's check summary and tab
  forever, even after newer runs succeed. **Look at the conclusion column, not
  just the count.** Rule of thumb: if the same check name appears with both
  `pass` and `fail` rows, the `fail` row is from an older auto-cancelled SHA.
  Verify with `gh api repos/keboola/agnes-the-ai-analyst/commits/<sha>/check-runs`
  — the raw API distinguishes `cancelled` from `failure` truthfully.
- **Branch protection's "strict" mode caches cancelled `test` as blocking** even
  after newer `test` runs succeed. Symptom: `mergeable_state: blocked` despite
  all required checks green on the latest SHA. Fix: re-run the cancelled
  `Release` workflow run (`gh run rerun <run-id>`); once its `test` job lands as
  success, the block clears. We've hit this on PRs #273, #281, #285, #286.
- **Required checks** (per branch protection): `test` + `docker-build` only.
  Other workflows (`cli-wheel-clean-install`, `build-and-push`,
  `Release`-pipeline, Devin Review) are advisory — green/red doesn't gate merge.
- **`enforce_admins: true`** in branch protection means `--admin` flag on
  `gh pr merge` does NOT bypass. Don't try; just fix the underlying block.
- **`lint-workflows.yml` is advisory.** Triggered on changes to
  `.github/workflows/**` or `scripts/ops/**.sh`. Runs `actionlint` on
  workflow YAMLs + `shellcheck --severity=warning` on freestanding ops
  scripts. The `actionlint` step has `continue-on-error: true` initially
  (pre-existing inventory has info-level findings); flip to fail-fast
  once the repo is actionlint-clean. The `shellcheck` step IS blocking at
  warning+ severity — info/style findings ride through, real bugs break
  CI.

### Recovery when something derails

- **Force-pushed and lost auto-merge?** GitHub *usually* preserves auto-merge
  across force-pushes for the same PR; if it cleared, just re-run
  `gh pr merge <N> --squash --auto --delete-branch`.
- **A cut PR went stale (merge conflict against a newer `main`)?** The queue
  wasn't fully flushed before something else merged past it — close the
  stale cut PR and re-dispatch `daily-cut.yml` rather than resolving the
  conflict by hand (see § The train-driver role above). Closing the PR
  leaves its `release-cut/vX.Y.Z` branch behind on the remote; there is
  nothing to clean up first — the branch is workflow-owned, and the
  re-dispatch force-pushes over it when it computes the same version.
- **Wrong version number tagged?** `git tag -d vX.Y.Z && git push --delete
  origin vX.Y.Z` then re-tag against the right SHA. Update the GitHub Release if
  you already created it.

### CHANGELOG merge hazards

**This section describes the failure mode the dedicated cut PR (above) was
built to eliminate.** It should now be rare — feature PRs never rename
`[Unreleased]` or bump the version, so a long-lived feature branch merging
`origin/main` only ever picks up a rename on main's side, not a competing
one from its own branch. Kept for the residual manual-cut path (the
"Emergency path when Actions dispatch isn't available" case above) and as a
diagnostic if an old habit resurfaces.

Merging `origin/main` into a long-lived feature branch touches `CHANGELOG.md`
on both sides almost every time — main keeps cutting releases while your
branch keeps adding `[Unreleased]` bullets. Two distinct failure modes show up
here; know which one you're looking at before you start editing.

**1. Bullet placement drift (the common one).** After the merge, a bullet you
added under `[Unreleased]` ends up living under the version section main just
released instead — because your bullet and main's release-cut both touched
the same region of the file and the merge resolved textually rather than
semantically. The fix is mechanical: move the bullet back up into
`[Unreleased]`. No headers are damaged; only bullet placement is wrong.

**2. Version-number collision (worse — malformed section, not just misplaced
content).** Symptom: after the merge, `git status` reports no conflict, but
the resulting `CHANGELOG.md` has a single `## [X.Y.Z]` section containing
bullets from **two unrelated changes**, frequently with a duplicated
subsection header (e.g. two `### Fixed` headers stacked back to back under
the same version).

- **Root cause.** A parallel/twin agent session (or another engineer) working
  the *same* feature branch independently ran its own release-cut process —
  bumping `pyproject.toml` and renaming that branch's `## [Unreleased]` to
  `## [X.Y.Z]` — without knowing `main` had already claimed and shipped that
  exact version number via a different PR. Both sides of the eventual merge
  now have a heading line that reads `## [X.Y.Z]`. Git's default merge
  strategy (`ort`) matches that line as "the same" section on both sides and
  interleaves the two bodies instead of raising a conflict, so the merge
  completes cleanly and silently produces a malformed changelog.
- **How to detect.**
  - `grep -n "^## \[" CHANGELOG.md` — every version header must appear
    exactly once. A version header that repeats, or shows up more than once
    in the output for the same `X.Y.Z`, is the signature of this bug.
  - Within the suspect version's block, count subsection headers, e.g.
    `grep -c "^### Fixed"` restricted to that block — more than one means two
    sections got interleaved under a single version header.
  - `grep -c "<distinctive phrase from your branch's own bullet>"` and the
    same for a distinctive phrase from main's real release bullet — each
    should be exactly 1. Zero means a bullet got dropped; both present under
    one duplicated-header block confirms the collision.
- **Fix pattern.**
  1. `git show origin/main:CHANGELOG.md` to pull the ground-truth content for
     the colliding version section.
  2. Diff that ground truth against the same section in your branch's
     `CHANGELOG.md` to see exactly which lines are your branch's own and
     which are main's.
  3. Move your branch's own new bullets **out** of the released version
     section and **into** `## [Unreleased]` (create it above the released
     section if the merge removed it).
  4. Restore the released version's section to match `origin/main` verbatim
     — same bullets, same subsection headers, duplicate header removed.
  5. Drop any local `pyproject.toml` version bump your branch made for that
     version — main already owns the release for that number; your branch
     shouldn't carry a redundant bump for a version it didn't actually cut.
  6. Re-run the detection greps above: each version header count is 1, no
     duplicated subsection headers, and your branch's own bullets are back
     under `[Unreleased]`.
- **Prevention.** Treat "I just cut a release-cut commit on this
  long-lived branch" as a signal to immediately fetch and check whether
  `main` claimed that exact version number in the meantime — *before*
  merging main into the branch. If it did, drop your local release-cut
  (bump + rename) and let the branch go back to accumulating under
  `[Unreleased]`; the real release-cut for your branch's own changes happens
  later, against whatever version number is actually next.

## Deploy workflows

Two separate release.yml-style workflows produce GHCR images. Pick the one that
matches what you're shipping.

### `release.yml` — auto-build on every push

Runs on **every** push to **every** branch.
- Push to `main` → `:stable`, `:stable-YYYY.MM.N` (CalVer).
- Push to non-main `<prefix>/<branch>` → `:dev`, `:dev-YYYY.MM.N`,
  `:dev-<branch-slug>`, and (when prefix isn't a Git Flow convention)
  `:dev-<prefix>-latest` alias.

VMs that pin to a floating tag (`:dev`, `:dev-<prefix>-latest`) auto-upgrade
within ~5 min via the cron in `agnes-auto-upgrade.sh`. Convenient for
per-developer dev VMs; **footgun for shared dev VMs** (last pusher wins,
regardless of who).

On a role-split (m-tier) VM, that same cron performs a sequential
`/readyz`-gated rolling recreate instead of one-shot `docker compose up -d`:
`worker`+`gateway` recreate first, then each named `api` replica one at a
time, gated on its own readiness before the next is touched. A hard failure
of the `worker`+`gateway` recreate itself, or any single replica never
reporting ready within the bounded timeout, aborts the rollout (webhook
alert, non-zero exit) **without** touching the remaining replicas — they
stay on the previous image and keep serving. See
[`DEPLOYMENT.md`](DEPLOYMENT.md) → *Multi-process* for the full mechanism,
including the `agnes-db-backup.sh` `pg_dump` + restore-canary coverage for
the on-VM Postgres side-car.

**Auto-rollback on smoke failure.** On `main` pushes, after `:stable` is
published, the `smoke-test` job pulls the just-built image and runs
`scripts/ops/post-deploy-smoke-test.sh` inside a docker-compose stack. If
that job fails, the `rollback-on-smoke-fail` job calls the reusable
`rollback.yml` workflow (see below) which re-points `:stable` to the
previous known-good build, marks the failed image as `:deprecated-*`,
and opens a tracking issue labeled `bug`.

### `rollback.yml` — reusable + manual rollback

Two entry points:
- **`workflow_call`** from `release.yml`'s `rollback-on-smoke-fail` job
  (auto-rollback path above).
- **`workflow_dispatch`** for manual operator rollback when something
  breaks post-deploy that the auto smoke-test missed.

**Manual rollback** — flip `:stable` back to a previous good build:

```bash
gh workflow run rollback.yml \
  --repo keboola/agnes-the-ai-analyst \
  -f failed_image_tag=stable-YYYY.MM.N
```

By default `target_image_tag` resolves by walking back through `stable-*`
git tags newest-first and picking the first that does NOT already carry a
`:deprecated-<stripped>` GHCR alias (i.e. wasn't previously auto-rolled-
back). That prevents cascading failures from re-pointing `:stable` at a
known-broken image. To force a specific target:

```bash
gh workflow run rollback.yml \
  --repo keboola/agnes-the-ai-analyst \
  -f failed_image_tag=stable-2026.05.531 \
  -f target_image_tag=stable-2026.04.474
```

Notes:
- The workflow does NOT delete the failed git tag (CalVer immutability is
  preserved) — only the GHCR `:stable` alias is re-pointed and the failed
  image gains a `:deprecated-*` audit alias.
- Re-tag order is `:stable` recovery first, then `:deprecated-*` audit, so
  a mid-step interruption leaves production healthy with at-worst missing
  audit metadata.
- Concurrency: `cancel-in-progress: false` (overrides the caller workflow's
  cancellation policy) so a subsequent push to `main` won't kill a
  rollback mid-flight.

### `keboola-deploy.yml` — tag-triggered, explicit deploy only

Runs **only** on git tags matching `keboola-deploy-*`. Publishes:
- `:keboola-deploy-<git-tag-suffix>` — immutable, tied to the exact commit
- `:keboola-deploy-latest` — floating alias the consumer pins to

**Operator workflow:**
```bash
git checkout <commit-or-branch>
git tag keboola-deploy-<descriptive-name>
git push origin keboola-deploy-<descriptive-name>
# → workflow builds + publishes both tags
# → VM cron picks up :keboola-deploy-latest within ~5 min
# → manual cron trigger (skip the wait): sudo /usr/local/bin/agnes-auto-upgrade.sh on the VM
```

Use this when the consumer (e.g. a customer dev VM) needs
**deploy-when-I-decide** semantics — no surprise rollouts from upstream branch
pushes by other contributors. The infra repo pins
`image_tag = "keboola-deploy-latest"` on the relevant VM.

### `prune-dev-tags.yml` — weekly CalVer + GHCR housekeeping

Cron `0 4 * * 0` (Sundays 04:00 UTC) + `workflow_dispatch`. Prunes legacy
CalVer git tags (`dev-YYYY.MM.N`, `stable-YYYY.MM.N`) and the matching
GHCR image versions older than `KEEP_MONTHS` (default `1` → keep current
+ previous month). Floating aliases (`:stable`, `:dev`, `*-latest`) are
never matched: they are git-tagless, and the GHCR pass explicitly skips
any version that shares a manifest with a floating alias to avoid
collateral deletion of `:stable` after a rollback re-tag.

**Manual preview** (no deletions, lists what would be pruned):

```bash
gh workflow run prune-dev-tags.yml \
  --repo keboola/agnes-the-ai-analyst \
  -f dry_run=true
```

**Force a wider window** (one-off aggressive cleanup):

```bash
gh workflow run prune-dev-tags.yml \
  --repo keboola/agnes-the-ai-analyst \
  -f keep_months=3
```

Scheduled (cron) runs always prune for real; `dry_run` is honored only on
manual dispatch. The script tracks per-tag remote-push / GHCR-DELETE
failures and exits non-zero on any failure, so a refused remote push (tag-
protection rule, missing scope) or a GHCR API error turns the cron run
red instead of silently swallowing it. Local `git tag -d` is gated on
successful remote push, so a refused delete leaves the local tag in place
for retry on the next run.

### Module versioning

The customer-instance Terraform module under `infra/modules/customer-instance/`
is published as `infra-vMAJOR.MINOR.PATCH` git tags (separate from app CalVer
tags). Bump on any module-API change; downstream infra repos pin to the tag in
their `source = "github.com/keboola/agnes-the-ai-analyst//infra/modules/customer-instance?ref=infra-v1.X.Y"`.

After merging a module change to `main`:
```bash
git tag infra-vX.Y.Z origin/main
git push origin infra-vX.Y.Z
```

### Replacing a VM after a startup-script change

Module sets `lifecycle { ignore_changes = [metadata_startup_script] }` on
`google_compute_instance.vm` so normal `terraform apply` doesn't churn running
VMs. To propagate a startup-script update, trigger the consumer's apply workflow
manually with the VM resource address — typical workflow_dispatch input is
`recreate_targets='module.agnes.google_compute_instance.vm["<vm-name>"]'`.

## Appendix: CHANGELOG entry skeleton

Copy this when adding to `## [Unreleased]` in `CHANGELOG.md`. Drop the sections
you don't need; keep the Keep-a-Changelog order.

```markdown
### Added
- New feature description.

### Changed
- Change description. **BREAKING** prefix + migration steps if operator-facing.

### Fixed
- Bug fix description.

### Removed
- **BREAKING** removed feature — what replaces it.

### Internal
- Refactors, test additions, dependency bumps with no behavior change.
```

The daily cut PR (`.github/workflows/daily-cut.yml`) renames
`## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD` and adds a fresh empty
`## [Unreleased]` on top — never a feature PR. CI publishes the matching
`stable-YYYY.MM.N` image tag for the cut PR's merge commit (see Deploy
workflows above).

## Slack release digest (optional)

`.github/workflows/release-digest.yml` posts **one aggregated Slack message a
day** (cron 04:00 UTC) summarizing every GitHub Release created since the
previous successful digest run — grouped Added/Changed/Fixed/Removed
highlights, per-version links, and a link to the full `CHANGELOG.md`. Quiet
days post nothing; a skipped night is caught up automatically on the next run
(the window is derived from the workflow's own run history, no stored state).

Opt in by setting the **`SLACK_RELEASE_WEBHOOK`** repository secret to a Slack
Incoming Webhook URL for the target channel. Without the secret the scheduled
run is a dry-run (payload printed to the job log only). Manual test:
`gh workflow run release-digest.yml -f since=2026-01-01T00:00:00Z` — with the
`since` input you control the window explicitly. The formatter lives in
`scripts/release_digest.py` (stdlib-only; unit tests in
`tests/test_release_digest.py`).
