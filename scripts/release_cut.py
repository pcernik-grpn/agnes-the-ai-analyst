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

The fix moves the cut out of feature PRs entirely: they only ever add a
changelog entry — since #2295 a ``changelog.d/`` fragment, see below — and
never the version bump or rename. A single **cut PR** (opened by
``.github/workflows/daily-cut.yml``, or by a human for an emergency hotfix)
does the version bump + rename, once a day, from a clean ``main``. This module
is the cut logic as pure functions over file contents, so the workflow and a
human both call the exact same code the unit tests pin — no divergent shell
implementation to drift from the tests.

Every cut also stamps a sha256 of the RELEASED region of ``CHANGELOG.md``
(everything from the first ``## [X.Y.Z]`` heading onward) into
``pyproject.toml``'s ``[tool.agnes] released_changelog_sha256`` — a second
guard, orthogonal to the duplicate-heading one above, for a merge that writes
a bullet INTO an already-released block instead of racing on the heading
itself (#1918). ``tests/test_changelog_integrity.py`` checks the live
CHANGELOG against that stored digest on every push.

Since #2295 a feature PR does not even write to ``CHANGELOG.md``: it adds one
small **fragment** file under ``changelog.d/`` (``### <Group>`` headings +
bullets, see ``changelog.d/README.md``). Two PRs never touch the same file, so
the one merge conflict that fired on every ``main`` sync is gone. The cut
folds every fragment into ``[Unreleased]`` (:func:`merge_fragments`) right
before the rename and deletes the fragment files, so the released
``CHANGELOG.md`` looks exactly as it always did.

Usage::

    scripts/release_cut.py --dry-run --json      # plan only, no writes
    scripts/release_cut.py                        # apply (minor bump)
    scripts/release_cut.py --bump patch            # emergency hotfix cut
    scripts/release_cut.py --rebaseline            # re-stamp the checksum after a deliberate, reviewed edit to released history — cuts nothing

Exit codes: ``0`` success (including the no-op case: an empty ``[Unreleased]``
is not an error, it just means there is nothing to cut today), ``1`` a real
problem (malformed CHANGELOG, duplicate version heading, bad version string).

Stdlib only, on purpose — no venv required to run it.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

UNRELEASED_HEADING = "## [Unreleased]"
_VERSION_HEADING_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\]")
_PYPROJECT_VERSION_RE = re.compile(r'^(version\s*=\s*")([^"]+)(")', re.MULTILINE)
_SERVER_JSON_VERSION_RE = re.compile(r'("version"\s*:\s*")([^"]+)(")')
_TOOL_AGNES_HEADING_RE = re.compile(r"^\[tool\.agnes\][ \t]*\n", re.MULTILINE)
_RELEASED_CHECKSUM_RE = re.compile(r'^(released_changelog_sha256\s*=\s*")([0-9a-f]{64})(")', re.MULTILINE)

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
# CHANGELOG fragments (changelog.d/)
#
# Every PR used to append its bullet to the one ``## [Unreleased]`` section,
# so any two PRs merging in the same window conflicted on CHANGELOG.md — 34
# of the 37 merge conflicts across two long autonomous runs were that file
# alone (#2295). A fragment is one small file per PR under ``changelog.d/``;
# nothing in it is shared with any other PR, so two PRs cannot conflict on
# it. The cut folds every fragment into ``[Unreleased]`` immediately before
# the rename and deletes the files.
# ---------------------------------------------------------------------------

FRAGMENTS_DIR = "changelog.d"
FRAGMENT_README = "README.md"
_GROUP_HEADING_RE = re.compile(r"^### +(\S.*?)\s*$")
_CONFLICT_MARKER_RE = re.compile(r"^(<{7}|={7}|>{7})( |$)")


class FragmentFormatError(ChangelogFormatError):
    """A ``changelog.d/`` fragment does not have the shape the cut can fold in."""


def _fragment_fix_hint(name: str) -> str:
    return (
        f"Fix {FRAGMENTS_DIR}/{name}: one or more '### <Group>' headings, each followed by "
        f"'- ' (or '* ') bullets at column 0, Group one of {', '.join(DEFAULT_SUBSECTIONS)} "
        f"(see {FRAGMENTS_DIR}/{FRAGMENT_README})."
    )


def parse_fragment(name: str, text: str) -> dict[str, list[str]]:
    """``{group: [raw body lines]}`` for one fragment, validated.

    Rules, each an error whose message carries the fix:

    * every ``### <Group>`` is one of :data:`DEFAULT_SUBSECTIONS`, once;
    * no other heading level — a fragment is a piece of the ``[Unreleased]``
      body, not a document of its own;
    * nothing but blank lines before the first group heading;
    * every group has at least one bullet (``-``/``*`` at column 0);
    * no unresolved conflict markers.

    Body lines come back raw (continuation lines and their indentation
    intact, surrounding blank lines trimmed) because they are spliced into
    ``CHANGELOG.md`` verbatim — the cut must never re-wrap prose.
    """
    where = f"{FRAGMENTS_DIR}/{name}"
    groups: dict[str, list[str]] = {}
    current: str | None = None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if _CONFLICT_MARKER_RE.match(raw):
            raise FragmentFormatError(f"{where}:{lineno}: unresolved merge-conflict marker. {_fragment_fix_hint(name)}")
        heading = _GROUP_HEADING_RE.match(raw)
        if heading:
            group = heading.group(1)
            if group not in DEFAULT_SUBSECTIONS:
                raise FragmentFormatError(f"{where}:{lineno}: unknown group '### {group}'. {_fragment_fix_hint(name)}")
            if group in groups:
                raise FragmentFormatError(
                    f"{where}:{lineno}: '### {group}' appears twice — keep one heading per group. "
                    f"{_fragment_fix_hint(name)}"
                )
            current = group
            groups[group] = []
            continue
        if raw.startswith("#"):
            raise FragmentFormatError(
                f"{where}:{lineno}: only '### <Group>' headings belong in a fragment. {_fragment_fix_hint(name)}"
            )
        if current is None:
            if raw.strip():
                raise FragmentFormatError(
                    f"{where}:{lineno}: text before the first '### <Group>' heading. {_fragment_fix_hint(name)}"
                )
            continue
        groups[current].append(raw)

    if not groups:
        raise FragmentFormatError(f"{where}: no '### <Group>' heading. {_fragment_fix_hint(name)}")

    for group, body in groups.items():
        while body and not body[0].strip():
            body.pop(0)
        while body and not body[-1].strip():
            body.pop()
        if not any(line.startswith(("- ", "* ")) for line in body):
            raise FragmentFormatError(f"{where}: '### {group}' has no bullet. {_fragment_fix_hint(name)}")
    return groups


def read_fragments(directory: Path) -> dict[str, str]:
    """``{filename: text}`` for every ``*.md`` in ``directory`` except the README.

    Sorted by name, so the assembled order never depends on filesystem order.
    An absent directory is an empty mapping — a checkout that predates
    fragments still cuts from ``[Unreleased]`` alone.
    """
    if not directory.is_dir():
        return {}
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(directory.glob("*.md"))
        if path.is_file() and path.name != FRAGMENT_README
    }


