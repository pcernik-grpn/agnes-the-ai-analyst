"""VerificationProcessor — first plugin of the session-pipeline framework.

Wraps the body of the pre-refactor `verification_detector.detector.run()`
inner loop so the LLM extraction + persist behavior is unchanged after the
framework refactor. Tests in `tests/test_corporate_memory_v1.py` are the
regression contract.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import duckdb

from connectors.llm import StructuredExtractor
from connectors.llm.exceptions import LLMError
from services.corporate_memory import contradiction as contradiction_module
from services.corporate_memory.confidence import compute_confidence
from services.session_pipeline.contract import ProcessorResult
from services.session_pipeline.lib import parse_jsonl
from services.verification_detector.duplicates import (
    _record_duplicate_candidates,
    find_duplicate_target,
)
from services.verification_detector.detector import (
    _generate_id,
    extract_verifications,
)

from src.repositories import (
    knowledge_repo,
)


logger = logging.getLogger(__name__)

# Wall-clock budget (seconds) for the per-verification-item loop in
# process_session(). Incident 2026-07-15: a session with dozens of
# verification items ran contradiction_module.detect_and_record() (an inline
# LLM call) for each one, sequentially, inside a single sync admin-endpoint
# call — over an hour holding a FastAPI threadpool worker + the system DB
# connection, starving every other request behind the reverse-proxy's 5s
# upstream timeout (app-wide 503s).
#
# Once the budget is exceeded mid-session, process_session() raises
# TimeBudgetExceeded instead of returning. Per the SessionProcessor contract
# (services/session_pipeline/contract.py), a raise means the runner does NOT
# mark the session processed, so it is picked up again on the next scheduler
# tick (cadence_minutes=15) — items already created before the budget hit
# hash-collide on retry (repo.create() takes the cheap "duplicate" path) and
# the duplicate branch skips re-recording evidence for a (item_id,
# source_user, source_ref) it already has, so a retry only pays the LLM cost
# for the items that didn't get a chance to run.
_TIME_BUDGET_SECONDS = 180


class TimeBudgetExceeded(Exception):
    """process_session() ran out of its wall-clock budget partway through a
    session's verification items. The session is intentionally left
    unprocessed so the remaining items are retried on the next scheduler
    tick — see _TIME_BUDGET_SECONDS above."""


class VerificationProcessor:
    name: str = "verification"
    cadence_minutes: int = 15

    def __init__(self, extractor: StructuredExtractor):
        self.extractor = extractor

    def process_session(
        self,
        session_path: Path,
        username: str,
        session_key: str,
        conn: duckdb.DuckDBPyConnection,
        **kwargs: object,
    ) -> ProcessorResult:
        # Read corporate_memory.sources.session_transcripts.* fresh on every
        # call (not cached at import/construction time) — same live-read
        # contract as resolve_distribution_mode() / the collector's own
        # governance_config lookup, so a save in /admin/server-config takes
        # effect on the next scheduler tick, no restart required (#1957).
        from app.instance_config import get_corporate_memory_config

        cm_config = get_corporate_memory_config() or {}
        session_transcripts_config = (cm_config.get("sources") or {}).get("session_transcripts") or {}

        if session_transcripts_config.get("enabled", True) is False:
            logger.info(
                "Session-transcript extraction disabled via "
                "corporate_memory.sources.session_transcripts.enabled — skipping %s",
                session_key,
            )
            return ProcessorResult(items_count=0)

        repo = knowledge_repo()
        session_id = f"session-{session_path.stem}-{username}"

        turns = parse_jsonl(session_path)
        if not turns:
            logger.info("Empty session: %s", session_key)
            return ProcessorResult(items_count=0)

        verifications = extract_verifications(self.extractor, username, session_id, turns)

        # Deterministic post-filter on corporate_memory.sources.
        # session_transcripts.detection_types, BEFORE dedup/insert below.
        # Absent key (legacy/no corporate_memory config, or the section
        # exists but doesn't set this key) keeps every detection_type the
        # LLM can return — behavior is unchanged from before this knob was
        # wired. An explicit empty list is a valid, if unusual, way to
        # block every detection type without disabling the source outright.
        allowed_detection_types = session_transcripts_config.get("detection_types")
        if allowed_detection_types is not None:
            allowed_set = set(allowed_detection_types)
            before_count = len(verifications)
            verifications = [v for v in verifications if v.get("detection_type") in allowed_set]
            dropped_count = before_count - len(verifications)
            if dropped_count:
                logger.info(
                    "detection_types filter dropped %d/%d extracted verification(s) for %s (allowed=%s)",
                    dropped_count,
                    before_count,
                    session_key,
                    sorted(allowed_set),
                )

        items_created = 0
        loop_start = time.monotonic()
        for idx, v in enumerate(verifications):
            if time.monotonic() - loop_start > _TIME_BUDGET_SECONDS:
                logger.warning(
                    "Verification processor exceeded %ds budget on %s after %d/%d "
                    "items (%d created) — stopping early, remaining items retried "
                    "on next scheduler tick",
                    _TIME_BUDGET_SECONDS,
                    session_key,
                    idx,
                    len(verifications),
                    items_created,
                )
                raise TimeBudgetExceeded(
                    f"time budget exceeded on {session_key} after {idx}/{len(verifications)} items"
                )

            item_id = _generate_id(v["title"], v["content"])
            existing = repo.get_by_id(item_id)
            if existing:
                # Hash collision on (title, content) → either another
                # analyst produced the same fact (ADR Decision 3 expects a
                # new evidence row per distinct verification event), or this
                # is the same session being retried after a prior
                # TimeBudgetExceeded and this item was already created +
                # given evidence on an earlier tick. Distinguish the two by
                # (source_user, source_ref): a retry re-processes the exact
                # same session_id for the exact same user, so skip it —
                # otherwise every retry tick appends another duplicate
                # evidence row for the same single confirmation event.
                already_recorded = any(
                    ev.get("source_user") == username and ev.get("source_ref") == session_id
                    for ev in repo.list_evidence(item_id)
                )
                if already_recorded:
                    logger.info(
                        "Evidence already recorded for %s on this session (retry) — skipping",
                        item_id,
                    )
                    continue
                logger.info(
                    "Duplicate item — recording evidence on existing: %s",
                    item_id,
                )
                repo.create_evidence(
                    item_id=item_id,
                    source_user=username,
                    source_ref=session_id,
                    detection_type=v.get("detection_type"),
                    user_quote=v.get("user_quote"),
                )
                continue

            # No exact-hash match — but the LLM's title/content is a
            # paraphrase, and _generate_id() hashes verbatim strings, so a
            # restated fact still needs to be caught here. The fuzzy dedup
            # gate looks for a same-domain item that is effectively the same
            # fact (entity-tag overlap, or lexical similarity as a fallback)
            # and, if found, merges into it instead of creating a
            # near-duplicate PENDING row. Failures here must not block
            # ingestion — fail open into the create path.
            #
            # A `correction` is excluded from the merge shortcut: it may be
            # OVERTURNING a stored fact rather than restating it, and a
            # correction is routinely a near-verbatim reword of the item it
            # contradicts ("computed monthly" -> "computed weekly"), so it
            # scores high on exactly the lexical/entity signals the gate
            # merges on. Absorbing it as confirming evidence would discard
            # the corrected content AND skip the contradiction check, which
            # only runs on the create path below. Route corrections to
            # create so detect_and_record() gets a chance to fire.
            duplicate_target = None
            if v.get("detection_type") != "correction":
                try:
                    duplicate_target = find_duplicate_target(
                        repo,
                        item_id=item_id,
                        title=v["title"],
                        content=v["content"],
                        domain=v.get("domain"),
                        entities=v.get("entities"),
                    )
                except Exception as e:
                    logger.warning("Fuzzy-duplicate lookup failed for %s: %s", item_id, e)
                    duplicate_target = None

            if duplicate_target is not None:
                target_id = duplicate_target["id"]
                already_recorded = any(
                    ev.get("source_user") == username and ev.get("source_ref") == session_id
                    for ev in repo.list_evidence(target_id)
                )
                if already_recorded:
                    logger.info(
                        "Fuzzy-duplicate evidence already recorded for %s on this session (retry) — skipping",
                        target_id,
                    )
                    continue
                logger.info(
                    "Paraphrased duplicate of %s detected — recording evidence instead of creating a new item",
                    target_id,
                )
                repo.create_evidence(
                    item_id=target_id,
                    source_user=username,
                    source_ref=session_id,
                    detection_type=v.get("detection_type"),
                    user_quote=v.get("user_quote"),
                )
                continue

            # Confidence is computed in code from (source_type, detection_type).
            # The LLM is not trusted to set its own credibility — see Q3 in
            # docs/archive/pd-ps-comments.md and the ADR.
            detection_type = v.get("detection_type")
            try:
                confidence_value = compute_confidence("user_verification", detection_type)
            except ValueError:
                # Unknown detection_type from the LLM; fall back to a
                # lookup-keyed default rather than the LLM-supplied value.
                confidence_value = compute_confidence("user_verification", "confirmation")
            repo.create(
                id=item_id,
                title=v["title"],
                content=v["content"],
                category="business_logic",
                source_user=username,
                tags=v.get("entities", []),
                status="pending",
                confidence=confidence_value,
                domain=v.get("domain"),
                entities=v.get("entities"),
                source_type="user_verification",
                source_ref=session_id,
                sensitivity="internal",
            )
            # Persist the verification evidence row — user_quote and
            # detection_type are the raw signal Bayesian re-calibration
            # will need later (Q3).
            repo.create_evidence(
                item_id=item_id,
                source_user=username,
                source_ref=session_id,
                detection_type=detection_type,
                user_quote=v.get("user_quote"),
            )
            items_created += 1

            # Record duplicate-candidate hints inline. Heuristic-only (no
            # LLM call) so it stays cheap; failures must never abort
            # session processing — log and continue. Issue #62.
            try:
                new_item = repo.get_by_id(item_id)
                if new_item is not None:
                    _record_duplicate_candidates(repo, new_item)
            except Exception as e:
                logger.warning(
                    "Duplicate-candidate detection failed for %s: %s",
                    item_id,
                    e,
                )

            # Run contradiction detection inline. Failure of the LLM
            # judge must not abort session processing — log and move on.
            try:
                new_item = repo.get_by_id(item_id)
                if new_item is not None:
                    contradiction_module.detect_and_record(self.extractor, new_item, repo)
            except LLMError as e:
                logger.warning("Contradiction check failed for %s: %s", item_id, e)
            except Exception as e:
                logger.warning(
                    "Unexpected error during contradiction check for %s: %s",
                    item_id,
                    e,
                )

        logger.info(
            "Processed %s: %d verifications, %d items created",
            session_key,
            len(verifications),
            items_created,
        )
        return ProcessorResult(items_count=items_created)


def build_verification_processor() -> VerificationProcessor:
    """Factory that constructs the LLM extractor from instance config + env.

    Mirrors the pattern in services/verification_detector/__main__.py and
    app/api/admin.py:run_verification_detector — both built the extractor
    lazily at call time. Raises if the LLM isn't configured."""
    from connectors.llm import create_extractor_from_env_or_config

    try:
        from app.instance_config import load_instance_config

        try:
            config = load_instance_config()
        except (ValueError, FileNotFoundError):
            config = {}
        ai_config = config.get("ai") if config else None
    except Exception:
        ai_config = None

    extractor = create_extractor_from_env_or_config(ai_config)
    return VerificationProcessor(extractor=extractor)
