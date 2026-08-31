#!/usr/bin/env python3
"""Print the test paths a diff plausibly touches, for the targeted local run.

Why this exists
---------------
The pre-push instruction used to be "run the full suite" — ~24 000 tests. On a
change that touches one endpoint that is minutes of waiting for a signal the
diff cannot possibly move, repeated once per review round. CI runs the full
suite anyway, in parallel, on every push.

This script answers the narrower question: *which test files could this diff
have broken?* It is a *selector*, not a proof — it is deliberately generous
(everything that names the changed module, by dotted path or by file path, plus
the filename-convention sibling) and it refuses to answer at all when the diff
is broad enough that the honest answer is "the whole suite".

    python3 scripts/dev/impacted_tests.py            # paths, one per line
    python3 scripts/dev/impacted_tests.py --json     # {"paths": [...], "reason": ...}
    python3 scripts/dev/impacted_tests.py --base HEAD  # uncommitted work only

Reasons go to **stderr**, paths to **stdout**, and the exit status says which
answer you got: ``0`` = here are the impacted files, ``1`` = this diff is too
broad to target (run ``pytest --lane fast`` and let CI cover the rest), ``2`` =
the selector itself could not run. It never widens to "the whole suite" behind
the caller's back.

``pytest --lane impacted`` (see the root ``conftest.py``) is the normal way to
use this — same selection, no shell quoting, and it degrades to the fast lane
on its own when the answer is "too broad".
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# Sentinel returned instead of a path list when the diff is too broad to
# target usefully. The caller degrades to the fast lane rather than to the full
# suite: "I cannot tell you which tests matter" is an argument for the cheap
# gate plus CI, never for 24 000 tests on a laptop.
TOO_BROAD: list[str] = []

# Source trees whose files map to tests. Anything outside these still gets the
# literal-path grep below (a doc or a template can have a test asserting on it),
# it just gets no dotted-module lookup.
SOURCE_ROOTS = ("src", "app", "cli", "connectors", "services", "scripts")

# Files that sit underneath so much of the suite that a name-based grep would
# UNDER-select: nearly every test depends on the DuckDB schema, the repository
# factory or the app factory without ever naming them. A diff touching one of
# these gets the full suite, and says so.
MERGE_MAGNETS = frozenset(
    {
        "conftest.py",
        "tests/conftest.py",
        "pytest.ini",
        "pyproject.toml",
        "uv.lock",
        "src/db.py",
        "src/db_pg.py",
        "src/repositories/__init__.py",
        "app/main.py",
    }
)

# A token matching more than this many test files is not identifying anything —
# it is a word the suite happens to use ("CHANGELOG.md", a two-letter module
# name). Dropping it keeps one generic path in a diff from dragging in a third
# of the suite. Tuned by backtesting against merged PRs; see the module tests.
MAX_FILES_PER_TOKEN = 40

# Past this share of the suite's total RECORDED time (from `.test_durations`),
# the selection has stopped being a shortcut. Measured in time rather than file
# count because 40 database-building files cost more than 200 parser ones.
MAX_SELECTED_TIME_SHARE = 0.25

_TEST_FILE = re.compile(r"(^|/)(test_[^/]+|[^/]+_test)\.py$")


def run_git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def changed_paths(repo: Path, base: str) -> list[str]:
    """Every path this branch changed relative to ``base``, committed or not.

    Uncommitted work counts: mid-task the file you just edited is exactly the
    one whose tests you want, and it is not committed yet.
    """
    if base == "HEAD":
        merge_base = "HEAD"
    else:
        try:
            merge_base = run_git("merge-base", "HEAD", base, cwd=repo).strip()
        except RuntimeError:
            # No such ref (a fresh clone with no `origin`, a detached checkout).
            # Fall back to the working tree alone rather than failing the gate.
            merge_base = "HEAD"

    paths: set[str] = set()
    for args in (
        ("diff", "--no-renames", "--name-only", merge_base),
        ("diff", "--no-renames", "--name-only", "--cached"),
        ("ls-files", "--others", "--exclude-standard"),
    ):
        paths.update(line for line in run_git(*args, cwd=repo).splitlines() if line.strip())
    return sorted(paths)


def is_test_path(path: str) -> bool:
    return bool(_TEST_FILE.search(path))


def search_tokens(path: str) -> list[str]:
    """Literals a test would have to contain to plausibly exercise ``path``."""
    tokens = [path]
    parts = path.split("/")
    if path.endswith(".py") and parts[0] in SOURCE_ROOTS:
        module = path[: -len(".py")].replace("/", ".")
        tokens.append(module)
        # `from src.foo import bar` never contains the dotted leaf, so also
        # search for the package plus the symbol-ish leaf name.
        if len(parts) > 1:
            tokens.append(f"{'.'.join(parts[:-1])} import {parts[-1][:-3]}")
    else:
        # Templates, docs, YAML: a test that asserts on one names the file.
        tokens.append(parts[-1])
    return tokens


def grep_one_token(repo: Path, token: str) -> set[str]:
    """Test files containing ``token`` as a fixed string."""
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "grep",
            "--files-with-matches",
            # `--untracked`: a test file you just created is not in the index
            # yet, and it is exactly the one that must not be missed.
            "--untracked",
            "--fixed-strings",
            "-e",
            token,
            "--",
            "tests/",
            "connectors/",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    # `git grep` exits 1 on "no match", which is not an error here.
    if result.returncode not in (0, 1):
        raise RuntimeError(f"git grep failed: {result.stderr.strip()}")
    return {line for line in result.stdout.splitlines() if is_test_path(line)}


def grep_tests(repo: Path, tokens: list[str]) -> set[str]:
    """Test files matching any DISCRIMINATING token.

    A token is searched on its own so an over-broad one can be dropped rather
    than poisoning the union: ``CHANGELOG.md`` appears in a hundred test files
    and identifies none of them.
    """
    found: set[str] = set()
    for token in tokens:
        hits = grep_one_token(repo, token)
        if len(hits) <= MAX_FILES_PER_TOKEN:
            found |= hits
    return found


def convention_siblings(repo: Path, path: str) -> set[str]:
    """``src/foo/bar.py`` → any ``tests/**/test_bar*.py`` that exists.

    Catches the common case where a test exercises a module through a public
    entry point and never names the module itself.
    """
    stem = Path(path).stem
    if not stem or stem in {"__init__", "main"}:
        return set()
    found = set()
    for directory in ("tests", "connectors"):
        base = repo / directory
        if not base.is_dir():
            continue
        found.update(
            str(candidate.relative_to(repo)) for candidate in base.rglob(f"test_{stem}*.py") if candidate.is_file()
        )
    return found


def select(repo: Path, base: str) -> tuple[list[str], str]:
    """Return ``(paths, reason)`` for the diff of ``base``..working tree."""
    return select_for_paths(repo, changed_paths(repo, base))


def select_for_paths(repo: Path, changed: list[str]) -> tuple[list[str], str]:
    """Return ``(paths, reason)``. ``paths`` is what to hand pytest."""
    if not changed:
        return [], "no changed files against the base ref — nothing to select"

    magnets = sorted(set(changed) & MERGE_MAGNETS)
    if magnets:
        return (
            TOO_BROAD,
            f"{magnets[0]} is a merge magnet — most tests depend on it without "
            "naming it, so no name-based selection would be honest",
        )

    selected: set[str] = set()
    for path in changed:
        if is_test_path(path):
            if (repo / path).exists():
                selected.add(path)
            continue
        selected |= grep_tests(repo, search_tokens(path))
        selected |= convention_siblings(repo, path)

    if not selected:
        return (
            [],
            f"no test file names any of the {len(changed)} changed paths — "
            "either the change is untested or its tests reach it indirectly; "
            "run --lane fast and let CI cover the rest",
        )

    share = recorded_time_share(repo, selected)
    if share > MAX_SELECTED_TIME_SHARE:
        return (
            TOO_BROAD,
            f"{len(selected)} test files matched, ~{share:.0%} of the suite's recorded "
            f"runtime (over the {MAX_SELECTED_TIME_SHARE:.0%} cap) — a subset this wide "
            "is not a shortcut",
        )

    return (
        sorted(selected),
        f"{len(selected)} test files (~{share:.0%} of the suite's runtime) matched {len(changed)} changed paths",
    )


def recorded_time_share(repo: Path, test_files: set[str]) -> float:
    """Fraction of the suite's recorded runtime that ``test_files`` accounts for.

    Returns 0.0 when `.test_durations` is missing — an unknown cost must not
    trip the cap and silently widen a targeted run to the whole suite.
    """
    try:
        durations = json.loads((repo / ".test_durations").read_text())
    except (OSError, ValueError):
        return 0.0
    total = 0.0
    selected_total = 0.0
    for nodeid, seconds in durations.items():
        if not isinstance(seconds, (int, float)):
            continue
        total += seconds
        if nodeid.split("::")[0] in test_files:
            selected_total += seconds
    return selected_total / total if total else 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base",
        default="origin/main",
        help="Base ref to diff against (default: origin/main). Use HEAD for uncommitted work only.",
    )
    parser.add_argument("--json", action="store_true", help="Emit {paths, reason} as JSON on stdout.")
    parser.add_argument(
        "--repo",
        default=".",
        help="Repository root (default: the current directory).",
    )
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    try:
        paths, reason = select(repo, args.base)
    except RuntimeError as exc:
        print(f"impacted_tests: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"paths": paths, "reason": reason}, indent=2))
        return 0

    print(f"impacted_tests: {reason}", file=sys.stderr)
    for path in paths:
        print(path)
    # Exit 1 on TOO_BROAD so a caller can tell "nothing to target" from "no
    # impacted tests" without parsing stdout. Never a path list widened to the
    # whole suite behind the caller's back.
    return 0 if paths else 1


if __name__ == "__main__":
    raise SystemExit(main())