def merge_fragments(changelog_text: str, fragments: Mapping[str, str]) -> str:
    """Fold every fragment's bullets into ``## [Unreleased]``, in filename order.

    Each fragment group's body is appended verbatim to the end of the matching
    ``### <Group>`` of ``[Unreleased]``; a group heading the section lacks is
    created in Keep-a-Changelog order. Everything outside the ``[Unreleased]``
    body — preamble and every released section — is preserved byte-for-byte,
    the same contract :func:`cut_changelog` keeps. No fragments: the text
    comes back unchanged.
    """
    if not fragments:
        return changelog_text
    parsed = [parse_fragment(name, text) for name, text in sorted(fragments.items())]

    lines = _lines(changelog_text)
    start, end = _unreleased_bounds(changelog_text)
    body = [line.rstrip("\n") for line in lines[start + 1 : end]]

    preamble: list[str] = []
    sections: list[tuple[str, str, list[str]]] = []  # (group, heading line, body lines)
    for line in body:
        heading = _GROUP_HEADING_RE.match(line)
        if heading:
            sections.append((heading.group(1), line, []))
        elif sections:
            sections[-1][2].append(line)
        else:
            preamble.append(line)

    def _section_for(group: str) -> list[str]:
        for name, _, section_body in sections:
            if name == group:
                return section_body
        rank = DEFAULT_SUBSECTIONS.index(group)
        insert_at = 0
        for index, (name, _, _) in enumerate(sections):
            if name in DEFAULT_SUBSECTIONS and DEFAULT_SUBSECTIONS.index(name) < rank:
                insert_at = index + 1
        fresh: list[str] = []
        sections.insert(insert_at, (group, f"### {group}", fresh))
        return fresh

    for groups in parsed:
        for group, fragment_body in groups.items():
            section_body = _section_for(group)
            while section_body and not section_body[-1].strip():
                section_body.pop()
            section_body.extend(fragment_body)

    rebuilt: list[str] = list(preamble)
    for _, heading_line, section_body in sections:
        rebuilt.append(heading_line)
        rebuilt.extend(section_body)
        if not section_body or section_body[-1].strip():
            rebuilt.append("")
    new_body = "".join(line + "\n" for line in rebuilt)

    return "".join(lines[: start + 1]) + new_body + "".join(lines[end:])


