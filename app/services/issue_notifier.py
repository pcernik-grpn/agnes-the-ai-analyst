"""Operator mirror for issue reports: one ``{"text": ...}`` post per report.

The record in ``issue_reports`` is the source of truth; this is the copy that
lets a team start recording problems in the chat channel they already watch.
``issues.webhook_url`` (env ``AGNES_ISSUES_WEBHOOK_URL``) is deliberately a
different key from ``notifications.alert_webhook_url``: user reports would
drown watchdog and sync alerts. The URL is operator configuration, so it
takes the same posture as the alert webhook (no SSRF pinning); a
user-supplied URL must never reach this function — route it through
``app/chat/webhook_delivery.py`` instead.
"""

from __future__ import annotations

import logging
import os

from services.telegram_bot.sender import post_webhook

logger = logging.getLogger(__name__)

_EXCERPT_CHARS = 300


def _config_value() -> str:
    try:
        from app.instance_config import get_value

        return str(get_value("issues", "webhook_url", default="") or "")
    except Exception:  # noqa: BLE001 — config not loadable → no mirror, never a crash
        return ""


def issues_webhook_url() -> str:
    return (os.environ.get("AGNES_ISSUES_WEBHOOK_URL") or _config_value()).strip()


def _excerpt(body: str | None) -> str:
    flat = " ".join((body or "").split())
    return flat if len(flat) <= _EXCERPT_CHARS else flat[: _EXCERPT_CHARS - 1] + "…"


def build_text(row: dict, *, public_base_url: str) -> str:
    base = public_base_url.rstrip("/")
    ctx = row.get("context_json") or {}
    who = row.get("created_by_email") or row.get("created_by") or "unknown"
    lines = [f"New issue #{row['number']} ({row.get('kind', 'bug')}) from {who}", str(row.get("title", "")).strip()]
    excerpt = _excerpt(row.get("body"))
    if excerpt:
        lines.append(f"> {excerpt}")
    meta = []
    if row.get("page_url"):
        meta.append(f"Page: {row['page_url']}")
    version = ctx.get("app_version")
    if version:
        commit = ctx.get("app_commit")
        meta.append(f"Version {version} ({commit})" if commit else f"Version {version}")
    if ctx.get("chat_session_id"):
        meta.append(f"chat session {ctx['chat_session_id']}")
    if meta:
        lines.append("  ·  ".join(meta))
    if row.get("screenshot_path"):
        lines.append(f"Screenshot: {base}/api/issues/{row['id']}/screenshot  (login required)")
    lines.append(f"Show: agnes admin issue show {row['number']}")
    return "\n".join(lines)


def notify_issue_filed(row: dict, *, public_base_url: str) -> bool:
    """Post the summary; ``True`` only when the webhook answered 2xx."""
    url = issues_webhook_url()
    if not url:
        logger.debug("issues.webhook_url not configured; report %s kept in the instance only", row.get("id"))
        return False
    return bool(post_webhook(url, {"text": build_text(row, public_base_url=public_base_url)}))
