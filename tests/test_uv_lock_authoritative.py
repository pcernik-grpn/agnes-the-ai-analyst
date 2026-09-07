"""``uv.lock`` is what ships — the pins that keep it that way.

Before this, the lock was committed but nothing read it: the image and every
CI job re-resolved ``pyproject.toml``'s ranges at build time, the daily release
cut bumped the version without re-locking, and any ``uv run``/``uv sync`` in a
worktree rewrote the stale lock — the dirty ``uv.lock`` on nearly every PR.
Now the image and CI install from the lock through one script, CI blocks a
lock that is behind pyproject, the cut re-locks, and the hooks run ``--frozen``.
Each pin below names the regression it stops.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci" / "install-from-lock.sh"
DOCKERFILE = ROOT / "Dockerfile"
CI = ROOT / ".github" / "workflows" / "ci.yml"
DAILY_CUT = ROOT / ".github" / "workflows" / "daily-cut.yml"
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
HOOKS = [ROOT / "scripts" / "post-edit-quality.sh", ROOT / "scripts" / "typecheck-core.sh"]


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class TestOneInstallPath:
    def test_the_script_installs_from_the_lock_without_touching_it(self):
        s = _read(SCRIPT)
        assert "uv export --frozen --no-emit-project" in s, (
            "--frozen: never rewrite the lock; --no-emit-project: the project goes on top"
        )
        assert 'uv pip install $TARGET --no-cache -r "$REQ"' in s
        assert "uv pip install $TARGET --no-cache --no-deps ." in s, (
            "the project itself, with its deps already pinned-installed"
        )
        assert 'TARGET="--system"' in s and 'TARGET="--python $INSTALL_PYTHON"' in s, (
            "system interpreter by default; INSTALL_PYTHON targets an explicit venv (the e2e jobs)"
        )
        assert os.access(SCRIPT, os.X_OK), "the Dockerfile and workflows exec it directly"

    def test_the_image_installs_through_the_script_not_the_ranges(self):
        d = _read(DOCKERFILE)
        assert "scripts/ci/install-from-lock.sh --no-dev server slack-socket telegram extraction" in d
        assert 'uv pip install --system --no-cache ".[' not in d, "range-resolving install is back in the image"
        assert "$(printf '%s' \"$EXTRA_EXTRAS\" | tr ',' ' ')" in d, (
            "the rich-image ARG must still splice in (image-rich.yml passes ',docling,embeddings')"
        )

    def test_no_workflow_resolves_from_the_ranges(self):
        # Any install of the PROJECT from its ranges — `uv pip install --system
        # ".[…]"`, `pip install -e ".[…]"`, `pip install .` — not tool installs
        # like `pip install ruff` (the advisory lint job's, not a dependency).
        project_install = re.compile(r'pip install(?: [^\n"]*)? (?:-e )?"?\.(?:\[|"|\s|$)', re.MULTILINE)
        offenders = [w.name for w in WORKFLOWS if project_install.search(_read(w))]
        assert offenders == [], (
            f"workflows re-resolving pyproject ranges instead of installing from the lock: {offenders}"
        )
        users = [w.name for w in WORKFLOWS if "scripts/ci/install-from-lock.sh" in _read(w)]
        assert {"ci.yml", "keboola-deploy.yml", "update-test-durations.yml", "e2e-docker.yml"} <= set(users), users

    def test_the_e2e_venv_jobs_install_from_the_lock_too(self):
        """They run pytest from a Python 3.11 venv; without INSTALL_PYTHON they
        fell back to `pip install -e ".[dev,server]"` and re-resolved the
        ranges (Devin Review on the lock PR)."""
        e2e = _read(ROOT / ".github" / "workflows" / "e2e-docker.yml")
        assert e2e.count("INSTALL_PYTHON=.venv/bin/python scripts/ci/install-from-lock.sh dev server") == 2
        assert "pip install -e" not in e2e


class TestTheLockCannotFallBehind:
    def test_ci_blocks_on_uv_lock_check(self):
        ci = _read(CI)
        assert "\n  lock-check:\n" in ci
        assert "run: uv lock --check" in ci
        rollup = ci[ci.index("\n  test:\n") : ci.index("\n  lint:\n")]
        assert "lock-check" in rollup.split("needs:")[1].split("\n")[0], (
            "the `test` rollup is the required check — lock-check must gate through it"
        )
        assert "needs.lock-check.result" in rollup

    def test_the_daily_cut_relocks_and_ships_the_lock(self):
        dc = _read(DAILY_CUT)
        assert "run: uv lock\n" in dc, (
            "the cut bumps pyproject's version; without a re-lock its own PR fails lock-check"
        )
        assert "git add uv.lock" in dc
        assert dc.index("run: uv lock\n") < dc.index("git add CHANGELOG.md"), "re-lock before the commit"

    def test_hooks_never_rewrite_the_lock(self):
        """Every non-comment `uv run` in a hook carries --frozen, whatever the
        flag order — `uv run --quiet --frozen ruff` is as good as
        `uv run --frozen --quiet ruff`."""
        for h in HOOKS:
            invocations = [ln for ln in _read(h).splitlines() if "uv run" in ln and not ln.lstrip().startswith("#")]
            assert invocations, f"{h.name}: the uv fallback is gone — was it meant to be?"
            unfrozen = [ln.strip() for ln in invocations if "--frozen" not in ln]
            assert unfrozen == [], f"{h.name}: an unfrozen `uv run` re-locks a stale lock: {unfrozen}"

    def test_the_build_backend_is_pinned_exactly(self):
        """uv.lock does not cover `[build-system] requires`; the project's own
        build (`uv build`, `uv pip install .`) fetches the backend into an
        isolated env at build time. A floating `hatchling` was the one input
        the lock left unpinned (Copilot review on the lock PR)."""
        block = re.search(r"\[build-system\]\n(.*?)\n\n", _read(ROOT / "pyproject.toml"), re.DOTALL).group(1)
        m = re.search(r'requires = \["hatchling==(\d+\.\d+\.\d+)"\]', block)
        assert m, f"[build-system] requires must pin hatchling exactly, got:\n{block}"

    def test_the_lock_records_the_current_project_version(self):
        """The class of drift the cut used to leave behind (0.95.0 in the lock,
        0.101.0 in pyproject) — checkable without uv."""
        version = re.search(r'^version\s*=\s*"([^"]+)"', _read(ROOT / "pyproject.toml"), re.MULTILINE).group(1)
        lock = _read(ROOT / "uv.lock")
        m = re.search(r'\[\[package\]\]\nname = "agnes-the-ai-analyst"\nversion = "([^"]+)"', lock)
        assert m, "the project entry is missing from uv.lock"
        assert m.group(1) == version, f"uv.lock records {m.group(1)}, pyproject.toml says {version} — run `uv lock`"

    def test_the_lock_is_in_sync_with_pyproject(self):
        """The real check, when uv is available (CI's test jobs have it)."""
        uv = shutil.which("uv")
        if not uv:
            pytest.skip("uv not on PATH")
        out = subprocess.run([uv, "lock", "--check"], cwd=ROOT, capture_output=True, text=True, check=False)
        assert out.returncode == 0, f"uv.lock is behind pyproject.toml — run `uv lock` and commit it:\n{out.stderr}"
