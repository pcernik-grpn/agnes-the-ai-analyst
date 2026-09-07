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
    FragmentFormatError,
    bump_major,
    bump_minor,
    bump_patch,
    bump_pyproject_version,
    bump_server_json_version,
    cut_changelog,
    has_unreleased_content,
    main,
    merge_fragments,
    parse_fragment,
    plan_release_cut,
    read_fragments,
    read_pyproject_version,
    read_released_checksum,
    released_region,
    released_region_sha256,
    unreleased_bullets,
    write_released_checksum,
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


# ---------------------------------------------------------------------------
# released-region checksum (#1918) — shared primitives
# ---------------------------------------------------------------------------


def test_released_region_starts_at_the_first_released_heading():
    region = released_region(_CL_WITH_BULLETS)
    assert region.startswith("## [0.88.0] - 2026-08-25")
    assert "A new user-visible thing." not in region  # that bullet is [Unreleased], not released
    assert "Something already released." in region


def test_released_region_is_empty_with_no_released_heading_yet():
    only_unreleased = _CL_HEADER + "## [Unreleased]\n\n### Added\n\n- pending\n"
    assert released_region(only_unreleased) == ""


def test_released_region_sha256_changes_when_released_text_changes():
    digest = released_region_sha256(_CL_WITH_BULLETS)
    mutated = _CL_WITH_BULLETS.replace("Something already released.", "Something EDITED after shipping.")
    assert released_region_sha256(mutated) != digest


def test_released_region_sha256_is_stable_for_unchanged_text():
    assert released_region_sha256(_CL_WITH_BULLETS) == released_region_sha256(_CL_WITH_BULLETS)


def test_released_region_sha256_ignores_unreleased_edits():
    """Only the released region is hashed — a pending ``[Unreleased]`` bullet
    changing does not move the digest, which is what lets a feature PR keep
    adding bullets forever without ever touching this checksum."""
    digest = released_region_sha256(_CL_WITH_BULLETS)
    edited_unreleased = _CL_WITH_BULLETS.replace("A new user-visible thing.", "A DIFFERENT pending thing.")
    assert released_region_sha256(edited_unreleased) == digest


def test_read_released_checksum_missing_is_none():
    assert read_released_checksum(_PYPROJECT) is None


def test_write_then_read_released_checksum_round_trips():
    digest = "a" * 64
    updated = write_released_checksum(_PYPROJECT, digest)
    assert read_released_checksum(updated) == digest
    # every other byte preserved, like bump_pyproject_version
    assert 'name = "agnes-the-ai-analyst"' in updated
    assert 'version = "0.88.0"' in updated


def test_write_released_checksum_replaces_an_existing_value_in_place():
    once = write_released_checksum(_PYPROJECT, "a" * 64)
    twice = write_released_checksum(once, "b" * 64)
    assert read_released_checksum(twice) == "b" * 64
    assert twice.count("[tool.agnes]") == 1  # not duplicated on a second write


# ---------------------------------------------------------------------------
# wiring the checksum into the cut itself (#1918)
# ---------------------------------------------------------------------------


def test_plan_release_cut_writes_the_new_released_region_checksum():
    plan = plan_release_cut(
        changelog_text=_CL_WITH_BULLETS,
        pyproject_text=_PYPROJECT,
        bump="minor",
        date="2026-08-26",
    )
    assert plan.released_changelog_sha256 == released_region_sha256(plan.changelog_text)
    assert read_released_checksum(plan.pyproject_text) == plan.released_changelog_sha256
    # the block that was just cut is now covered by the digest
    assert "A new user-visible thing." in released_region(plan.changelog_text)


def test_plan_release_cut_noop_leaves_the_checksum_untouched():
    plan = plan_release_cut(
        changelog_text=_CL_EMPTY_UNRELEASED,
        pyproject_text=_PYPROJECT,
        bump="minor",
        date="2026-08-26",
    )
    assert plan.noop is True
    assert plan.released_changelog_sha256 is None
    assert plan.pyproject_text is None


