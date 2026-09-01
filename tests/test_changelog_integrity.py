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
  ``##`` heading unique and so slips past the three checks above, or
* the *same bullet listed twice* under ``## [Unreleased]`` with every
  heading unique — what is left when someone "resolves" the previous shape
  by concatenating the two duplicate groups' bodies, which merges the
  headings but keeps both copies of the bullets they shared; or
* — #1918 — a bullet written straight INTO an already-released block. None of
  the shapes above are disturbed: no heading repeats, version order holds, no
  group or bullet is duplicated, because the bullet lands under an existing
  heading exactly once. This struck twice in one day during a merge train. A
  released block is the permanent record of what shipped under a tag; a
  GitHub Release generated from a mutated block describes a version that
  never existed.

This exact corruption struck four times during the 2026-08 remediation
program. ``scripts/release_cut.py`` already refuses to *cut* on top of a
duplicated heading (:func:`assert_no_duplicate_headings`), but that only fires
on the days a cut runs; a corrupt ``main`` can sit undetected until then. This
test makes the invariant a fast, every-push CI check over the committed file.

Assertions (pure file parse, no I/O beyond reading ``CHANGELOG.md`` and
``pyproject.toml``):

1. exactly one ``## [Unreleased]`` heading;
2. no duplicate ``## [X.Y.Z]`` version headings anywhere;
3. version sections appear in strictly descending semver order after
   ``[Unreleased]``;
4. no ``### <Group>`` heading repeats inside ``## [Unreleased]``;
5. no *bullet* repeats inside ``## [Unreleased]``;
6. the RELEASED region (everything from the first ``## [X.Y.Z]`` heading to
   EOF) matches the sha256 ``scripts/release_cut.py`` stamps into
   ``pyproject.toml``'s ``[tool.agnes] released_changelog_sha256`` at every
   cut (:func:`assert_released_region_unchanged`) — the #1918 fix.

Guards 1-5 are SHAPE guards, deliberately scoped to ``[Unreleased]`` (see each
docstring for why: released history legitimately carries 18 duplicate
``###`` headings and 30 duplicate bullets as shipped damage no merge of
*pending* bullets can reach or worsen). Guard 6 is an IMMUTABILITY guard: it
does not look at shape at all, only whether the released region's bytes
changed since the last cut. The two are orthogonal — a released section can
fail every shape check forever (legacy) while still passing guard 6, and a
perfectly-shaped released section fails guard 6 the moment one character in
it changes.

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
    cut_changelog,
    find_version_headings,
    parse_version,
    read_released_checksum,
    released_region_sha256,
    write_released_checksum,
)

CHANGELOG_PATH = Path(__file__).resolve().parents[1] / "CHANGELOG.md"
PYPROJECT_PATH = Path(__file__).resolve().parents[1] / "pyproject.toml"


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


