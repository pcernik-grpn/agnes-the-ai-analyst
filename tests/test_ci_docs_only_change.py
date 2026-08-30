"""Tests for ``scripts/ci/docs_only_change.py``.

The removed `.github/workflows/release.yml` `paths-ignore:` list is
replicated as the ``_is_ignored_path`` / ``decide`` logic; these tests pin
the exact depth semantics (root-level ``*.md`` vs. any-depth ``docs/**``)
alongside the branch-create, force-push, and empty-diff edge cases the
`create:` trigger used to paper over.

Every test builds a throwaway git repository under ``tmp_path`` with real
commits and calls :func:`scripts.ci.docs_only_change.decide` directly —
no subprocess re-invocation of the script and no app/create_app imports.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.ci.docs_only_change import ZERO_SHA, decide, main


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def _init_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(repo_dir, "init", "-q")
    _git(repo_dir, "config", "user.email", "ci@example.com")
    _git(repo_dir, "config", "user.name", "CI Test")


def _commit(repo_dir: Path, paths: dict[str, str], message: str) -> str:
    for relative_path, content in paths.items():
        full_path = repo_dir / relative_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content)
        _git(repo_dir, "add", relative_path)
    _git(repo_dir, "commit", "-q", "-m", message)
    return _git(repo_dir, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)
    return repo_dir


def test_zero_before_sha_always_builds(repo: Path) -> None:
    head = _commit(repo, {"app/main.py": "print('hi')\n"}, "init")
    build, reason = decide(repo, ZERO_SHA, head)
    assert build is True
    assert "zero" in reason


def test_empty_before_sha_always_builds(repo: Path) -> None:
    """`workflow_dispatch` has no `before` SHA at all (`github.event.before`
    resolves to an empty string) — treat it the same as a zero-SHA push."""
    head = _commit(repo, {"app/main.py": "print('hi')\n"}, "init")
    build, reason = decide(repo, "", head)
    assert build is True
    assert "empty" in reason or "zero" in reason


def test_unknown_before_sha_always_builds(repo: Path) -> None:
    head = _commit(repo, {"app/main.py": "print('hi')\n"}, "init")
    unknown_sha = "f" * 40
    build, reason = decide(repo, unknown_sha, head)
    assert build is True
    assert "unknown" in reason


def test_empty_diff_always_builds(repo: Path) -> None:
    head = _commit(repo, {"app/main.py": "print('hi')\n"}, "init")
    build, reason = decide(repo, head, head)
    assert build is True
    assert "empty diff" in reason


def test_docs_only_change_under_root_docs_dir_skips_build(repo: Path) -> None:
    before = _commit(repo, {"app/main.py": "print('hi')\n"}, "init")
    head = _commit(
        repo,
        {"docs/architecture.md": "# Architecture\n", "docs/nested/deep.md": "deep\n"},
        "docs update",
    )
    build, reason = decide(repo, before, head)
    assert build is False
    assert "paths-ignore" in reason


def test_root_level_markdown_change_skips_build(repo: Path) -> None:
    before = _commit(repo, {"app/main.py": "print('hi')\n"}, "init")
    head = _commit(repo, {"README.md": "# Readme\n"}, "readme update")
    build, _reason = decide(repo, before, head)
    assert build is False


def test_nested_markdown_outside_docs_dir_still_builds(repo: Path) -> None:
    """`*.md` in the original `paths-ignore` matched root-level files only —
    GitHub's cheat sheet is explicit that a slash-free pattern like `*.jsx?`
    does not reach into subdirectories (that requires `**/*.jsx?`). A nested
    `.md` file living outside `docs/` (e.g. under `.claude/skills/**`) is
    therefore NOT docs-only under the replicated semantics."""
    before = _commit(repo, {"app/main.py": "print('hi')\n"}, "init")
    head = _commit(repo, {".claude/skills/foo/SKILL.md": "notes\n"}, "nested md")
    build, reason = decide(repo, before, head)
    assert build is True
    assert "non-ignored path changed" in reason


def test_license_only_change_skips_build(repo: Path) -> None:
    before = _commit(repo, {"app/main.py": "print('hi')\n"}, "init")
    head = _commit(repo, {"LICENSE": "MIT\n"}, "license update")
    build, _reason = decide(repo, before, head)
    assert build is False


def test_mixed_docs_and_code_change_builds(repo: Path) -> None:
    before = _commit(repo, {"app/main.py": "print('hi')\n"}, "init")
    head = _commit(
        repo,
        {"docs/architecture.md": "# Architecture\n", "app/main.py": "print('changed')\n"},
        "docs plus code",
    )
    build, reason = decide(repo, before, head)
    assert build is True
    assert "app/main.py" in reason


def test_main_prints_expected_two_lines(
    repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _commit(repo, {"app/main.py": "print('hi')\n"}, "init")
    head = _commit(repo, {"README.md": "# Readme\n"}, "readme update")
    monkeypatch.chdir(repo)

    exit_code = main([before, head])

    assert exit_code == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "build=false"
    assert lines[1].startswith("# reason:")


def test_main_requires_exactly_two_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["only-one-arg"])
    assert exit_code == 2
