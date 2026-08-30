"""TCRD-228: password managers must be able to fill and save on the auth forms.

Two participants on the same call, same login page — 1Password offered
credentials to one and nothing to the other. Two defects in the templates
explain the flakiness:

- The login identifier fields said ``autocomplete="email"``. Managers key
  login credentials on ``username`` (the spec's token for the account
  identifier, even when it is an email address); ``email`` is the token for
  contact forms, which managers fill from identities, not from logins.
- The setup and reset forms carried the account email only as
  ``<input type="hidden">``. A manager watching a ``new-password`` field with
  no visible username field has nothing to associate the password with — so
  the credential never gets SAVED at setup time, and the login form later has
  nothing to offer. The fix is a visible, readonly username field in the same
  form (readonly inputs still submit, so the POST contract is unchanged).

String-level assertions on the templates, same style as the design-system
contract tests — the forms are static Jinja, there is no runtime to probe.
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATES = Path("app/web/templates")


def _field(template: str, input_id: str) -> str:
    """The <input …> tag with the given id, as one string."""
    text = (TEMPLATES / template).read_text(encoding="utf-8")
    m = re.search(rf'<input[^>]*\bid="{input_id}"[^>]*>', text, re.S)
    assert m, f"{template}: no input with id={input_id}"
    return m.group(0)


class TestLoginIdentifierIsUsername:
    def test_password_login_email_field(self):
        f = _field("login_email.html", "email-signin")
        assert 'autocomplete="username"' in f, (
            "the login identifier must be autocomplete=username — managers "
            "key saved logins on it; 'email' is the contact-form token"
        )

    def test_magic_link_email_field(self):
        text = (TEMPLATES / "login_magic_link.html").read_text(encoding="utf-8")
        assert 'autocomplete="username"' in text

    def test_request_access_email_field(self):
        f = _field("login_email.html", "email-signup")
        assert 'autocomplete="username"' in f


class TestSetupAndResetExposeAUsernameToSaveAgainst:
    @staticmethod
    def _assert_visible_username(template: str):
        text = (TEMPLATES / template).read_text(encoding="utf-8")
        m = re.search(r'<input[^>]*name="email"[^>]*>', text, re.S)
        assert m, f"{template}: no email input at all"
        tag = m.group(0)
        assert 'type="hidden"' not in tag, (
            f"{template}: the email is hidden — a manager watching the "
            "new-password field has no username to associate the credential "
            "with, so nothing gets saved"
        )
        assert 'autocomplete="username"' in tag
        assert "readonly" in tag, (
            f"{template}: the email is token-bound — visible for the manager, readonly for the person"
        )

    def test_password_setup_form(self):
        self._assert_visible_username("password_setup.html")

    def test_password_reset_form(self):
        self._assert_visible_username("password_reset.html")