def _unreleased_bullet_blocks(changelog_text: str) -> list[tuple[int, str]]:
    """``[(line_number, normalized_bullet), ...]`` for ``## [Unreleased]``.

    A *block* is one bullet in full: the ``-``/``*`` marker line at column 0
    plus every continuation line belonging to it. Continuations are
    hanging-indented by convention, which is what makes the block boundary
    computable — a marker only starts a new bullet at column 0, so a nested
    sub-bullet or a line of prose opening with an ``*emphasis*`` marker stays
    part of its parent (the same rule ``scripts.release_cut._bullets_in``
    already relies on). A blank line ends the block *unless* the next
    non-blank line is itself indented: this CHANGELOG has ~40 bullets whose
    body runs to a second, blank-line-separated paragraph, and truncating
    them at the paragraph break would compare only their opening paragraphs.

    Normalization collapses every whitespace run — the newlines joining
    continuation lines included — into single spaces, and drops the marker.
    Line wrapping is therefore invisible to the comparison, which is
    deliberate: re-wrapping a bullet to a different column does not make it a
    different bullet, and no two genuinely distinct bullets are distinguished
    only by where their lines break.
    """
    lines = changelog_text.splitlines()
    start, end = _unreleased_bounds(changelog_text)
    body = lines[start + 1 : end]

    def _starts_bullet(line: str) -> bool:
        stripped = line.strip()
        if not stripped or line[0] not in "-*":
            return False
        # a `---` / `***` horizontal rule carries no text of its own
        return not set(stripped) <= {"-", "*", " "}

    def _continues(index: int) -> bool:
        """Does ``body[index]`` extend the open bullet rather than end it?"""
        line = body[index]
        if line.strip():
            return not line.startswith("#") and not _starts_bullet(line)
        # blank: only a paragraph break inside the bullet if an indented
        # continuation line follows before any heading/bullet/end-of-section
        for later in body[index + 1 :]:
            if not later.strip():
                continue
            return later[:1].isspace() and not later.strip().startswith("#")
        return False

    blocks: list[tuple[int, str]] = []
    index = 0
    while index < len(body):
        if not _starts_bullet(body[index]):
            index += 1
            continue
        first, parts = index, [body[index].strip()[1:]]
        index += 1
        while index < len(body) and _continues(index):
            parts.append(body[index])
            index += 1
        # `start + 1` is body[0]; +1 again for 1-based line numbers
        blocks.append((start + first + 2, " ".join(" ".join(parts).split())))
    return blocks


def assert_no_duplicate_unreleased_bullets(changelog_text: str) -> None:
    """No bullet appears twice inside ``## [Unreleased]``.

    This is the residue of the previous guard's failure mode. When a merge
    leaves ``### Added`` … ``### Added``, the tempting repair is to
    concatenate the two groups' bodies under one heading — which satisfies
    :func:`assert_no_duplicate_unreleased_subsections` (one heading now) while
    keeping *both* copies of every bullet the two groups had in common. That
    is what happened while consolidating a merge in #1588: headings merged,
    bullets doubled, all four earlier checks green, and the doubled release
    notes were caught by eye rather than by CI.

    What counts as "the same bullet" is the whole block, whitespace-collapsed
    — not its first line. Sharing a first line is not evidence of duplication
    in this CHANGELOG: released history holds pairs of bullets whose opening
    line is identical and whose bodies genuinely differ, because a bullet was
    revised in place and both revisions survive (e.g. the two flea-market
    content-guardrail bullets, identical for 130 characters and then differing
    in every threshold they quote). A first-line check would reject those.
    Conversely the corruption copies text verbatim, so exact-after-whitespace
    equality is enough to catch it — no fuzzy matching, which would only buy
    back the false positives the block comparison just avoided.

    Scoped to ``[Unreleased]`` for the same reason as the subsection check:
    released sections carry this damage as legacy (30 bullets in shipped
    history are exact duplicates of another bullet in the same section) and
    no merge of *pending* bullets can reach or worsen it.
    """
    by_text: dict[str, list[int]] = {}
    for line_number, bullet in _unreleased_bullet_blocks(changelog_text):
        by_text.setdefault(bullet, []).append(line_number)
    dupes = {bullet: lines for bullet, lines in by_text.items() if len(lines) > 1}
    detail = "; ".join(
        f"lines {', '.join(str(n) for n in lines)}: {bullet[:80]}…" for bullet, lines in sorted(dupes.items())
    )
    assert not dupes, (
        f"CHANGELOG.md '## [Unreleased]' lists {len(dupes)} bullet(s) more than once — {detail} "
        f"(docs/RELEASING.md § CHANGELOG merge hazards). This is what merging two duplicate "
        f"'### <Group>' blocks by concatenating their bodies leaves behind: one heading, every "
        f"shared bullet twice. Fix by keeping one copy of each bullet, not by deduplicating headings."
    )


