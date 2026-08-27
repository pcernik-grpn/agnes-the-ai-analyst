"""SL010 — holistic LLM craft review.

The mechanical linter rules (SL002 bloat, SL011 trigger-phrase regex,
SL012 lexical duplicate recall — see ``skill_lint.py`` / ``lint_corpus.py``)
are cheap heuristics. This module is the substantive-judgement layer: one
LLM call per skill that judges trigger clarity, single-purpose-ness, and
confirms (or rejects) the lexical duplicate shortlist against the actual
purpose of the candidates.

Mirrors the invocation style of ``llm_review.review_bundle`` — same
``AnthropicExtractor`` construction, ``system=``/``prompt=`` split to keep
the trust boundary intact, defensive parsing of the verdict.

Two public entry points:

* :func:`craft_review` — the low-level function (explicit ``api_key``/
  ``model``). Its contract is unconditional: **any failure degrades to an
  empty finding list**, never a raised exception, never a synthetic
  "degraded" finding. This is what direct callers and tests exercise.
* :func:`default_craft_caller` — builds a ``CraftCaller`` (the 3-arg
  callable ``skill_lint.lint_skill`` accepts) bound to the instance's
  configured LLM, or returns ``None`` when no key is configured / the
  guardrails LLM provider isn't ready (mirrors the readiness gate used by
  ``app/api/store.py``'s dry-run endpoint).

Failure-path design note
-------------------------
``lint_skill`` needs to tell "the craft review ran and found nothing"
apart from "the craft review couldn't run at all" — the latter must fall
back to the degraded-mode SL011/SL012 heuristics, the former must not
(the whole point of SL010 is that it supersedes them). But ``craft_review``
itself is contractually not allowed to expose that distinction — its
return type is just ``list[LintFinding]``, and "any failure -> []" would
make a transient API outage indistinguishable from "the LLM confirmed the
skill is clean".

Both concerns are real, so they're split across two layers: the private
``CraftUnavailable`` exception is raised by the shared implementation
(``_craft_review_or_raise``) on any failure. ``craft_review()`` catches it
and returns ``[]`` (its documented contract). The ``CraftCaller`` returned
by ``default_craft_caller()`` calls the *raising* implementation directly
and lets ``CraftUnavailable`` propagate — ``skill_lint.lint_skill`` catches
it there, sets ``llm_used=False``, and runs SL011/SL012 as if ``craft``
had been ``None`` all along.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from connectors.llm.anthropic_provider import AnthropicExtractor
from connectors.llm.exceptions import LLMError
from connectors.llm.factory import create_vertex_extractor, vertex_config_or_none

from .lint_corpus import CorpusDoc
from .prompts import (
    CRAFT_REVIEW_JSON_SCHEMA,
    CRAFT_REVIEW_PROMPT,
    build_craft_review_prompt,
)

if TYPE_CHECKING:
    from .skill_lint import LintFinding

logger = logging.getLogger(__name__)

# The verdict schema is tiny (two booleans, one short string, one array of
# ids) — a generous ceiling still keeps output cost negligible while
# leaving headroom for a chatty trigger_rewrite sentence and a handful of
# duplicate ids.
MAX_RESPONSE_TOKENS = 800

CraftCaller = Callable[[Dict[str, Any], str, List[Tuple[CorpusDoc, float]]], List["LintFinding"]]


class CraftUnavailable(Exception):
    """Internal signal: the SL010 LLM call or verdict parsing failed.

    Raised only by ``_craft_review_or_raise``. ``craft_review()`` catches
    this and returns ``[]`` (its public contract). The ``CraftCaller``
    built by ``default_craft_caller()`` calls the raising implementation
    directly so ``skill_lint.lint_skill`` can catch this exception and
    fall back to the degraded-mode rules — see the module docstring.
    """


def _craft_review_or_raise(
    entity: Dict[str, Any],
    skill_md: str,
    candidates: List[Tuple[CorpusDoc, float]],
    *,
    api_key: str,
    model: str,
) -> List["LintFinding"]:
    prompt = build_craft_review_prompt(entity, skill_md, candidates)
    try:
        # Constructed inside the error boundary: the SDK import is deferred
        # to first use, so client construction must translate to
        # CraftUnavailable like any other LLM failure — craft_review()'s
        # Never-raises contract depends on it. An empty api_key is the
        # documented vertex sentinel from default_api_key_loader.
        extractor: AnthropicExtractor
        if not api_key and vertex_config_or_none():
            extractor = create_vertex_extractor(model)
        else:
            extractor = AnthropicExtractor(api_key=api_key, model=model)
        result = extractor.extract_json(
            prompt=prompt,
            system=CRAFT_REVIEW_PROMPT,
            max_tokens=MAX_RESPONSE_TOKENS,
            json_schema=CRAFT_REVIEW_JSON_SCHEMA,
            schema_name="store_guardrails_craft_review",
        )
    except LLMError as e:
        logger.warning("SL010 craft review LLM call failed: %s", type(e).__name__)
        raise CraftUnavailable(str(e)) from e
    except Exception as e:  # pragma: no cover - defensive catch-all
        logger.exception("SL010 craft review unexpected error")
        raise CraftUnavailable(str(e)) from e

    if not isinstance(result, dict):
        raise CraftUnavailable(f"non-dict verdict: {type(result).__name__}")

    return _findings_from_verdict(result, candidates)


def _findings_from_verdict(
    verdict: Dict[str, Any],
    candidates: List[Tuple[CorpusDoc, float]],
) -> List["LintFinding"]:
    """Map the raw verdict dict to findings, defensively.

    Mirrors ``llm_review._normalize_content_quality``'s stance: missing
    or wrong-typed fields degrade to "no finding" rather than raising or
    fabricating evidence. A hallucinated duplicate id (one the model
    wasn't actually offered as a candidate) is silently dropped — naming
    an id the operator can't look up would be worse than not flagging it.
    """
    findings: List["LintFinding"] = []

    trigger_clear = verdict.get("trigger_clear")
    if trigger_clear is False:
        trigger_rewrite = str(verdict.get("trigger_rewrite") or "").strip()
        message = "Description doesn't clearly state when to invoke this skill."
        if trigger_rewrite:
            message += f" Suggested rewrite: {trigger_rewrite}"
        findings.append(
            {
                "rule_id": "SL010",
                "severity": "warn",
                "message": message,
                "evidence": {"trigger_clear": False, "trigger_rewrite": trigger_rewrite},
                "doc_url": "/docs/skill-guidelines#sl010",
            }
        )

    single_purpose = verdict.get("single_purpose")
    if single_purpose is False:
        findings.append(
            {
                "rule_id": "SL010",
                "severity": "warn",
                "message": (
                    "Skill appears to bundle multiple unrelated purposes. "
                    "Consider splitting it into separate, single-purpose skills."
                ),
                "evidence": {"single_purpose": False},
                "doc_url": "/docs/skill-guidelines#sl010",
            }
        )

    duplicates_raw = verdict.get("duplicates")
    duplicate_ids: List[str] = []
    if isinstance(duplicates_raw, list):
        candidate_ids = {str(doc["id"]) for doc, _ in candidates}
        for item in duplicates_raw:
            item_id = str(item)
            if item_id in candidate_ids and item_id not in duplicate_ids:
                duplicate_ids.append(item_id)

    if duplicate_ids:
        findings.append(
            {
                "rule_id": "SL010",
                "severity": "warn",
                "message": (
                    "LLM review confirmed this skill duplicates existing "
                    f"marketplace skill(s): {', '.join(duplicate_ids)}."
                ),
                "evidence": {"duplicates": duplicate_ids},
                "doc_url": "/docs/skill-guidelines#sl010",
            }
        )

    return findings


def craft_review(
    entity: Dict[str, Any],
    skill_md: str,
    candidates: List[Tuple[CorpusDoc, float]],
    *,
    api_key: str,
    model: str,
) -> List["LintFinding"]:
    """Run the SL010 holistic craft review against a single skill.

    Returns up to three ``warn``-severity findings (trigger clarity,
    single-purpose, confirmed duplicates), each ``doc_url`` pointing at
    ``/docs/skill-guidelines#sl010``. An empty list means either "clean"
    or "the review couldn't run" — see the module docstring for why that
    ambiguity is intentional here and resolved one layer up, in
    ``default_craft_caller()`` / ``skill_lint.lint_skill``.

    Never raises: any failure (LLM error, non-JSON/malformed response,
    a hallucinated candidate id) degrades to ``[]``.

    WARNING: do NOT wire this function directly as ``lint_skill``'s
    ``craft=`` argument — its []-on-failure contract makes an LLM outage
    indistinguishable from a clean verdict, silently suppressing the
    SL011/SL012 fallback. Use ``default_craft_caller()`` for that wiring;
    the caller it returns raises ``CraftUnavailable`` on failure instead.
    """
    try:
        return _craft_review_or_raise(entity, skill_md, candidates, api_key=api_key, model=model)
    except CraftUnavailable:
        return []


def default_craft_caller() -> Optional[CraftCaller]:
    """Build a ``CraftCaller`` bound to the instance's configured LLM.

    Returns ``None`` — i.e. the linter degrades to SL011/SL012 — unless BOTH
    gates pass, exactly as ``app/api/store.py``'s dry-run endpoint gates its
    LLM security review:

    * ``get_guardrails_enabled()`` — the operator's master switch. Turning
      guardrails off is a documented way to stop spending LLM tokens; the
      linter must honour that intent rather than billing on every dry-run,
      publish, and weekly audit just because a key happens to be present.
    * ``get_guardrails_llm_provider_ready()`` — a usable provider/key exists.

    Unlike ``craft_review()``, the callable this returns lets
    ``CraftUnavailable`` propagate on failure (see module docstring) so
    ``skill_lint.lint_skill`` can distinguish "clean" from "unavailable".
    """
    from app.instance_config import get_guardrails_enabled, get_guardrails_llm_provider_ready

    if not (get_guardrails_enabled() and get_guardrails_llm_provider_ready()):
        return None

    from .runner import default_api_key_loader, default_model_loader

    try:
        api_key = default_api_key_loader()
        model = default_model_loader()
    except Exception:
        logger.exception("default_craft_caller: failed to resolve LLM config")
        return None

    def _caller(
        entity: Dict[str, Any],
        skill_md: str,
        candidates: List[Tuple[CorpusDoc, float]],
    ) -> List["LintFinding"]:
        return _craft_review_or_raise(entity, skill_md, candidates, api_key=api_key, model=model)

    return _caller


__all__ = ["CraftCaller", "CraftUnavailable", "craft_review", "default_craft_caller"]
