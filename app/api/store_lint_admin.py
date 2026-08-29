"""Admin surface for the skill linter (v89) — findings list, manual audit
trigger, and per-finding dismissal.

Advisory-only, admin-gated. Mirrors the pattern used by
``app/api/authoring_suggestions.py``'s ``admin_router``: a small
``APIRouter`` registered alongside its public sibling (here, the main
``/api/store`` router) in ``app/main.py``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.auth.access import require_admin
from app.instance_config import (
    get_lint_audit_min_interval_hours,
    get_lint_duplicate_top_n,
)
from src.audit_helpers import log_safe
from src.repositories import store_entities_repo, store_lint_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/store", tags=["store-lint-admin"])


class LintAuditBody(BaseModel):
    """Body for ``POST /lint-audit``. ``force`` bypasses the self-guard."""

    force: bool = False


class LintDismissBody(BaseModel):
    """Body for ``POST /lint-dismiss``."""

    entity_id: str
    rule_id: str


@router.get("/lint-findings")
async def list_lint_findings(
    include_dismissed: bool = False,
    admin: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Every entity's current-generation findings, plus the most recent
    audit run (of any trigger) for the admin UI's "last checked" chip."""
    repo = store_lint_repo()
    return {
        "findings": repo.all_latest_findings(include_dismissed=include_dismissed),
        "last_run": repo.last_run(),
    }


def _audit_due(last_run: Optional[Dict[str, Any]], min_interval_hours: int) -> bool:
    """Whether enough time has passed since ``last_run`` to run another audit.

    ``started_at`` comes back as a ``datetime`` — naive from DuckDB (plain
    ``TIMESTAMP``) or tz-aware from Postgres (``TIMESTAMPTZ``) — or,
    defensively, an ISO string. Treat a naive value as already being in UTC
    (matches how both backends populate the column via ``CURRENT_TIMESTAMP``)
    rather than assuming the local server timezone.
    """
    if last_run is None:
        return True
    started_at = last_run.get("started_at")
    if started_at is None:
        return True
    if isinstance(started_at, str):
        try:
            started_at = datetime.fromisoformat(started_at)
        except ValueError:
            return True
    if started_at.tzinfo is not None:
        now = datetime.now(timezone.utc)
    else:
        # Naive-UTC comparison: drop tzinfo from an aware "now" instead of
        # the deprecated datetime.utcnow().
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    return (now - started_at) >= timedelta(hours=min_interval_hours)


def _run_full_audit(trigger: str) -> Dict[str, Any]:
    """Blocking full-corpus lint pass — run off the event loop via
    ``run_in_threadpool``.

    Loads the duplicate-recall corpus and the craft caller ONCE and reuses
    both across every published skill (unlike the one-off dry-run / publish
    paths, which build their own per call). Entities whose content hash
    hasn't changed since the last ACTUAL lint are skipped and their existing
    findings are re-tagged to this run via ``carry_forward`` instead of
    being re-linted.
    """
    # Local import: avoids a module-level import cycle (app.api.store
    # imports from src.repositories / src.store_guardrails at module scope;
    # this module only needs store.py's baked-tree helpers, and only at
    # call time).
    from app.api.store import _find_skill_md, _plugin_dir
    from src.store_guardrails.craft_review import default_craft_caller
    from src.store_guardrails.lint_corpus import load_corpus, top_candidates
    from src.store_guardrails.skill_lint import compute_content_hash, lint_skill

    repo = store_lint_repo()
    run_id = repo.start_run(trigger)

    corpus = load_corpus()
    craft = default_craft_caller()
    top_n = get_lint_duplicate_top_n()

    linted = 0
    skipped = 0
    findings_count = 0

    try:
        items, _total = store_entities_repo().list(
            type="skill",
            visibility_status=["approved"],
            limit=100_000,
        )
    except Exception:
        logger.exception("lint audit: failed to list published skills")
        # Deliberately do NOT finish_run here: an audit that never got a
        # corpus to lint must not count as "we just audited", or the
        # self-guard would suppress retries for the whole min-interval
        # (default 6 days) after one transient failure. Leaving the run
        # unfinished keeps it out of last_full_audit_run(), so the next
        # scheduled attempt runs normally.
        return {
            "run_id": run_id,
            "trigger": trigger,
            "error": "failed_to_list_skills",
            "entities_linted": 0,
            "entities_skipped": 0,
            "findings_count": 0,
        }

    for item in items:
        entity_id = item.get("id")
        if not entity_id:
            continue
        try:
            skill_md_path = _find_skill_md(_plugin_dir(entity_id))
            if skill_md_path is None:
                continue
            skill_md = skill_md_path.read_text(encoding="utf-8", errors="replace")
            content_hash = compute_content_hash(skill_md)

            if repo.last_content_hash(entity_id) == content_hash:
                # Unchanged since the last ACTUAL lint — re-tag the
                # existing findings to this run instead of re-linting
                # identical bytes. NOTE: carry_forward intentionally does
                # NOT touch store_lint_entity_state (that row tracks the
                # last ACTUAL lint, not "was touched by a run"), so the
                # next audit still reads the same content_hash here.
                repo.carry_forward(entity_id, run_id)
                skipped += 1
                continue

            entity = {
                "id": entity_id,
                "name": item.get("name"),
                "description": item.get("description"),
                "type": "skill",
            }
            candidates = top_candidates(
                entity.get("name") or "",
                entity.get("description") or "",
                skill_md,
                corpus,
                n=top_n,
                exclude_id=entity_id,
            )
            report = lint_skill(
                entity,
                skill_md,
                plugin_dir=_plugin_dir(entity_id),
                candidates=candidates,
                craft=craft,
            )
            repo.replace_findings(entity_id, run_id, report["findings"], report["content_hash"])
            linted += 1
            findings_count += len(report["findings"])
        except Exception:
            logger.exception("lint audit: failed linting entity %s", entity_id)

    repo.finish_run(run_id, linted=linted, skipped=skipped, findings=findings_count)
    return {
        "run_id": run_id,
        "trigger": trigger,
        "entities_linted": linted,
        "entities_skipped": skipped,
        "findings_count": findings_count,
    }