def assert_released_region_unchanged(changelog_text: str, pyproject_text: str) -> None:
    """The RELEASED region (everything from the first ``## [X.Y.Z]`` heading
    to EOF) must match the digest ``scripts/release_cut.py`` stamps into
    ``pyproject.toml``'s ``[tool.agnes] released_changelog_sha256`` at every
    cut.

    This is the guard #1918 exists for. The five guards above are SHAPE
    guards, scoped to ``[Unreleased]`` on purpose (see their docstrings) —
    none of them notice a bullet a bad merge wrote INTO an already-released
    block, because a bullet landing under an existing heading disturbs no
    heading count, no version order, and creates no duplicate. A released
    block is the permanent record of what shipped under a tag; a GitHub
    Release generated from a mutated block describes a version that never
    existed. This struck twice in one day during a merge train.

    ``cut_changelog``'s own docstring guarantees everything from the heading
    *after* the one it renames onward is preserved byte-for-byte — which is
    exactly what makes one whole-region hash sufficient rather than a hash
    per version block: the region legitimately changes on exactly one
    occasion (a cut growing a freshly-released block at its top), and
    ``plan_release_cut`` recomputes and re-stores this same digest in the
    same write that performs that change. So the stored and live digests can
    only disagree via an out-of-band edit to already-released history.

    NOTE — deviation from the issue's acceptance criteria: a single
    whole-region hash can tell WHETHER released history changed but cannot
    NAME which of the ~625 released blocks it landed in; the message below
    can only point at the region as a whole. Naming the specific block would
    need a digest-per-block manifest (~625 entries) kept in sync forever —
    deliberately not built here.
    """
    live = released_region_sha256(changelog_text)
    stored = read_released_checksum(pyproject_text)
    assert live == stored, (
        "CHANGELOG.md's RELEASED region (everything from the first '## [X.Y.Z]' heading "
        "to EOF) no longer matches the checksum recorded in pyproject.toml's [tool.agnes] "
        "released_changelog_sha256 — a RELEASED block changed after it shipped "
        "(docs/RELEASING.md § CHANGELOG merge hazards). Find the drift with "
        "`git diff origin/main -- CHANGELOG.md` — it should only ever show new bullets under "
        "'## [Unreleased]'. If this edit to released history is deliberate and reviewed, "
        "rebaseline with `python scripts/release_cut.py --rebaseline`. "
        f"(stored={stored!r}, live={live!r})"
    )


# ---------------------------------------------------------------------------
# the real committed CHANGELOG must satisfy all six
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def changelog_text() -> str:
    return CHANGELOG_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def pyproject_text() -> str:
    return PYPROJECT_PATH.read_text(encoding="utf-8")


def test_exactly_one_unreleased_heading(changelog_text: str) -> None:
    assert_single_unreleased(changelog_text)


def test_no_duplicate_version_headings(changelog_text: str) -> None:
    # reuses the same guard the daily cut runs before renaming [Unreleased]
    assert_no_duplicate_headings(changelog_text)


def test_versions_strictly_descending(changelog_text: str) -> None:
    assert_versions_strictly_descending(changelog_text)


def test_no_duplicate_unreleased_subsections(changelog_text: str) -> None:
    assert_no_duplicate_unreleased_subsections(changelog_text)


def test_no_duplicate_unreleased_bullets(changelog_text: str) -> None:
    assert_no_duplicate_unreleased_bullets(changelog_text)


def test_released_region_matches_the_stored_checksum(changelog_text: str, pyproject_text: str) -> None:
    # the sixth guard: IMMUTABILITY, not shape — see assert_released_region_unchanged
    assert_released_region_unchanged(changelog_text, pyproject_text)


