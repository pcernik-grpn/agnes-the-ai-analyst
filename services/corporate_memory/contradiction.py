"""Contradiction detection for corporate memory knowledge items.

Architecture (see docs/ADR-corporate-memory-v1.md, Decision 4):

  - Topic / content matching is performed by a single Haiku call with strict
    structured output. The brittle SQL keyword pre-filter that used to live
    here was removed.
  - Domain remains a hard SQL conjunct (cheap, scales with corpus size).
  - The same Haiku call returns the structured resolution suggestion —
    contradiction detection and resolution arrive in one shot.

Public API:

  detect_and_record(extractor, new_item, repo)
      -> list[str] of contradiction IDs persisted to knowledge_contradictions.

  find_and_judge(extractor, new_item, repo)
      -> list[dict] of contradiction records (not yet persisted).

The ``check_contradiction`` and ``check_contradictions`` helpers are retained
for callers that want the low-level path (single Haiku call + persistence
disabled).
"""

import logging

from connectors.llm import StructuredExtractor
from connectors.llm.exceptions import LLMError
# ``repo`` parameters below are duck-typed — accept both the DuckDB
# ``KnowledgeRepository`` and the Postgres ``KnowledgePgRepository``
# via the factory in ``src.repositories.knowledge_repo()``.

from .prompts import (
    BATCH_CONTRADICTION_PROMPT,
    BATCH_CONTRADICTION_SCHEMA,
    format_candidates_block,
)

logger = logging.getLogger(__name__)

# Hard cap on candidates per call — keeps prompt size bounded even if a
# domain accumulates a very large corpus. Above this, callers should shard.
DEFAULT_CANDIDATE_LIMIT = 100

# "merge" is the LLM-proposed action; the API resolution field uses "merged" — intentionally different terms
_VALID_ACTIONS = {"kept_a", "kept_b", "merge", "both_valid"}
_VALID_SEVERITIES = {"hard", "soft"}


def find_candidates(
    repo,
    new_item: dict,
    max_candidates: int = DEFAULT_CANDIDATE_LIMIT,
) -> list[dict]:
    """Load same-domain candidates for the LLM judge.

    Domain is the only SQL narrowing applied. If the new item has no domain,
    candidates from all domains are returned (the LLM still judges each).
    """
    item_id = new_item.get("id", "")
    domain = new_item.get("domain")
    candidates = repo.find_contradiction_candidates(
        new_item_id=item_id,
        domain=domain,
        limit=max_candidates,
    )
    if len(candidates) == max_candidates:
        # #63: the repo's `ORDER BY updated_at DESC LIMIT ?` silently dropped
        # older approved items once this domain's corpus outgrew the cap —
        # a fail-open on a contradiction *scanner*: it can report "no
        # contradictions" over a corpus it never fully read. This does NOT
        # fix that (domain sharding / the Batch API are a cost decision for
        # a human — see the "Consider domain sharding (V2 TODO)" warning
        # below), it only stops the truncation from being invisible.
        # TODO(#63): decide sharding vs. Batch API vs. raising the cap.
        logger.warning(
            "Contradiction candidate scan for domain %r hit the cap "
            "(returned %d candidates, limit=%d) — the corpus may hold more "
            "approved items than were scanned; items beyond the cap were "
            "not judged for contradictions.",
            domain,
            len(candidates),
            max_candidates,
        )
    return candidates


def _build_resolution(judgment: dict) -> dict | None:
    """Project the flat resolution_* fields into a nested resolution dict.

    Returns None when the judgment is not a contradiction or the LLM did not
    populate resolution fields.
    """
    action = judgment.get("resolution_action")
    if not judgment.get("is_contradiction") or not action:
        return None
    if action not in _VALID_ACTIONS:
        logger.warning("Invalid resolution_action from LLM: %r — dropping", action)
        return None
    resolution: dict = {
        "action": action,
        "justification": judgment.get("resolution_justification") or "",
    }
    if action == "merge":
        merged = judgment.get("resolution_merged_content")
        if merged:
            resolution["merged_content"] = merged
    return resolution


def _judgment_to_record(judgment: dict, new_item_id: str) -> dict:
    """Convert one Haiku judgment row into the contradiction record shape."""
    severity = judgment.get("severity")
    if severity not in _VALID_SEVERITIES:
        severity = None
    record: dict = {
        "item_a_id": new_item_id,
        "item_b_id": judgment["candidate_id"],
        "explanation": judgment.get("explanation", ""),
        "severity": severity,
    }
    resolution = _build_resolution(judgment)
    if resolution is not None:
        record["suggested_resolution"] = resolution
    return record


