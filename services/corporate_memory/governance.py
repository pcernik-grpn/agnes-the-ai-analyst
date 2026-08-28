"""Shared approval-status resolution for Corporate Memory governance (#1573).

Two ingestion paths need to turn the ``corporate_memory.approval_mode``
config knob into an initial item status: the CLAUDE.local.md collector
(``services/corporate_memory/collector.py``) and the human-authored
``POST /api/memory`` endpoint (``app/api/memory.py``). Before #1573 only the
collector read ``approval_mode`` at all, and even there ``"threshold"``
silently fell through to the same branch as ``"review_queue"`` — there was
no confidence cutoff to compare against. Centralizing the decision here
means both ingestion paths — and any future one — get the same rules and
can't drift.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Cutoff used by approval_mode="threshold" when the instance hasn't set its
# own corporate_memory.auto_publish_min_confidence. Deliberately above every
# un-modified base score in confidence.py's defaults (claude_local_md=0.50,
# session_transcript=0.50, user_verification.confirmation=0.60) so opting
# into "threshold" without also tuning confidence stays conservative —
# items still land in the review queue until an admin raises a source's
# base confidence or lowers this cutoff.
DEFAULT_AUTO_PUBLISH_MIN_CONFIDENCE = 0.80

_KNOWN_APPROVAL_MODES = frozenset({"review_queue", "auto_publish", "threshold"})


def resolve_initial_status(
    governance_config: Optional[dict],
    confidence: Optional[float] = None,
) -> str:
    """Return ``"approved"`` or ``"pending"`` for a newly-ingested item.

    - No ``corporate_memory`` config at all: legacy mode (the feature is
      opt-in) — auto-approved, matching the documented "no admin review"
      default.
    - ``approval_mode == "auto_publish"``: always approved.
    - ``approval_mode == "threshold"``: approved when ``confidence`` is
      known and ``>= corporate_memory.auto_publish_min_confidence``
      (default :data:`DEFAULT_AUTO_PUBLISH_MIN_CONFIDENCE`); otherwise
      queued. An unknown confidence (``None``) always queues — never
      guesses a score to auto-publish on.
    - ``approval_mode == "review_queue"``, or any unrecognized value:
      queued. An unrecognized value is logged so a typo'd config setting
      is visible rather than silently degrading to review_queue.
    """
    if not governance_config:
        return "approved"

    approval_mode = governance_config.get("approval_mode", "review_queue")

    if approval_mode == "auto_publish":
        return "approved"

    if approval_mode == "threshold":
        cutoff = float(governance_config.get("auto_publish_min_confidence", DEFAULT_AUTO_PUBLISH_MIN_CONFIDENCE))
        if confidence is not None and confidence >= cutoff:
            return "approved"
        return "pending"

    if approval_mode not in _KNOWN_APPROVAL_MODES:
        logger.warning(
            "corporate_memory.approval_mode=%r is not recognized (expected one "
            "of %s) — treating new items as review_queue (pending)",
            approval_mode,
            sorted(_KNOWN_APPROVAL_MODES),
        )

    return "pending"
