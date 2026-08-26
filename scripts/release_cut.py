#!/usr/bin/env python3
"""Pure functions for the Agnes daily release-cut.

Why this exists
----------------
Per-PR release-cuts (the old rule: "the version bump + CHANGELOG rename ship
in the same PR that earns the version") caused a structural race — two PRs
racing to claim the same version number produce a duplicated ``## [X.Y.Z]``
heading once merged, silently, because git's merge strategy matches the
heading line as "the same" section on both sides and interleaves the bodies
instead of conflicting (see ``docs/RELEASING.md`` § CHANGELOG merge hazards).

The fix moves the cut out of feature PRs entirely: they only ever add
``## [Unreleased]`` bullets. A single **cut PR** (opened by
``.github/workflows/daily-cut.yml``, or by a human for an emergency hotfix)
does the version bump + rename, once a day, from a clean ``main``. This module
is the cut logic as pure functions over file contents, so the workflow and a
human both call the exact same code the unit tests pin — no divergent shell
implementation to drift from the tests.

Usage::

    scripts/release_cut.py --dry-run --json      # plan only, no writes
    scripts/release_cut.py                        # apply (minor bump)
    scripts/release_cut.py --bump patch            # emergency hotfix cut

Exit codes: ``0`` success (including the no-op case: an empty ``[Unreleased]``
is not an error, it just means there is nothing to cut today), ``1`` a real
problem (malformed CHANGELOG, duplicate version heading, bad version string).

Stdlib only, on purpose — no venv required to run it.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

UNRELEASED_HEADING = "## [Unreleased]"
_VERSION_HEADING_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\]")
_PYPROJECT_VERSION_RE = re.compile(r'^(version\s*=\s*")([^"]+)(")', re.MULTILINE)
_SERVER_JSON_VERSION_RE = re.compile(r'("version"\s*:\s*")([^"]+)(")')

# Keep-a-Changelog subsection order used for a freshly opened [Unreleased].
DEFAULT_SUBSECTIONS = ("Added", "Changed", "Fixed", "Removed", "Internal")


class ChangelogFormatError(ValueError):
    """``CHANGELOG.md`` does not have the shape a cut can operate on."""


class DuplicateHeadingError(ChangelogFormatError):
    """Two version sections share one heading — the 3-way-merge failure class.

    Detected instead of silently cutting on top of it: interleaving a new cut
    into an already-malformed file would make the mess worse, not fix it. A
    human has to resolve the collision by hand first (see ``docs/RELEASING.md``
    § CHANGELOG merge hazards for the fix pattern).
    """


# ---------------------------------------------------------------------------
# version parsing / bumping
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Version:
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:  # noqa: D105
        return f"{self.major}.{self.minor}.{self.patch}"


def parse_version(version: str) -> _Version:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version.strip())
    if not match:
        raise ValueError(f"not a semver X.Y.Z version: {version!r}")
    major, minor, patch = (int(part) for part in match.groups())
    return _Version(major, minor, patch)


def bump_minor(version: str) -> str:
    """The daily-batch bump: middle digit up, patch digit resets to 0."""
    v = parse_version(version)
    return str(_Version(v.major, v.minor + 1, 0))


def bump_patch(version: str) -> str:
    """The emergency-hotfix bump: last digit up only."""
    v = parse_version(version)
    return str(_Version(v.major, v.minor, v.patch + 1))


def bump_major(version: str) -> str:
    """The milestone bump: first digit up, both others reset to 0."""
    v = parse_version(version)
    return str(_Version(v.major + 1, 0, 0))


BUMPERS = {"minor": bump_minor, "patch": bump_patch, "major": bump_major}


# ---------------------------------------------------------------------------
# pyproject.toml / server.json version fields
# ---------------------------------------------------------------------------


def read_pyproject_version(pyproject_text: str) -> str:
    match = _PYPROJECT_VERSION_RE.search(pyproject_text)
    if not match:
        raise ChangelogFormatError('pyproject.toml has no version = "X.Y.Z" line')
    return match.group(2)


def bump_pyproject_version(pyproject_text: str, new_version: str) -> str:
    match = _PYPROJECT_VERSION_RE.search(pyproject_text)
    if not match:
        raise ChangelogFormatError('pyproject.toml has no version = "X.Y.Z" line')
    return (
        pyproject_text[: match.start()] + match.group(1) + new_version + match.group(3) + pyproject_text[match.end() :]
    )


def bump_server_json_version(server_json_text: str, new_version: str) -> str:
    match = _SERVER_JSON_VERSION_RE.search(server_json_text)
    if not match:
        raise ChangelogFormatError('server.json has no "version": "X.Y.Z" field')
    return (
        server_json_text[: match.start()]
        + match.group(1)
        + new_version
        + match.group(3)
        + server_json_text[match.end() :]
    )


# ---------------------------------------------------------------------------
# CHANGELOG.md — headings, bullets, the cut itself
# ---------------------------------------------------------------------------


def _lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def find_version_headings(changelog_text: str) -> list[tuple[int, str]]:
    """``[(line_index, version_or_'Unreleased'), ...]`` in document order.

    ``line_index`` indexes into ``_lines(changelog_text)`` (i.e. keeps
    newlines), so callers can slice the original text back out verbatim.
    """
    result: list[tuple[int, str]] = []
    for i, line in enumerate(_lines(changelog_text)):
        stripped = line.strip()
        if stripped == UNRELEASED_HEADING:
            result.append((i, "Unreleased"))
            continue
        match = _VERSION_HEADING_RE.match(stripped)
        if match:
            result.append((i, match.group(1)))
    return result


def assert_no_duplicate_headings(changelog_text: str) -> None:
    """Raise :class:`DuplicateHeadingError` if any heading repeats.

    This is the guard for the known 3-way-merge dup-heading failure class:
    two branches independently cut the same version number, and the merge
    interleaves both bodies under one heading instead of conflicting.
    """
    seen: set[str] = set()
    dupes: list[str] = []
    for _, version in find_version_headings(changelog_text):
        if version in seen and version not in dupes:
            dupes.append(version)
        seen.add(version)
    if dupes:
        names = ", ".join(f"[{d}]" for d in dupes)
        raise DuplicateHeadingError(
            f"CHANGELOG.md has duplicate version heading(s): {names} — this is "
            f"the known 3-way-merge collision (docs/RELEASING.md § CHANGELOG "
            f"merge hazards). Fix by hand before cutting."
        )


def _unreleased_bounds(changelog_text: str) -> tuple[int, int]:
    """``(heading_index, next_heading_index)`` into ``_lines(changelog_text)``.

    ``next_heading_index`` is ``len(lines)`` when ``[Unreleased]`` is the last
    heading in the file (never true in practice, but handled).
    """
    headings = find_version_headings(changelog_text)
    unreleased_at = next((i for i, v in headings if v == "Unreleased"), None)
    if unreleased_at is None:
        raise ChangelogFormatError("CHANGELOG.md has no '## [Unreleased]' heading")
    later = [i for i, _ in headings if i > unreleased_at]
    next_at = later[0] if later else len(_lines(changelog_text))
    return unreleased_at, next_at


def _bullets_in(lines: list[str]) -> list[str]:
    """Top-level bullet lines (collapsed whitespace, ``---`` rules skipped).

    A bullet marker must sit at column 0 — a wrapped bullet's continuation
    lines are hanging-indented by convention in this CHANGELOG, and prose
    inside a bullet routinely opens a line with an *emphasis* marker (e.g.
    ``  *was* previously published...``); stripping leading whitespace
    before checking the marker would misread that continuation line as its
    own bullet.
    """
    bullets: list[str] = []
    for raw in lines:
        content = raw.rstrip("\n")
        if not content or content[0] not in "-*":
            continue
        stripped = content.strip()
        text = stripped[1:].strip()
        if not text or set(stripped) <= {"-", "*", " "}:
            continue  # a `---` rule, not a bullet
        bullets.append("- " + " ".join(text.split()))
    return bullets


def unreleased_bullets(changelog_text: str) -> list[str]:
    """Bullets currently sitting under ``## [Unreleased]``."""
    lines = _lines(changelog_text)
    start, end = _unreleased_bounds(changelog_text)
    return _bullets_in(lines[start + 1 : end])


