"""Pytest wrapper for the role-split rolling-recreate bash harness.

The actual test logic lives in ``tests/test_auto_upgrade_role_split.sh``
(same pattern as ``tests/test_db_backup_pg_canary.sh`` /
``tests/test_state_applier_host_script.sh``): it fakes `docker`, `curl`,
`logger`, and `flock` on PATH, sandboxes the paths
``scripts/ops/agnes-auto-upgrade.sh`` reads/writes, and drives the
scenarios listed in that file's header (single-container one-shot,
role-split rolling recreates healthy/aborting, the sync-defer probes and
their fail-open path, the every-tick docker GC, and the host-artifact
refresh + self-update from the pinned image's ``/opt/agnes-host/``)
asserting the exact `docker`/`curl` command lines the topology
detection + rolling-recreate + defer logic produces. This wrapper just
makes it part of the ``pytest tests/`` run so CI enforces it automatically
instead of requiring a manual invocation.

Like ``tests/test_db_backup_pg_canary.py``, this needs a bash >=4
interpreter on PATH — not because ``agnes-auto-upgrade.sh`` itself uses a
bash-4-only construct (it doesn't), but for consistency with the sibling
host-script harnesses in this wave, which do (``${STAGE^^}`` in
``agnes-db-backup.sh`` / ``agnes-watchdog.sh``). Skips rather than fails
when no bash >=4 is discoverable, so an unpatched macOS toolchain doesn't
block a local run; install one (e.g. ``brew install bash``) to exercise it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path("tests/test_auto_upgrade_role_split.sh")

# Host-side artifacts the VM runs: the ops scripts plus the boot startup
# template that seeds them. Every one of these is refreshed from the pinned
# image's /opt/agnes-host/, never from the source repo over the network.
HOST_SCRIPTS = sorted(Path("scripts/ops").glob("*.sh")) + [
    Path("infra/modules/customer-instance/startup-script.sh.tpl")
]


def _find_bash4() -> str | None:
    candidates = []
    found = shutil.which("bash")
    if found:
        candidates.append(found)
    for extra in ("/opt/homebrew/bin/bash", "/usr/local/bin/bash", "/bin/bash"):
        if extra not in candidates and Path(extra).exists():
            candidates.append(extra)
    for candidate in candidates:
        try:
            out = subprocess.run(
                [candidate, "-c", "echo ${BASH_VERSINFO[0]}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if out.returncode == 0 and out.stdout.strip().isdigit() and int(out.stdout.strip()) >= 4:
            return candidate
    return None


def test_role_split_rolling_recreate_and_data_refresh_defer():
    bash4 = _find_bash4()
    if bash4 is None:
        pytest.skip(
            "no bash >=4 found on PATH — install one (e.g. `brew install bash` "
            "on macOS) to run this harness locally. CI runners ship bash >=4 "
            "by default."
        )

    proc = subprocess.run(
        [bash4, str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"role-split rolling-recreate harness failed (bash={bash4}):\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    assert "OK" in proc.stdout.splitlines()[-1]


def test_host_scripts_never_fetch_the_source_repo():
    """No host-side script may pull an artifact off the source repo's main branch.

    The static half of scenarios I/K in the harness above, and worth having
    separately for two reasons. It runs everywhere — the harness *skips*
    without a bash >=4 interpreter, so on such a toolchain nothing else
    would notice a raw-main fetch creeping back. And it is broad: the
    harness only exercises the code paths its scenarios reach, whereas this
    covers every host script in one sweep, including branches gated behind
    an env var no scenario happens to set.

    That gap is exactly how the bug this guards against shipped. The change
    that moved host artifacts onto the pinned image deleted the ``RAW_BASE``
    definition but left one ``$RAW_BASE`` fetch behind, inside the block
    gated on ``APPS_SUBDOMAIN_BASE``. Under ``set -u`` the stale reference
    was not merely a leftover fetch: expanding it aborted the entire tick
    on every VM with data-app subdomains configured — before the recreate,
    and before the self-update that would have delivered the repair.

    Keeping this as a substring scan rather than a ``RAW_BASE``-name check
    is deliberate: the hazard is fetching host config from a moving branch
    (which breaks the image tag's role as the single version pin, and needs
    egress to a repo that may be private), not any particular variable name
    someone might spell it with next time.
    """
    # A glob resolved against the wrong cwd yields nothing, and the sweep
    # below would then pass while checking zero files. Pin the corpus first
    # so this can only go green by actually reading the scripts.
    missing = [str(p) for p in HOST_SCRIPTS if not p.is_file()]
    assert not missing, f"host scripts not found (wrong working directory?): {missing}"
    assert len(HOST_SCRIPTS) >= 5, (
        f"expected the ops-script corpus to be several files, found {len(HOST_SCRIPTS)} — "
        "a shrunken glob would make this sweep vacuous"
    )

    offenders: list[str] = []
    for script in HOST_SCRIPTS:
        text = script.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue  # prose about the retired fetch is fine
            if "raw.githubusercontent.com" in line or "RAW_BASE" in line:
                offenders.append(f"{script}:{line_number}: {line.strip()}")

    assert not offenders, (
        "host-side scripts must source every artifact from the pinned image's "
        "/opt/agnes-host/ (via `docker create` + `docker cp`), never over the "
        "network from the source repo's main branch:\n  " + "\n  ".join(offenders)
    )
