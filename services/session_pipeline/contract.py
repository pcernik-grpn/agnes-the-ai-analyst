"""Contract for session-pipeline processors.

A processor is anything that, given a parsed Claude Code session jsonl file,
emits some side effect — knowledge extraction, usage events, error metrics,
security findings, etc. The runner (`services/session_pipeline/runner.py`)
calls process_session() once per unprocessed file and persists state on success.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import duckdb


@dataclass(frozen=True)
class ProcessorResult:
    """Per-session outcome surfaced to the runner. items_count is the number
    of records the processor produced (knowledge items, events, etc.) and
    is stored in session_processor_state.items_extracted for observability —
    not load-bearing for the framework's correctness."""

    items_count: int = 0


@runtime_checkable
class SessionProcessor(Protocol):
    """Implementations live in services/session_processors/<name>.py and
    are listed in services/session_processors/__init__.py:PROCESSORS."""

    name: str
    """Unique processor key. Used in session_processor_state.processor_name
    and as the URL query param for /api/admin/run-session-processor."""

    cadence_minutes: int
    """How often the scheduler should invoke this processor. The actual
    schedule entry is built in services/scheduler/__main__.py from this value
    (env-overridable per processor).

    A processor may additionally declare a ``version: int`` class attribute
    (not part of the Protocol — the runner reads it via ``getattr``, so
    existing processors need not change). When present, the runner records it
    in ``session_processor_state`` alongside the content hash, and bumping it
    invalidates every previously processed session — the mechanism behind
    ``USAGE_PROCESSOR_VERSION`` backfills. Declare one only when re-processing
    the whole backlog on a bump is affordable (the usage processor is cheap
    and local; an LLM-driven processor may not want this)."""

    def process_session(
        self,
        session_path: Path,
        username: str,
        session_key: str,
        conn: duckdb.DuckDBPyConnection,
    ) -> ProcessorResult:
        """Process exactly one session jsonl. Idempotent per
        (name, session_key, file_hash).

        Raise = the runner will NOT mark this session as processed for this
        processor → it will be retried on the next scheduler tick. Return =
        the runner marks it processed and skips it next time (until its
        file_hash changes)."""
        ...
