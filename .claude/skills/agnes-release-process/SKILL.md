---
name: agnes-release-process
description: Rules for opening a PR, the CHANGELOG bullet, the daily release-cut PR, and the post-merge tag + GitHub Release. Use before opening a PR, before merge, when handling a release-cut, and when picking a version bump.
---

# Agnes release process

Source of truth for the rules in `CLAUDE.md § Release process` and
`docs/RELEASING.md`. This skill is invoked by the main agent during planning,
by `agnes-reviewer-rules` during review, and by `agnes-releaser` during a
manual/emergency release-cut. When the rules below conflict with the master
documents above, the master documents win — update this skill.

## When this skill applies

- Opening a PR
- Reviewing a PR (confirming it does NOT carry a release-cut)
- Handling `.github/workflows/daily-cut.yml`'s PR, or cutting an emergency
  release by hand
- Post-merge tagging + GitHub Release

## CHANGELOG discipline

Every PR that changes **user-visible behavior** MUST add a bullet under
`## [Unreleased]` in `CHANGELOG.md`, grouped under Added / Changed / Fixed /
Removed / Internal. Breaking changes are prefixed `**BREAKING**`.

Doc-only PRs (`docs/**`, README) typically do not need a bullet. Apply
judgment based on the diff — if the docs change describes new behavior that
should have shipped with a code change, the *code* PR carries the bullet.

The CHANGELOG entry is part of the PR that introduces the change — never a
follow-up PR.

## Release-cut is a dedicated cut PR — feature PRs never cut

**A feature/fix PR never bumps `pyproject.toml`, never touches `server.json`'s
version field, and never renames `## [Unreleased]`.** It only ever adds a
bullet. This is deliberate: the old rule (the release-cut ships in whichever
PR happens to land last with content in `[Unreleased]`) raced two PRs against
the same version number and produced a duplicated `## [X.Y.Z]` CHANGELOG
heading on merge — see `docs/RELEASING.md` § CHANGELOG merge hazards for the
failure signature.

The cut is centralized: `.github/workflows/daily-cut.yml` runs once a day
(and on manual dispatch), computes the next version from `pyproject.toml`,
rewrites `CHANGELOG.md` + `pyproject.toml` + `server.json`, and opens a PR
labeled `release-cut`. A human reviews and merges it — the workflow never
merges or tags anything itself. The cut arithmetic is pure functions in
`scripts/release_cut.py`; use it directly for a manual/emergency cut instead
of hand-editing:

```bash
python3 scripts/release_cut.py --dry-run --json   # inspect the plan first
python3 scripts/release_cut.py --bump patch       # write the cut, e.g. for a hotfix
```

An empty `[Unreleased]` is a no-op (nothing written, exit 0) — safe to run
speculatively.

### Reviewing a PR for this rule

`agnes-reviewer-rules` checks: does this PR touch `pyproject.toml`'s
`version`, `server.json`'s `version`, or rename `## [Unreleased]`? If yes AND
the PR is not itself labeled `release-cut`, that is Missing (a feature PR
must not carry a cut) — ask the author to drop those hunks; the bullet alone
is enough.

## Version bump decision (for the cut PR only)

- **Minor** (`X.Y+1.0`): the daily-batch default. `daily-cut.yml`'s scheduled
  run always uses this.
- **Patch** (`X.Y.Z+1`): emergency hotfix only, dispatched by hand
  (`gh workflow run daily-cut.yml -f bump=patch`, or
  `scripts/release_cut.py --bump patch` directly). Still ships the entire
  current `[Unreleased]` content.
- **Major** (`X+1.0.0`): milestone, human decision only — dispatch with
  `bump=major`, never automatic.

## The train-driver role

Merge the ready PR queue completely first, then merge the cut PR — in that
order. Once a cut PR is open, avoid merging further feature PRs until it
lands (they'd grow `main`'s `[Unreleased]` underneath a cut branch that
already snapshotted an older state of that section). If the cut PR shows a
merge conflict against a newer `main`, close it and re-dispatch
`daily-cut.yml` rather than resolving the conflict by hand.

## Post-merge sequence

After the cut PR is merged to `main`, `release.yml`'s ordinary push-to-main
build publishes `:stable` / `:stable-YYYY.MM.N` automatically — no extra step
for the image. The semver tag + GitHub Release are a deliberate separate
step (no tag-on-merge automation), and the cut PR's own body carries the
exact command:

```
gh workflow run tag-release.yml -f tag=vX.Y.Z
```

`target` defaults to `main`'s current HEAD, correct as long as nothing else
merged in between. `tag-release.yml` validates server-side (tag shape,
target on main, tag == pyproject version at the target, CHANGELOG section
present), creates the tag ref via the API, and publishes the Release with
the CHANGELOG section as its body; re-dispatching is idempotent/repairing.

Never tag or release before merge.

## Post-merge auto-rollback

On every `main` push, GitHub Actions `release.yml` builds the `:stable`
image and a `smoke-test` job pulls it and runs a docker-compose stack.
If the smoke test fails:

- `rollback-on-smoke-fail` calls `rollback.yml`, which re-points `:stable`
  to the previous known-good build.
- A tracking issue labeled `bug` is opened with the failing image, the
  commit SHA, the deprecated tag, and the rollback target.

Success signal after merge: `smoke-test` green AND `rollback-on-smoke-fail`
skipped. If rollback fires, the merge shipped a broken image to GHCR —
investigate the tracking issue before any further push.

Manual rollback, forced target, and weekly tag-pruning operator commands
live in `docs/RELEASING.md`.

## Tests before push

Run `.venv/bin/pytest tests/ connectors/ --lane fast --tb=short -n auto -q`
(2:57) before every push, after `--lane impacted` for what the diff touches.
CI runs the full suite on the push — running it locally too is what makes a
merge cycle take hours. Failures in code you touched: fix before pushing.
Failures unrelated: confirm they reproduce on a clean branch, note in the PR
body, do not block.

A **release-cut PR** is the exception: it is the artifact the tag is built from,
so run the full suite on it once before merge.
