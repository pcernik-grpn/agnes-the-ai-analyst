"""LLM security review — agentic verdict over the uploaded bundle.

Mirrors the corporate-memory extraction pattern: builds a prompt, calls
``StructuredExtractor.extract_json`` against a strict JSON schema, parses
the result. The model tier is configurable per-instance via
``guardrails.review_model`` (Haiku / Sonnet / Opus) — see
``app/instance_config.get_guardrails_review_model``.

Cost cap: single-shot, MAX_REVIEW_BYTES content payload, max_tokens
budget tuned for the schema. Retries inside the extractor handle
transient errors; a hard timeout (configured at the connector level)
bounds wall-clock cost.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from connectors.llm.anthropic_provider import AnthropicExtractor
from connectors.llm.exceptions import (
    LLMError,
)
from connectors.llm.factory import create_vertex_extractor, vertex_config_or_none

from .prompts import (
    REVIEW_JSON_SCHEMA,
    SYSTEM_PROMPT,
    build_review_prompt,
)

logger = logging.getLogger(__name__)

# Bound the response budget. The schema's two arrays (findings +
# content_quality.issues) are individually capped at maxItems=20, but
# each item is ~120-180 tokens (severity/category/file/explanation/
# fix_hint or file/field/issue/hint). A bundle with many weak
# descriptions can easily hit 4-5k output tokens. Stay generous on
# Haiku/Sonnet — output cost is negligible compared to the cost of a
# truncated verdict pinning the submission in `review_error`.
MAX_RESPONSE_TOKENS = 6000


def review_bundle(
    plugin_dir: Path,
    *,
    type_: str,
    name: str,
    version: str,
    description: str | None,
    api_key: str,
    model: str,
) -> dict[str, Any]:
    """Run the LLM review against the baked plugin tree.

    Returns a dict with the schema:
        ``{risk_level, summary, findings[], template_placeholders_found,
           reviewed_by_model, error}``

    ``error`` is set only when the LLM call itself failed — the runner
    surfaces this as ``status='review_error'`` and exposes a retry path
    in the admin UI. On success ``error`` is None and the schema fields
    are populated by the model.
    """
    user_payload = build_review_prompt(
        plugin_dir,
        type_=type_,
        name=name,
        version=version,
        description=description,
    )

    try:
        # Constructed inside the error boundary: the SDK import is deferred
        # to first use, so client construction is a failure point too — an
        # escaped exception here would bubble through BackgroundTasks and
        # pin the submission at pending_llm. An empty api_key is the
        # documented vertex sentinel from default_api_key_loader.
        extractor: AnthropicExtractor
        if not api_key and vertex_config_or_none():
            extractor = create_vertex_extractor(model)
        else:
            extractor = AnthropicExtractor(api_key=api_key, model=model)
        # Pass SYSTEM_PROMPT via the SDK's separate ``system=`` parameter
        # so a crafted README inside the uploaded bundle cannot override
        # the reviewer rules. The user-content payload wraps the bundle
        # files in <bundle>...</bundle> sentinels per the trust-boundary
        # paragraph in SYSTEM_PROMPT.
        result = extractor.extract_json(
            prompt=user_payload,
            system=SYSTEM_PROMPT,
            max_tokens=MAX_RESPONSE_TOKENS,
            json_schema=REVIEW_JSON_SCHEMA,
            schema_name="store_guardrails_review",
        )
    except LLMError as e:
        # Bubble up as a structured error the runner can persist into
        # store_submissions.llm_findings — operators see the failure
        # type without having to grep logs.
        logger.warning(
            "LLM guardrail review failed for %s/%s@%s: %s",
            type_,
            name,
            version,
            type(e).__name__,
        )
        return {
            "risk_level": None,
            "summary": None,
            "findings": [],
            "template_placeholders_found": 0,
            "reviewed_by_model": model,
            "error": f"{type(e).__name__}: {e}",
        }
    except Exception as e:
        # Catch-all: AnthropicExtractor only translates the four transient
        # error classes into LLMError. Anything else (BadRequestError on
        # schema mismatch, NotFoundError on a stale model ID, library
        # bugs) would otherwise bubble through BackgroundTasks and land
        # the request in the unhandled-error path while the submission
        # row stays stuck in 'pending_llm' forever. Convert here so the
        # admin queue surfaces a real review_error with a retry button.
        logger.exception(
            "LLM guardrail review unexpected error for %s/%s@%s",
            type_,
            name,
            version,
        )
        return {
            "risk_level": None,
            "summary": None,
            "findings": [],
            "template_placeholders_found": 0,
            "reviewed_by_model": model,
            "error": f"{type(e).__name__}: {e}",
        }

    # Defensive: ensure the keys we rely on exist with sane defaults even
    # if the model returns the optional ones empty. ``risk_level`` is
    # special-cased — defaulting it to "medium" would silently look like
    # a model decision and trigger an implicit block. Surface as an error
    # so the runner persists `status='review_error'` and the admin sees
    # a retry button.
    risk_level = result.get("risk_level")
    content_quality = _normalize_content_quality(result.get("content_quality"))
    if not risk_level:
        return {
            "risk_level": None,
            "summary": result.get("summary") or "",
            "findings": result.get("findings") or [],
            "template_placeholders_found": int(result.get("template_placeholders_found") or 0),
            "content_quality": content_quality,
            "reviewed_by_model": model,
            "error": "missing_risk_level",
        }
    return {
        "risk_level": risk_level,
        "summary": result.get("summary") or "",
        "findings": result.get("findings") or [],
        "template_placeholders_found": int(result.get("template_placeholders_found") or 0),
        "content_quality": content_quality,
        "reviewed_by_model": model,
        "error": None,
    }


def _normalize_content_quality(value: Any) -> dict[str, Any]:
    """Coerce the model's content_quality output to a stable shape.

    Missing or malformed content_quality is treated as pass — keeps
    backwards compatibility with older recorded verdicts and ensures a
    weird LLM response can't accidentally block all submissions. The
    safe-by-default-on-empty stance is intentional: hard blocking is
    the mechanical tier's job; the LLM tier is the substantive
    judgement layer.

    The verdict is treated as an aggregate of the evidence: if the
    model said `fail` with empty issues we downgrade to `pass` (no
    visible reason to block); if it said `pass` with non-empty issues
    we promote to `fail` (defense in depth — a compromised or
    prompt-injected model that flipped the verdict without zeroing the
    issues would otherwise sneak through). #277 LOW #2.
    """
    if not isinstance(value, dict):
        return {"verdict": "pass", "issues": []}
    verdict = value.get("verdict")
    if verdict not in {"pass", "fail"}:
        verdict = "pass"
    issues_raw = value.get("issues") or []
    issues: list = []
    if isinstance(issues_raw, list):
        for item in issues_raw:
            if not isinstance(item, dict):
                continue
            issues.append(
                {
                    "file": str(item.get("file") or ""),
                    "field": str(item.get("field") or "frontmatter.description"),
                    "issue": str(item.get("issue") or ""),
                    "hint": str(item.get("hint") or ""),
                }
            )
    # If verdict claims fail but no issues were enumerated, downgrade to
    # pass — we'd otherwise block a submission with no rendered reason.
    if verdict == "fail" and not issues:
        verdict = "pass"
    # Symmetric defense: if the model emitted issues but said pass,
    # promote to fail. The verdict must aggregate the evidence; a
    # compromised or prompt-injected model that flips the verdict
    # without zeroing the issues list would otherwise sneak through.
    # Issue #277 LOW finding #2.
    if verdict == "pass" and issues:
        verdict = "fail"
    return {"verdict": verdict, "issues": issues}


def is_safe(verdict: dict[str, Any]) -> bool:
    """Decide whether a review verdict permits publication.

    Pass condition: ``risk_level IN ('safe','low')`` AND no individual
    finding has severity ``high|critical`` AND ``content_quality.verdict
    == 'pass'``. We intentionally let ``medium`` findings through with a
    low-risk verdict — the model uses medium for "would benefit from
    review but no immediate exploit". Operators escalate to
    Sonnet/Opus if they want a stricter floor. Content quality is a
    hard gate: weak descriptions block, no severity scale.
    """
    if verdict.get("error"):
        return False
    risk = (verdict.get("risk_level") or "").lower()
    if risk not in {"safe", "low"}:
        return False
    for finding in verdict.get("findings") or []:
        sev = (finding.get("severity") or "").lower()
        if sev in {"high", "critical"}:
            return False
    content_quality = verdict.get("content_quality") or {}
    if (content_quality.get("verdict") or "pass").lower() == "fail":
        return False
    return True
