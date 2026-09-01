"""Repository for session_processor_state — per-(processor, session) bookkeeping
for the session pipeline framework (services/session_pipeline/).

Composite PK (processor_name, session_file) lets each processor track its own
processed-set independently. file_hash invalidates the row when a session jsonl
grows (Claude Code appending live to an active session) so processors reprocess
the new content rather than treating the first hash as final.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

# When a jsonl is modified immediately after the previous processing tick,
# ``st_mtime`` can lag ``datetime.now()`` by a few milliseconds on some
# filesystems/VM clocks.  Treat mtimes within this window of ``processed_at``
# as ambiguous and verify with the stored file hash before discarding.
_MTIME_SKEW_WINDOW = timedelta(milliseconds=50)

# A processor that declares a ``version`` gets it recorded inside the stored
# file_hash as ``v<version>:<md5>`` (no schema change — the DuckDB app-state
# ladder is frozen under the A3 ratchet). Unambiguous against a bare md5:
# hex digests carry no ``:``. The format is private to this repo and its PG
# sibling; callers pass ``version`` and a bare content hash.
_VERSIONED_HASH_RE = re.compile(r"^v(\d+):(.*)$")


def _md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _compose_versioned_hash(file_hash: str, version: int | None) -> str:
    return file_hash if version is None else f"v{version}:{file_hash}"


def _split_versioned_hash(stored: str | None) -> tuple[int | None, str | None]:
    """``(version, content_hash)`` parsed from a stored file_hash value.
    Legacy rows (bare md5) and NULLs come back as ``(None, stored)``."""
    if not stored:
        return None, stored
    m = _VERSIONED_HASH_RE.match(stored)
    if m:
        return int(m.group(1)), m.group(2)
    return None, stored


class SessionProcessorStateRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def is_processed(
        self,
        processor_name: str,
        session_file: str,
        file_hash: str,
        *,
        version: int | None = None,
    ) -> bool:
        """True iff a state row exists for (processor_name, session_file) AND
        the stored file_hash matches the supplied current hash — at the same
        processor *version*, when one is declared. Hash mismatch (session
        jsonl grew since last run) or version mismatch (processor bumped its
        version to force a backfill; also covers legacy rows written before
        the processor declared one) is treated as unprocessed so the
        processor reprocesses on the next tick."""
        result = self.conn.execute(
            """SELECT file_hash FROM session_processor_state
                WHERE processor_name = ? AND session_file = ?""",
            [processor_name, session_file],
        ).fetchone()
        if result is None:
            return False
        return result[0] == _compose_versioned_hash(file_hash, version)

    def mark_processed(
        self,
        processor_name: str,
        session_file: str,
        username: str,
        items_count: int,
        file_hash: str,
        read_at: datetime | None = None,
        *,
        version: int | None = None,
    ) -> None:
        """UPSERT — overwrites previous state row for (processor, session).

        *read_at* should be the moment the file hash was observed; when the
        processor runs for a long time or appends to the jsonl mid-run, this
        preserves the correct mtime/ordering relationship for the next scan.

        *version*, when the processor declares one, is recorded inside the
        stored hash so a later bump invalidates the row (see ``is_processed``
        / ``scan_unprocessed_for``).
        """
        processed_at = read_at if read_at is not None else datetime.now(UTC)
        stored_hash = _compose_versioned_hash(file_hash, version)
        self.conn.execute(
            """INSERT INTO session_processor_state
                (processor_name, session_file, username, processed_at, items_extracted, file_hash)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (processor_name, session_file) DO UPDATE
                SET processed_at = excluded.processed_at,
                    items_extracted = excluded.items_extracted,
                    file_hash = excluded.file_hash,
                    username = excluded.username""",
            [processor_name, session_file, username, processed_at, items_count, stored_hash],
        )

    def delete_for_processors(self, processor_names: list[str]) -> int:
        """DELETE every state row whose processor_name is in *processor_names*.
        Returns the number of rows deleted. Empty input → 0 (no query).

        Backs admin_usage.reprocess_usage, which wipes the 'usage' +
        'marketplace_rollup_30d' processor state so the next scheduler tick
        re-scans every JSONL. cursor.rowcount is unreliable on DuckDB, so we
        count via RETURNING 1 + fetchall()."""
        if not processor_names:
            return 0
        placeholders = ",".join("?" for _ in processor_names)
        rows = self.conn.execute(
            f"""DELETE FROM session_processor_state
                WHERE processor_name IN ({placeholders})
                RETURNING 1""",
            list(processor_names),
        ).fetchall()
        return len(rows)

    def max_processed_at(self, processor_name: str) -> datetime | None:
        """Most recent processed_at across all session rows for *processor_name*,
        or None if the processor has no state rows. Backs the session-pipeline
        health check in app/api/health.py."""
        row = self.conn.execute(
            "SELECT MAX(processed_at) FROM session_processor_state WHERE processor_name = ?",
            [processor_name],
        ).fetchone()
        return row[0] if row else None

    def activity_since(self, processor_name: str, since: datetime) -> dict:
        """Most recent ``processed_at`` + summed ``items_extracted`` across
        *processor_name*'s rows touched at/after *since*.

        Backs the Activity Center health pulse's "memory pipeline" field
        (``app/api/activity.py`` ``_compute_health``): a non-null
        ``last_processed_at`` means the processor ran within the window.
        """
        row = self.conn.execute(
            """SELECT MAX(processed_at), SUM(items_extracted)
                FROM session_processor_state
                WHERE processor_name = ? AND processed_at >= ?""",
            [processor_name, since],
        ).fetchone()
        last_processed_at = row[0] if row else None
        items_extracted = int(row[1] or 0) if row else 0
        return {"last_processed_at": last_processed_at, "items_extracted": items_extracted}

    def processed_session_files(self, processor_name: str) -> set[str]:
        """The set of session_file values this processor has a state row for.
        Backs the FIFO stuck-file check in app/api/health.py."""
        rows = self.conn.execute(
            "SELECT session_file FROM session_processor_state WHERE processor_name = ?",
            [processor_name],
        ).fetchall()
        return {r[0] for r in rows}

    def get_states_for_session_files(
        self,
        processor_name: str,
        session_files: list[str],
    ) -> dict[str, dict]:
        """For *processor_name*, return ``{session_file: {'processed_at': ...,
        'items_extracted': ...}}`` for each of *session_files* that has a state
        row. Empty input → ``{}``. Backs the pipeline-status enrichment in
        app/api/me_stats.py."""
        if not session_files:
            return {}
        placeholders = ",".join("?" for _ in session_files)
        rows = self.conn.execute(
            f"""SELECT session_file, processed_at, items_extracted
                FROM session_processor_state
                WHERE processor_name = ?
                  AND session_file IN ({placeholders})""",
            [processor_name, *session_files],
        ).fetchall()
        return {r[0]: {"processed_at": r[1], "items_extracted": r[2]} for r in rows}

    def scan_unprocessed_for(
        self,
        processor_name: str,
        session_dir: Path,
        *,
        version: int | None = None,
    ) -> list[tuple[str, Path]]:
        """Return (username, jsonl_path) pairs in *session_dir* that this
        processor needs to (re)process: no state row, OR state row whose
        stored hash does not match the current file content, OR state row
        with an mtime newer than the stored ``processed_at``, OR — when the
        caller declares a processor *version* — a state row written at a
        different (or no) version. The version check must live HERE, not only
        in ``is_processed``: the mtime precheck below skips an untouched file
        before any hash is consulted, so without it a version bump would
        never re-process the existing backlog (observed live 2026-09-01).

        ``st_mtime`` is used as a cheap precheck, but it can lag
        ``datetime.now()`` by a few milliseconds on some filesystems/VM clocks.
        When mtime is within :data:`_MTIME_SKEW_WINDOW` of ``processed_at`` we
        verify with the stored ``file_hash`` before discarding the file.
        Files that survive the precheck still go through the runner's per-file
        ``is_processed(file_hash)`` check for authoritative hash-based
        invalidation.
        """
        results: list[tuple[str, Path]] = []
        if not session_dir.exists():
            return results

        known: dict[str, tuple[datetime | None, str]] = {}
        rows = self.conn.execute(
            """SELECT session_file, processed_at, file_hash FROM session_processor_state
                WHERE processor_name = ?""",
            [processor_name],
        ).fetchall()
        for sf, pa, fh in rows:
            known[sf] = (pa, fh)

        for user_dir in session_dir.iterdir():
            if not user_dir.is_dir():
                continue
            username = user_dir.name
            for jsonl_file in sorted(user_dir.glob("*.jsonl")):
                key = f"{username}/{jsonl_file.name}"
                if key not in known:
                    # No state row → definitely needs processing.
                    results.append((username, jsonl_file))
                    continue
                processed_at, stored_hash = known[key]
                stored_version, stored_content_hash = _split_versioned_hash(stored_hash)
                if version is not None and stored_version != version:
                    # Row written at an older processor version (or before the
                    # processor declared one) → dirty regardless of mtime.
                    results.append((username, jsonl_file))
                    continue
                if processed_at is None:
                    # Defensive: row without processed_at shouldn't happen
                    # (mark_processed always sets it), but if it does,
                    # surface for the runner.
                    results.append((username, jsonl_file))
                    continue
                try:
                    mtime_epoch = jsonl_file.stat().st_mtime
                except OSError:
                    # Stat failure: surface for the runner — it'll fail the
                    # hash compute next and report a clean error in stats
                    # rather than us silently dropping the file here.
                    results.append((username, jsonl_file))
                    continue
                # Compare in naive-UTC: the DuckDB connection helper
                # (`src.db._open_duckdb`) pins the session timezone to UTC,
                # so `processed_at` reads as UTC-clock-naive. Convert the
                # file's epoch mtime to UTC-naive on the same axis.
                mtime = datetime.fromtimestamp(mtime_epoch, tz=UTC).replace(tzinfo=None)
                if processed_at.tzinfo is not None:
                    processed_at = processed_at.replace(tzinfo=None)

                if mtime >= processed_at:
                    # File touched since/at last run — could be a live-append
                    # (Claude Code writing to an active session). Surface
                    # for the runner; its hash compare will skip if content
                    # is identical (some editors rewrite-without-change).
                    results.append((username, jsonl_file))
                    continue

                if (processed_at - mtime) <= _MTIME_SKEW_WINDOW:
                    # mtime is too close to processed_at to trust alone:
                    # clock skew can make a post-process write look earlier
                    # than processed_at.  Verify the stored hash — the content
                    # part of it, so versioned rows don't churn here.
                    try:
                        if _md5_file(jsonl_file) != stored_content_hash:
                            results.append((username, jsonl_file))
                    except OSError:
                        results.append((username, jsonl_file))
                # else: stable session, skip without hashing.
        return results
