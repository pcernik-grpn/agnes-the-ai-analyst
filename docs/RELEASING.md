# Releasing & deploying

The full release process for Agnes. CLAUDE.md carries the short version; this
doc is the operational reference. Read it linearly the first few times — once
internalized, the order matters less, but the **non-obvious gotchas never go
away**.

## Changelog discipline — non-negotiable

**Every PR that adds, removes, or changes user-visible behavior MUST add a
CHANGELOG fragment (`changelog.d/<slug>.md`) in the same PR.** No exceptions, no follow-ups, no "I'll do it
after merge". User-visible = anything an operator, end-user, or downstream
integrator can observe: CLI flags / output / exit codes, REST endpoints /
payloads / status codes, web UI, `instance.yaml` schema, env vars,
`extract.duckdb` contract, Docker / compose / Caddyfile knobs, default
behaviors, breaking changes, security fixes.

**How:**
- Create `changelog.d/<slug>.md` (any unique name; the branch slug works) with
  `### <Group>` headings and bullets — the format is `changelog.d/README.md`.
  Never write the bullet under `## [Unreleased]` in `CHANGELOG.md`: that
  section is assembled by the daily cut, and `tests/test_changelog_integrity.py`
  rejects an inline bullet. One file per PR is what makes two PRs unable to
  conflict on the changelog (#2295: 34 of 37 conflicts across two long runs).
- The cut folds every fragment into `[Unreleased]` in filename order, renames
  the section, and deletes the fragment files — so the released `CHANGELOG.md`
  looks exactly as before.
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

**Feature PRs never bump `pyproject.toml`, rename `## [Unreleased]`, edit
`CHANGELOG.md`, or touch `server.json`'s version field. They only ever add a
`changelog.d/` fragment** (see the Changelog discipline section above).
The version bump + CHANGELOG rename is cut once a day by
`.github/workflows/daily-cut.yml`, which opens a PR labeled `release-cut` for
a human to review and merge. It never merges or tags anything itself. After
the bump it runs `uv lock`, so `uv.lock`'s own version line follows
`pyproject.toml` — the image installs from the lock behind CI's blocking
`lock-check`, and a cut PR that skipped this would fail its own checks.

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
  scheduled (cron) run always use this. Every fragment accumulated under
  `changelog.d/` since the last cut ships together.
- **Patch (`X.Y.Z+1`) — emergency hotfix only.** Dispatch `daily-cut.yml`
  manually with `bump: patch` (`gh workflow run daily-cut.yml -f bump=patch`)
  to ship immediately, outside the daily cadence. It still ships *every*
  pending fragment — if unrelated in-flight work has
  already merged, prefer waiting for the next scheduled minor cut instead.
- **Major (`X+1.0.0`) — milestone, human decision.** Dispatch with `bump:
  major` only when the team has actually decided this cut is a milestone
  boundary; never automatic.

### Landing PRs through the merge queue

Since 2026-09-07 `main` has a GitHub **merge queue** (ruleset *Merge queue on
main*: merge-commit method, `ALLGREEN` grouping, up to five entries per group,
60 min check timeout; the `internal` team can bypass it — see the last bullet
below for when that is legitimate)
and its "require branch up to date" rule is off. This is the automated form of the merge train that used to land
most of `main` as `Train N: #…`; the hand-driven train is retired.

- **Queue a ready PR** with `gh pr merge <N> --merge --auto`
  or "Merge when ready" in the UI (GitHub deletes the head branch after the queue merges it (repo setting *Automatically delete head branches*).) The queue creates a temporary branch with
  `main` + the queued PRs, `ci.yml` runs on that `merge_group` event, and the
  required checks (`test`, `docker-build`) must pass on the merged result
  before it lands. A group in which one entry fails is re-formed without it.
- **Do not update a PR's branch because `main` moved.** BEHIND is the normal
  state between queue runs; the queue tests the combination.
- **One approving review gates entry to the queue**, evaluated by the queue
  itself — ruleset bypass does not apply inside it. For an organization
  member's PR a clean Devin verdict supplies that approval
  (`.github/workflows/devin-clean-approves.yml`, added in #2339: approves on "No Issues
  Found", dismisses its approval when a later verdict lists issues), so the
  gate is CI green + Devin clean. The approval persists across later pushes,
  as a human's does under this ruleset — Devin re-reviews only some pushes,
  so pinning approval to one commit would strand most PRs. An outside collaborator's PR needs a human approval; so does the cut
  PR (its commits are `github-actions[bot]`'s own, which the bridge cannot
  self-approve). A blocked PR names that approver.
- **The cut PR rides the same queue.** Queue it after the feature PRs you
  want in that version have landed. A fragment merged after the cut PR opened
  is a new file under `changelog.d/` and does not collide with the cut branch;
  it rides the next day's cut. A cut PR that *does* conflict with `main` (one
  cut before fragments existed, or a hand-edited `CHANGELOG.md`) is closed and
  `daily-cut.yml` re-dispatched — never hand-resolved, since a hand-resolved
  cut is exactly the collision class the cut PR exists to prevent.
  `daily-cut.yml` will not open a second cut PR while one is open (it checks
  for the `release-cut` label first).
- **Bypassing the queue is the exception, and you say why.** Members of the
  `internal` team can merge directly (`gh pr merge <N> --merge --admin`, or
  the "bypass rules" checkbox), which skips the queue and the review rule;
  classic branch protection still requires `test` and `docker-build` green,
  so a bypass merge is "not re-tested against current `main`", never
  "untested". Legitimate reasons: a hotfix, the cut PR (its bot commits can
  never get the bridge's approval), or a PR Devin never re-reviewed after its
  last push and so has no approval — Devin re-reviews only some heads. Put the
  reason in the merge commit or a PR comment. Everything else goes through the
  queue with `gh pr merge <N> --merge --auto`.
- **Changing the queue's behaviour** (merge method, group size, timeout) is a
  ruleset edit (`gh api repos/<owner>/<repo>/rulesets/<id>`), not a per-PR
  flag; `--merge` on the `gh` command line only has to be *a* method so `gh`
  runs non-interactively.

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
git add -A changelog.d   # the cut deleted the shipped fragments — stage the deletions
git commit -m "release: <version>"
git push -u origin HEAD
gh pr create --label release-cut --title "release: <version>" --body "..."
```

`--dry-run --json` prints the computed plan (next version, the bullets it
would ship) without writing anything — use it to sanity-check before
committing. Nothing pending (no fragment, empty `[Unreleased]`) is a no-op
(exit 0, nothing written), so running it speculatively is always safe.

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

# 3. Add a CHANGELOG fragment: changelog.d/<slug>.md with
#    `### Added|Changed|Fixed|Removed|Internal` headings + bullets
#    (format: changelog.d/README.md). Mark BREAKING with **BREAKING** prefix.
#    Do NOT touch CHANGELOG.md, pyproject.toml or server.json — the cut PR
#    folds the fragments in and owns the version.

# 4. Commit the change(s).

# 5. Run the full pytest suite locally:
#    `pytest tests/ -p no:xdist -q` (or `-n auto` if xdist works).
#    Pre-existing fails (e.g. test_readers_in_pre_init_dir under
#    subprocess timeout) are OK to ignore; verify by reverting your
#    diff and reproducing on bare main.

# 6. Push branch + open PR + queue it (the merge queue lands it once the
#    required checks pass on main + this PR; the queue's own merge method
#    applies, see § Landing PRs through the merge queue):
#    git push -u origin HEAD
#    gh pr create --repo keboola/agnes-the-ai-analyst \
#      --head <branch> --title "<...>" --body "<...>"
#    gh pr merge <N> --repo keboola/agnes-the-ai-analyst \
#      --merge --auto
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

- **Approval to enter the queue.** The ruleset requires one approving review,
  evaluated by the queue itself, so nobody's bypass helps and GitHub still
  forbids self-approval. For an organization member's PR the approval comes
  from Devin's clean verdict (`.github/workflows/devin-clean-approves.yml`,
  added in #2339): fix what Devin flagged, push, wait for the re-review. The
  approval persists across later pushes and falls only to a later Devin
  review that lists issues. An outside
  collaborator's PR and the cut PR need a person other than the author to
  approve (`gh pr review <N> --approve`).
- **Other people's PRs you're taking over**: dismiss any prior
  CHANGES_REQUESTED reviews (yours or someone else's) before the queue will
  take the PR; after pushing your fixes, wait for Devin's verdict (member PR)
  or get the human approval.
- **Devin Review**: not a required status check, but no longer advisory for a
  member's PR — its verdict IS the approval. Read its findings; a verdict that
  lists issues keeps the PR out of the queue until the next clean one.

### CI quirks you WILL hit

- **`gh pr checks` glosses CANCELLED as `fail`.** When you force-push (rebase,
  amend), GitHub auto-cancels the in-flight `Release` workflow run on the older
  SHA. Those cancelled jobs show up as "fail" in the PR's check summary and tab
  forever, even after newer runs succeed. **Look at the conclusion column, not
  just the count.** Rule of thumb: if the same check name appears with both
  `pass` and `fail` rows, the `fail` row is from an older auto-cancelled SHA.
  Verify with `gh api repos/keboola/agnes-the-ai-analyst/commits/<sha>/check-runs`
  — the raw API distinguishes `cancelled` from `failure` truthfully.
- **(Historical, pre-2026-09-07) branch protection's "strict" mode cached a
  cancelled `test` as blocking** even after newer `test` runs succeeded;
  the fix was to re-run the cancelled `Release` run (`gh run rerun <run-id>`).
  The strict (up-to-date) rule is off now and the merge queue owns the
  "tested against current `main`" guarantee, so this block can no longer
  occur. Kept because PRs #273, #281, #285, #286 in the history reference it.
- **Required checks** (per branch protection): `test` + `docker-build` only.
  Other workflows (`cli-wheel-clean-install`, `build-and-push`,
  `Release`-pipeline) are advisory — green/red doesn't gate merge. Devin Review
  is not a required check either, but its clean verdict is what supplies the
  approving review an organization member's PR needs to enter the queue (see
  § Landing PRs through the merge queue).
- **`enforce_admins: true`** in branch protection means `--admin` flag on
  `gh pr merge` does NOT bypass. Don't try; just fix the underlying block.
- **A cut PR's CI needs one click before it can merge.** See § A cut PR
  arrives with its CI unapproved below.
- **`lint-workflows.yml` is advisory.** Triggered on changes to
  `.github/workflows/**` or `scripts/ops/**.sh`. Runs `actionlint` on
  workflow YAMLs + `shellcheck --severity=warning` on freestanding ops
  scripts. The `actionlint` step has `continue-on-error: true` initially
  (pre-existing inventory has info-level findings); flip to fail-fast
  once the repo is actionlint-clean. The `shellcheck` step IS blocking at
  warning+ severity — info/style findings ride through, real bugs break
  CI.

### A cut PR arrives with its CI unapproved

`daily-cut.yml` opens the cut PR as `github-actions[bot]`, and GitHub queues
the `pull_request` workflow run for a bot-opened PR in **`action_required`**
— created, but not started. Until someone clicks **Approve and run
workflows** on it, `test` and `docker-build` never report, so `gh pr merge`
answers:

    405  Repository rule violations found
         2 of 2 required status checks are expected

That is the whole thing: one click, then merge normally. Nothing is broken
and no special privilege is involved — the run exists and is waiting.

**If you are not a human, you cannot give that click.** The REST equivalent,
`POST /repos/{owner}/{repo}/actions/runs/{run_id}/approve`, answers

    403  Resource not accessible by integration

to an integration token — the `actions: write` permission an app can be
granted does not cover approving a queued-for-approval run. There is no
token scope to add and no retry that helps. So a non-human release driver
should not open a cut through `daily-cut.yml` at all and then get stuck one
click short of merging; take the § Emergency path when Actions dispatch
isn't available above instead. Cutting by hand with `scripts/release_cut.py`
and opening the PR under your own identity produces the *same* diff, and its
`pull_request` run starts immediately — the queue-for-approval rule keys on
who opened the PR, not on what the branch contains. 0.97.0 shipped this way.

**Until 2026-09-07 the same 405 had a second, unrelated cause: a branch that
was behind.** Branch protection's "require branches to be up to date" rule
evaluated the required checks against the CURRENT base, so a PR whose `test`
and `docker-build` were green — but whose head predated the latest `main` —
was refused with that identical message, and the fix was to update the branch.
That rule is off now and the merge queue tests the merged result itself, so a
PR reading `mergeable_state: behind` is not refused: queue it. If you still
see the 405 on a behind PR, the cause is the unapproved-run one above (check
`blocked` and the never-reported checks), never the staleness. Worth stating
because the message names neither cause, so the one that comes to mind is
whichever you debugged last — and this one no longer exists.

**Do not reach for `gh workflow run ci.yml` instead.** It looks like the
obvious workaround and it is not one. A `workflow_dispatch` run does put
green check-runs on the PR's head SHA — 18 of them on the 0.97.0 cut,
`test` and `docker-build` among them — and the merge is refused anyway with
that same message. Tested on two separate cut PRs (#2144, #2159). The
required contexts want the run from the `pull_request` event; the dispatched
one does not stand in for it. The dispatch is still useful for *checking* a
cut's content before approving, but it does not open the gate.

Two things that make this easy to misdiagnose:

- `mergeable_state` reads **`blocked`** in this state. Everywhere else in
  this repo that means "review required", and here it does not — nothing
  about approving the PR will clear it.
- The check tab looks *empty* rather than red, so it reads as "CI never ran"
  rather than "CI is waiting for you".

For the record of what a healthy cut looks like: on 0.96.0's cut the
`pull_request` run was created at `06:15:52`, the same minute the bot pushed
the branch, and shows `run_attempt: 2` — the attempt bump is the approve/
re-run click, not a second push.

### Recovery when something derails

- **Force-pushed and lost auto-merge?** GitHub *usually* preserves auto-merge
  across force-pushes for the same PR; if it cleared, just re-run
  `gh pr merge <N> --merge --auto` to re-queue it.
- **A cut PR went stale (merge conflict against a newer `main`)?** Close the
  stale cut PR and re-dispatch `daily-cut.yml` rather than resolving the
  conflict by hand (see § Landing PRs through the merge queue above).
  Closing the PR leaves its `release-cut/vX.Y.Z` branch behind on the
  remote; there is nothing to clean up first — the branch is workflow-owned,
  and the re-dispatch force-pushes over it when it computes the same version.
- **Wrong version number tagged?** `git tag -d vX.Y.Z && git push --delete
  origin vX.Y.Z` then re-tag against the right SHA. Update the GitHub Release if
  you already created it.

### CHANGELOG merge hazards

**Since #2295 a feature PR does not touch `CHANGELOG.md` at all** — its entry
is a `changelog.d/` fragment, folded in by the cut. A `main` merge into a
feature branch therefore no longer has a `CHANGELOG.md` side of yours to
relocate, and neither failure mode below can start from a feature PR. The
section stays for the cut PR itself and for branches that predate fragments.

**The dedicated cut PR (above) eliminated failure mode 2 below, not failure
mode 1.** Feature PRs never rename `[Unreleased]` or bump the version, so two
branches can no longer claim the same version number — that is the collision
class, and it is gone. Failure mode 1 is a different animal and is *not* rare:
it needs only a merge, and on 2026-09-01 it happened four times in one day,
once in a ~1160-line merge, each caught by hand. That is what the automated
backstop under mode 1 exists for (#1918).

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

**Automated backstop for failure mode 1.** `tests/test_changelog_integrity.py`
checks a sha256 of the whole RELEASED region (everything from the first
`## [X.Y.Z]` heading to EOF) against a digest `scripts/release_cut.py` stamps
into `pyproject.toml`'s `[tool.agnes] released_changelog_sha256` at every cut.
A bullet landing in an already-released block — exactly the damage above —
changes that region's bytes and fails CI on the next push, even though it
disturbs no heading and creates no duplicate (the file's other five guards,
all scoped to `[Unreleased]`, stay green on it). Find the drift with `git diff
origin/main -- CHANGELOG.md`, move the bullet back up into `[Unreleased]`, and
push again — the stored checksum only ever needs recomputing by a real cut. If
an edit to released history is instead deliberate and reviewed (a rare,
explicit exception), rebaseline it with `python scripts/release_cut.py
--rebaseline`, which recomputes and rewrites only the stored digest and cuts
nothing.

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

Runs on **every** push to **every** branch — except a push whose diff only
touches `docs/**`, root-level `*.md`, or `LICENSE` (a `decide` job replicates
the old `paths-ignore` semantics via `scripts/ci/docs_only_change.py`), which
renders `build-and-push` as a neutral SKIPPED check and produces no image at
all. A docs-only push to `main` therefore does not publish a new `:stable`.
A zero-diff branch-create (a fresh branch off `main` with no extra commits)
still builds, so a dev VM pinned to a `:dev-<slug>` floating tag always gets
an image.
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

Copy this into `changelog.d/<slug>.md`. Drop the sections you don't need; keep
the Keep-a-Changelog order.

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

The daily cut PR (`.github/workflows/daily-cut.yml`) folds every
`changelog.d/` fragment into `## [Unreleased]`, renames it to
`## [X.Y.Z] - YYYY-MM-DD`, deletes the fragments, and adds a fresh empty
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
