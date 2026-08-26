"""Tests for ``scripts/release_cut.py`` — the daily release-cut logic.

E1 (docs/superpowers/plans/2026-08-24-agnes-remediation-program.md, Track E)
moves the version bump + CHANGELOG rename out of feature PRs (which raced on
the same ``[Unreleased]`` section and produced duplicated version headings on
merge) and into a single daily cut PR. This module is the cut arithmetic as
pure functions over file contents; ``daily-cut.yml`` and an emergency-hotfix
human both call the exact same code these tests pin.
"""

from __future__ import annotations

import pytest

from scripts.release_cut import (
    ChangelogFormatError,
    DuplicateHeadingError,
    bump_major,
    bump_minor,
    bump_patch,
    bump_pyproject_version,
    bump_server_json_version,
    cut_changelog,
    has_unreleased_content,
    plan_release_cut,
    read_pyproject_version,
    unreleased_bullets,
)

# ---------------------------------------------------------------------------
# version bumping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "current, expected",
    [
        ("0.88.0", "0.89.0"),
        ("0.9.9", "0.10.0"),
        ("0.83.99", "0.84.0"),
        ("1.2.3", "1.3.0"),
        ("2.0.0", "2.1.0"),
    ],
)
def test_bump_minor_from_arbitrary_current_version(current, expected):
    assert bump_minor(current) == expected


@pytest.mark.parametrize(
    "current, expected",
    [
        ("0.88.0", "0.88.1"),
        ("0.83.99", "0.83.100"),
        ("1.2.3", "1.2.4"),
    ],
)
def test_bump_patch_from_arbitrary_current_version(current, expected):
    assert bump_patch(current) == expected


def test_bump_major_resets_minor_and_patch():
    assert bump_major("0.88.3") == "1.0.0"


def test_bump_rejects_malformed_version():
    with pytest.raises(ValueError):
        bump_minor("not-a-version")
    with pytest.raises(ValueError):
        bump_minor("1.2")


# ---------------------------------------------------------------------------
# pyproject.toml / server.json
# ---------------------------------------------------------------------------

_PYPROJECT = """[project]
name = "agnes-the-ai-analyst"
version = "0.88.0"
description = "x"
"""

_SERVER_JSON = """{
  "name": "io.github.keboola/agnes",
  "version": "0.88.0",
  "other": "field"
}
"""


def test_read_pyproject_version():
    assert read_pyproject_version(_PYPROJECT) == "0.88.0"


def test_read_pyproject_version_missing_raises():
    with pytest.raises(ChangelogFormatError):
        read_pyproject_version("[project]\nname = 'x'\n")


def test_bump_pyproject_version_touches_only_the_version_line():
    updated = bump_pyproject_version(_PYPROJECT, "0.89.0")
    assert 'version = "0.89.0"' in updated
    assert 'name = "agnes-the-ai-analyst"' in updated
    # every other byte preserved
    assert updated.replace("0.89.0", "0.88.0") == _PYPROJECT


def test_bump_server_json_version_touches_only_the_version_field():
    updated = bump_server_json_version(_SERVER_JSON, "0.89.0")
    assert '"version": "0.89.0"' in updated
    assert '"other": "field"' in updated
    assert updated.replace("0.89.0", "0.88.0") == _SERVER_JSON


def test_bump_server_json_version_missing_field_raises():
    with pytest.raises(ChangelogFormatError):
        bump_server_json_version('{"name": "x"}', "0.89.0")


# ---------------------------------------------------------------------------
# CHANGELOG bullet extraction
# ---------------------------------------------------------------------------

_CL_HEADER = """# Changelog

All notable changes to Agnes.

---

"""

_CL_EMPTY_UNRELEASED = (
    _CL_HEADER
    + """## [Unreleased]

### Added

### Changed

### Fixed

### Removed

### Internal

## [0.88.0] - 2026-08-25

### Added

- Something already released.
"""
)