# ---------------------------------------------------------------------------
# released-region immutability checksum (CHANGELOG.md <-> pyproject.toml)
#
# #1918: every guard above is scoped to [Unreleased] on purpose (see their
# docstrings) — none of them notice a bullet a bad merge wrote INTO an
# already-released block, since a well-placed bullet under an existing
# heading disturbs no ordering and creates no duplicate. A released block is
# the permanent record of what shipped under a tag, so this is a second,
# orthogonal guard: not "is [Unreleased] well-formed" but "did released
# history change since it was cut".
# ---------------------------------------------------------------------------


def released_region(changelog_text: str) -> str:
    """The RELEASED slice of ``CHANGELOG.md``: from the first version heading
    that is not ``[Unreleased]`` to end of file.

    Reuses :func:`find_version_headings`, so it copes with the legacy
    em-dash headings (``## [0.11.4] — 2026-04-27Some title...``) the same
    way every other guard in this module does. Returns ``""`` when no
    released heading exists yet (a brand-new CHANGELOG with only
    ``[Unreleased]``) — there is nothing released yet to protect.
    """
    lines = _lines(changelog_text)
    for index, version in find_version_headings(changelog_text):
        if version != "Unreleased":
            return "".join(lines[index:])
    return ""


def released_region_sha256(changelog_text: str) -> str:
    """sha256 hex digest of :func:`released_region` — the value stamped into
    ``pyproject.toml``'s ``[tool.agnes] released_changelog_sha256`` at every
    cut and checked against on every push (``tests/test_changelog_integrity.py``).

    One hash over the *whole* released region, not one per version block:
    :func:`cut_changelog`'s own docstring already guarantees everything from
    the heading after the one it renames onward is preserved byte-for-byte,
    so the region as a whole only ever legitimately changes by growing a
    freshly-cut block at its top — an event ``plan_release_cut`` recomputes
    and re-stores this same digest for, in the same write. A single hash can
    therefore tell WHETHER released history changed, just not WHICH of its
    many blocks — naming the specific block would need a digest-per-block
    manifest kept in sync forever, deliberately not built.
    """
    return hashlib.sha256(released_region(changelog_text).encode("utf-8")).hexdigest()


def read_released_checksum(pyproject_text: str) -> str | None:
    """The ``[tool.agnes] released_changelog_sha256`` value, or ``None`` when
    unset (a checkout that predates this guard, or a hand-edited
    ``pyproject.toml`` that dropped the key)."""
    match = _RELEASED_CHECKSUM_RE.search(pyproject_text)
    return match.group(2) if match else None


