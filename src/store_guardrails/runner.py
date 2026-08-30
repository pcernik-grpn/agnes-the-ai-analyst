"""Orchestrator for the flea-market guardrail pipeline.

Two public entry points:

* :func:`run_inline_checks` — synchronous, runs manifest + static-security
  + quality+templating against a baked plugin tree. Returns an
  ``InlineResult`` the upload endpoint inspects to decide between
  ``blocked_inline`` and ``pending_llm``.

* :func:`run_llm_review` — async background-task entry point. Reads the
  pending submission row, calls the LLM reviewer, persists the verdict,
  and flips ``store_entities.visibility_status`` accordingly. Idempotent
  via the submission ID.

``run_llm_review`` resolves its repositories from the
``src.repositories`` factory, so it persists the verdict to whichever
backend the deployment runs on. The earlier DuckDB-only ``conn_factory``
path silently no-op'd ("submission vanished") on Postgres-backed
instances — the submission row lived in Postgres, the DuckDB handle was
empty — leaving the row stuck at ``pending_llm`` with no verdict.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import (
    content_check,
    llm_review,
    manifest_check,
    quality_check,
    static_scan,
)

logger = logging.getLogger(__name__)


@dataclass
class InlineResult:
    """Aggregate verdict from the inline (no-LLM) checks."""

    manifest: Dict[str, Any] = field(default_factory=dict)
    static_security: Dict[str, Any] = field(default_factory=dict)
    content: Dict[str, Any] = field(default_factory=dict)
    quality: Dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        # Quality is a soft check — it can ``warn`` but never ``fail``,
        # so we ignore its status here. Content is a hard fail
        # (per-component description floor) and joins manifest +
        # static-security as a blocking condition.
        return (
            self.manifest.get("status") == "pass"
            and self.static_security.get("status") == "pass"
            and self.content.get("status") == "pass"
        )

    def to_response_dict(self) -> Dict[str, Any]:
        """Shape for the structured 422 body returned to the uploader."""
        return {
            "manifest": self.manifest,
            "static_security": self.static_security,
            "content": self.content,
            "quality": self.quality,
        }


@dataclass
class LlmResult:
    """Aggregate verdict from the LLM security review."""

    verdict: Dict[str, Any] = field(default_factory=dict)
    reviewed_by_model: Optional[str] = None
    error: Optional[str] = None

    @property
    def passed(self) -> bool:
        if self.error:
            return False
        return llm_review.is_safe(self.verdict)


def run_inline_checks(
    plugin_dir: Path,
    *,
    type_: str,
    description: Optional[str],
) -> InlineResult:
    """Run the deterministic checks and aggregate the verdicts.

    Content check merges per-component issues with the submission-level
    description check — both go into ``content.issues`` so the rejection
    UI doesn't need a special case for "submission description failed
    AND a plugin agent description failed" combos.
    """
    content = content_check.check(plugin_dir)
    if type_ in ("skill", "agent"):
        # Skill/agent bakes write a SYNTHETIC .claude-plugin/plugin.json
        # whose description IS the submission description. An issue
        # attributed to that file points the submitter at a manifest they
        # never wrote (and must not add — the upload rejects ZIPs that
        # contain one). Drop it here; the submission-level check below
        # evaluates the same string and owns the actionable message.
        kept = [i for i in (content.get("issues") or []) if i.get("file") != ".claude-plugin/plugin.json"]
        content = {"status": "fail" if kept else "pass", "issues": kept}
    submission_desc = content_check.check_submission_description(description)
    if submission_desc.get("issues"):
        # Merge the submission-level issues into the same bag. Mark the
        # aggregate status fail if either component- or submission-level
        # check failed.
        if type_ in ("skill", "agent"):
            md_name = "SKILL.md" if type_ == "skill" else "the agent .md"
            for issue in submission_desc["issues"]:
                issue["hint"] = (
                    f"{issue.get('hint', '')} For {type_} uploads the tile "
                    f"description comes from the --description flag (or the "
                    f"description field in the upload form), falling back to "
                    f"{md_name} frontmatter `description`. Do not add a "
                    f".claude-plugin/plugin.json to the ZIP."
                ).strip()
        merged_issues = list(content.get("issues") or []) + list(submission_desc["issues"])
        content = {
            "status": "fail" if merged_issues else "pass",
            "issues": merged_issues,
        }

    return InlineResult(
        manifest=manifest_check.check(plugin_dir, type_),
        static_security=static_scan.scan_dir(plugin_dir),
        content=content,
        quality=quality_check.check(plugin_dir, description=description),
    )


# ---------------------------------------------------------------------------
# Async LLM review (BackgroundTasks entry point)
# ---------------------------------------------------------------------------


def run_llm_review(
    submission_id: str,
    *,
    plugin_dir: Path,
    api_key_loader: Callable[[], str],
    model_loader: Callable[[], str],
    conn_factory: Optional[Callable[[], Any]] = None,
    subs_repo: Any = None,
    ents_repo: Any = None,
    audit: Any = None,
    precheck_verdict: Optional[Dict[str, Any]] = None,
) -> LlmResult:
    """Background-task entry point. Resolves the LLM verdict and persists it.

    Side effects:
        * ``store_submissions.update_status`` — flips status to
          ``approved`` / ``blocked_llm`` / ``review_error`` based on the
          verdict.
        * ``store_entities.set_visibility`` — flips to ``approved`` on
          pass; stays ``pending`` on fail (admin can override or the
          uploader can retry).
        * ``audit_log`` — one row per outcome with the verdict in
          ``params``.

    Errors during the LLM call are recorded as ``review_error`` and
    surface a retry button in the admin UI. Errors during persistence
    propagate — those mean a bug in our DB layer and we want a stack.
    """
    # Resolve repositories from the factory so the verdict is read from /
    # written to whichever backend the deployment runs on. ``conn_factory``
    # is accepted for call-site/test compatibility but no longer used — the
    # old ``conn_factory=get_system_db`` path was DuckDB-only and made this
    # whole function a silent no-op on Postgres-backed instances (the
    # submission row lives in PG; the DuckDB handle is empty, so ``get()``
    # returned None → "submission vanished" → the row stuck at pending_llm
    # forever). Explicit repos can still be injected by tests.
    if subs_repo is None:
        from src.repositories import store_submissions_repo

        subs_repo = store_submissions_repo()
    if ents_repo is None:
        from src.repositories import store_entities_repo

        ents_repo = store_entities_repo()
    if audit is None:
        from src.repositories import audit_repo

        audit = audit_repo()
    try:
        sub = subs_repo.get(submission_id)
        if sub is None:
            logger.warning("run_llm_review: submission %s vanished", submission_id)
            return LlmResult(error="submission_missing")

        entity_id = sub.get("entity_id")
        type_ = sub["type"]
        name = sub["name"]
        version = sub.get("version") or ""
        submitter_id = sub.get("submitter_id")

        if precheck_verdict is not None and not precheck_verdict.get("error"):
            # Reuse the synchronous pre-check verdict — skip the Anthropic call.
            # Halves LLM cost per skill publish (pre-check + async review → pre-check only).
            # plugin_dir is not read in this branch so the exists() guard is not needed;
            # promote_to_version reads from the versioned bundle dir, not plugin_dir.
            verdict = precheck_verdict
            model = precheck_verdict.get("reviewed_by_model") or model_loader()
        else:
            if not plugin_dir.exists():
                # Bundle was deleted between accept + review (e.g. submitter
                # deleted their entity). Mark the submission so admins can
                # see the trail without a phantom approval.
                subs_repo.update_status(submission_id, status="review_error")
                audit.log(
                    user_id=submitter_id,
                    action="store.submission.review_error",
                    resource=f"store_submission:{submission_id}",
                    params={"reason": "plugin_dir_missing"},
                    result="error",
                )
                return LlmResult(error="plugin_dir_missing")

            try:
                api_key = api_key_loader()
                model = model_loader()
            except Exception as e:  # config error
                logger.exception("run_llm_review: failed to load LLM config")
                subs_repo.update_status(submission_id, status="review_error")
                audit.log(
                    user_id=submitter_id,
                    action="store.submission.review_error",
                    resource=f"store_submission:{submission_id}",
                    params={"reason": "llm_config_unavailable", "error": str(e)},
                    result="error",
                )
                return LlmResult(error=f"config:{e}")

            verdict = llm_review.review_bundle(
                plugin_dir,
                type_=type_,
                name=name,
                version=version,
                description=None,
                api_key=api_key,
                model=model,
            )

        if verdict.get("error"):
            subs_repo.update_status(
                submission_id,
                status="review_error",
                llm_findings=verdict,
                reviewed_by_model=model,
            )
            audit.log(
                user_id=submitter_id,
                action="store.submission.review_error",
                resource=f"store_submission:{submission_id}",
                params={"verdict": verdict},
                result="error",
            )
            return LlmResult(verdict=verdict, reviewed_by_model=model, error=verdict["error"])

        passed = llm_review.is_safe(verdict)
        if passed:
            written = subs_repo.update_status(
                submission_id,
                status="approved",
                llm_findings=verdict,
                reviewed_by_model=model,
            )
            if not written:
                # The row hit a terminal status (approved / overridden /
                # blocked_inline) before this BG verdict could land —
                # most commonly an admin Override fired while the LLM
                # call was running. Skip the entire downstream cascade
                # (visibility flip, version promote, "approved" audit
                # entry that would contradict the row) and log the
                # suppression instead so the operator timeline shows
                # the dropped verdict.
                audit.log(
                    user_id=submitter_id,
                    action="store.submission.bg_verdict_skipped",
                    resource=f"store_submission:{submission_id}",
                    params={
                        "attempted_verdict": "approved",
                        "reason": "submission already at terminal status (CAS no-op)",
                        "model": model,
                    },
                    result="skipped",
                )
                return LlmResult(verdict=verdict, reviewed_by_model=model)
            # Two outcomes are possible AND independent here:
            #
            # 1. Initial-upload (v1) approval flips visibility from
            #    'pending' to 'approved'. set_visibility_if_pending
            #    returns True. No promotion (v1 IS current).
            #
            # 2. v2+ edit/restore approval doesn't flip visibility
            #    (entity already 'approved' under deferred-promotion).
            #    set_visibility_if_pending returns False. We MUST
            #    still promote — copy the new version dir to live +
            #    bump entity.version_no/version/file_size.
            #
            # 3. Admin archived the row mid-flight: visibility =
            #    'archived'. set_visibility_if_pending returns False
            #    AND we must NOT promote. Detect via the row's
            #    current visibility, not via the flip's return value.
            #
            # The flip's return value alone can't distinguish (2)
            # from (3). Inspect the row's actual state to decide.
            visibility_flipped = False
            promoted_to: Optional[int] = None
            superseded_reason: Optional[str] = None
            if entity_id:
                visibility_flipped = ents_repo.set_visibility_if_pending(
                    entity_id,
                    "approved",
                )
                ent_row = ents_repo.get(entity_id) or {}
                current_visibility = ent_row.get("visibility_status")
                # Only promote when the entity is actually in a
                # serve-able state. Archive / hidden-by-admin paths
                # leave alone.
                if current_visibility == "approved":
                    # v48 (#329): attribution lookup is live — the next
                    # UsageProcessor tick preloads the approved entity by
                    # name against marketplace_plugins / store_entities;
                    # no cached attribution rows to refresh on promote.
                    # Look up THIS submission's version entry by
                    # submission_id, NOT by hash. Hash-based lookup
                    # breaks when a user re-uploads byte-identical
                    # bundles (e.g. v2 same content as v1): the loop
                    # picks the FIRST history entry with that hash
                    # (always v1, n=1), so `target_version_no` lands at
                    # 1 instead of the actual new entry's n. The
                    # forward-only `target > current` guard then skips
                    # the promote, leaving the entity stuck at v1.
                    # Surfaced live on a development deployment with
                    # an entity that had 5+ identical-hash history rows.
                    from app.api.store import _version_no_for_submission

                    target_version_no = _version_no_for_submission(
                        ent_row,
                        submission_id,
                    )
                    # Forward-only promotion. A late verdict landing for
                    # an older submission must NOT demote the live bundle
                    # past a version that was approved more recently.
                    if target_version_no is not None and target_version_no > int(ent_row.get("version_no") or 0):
                        # Atomic helper: swap live bundle first, then
                        # update the DB. Eliminates the
                        # "DB promoted but live still on prior bytes"
                        # window flagged by adversarial review.
                        from app.api.store import promote_to_version

                        promoted_to = promote_to_version(
                            entity_id,
                            target_version_no,
                            ents_repo,
                        )
                else:
                    # Entity left the serve-able states between BG
                    # task start + verdict-write. Record so admin
                    # triaging the queue sees why an "approved"
                    # verdict didn't change the live state.
                    superseded_reason = (
                        f"entity left review window before LLM verdict "
                        f"landed (current visibility: {current_visibility})"
                    )

            # Audit the verdict + the action taken.
            if superseded_reason:
                audit.log(
                    user_id=submitter_id,
                    action="store.submission.bg_verdict_skipped",
                    resource=f"store_submission:{submission_id}",
                    params={
                        "attempted_verdict": "approved",
                        "reason": superseded_reason,
                        "model": model,
                    },
                    result="skipped",
                )
            else:
                audit.log(
                    user_id=submitter_id,
                    action="store.submission.approved",
                    resource=f"store_submission:{submission_id}",
                    params={
                        "risk_level": verdict.get("risk_level"),
                        "model": model,
                        "visibility_flipped": visibility_flipped,
                        "promoted_to_version_no": promoted_to,
                    },
                    result="success",
                )
                from app.api.store import _notify_submitter

                _notify_submitter(sub, decision="approved")
        else:
            written = subs_repo.update_status(
                submission_id,
                status="blocked_llm",
                llm_findings=verdict,
                reviewed_by_model=model,
            )
            if not written:
                # CAS no-op: row hit a terminal status before this
                # verdict landed (admin override, prior terminal write).
                # See the parallel `approved` branch above — same
                # treatment: log the suppression, skip the misleading
                # "blocked_llm" audit entry, return early.
                audit.log(
                    user_id=submitter_id,
                    action="store.submission.bg_verdict_skipped",
                    resource=f"store_submission:{submission_id}",
                    params={
                        "attempted_verdict": "blocked_llm",
                        "reason": "submission already at terminal status (CAS no-op)",
                        "model": model,
                    },
                    result="skipped",
                )
                return LlmResult(verdict=verdict, reviewed_by_model=model)
            # On block, entity state depends on which path triggered
            # this submission:
            #
            # - Initial v1 upload: visibility='pending' (invisible to
            #   non-admins). Admin can override.
            # - v2+ edit / restore (deferred promotion): entity stays
            #   'approved' at the prior version. Existing installers
            #   keep receiving the previously approved bundle; nothing
            #   to roll back. The submission row carries the verdict
            #   so admin can Override + publish if the block was a
            #   false positive.
            audit.log(
                user_id=submitter_id,
                action="store.submission.blocked_llm",
                resource=f"store_submission:{submission_id}",
                params={
                    "risk_level": verdict.get("risk_level"),
                    "finding_count": len(verdict.get("findings") or []),
                    "model": model,
                },
                result="blocked",
            )
            from app.api.store import _notify_submitter

            _notify_submitter(sub, decision="blocked_llm")

        return LlmResult(verdict=verdict, reviewed_by_model=model)
    finally:
        # Repositories come from the factory — a process-wide DuckDB
        # singleton or a pooled Postgres engine. Neither is owned by this
        # call, so there is nothing instance-scoped to close here.
        pass


# Convenience: default factories the API layer wires up. Kept here so
# tests can swap in mocks without monkeypatching the API module.


def default_api_key_loader() -> str:
    """Read ANTHROPIC_API_KEY from the environment.

    The same env var the corporate-memory service uses; keeping a single
    convention so operators don't juggle multiple keys.

    Returns ``""`` (a documented sentinel, not an error) when the instance
    is configured for Vertex — that mode has no static key at all; the
    review call-sites build a VertexExtractor when they see it.
    """
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        # Fall back to LLM_API_KEY for parity with the factory's
        # backward-compat resolution path.
        key = os.environ.get("LLM_API_KEY", "").strip()
    if not key:
        from connectors.llm.factory import vertex_config_or_none

        if vertex_config_or_none():
            return ""
        raise RuntimeError(
            "Guardrail LLM review requires ANTHROPIC_API_KEY (or LLM_API_KEY) "
            "in the environment — or ai.provider: vertex in instance.yaml. "
            "Set one on the FastAPI container, or disable guardrails via "
            "instance.yaml: guardrails.enabled: false"
        )
    return key


def default_model_loader() -> str:
    """Resolve the configured model tier to a concrete Anthropic model ID."""
    from app.instance_config import get_guardrails_review_model

    return get_guardrails_review_model()