def test_block_extractor_finds_bullets_in_a_known_fixture() -> None:
    """The duplicate-bullet guard is only as real as its parse.

    A block extractor that silently found nothing would make
    :func:`assert_no_duplicate_unreleased_bullets` vacuously true on every
    file forever — the same false-green this module exists to prevent, one
    level down.

    The anti-vacuity check runs against a fixture, not the live file,
    because ``[Unreleased]`` is legitimately EMPTY on a release-cut branch:
    ``scripts/release_cut.py`` renames the section to the new version and
    opens a fresh empty one, so asserting the real file always yields
    bullets would fail every cut PR the daily-cut workflow (#1567) opens —
    turning the guard into a release blocker rather than a corruption
    detector.
    """
    blocks = _unreleased_bullet_blocks(_GOOD)
    assert blocks, "parsed zero bullets from a fixture that has one — the block extractor is broken"


def test_live_unreleased_bullets_are_parsed_where_they_exist(changelog_text: str) -> None:
    """Whatever the live file does hold under ``[Unreleased]`` must parse.

    Empty is allowed (a fresh cut); silently dropping bullets that ARE
    there is not. Line numbers must land inside the section.
    """
    start, end = _unreleased_bounds(changelog_text)
    raw_bullet_lines = [
        line for line in changelog_text.splitlines()[start:end] if line.startswith("- ") or line.startswith("* ")
    ]
    blocks = _unreleased_bullet_blocks(changelog_text)
    if raw_bullet_lines:
        assert blocks, (
            f"'## [Unreleased]' holds {len(raw_bullet_lines)} bullet line(s) but the "
            "block extractor parsed none — the extractor is broken"
        )
    for line_number, bullet in blocks:
        assert start + 1 < line_number <= end, f"bullet at line {line_number} is outside [Unreleased]"
        assert bullet.strip(), f"empty bullet parsed at line {line_number}"


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
    """The synthetic well-formed fixture passes all five SHAPE guards (proves
    the guards fire on the corruption, not indiscriminately).

    The sixth (immutability) guard needs a paired pyproject digest, not just
    changelog text, so its own "good" case lives among the teeth tests below
    (``test_a_release_cut_output_still_passes_the_guard``) instead of here.
    """
    assert_single_unreleased(_GOOD)
    assert_no_duplicate_headings(_GOOD)
    assert_versions_strictly_descending(_GOOD)
    assert_no_duplicate_unreleased_subsections(_GOOD)
    assert_no_duplicate_unreleased_bullets(_GOOD)


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
    # shape guards stay Unreleased-only; released-history IMMUTABILITY is the
    # separate sixth guard (assert_released_region_unchanged), not this one.
    corrupt = _GOOD.replace(
        "- a shipped change",
        "- a shipped change\n\n### Added\n\n- a legacy duplicate in released history",
    )
    assert_no_duplicate_unreleased_subsections(corrupt)


# --- fifth guard: the same bullet listed twice under one heading -----------

# A realistic multi-line bullet: `- **Title.**` plus hanging-indented
# continuation lines, the shape every bullet in this CHANGELOG actually has.
_WRAPPED_BULLET = """- **The broker no longer forwards an unscoped token.** The outbound
  upstream URL is built from the same canonical subpath used for the
  policy decision, so a caller cannot reach a sibling endpoint.
"""

# The same bullet, re-wrapped at a different column — still the same bullet.
_WRAPPED_BULLET_REFLOWED = """- **The broker no longer forwards an unscoped token.** The
  outbound upstream URL is built from the same canonical
  subpath used for the policy decision, so a caller cannot
  reach a sibling endpoint.
"""


