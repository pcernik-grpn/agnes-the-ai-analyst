---
name: agnes-release-process
description: Rules for opening a PR, the CHANGELOG bullet, the release-cut commit, and the post-merge tag + GitHub Release. Use before opening a PR, before merge, when handling a release-cut, and when picking a version bump.
---

# Agnes release process

Source of truth for the rules in `CLAUDE.md § Release process` and
`docs/RELEASING.md`. This skill is invoked by the main agent during planning,
by `agnes-reviewer-rules` during review, and by `agnes-releaser` during the
release-cut. When the rules below conflict with the master documents above,
the master documents win — update this skill.

## When this skill applies

- Opening a PR
- Reviewing a PR (release-cut implications)
- Cutting a release (version bump, CHANGELOG rename)
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

## Release-cut belongs in the PR

If a PR lands the only `[Unreleased]` content since the last release, the
release-cut is the **last commit on that PR**:

1. Bump `pyproject.toml` (`version = "X.Y.Z"`).
2. Rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`.
3. Add a new empty `## [Unreleased]` above it.

The release-cut is never a standalone follow-up PR.

## Version bump decision

- **Patch** (X.Y.Z+1): default for bug fixes, internal refactors, doc tweaks,
  small features that do not change documented behavior.
- **Minor** (X.Y+1.0): new user-visible features, new APIs, schema migrations
  that are backwards-compatible. **Ask the user before picking minor.**
- **Major** (X+1.0.0): breaking changes, removed APIs, incompatible schema
  changes. Requires explicit user confirmation.

## Post-merge sequence

After the PR with the release-cut is merged to `main`:

1. `git tag vX.Y.Z <release-cut-sha>`
2. `git push origin vX.Y.Z`
3. `gh release create vX.Y.Z --title "vX.Y.Z" --notes "<CHANGELOG body for [X.Y.Z]>"`

From an environment that cannot push tags (remote/CI-managed sessions
commonly allow branch pushes but refuse `refs/tags/*`), dispatch the
server-side equivalent instead — it validates (tag shape, target on main,
tag == pyproject version at the target, CHANGELOG section present),
creates the tag ref via the API, and publishes the Release with the
CHANGELOG section as its body; re-dispatching is idempotent/repairing:

```
gh workflow run tag-release.yml -f tag=vX.Y.Z -f target=<release-cut-sha>  # the commit that bumped pyproject (squash/merge commit; for an unsquashed cut, the cut commit itself)
```

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

Run `.venv/bin/pytest tests/ connectors/ --tb=short -n auto -q` before every push.
Failures in code you touched: fix before pushing. Failures unrelated:
confirm they reproduce on a clean branch, note in the PR body, do not block.