_CL_WITH_BULLETS = (
    _CL_HEADER
    + """## [Unreleased]

### Added

- A new user-visible thing.

### Fixed

- A bug fix.

## [0.88.0] - 2026-08-25

### Added

- Something already released.
"""
)


def test_unreleased_bullets_extraction():
    assert unreleased_bullets(_CL_WITH_BULLETS) == [
        "- A new user-visible thing.",
        "- A bug fix.",
    ]


def test_unreleased_bullets_empty_when_only_subsection_headers():
    assert unreleased_bullets(_CL_EMPTY_UNRELEASED) == []


def test_has_unreleased_content():
    assert has_unreleased_content(_CL_WITH_BULLETS) is True
    assert has_unreleased_content(_CL_EMPTY_UNRELEASED) is False


def test_missing_unreleased_heading_raises():
    with pytest.raises(ChangelogFormatError):
        unreleased_bullets(_CL_HEADER + "## [0.88.0] - 2026-08-25\n\n- x\n")


def test_indented_emphasis_inside_a_bullet_is_not_a_second_bullet():
    """A wrapped bullet's continuation line can open with an *emphasis*
    marker (e.g. '  *was* previously published good...') — real prose from
    this CHANGELOG. It must not be misread as its own top-level bullet."""
    changelog = (
        _CL_HEADER
        + """## [Unreleased]

### Fixed

- A bug fix whose prose wraps and the next line happens to open with
  *emphasis* right after the hanging indent, not a new bullet.

## [0.88.0] - 2026-08-25

### Added

- Something already released.
"""
    )
    bullets = unreleased_bullets(changelog)
    assert len(bullets) == 1
    assert bullets[0].startswith("- A bug fix whose prose wraps")


# ---------------------------------------------------------------------------
# duplicate-heading guard (the known 3-way-merge failure class)
# ---------------------------------------------------------------------------


def test_duplicate_version_heading_raises():
    dup = (
        _CL_HEADER
        + """## [Unreleased]

### Added

- new thing

## [0.88.0] - 2026-08-25

### Added

- branch A's bullet

## [0.88.0] - 2026-08-25

### Fixed

- branch B's bullet
"""
    )
    with pytest.raises(DuplicateHeadingError):
        cut_changelog(dup, "0.89.0", date="2026-08-26")


def test_duplicate_unreleased_heading_also_raises():
    dup = _CL_HEADER + "## [Unreleased]\n\n- a\n\n## [Unreleased]\n\n- b\n"
    with pytest.raises(DuplicateHeadingError):
        cut_changelog(dup, "0.89.0", date="2026-08-26")


def test_plan_release_cut_surfaces_duplicate_heading_as_an_error():
    dup = (
        _CL_HEADER
        + "## [Unreleased]\n\n- new\n\n## [0.88.0] - 2026-08-25\n\n- a\n\n"
        + "## [0.88.0] - 2026-08-25\n\n- b\n"
    )
    with pytest.raises(DuplicateHeadingError):
        plan_release_cut(changelog_text=dup, pyproject_text=_PYPROJECT, bump="minor", date="2026-08-26")


# ---------------------------------------------------------------------------
# cut_changelog: rename + fresh Unreleased + byte-for-byte preservation
# ---------------------------------------------------------------------------


def test_cut_changelog_renames_unreleased_and_opens_a_fresh_one():
    result = cut_changelog(_CL_WITH_BULLETS, "0.89.0", date="2026-08-26")

    assert "## [Unreleased]" in result
    assert "## [0.89.0] - 2026-08-26" in result
    # the old bullets now live under the new version heading
    unreleased_idx = result.index("## [Unreleased]")
    new_version_idx = result.index("## [0.89.0]")
    old_version_idx = result.index("## [0.88.0]")
    assert unreleased_idx < new_version_idx < old_version_idx

    new_section = result[new_version_idx:old_version_idx]
    assert "A new user-visible thing." in new_section
    assert "A bug fix." in new_section

    # the fresh [Unreleased] carries the standard empty subsections and none
    # of the old bullets
    fresh_section = result[unreleased_idx:new_version_idx]
    for name in ("Added", "Changed", "Fixed", "Removed", "Internal"):
        assert f"### {name}" in fresh_section
    assert "A new user-visible thing." not in fresh_section


