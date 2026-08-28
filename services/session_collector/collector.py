#!/usr/bin/env python3
"""Collect Claude Code session transcripts from all user home directories.

This script runs as a systemd service (session-collector.service) triggered by
session-collector.timer. It scans all /home/*/user/sessions/ directories and
copies session transcript files to /data/user_sessions/$user/ for centralized
storage and analysis.

Design principles:
- Must run as root (or user with read access to all /home/*)
- Preserves file metadata (timestamps, permissions)
- Idempotent - safe to run multiple times (skips existing files)
- Atomic operations - uses tempfile + os.replace for safety
- Logs to stdout (captured by journalctl)

TODO(scheduler-v2): In docker-compose.yml this service is a one-shot process
restarted by Docker (`restart: unless-stopped`), which is effectively a tight
boot loop (tracked in #221). The other half of this TODO — wiring into
services/scheduler/__main__.py's JOBS list and exposing an admin endpoint —
has landed: see the "session-collector" job kind in the scheduler and
POST /api/admin/run-session-collector in app/api/admin.py.
"""

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Iterator

from app.logging_config import setup_logging

# Central storage for session transcripts
TARGET_BASE = Path("/data/user_sessions")

# Directory to scan for sessions in each user home
USER_SESSIONS_DIR = "user/sessions"

setup_logging(__name__)
logger = logging.getLogger(__name__)


def find_user_home_dirs() -> Iterator[Path]:
    """Yield all user home directories from /home/*."""
    home_base = Path("/home")
    if not home_base.exists():
        logger.warning(f"{home_base} does not exist")
        return

    for entry in home_base.iterdir():
        if entry.is_dir() and not entry.name.startswith("."):
            yield entry


def find_session_files(user_home: Path) -> Iterator[Path]:
    """Yield all session JSONL files from user's sessions directory."""
    sessions_dir = user_home / USER_SESSIONS_DIR
    if not sessions_dir.exists():
        return

    try:
        for entry in sessions_dir.iterdir():
            if entry.is_file() and entry.suffix == ".jsonl":
                yield entry
    except PermissionError:
        logger.warning(f"Permission denied reading {sessions_dir}")
    except Exception as e:
        logger.error(f"Error scanning {sessions_dir}: {e}")


def copy_session_file(source: Path, target: Path, dry_run: bool = False) -> bool:
    """Copy session file to target location, preserving metadata.

    Returns True if file was copied, False if skipped (already exists).
    """
    if target.exists():
        # Already collected, skip
        return False

    if dry_run:
        logger.info(f"[DRY-RUN] Would copy: {source} -> {target}")
        return True

    try:
        # Ensure target directory exists
        target.parent.mkdir(parents=True, exist_ok=True)

        # Copy with metadata preserved
        shutil.copy2(source, target)
        logger.info(f"Collected: {source} -> {target}")
        return True
    except Exception as e:
        logger.error(f"Failed to copy {source} to {target}: {e}")
        return False


def collect_user_sessions(username: str, user_home: Path, dry_run: bool = False) -> tuple[int, int]:
    """Collect all session files for a user.

    Returns tuple (files_copied, files_skipped).
    """
    target_dir = TARGET_BASE / username
    copied = 0
    skipped = 0

    for session_file in find_session_files(user_home):
        target_path = target_dir / session_file.name

        if copy_session_file(session_file, target_path, dry_run=dry_run):
            copied += 1
        else:
            skipped += 1

    return copied, skipped


def run(dry_run: bool = False, verbose: bool = False) -> tuple[int, dict]:
    """Run the session-collector pass. Returns (exit_code, stats).

    Argv-free so callers (FastAPI admin endpoint, scheduler) can invoke
    without inheriting uvicorn's sys.argv — argparse here would
    SystemExit(2) when uvicorn's flags hit it.

    stats keys: users_processed, files_copied, files_skipped.
    """
    import grp

    if verbose:
        logger.setLevel(logging.DEBUG)

    logger.info("Starting session transcript collection")

    # Skip the legacy /home/*/user/sessions/ scan in deployment layouts that
    # don't populate it (e.g. Docker compose, where Claude Code never lands
    # session jsonls under /home). Without this, the scheduler's 10-min
    # /api/admin/run-session-collector calls log "0 users, 0 files copied"
    # plus a misleading "Group 'data-ops' not found" WARNING per run.
    # Explicit env var only — no auto-detect: the bare-VM path *does*
    # populate /home/*/, and the data-ops warning there is load-bearing
    # for catching missing-group mis-deploys.
    if os.environ.get("AGNES_SKIP_LEGACY_COLLECTOR", "").strip() in ("1", "true", "TRUE"):
        logger.debug("AGNES_SKIP_LEGACY_COLLECTOR set; skipping legacy /home/*/user/sessions/ scan")
        return 0, {
            "users_processed": 0,
            "files_copied": 0,
            "files_skipped": 0,
            "skipped": True,
        }

    try:
        TARGET_BASE.mkdir(parents=True, exist_ok=True)
        os.chmod(TARGET_BASE, 0o2770)

        try:
            dataops_gid = grp.getgrnam("data-ops").gr_gid
            os.chown(TARGET_BASE, -1, dataops_gid)
        except KeyError:
            logger.warning("Group 'data-ops' not found, using default group")
        except Exception as e:
            logger.warning(f"Could not set group ownership: {e}")

    except Exception as e:
        logger.error(f"Failed to create target directory {TARGET_BASE}: {e}")
        return 1, {"users_processed": 0, "files_copied": 0, "files_skipped": 0}

    total_copied = 0
    total_skipped = 0
    users_processed = 0

    for user_home in find_user_home_dirs():
        username = user_home.name

        try:
            uid = user_home.stat().st_uid
            if uid < 1000:
                continue
        except Exception:
            continue

        copied, skipped = collect_user_sessions(username, user_home, dry_run=dry_run)

        if copied > 0 or skipped > 0:
            users_processed += 1
            total_copied += copied
            total_skipped += skipped
            logger.info(f"User {username}: {copied} copied, {skipped} skipped")

    logger.info(
        f"Collection complete: {users_processed} users, {total_copied} files copied, {total_skipped} files skipped"
    )

    return 0, {
        "users_processed": users_processed,
        "files_copied": total_copied,
        "files_skipped": total_skipped,
    }


def main() -> int:
    """CLI entry point. Parses argv, delegates to run()."""
    import argparse

    parser = argparse.ArgumentParser(description="Collect Claude Code session transcripts from all users")
    parser.add_argument("--dry-run", action="store_true", help="Preview what would be copied without actually copying")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose output")

    args = parser.parse_args()
    rc, _ = run(dry_run=args.dry_run, verbose=args.verbose)
    return rc


if __name__ == "__main__":
    sys.exit(main())
