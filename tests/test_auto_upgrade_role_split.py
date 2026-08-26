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