def write_released_checksum(pyproject_text: str, digest: str) -> str:
    """Set (or create) ``[tool.agnes] released_changelog_sha256 = "<digest>"``.

    Mirrors :func:`bump_pyproject_version`: targeted line surgery rather than
    a tomllib round-trip, so nothing else in the file gets reformatted. When
    the key already exists only its value changes in place; when
    ``[tool.agnes]`` exists without the key, the key is appended right under
    the heading; otherwise a fresh table is appended at end of file.
    """
    match = _RELEASED_CHECKSUM_RE.search(pyproject_text)
    if match:
        return (
            pyproject_text[: match.start()] + match.group(1) + digest + match.group(3) + pyproject_text[match.end() :]
        )

    # json.dumps, not a hand-written f'"{digest}"' literal: a TOML basic
    # string shares JSON's quoting/escaping for this plain-ASCII-hex value,
    # and building the quotes this way (rather than splicing literal `"`
    # characters around an interpolated value) is what keeps this line out
    # of tests/test_security_audit_20260805.py's hand-quoted-identifier
    # ratchet — the shape it bans is exactly `"{value}"`, regardless of
    # whether the quoted thing is a SQL identifier or, as here, a TOML value.
    line = f"released_changelog_sha256 = {json.dumps(digest)}\n"

    heading = _TOOL_AGNES_HEADING_RE.search(pyproject_text)
    if heading:
        insert_at = heading.end()
        return pyproject_text[:insert_at] + line + pyproject_text[insert_at:]

    prefix = pyproject_text if pyproject_text.endswith("\n") else pyproject_text + "\n"
    return prefix + "\n[tool.agnes]\n" + line


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
    released_changelog_sha256: str | None = None
    fragment_paths: tuple[str, ...] = ()

    def as_json(self) -> dict:
        return {
            "noop": self.noop,
            "previous_version": self.previous_version,
            "version": self.version,
            "bullets": list(self.bullets),
            "date": self.date,
            "released_changelog_sha256": self.released_changelog_sha256,
            "fragments": list(self.fragment_paths),
        }


def plan_release_cut(
    *,
    changelog_text: str,
    pyproject_text: str,
    server_json_text: str | None = None,
    bump: str = "minor",
    date: str,
    fragments: Mapping[str, str] | None = None,
) -> ReleaseCutPlan:
    """Compute the cut without writing anything.

    ``fragments`` (``{filename: text}``, see :func:`read_fragments`) are
    folded into ``[Unreleased]`` first; the plan lists them in
    ``fragment_paths`` so the caller deletes exactly the files it shipped.

    Idempotent by construction: nothing pending (no ``[Unreleased]`` bullet,
    no fragment) always returns ``noop=True`` regardless of ``bump`` — there
    is nothing to ship, so re-running the daily workflow on a quiet day is a
    safe no-op.
    """
    if bump not in BUMPERS:
        raise ValueError(f"unknown bump kind: {bump!r} (expected one of {sorted(BUMPERS)})")

    assert_no_duplicate_headings(changelog_text)
    previous_version = read_pyproject_version(pyproject_text)
    merged = merge_fragments(changelog_text, fragments or {})
    bullets = tuple(unreleased_bullets(merged))

    if not bullets:
        return ReleaseCutPlan(noop=True, previous_version=previous_version, version=None, bullets=(), date=date)

    new_version = BUMPERS[bump](previous_version)
    new_changelog = cut_changelog(merged, new_version, date=date)
    # The just-cut block is now part of the released region, so the digest
    # covering it is computed AFTER the rename — this is what keeps the
    # stored checksum and tests/test_changelog_integrity.py's live guard in
    # agreement the moment this plan is written to disk (#1918).
    digest = released_region_sha256(new_changelog)
    new_pyproject = write_released_checksum(bump_pyproject_version(pyproject_text, new_version), digest)
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
        released_changelog_sha256=digest,
        fragment_paths=tuple(sorted(fragments or {})),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _today_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def _rebaseline(args: argparse.Namespace) -> int:
    """``--rebaseline``: accept the current ``CHANGELOG.md`` released region
    as ground truth and rewrite only the stored checksum. Cuts nothing.

    The documented escape hatch for a deliberate, reviewed edit to
    already-released history (docs/RELEASING.md § CHANGELOG merge hazards)
    — the live guard in ``tests/test_changelog_integrity.py`` would
    otherwise flag that edit forever.
    """
    try:
        changelog_text = args.changelog.read_text(encoding="utf-8")
        pyproject_text = args.pyproject.read_text(encoding="utf-8")
        assert_no_duplicate_headings(changelog_text)
    except (ChangelogFormatError, OSError) as exc:
        print(f"release_cut: {exc}", file=sys.stderr)
        return 1

    digest = released_region_sha256(changelog_text)
    new_pyproject = write_released_checksum(pyproject_text, digest)

    if args.json:
        print(json.dumps({"rebaseline": True, "released_changelog_sha256": digest}, indent=2))
    else:
        print(f"release_cut: --rebaseline released_changelog_sha256 = {digest}")

    if args.dry_run:
        return 0

    args.pyproject.write_text(new_pyproject, encoding="utf-8")
    if not args.json:
        print(f"release_cut: wrote {args.pyproject}")
    return 0


