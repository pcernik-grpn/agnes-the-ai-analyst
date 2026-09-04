"""Sync a user's Google Workspace group membership.

``fetch_user_groups`` is the read primitive: keyless Domain-Wide Delegation
fetches the user's Workspace groups via the Admin SDK. ``apply_user_groups``
combines that fetch with the same prefix-filter + system-group-mapping +
``user_group_members`` write the OAuth callback performs, so any caller —
the browser OAuth callback in ``app/auth/providers/google.py``, the
``POST /auth/refresh-groups`` endpoint, etc. — refreshes the snapshot the
same way.

Uses keyless Domain-Wide Delegation: the VM service account signs the
impersonation JWT via the IAM ``signJwt`` API (no private key on disk),
then exchanges that JWT for a short-lived OAuth token scoped to
``admin.directory.group.readonly``. The Admin SDK ``groups.list?userKey=``
endpoint returns the user's static AND dynamic group memberships in one
call.

Required GCP setup (one-off):

  - The VM SA grants itself ``roles/iam.serviceAccountTokenCreator`` so it
    can call ``IAMCredentials.signJwt`` for its own identity.
  - A Domain-Wide Delegation entry exists in admin.google.com → Security →
    API controls → Domain-wide Delegation, mapping the VM SA's numeric
    Unique ID to scope ``admin.directory.group.readonly``.

Required env on the VM:

  - ``GOOGLE_ADMIN_SDK_SUBJECT`` — the Workspace admin email the SA
    impersonates. Must be a real Workspace user with directory read
    privileges. When unset, this module fails soft and returns ``[]``.
  - ``GOOGLE_ADMIN_SDK_SA_EMAIL`` (optional) — explicit SA email override.
    When unset, the SA is auto-detected from the GCE metadata server, i.e.
    whichever SA the VM is currently running as. Useful off-VM (CI, tests).

Local dev / CI:

  Set ``GOOGLE_ADMIN_SDK_MOCK_GROUPS`` to a comma-separated list of group
  emails to bypass all Google calls. Empty value → empty list. Unset →
  the real keyless-DWD path.
"""

from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger(__name__)

# Bypass real API entirely. Comma-separated group emails. Empty → []. Unset →
# real keyless-DWD path.
MOCK_ENV = "GOOGLE_ADMIN_SDK_MOCK_GROUPS"

# Required: the Workspace admin email impersonated through DWD.
SUBJECT_ENV = "GOOGLE_ADMIN_SDK_SUBJECT"

# Optional: SA email override. When unset, auto-detect from GCE metadata.
SA_EMAIL_ENV = "GOOGLE_ADMIN_SDK_SA_EMAIL"

SCOPE = "https://www.googleapis.com/auth/admin.directory.group.readonly"

_METADATA_SA_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email"


def fetch_user_groups(email: str) -> List[str]:
    """Return the list of group emails ``email`` is a member of.

    Fail-soft: returns ``[]`` on any error (missing config, metadata server
    unreachable, API 4xx/5xx, network outage). The caller in the OAuth
    callback treats ``[]`` as "no data" and leaves the previous membership
    snapshot intact — so a transient outage does not wipe a user's groups.
    """
    mock = os.environ.get(MOCK_ENV)
    if mock is not None:
        return [g.strip() for g in mock.split(",") if g.strip()]
    return _fetch_real(email)