def test_bullet_duplicated_under_one_heading_is_caught() -> None:
    """The #1588 shape: duplicate ``### Added`` groups "resolved" by
    concatenating their bodies.

    Merging the two headings is the tempting repair, and it satisfies the
    fourth guard — one ``### Added`` again — while leaving both copies of
    every bullet the two groups shared. All four earlier guards are asserted
    green on this fixture first, so the coverage gap is pinned rather than
    assumed: if a future refactor ever made one of them cover this case, the
    redundancy becomes visible instead of silent.
    """
    corrupt = _GOOD.replace(
        "- a pending change\n",
        f"- a pending change\n{_WRAPPED_BULLET}- a second pending change\n{_WRAPPED_BULLET}",
    )
    assert_single_unreleased(corrupt)
    assert_no_duplicate_headings(corrupt)
    assert_versions_strictly_descending(corrupt)
    assert_no_duplicate_unreleased_subsections(corrupt)

    with pytest.raises(AssertionError, match="more than once"):
        assert_no_duplicate_unreleased_bullets(corrupt)


def test_duplicate_bullet_is_caught_across_different_groups() -> None:
    """One bullet under ``### Added`` and again under ``### Fixed``.

    A merge can land the two copies in different groups, which leaves every
    heading unique too. The guard scopes to the whole ``[Unreleased]``
    section rather than per-group for exactly this reason: a change belongs
    in the release notes once, whichever group it sits in.
    """
    corrupt = _GOOD.replace(
        "- a pending change\n",
        f"- a pending change\n{_WRAPPED_BULLET}\n### Fixed\n\n{_WRAPPED_BULLET}",
    )
    assert_no_duplicate_unreleased_subsections(corrupt)

    with pytest.raises(AssertionError, match="more than once"):
        assert_no_duplicate_unreleased_bullets(corrupt)


def test_a_reflowed_duplicate_is_still_a_duplicate() -> None:
    """Line wrapping is not identity — the copy re-wrapped at a different
    column is the same bullet, which is why the comparison collapses
    whitespace before comparing."""
    corrupt = _GOOD.replace(
        "- a pending change\n",
        f"- a pending change\n{_WRAPPED_BULLET}{_WRAPPED_BULLET_REFLOWED}",
    )
    with pytest.raises(AssertionError, match="more than once"):
        assert_no_duplicate_unreleased_bullets(corrupt)


def test_bullets_sharing_a_first_line_are_not_duplicates() -> None:
    """The false positive a first-line check would produce.

    Released history holds pairs of bullets whose opening line is identical
    and whose bodies genuinely differ — a bullet revised in place while the
    earlier revision survived (the two flea-market content-guardrail bullets
    agree for 130 characters, then disagree in every threshold they quote).
    Comparing whole blocks is what keeps those legitimate.
    """
    revised = _WRAPPED_BULLET.replace("a sibling endpoint", "a sibling endpoint or a sibling tenant")
    assert revised.split("\n")[0] == _WRAPPED_BULLET.split("\n")[0]  # same first line
    fixture = _GOOD.replace("- a pending change\n", f"- a pending change\n{_WRAPPED_BULLET}{revised}")

    assert_no_duplicate_unreleased_bullets(fixture)


def test_multi_paragraph_bullets_compare_by_their_whole_body() -> None:
    """~40 bullets in this CHANGELOG run to a second, blank-line-separated
    paragraph. Ending the block at the paragraph break would compare only
    their opening paragraphs and flag two distinct bullets as one."""
    shared_opening = "- **The same opening paragraph.** It says the same thing\n  in both bullets, at length.\n"
    fixture = _GOOD.replace(
        "- a pending change\n",
        f"- a pending change\n{shared_opening}\n  But this one closes on the first follow-up.\n"
        f"{shared_opening}\n  While this one closes on a different follow-up entirely.\n",
    )
    assert_no_duplicate_unreleased_bullets(fixture)

    # ...and two bullets identical through *both* paragraphs still are caught
    corrupt = _GOOD.replace(
        "- a pending change\n",
        f"- a pending change\n{shared_opening}\n  And the same follow-up.\n{shared_opening}\n  And the same follow-up.\n",
    )
    with pytest.raises(AssertionError, match="more than once"):
        assert_no_duplicate_unreleased_bullets(corrupt)


