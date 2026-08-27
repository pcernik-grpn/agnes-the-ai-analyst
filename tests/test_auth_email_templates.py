"""Auth email templates + multipart transport (invite / reset / magic link).

Pins the redesign of the three auth emails: branded subject with the instance
name, HTML part with a CTA button and link validity, plaintext part carrying
the same copy, instance-named ``From:`` display name — replacing the bare
one-line plaintext ("Click to set up your password: <url>") that commonly
tripped spam filters.
"""

from datetime import timedelta
from email.utils import parseaddr

import pytest

from app.auth.email_templates import (
    human_ttl,
    invite_email,
    magic_link_email,
    reset_email,
)

LINK = "https://analyst.example.com/auth/password/setup?email=jan%40example.com&token=abc123"


@pytest.fixture
def named_instance(monkeypatch):
    monkeypatch.setattr("app.instance_config.get_instance_name", lambda: "Acme Analyst")


class TestHumanTtl:
    def test_days(self):
        assert human_ttl(timedelta(days=7)) == "7 days"

    def test_single_day_reads_as_hours(self):
        assert human_ttl(timedelta(days=1)) == "24 hours"

    def test_hours(self):
        assert human_ttl(timedelta(hours=24)) == "24 hours"

    def test_single_hour(self):
        assert human_ttl(timedelta(seconds=3600)) == "1 hour"

    def test_minutes(self):
        assert human_ttl(timedelta(minutes=15)) == "15 minutes"

    def test_mixed_falls_through_to_minutes(self):
        assert human_ttl(timedelta(hours=1, minutes=30)) == "90 minutes"


class TestTemplateCopy:
    """Each flow: subject names the instance, both parts carry link + validity."""

    def test_invite(self, named_instance):
        subject, text, html_body = invite_email("jan@example.com", LINK, timedelta(days=7))
        assert subject == "You've been invited to Acme Analyst"
        for part in (text, html_body):
            assert LINK in part or LINK.replace("&", "&amp;") in part
            assert "7 days" in part
        assert "Set up your account" in text
        assert "Set up your account" in html_body
        # Anti-phishing context: who this is for, where, and the opt-out.
        assert "jan@example.com" in text
        assert "analyst.example.com" in text
        assert "safely ignore" in text

    def test_reset(self, named_instance):
        subject, text, html_body = reset_email("jan@example.com", LINK, timedelta(hours=24))
        assert subject == "Reset your Acme Analyst password"
        assert "Choose a new password" in text
        assert "Choose a new password" in html_body
        assert "24 hours" in text
        assert "your password stays unchanged" in text

    def test_magic_link(self, named_instance):
        subject, text, html_body = magic_link_email("jan@example.com", LINK, timedelta(seconds=3600))
        assert subject == "Your sign-in link — Acme Analyst"
        assert "Sign in" in html_body
        assert "1 hour" in text

    def test_html_is_self_contained(self, named_instance):
        """No images and no external fetches — nothing for a client to block."""
        _, _, html_body = invite_email("jan@example.com", LINK, timedelta(days=7))
        assert "<img" not in html_body
        assert "http" not in html_body.replace(LINK.replace("&", "&amp;"), "")


class TestHtmlEscaping:
    """Operator config and caller-supplied text are data, not markup."""

    def test_instance_name_is_escaped(self, monkeypatch):
        monkeypatch.setattr(
            "app.instance_config.get_instance_name",
            lambda: 'Acme <script>alert("x")</script> & Co',
        )
        _, _, html_body = invite_email("jan@example.com", LINK, timedelta(days=7))
        assert "<script>" not in html_body
        assert "&lt;script&gt;" in html_body

    def test_email_address_is_escaped(self, named_instance):
        _, _, html_body = invite_email('"<b>x</b>"@example.com', LINK, timedelta(days=7))
        assert "<b>x</b>" not in html_body

    def test_link_is_attribute_escaped(self, named_instance):
        _, _, html_body = invite_email("jan@example.com", 'https://h.example/?a=1&b="2"', timedelta(days=7))
        assert 'href="https://h.example/?a=1&amp;b=&quot;2&quot;"' in html_body


class TestMultipartTransport:
    """send_smtp_email with body_html → multipart/alternative, named sender."""

    def _capture(self, monkeypatch, **kwargs):
        from app.auth._common import send_smtp_email

        sent: list = []

        class FakeSMTP:
            def __init__(self, host, port):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def starttls(self):
                pass

            def login(self, user, password):
                pass

            def send_message(self, msg):
                sent.append(msg)

        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
        send_smtp_email("to@example.com", "Subject", "plain text", **kwargs)
        assert len(sent) == 1
        return sent[0]

    def test_html_body_builds_multipart_alternative(self, monkeypatch):
        msg = self._capture(monkeypatch, body_html="<p>hi</p>")
        assert msg.get_content_type() == "multipart/alternative"
        parts = list(msg.iter_parts())
        # Plaintext first, HTML last — clients render the last part they support.
        assert [p.get_content_type() for p in parts] == ["text/plain", "text/html"]
        assert "plain text" in parts[0].get_content()
        assert "<p>hi</p>" in parts[1].get_content()

    def test_without_html_stays_plaintext(self, monkeypatch):
        msg = self._capture(monkeypatch)
        assert msg.get_content_type() == "text/plain"
        assert "plain text" in msg.get_content()

    def test_from_carries_instance_display_name(self, monkeypatch):
        monkeypatch.setenv("SMTP_FROM", "noreply@example.com")
        monkeypatch.setattr("app.instance_config.get_instance_name", lambda: "Acme Analyst")
        msg = self._capture(monkeypatch)
        name, addr = parseaddr(str(msg["From"]))
        assert addr == "noreply@example.com"
        assert name == "Acme Analyst"
