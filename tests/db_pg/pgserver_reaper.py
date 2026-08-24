"""Reaper for orphaned ``agnes-pgserver-*`` Postgres data directories.

``_start_pgserver`` in ``tests/db_pg/conftest.py`` creates its data dir via
``tempfile.mkdtemp(prefix="agnes-pgserver-")`` and removes it in a ``finally``
block at session end. A hard-killed run (SIGKILL, OOM kill, crash after the
disk fills up) never reaches that ``finally``, leaving a ~300 MB data dir
behind — plus a still-running detached postmaster that nothing else will
ever stop (#1362).

The fixture writes an owner sentinel (``agnes-owner.pid`` — the pytest PID)
into the data dir at creation, which is what lets the reaper act on live
orphans: owner alive → a genuine concurrent session, never touch; owner dead
but postmaster alive → orphan, stop it (identity-checked first) and reap.

Deliberately conservative — a skipped dir costs ~300 MB of disk until the
next run; a wrongly reaped dir kills a concurrent worktree session's live
Postgres:

- only ``agnes-pgserver-*`` dirs directly under the given temp root;
- only dirs older than ``min_age_seconds`` — a fresh dir may belong to a
  concurrent session still running initdb, before ``postmaster.pid`` exists;
- a dir whose ``postmaster.pid`` names a live PID is only touched when the
  owner sentinel names a DEAD owner **and** the live PID verifiably is a
  postgres postmaster running on that very data dir (guards PID reuse);
  pre-sentinel dirs therefore keep the old skip behavior;
- an unreadable or unparsable ``postmaster.pid`` keeps the dir (never guess).

Manual sweep: ``python -m tests.db_pg.pgserver_reaper`` (``--force`` ignores
the minimum-age guard).
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

MIN_AGE_SECONDS = 3600
OWNER_SENTINEL = "agnes-owner.pid"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # PermissionError etc. — the process exists, just not ours
    return True


def _postmaster_pid(pgdata: Path) -> int | None:
    """PID from the first line of ``postmaster.pid``, or None if unreadable."""
    try:
        return int((pgdata / "postmaster.pid").read_text().splitlines()[0].strip())
    except (OSError, ValueError, IndexError):
        return None


def _owner_pid(pgdata: Path) -> int | None:
    """PID from the ``agnes-owner.pid`` sentinel, or None if absent/unreadable."""
    try:
        return int((pgdata / OWNER_SENTINEL).read_text().strip())
    except (OSError, ValueError):
        return None


def _cmdline_matches_pgdata(cmdline: list[str], pgdata: Path) -> bool:
    """Does this cmdline look like a postgres postmaster running on ``pgdata``?

    Paths are compared resolved, not as substrings: on macOS the fixture
    creates the dir under ``/var/folders/…`` (what ``tempfile.gettempdir()``
    yields) while the postmaster's ``-D`` argument carries the resolved
    ``/private/var/folders/…`` — a literal comparison would silently never
    match a real orphan on the very platform the leak was observed on
    (Devin Review on #1367). A plain substring check remains as fallback.
    """
    if not cmdline or "postgres" not in os.path.basename(cmdline[0]):
        return False
    try:
        target = pgdata.resolve()
    except OSError:
        return False
    for part in cmdline[1:]:
        if not part or not part.startswith(("/", ".")):
            continue  # flags and empty args, not paths
        try:
            if Path(part).resolve() == target:
                return True
        except OSError:
            continue
    return any(str(pgdata) in part for part in cmdline[1:])


def _is_postmaster_for(pid: int, pgdata: Path) -> bool:
    """True only if ``pid`` is verifiably a postgres postmaster on ``pgdata``.

    Guards against PID reuse: ``postmaster.pid`` may name a PID that now
    belongs to an unrelated process. Any doubt (psutil missing, process gone,
    access denied, foreign cmdline) answers False — the caller then leaves
    the dir alone, which is the cheap direction.
    """
    try:
        import psutil  # type: ignore[import-untyped]  # transitive via pixeltable-pgserver

        cmdline = psutil.Process(pid).cmdline()
    except Exception:
        return False
    return _cmdline_matches_pgdata(cmdline, pgdata)


def _stop_orphan_postmaster(pid: int, *, timeout: float = 10.0) -> bool:
    """SIGTERM (postgres smart shutdown) and wait; True once the process is gone.

    psutil rather than a bare ``os.kill(pid, 0)`` poll: ``wait()`` also reaps
    the process if it happens to be our child (where a plain kill-0 loop would
    see the zombie as alive forever). Any failure — already gone counts as
    success; access denied or still alive at the deadline counts as not
    stopped — leaves the caller to retry next session.
    """
    try:
        import psutil  # type: ignore[import-untyped]  # transitive via pixeltable-pgserver
    except ImportError:
        return False
    try:
        proc = psutil.Process(pid)
        proc.terminate()
        proc.wait(timeout)
        return True
    except psutil.NoSuchProcess:
        return True
    except Exception:
        return False


def reap_orphaned_pgserver_dirs(tmp_root: Path, *, min_age_seconds: int = MIN_AGE_SECONDS) -> list[Path]:
    """Remove orphaned pgserver data dirs under ``tmp_root``; return removed paths."""
    removed: list[Path] = []
    now = time.time()
    for d in tmp_root.glob("agnes-pgserver-*"):
        try:
            if not d.is_dir():
                continue
            owner = _owner_pid(d)
            # A sentinel naming a DEAD owner is positive proof of an orphan.
            owner_dead = owner is not None and not _pid_alive(owner)
            # The minimum-age guard exists only for dirs we cannot positively
            # classify: one still mid-initdb (sentinel not yet written) or a
            # pre-sentinel dir from an older release. Applying it to a
            # provably-dead owner as well is what let orphans pile up — a
            # local `-n auto` db_pg run creates one dir PER WORKER and
            # abandons them all on the next hard kill, far faster than they
            # age out, so a measured 10 dirs / 4.1 GB all sat under the 1 h
            # threshold at once. Age is now consulted only when ownership is
            # inconclusive.
            if not owner_dead and now - d.stat().st_mtime < min_age_seconds:
                continue
            if (d / "postmaster.pid").exists():
                pid = _postmaster_pid(d)
                if pid is None:
                    continue
                if _pid_alive(pid):
                    # Live postmaster: only a verified orphan may be stopped.
                    if not owner_dead:
                        continue  # concurrent session, or pre-sentinel dir
                    if not _is_postmaster_for(pid, d):
                        continue  # PID reused by an unrelated process
                    if not _stop_orphan_postmaster(pid):
                        continue  # didn't die in time — retry next session
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d)
        except OSError:
            continue  # racing another session's cleanup — never fail the suite
    return removed


if __name__ == "__main__":
    import argparse
    import tempfile

    parser = argparse.ArgumentParser(
        description="Sweep orphaned agnes-pgserver-* data dirs (and their live orphaned postmasters) from the temp root."
    )
    parser.add_argument("--force", action="store_true", help="ignore the minimum-age guard")
    args = parser.parse_args()
    reaped = reap_orphaned_pgserver_dirs(
        Path(tempfile.gettempdir()),
        min_age_seconds=0 if args.force else MIN_AGE_SECONDS,
    )
    for path in reaped:
        print(path)
    print(f"{len(reaped)} dir(s) removed")
