"""One place where a completed sign-in becomes an ``audit_log`` row.

Every provider's success path has the same shape — resolve the identity, mint
a token, set the cookie, redirect — and each of the five used to reach the end
of it without writing anything down. ``login_failed`` was the only
authentication event in the trail, so an instance whose people sign in through
Google could not answer "who signed in, and when" at all, and the
invited → activated → signed-in lifecycle was recorded only at its first step.

Recording it here rather than at each success point is the part that lasts:
``tests/test_audit_login.py`` walks this package by source, so the sixth
provider cannot ship silently the way the first five did.

Client kind is the caller's to state. The browser flows are ``web``; the JSON
``/auth/password/login`` used by the CLI and desktop client is ``cli``, and an
audit trail that called that one a browser session would misrepresent a
non-interactive credential as a human clicking through the UI.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import Request

from src.audit_helpers import log_safe

# Sibling of the long-standing `login_failed`, which is why this is
# `login_success` rather than a bare `login` — the two read as a pair in the
# Activity Center's action facet.
LOGIN_SUCCESS = "login_success"

# The invite becoming a real account with a password of the person's own
# choosing. Distinct from `user.invite` (someone was asked) and from
# `login_success` (someone arrived) because only this one says the invite was
# actually consumed.
ACCOUNT_ACTIVATED = "account_activated"

# A setup link was minted and mailed on the self-service path. The HTTP
# response is deliberately identical whether or not the address matched, so
# this row is the only place the real outcome is visible.
SETUP_LINK_REQUESTED = "setup_link_requested"


def audit_auth_event(
    action: str,
    user_id: Optional[str],
    *,
    provider: str,
    request: Optional[Request] = None,
    client_kind: str = "web",
    result: str = "success",
    **extra: Any,
) -> None:
    """Write one authentication event. Never raises — ``log_safe`` owns that
    policy for the whole codebase, and a failed audit write must not fail the
    sign-in it describes."""
    from app.auth.client_ip import trusted_client_ip

    params: dict[str, Any] = {"provider": provider}
    params.update({k: v for k, v in extra.items() if v is not None})

    log_safe(
        user_id=user_id,
        action=action,
        resource="auth",
        params=params,
        result=result,
        client_ip=trusted_client_ip(request),
        client_kind=client_kind,
    )


def audit_login_success(
    user_id: Optional[str],
    *,
    provider: str,
    request: Optional[Request] = None,
    client_kind: str = "web",
    **extra: Any,
) -> None:
    """Someone finished signing in. Call on the success path, after the
    identity is resolved and before the response is returned."""
    audit_auth_event(
        LOGIN_SUCCESS,
        user_id,
        provider=provider,
        request=request,
        client_kind=client_kind,
        **extra,
    )
