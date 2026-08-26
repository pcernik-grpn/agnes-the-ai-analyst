"""CI guard against the silent-rebase CHANGELOG corruption.

The failure class this catches
------------------------------
Git's 3-way merge can relocate a branch's ``## [Unreleased]`` bullets into an
already-released section — and *report zero conflicts* while doing it, because
it matches heading lines as "the same section" on both sides and interleaves
the bodies instead of conflicting. The visible damage is one of:

* a version heading duplicated (``## [X.Y.Z]`` appears twice), or
* ``## [Unreleased]`` split/duplicated, or
* released sections left out of descending order (a relocated block lands
  below an older version), or
* a group heading repeated *inside* ``## [Unreleased]`` (``### Added`` …
  ``### Internal`` … ``### Added``) — the shape the corruption takes when
  both sides only ever appended under ``[Unreleased]``, which leaves every
  ``##`` heading unique and so slips past the three checks above.

This exact corruption struck four times during the 2026-08 remediation
program. ``scripts/release_cut.py`` already refuses to *cut* on top of a
duplicated heading (:func:`assert_no_duplicate_headings`), but that only fires
on the days a cut runs; a corrupt ``main`` can sit undetected until then. This
test makes the invariant a fast, every-push CI check over the committed file.

Assertions (pure file parse, no I/O beyond reading ``CHANGELOG.md``):

1. exactly one ``## [Unreleased]`` heading;
2. no duplicate ``## [X.Y.Z]`` version headings anywhere;
3. version sections appear in strictly descending semver order after
   ``[Unreleased]``;
4. no ``### <Group>`` heading repeats inside ``## [Unreleased]``.

The parsing / dup-heading logic is imported from ``scripts.release_cut`` (the
E1 daily-cut module) rather than re-implemented, so this guard and the cut
enforce the *same* notion of a well-formed CHANGELOG.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from scripts.release_cut import (
    DuplicateHeadingError,
    _unreleased_bounds,
    assert_no_duplicate_headings,
    find_version_headings,
    parse_version,
)

CHANGELOG_PATH = Path(__file__).resolve().parents[1] / "CHANGELOG.md"


# ---------------------------------------------------------------------------
# guards — take changelog text, raise on corruption. Shared by the real-file
# tests below and the teeth tests that prove each guard actually fires.
# ---------------------------------------------------------------------------


def assert_single_unreleased(changelog_text: str) -> None:
    """Exactly one ``## [Unreleased]`` heading (0 or 2+ means a bad merge)."""
    count = sum(1 for _, version in find_version_headings(changelog_text) if version == "Unreleased")
    assert count == 1, (
        f"CHANGELOG.md must have exactly one '## [Unreleased]' heading, found {count} — "
        f"a 3-way merge that duplicated or dropped it (docs/RELEASING.md § CHANGELOG merge hazards)."
    )


def assert_versions_strictly_descending(changelog_text: str) -> None:
    """Version headings are in strictly descending semver order after Unreleased.

    A relocated ``[Unreleased]`` block landing under an older section shows up
    here as an out-of-order (or equal) pair even when no heading is duplicated.
    """
    versions = [version for _, version in find_version_headings(changelog_text) if version != "Unreleased"]
    order = [(v.major, v.minor, v.patch) for v in map(parse_version, versions)]
    for i in range(len(order) - 1):
        assert order[i] > order[i + 1], (
            f"CHANGELOG.md version headings are not in strictly descending order: "
            f"[{versions[i]}] precedes [{versions[i + 1]}] — a silent-rebase relocation "
            f"(docs/RELEASING.md § CHANGELOG merge hazards)."
        )


def assert_no_duplicate_unreleased_subsections(changelog_text: str) -> None:
    """No ``### <Group>`` heading repeats inside ``## [Unreleased]``.

    When both sides of a merge only ever appended under ``[Unreleased]``, the
    3-way merge can keep *both* copies of a group heading rather than
    conflicting — ``### Added`` … ``### Internal`` … ``### Added``. No ``##``
    heading is duplicated and the version order is untouched, so the three
    guards above all stay green; this one is what notices.

    Deliberately scoped to ``[Unreleased]``. Released sections are frozen
    history and 18 of them already violate this rule as legacy damage (the
    pre-0.61 ones so badly that a ``###`` heading sits glued onto the ``##``
    line), which no merge of *pending* bullets can reach or worsen.
    ``[Unreleased]`` is the section every PR writes to and the one a daily cut
    is about to rename into permanent history — so it is the one worth
    holding clean, and retro-repairing 15k lines of shipped history is a
    separate concern from stopping the next corruption.
    """
    start, end = _unreleased_bounds(changelog_text)
    counts: Counter[str] = Counter(
        line.strip() for line in changelog_text.splitlines()[start + 1 : end] if line.startswith("### ")
    )
    dupes = sorted(heading for heading, n in counts.items() if n > 1)
    assert not dupes, (
        f"CHANGELOG.md '## [Unreleased]' repeats group heading(s): {', '.join(dupes)} — "
        f"a 3-way merge that interleaved two branches' bullets instead of conflicting "
        f"(docs/RELEASING.md § CHANGELOG merge hazards). Fix by merging the duplicate "
        f"blocks into one, keeping every bullet."
    )


