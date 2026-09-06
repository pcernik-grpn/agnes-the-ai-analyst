# CHANGELOG fragments

One file per PR. The daily release cut (`scripts/release_cut.py`, run by
`.github/workflows/daily-cut.yml`) folds every fragment in this directory into
the `## [Unreleased]` section of `CHANGELOG.md`, renames that section to the
new version, and deletes the fragment files. Nothing here is shared between
PRs, so two PRs never conflict on the changelog again (#2295: 34 of the 37
merge conflicts across two long autonomous runs were `CHANGELOG.md` alone).

## Writing one

Create `changelog.d/<slug>.md`. Any unique name works; the branch slug or
`<issue>-<short-topic>` is the convention. The content is a piece of the
`[Unreleased]` body:

```markdown
### Fixed
- **Lead with what the user sees.** Then the mechanism, the flag, the doc to
  read. Continuation lines are hanging-indented like this one.

### Internal
- A refactor or dependency bump with no behavior change.
```

Rules, each enforced by `tests/test_changelog_integrity.py` with the fix
spelled out in the failure message:

- Only `### Added` / `### Changed` / `### Fixed` / `### Removed` /
  `### Internal` headings, each at most once per fragment; no other heading
  level.
- Every group has at least one `- ` bullet at column 0. Bullets are spliced
  into `CHANGELOG.md` verbatim, so write them in the finished style.
- Breaking changes start with `**BREAKING**`.
- Never write a pending bullet under `## [Unreleased]` in `CHANGELOG.md`
  itself. That section is assembled at the cut; the guard rejects inline
  bullets and tells you which fragment to move them into.
- Never bump `pyproject.toml`'s version, edit `server.json`, or touch a
  released `## [X.Y.Z]` block in a feature PR. That is the cut PR's job.

`python3 scripts/release_cut.py --dry-run` rehearses the fold locally and
prints exactly the bullets that would ship.