def test_nested_sub_bullets_are_folded_into_their_parent() -> None:
    """An indented sub-bullet is part of its parent, not a bullet of its own.

    Two different parents may legitimately end on the same short sub-bullet
    (``- Tests added.``); only a marker at column 0 opens a new block, so
    that shared tail never reads as a duplicate on its own.
    """
    fixture = _GOOD.replace(
        "- a pending change\n",
        "- **First parent.** Does one thing.\n  - Tests added.\n"
        "- **Second parent.** Does another thing.\n  - Tests added.\n",
    )
    assert_no_duplicate_unreleased_bullets(fixture)


def test_duplicate_bullet_in_a_released_section_is_ignored() -> None:
    """Scoped to ``[Unreleased]``, like the subsection guard above: 30
    bullets in shipped history are exact duplicates of another bullet in the
    same released section, and no merge of pending bullets can reach them."""
    # shape guards stay Unreleased-only; released-history IMMUTABILITY is the
    # separate sixth guard (assert_released_region_unchanged), not this one.
    corrupt = _GOOD.replace(
        "- a shipped change\n",
        f"- a shipped change\n{_WRAPPED_BULLET}{_WRAPPED_BULLET}",
    )
    assert_no_duplicate_unreleased_bullets(corrupt)


# --- sixth guard: released history must not change (#1918) -----------------

# A standalone pyproject.toml stub — the digest these tests bake into it is
# always computed FOR the paired _GOOD-derived text, never borrowed from the
# real repo's pyproject.toml (whose digest covers the real CHANGELOG.md, an
# unrelated document).
_PYPROJECT_STUB = '[project]\nname = "x"\nversion = "0.89.0"\n'


def test_bullet_injected_into_a_released_block_is_caught() -> None:
    """The #1918 shape: a bullet added straight into an already-released
    ``### Added`` group. Every shape guard above stays green on this — no
    heading repeats, order holds, nothing is duplicated — which is exactly
    why #1918 needed a guard that isn't a shape guard."""
    corrupt = _GOOD.replace(
        "- a shipped change\n",
        "- a shipped change\n- a bullet a bad merge slipped into shipped history\n",
    )
    good_pyproject = write_released_checksum(_PYPROJECT_STUB, released_region_sha256(_GOOD))

    assert_single_unreleased(corrupt)
    assert_no_duplicate_headings(corrupt)
    assert_versions_strictly_descending(corrupt)
    assert_no_duplicate_unreleased_subsections(corrupt)
    assert_no_duplicate_unreleased_bullets(corrupt)

    with pytest.raises(AssertionError, match="RELEASED"):
        assert_released_region_unchanged(corrupt, good_pyproject)


def test_blank_line_change_inside_a_released_block_is_caught() -> None:
    """Even whitespace-only drift inside a released block must be caught —
    the checksum is over raw bytes, not a normalized/whitespace-collapsed
    view like the bullet-block comparison the fifth guard uses."""
    corrupt = _GOOD.replace(
        "## [0.88.0] - 2026-08-24\n\n### Fixed",
        "## [0.88.0] - 2026-08-24\n\n\n### Fixed",
    )
    good_pyproject = write_released_checksum(_PYPROJECT_STUB, released_region_sha256(_GOOD))

    with pytest.raises(AssertionError, match="RELEASED"):
        assert_released_region_unchanged(corrupt, good_pyproject)


def test_a_release_cut_output_still_passes_the_guard() -> None:
    """The one legitimate way released history grows: a cut. Cutting
    ``_GOOD`` through the real ``cut_changelog`` and recomputing the digest
    the way ``plan_release_cut`` does must NOT trip the guard — proves the
    checksum tracks "changed since the last cut", not "changed at all"."""
    cut = cut_changelog(_GOOD, "0.90.0", date="2026-08-26")
    cut_pyproject = write_released_checksum(_PYPROJECT_STUB, released_region_sha256(cut))

    assert_released_region_unchanged(cut, cut_pyproject)