def test_cut_changelog_preserves_older_sections_byte_for_byte():
    result = cut_changelog(_CL_WITH_BULLETS, "0.89.0", date="2026-08-26")
    old_section_start = _CL_WITH_BULLETS.index("## [0.88.0]")
    old_section_text = _CL_WITH_BULLETS[old_section_start:]
    assert result.endswith(old_section_text)


def test_cut_changelog_preserves_the_preamble_byte_for_byte():
    result = cut_changelog(_CL_WITH_BULLETS, "0.89.0", date="2026-08-26")
    assert result.startswith(_CL_HEADER)


def test_cut_changelog_idempotent_shape_on_second_run():
    """Cutting a file whose [Unreleased] is already empty still produces a
    well-formed file (rename of an empty section is legal at the string
    level) — the *decision* not to do this lives in plan_release_cut, this
    just confirms cut_changelog itself never corrupts a re-run."""
    once = cut_changelog(_CL_EMPTY_UNRELEASED, "0.89.0", date="2026-08-26")
    assert once.count("## [Unreleased]") == 1
    assert once.count("## [0.89.0]") == 1
    assert once.count("## [0.88.0]") == 1


# ---------------------------------------------------------------------------
# plan_release_cut: the orchestration a CLI/workflow calls
# ---------------------------------------------------------------------------


def test_plan_release_cut_is_a_noop_on_empty_unreleased():
    """The idempotence contract: a quiet day (no bullets since the last cut)
    is not an error and writes nothing — the daily workflow just skips
    opening a PR."""
    plan = plan_release_cut(
        changelog_text=_CL_EMPTY_UNRELEASED,
        pyproject_text=_PYPROJECT,
        bump="minor",
        date="2026-08-26",
    )
    assert plan.noop is True
    assert plan.version is None
    assert plan.bullets == ()
    assert plan.changelog_text is None
    assert plan.pyproject_text is None


def test_plan_release_cut_computes_minor_bump_and_bullets():
    plan = plan_release_cut(
        changelog_text=_CL_WITH_BULLETS,
        pyproject_text=_PYPROJECT,
        bump="minor",
        date="2026-08-26",
    )
    assert plan.noop is False
    assert plan.previous_version == "0.88.0"
    assert plan.version == "0.89.0"
    assert plan.bullets == ("- A new user-visible thing.", "- A bug fix.")
    assert plan.changelog_text is not None
    assert 'version = "0.89.0"' in plan.pyproject_text


def test_plan_release_cut_patch_bump_for_hotfix():
    plan = plan_release_cut(
        changelog_text=_CL_WITH_BULLETS,
        pyproject_text=_PYPROJECT,
        bump="patch",
        date="2026-08-26",
    )
    assert plan.version == "0.88.1"


def test_plan_release_cut_updates_server_json_when_provided():
    plan = plan_release_cut(
        changelog_text=_CL_WITH_BULLETS,
        pyproject_text=_PYPROJECT,
        server_json_text=_SERVER_JSON,
        bump="minor",
        date="2026-08-26",
    )
    assert plan.server_json_text is not None
    assert '"version": "0.89.0"' in plan.server_json_text


def test_plan_release_cut_without_server_json_leaves_it_none():
    plan = plan_release_cut(
        changelog_text=_CL_WITH_BULLETS,
        pyproject_text=_PYPROJECT,
        bump="minor",
        date="2026-08-26",
    )
    assert plan.server_json_text is None


def test_plan_release_cut_rejects_unknown_bump_kind():
    with pytest.raises(ValueError):
        plan_release_cut(
            changelog_text=_CL_WITH_BULLETS,
            pyproject_text=_PYPROJECT,
            bump="banana",
            date="2026-08-26",
        )
