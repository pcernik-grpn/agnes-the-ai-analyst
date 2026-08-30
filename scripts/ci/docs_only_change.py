#!/usr/bin/env python3
"""Decide whether a push's diff needs a Release image build.

Why this exists
----------------
``.github/workflows/release.yml`` used to gate ``on.push`` with a
``paths-ignore`` list (``docs/**``, ``*.md``, ``LICENSE``) *and* a `create:`
trigger to cover the branch-creation edge case where `paths-ignore` diffs the
new ref against the default branch and a zero-diff branch matches every
ignore pattern. GitHub fires both a `create` and a `push` event for a brand
new branch's first commit, so that pairing produced two runs for the same
commit; `concurrency: cancel-in-progress` killed one of them, leaving a
cancelled run's job check-contexts stuck red on the PR.

This script replaces `paths-ignore` with an in-workflow decision so the
`create:` trigger (and the duplicate run it caused) can be removed outright.
It replicates the *exact* semantics `paths-ignore` had, because GitHub's glob
matching for path filters is depth-sensitive in a way that is easy to get
subtly wrong:

- ``docs/**`` matches any file at any depth under a top-level ``docs/``
  directory.
- ``*.md`` (no ``/`` in the pattern) matches only files at the *repository
  root* — GitHub's own "Filter pattern cheat sheet" example for a pattern
  without a path separator (e.g. ``*.jsx?``) is explicit that it matches
  root-level files only; the any-depth form would have to be written
  ``**/*.md``. A nested markdown file living outside ``docs/`` (for example
  under ``.claude/skills/**``) is therefore NOT docs-only under the original
  rule, and this script must not treat it as such.
- ``LICENSE`` (a literal, no wildcard) matches only the root-level file named
  exactly ``LICENSE``.

Usage::

    python3 scripts/ci/docs_only_change.py <before_sha> <head_sha>

Prints two lines to stdout:

    build=true|false
    # reason: <one-line explanation>

The caller (the `decide` job in release.yml) takes line 1 verbatim into
``$GITHUB_OUTPUT`` and logs line 2 for humans.

Stdlib only, on purpose — no venv required to run it in CI.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ZERO_SHA = "0" * 40

# Mirrors the removed `paths-ignore:` list exactly (see module docstring for
# the depth semantics of each entry).
_IGNORED_ROOT_FILES = {"LICENSE"}
_IGNORED_DIR_PREFIX = "docs/"
_IGNORED_ROOT_SUFFIX = ".md"


def _is_ignored_path(path: str) -> bool:
    """Would the removed ``paths-ignore`` list have ignored ``path``?"""
    if path in _IGNORED_ROOT_FILES:
        return True
    if path.startswith(_IGNORED_DIR_PREFIX):
        return True
    # `*.md` in paths-ignore matches root-level files only: no `/` in path.
    return "/" not in path and path.endswith(_IGNORED_ROOT_SUFFIX)


def _run_git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _sha_is_known(repo_dir: Path, sha: str) -> bool:
    result = _run_git(repo_dir, "cat-file", "-e", f"{sha}^{{commit}}")
    return result.returncode == 0


def decide(repo_dir: Path, before_sha: str, head_sha: str) -> tuple[bool, str]:
    """Return ``(build, reason)`` for the given push's before/head SHAs.

    ``build`` is ``True`` whenever the workflow should run the image build —
    the safe default whenever the diff can't be established with confidence.
    """
    if not before_sha or before_sha == ZERO_SHA:
        return True, "before_sha is empty or all zeros (branch-create / zero-diff push)"

    if not _sha_is_known(repo_dir, before_sha):
        return True, f"before_sha {before_sha} is unknown to local git (force-push edge)"

    diff = _run_git(repo_dir, "diff", "--name-only", before_sha, head_sha)
    if diff.returncode != 0:
        return True, f"git diff {before_sha}..{head_sha} failed: {diff.stderr.strip()}"

    changed_paths = [line for line in diff.stdout.splitlines() if line.strip()]
    if not changed_paths:
        return True, "empty diff (e.g. a merge commit with no content changes)"

    non_ignored = [path for path in changed_paths if not _is_ignored_path(path)]
    if non_ignored:
        return True, f"non-ignored path changed: {non_ignored[0]}"

    return False, "every changed path matched paths-ignore (docs/**, root-level *.md, LICENSE)"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: docs_only_change.py <before_sha> <head_sha>", file=sys.stderr)
        return 2

    before_sha, head_sha = argv
    build, reason = decide(Path.cwd(), before_sha, head_sha)
    print(f"build={'true' if build else 'false'}")
    print(f"# reason: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
