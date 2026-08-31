"""UsageProcessor — extracts skill / agent / tool invocation events from
Claude Code session jsonls. See Phase A.3 of platform-telemetry epic."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import duckdb

from services.session_pipeline.contract import ProcessorResult
from services.session_pipeline.lib import parse_jsonl
from services.session_processors.usage_lib import (
    USAGE_PROCESSOR_VERSION,
    MarketplaceItemLookup,
    compute_summary,
    iter_events,
    iter_turn_usage,
)

from src.repositories import (
    usage_repo,
    usage_turns_repo,
    use_pg,
)


logger = logging.getLogger(__name__)

#: One DEBUG line per process when the app-state backend cannot hold turns.
#: `usage_turns` is PG-only (A3 PG-first ratchet), and the processor runs per
#: session — logging the skip every time would drown the tick's own output on
#: a DuckDB instance where the condition is permanent, not an incident.
_logged_turns_unavailable = False

#: Chat exports. Their turns are written live by the chat manager at message
#: persist (with cache tokens the export itself does not carry), so emitting
#: here would mint a second, differently-keyed copy of every one of them.
_CHAT_SESSION_PREFIX = "chat-"


class UsageProcessor:
    name: str = "usage"
    cadence_minutes: int = 10

    def process_session(
        self,
        session_path: Path,
        username: str,
        session_key: str,
        conn: duckdb.DuckDBPyConnection,
        *,
        user_id: str | None = None,
    ) -> ProcessorResult:
        turns = parse_jsonl(session_path)
        events = list(iter_events(turns))

        # Derive session_id from first turn that carries one
        session_id = session_key
        for t in turns:
            sid = t.get("sessionId")
            if sid:
                session_id = sid
                break

        lookup = MarketplaceItemLookup()
        rows = []
        for e in events:
            source, parent_plugin, _local, _type = lookup.resolve(e)
            # `usage_events.ref_id` carries the parent plugin name (curated)
            # or '' (flea standalone / builtin). Empty string normalised to
            # NULL for backwards compat with admin telemetry endpoints that
            # filter `ref_id IS NOT NULL`.
            ref_id = parent_plugin or None
            # Stable dedup key: session_id + event_uuid + tool_id + event_type + tool_name + command_name.
            # tool_id (tu_xxx) disambiguates parallel tool_use items in the same assistant turn
            # that share the same event_uuid, event_type, and tool_name.
            id_input = (
                f"{session_id}|{e.event_uuid or ''}|{e.tool_id or ''}"
                f"|{e.event_type}|{e.tool_name or ''}"
                f"|{e.command_name or ''}"
            )
            event_id = hashlib.sha256(id_input.encode()).hexdigest()
            rows.append(
                {
                    "id": event_id,
                    "session_id": session_id,
                    "session_file": session_key,
                    "username": username,
                    "user_id": user_id,
                    "event_uuid": e.event_uuid,
                    "parent_uuid": e.parent_uuid,
                    "event_type": e.event_type,
                    "tool_name": e.tool_name,
                    "skill_name": e.skill_name,
                    "subagent_type": e.subagent_type,
                    "command_name": e.command_name,
                    "is_error": e.is_error,
                    "source": source,
                    "ref_id": ref_id,
                    "model": e.model,
                    "cwd": e.cwd,
                    "occurred_at": e.occurred_at,
                    "processor_version": USAGE_PROCESSOR_VERSION,
                }
            )

        summary = compute_summary(turns, rows)
        summary["session_file"] = session_key
        summary["username"] = username
        summary["user_id"] = user_id
        # Override session_id with the resolved one
        if not summary.get("session_id"):
            summary["session_id"] = session_id

        repo = usage_repo()
        n_written = repo.upsert_events(rows, processor_version=USAGE_PROCESSOR_VERSION)
        # Before the summary write: for a chat session this step is what puts
        # the cache totals INTO the summary being written.
        n_turns = self._record_turns(
            turns,
            session_key=session_key,
            session_id=session_id,
            user_id=user_id,
            summary=summary,
        )
        repo.upsert_summary(summary, processor_version=USAGE_PROCESSOR_VERSION)

        logger.info(
            "usage processor: %d events, %d turns written for session %s",
            n_written,
            n_turns,
            session_key,
        )
        return ProcessorResult(items_count=len(rows))

    def _record_turns(
        self,
        turns: list[dict],
        *,
        session_key: str,
        session_id: str,
        user_id: str | None,
        summary: dict,
    ) -> int:
        """Store one ``usage_turns`` row per assistant turn; return how many
        were new.

        Three behaviours, in order:

        1. **No Postgres, nothing to do.** ``usage_turns`` is PG-only (A3
           PG-first ratchet), so on a DuckDB app-state instance the session
           summary remains the finest grain available. The guard is what keeps
           ``RequiresPostgresBackend`` from turning a working summary pipeline
           into a per-session crash.
        2. **Chat exports emit nothing.** Their turns already exist — written
           live at message persist, and carrying cache tokens the export
           itself never had. Instead of duplicating them, the summary being
           written adopts their cache totals, so ``agnes_sessions`` and
           ``agnes_turns`` report the same numbers for a chat session.
        3. **Everything else emits its turns**, idempotently: the unique
           ``(session_file, turn_uuid)`` key means a re-process of a grown
           session adds only the turns that are actually new.

        Never raises. A telemetry table that is unreachable (or a schema not
        yet migrated) must not stop the session summary — the coarser signal
        every usage surface already depends on — from being written. The next
        content change, or an operator reprocess, retries the write for free.
        """
        global _logged_turns_unavailable

        if not use_pg():
            if not _logged_turns_unavailable:
                _logged_turns_unavailable = True
                logger.debug(
                    "usage processor: per-turn token rows need the Postgres app-state "
                    "backend; recording session-grain summaries only"
                )
            return 0

        try:
            repo = usage_turns_repo()
            basename = session_key.rsplit("/", 1)[-1]
            if basename.startswith(_CHAT_SESSION_PREFIX):
                # Look the live rows up by BASENAME, not by session_key: the
                # chat manager writes its turns as they happen, keyed
                # `chat-<chat_id>.jsonl`, while this pipeline keys a session by
                # `<dir_name>/<filename>` because the directory is what carries
                # the uploading identity. Joining on the pipeline's key would
                # silently find nothing and leave every chat session reading
                # zero cache tokens — the exact bug this overlay exists to fix.
                totals = repo.cache_totals_for_session_file(basename)
                # Only overlay when live rows exist: an empty result is "no
                # turns recorded yet", not "this session cached nothing", and
                # writing its zeros would erase whatever the export carried.
                if totals["cache_read_tokens"] or totals["cache_creation_tokens"]:
                    summary["cache_read_tokens"] = totals["cache_read_tokens"]
                    summary["cache_creation_tokens"] = totals["cache_creation_tokens"]
                return 0

            rows = [
                {
                    **turn,
                    "session_file": session_key,
                    "session_id": session_id,
                    "user_id": user_id,
                    "surface": "claude_code",
                    "processor_version": USAGE_PROCESSOR_VERSION,
                }
                for turn in iter_turn_usage(turns)
            ]
            return repo.insert_batch(rows)
        except Exception:
            logger.warning(
                "usage processor: could not record per-turn tokens for %s (summary still written)",
                session_key,
                exc_info=True,
            )
            return 0