def has_unreleased_content(changelog_text: str) -> bool:
    return bool(unreleased_bullets(changelog_text))


def _fresh_unreleased_block() -> str:
    parts = [UNRELEASED_HEADING, ""]
    for name in DEFAULT_SUBSECTIONS:
        parts.append(f"### {name}")
        parts.append("")
    return "\n".join(parts) + "\n"


def cut_changelog(changelog_text: str, new_version: str, *, date: str) -> str:
    """Rename ``## [Unreleased]`` to ``## [new_version] - date`` and open a
    fresh, empty ``## [Unreleased]`` above it.

    The old section's body (bullets, subsection headers, exact bytes) moves
    to the new version heading unchanged. Everything before the old
    ``[Unreleased]`` heading and everything from the following heading
    onward — i.e. every previously released section — is preserved
    byte-for-byte: this function only ever touches the single line range it
    owns.
    """
    assert_no_duplicate_headings(changelog_text)
    lines = _lines(changelog_text)
    start, end = _unreleased_bounds(changelog_text)

    newline = "\n" if lines[start].endswith("\n") else ""
    new_heading_line = f"## [{new_version}] - {date}{newline}"

    before = "".join(lines[:start])
    body = "".join(lines[start + 1 : end])
    after = "".join(lines[end:])

    return before + _fresh_unreleased_block() + new_heading_line + body + after


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReleaseCutPlan:
    """The result of computing a cut, before anything is written to disk."""

    noop: bool
    previous_version: str
    version: str | None
    bullets: tuple[str, ...]
    date: str
    changelog_text: str | None = None
    pyproject_text: str | None = None
    server_json_text: str | None = None

    def as_json(self) -> dict:
        return {
            "noop": self.noop,
            "previous_version": self.previous_version,
            "version": self.version,
            "bullets": list(self.bullets),
            "date": self.date,
        }