def _apply_plan(plan: ReleaseCutPlan, args: argparse.Namespace) -> list[str]:
    """Write the cut to disk all-or-nothing; returns the paths written.

    Every write and every fragment deletion is recorded with its undo. If any
    step fails, the steps already applied are reverted in reverse order before
    the error propagates, so a retry sees the pre-cut checkout instead of
    bumping the version a second time on top of a half-applied cut (and
    republishing fragments the first attempt had already folded in).
    """
    assert plan.changelog_text is not None
    assert plan.pyproject_text is not None
    undo: list[tuple[Path, str | None]] = []  # (path, original text; None = did not exist)

    def _write(path: Path, text: str) -> None:
        undo.append((path, path.read_text(encoding="utf-8") if path.is_file() else None))
        path.write_text(text, encoding="utf-8")

    def _remove(path: Path) -> None:
        undo.append((path, path.read_text(encoding="utf-8")))
        path.unlink()

    try:
        _write(args.changelog, plan.changelog_text)
        _write(args.pyproject, plan.pyproject_text)
        written = [str(args.changelog), str(args.pyproject)]
        if plan.server_json_text is not None:
            _write(args.server_json, plan.server_json_text)
            written.append(str(args.server_json))
        for name in plan.fragment_paths:
            _remove(args.fragments_dir / name)
    except OSError:
        for path, original in reversed(undo):
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(original, encoding="utf-8")
        raise
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--server-json", type=Path, default=Path("server.json"))
    parser.add_argument(
        "--fragments-dir",
        type=Path,
        default=None,
        help=(
            "per-PR CHANGELOG fragments folded into [Unreleased] by the cut and deleted "
            "(default: the changelog.d/ next to --changelog — never the current directory's, so a cut "
            "pointed at another CHANGELOG cannot consume this checkout's fragments)"
        ),
    )
    parser.add_argument(
        "--no-server-json",
        action="store_true",
        help="skip server.json even if present (e.g. a checkout that dropped it)",
    )
    parser.add_argument("--bump", choices=sorted(BUMPERS), default="minor")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD, default: today (UTC)")
    parser.add_argument("--dry-run", action="store_true", help="compute and print the plan, write nothing")
    parser.add_argument("--json", action="store_true", help="machine-readable plan on stdout")
    parser.add_argument(
        "--rebaseline",
        action="store_true",
        help=(
            "recompute [tool.agnes] released_changelog_sha256 in pyproject.toml from the current "
            "CHANGELOG.md and rewrite only that value — cuts nothing. Use after a deliberate, "
            "reviewed edit to already-released history."
        ),
    )
    args = parser.parse_args(argv)
    if args.fragments_dir is None:
        args.fragments_dir = args.changelog.parent / FRAGMENTS_DIR

    if args.rebaseline:
        return _rebaseline(args)

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
            fragments=read_fragments(args.fragments_dir),
        )
    except (ChangelogFormatError, ValueError, OSError) as exc:
        print(f"release_cut: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(plan.as_json(), indent=2))
    elif plan.noop:
        print(
            f"release_cut: nothing pending at {plan.previous_version} — [Unreleased] is empty and "
            f"{args.fragments_dir}/ has no fragments; nothing to cut today."
        )
    else:
        print(
            f"release_cut: {plan.previous_version} -> {plan.version} "
            f"({len(plan.bullets)} bullet(s), {len(plan.fragment_paths)} fragment(s)):"
        )
        for bullet in plan.bullets:
            print(f"  {bullet}")

    if plan.noop or args.dry_run:
        return 0

    try:
        written = _apply_plan(plan, args)
    except OSError as exc:
        print(f"release_cut: {exc} — the partial cut was rolled back, nothing changed", file=sys.stderr)
        return 1

    if not args.json:
        print(f"release_cut: wrote {', '.join(written)}")
        if plan.fragment_paths:
            print(f"release_cut: removed {len(plan.fragment_paths)} fragment(s) from {args.fragments_dir}/")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
