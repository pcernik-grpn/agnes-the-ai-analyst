### Changed
- **`uv.lock` is now what ships.** The Docker image and every CI job install
  the exact pinned set from the lock (`scripts/ci/install-from-lock.sh`, an
  `uv export --frozen` → `uv pip install` pair) instead of re-resolving
  `pyproject.toml`'s ranges at build time — so what CI tested is what the image
  runs, and a Dependabot bump of a transitive dependency now actually reaches
  the image. A new blocking CI job, `lock-check` (`uv lock --check`), fails a
  PR that changes `pyproject.toml` without re-locking, and the daily release
  cut re-locks after its version bump. The lock itself is regenerated here; it
  had been behind `pyproject.toml` since the 0.95.0 cut (six releases and two
  added dependencies), which is also why any `uv run` in a worktree rewrote it.

### Internal
- The post-edit and typecheck hooks' `uv run` fallbacks pass `--frozen`, so
  local tooling never rewrites `uv.lock` again — the source of the dirty lock
  on nearly every PR's worktree.
