"""The exhaustiveness gate: `assert_never` has to be enforced, not decorative.

`typing.assert_never` turns "someone added an enum member and missed a
dispatch arm" from a production surprise into a type error — but only where
a type checker actually blocks. In this repo mypy was advisory everywhere
(`|| true` + `continue-on-error` in ci.yml, and not installed at all
locally, so the post-edit hook skipped it silently), which means an
`assert_never` would have raised only if the missing arm was reached at
runtime on a customer's stack. That is a tripwire, not a guarantee.

These tests are that guarantee. They ride the test suite so the check lands
on the already-required `test` job:

1. the typed core is clean, so a dropped arm fails CI;
2. the mechanism genuinely bites — proven by mutating a real dispatch and
   observing mypy reject it, so a mypy upgrade or a config change that
   quietly disables the check cannot pass unnoticed.

Point 2 matters more than it looks: a gate that has stopped detecting
anything looks exactly like a gate with nothing to report.
"""

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE_SCRIPT = REPO_ROOT / "scripts" / "typecheck-core.sh"


def _mypy_command() -> list[str] | None:
    """Resolve mypy the same way the gate script does."""
    venv_mypy = REPO_ROOT / ".venv" / "bin" / "mypy"
    if venv_mypy.is_file() and os.access(venv_mypy, os.X_OK):
        return [str(venv_mypy)]
    found = shutil.which("mypy")
    if found:
        return [found]
    return None


def _require_mypy() -> list[str]:
    """mypy is declared in the [dev] extra, so CI must never skip these.

    Locally a partial env is tolerated; in CI a missing mypy means the gate
    silently stopped gating, which is the failure mode these tests exist to
    prevent.
    """
    command = _mypy_command()
    if command:
        return command
    if os.environ.get("CI"):
        pytest.fail("mypy is missing in CI — the exhaustiveness gate cannot run. It is declared in the [dev] extra.")
    pytest.skip("mypy not installed locally; install the dev extra to run the exhaustiveness gate")


def test_gate_script_is_executable():
    assert GATE_SCRIPT.is_file(), f"missing {GATE_SCRIPT}"
    assert os.access(GATE_SCRIPT, os.X_OK), f"{GATE_SCRIPT} is not executable (chmod +x)"


def test_typed_core_is_not_empty_and_lists_real_files():
    listing = subprocess.run(
        ["bash", str(GATE_SCRIPT), "--list"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
    )
    modules = [line.strip() for line in listing.stdout.splitlines() if line.strip()]

    assert modules, "the typed core is empty — nothing is being enforced"
    for module in modules:
        assert (REPO_ROOT / module).is_file(), f"typed-core entry does not exist: {module}"


def test_modules_using_assert_never_are_in_the_typed_core():
    """An `assert_never` outside the typed core enforces nothing.

    Without this, adding exhaustive dispatch to a module nobody type-checks
    reads as a guarantee while behaving like a comment.
    """
    listing = subprocess.run(
        ["bash", str(GATE_SCRIPT), "--list"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
    )
    core = {line.strip() for line in listing.stdout.splitlines() if line.strip()}

    users = subprocess.run(
        ["git", "grep", "-l", "assert_never", "--", "src", "app", "cli", "connectors", "services"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    # `git grep` exits 1 when there are no matches; that is not an error here.
    unguarded = sorted({p.strip() for p in users.stdout.splitlines() if p.strip()} - core)

    assert not unguarded, (
        "these modules use assert_never but are not in the typed core, so the "
        f"exhaustiveness check does not block for them: {unguarded}. "
        "Add them to CORE_MODULES in scripts/typecheck-core.sh."
    )


def test_typed_core_type_checks_clean():
    """The gate itself: a dropped dispatch arm fails here."""
    _require_mypy()
    result = subprocess.run(
        ["bash", str(GATE_SCRIPT)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, f"typed core is not mypy-clean:\n{result.stdout}\n{result.stderr}"


def test_a_missing_dispatch_arm_is_actually_rejected(tmp_path):
    """Prove the mechanism bites, so it cannot rot into a no-op.

    Uses a self-contained snippet rather than the real module: this asserts
    that *this* mypy, with the flags the gate passes, still reports a missing
    enum arm at `assert_never`.
    """
    mypy = _require_mypy()

    snippet = tmp_path / "missing_arm.py"
    snippet.write_text(
        textwrap.dedent('''
        """Deliberately non-exhaustive dispatch — mypy must reject this."""
        from enum import StrEnum
        from typing import assert_never


        class Colour(StrEnum):
            RED = "red"
            GREEN = "green"
            BLUE = "blue"


        def describe(c: Colour) -> str:
            if c is Colour.RED:
                return "warm"
            elif c is Colour.GREEN:
                return "cool"
            else:
                assert_never(c)
        ''').lstrip()
    )

    result = subprocess.run(
        [*mypy, str(snippet), "--ignore-missing-imports"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    assert result.returncode != 0, (
        "mypy accepted a dispatch that omits an enum member — the exhaustiveness "
        f"check is no longer enforcing anything.\n{result.stdout}"
    )
    assert "assert_never" in result.stdout, f"unexpected mypy output:\n{result.stdout}"
    assert "Colour.BLUE" in result.stdout, (
        f"mypy flagged the file but did not name the missing member:\n{result.stdout}"
    )


def test_a_complete_dispatch_is_accepted(tmp_path):
    """The other half: the gate must not reject correct code.

    A check that fails on everything would pass the test above while making
    the codebase unworkable.
    """
    mypy = _require_mypy()

    snippet = tmp_path / "complete_arms.py"
    snippet.write_text(
        textwrap.dedent('''
        """Exhaustive dispatch — mypy must accept this."""
        from enum import StrEnum
        from typing import assert_never


        class Colour(StrEnum):
            RED = "red"
            GREEN = "green"


        def describe(c: Colour) -> str:
            if c is Colour.RED:
                return "warm"
            elif c is Colour.GREEN:
                return "cool"
            else:
                assert_never(c)
        ''').lstrip()
    )

    result = subprocess.run(
        [*mypy, str(snippet), "--ignore-missing-imports"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, f"mypy rejected an exhaustive dispatch:\n{result.stdout}"
