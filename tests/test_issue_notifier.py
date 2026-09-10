"""Operator mirror for issue reports — ``app/services/issue_notifier.py``.

One ``{"text": ...}`` POST per filed report, best-effort (never raises), to
whichever incoming webhook the operator configured (``issues.webhook_url`` /
env ``AGNES_ISSUES_WEBHOOK_URL``). The record in ``issue_reports`` stays the
source of truth regardless of delivery outcome.
"""

from __future__ import annotations

from app.services import issue_notifier as n


def _row(**over):
    base = {
        "id": "iss_abc",
        "number": 42,
        "kind": "bug",
        "title": "Tables render raw while streaming",
        "body": "During streaming I see raw | and --- until the answer completes. " * 10,
        "created_by_email": "analyst@example.com",
        "page_url": "https://agnes.example.com/chat?session=35b6",
        "context_json": {"app_version": "0.98.3", "app_commit": "95bc14b", "chat_session_id": "35b6"},
        "screenshot_path": "issues/iss_abc/screenshot.png",
    }
    base.update(over)
    return base


def test_text_names_number_kind_title_page_version_and_links():
    text = n.build_text(_row(), public_base_url="https://agnes.example.com")
    assert text.startswith("New issue #42 (bug) from analyst@example.com")
    assert "Tables render raw while streaming" in text
    assert "https://agnes.example.com/chat?session=35b6" in text
    assert "0.98.3 (95bc14b)" in text
    assert "https://agnes.example.com/api/issues/iss_abc/screenshot" in text
    assert "agnes admin issue show 42" in text
    excerpt_line = next(line for line in text.splitlines() if line.startswith("> "))
    assert len(excerpt_line) <= 305 and "\n" not in excerpt_line


def test_no_screenshot_line_without_screenshot():
    assert "Screenshot:" not in n.build_text(_row(screenshot_path=None), public_base_url="https://x")


def test_unconfigured_webhook_is_a_noop(monkeypatch):
    monkeypatch.delenv("AGNES_ISSUES_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(n, "_config_value", lambda: "")
    called = []
    monkeypatch.setattr(n, "post_webhook", lambda url, payload, **kw: called.append(url) or True)
    assert n.notify_issue_filed(_row(), public_base_url="https://x") is False
    assert called == []


def test_posts_text_payload_and_reports_delivery(monkeypatch):
    monkeypatch.setenv("AGNES_ISSUES_WEBHOOK_URL", "https://hooks.example.com/abc")
    seen = {}
    monkeypatch.setattr(n, "post_webhook", lambda url, payload, **kw: seen.update(url=url, payload=payload) or True)
    assert n.notify_issue_filed(_row(), public_base_url="https://x") is True
    assert seen["url"] == "https://hooks.example.com/abc" and set(seen["payload"]) == {"text"}


def test_env_beats_config(monkeypatch):
    monkeypatch.setenv("AGNES_ISSUES_WEBHOOK_URL", "https://env.example.com/h")
    monkeypatch.setattr(n, "_config_value", lambda: "https://cfg.example.com/h")
    assert n.issues_webhook_url() == "https://env.example.com/h"
