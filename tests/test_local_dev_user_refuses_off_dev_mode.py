"""``_get_local_dev_user`` answers None off LOCAL_DEV_MODE, in its own body.

The docstring promised this from the start; the body never checked. It looked
the configured address up and returned whatever it found, so on a deployment
that happens to hold an account at ``get_local_dev_email()`` the function
answered with a real principal in any mode.

That was survivable while every caller sat inside an ``if is_local_dev_mode():``
block. It stopped being survivable when ``app/auth/view_as.py`` started asking
this function whether a view-as ticket with no verifiable session behind it may
be accepted: at that point the single ``if`` above the call site became the only
thing standing between a stray configured account and an accepted bearer.

The existing coverage could not catch it. Every test of the configured-identity
fallback monkeypatches ``local_dev_viewer_id`` — they pin what the CONSUMERS do
with an answer, never that the function itself refuses to answer. So these
tests drive the real function, and one of them drives it through the security
consumer that made the gap matter.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def dev_user(tmp_path, monkeypatch, shared_app):
    """A seeded account at the configured dev address, mode OFF.

    The shape the old body got wrong: the row exists, so a lookup succeeds —
    only the mode says it must not be used.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.repositories import users_repo

    from app.auth import dependencies as deps

    monkeypatch.setattr(deps, "get_local_dev_email", lambda: "dev@local")
    users_repo().create(id="dev-user-1", email="dev@local", name="Dev")
    return deps


def test_it_refuses_when_the_mode_is_off_even_though_the_account_exists(dev_user, monkeypatch):
    deps = dev_user
    monkeypatch.setattr(deps, "is_local_dev_mode", lambda: False)
    assert deps._get_local_dev_user() is None


def test_the_positive_control_still_answers(dev_user, monkeypatch):
    """Without this the test above would pass just as happily if the lookup
    were broken outright, or the address never seeded — a green that asserts
    nothing about the guard."""
    deps = dev_user
    monkeypatch.setattr(deps, "is_local_dev_mode", lambda: True)
    user = deps._get_local_dev_user()
    assert user is not None
    assert user["email"] == "dev@local"


def test_the_view_as_fallback_declines_off_dev_mode(dev_user, monkeypatch):
    """Through the consumer that makes this a security primitive rather than a
    convenience: with the mode off, no configured identity is bindable, so a
    ticket cannot be honoured without a real session."""
    deps = dev_user
    monkeypatch.setattr(deps, "is_local_dev_mode", lambda: False)
    from app.auth import view_as as va_mod

    monkeypatch.setattr(va_mod, "is_local_dev_mode", lambda: False, raising=False)
    assert va_mod.local_dev_viewer_id() is None

    ticket = va_mod.ViewAsTicket("dev-user-1", "dev@local", "target-1", "t@x")
    assert va_mod.session_matches_viewer(None, ticket) is False
    assert va_mod.binding_is_possible(None) is False