def find_and_judge(
    extractor: StructuredExtractor,
    new_item: dict,
    repo,
    max_candidates: int = DEFAULT_CANDIDATE_LIMIT,
) -> list[dict]:
    """Topic + judgment + resolution in one Haiku call.

    Loads same-domain candidates from SQL, then asks Haiku to judge each one
    in a single batched structured-output call. Returns contradiction records
    ready for persistence (item_a_id, item_b_id, explanation, severity,
    optional suggested_resolution dict).

    Empty corpus → returns immediately without an LLM call (cost guard).
    Hallucinated candidate_ids returned by Haiku are dropped.
    """
    candidates = find_candidates(repo, new_item, max_candidates)
    if not candidates:
        return []

    if len(candidates) > 50:
        logger.warning(
            "Contradiction candidate batch is large (%d items); prompt may exceed model context window. "
            "Consider domain sharding (V2 TODO).",
            len(candidates),
        )

    prompt = BATCH_CONTRADICTION_PROMPT.format(
        new_id=new_item.get("id", ""),
        new_domain=new_item.get("domain") or "unspecified",
        new_title=new_item.get("title", ""),
        new_content=new_item.get("content", ""),
        candidates_block=format_candidates_block(candidates),
    )

    from src.observability.llm_context import llm_context

    try:
        with llm_context(workload="corporate_memory", purpose="contradiction"):
            result = extractor.extract_json(
                prompt=prompt,
                max_tokens=4096,
                json_schema=BATCH_CONTRADICTION_SCHEMA,
                schema_name="batch_contradiction",
            )
    except LLMError as e:
        logger.error("Batch contradiction judgment failed: %s", e)
        return []

    judgments = result.get("judgments", [])
    valid_ids = {c["id"] for c in candidates}
    new_item_id = new_item.get("id", "")
    records: list[dict] = []
    for j in judgments:
        cid = j.get("candidate_id")
        if cid not in valid_ids:
            logger.warning(
                "Dropping judgment for unknown candidate_id %r (not in input set)",
                cid,
            )
            continue
        if not j.get("is_contradiction"):
            continue
        records.append(_judgment_to_record(j, new_item_id))
    return records


def detect_and_record(
    extractor: StructuredExtractor,
    new_item: dict,
    repo,
    max_candidates: int = DEFAULT_CANDIDATE_LIMIT,
) -> list[str]:
    """Run find_and_judge and persist the resulting contradictions.

    Returns the list of newly created contradiction IDs.
    """
    records = find_and_judge(extractor, new_item, repo, max_candidates)
    contradiction_ids: list[str] = []
    for r in records:
        cid = repo.create_contradiction(
            item_a_id=r["item_a_id"],
            item_b_id=r["item_b_id"],
            explanation=r["explanation"],
            severity=r.get("severity"),
            suggested_resolution=r.get("suggested_resolution"),
        )
        contradiction_ids.append(cid)
        logger.info(
            "Contradiction recorded: %s vs %s (severity=%s)",
            r["item_a_id"],
            r["item_b_id"],
            r.get("severity"),
        )
    return contradiction_ids


# ---------------------------------------------------------------------------
# Legacy single-pair helpers — kept so callers that want a one-off pairwise
# judgment (e.g. tests, ad-hoc tooling) can still use them. NOT used by the
# detector pipeline.
# ---------------------------------------------------------------------------


def check_contradiction(
    extractor: StructuredExtractor,
    item_a: dict,
    item_b: dict,
) -> dict:
    """Single-pair judgment via the batched API. Always returns a dict with
    ``contradicts`` (bool), ``explanation`` (str), and optionally
    ``severity`` and ``suggested_resolution`` (dict).
    """
    prompt = BATCH_CONTRADICTION_PROMPT.format(
        new_id=item_a.get("id", "new"),
        new_domain=item_a.get("domain") or "unspecified",
        new_title=item_a.get("title", ""),
        new_content=item_a.get("content", ""),
        candidates_block=format_candidates_block([item_b]),
    )
    from src.observability.llm_context import llm_context

    try:
        with llm_context(workload="corporate_memory", purpose="contradiction"):
            result = extractor.extract_json(
                prompt=prompt,
                max_tokens=1024,
                json_schema=BATCH_CONTRADICTION_SCHEMA,
                schema_name="batch_contradiction",
            )
    except LLMError as e:
        logger.error("Pair contradiction check failed: %s", e)
        return {"contradicts": False, "explanation": f"Check failed: {e}"}
    judgments = result.get("judgments", [])
    if not judgments:
        return {"contradicts": False, "explanation": "No judgment returned"}
    j = judgments[0]
    out: dict = {
        "contradicts": bool(j.get("is_contradiction")),
        "explanation": j.get("explanation", ""),
    }
    severity = j.get("severity")
    if severity in _VALID_SEVERITIES:
        out["severity"] = severity
    resolution = _build_resolution(j)
    if resolution is not None:
        out["suggested_resolution"] = resolution
    return out


def check_contradictions(
    extractor: StructuredExtractor,
    new_item: dict,
    repo,
    max_candidates: int = DEFAULT_CANDIDATE_LIMIT,
) -> list[dict]:
    """Backwards-compatible alias for find_and_judge."""
    return find_and_judge(extractor, new_item, repo, max_candidates)
