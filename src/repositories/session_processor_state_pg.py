"""Postgres-backed session_processor_state repository.

Mirrors ``src/repositories/session_processor_state.py``. PG ``TIMESTAMP
WITH TIME ZONE`` preserves UTC offsets across the round-trip, so we no
longer need the strip-tz step the DuckDB impl carries.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Engine

# Mirrors ``src.repositories.session_processor_state._MTIME_SKEW_WINDOW``.
_MTIME_SKEW_WINDOW = timedelta(milliseconds=50)

# Mirrors ``src.repositories.session_processor_state._VERSIONED_HASH_RE`` —
# a declared processor version is recorded inside the stored file_hash as
# ``v<version>:<md5>`` so a bump invalidates the row without a schema change.
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


class SessionProcessorStatePgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def is_processed(
        self,
        processor_name: str,
        session_file: str,
        file_hash: str,
        *,
        version: int | None = None,
    ) -> bool:
        """Mirrors the DuckDB sibling: hash mismatch OR processor-version
        mismatch (incl. legacy rows written before the processor declared a
        version) reads as unprocessed."""
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    """SELECT file_hash FROM session_processor_state
                       WHERE processor_name = :p AND session_file = :s"""
                ),
                {"p": processor_name, "s": session_file},
            ).first()
        if row is None:
            return False
        return row[0] == _compose_versioned_hash(file_hash, version)

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
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO session_processor_state
                        (processor_name, session_file, username, processed_at, items_extracted, file_hash)
                        VALUES (:p, :s, :u, :now, :ic, :h)
                        ON CONFLICT (processor_name, session_file) DO UPDATE
                        SET processed_at = EXCLUDED.processed_at,
                            items_extracted = EXCLUDED.items_extracted,
                            file_hash = EXCLUDED.file_hash,
                            username = EXCLUDED.username"""
                ),
                {
                    "p": processor_name,
                    "s": session_file,
                    "u": username,
                    "now": processed_at,
                    "ic": items_count,
                    "h": stored_hash,
                },
            )

    def delete_for_processors(self, processor_names: list[str]) -> int:
        """DELETE every state row whose processor_name is in *processor_names*.
        Returns the number of rows deleted. Empty input → 0 (no query).

        Mirrors the DuckDB sibling — count via ``RETURNING 1`` + ``.all()`` for
        cross-backend parity."""
        if not processor_names:
            return 0
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text(
                    """DELETE FROM session_processor_state
                       WHERE processor_name = ANY(:names)
                       RETURNING 1"""
                ),
                {"names": list(processor_names)},
            ).all()
        return len(rows)

    def max_processed_at(self, processor_name: str) -> datetime | None:
        """Most recent processed_at across all session rows for *processor_name*,
        or None if the processor has no state rows."""
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT MAX(processed_at) FROM session_processor_state WHERE processor_name = :p"),
                {"p": processor_name},
            ).first()
        return row[0] if row else None

    def activity_since(self, processor_name: str, since: datetime) -> dict:
        """Mirrors ``SessionProcessorStateRepository.activity_since``."""
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    """SELECT MAX(processed_at), SUM(items_extracted)
                       FROM session_processor_state
                       WHERE processor_name = :p AND processed_at >= :since"""
                ),
                {"p": processor_name, "since": since},
            ).first()
        last_processed_at = row[0] if row else None
        items_extracted = int(row[1] or 0) if row else 0
        return {"last_processed_at": last_processed_at, "items_extracted": items_extracted}

    def processed_session_files(self, processor_name: str) -> set[str]:
        """The set of session_file values this processor has a state row for."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT session_file FROM session_processor_state WHERE processor_name = :p"),
                {"p": processor_name},
            ).all()
        return {r[0] for r in rows}

    def get_states_for_session_files(
        self,
        processor_name: str,
        session_files: list[str],
    ) -> dict[str, dict]:
        """For *processor_name*, return ``{session_file: {'processed_at': ...,
        'items_extracted': ...}}`` for each of *session_files* that has a state
        row. Empty input → ``{}``."""
        if not session_files:
            return {}
        stmt = sa.text(
            """SELECT session_file, processed_at, items_extracted
               FROM session_processor_state
               WHERE processor_name = :p
                 AND session_file IN :files"""
        ).bindparams(sa.bindparam("files", expanding=True))
        with self._engine.connect() as conn:
            rows = conn.execute(
                stmt,
                {"p": processor_name, "files": list(session_files)},
            ).all()
        return {r[0]: {"processed_at": r[1], "items_extracted": r[2]} for r in rows}

    def scan_unprocessed_for(
        self,
        processor_name: str,
        session_dir: Path,
        *,
        version: int | None = None,
    ) -> list[tuple[str, Path]]:
        """Mirrors the DuckDB sibling — see its docstring for why the
        processor-version check must live in the scan, not only in
        ``is_processed``."""
        results: list[tuple[str, Path]] = []
        if not session_dir.exists():
            return results

        known: dict[str, tuple[datetime | None, str]] = {}
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT session_file, processed_at, file_hash FROM session_processor_state
                       WHERE processor_name = :p"""
                ),
                {"p": processor_name},
            ).all()
        for sf, pa, fh in rows:
            known[sf] = (pa, fh)

        for user_dir in session_dir.iterdir():
            if not user_dir.is_dir():
                continue
            username = user_dir.name
            for jsonl_file in sorted(user_dir.glob("*.jsonl")):
                key = f"{username}/{jsonl_file.name}"
                if key not in known:
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
                    results.append((username, jsonl_file))
                    continue
                try:
                    mtime_epoch = jsonl_file.stat().st_mtime
                except OSError:
                    results.append((username, jsonl_file))
                    continue
                # PG TIMESTAMPTZ keeps tz on the round-trip; compare against
                # a tz-aware mtime so we don't lose precision.
                mtime = datetime.fromtimestamp(mtime_epoch, tz=UTC)
                if processed_at.tzinfo is None:
                    processed_at = processed_at.replace(tzinfo=UTC)
                if mtime >= processed_at:
                    results.append((username, jsonl_file))
                    continue
                if (processed_at - mtime) <= _MTIME_SKEW_WINDOW:
                    try:
                        if _md5_file(jsonl_file) != stored_content_hash:
                            results.append((username, jsonl_file))
                    except OSError:
                        results.append((username, jsonl_file))
        return results