# ---------------------------------------------------------------------------
# the real committed CHANGELOG must satisfy all four
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def changelog_text() -> str:
    return CHANGELOG_PATH.read_text(encoding="utf-8")


def test_exactly_one_unreleased_heading(changelog_text: str) -> None:
    assert_single_unreleased(changelog_text)


def test_no_duplicate_version_headings(changelog_text: str) -> None:
    # reuses the same guard the daily cut runs before renaming [Unreleased]
    assert_no_duplicate_headings(changelog_text)


def test_versions_strictly_descending(changelog_text: str) -> None:
    assert_versions_strictly_descending(changelog_text)


def test_no_duplicate_unreleased_subsections(changelog_text: str) -> None:
    assert_no_duplicate_unreleased_subsections(changelog_text)


# ---------------------------------------------------------------------------
# teeth — each guard must FIRE on the specific corruption it exists to catch.
# Without these, a no-op guard would pass forever and give false green.
# ---------------------------------------------------------------------------

_GOOD = """# Changelog

## [Unreleased]

### Added
- a pending change

## [0.89.0] - 2026-08-25

### Added
- a shipped change

## [0.88.0] - 2026-08-24

### Fixed
- an older fix
"""


def test_good_fixture_passes_every_guard() -> None:
    """The synthetic well-formed fixture passes all three (proves the guards
    fire on the corruption, not indiscriminately)."""
    assert_single_unreleased(_GOOD)
    assert_no_duplicate_headings(_GOOD)
    assert_versions_strictly_descending(_GOOD)
    assert_no_duplicate_unreleased_subsections(_GOOD)


def test_duplicate_version_heading_is_caught() -> None:
    # the classic 3-way merge: a second branch's bullets land under a fresh,
    # duplicated [0.89.0] heading
    corrupt = _GOOD.replace("## [0.88.0] - 2026-08-24", "## [0.89.0] - 2026-08-25")
    with pytest.raises(DuplicateHeadingError):
        assert_no_duplicate_headings(corrupt)


def test_two_unreleased_headings_are_caught() -> None:
    # a relocated block reintroduces a second [Unreleased] heading
    corrupt = _GOOD.replace(
        "## [0.89.0] - 2026-08-25",
        "## [Unreleased]\n\n### Added\n- stray relocated bullet\n\n## [0.89.0] - 2026-08-25",
    )
    with pytest.raises(AssertionError):
        assert_single_unreleased(corrupt)


def test_out_of_order_versions_are_caught() -> None:
    # a released section left out of descending order (no heading duplicated)
    corrupt = _GOOD.replace("## [0.88.0] - 2026-08-24", "## [0.90.0] - 2026-08-26")
    with pytest.raises(AssertionError):
        assert_versions_strictly_descending(corrupt)


def test_interleaved_unreleased_groups_are_caught() -> None:
    """Both branches appended under ``[Unreleased]``; the merge kept both group
    headings instead of conflicting.

    This is the variant that motivated the fourth guard: it duplicates no
    ``##`` heading and disturbs no version order, so the first three checks
    are green on it — asserted here explicitly, so that if a future refactor
    ever made one of them cover this case, the redundancy is visible rather
    than assumed.
    """
    corrupt = _GOOD.replace(
        "- a pending change",
        "- a pending change\n\n### Internal\n\n- the other branch's bullet\n\n### Added\n\n- an interleaved bullet",
    )
    assert_single_unreleased(corrupt)
    assert_no_duplicate_headings(corrupt)
    assert_versions_strictly_descending(corrupt)

    with pytest.raises(AssertionError, match="repeats group heading"):
        assert_no_duplicate_unreleased_subsections(corrupt)


def test_duplicate_subsection_in_a_released_section_is_ignored() -> None:
    """The fourth guard is scoped to ``[Unreleased]`` — legacy duplication in
    shipped sections (18 of them carry it) must not fail the build."""
    corrupt = _GOOD.replace(
        "- a shipped change",
        "- a shipped change\n\n### Added\n\n- a legacy duplicate in released history",
    )
    assert_no_duplicate_unreleased_subsections(corrupt)