def _detect_sa_email() -> str | None:
    """Return the SA email this process should impersonate as.

    Order of resolution:
      1. ``GOOGLE_ADMIN_SDK_SA_EMAIL`` env var — explicit override.
      2. GCE metadata server — the SA the VM is attached to.

    Returns ``None`` when neither is available (off-VM with no override).
    """
    explicit = os.environ.get(SA_EMAIL_ENV, "").strip()
    if explicit:
        return explicit
    try:
        req = urllib.request.Request(
            _METADATA_SA_URL,
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.read().decode("ascii").strip()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return None


def _fetch_real(email: str) -> List[str]:
    try:
        from google.auth import default, iam
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        logger.warning("google-api-python-client / google-auth not installed; skipping group fetch")
        return []

    subject = os.environ.get(SUBJECT_ENV, "").strip()
    if not subject:
        logger.warning(
            "%s not set; skipping group fetch (keyless DWD requires an admin email to impersonate)",
            SUBJECT_ENV,
        )
        return []

    sa_email = _detect_sa_email()
    if not sa_email:
        logger.warning(
            "Could not determine VM service account email "
            "(metadata server unreachable and %s not set); "
            "skipping group fetch",
            SA_EMAIL_ENV,
        )
        return []

    try:
        source, _ = default()
        signer = iam.Signer(Request(), source, sa_email)
        creds = service_account.Credentials(
            signer=signer,
            service_account_email=sa_email,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=[SCOPE],
            subject=subject,
        )
        service = build(
            "admin",
            "directory_v1",
            credentials=creds,
            cache_discovery=False,
        )
    except Exception as e:  # noqa: BLE001 - fail-soft by design
        logger.warning("Admin SDK init failed: %s", e)
        return []

    groups: List[str] = []
    page_token: str | None = None
    try:
        while True:
            resp = (
                service.groups()
                .list(
                    userKey=email,
                    maxResults=200,
                    pageToken=page_token,
                )
                .execute()
            )
            for g in resp.get("groups", []):
                gid = g.get("email")
                if gid:
                    groups.append(gid)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except Exception as e:  # noqa: BLE001 - fail-soft by design
        logger.warning("Group fetch failed for %s: %s", email, e)
        return []

    return groups


# Env vars driving prefix filter + system-group mapping. Read per-call so
# operators can flip them via env without restarting the process; matches
# the OAuth callback's historical behavior.
#
# `AGNES_GROUP_EVERYONE_EMAIL` was the third one, mapping a Workspace group
# into the seeded `Everyone` row. Migration 0098 converted every instance
# that had it set into an ordinary synced group named after the email, so
# nothing reads the variable at runtime any more — it is inert if still set.
# `AGNES_GROUP_ADMIN_EMAIL` is unaffected: `Admin` is a capability, not an
# audience, and mapping it to a Workspace group narrows nothing.
PREFIX_ENV = "AGNES_GOOGLE_GROUP_PREFIX"
ADMIN_EMAIL_ENV = "AGNES_GROUP_ADMIN_EMAIL"


@dataclass
class SyncResult:
    """Outcome of an ``apply_user_groups`` call.

    Attributes:
      fetched: every group email returned by the Admin SDK (lowercased).
      relevant: subset that survived the prefix filter (lowercased).
      denied: True iff prefix is configured, fetch was non-empty, and
        zero groups matched the prefix. Caller decides what to do
        (OAuth callback redirects; refresh endpoint returns 403).
      soft_failed: True iff the Admin SDK returned an empty list. The
        OAuth callback treats this as "no change" rather than wiping the
        snapshot, since a transient API error and a genuinely-zero-groups
        user are indistinguishable. ``apply_user_groups`` mirrors that:
        no writes happen in this case.
      applied: True iff ``replace_google_sync_groups`` wrote rows. False
        when ``soft_failed`` or ``denied``.
    """

    fetched: List[str] = field(default_factory=list)
    relevant: List[str] = field(default_factory=list)
    denied: bool = False
    soft_failed: bool = False
    applied: bool = False


def apply_user_groups(user_id: str, email: str, conn) -> SyncResult:
    """Refresh this user's ``source='google_sync'`` group memberships.

    Same write path as the OAuth callback: fetch via DWD, apply prefix
    filter + admin/everyone mapping, then ``replace_google_sync_groups``.
    Splits out so the OAuth callback and the post-login refresh
    endpoint share one implementation. Fail-soft: any internal error
    (Admin SDK fetch, repo write, group ensure) is logged and returns a
    ``soft_failed=True`` result rather than raising — callers can treat
    the call as a no-op.

    Outcomes (mutually exclusive on a single call):

    - ``applied=True`` — fetch succeeded, write path ran. The user's
      ``source='google_sync'`` membership set now reflects ``relevant``
      (with admin/everyone mapping applied).
    - ``denied=True`` — fetch returned a non-empty set but the prefix
      filter dropped all of them. Existing rows are **NOT** cleared
      (prefix mismatches may be transient — Admin SDK lag, operator
      typo in ``PREFIX_ENV``); caller decides whether to act on it.
      OAuth callback turns this into a ``/login?error=not_in_allowed_group``
      redirect; refresh endpoint surfaces ``denied=True`` in the response.
    - ``soft_failed=True`` — fetch raised, fetch returned empty (Admin
      SDK quota / propagation), or the write path raised (DB lock /
      transient PG outage). Existing rows preserved.

    ``conn`` is accepted for backwards compatibility with the OAuth
    callback's pre-extraction signature, but the function routes its
    own reads/writes through the ``user_groups_repo()`` /
    ``user_group_members_repo()`` factory pair — so it respects the
    active state backend regardless of which engine ``conn`` happens
    to point at. The refresh endpoint relies on this for correct
    diff-computation on Postgres deploys (see PR #520 Devin review).

    The user-create / fanout side of the OAuth callback is NOT here —
    callers that need to mint a user run that themselves; this function
    assumes ``user_id`` already exists.
    """
    del conn  # unused — see docstring (backend selection via repo factories)
    from src.db import SYSTEM_ADMIN_GROUP
    from src.repositories import (
        user_group_members_repo,
        user_groups_repo,
    )

    result = SyncResult()

    prefix = os.environ.get(PREFIX_ENV, "").strip().lower()
    admin_email = os.environ.get(ADMIN_EMAIL_ENV, "").strip().lower()

    try:
        group_emails = fetch_user_groups(email)
    except Exception as e:  # noqa: BLE001 - fail-soft by design
        logger.warning("Group fetch raised for %s: %s", email, e)
        result.soft_failed = True
        return result

    if not group_emails:
        logger.info(
            "Google group sync for %s: empty result, preserving existing memberships",
            email,
        )
        result.soft_failed = True
        return result

    fetched = [g.lower() for g in group_emails]
    result.fetched = fetched

    if prefix:
        relevant = [g for g in fetched if g.startswith(prefix)]
    else:
        relevant = list(fetched)
    result.relevant = relevant

    if prefix and not relevant:
        # No matching groups → caller has lost their prefix-policy fit. We
        # do NOT clear the existing `source='google_sync'` rows here: the
        # OAuth callback turns this into a `/login?error=not_in_allowed_group`
        # redirect (user can't sign in, so RBAC state is moot), and the
        # refresh endpoint surfaces `denied=True` so the operator can act
        # on it. Aggressively wiping their RBAC on a transient prefix
        # mismatch (e.g. Admin SDK lag, operator typo in the prefix env
        # var) would risk locking the user out of work they own. Operators
        # who need the strict policy can run the prefix-cleanup admin path.
        logger.info(
            "Google group sync for %s denied: no group with prefix %r in %s",
            email,
            prefix,
            fetched,
        )
        result.denied = True
        return result

    # Group-ensure + write block. The OAuth callback's outermost handler
    # turns any uncaught exception here into `/login?error=oauth_failed`
    # — the user can't sign in at all — and the refresh endpoint turns
    # it into HTTP 500. The pre-extraction OAuth callback wrapped the
    # whole sync in a single `try/except Exception as sync_err` that
    # swallowed errors and let the user proceed with stale groups.
    # We preserve that fail-soft contract here so the docstring promise
    # ("any internal error is logged and returns soft_failed=True") and
    # the OAuth callback's "apply_user_groups never raises" comment stay
    # honest after the extraction. A transient `ug_repo.ensure()` /
    # `get_by_name()` hiccup (DuckDB write lock, PG connection drop)
    # downgrades to soft-fail rather than locking the user out.
    try:
        ug_repo = user_groups_repo()
        members_repo = user_group_members_repo()

        group_ids: list[str] = []
        for email_addr in relevant:
            if admin_email and email_addr == admin_email:
                sys_admin = ug_repo.get_by_name(SYSTEM_ADMIN_GROUP)
                if sys_admin:
                    group_ids.append(sys_admin["id"])
                continue
            # `AGNES_GROUP_EVERYONE_EMAIL` used to have a branch here,
            # routing its Workspace group's members into the seeded
            # `Everyone` row. That is what made "everyone" mean a SUBSET of
            # the instance on a mirrored deployment, while the marketplace's
            # own everyone meant all of it. Migration 0098 converts the
            # mapping to an ordinary group named after the Workspace email,
            # which is exactly what the fall-through below creates and keeps
            # in sync — so the mapping survives as a real audience and
            # "everyone" now always means every account.
            # Regular synced group: name = full email. ``ensure()`` is
            # get-or-create-by-name and stamps created_by='system:google-sync'
            # on first create.
            g = ug_repo.ensure(email_addr)
            group_ids.append(g["id"])

        members_repo.replace_google_sync_groups(
            user_id,
            group_ids,
            added_by="system:google-sync",
        )
    except Exception as e:  # noqa: BLE001 - fail-soft by design
        logger.warning("Group write failed for %s: %s", email, e)
        result.soft_failed = True
        return result

    result.applied = True
    logger.info(
        "Google group sync for %s: %d group(s) (filtered from %d fetched, prefix=%r) [%s]",
        email,
        len(group_ids),
        len(fetched),
        prefix,
        ", ".join(relevant),
    )
    return result


_everyone_missing_warned = False


def ensure_everyone_membership(user_id: str, added_by: str) -> bool:
    """Grant ``user_id`` a ``source='system_seed'`` row in the Everyone group.

    Unconditional since 0098. It used to no-op whenever
    ``AGNES_GROUP_EVERYONE_EMAIL`` was set, because on those instances the
    seeded ``Everyone`` row was mirrored from a Workspace group and a stray
    local membership would have fought the Workspace-authoritative set.
    The mapping is now an ordinary group of its own (see
    ``apply_user_groups``), so nothing mirrors ``Everyone`` any more and it
    can go back to meaning what it says on every instance.

    Called at CREATION time only, never re-asserted at login/boot — an
    admin who manually removes a member via the admin path stays removed.
    Callers only invoke this once, at creation, so it never re-adds a row
    an admin already deleted later.

    Returns ``True`` iff a membership write was attempted (the group
    existed and ``add_member`` ran) — not "row was newly inserted";
    ``add_member`` is itself idempotent. Returns ``False`` when the
    Everyone system group is missing (unhealthy install / a test fixture
    that never seeded system groups) — logged once at warning level so
    repeated calls during a single unhealthy run don't spam.

    Routes through the ``src.repositories`` factory functions exclusively
    (never the raw DuckDB system-connection getter, never direct repo
    instantiation) so the active backend (DuckDB or Postgres) is
    respected — required by the dual-backend contract and enforced by
    ``tests/test_backend_split_guard.py``.
    """
    global _everyone_missing_warned

    from src.db import SYSTEM_EVERYONE_GROUP
    from src.repositories import user_group_members_repo, user_groups_repo

    everyone = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
    if not everyone:
        if not _everyone_missing_warned:
            logger.warning(
                "ensure_everyone_membership: %r system group not found — "
                "skipping auto-grant for %s (subsequent occurrences logged "
                "at debug)",
                SYSTEM_EVERYONE_GROUP,
                user_id,
            )
            _everyone_missing_warned = True
        else:
            logger.debug(
                "ensure_everyone_membership: %r system group still not found — skipping auto-grant for %s",
                SYSTEM_EVERYONE_GROUP,
                user_id,
            )
        return False

    user_group_members_repo().add_member(
        user_id=user_id,
        group_id=everyone["id"],
        source="system_seed",
        added_by=added_by,
    )
    return True
