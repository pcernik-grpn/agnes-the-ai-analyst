"""VerificationProcessor — first plugin of the session-pipeline framework.

Wraps the body of the pre-refactor `verification_detector.detector.run()`
inner loop so the LLM extraction + persist behavior is unchanged after the
framework refactor. Tests in `tests/test_corporate_memory_v1.py` are the
regression contract.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
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
    _load_active_policy,
    extract_verifications,
)
from src.memory_detection_logging import record_detection_run

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

        # issue #1971 Part 3: one memory_detection_runs row per session scan
        # (never per-tick — a tick may scan zero-to-many sessions, and a
        # per-session row is what lets an admin correlate a policy edit with
        # the exact session it changed behavior on). run_started_at is
        # captured here, not at the top of the method, so the disabled- and
        # empty-session early returns above never log a run: no LLM call was
        # made, so there is nothing to report.
        run_started_at = datetime.now(timezone.utc)
        # Same live policy lookup extract_verifications() makes internally —
        # called again here (not plumbed through as a parameter) purely for
        # the run-log's policy_fingerprint, so extract_verifications' public
        # signature is untouched. Cheap: one repo lookup, once per session
        # scan, not a hot path.
        policy_text_for_fingerprint = _load_active_policy()

        # issue #1971 Part 6: corporate_memory.sources.session_transcripts.
        # max_turns_per_session was documented but never read — the
        # truncation window stayed pinned to extract_verifications' own
        # hardcoded default regardless of what an operator set here.
        # Invalid/non-positive values fall back to that default rather than
        # disabling truncation or raising.
        configured_max_turns = session_transcripts_config.get("max_turns_per_session")
        max_turns_kwargs = {}
        if isinstance(configured_max_turns, int) and configured_max_turns > 0:
            max_turns_kwargs["max_turns"] = configured_max_turns

        verifications = extract_verifications(self.extractor, username, session_id, turns, **max_turns_kwargs)
        items_proposed = len(verifications)
        items_filtered = 0

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
            items_filtered += dropped_count
            if dropped_count:
                logger.info(
                    "detection_types filter dropped %d/%d extracted verification(s) for %s (allowed=%s)",
                    dropped_count,
                    before_count,
                    session_key,
                    sorted(allowed_set),
                )

        items_created = 0
        items_routed_side_domain = 0
        # issue #1971 Part 5 — hoisted out of the loop below (it's the same
        # value every iteration; `import` is cheap once cached, but there is
        # no reason to re-resolve it per verification item).
        from src.db import ENGAGEMENT_SCOPED_DOMAIN_SLUG

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
                record_detection_run(
                    source="session_transcripts",
                    started_at=run_started_at,
                    finished_at=datetime.now(timezone.utc),
                    sessions_scanned=1,
                    items_proposed=items_proposed,
                    items_filtered=items_filtered,
                    items_inserted=items_created,
                    items_routed_side_domain=items_routed_side_domain,
                    policy_text=policy_text_for_fingerprint,
                    error=f"time budget exceeded on {session_key} after {idx}/{len(verifications)} items",
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

            # Confidence is computed in code from (source_type, detection_type),
            # UNAFFECTED by scope — the LLM is not trusted to set its own
            # credibility (Q3, docs/archive/pd-ps-comments.md) and routing is
            # an orthogonal, separately-deterministic decision (below).
            detection_type = v.get("detection_type")
            try:
                confidence_value = compute_confidence("user_verification", detection_type)
            except ValueError:
                # Unknown detection_type from the LLM; fall back to a
                # lookup-keyed default rather than the LLM-supplied value.
                confidence_value = compute_confidence("user_verification", "confirmation")

            # issue #1971 Part 5: `scope` is a MODEL-PROPOSED label; the
            # ROUTING it drives is deterministic code, never the model. An
            # "engagement" item is still created (route, never drop) — it
            # just lands in the dedicated engagement-scoped memory domain
            # instead of whichever topic domain (finance/engineering/…) the
            # model reported, so it never mixes into the general pool a
            # domain grant (or the default per-domain bundle/manifest) would
            # otherwise surface it through.

            domain_slug = v.get("domain")
            if v.get("scope") == "engagement":
                domain_slug = ENGAGEMENT_SCOPED_DOMAIN_SLUG
                items_routed_side_domain += 1

            repo.create(
                id=item_id,
                title=v["title"],
                content=v["content"],
                category="business_logic",
                source_user=username,
                tags=v.get("entities", []),
                status="pending",
                confidence=confidence_value,
                domain=domain_slug,
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
        record_detection_run(
            source="session_transcripts",
            started_at=run_started_at,
            finished_at=datetime.now(timezone.utc),
            sessions_scanned=1,
            items_proposed=items_proposed,
            items_filtered=items_filtered,
            items_inserted=items_created,
            items_routed_side_domain=items_routed_side_domain,
            policy_text=policy_text_for_fingerprint,
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
        # issue #1971 Part 6: corporate_memory.extraction.model was
        # documented but never read on this path either — mirrors the
        # collector's own override (services/corporate_memory/collector.py)
        # and the extraction.facts.model precedent. A copy, never a
        # mutation of the caller's own ai_config dict.
        model_override = (
            ((config.get("corporate_memory") or {}).get("extraction") or {}).get("model") if config else None
        )
        if model_override and ai_config:
            ai_config = {**ai_config, "model": model_override}
    except Exception:
        ai_config = None

    extractor = create_extractor_from_env_or_config(ai_config)
    return VerificationProcessor(extractor=extractor)


def dry_run_verification_detection(
    extractor: StructuredExtractor,
    *,
    limit: int = 5,
    session_data_dir: "Path | None" = None,
) -> dict:
    """Preview what the transcript detector WOULD do with the CURRENT saved
    policy, over up to ``limit`` currently-queued sessions (issue #1971 Part
    4, the body behind ``POST /api/memory/admin/detection-dry-run``).
    Writes NOTHING to ``knowledge_items``/``verification_evidence`` — records
    only a ``memory_detection_runs`` row (``dry_run=True``).

    "Currently queued" = the same candidate set
    ``services.session_pipeline.runner.run_processor`` would pick up on its
    next tick (``session_processor_state_repo().scan_unprocessed_for(...)``)
    — a preview of the NEXT real run, not an arbitrary "last N files on
    disk" that might re-show sessions already processed.

    Deliberately narrower than the real write path in
    :meth:`VerificationProcessor.process_session`: no duplicate/fuzzy-
    duplicate resolution and no contradiction detection run (both either
    write rows or spend an extra LLM call, and both are refinement on top of
    "would this land in the queue", not core to it) — so
    ``items_would_insert`` is an UPPER BOUND (assumes no item collides with
    one already in the review queue), not a guaranteed exact count.

    ``session_data_dir`` mirrors ``run_processor``'s own parameter; when
    omitted, ``SESSION_DATA_DIR`` is read fresh from the environment at call
    time (not the runner module's import-time-bound constant), matching the
    live-config-read convention ``process_session`` itself already uses.
    """
    import os

    from app.instance_config import get_corporate_memory_config
    from services.session_pipeline.runner import resolve_user_identity
    from src.repositories import session_processor_state_repo

    effective_dir = (
        session_data_dir
        if session_data_dir is not None
        else Path(os.environ.get("SESSION_DATA_DIR", "/data/user_sessions"))
    )
    limit = max(1, min(int(limit or 5), 20))
    run_started_at = datetime.now(timezone.utc)
    policy_text = _load_active_policy()

    cm_config = get_corporate_memory_config() or {}
    session_transcripts_config = (cm_config.get("sources") or {}).get("session_transcripts") or {}

    result: dict = {
        "sessions_scanned": 0,
        "items_proposed": 0,
        "items_filtered": 0,
        "items_would_insert": 0,
        "items_routed_side_domain": 0,
        "proposals": [],
        "disabled": False,
    }

    if session_transcripts_config.get("enabled", True) is False:
        result["disabled"] = True
        record_detection_run(
            source="session_transcripts",
            started_at=run_started_at,
            finished_at=datetime.now(timezone.utc),
            dry_run=True,
            policy_text=policy_text,
        )
        return result

    allowed_detection_types = session_transcripts_config.get("detection_types")
    allowed_set = set(allowed_detection_types) if allowed_detection_types is not None else None

    state_repo = session_processor_state_repo()
    candidates = state_repo.scan_unprocessed_for("verification", effective_dir, version=None)[:limit]

    knowledge = knowledge_repo()

    for dir_name, jsonl_path in candidates:
        turns = parse_jsonl(jsonl_path)
        if not turns:
            continue
        result["sessions_scanned"] += 1

        _uid, email = resolve_user_identity(dir_name)
        username = email or dir_name
        session_key = f"{dir_name}/{jsonl_path.name}"
        session_id = f"session-{jsonl_path.stem}-{username}"

        verifications = extract_verifications(extractor, username, session_id, turns)
        result["items_proposed"] += len(verifications)

        if allowed_set is not None:
            before = len(verifications)
            verifications = [v for v in verifications if v.get("detection_type") in allowed_set]
            result["items_filtered"] += before - len(verifications)

        for v in verifications:
            item_id = _generate_id(v.get("title", ""), v.get("content", ""))
            scope = v.get("scope") or "general"
            routed = scope == "engagement"
            if routed:
                result["items_routed_side_domain"] += 1
            would_insert = knowledge.get_by_id(item_id) is None
            if would_insert:
                result["items_would_insert"] += 1
            result["proposals"].append(
                {
                    "title": v.get("title"),
                    "detection_type": v.get("detection_type"),
                    "scope": scope,
                    "would_insert": would_insert,
                    "session": session_key,
                }
            )

    record_detection_run(
        source="session_transcripts",
        started_at=run_started_at,
        finished_at=datetime.now(timezone.utc),
        sessions_scanned=result["sessions_scanned"],
        items_proposed=result["items_proposed"],
        items_filtered=result["items_filtered"],
        items_inserted=0,  # dry run — nothing is ever actually inserted
        items_routed_side_domain=result["items_routed_side_domain"],
        dry_run=True,
        policy_text=policy_text,
    )
    return result
