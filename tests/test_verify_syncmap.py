"""Tests for ``scripts/verify_syncmap.py`` — the in-loop sync-map verifier.

The script exists to give an agent (or a human) fast, deterministic feedback on
the ``CONTRIBUTING.md`` sync-map rows that carry **no CI guard**. Rows that are
already guarded (route auth → ``tests/test_route_auth_guard.py``; repo method
parity → ``tests/db_pg/test_repo_method_parity.py``; migration ladder →
``tests/test_db_schema_version.py``) are deliberately NOT reimplemented here —
the verifier's job is to cover the gap, and the wrapping skill runs the
existing guards alongside it.

Each detector is a pure function over text/AST so it is testable without a git
repo; ``main()`` is the only part that shells out to git.
"""

from __future__ import annotations

import textwrap

import pytest

from scripts.verify_syncmap import (
    BLOCKING,
    WARN,
    _is_changelog_fragment,
    check_changelog,
    check_entity_scoped_authz,
    check_remote_attach,
    check_resource_type_registry,
    check_scope_flags,
    parse_added_lines,
    unreleased_bullets,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _rules(findings) -> set[str]:
    return {f.rule for f in findings}


# ---------------------------------------------------------------------------
# unified-diff parsing
# ---------------------------------------------------------------------------


def test_parse_added_lines_maps_files_to_new_line_numbers():
    diff = textwrap.dedent(
        """\
        diff --git a/cli/commands/foo.py b/cli/commands/foo.py
        index 1111111..2222222 100644
        --- a/cli/commands/foo.py
        +++ b/cli/commands/foo.py
        @@ -10,0 +11,2 @@ def foo():
        +    remote: bool = typer.Option(False, "--remote"),
        +    limit: int = 10,
        """
    )
    added = parse_added_lines(diff)
    assert list(added) == ["cli/commands/foo.py"]
    assert added["cli/commands/foo.py"] == [
        (11, '    remote: bool = typer.Option(False, "--remote"),'),
        (12, "    limit: int = 10,"),
    ]


def test_parse_added_lines_ignores_removed_lines_and_headers():
    diff = textwrap.dedent(
        """\
        diff --git a/a.py b/a.py
        --- a/a.py
        +++ b/a.py
        @@ -1,2 +1,1 @@
        -gone = 1
         kept = 2
        """
    )
    assert parse_added_lines(diff) == {}


def test_parse_added_lines_handles_multiple_files_and_hunks():
    diff = textwrap.dedent(
        """\
        diff --git a/a.py b/a.py
        --- a/a.py
        +++ b/a.py
        @@ -1,0 +2,1 @@
        +first = 1
        diff --git a/b.py b/b.py
        --- a/b.py
        +++ b/b.py
        @@ -5,0 +6,1 @@
        +second = 2
        @@ -20,0 +30,1 @@
        +third = 3
        """
    )
    added = parse_added_lines(diff)
    assert added["a.py"] == [(2, "first = 1")]
    assert added["b.py"] == [(6, "second = 2"), (30, "third = 3")]


def test_parse_added_lines_skips_new_file_marker_of_devnull_deletes():
    """A deleted file has `+++ /dev/null` — it must not become a key."""
    diff = textwrap.dedent(
        """\
        diff --git a/gone.py b/gone.py
        --- a/gone.py
        +++ /dev/null
        @@ -1,1 +0,0 @@
        -x = 1
        """
    )
    assert parse_added_lines(diff) == {}


# ---------------------------------------------------------------------------
# ResourceType ↔ ResourceTypeSpec  (sync-map: "CI guard? NO")
# ---------------------------------------------------------------------------

_RT_CLEAN = textwrap.dedent(
    """\
    class ResourceType(StrEnum):
        TABLE = "table"
        RECIPE = "recipe"

    RESOURCE_TYPES: dict[ResourceType, ResourceTypeSpec] = {
        ResourceType.TABLE: ResourceTypeSpec(key=ResourceType.TABLE),
        ResourceType.RECIPE: ResourceTypeSpec(key=ResourceType.RECIPE),
    }
    """
)


def test_resource_type_registry_clean_source_has_no_findings():
    assert check_resource_type_registry(_RT_CLEAN) == []


def test_resource_type_registry_flags_member_missing_from_registry():
    source = _RT_CLEAN.replace(
        '    RECIPE = "recipe"',
        '    RECIPE = "recipe"\n    DASHBOARD = "dashboard"',
    )
    findings = check_resource_type_registry(source)
    assert len(findings) == 1
    assert findings[0].severity == BLOCKING
    assert "DASHBOARD" in findings[0].message
    # cites the enum member line, and points at the registry as the mirror
    assert findings[0].line == 4
    assert "RESOURCE_TYPES" in findings[0].mirror


def test_resource_type_registry_ignores_non_member_assignments():
    """Dunder/annotation noise inside the enum body must not be treated as a
    member (an enum body may legitimately carry a docstring and helpers)."""
    source = textwrap.dedent(
        """\
        class ResourceType(StrEnum):
            \"\"\"Docstring.\"\"\"

            TABLE = "table"

            def helper(self):
                return 1

        RESOURCE_TYPES: dict[ResourceType, ResourceTypeSpec] = {
            ResourceType.TABLE: ResourceTypeSpec(key=ResourceType.TABLE),
        }
        """
    )
    assert check_resource_type_registry(source) == []


def test_resource_type_registry_tolerates_absent_enum():
    """Parsing a file that does not define the enum is a no-op, not a crash."""
    assert check_resource_type_registry("x = 1\n") == []


def test_resource_type_registry_reports_every_missing_member():
    source = textwrap.dedent(
        """\
        class ResourceType(StrEnum):
            A = "a"
            B = "b"

        RESOURCE_TYPES: dict[ResourceType, ResourceTypeSpec] = {}
        """
    )
    findings = check_resource_type_registry(source)
    assert {f.message.split()[0] for f in findings} == {"ResourceType.A", "ResourceType.B"}


# ---------------------------------------------------------------------------
# CHANGELOG [Unreleased] bullet  (sync-map: "CI guard? NO")
# ---------------------------------------------------------------------------

_CL_EMPTY = textwrap.dedent(
    """\
    # Changelog

    ## [Unreleased]

    ### Added

    ### Fixed

    ## [0.1.0] - 2026-01-01

    ### Added

    - old thing
    """
)

_CL_WITH_BULLET = _CL_EMPTY.replace(
    "### Added\n\n### Fixed",
    "### Added\n\n- a new user-visible thing\n\n### Fixed",
)


def test_unreleased_bullets_extracts_only_the_unreleased_section():
    assert unreleased_bullets(_CL_EMPTY) == []
    assert unreleased_bullets(_CL_WITH_BULLET) == ["- a new user-visible thing"]


def test_unreleased_bullets_stops_at_the_next_version_heading():
    """Bullets under the previous release must never count as Unreleased."""
    assert "- old thing" not in unreleased_bullets(_CL_WITH_BULLET)


def test_changelog_flags_code_change_without_a_new_bullet():
    findings = check_changelog(
        base_changelog=_CL_EMPTY,
        head_changelog=_CL_EMPTY,
        changed_paths=["app/api/foo.py"],
        version_bumped=False,
    )
    assert len(findings) == 1
    assert findings[0].severity == BLOCKING
    assert findings[0].file == "CHANGELOG.md"


def test_changelog_passes_when_a_new_bullet_was_added():
    findings = check_changelog(
        base_changelog=_CL_EMPTY,
        head_changelog=_CL_WITH_BULLET,
        changed_paths=["app/api/foo.py"],
        version_bumped=False,
    )
    assert findings == []


_FRAG = "### Added\n- a new user-visible thing\n"


def _check_with_fragments(before: dict[str, str], after: dict[str, str]):
    return check_changelog(
        base_changelog=_CL_EMPTY,
        head_changelog=_CL_EMPTY,
        changed_paths=["app/api/foo.py", *after.keys(), *before.keys()],
        version_bumped=False,
        fragments_before=before,
        fragments_after=after,
    )


def test_changelog_passes_when_a_fragment_was_added():
    """The post-#2295 shape: CHANGELOG.md untouched, one changelog.d/ file added."""
    assert _check_with_fragments({}, {"changelog.d/foo-endpoint.md": _FRAG}) == []


def test_changelog_deleted_fragment_does_not_count():
    """`git diff --name-only` lists deletions too; a deleted fragment is no entry."""
    findings = _check_with_fragments({"changelog.d/old.md": _FRAG}, {"changelog.d/old.md": ""})
    assert len(findings) == 1
    assert "changelog.d/<slug>.md" in findings[0].message


def test_changelog_touched_fragment_without_a_new_bullet_does_not_count():
    """Re-wrapping someone else's fragment is not this PR's release note."""
    rewrapped = _FRAG.replace("- a new user-visible thing", "-   a new user-visible thing")
    assert len(_check_with_fragments({"changelog.d/theirs.md": _FRAG}, {"changelog.d/theirs.md": rewrapped})) == 1


def test_changelog_fragment_that_gains_a_bullet_counts():
    grown = _FRAG + "- and a second thing from this PR\n"
    assert _check_with_fragments({"changelog.d/mine.md": _FRAG}, {"changelog.d/mine.md": grown}) == []


def test_changelog_fragment_path_alone_is_not_enough():
    """The path list is not evidence — only the fragment texts are."""
    findings = check_changelog(
        base_changelog=_CL_EMPTY,
        head_changelog=_CL_EMPTY,
        changed_paths=["app/api/foo.py", "changelog.d/foo-endpoint.md"],
        version_bumped=False,
    )
    assert len(findings) == 1


def test_changelog_readme_in_changelog_d_is_not_a_fragment():
    assert _is_changelog_fragment("changelog.d/foo.md") is True
    assert _is_changelog_fragment("changelog.d/README.md") is False
    assert _is_changelog_fragment("docs/changelog.d/foo.md") is False


def test_changelog_ignores_test_docs_and_tooling_only_changes():
    for path in (
        "tests/test_foo.py",
        "docs/architecture.md",
        "scripts/verify_syncmap.py",
        ".claude/skills/foo/SKILL.md",
        "README.md",
        ".github/workflows/ci.yml",
    ):
        assert (
            check_changelog(
                base_changelog=_CL_EMPTY,
                head_changelog=_CL_EMPTY,
                changed_paths=[path],
                version_bumped=False,
            )
            == []
        ), path


def test_changelog_skips_the_check_on_a_release_cut():
    """A release-cut moves [Unreleased] content under the new version, leaving
    [Unreleased] empty. That is the one legitimate 'code changed, no new
    Unreleased bullet' shape — detected via the pyproject version bump."""
    findings = check_changelog(
        base_changelog=_CL_WITH_BULLET,
        head_changelog=_CL_EMPTY,
        changed_paths=["app/api/foo.py", "pyproject.toml"],
        version_bumped=True,
    )
    assert findings == []


def test_changelog_flags_a_reordered_but_unchanged_bullet_set():
    """Re-indenting or reordering existing bullets is not a new bullet."""
    reordered = _CL_WITH_BULLET.replace("- a new user-visible thing", "-   a new user-visible thing")
    findings = check_changelog(
        base_changelog=_CL_WITH_BULLET,
        head_changelog=reordered,
        changed_paths=["src/db.py"],
        version_bumped=False,
    )
    assert len(findings) == 1


# ---------------------------------------------------------------------------
# command-UX: no NEW boolean scope flag  (sync-map: "CI guard? NO")
# ---------------------------------------------------------------------------


def _cli_source(params: str) -> str:
    return f"import typer\n\n\ndef cmd(\n{params}\n):\n    pass\n"


def _added_for(source: str, path: str = "cli/commands/foo.py") -> dict:
    """Treat every line of `source` as added (a brand-new command module)."""
    return {path: list(enumerate(source.splitlines(), start=1))}


def test_scope_flags_rejects_a_new_boolean_scope_flag():
    src = _cli_source('    server: bool = typer.Option(False, "--server", help="run"),')
    findings = check_scope_flags(_added_for(src), {"cli/commands/foo.py": src})
    assert len(findings) == 1
    assert findings[0].severity == BLOCKING
    assert findings[0].line == 5
    assert "--scope" in findings[0].message


def test_scope_flags_rejects_new_remote_and_local_aliases():
    """`--remote`/`--local` are FROZEN aliases on the two commands that already
    have them — a NEW command may not add one."""
    for flag in ("--remote", "--local"):
        src = _cli_source(f'    x: bool = typer.Option(False, "{flag}"),')
        assert len(check_scope_flags(_added_for(src), {"cli/commands/foo.py": src})) == 1, flag


def test_scope_flags_catches_the_multi_line_typer_style():
    """The repo's dominant style wraps typer.Option across lines, so the `bool`
    annotation and the flag literal land on DIFFERENT lines (see
    cli/commands/search.py:27, cli/commands/snapshot.py:199). A line-by-line
    substring match passes these silently — the exact shape that must block."""
    src = _cli_source(
        "    local: bool = typer.Option(\n"
        "        False,\n"
        '        "--local",\n'
        '        help="Shorthand for --scope local",\n'
        "    ),"
    )
    findings = check_scope_flags(_added_for(src), {"cli/commands/foo.py": src})
    assert len(findings) == 1
    assert findings[0].severity == BLOCKING
    # cites the parameter, not the buried flag literal
    assert findings[0].line == 5


def test_scope_flags_catches_flag_on_a_continuation_line():
    src = _cli_source('    expired: bool = typer.Option(\n        False, "--server",\n    ),')
    assert len(check_scope_flags(_added_for(src), {"cli/commands/foo.py": src})) == 1


def test_scope_flags_allows_the_scope_option_itself():
    src = _cli_source('    scope: str = typer.Option("auto", "--scope", help="auto|local"),')
    assert check_scope_flags(_added_for(src), {"cli/commands/foo.py": src}) == []


def test_scope_flags_allows_a_non_boolean_option_named_remote():
    """The rule bans a boolean *scope* flag, not the substring 'remote'."""
    src = _cli_source(
        "\n".join(
            [
                '    remote_url: str = typer.Option(None, "--remote-url"),',
                '    limit: int = typer.Option(10, "--limit"),',
            ]
        )
    )
    assert check_scope_flags(_added_for(src), {"cli/commands/foo.py": src}) == []


def test_scope_flags_handles_optional_bool_annotations():
    src = _cli_source('    remote: bool | None = typer.Option(None, "--remote"),')
    assert len(check_scope_flags(_added_for(src), {"cli/commands/foo.py": src})) == 1


def test_scope_flags_only_inspects_cli_command_modules():
    src = _cli_source('    remote: bool = typer.Option(False, "--remote"),')
    assert check_scope_flags(_added_for(src, "app/api/foo.py"), {"app/api/foo.py": src}) == []


def test_scope_flags_ignores_untouched_parameters():
    """Diff-scoped: an existing frozen alias must not be re-reported just
    because some other line in the file changed."""
    src = _cli_source('    remote: bool = typer.Option(False, "--remote"),')
    added = {"cli/commands/foo.py": [(7, "    pass")]}
    assert check_scope_flags(added, {"cli/commands/foo.py": src}) == []


def test_scope_flags_ignores_unparseable_sources():
    added = {"cli/commands/foo.py": [(1, "x")]}
    assert check_scope_flags(added, {"cli/commands/foo.py": "def (:\n"}) == []


def test_scope_flags_live_cli_commands_are_clean():
    """Frozen aliases already in cli/commands/ must not trip the guard when
    their lines are not part of the diff."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for name in ("search.py", "snapshot.py", "query.py", "explore.py"):
        path = root / "cli" / "commands" / name
        if not path.is_file():
            continue
        rel = f"cli/commands/{name}"
        assert check_scope_flags({rel: [(1, "import typer")]}, {rel: path.read_text()}) == [], name


# ---------------------------------------------------------------------------
# query_mode='remote' ⇒ _remote_attach  (sync-map: "CI guard? NO")
# ---------------------------------------------------------------------------


def test_remote_attach_flags_remote_query_mode_without_the_table(tmp_path):
    conn = tmp_path / "connectors" / "acme"
    conn.mkdir(parents=True)
    (conn / "extractor.py").write_text("query_mode = 'remote'\n")
    added = {"connectors/acme/extractor.py": [(4, "    query_mode = 'remote'")]}
    findings = check_remote_attach(added, repo_root=tmp_path)
    assert len(findings) == 1
    assert findings[0].severity == BLOCKING
    assert "_remote_attach" in findings[0].message


def test_remote_attach_satisfied_by_a_sibling_module_in_the_connector(tmp_path):
    conn = tmp_path / "connectors" / "acme"
    conn.mkdir(parents=True)
    (conn / "extractor.py").write_text("query_mode = 'remote'\n")
    (conn / "extract_init.py").write_text("CREATE TABLE _remote_attach (...)\n")
    added = {"connectors/acme/extractor.py": [(4, "    query_mode = 'remote'")]}
    assert check_remote_attach(added, repo_root=tmp_path) == []


def test_remote_attach_ignores_local_query_modes(tmp_path):
    conn = tmp_path / "connectors" / "acme"
    conn.mkdir(parents=True)
    (conn / "extractor.py").write_text("query_mode = 'local'\n")
    added = {"connectors/acme/extractor.py": [(4, "    query_mode = 'local'")]}
    assert check_remote_attach(added, repo_root=tmp_path) == []


def test_remote_attach_ignores_files_outside_connectors(tmp_path):
    added = {"src/orchestrator.py": [(4, "query_mode = 'remote'")]}
    assert check_remote_attach(added, repo_root=tmp_path) == []


# ---------------------------------------------------------------------------
# entity-scoped endpoint authz  (WARN — complements test_route_auth_guard.py,
# which proves a route has *some* auth but cannot judge authz depth)
# ---------------------------------------------------------------------------

_AUTHN_ONLY = textwrap.dedent(
    """\
    @router.get("/{slug}")
    async def get_thing(slug: str, user: dict = Depends(get_current_user)):
        return {"slug": slug}
    """
)


def test_entity_scoped_authz_warns_on_authn_only_parameterised_route():
    added = {"app/api/things.py": [(1, '@router.get("/{slug}")')]}
    findings = check_entity_scoped_authz(added, {"app/api/things.py": _AUTHN_ONLY})
    assert len(findings) == 1
    assert findings[0].severity == WARN
    assert "require_resource_access" in findings[0].message


def test_entity_scoped_authz_quiet_when_require_resource_access_present():
    source = _AUTHN_ONLY.replace(
        "Depends(get_current_user)",
        'Depends(require_resource_access(ResourceType.THING, "{slug}"))',
    )
    added = {"app/api/things.py": [(1, '@router.get("/{slug}")')]}
    assert check_entity_scoped_authz(added, {"app/api/things.py": source}) == []


def test_entity_scoped_authz_quiet_when_require_admin_present():
    source = _AUTHN_ONLY.replace("Depends(get_current_user)", "Depends(require_admin)")
    added = {"app/api/things.py": [(1, '@router.get("/{slug}")')]}
    assert check_entity_scoped_authz(added, {"app/api/things.py": source}) == []


def test_entity_scoped_authz_quiet_on_collection_routes_without_a_path_param():
    source = textwrap.dedent(
        """\
        @router.get("")
        async def list_things(user: dict = Depends(get_current_user)):
            return []
        """
    )
    added = {"app/api/things.py": [(1, '@router.get("")')]}
    assert check_entity_scoped_authz(added, {"app/api/things.py": source}) == []


def test_entity_scoped_authz_quiet_when_body_calls_an_authorization_helper():
    """Several handlers authorize inside the body rather than via Depends —
    that is a legitimate shape and must not produce noise."""
    source = textwrap.dedent(
        """\
        @router.get("/{slug}")
        async def get_thing(slug: str, user: dict = Depends(get_current_user)):
            require_resource_access(ResourceType.THING, slug)
            return {}
        """
    )
    added = {"app/api/things.py": [(1, '@router.get("/{slug}")')]}
    assert check_entity_scoped_authz(added, {"app/api/things.py": source}) == []


def test_entity_scoped_authz_ignores_unparseable_sources():
    added = {"app/api/things.py": [(1, '@router.get("/{slug}")')]}
    assert check_entity_scoped_authz(added, {"app/api/things.py": "def (:\n"}) == []


# ---------------------------------------------------------------------------
# meta: the live repo satisfies the full-sweep invariants
# ---------------------------------------------------------------------------


def test_live_resource_types_module_is_clean():
    """Full sweep (not diff-scoped): every ResourceType member is registered."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "resource_types.py").read_text()
    findings = check_resource_type_registry(src)
    assert findings == [], [f.message for f in findings]


@pytest.mark.parametrize("module", ["scripts.verify_syncmap"])
def test_script_is_importable_without_optional_deps(module):
    """The verifier runs in the edit loop before any venv work — it must import
    with nothing but the stdlib."""
    import importlib
    import sys

    mod = importlib.import_module(module)
    third_party = {name.split(".")[0] for name in getattr(mod, "__annotations__", {})}
    assert "typer" not in third_party
    assert sys.modules[module] is mod
