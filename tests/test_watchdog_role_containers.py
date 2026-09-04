"""Pytest wrapper for the watchdog role-container-fleet bash harness.

The actual test logic lives in ``tests/test_watchdog_role_containers.sh``
(same pattern as ``tests/test_auto_upgrade_role_split.sh`` /
``tests/test_db_backup_pg_canary.sh``): it fakes `docker` and `logger` on
PATH, sandboxes the paths ``agnes-watchdog.sh`` reads/writes, and drives
eight scenarios (single-container topology, a role-split fleet scanned and
named container-by-container with sidecars filtered out, the
coordination-backend-unreachable signature firing past its low-noise
threshold while a single blip does not, that same signature staying
silent when redis isn't configured, the legacy-name fallback when
`docker compose ps` yields nothing, the same signature armed from .env
instead of instance.yaml, and the two shapes of a failed one-shot job —
blocking a strict boot into an empty fleet, and failing behind a fleet
that still looks healthy) asserting the exact `docker` command
lines and alert text the container-enumeration + per-container signature
scan produces. This wrapper just makes it part of the ``pytest tests/``
run so CI enforces it automatically instead of requiring a manual
invocation.

Like ``tests/test_auto_upgrade_role_split.py`` / ``tests/test_db_backup_pg_canary.py``,
this needs a bash >=4 interpreter on PATH (the watchdog script itself uses
``${STAGE^^}``, a bash-4+ construct). Skips rather than fails when no
bash >=4 is discoverable, so an unpatched macOS toolchain doesn't block a
local run; install one (e.g. ``brew install bash``) to exercise it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path("tests/test_watchdog_role_containers.sh")


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


def test_watchdog_scans_every_role_container_and_gates_coordination_alert():
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
        f"watchdog role-container harness failed (bash={bash4}):\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    assert "OK" in proc.stdout.splitlines()[-1]