def test_main_cut_writes_the_released_checksum_alongside_the_version_bump(tmp_path):
    """The zero-workflow-edit design: ``main()`` already writes pyproject.toml
    (which ``daily-cut.yml`` already ``git add``s) on every cut, so once that
    write includes the checksum, the workflow needs no edits to pick it up."""
    changelog = tmp_path / "CHANGELOG.md"
    pyproject = tmp_path / "pyproject.toml"
    changelog.write_text(_CL_WITH_BULLETS, encoding="utf-8")
    pyproject.write_text(_PYPROJECT, encoding="utf-8")

    rc = main(
        [
            "--changelog",
            str(changelog),
            "--pyproject",
            str(pyproject),
            "--no-server-json",
            "--bump",
            "minor",
            "--date",
            "2026-08-26",
        ]
    )
    assert rc == 0

    new_changelog = changelog.read_text(encoding="utf-8")
    new_pyproject = pyproject.read_text(encoding="utf-8")
    assert read_released_checksum(new_pyproject) == released_region_sha256(new_changelog)
    assert 'version = "0.89.0"' in new_pyproject


def test_main_rebaseline_rewrites_the_checksum_without_cutting(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    pyproject = tmp_path / "pyproject.toml"
    changelog.write_text(_CL_WITH_BULLETS, encoding="utf-8")
    pyproject.write_text(write_released_checksum(_PYPROJECT, "0" * 64), encoding="utf-8")

    rc = main(["--changelog", str(changelog), "--pyproject", str(pyproject), "--rebaseline"])
    assert rc == 0

    # CHANGELOG.md is untouched — --rebaseline cuts nothing
    assert changelog.read_text(encoding="utf-8") == _CL_WITH_BULLETS
    assert read_released_checksum(pyproject.read_text(encoding="utf-8")) == released_region_sha256(_CL_WITH_BULLETS)


def test_main_rebaseline_dry_run_writes_nothing(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    pyproject = tmp_path / "pyproject.toml"
    changelog.write_text(_CL_WITH_BULLETS, encoding="utf-8")
    pyproject.write_text(_PYPROJECT, encoding="utf-8")

    rc = main(["--changelog", str(changelog), "--pyproject", str(pyproject), "--rebaseline", "--dry-run"])
    assert rc == 0
    assert pyproject.read_text(encoding="utf-8") == _PYPROJECT


def test_main_rebaseline_refuses_a_malformed_changelog(tmp_path):
    dup = _CL_HEADER + "## [0.88.0] - 2026-08-25\n\n- a\n\n## [0.88.0] - 2026-08-25\n\n- b\n"
    changelog = tmp_path / "CHANGELOG.md"
    pyproject = tmp_path / "pyproject.toml"
    changelog.write_text(dup, encoding="utf-8")
    pyproject.write_text(_PYPROJECT, encoding="utf-8")

    rc = main(["--changelog", str(changelog), "--pyproject", str(pyproject), "--rebaseline"])
    assert rc == 1
    assert pyproject.read_text(encoding="utf-8") == _PYPROJECT  # untouched on failure


# ---------------------------------------------------------------------------
# changelog.d/ fragments (#2295)
# ---------------------------------------------------------------------------

_FRAG_ADDED = "### Added\n- **A thing from PR one.** Details.\n"
_FRAG_TWO_GROUPS = "### Fixed\n- A fix from PR two,\n  wrapped onto a second line.\n\n### Internal\n- A refactor.\n"
_PYPROJECT_FOR_FRAGMENTS = '[project]\nname = "agnes"\nversion = "0.88.0"\n'


def test_parse_fragment_returns_groups_with_raw_body_lines():
    groups = parse_fragment("one.md", _FRAG_TWO_GROUPS)
    assert list(groups) == ["Fixed", "Internal"]
    assert groups["Fixed"] == ["- A fix from PR two,", "  wrapped onto a second line."]
    assert groups["Internal"] == ["- A refactor."]


@pytest.mark.parametrize(
    "bad, needle",
    [
        ("- a bullet with no heading\n", "text before the first"),
        ("### Security\n- x\n", "unknown group"),
        ("### Added\n\n", "has no bullet"),
        ("## [Unreleased]\n### Added\n- x\n", "only '### <Group>'"),
        ("### Added\n- x\n### Added\n- y\n", "appears twice"),
        ("### Added\n<<<<<<< HEAD\n- x\n", "conflict marker"),
        ("\n\n", "no '### <Group>' heading"),
    ],
)
def test_parse_fragment_rejects_a_malformed_fragment_and_names_the_fix(bad, needle):
    with pytest.raises(FragmentFormatError) as excinfo:
        parse_fragment("bad.md", bad)
    message = str(excinfo.value)
    assert needle in message
    assert "changelog.d/bad.md" in message
    assert "changelog.d/README.md" in message


def test_merge_fragments_appends_under_existing_groups_in_filename_order():
    merged = merge_fragments(
        _CL_WITH_BULLETS,
        {"b.md": "### Added\n- From b.\n", "a.md": "### Added\n- From a.\n"},
    )
    assert unreleased_bullets(merged) == [
        "- A new user-visible thing.",
        "- From a.",
        "- From b.",
        "- A bug fix.",
    ]


def test_merge_fragments_creates_a_missing_group_in_keep_a_changelog_order():
    merged = merge_fragments(_CL_WITH_BULLETS, {"x.md": "### Changed\n- Now different.\n"})
    body = merged.split("## [Unreleased]")[1].split("## [0.88.0]")[0]
    assert body.index("### Added") < body.index("### Changed") < body.index("### Fixed")
    assert "### Changed\n- Now different.\n\n### Fixed" in body


def test_merge_fragments_preserves_preamble_and_released_sections_byte_for_byte():
    merged = merge_fragments(_CL_WITH_BULLETS, {"x.md": _FRAG_ADDED})
    assert merged.startswith(_CL_HEADER)
    assert released_region(merged) == released_region(_CL_WITH_BULLETS)


def test_merge_fragments_keeps_continuation_lines_verbatim():
    merged = merge_fragments(_CL_EMPTY_UNRELEASED, {"x.md": _FRAG_TWO_GROUPS})
    assert "### Fixed\n- A fix from PR two,\n  wrapped onto a second line.\n\n### Removed" in merged
    assert "### Internal\n- A refactor.\n\n## [0.88.0]" in merged


def test_merge_fragments_without_fragments_is_the_identity():
    assert merge_fragments(_CL_WITH_BULLETS, {}) == _CL_WITH_BULLETS


def test_merge_fragments_surfaces_a_malformed_fragment():
    with pytest.raises(FragmentFormatError):
        merge_fragments(_CL_WITH_BULLETS, {"bad.md": "### Nope\n- x\n"})


def test_plan_release_cut_folds_fragments_and_lists_them_for_deletion():
    plan = plan_release_cut(
        changelog_text=_CL_EMPTY_UNRELEASED,
        pyproject_text=_PYPROJECT_FOR_FRAGMENTS,
        date="2026-09-06",
        fragments={"pr-2.md": _FRAG_TWO_GROUPS, "pr-1.md": _FRAG_ADDED},
    )
    assert plan.noop is False
    assert plan.version == "0.89.0"
    assert plan.fragment_paths == ("pr-1.md", "pr-2.md")
    assert "- **A thing from PR one.** Details." in plan.bullets
    assert "- A fix from PR two," in plan.bullets  # bullets are first lines, as before
    assert plan.changelog_text is not None
    assert "## [0.89.0] - 2026-09-06" in plan.changelog_text
    assert "- **A thing from PR one.** Details." in plan.changelog_text
    assert plan.as_json()["fragments"] == ["pr-1.md", "pr-2.md"]


def test_plan_release_cut_is_a_noop_with_empty_unreleased_and_no_fragments():
    plan = plan_release_cut(
        changelog_text=_CL_EMPTY_UNRELEASED,
        pyproject_text=_PYPROJECT_FOR_FRAGMENTS,
        date="2026-09-06",
        fragments={},
    )
    assert plan.noop is True
    assert plan.fragment_paths == ()


def test_read_fragments_skips_the_readme_and_non_markdown_and_sorts(tmp_path):
    directory = tmp_path / "changelog.d"
    directory.mkdir()
    (directory / "README.md").write_text("how to", encoding="utf-8")
    (directory / "b.md").write_text(_FRAG_ADDED, encoding="utf-8")
    (directory / "a.md").write_text(_FRAG_ADDED, encoding="utf-8")
    (directory / "notes.txt").write_text("ignored", encoding="utf-8")
    assert list(read_fragments(directory)) == ["a.md", "b.md"]
    assert read_fragments(tmp_path / "missing") == {}


def _fragment_checkout(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(_CL_EMPTY_UNRELEASED, encoding="utf-8")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(_PYPROJECT_FOR_FRAGMENTS, encoding="utf-8")
    fragments = tmp_path / "changelog.d"
    fragments.mkdir()
    (fragments / "README.md").write_text("how to", encoding="utf-8")
    (fragments / "pr.md").write_text(_FRAG_ADDED, encoding="utf-8")
    return changelog, pyproject, fragments


def test_main_cut_deletes_exactly_the_fragments_it_shipped(tmp_path):
    changelog, pyproject, fragments = _fragment_checkout(tmp_path)
    rc = main(
        [
            "--changelog",
            str(changelog),
            "--pyproject",
            str(pyproject),
            "--no-server-json",
            "--fragments-dir",
            str(fragments),
            "--date",
            "2026-09-06",
        ]
    )
    assert rc == 0
    assert not (fragments / "pr.md").exists()
    assert (fragments / "README.md").exists()
    written = changelog.read_text(encoding="utf-8")
    assert "## [0.89.0] - 2026-09-06" in written
    assert "- **A thing from PR one.** Details." in written
    assert unreleased_bullets(written) == []


def test_main_dry_run_leaves_the_fragments_in_place(tmp_path):
    changelog, pyproject, fragments = _fragment_checkout(tmp_path)
    rc = main(
        [
            "--changelog",
            str(changelog),
            "--pyproject",
            str(pyproject),
            "--no-server-json",
            "--fragments-dir",
            str(fragments),
            "--date",
            "2026-09-06",
            "--dry-run",
        ]
    )
    assert rc == 0
    assert (fragments / "pr.md").exists()
    assert changelog.read_text(encoding="utf-8") == _CL_EMPTY_UNRELEASED


def test_main_default_fragments_dir_follows_the_changelog_not_the_cwd(tmp_path, monkeypatch):
    """Regression: the default used to be ``Path("changelog.d")`` against CWD, so
    a ``main()`` pointed at a temp ``--changelog`` (this very test module) read the
    REPO's fragments into the temp cut and deleted them from the checkout."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    decoy_dir = checkout / "changelog.d"
    decoy_dir.mkdir()
    (decoy_dir / "decoy.md").write_text("### Added\n- Must survive.\n", encoding="utf-8")
    monkeypatch.chdir(checkout)

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    changelog, pyproject, fragments = _fragment_checkout(elsewhere)
    rc = main(
        [
            "--changelog",
            str(changelog),
            "--pyproject",
            str(pyproject),
            "--no-server-json",
            "--date",
            "2026-09-06",
        ]
    )
    assert rc == 0
    assert (decoy_dir / "decoy.md").exists(), "the CWD's fragments must not be touched"
    written = changelog.read_text(encoding="utf-8")
    assert "- Must survive." not in written
    assert "- **A thing from PR one.** Details." in written, "the changelog's own changelog.d/ IS folded"
    assert not (fragments / "pr.md").exists()


def test_main_reports_a_malformed_fragment_and_writes_nothing(tmp_path, capsys):
    changelog, pyproject, fragments = _fragment_checkout(tmp_path)
    (fragments / "bad.md").write_text("### Nope\n- x\n", encoding="utf-8")
    rc = main(
        [
            "--changelog",
            str(changelog),
            "--pyproject",
            str(pyproject),
            "--no-server-json",
            "--fragments-dir",
            str(fragments),
            "--date",
            "2026-09-06",
        ]
    )
    assert rc == 1
    assert "changelog.d/bad.md" in capsys.readouterr().err
    assert changelog.read_text(encoding="utf-8") == _CL_EMPTY_UNRELEASED
    assert (fragments / "pr.md").exists()
