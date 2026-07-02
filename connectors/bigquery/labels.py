"""BigQuery job labels for Foundry cost attribution (FAI-105).

Every Foundry-issued BQ job we control is tagged with a small, consistent
label set so usage is groupable per user / workload in
INFORMATION_SCHEMA.JOBS and the Cloud Billing export.

BigQuery label rules (enforced by ``_sanitize_label_value``): keys and
values are lowercase letters, digits, '-' or '_', max 63 chars. A label
whose value is empty after sanitization is dropped.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_WORKLOAD_TYPE = "foundryai"
# Any run of chars outside the BQ-allowed set collapses to a single '_'.
_INVALID = re.compile(r"[^a-z0-9_-]+")


def _sanitize_label_value(raw: str) -> str:
    """Coerce an arbitrary string into a valid BQ label value.

    Lowercase, replace every run of chars outside [a-z0-9_-] with '_',
    strip leading/trailing separators, truncate to 63. Returns '' when
    nothing valid remains (caller drops empty-valued labels).
    """
    if not raw:
        return ""
    s = _INVALID.sub("_", str(raw).lower()).strip("_-")
    return s[:63]


def _user_id_label(user: dict | None) -> str:
    """Sanitized email local-part for the requesting human user.

    Returns '' for no user or the scheduler service account — those jobs
    carry no user_id label (agent_name still conveys the path).
    """
    if not user:
        return ""
    # Local import avoids a module-load cycle (audit_helpers imports auth).
    from src.audit_helpers import client_kind_from_user

    if client_kind_from_user(user) == "scheduler":
        return ""
    identity = user.get("email") or user.get("id") or ""
    local_part = str(identity).split("@", 1)[0]
    return _sanitize_label_value(local_part)


def build_bq_job_labels(
    user: dict | None,
    agent_name: str,
    environment: str | None,
) -> dict[str, str]:
    """Build the BQ job-label dict for a Foundry-issued query.

    Pure + total: never raises. Applies BQ label rules and drops any
    label whose value is empty after sanitization.
    """
    try:
        labels: dict[str, str] = {"workload_type": _WORKLOAD_TYPE}
        agent = _sanitize_label_value(agent_name)
        if agent:
            labels["agent_name"] = agent
        env = _sanitize_label_value(environment or "")
        if env:
            labels["environment"] = env
        uid = _user_id_label(user)
        if uid:
            labels["user_id"] = uid
        return labels
    except Exception:  # totality: labeling must never break a query
        logger.warning("build_bq_job_labels failed; proceeding unlabeled", exc_info=True)
        return {}


def job_labels_for(user: dict | None, agent_name: str) -> dict[str, str]:
    """Read ``instance.environment`` from config and build the label dict.

    Defensive — returns {} on any failure so a labeling problem can never
    block a query. This is the entry point callsites use.
    """
    try:
        from app.instance_config import get_value

        environment = get_value("instance", "environment", default="") or ""
    except Exception:
        environment = ""
    return build_bq_job_labels(user, agent_name, environment)
