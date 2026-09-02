"""Shared first-login provisioning — the single write path for every auth
provider that auto-creates accounts (Google OAuth, Keboola OAuth).

Extracted verbatim from the Google callback so the four steps can never
drift apart per provider: create user → Everyone membership → v39
system-plugin fanout → deactivated-account rejection. (The Google-specific
Workspace group sync stays in google.py — it runs for returning users too
and is not provisioning.)
"""

import logging
import uuid

from src.repositories import users_repo
from src.user_identity import normalize_email

logger = logging.getLogger(__name__)


class UserDeactivatedError(Exception):
    """Raised when the identity maps to a deactivated Agnes account."""


def ensure_user(email: str, name: str, *, source: str) -> dict:
    """Return the user for ``email``, creating it on first login.

    ``source`` tags the Everyone-membership write (audit trail), e.g.
    ``"auth.google:first-signin"``.

    Raises :class:`UserDeactivatedError` for a deactivated account —
    callers translate that to their surface's 401/redirect.

    Identity is matched case-insensitively and stored normalized (stripped,
    lower-cased). Providers disagree on normalization — Microsoft lower-cases
    the resolved claim, Google passes the raw ``email`` claim through — and
    ``get_by_email`` is an exact string match on both backends, so without this
    the same person arriving through two providers (or through one IdP that
    changed a claim's casing) would end up on two accounts. Normalizing here
    rather than per provider is what makes every provider agree; the
    case-insensitive read still matches accounts created before this landed.

    ``get_by_email_ci`` is the ONLY lookup, deliberately. An exact-match read
    in front of it would win whenever the arriving claim matches the *newer*
    of two coexisting case variants — which is exactly the population the
    case-insensitive read exists for — and the documented "oldest wins" rule
    would never apply to it. Case is folded in SQL only (the argument is
    stripped, not lower-cased) so "equal" is one engine's definition rather
    than Python's and the engine's composed.
    """
    repo = users_repo()
    stripped = (email or "").strip()
    user = repo.get_by_email_ci(stripped) if stripped else None
    normalized = normalize_email(stripped)
    if not user:
        user_id = str(uuid.uuid4())
        email = normalized or email
        repo.create(id=user_id, email=email, name=name)
        # Issue #748: auto-grant Everyone at creation (source='system_seed')
        # unless AGNES_GROUP_EVERYONE_EMAIL maps Everyone to a Workspace
        # group. Creation-time only: never called again for a returning
        # user, so an admin's manual removal later sticks.
        try:
            from app.auth.group_sync import ensure_everyone_membership

            ensure_everyone_membership(user_id, added_by=source)
        except Exception:
            logger.exception("ensure_everyone_membership failed for new user %s", email)
        user = repo.get_by_email(email)
    if not bool(user.get("active", True)):
        raise UserDeactivatedError(email)
    return user