def plan_release_cut(
    *,
    changelog_text: str,
    pyproject_text: str,
    server_json_text: str | None = None,
    bump: str = "minor",
    date: str,
) -> ReleaseCutPlan:
    """Compute the cut without writing anything.

    Idempotent by construction: an empty ``[Unreleased]`` (no bullets) always
    returns ``noop=True`` regardless of ``bump`` — there is nothing to ship,
    so re-running the daily workflow on a quiet day is a safe no-op.
    """
    if bump not in BUMPERS:
        raise ValueError(f"unknown bump kind: {bump!r} (expected one of {sorted(BUMPERS)})")

    assert_no_duplicate_headings(changelog_text)
    previous_version = read_pyproject_version(pyproject_text)
    bullets = tuple(unreleased_bullets(changelog_text))

    if not bullets:
        return ReleaseCutPlan(noop=True, previous_version=previous_version, version=None, bullets=(), date=date)

    new_version = BUMPERS[bump](previous_version)
    new_changelog = cut_changelog(changelog_text, new_version, date=date)
    new_pyproject = bump_pyproject_version(pyproject_text, new_version)
    new_server_json = bump_server_json_version(server_json_text, new_version) if server_json_text is not None else None

    return ReleaseCutPlan(
        noop=False,
        previous_version=previous_version,
        version=new_version,
        bullets=bullets,
        date=date,
        changelog_text=new_changelog,
        pyproject_text=new_pyproject,
        server_json_text=new_server_json,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _today_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--server-json", type=Path, default=Path("server.json"))
    parser.add_argument(
        "--no-server-json",
        action="store_true",
        help="skip server.json even if present (e.g. a checkout that dropped it)",
    )
    parser.add_argument("--bump", choices=sorted(BUMPERS), default="minor")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD, default: today (UTC)")
    parser.add_argument("--dry-run", action="store_true", help="compute and print the plan, write nothing")
    parser.add_argument("--json", action="store_true", help="machine-readable plan on stdout")
    args = parser.parse_args(argv)

    date = args.date or _today_utc()

    try:
        changelog_text = args.changelog.read_text(encoding="utf-8")
        pyproject_text = args.pyproject.read_text(encoding="utf-8")
        server_json_text = None
        if not args.no_server_json and args.server_json.is_file():
            server_json_text = args.server_json.read_text(encoding="utf-8")
        plan = plan_release_cut(
            changelog_text=changelog_text,
            pyproject_text=pyproject_text,
            server_json_text=server_json_text,
            bump=args.bump,
            date=date,
        )
    except (ChangelogFormatError, ValueError, OSError) as exc:
        print(f"release_cut: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(plan.as_json(), indent=2))
    elif plan.noop:
        print(f"release_cut: [Unreleased] is empty at {plan.previous_version} — nothing to cut today.")
    else:
        print(f"release_cut: {plan.previous_version} -> {plan.version} ({len(plan.bullets)} bullet(s)):")
        for bullet in plan.bullets:
            print(f"  {bullet}")

    if plan.noop or args.dry_run:
        return 0

    assert plan.changelog_text is not None
    assert plan.pyproject_text is not None
    args.changelog.write_text(plan.changelog_text, encoding="utf-8")
    args.pyproject.write_text(plan.pyproject_text, encoding="utf-8")
    written = [str(args.changelog), str(args.pyproject)]
    if plan.server_json_text is not None:
        args.server_json.write_text(plan.server_json_text, encoding="utf-8")
        written.append(str(args.server_json))

    if not args.json:
        print(f"release_cut: wrote {', '.join(written)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