@router.post("/lint-audit")
async def run_lint_audit(
    request: Request,
    body: LintAuditBody = LintAuditBody(),
    admin: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Run (or skip) a full-corpus lint audit.

    ``body`` defaults so a body-less POST (the scheduler sends none) is valid
    rather than 422.

    Self-guard: unless ``force``, refuses to re-run within
    ``get_lint_audit_min_interval_hours()`` of the most recent *full-audit* run
    (``scheduler``/``admin`` trigger only — per-publish runs are ignored, else
    routine publishing would perpetually reset the interval), to keep a
    misconfigured scheduler (or an admin mashing the button) from hammering the
    LLM craft-review tier.
    """
    repo = store_lint_repo()
    last = repo.last_full_audit_run()
    if not body.force and not _audit_due(last, get_lint_audit_min_interval_hours()):
        log_safe(
            user_id=admin.get("id"),
            action="run_store_lint_audit",
            resource="job:store-lint-audit",
            params={"skipped": True},
        )
        return {"skipped": True, "last_run": last}

    # Label automated vs manual runs. The scheduler sidecar sends only
    # `Authorization: Bearer $SCHEDULER_API_TOKEN` (no custom header — see
    # services/scheduler/__main__.py::_call_api), which the app resolves to the
    # synthetic scheduler user, so the principal's email is the reliable
    # signal. The header check stays as an override for other callers.
    from app.auth.scheduler_token import SCHEDULER_USER_EMAIL

    is_scheduler = bool(request.headers.get("X-Agnes-Scheduler")) or (
        (admin.get("email") or "").strip().lower() == SCHEDULER_USER_EMAIL
    )
    trigger = "scheduler" if is_scheduler else "admin"
    result = await run_in_threadpool(_run_full_audit, trigger)
    log_safe(
        user_id=admin.get("id"),
        action="run_store_lint_audit",
        resource="job:store-lint-audit",
        params={
            "trigger": trigger,
            "entities_linted": result.get("entities_linted"),
            "entities_skipped": result.get("entities_skipped"),
            "findings_count": result.get("findings_count"),
        },
    )
    return result


@router.post("/lint-dismiss")
async def dismiss_lint_finding(
    body: LintDismissBody,
    admin: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Dismiss one ``(entity_id, rule_id)`` finding.

    Keyed to the finding's current ``content_hash`` — a subsequent content
    change on the entity auto-resets the dismissal (the repo's
    ``is_dismissed``/``latest_findings(include_dismissed=False)`` compare
    the stored hash against the live one).
    """
    repo = store_lint_repo()
    findings = repo.latest_findings(body.entity_id, include_dismissed=True)
    match = next((f for f in findings if f["rule_id"] == body.rule_id), None)
    if match is None:
        raise HTTPException(status_code=404, detail="finding_not_found")
    dismissed_by = admin.get("email") or admin.get("id") or "admin"
    repo.dismiss(body.entity_id, body.rule_id, dismissed_by, match["content_hash"])
    return {"dismissed": True}
