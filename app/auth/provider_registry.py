"""Per-instance auth provider allowlist (spec 2026-08-12, default flipped
2026-08 — B6 of the remediation program).

``auth.providers`` in instance.yaml (env override ``AGNES_AUTH_PROVIDERS``,
comma-separated) narrows which login methods this instance offers. Unset =
every available provider EXCEPT ``email``: the magic link's ``GET /auth/
email/verify`` consumes its single-use token on the request, so a corporate
mail scanner that opens the link before the human clicks silently burns it —
a structural UX flaw the magic link should never be handed to a user by
default. Every other provider (google/password/keboola/microsoft) keeps the
original "offered when configured" default; an operator who wants the magic
link back adds ``email`` to ``auth.providers`` explicitly (``**BREAKING**``:
an instance that relied on the implicit offering must do this after
upgrading). An explicitly empty (or all-unknown) list is a misconfiguration:
rejected at the admin API, and treated here as unset with a loud error log
so one overlay write can never lock every user out of the instance. A
narrower rescue applies at read time when the list names only
*unconfigured* providers (e.g. ``keboola`` with no stack configured): an
allowlist that would leave zero usable login methods falls back to
password + magic link — this rescue is unrelated to the unset-default above
and unaffected by it, since it only fires on an EXPLICIT, entirely-unusable
allowlist — so the env / static-file path — which the admin API's lockout
guard never sees — cannot lock the instance out either. Deliberately NOT
"treat as unset": that would re-offer the self-provisioning OAuth
providers, turning one typo into a widening of who may sign in.

A THIRD, narrower rescue protects an already-deployed instance from the
B6 default flip itself (Devin Review on PR #1548): one with SMTP configured,
no OAuth, and no user holding a password relied on the magic link as its
ONLY working door before this change — the unset default would otherwise
take that door away on upgrade with no runtime recovery path (the admin
API's write-time guard never ran, since nothing was ever saved to trigger
it, and the misconfiguration rescue above doesn't apply either, since the
value is unset rather than a narrowed list that resolves to nothing). See
:func:`_email_default_offering`.
"""

import importlib
import logging
import os
from typing import Callable, Optional

from fastapi import HTTPException

from app.instance_config import get_value

logger = logging.getLogger(__name__)

KNOWN_PROVIDERS: tuple[str, ...] = ("google", "email", "password", "keboola", "microsoft")

# Single-slot cache for the parsed allowlist, keyed by the raw configured value.
# See configured_allowlist() for why parsing + misconfig logging must not re-run
# per request.
_ALLOWLIST_CACHE: Optional[tuple[tuple, Optional[list[str]]]] = None

# Providers whose usability depends on instance configuration; ``password``
# needs none and is always usable. Mirrors the login page's per-provider
# ``is_available()`` probes.
_AVAILABILITY_PROBES: dict[str, str] = {
    "google": "app.auth.providers.google",
    "email": "app.auth.providers.email",
    "keboola": "app.auth.providers.keboola",
    "microsoft": "app.auth.providers.microsoft",
}

# What an unusable allowlist falls back to. Both require an existing user row
# to authenticate anybody (password: ``password_hash``; email: the magic link
# is only minted for a known address), so the fallback can never widen who may
# sign in — unlike "treat as unset", which re-offers the self-provisioning
# OAuth providers.
_RESCUE_PROVIDERS: tuple[str, ...] = ("password", "email")

# One-shot marker so the lockout rescue logs once per distinct configuration,
# not on every request (same rationale as the parse cache above).
_LOCKOUT_RESCUE_LOGGED: Optional[tuple] = None


def _probe_availability(name: str) -> tuple[bool, bool]:
    """``(available, probe_raised)`` for one provider.

    The second element is returned rather than recorded in module state: this
    runs from a FastAPI dependency on every ``/auth/*`` request, executed
    concurrently in the threadpool, so a shared marker would let one request's
    reset erase another's — precisely during the transient fault the signal
    exists to tolerate. See :func:`_provider_available` for the failure
    direction and :func:`_rescue_if_unusable` for what reads the flag.
    """
    module_path = _AVAILABILITY_PROBES.get(name)
    if module_path is None:
        return True, False
    try:
        return bool(importlib.import_module(module_path).is_available()), False
    except Exception:
        logger.warning("availability probe for provider %r raised; treating as unavailable", name, exc_info=True)
        return False, True


def _provider_available(name: str) -> bool:
    """Config-completeness of one provider (``password``: nothing to
    configure). Probes lazily and treats a raising probe as unavailable,
    matching the login page's try/except around the same calls.

    Raise-as-unavailable is a deliberate direction of failure: on a healthy
    instance these probes are in-memory config/env reads that do not raise,
    and if one somehow does, reading it as available would leave the login
    page offering only a provider that is actively broken — a lockout.
    :func:`_probe_availability` reports that it raised, though, so the rescue
    can tell
    "probed and found unconfigured" from "could not tell": since the rescue
    now NARROWS rather than widens, letting a transient fault trigger it would
    take the operator's intended door offline for the fault's duration on no
    information at all. Availability here gates OFFERING, never identity.
    """
    return _probe_availability(name)[0]


def configured_allowlist() -> Optional[list[str]]:
    raw_env = os.environ.get("AGNES_AUTH_PROVIDERS")
    if raw_env is not None:
        cache_key: tuple = ("env", raw_env)
        source: Optional[object] = raw_env
    else:
        source = get_value("auth", "providers")
        cache_key = ("cfg", repr(source))

    # Single-slot cache keyed on the raw configured value. `configured_allowlist`
    # runs on every /auth/* request (router dependency) and several times per
    # /login render, so parsing — and especially the misconfig `logger.warning`/
    # `logger.error` below — must not re-fire per request: a stale typo would
    # otherwise write duplicate lines on every page load. The key changes when
    # the env/config value changes (typical in tests), so the cache transparently
    # re-parses and the diagnostic is emitted once per distinct configuration.
    # Same pattern as `_LOCAL_DEV_GROUPS_CACHE` in app.auth.dependencies.
    global _ALLOWLIST_CACHE
    if _ALLOWLIST_CACHE is not None and _ALLOWLIST_CACHE[0] == cache_key:
        return _rescue_if_unusable(cache_key, _ALLOWLIST_CACHE[1])

    result = _parse_allowlist(source)
    _ALLOWLIST_CACHE = (cache_key, result)
    return _rescue_if_unusable(cache_key, result)


def _rescue_if_unusable(cache_key: tuple, allowlist: Optional[list[str]]) -> Optional[list[str]]:
    """Fall back to local sign-in when an allowlist names only unconfigured providers.

    ``auth.providers: [keboola]`` with no stack configured would render zero
    login buttons and 404 every ``/auth/*`` route — an unrecoverable lockout
    reachable via env/instance.yaml, which the admin API's write-time guard
    never sees (Devin Review on PR #1288). Availability is re-probed per call
    (NOT folded into the parse cache) because provider configuration can
    change at runtime via the settings overlay; the probes are cheap config
    reads and short-circuit on the first available provider. The error log is
    once per distinct configuration, like the parse diagnostics.

    The rescue lands on ``_RESCUE_PROVIDERS`` rather than on "unset", because
    "unset" means *every* provider and that turns a misconfiguration into a
    widening: an operator who narrowed to one OAuth provider and then mistyped
    its configuration would get Google back on the login page, and with
    ``auth.allowed_domain`` unset any Google account self-provisions. Password
    and magic link both need an existing user row, so they end the lockout
    without admitting anyone new."""
    if allowlist is None:
        return allowlist
    # Local, not shared: see `_probe_availability`. Short-circuits on the first
    # available provider exactly as before.
    any_raised = False
    for name in allowlist:
        available, raised = _probe_availability(name)
        if available:
            return allowlist
        any_raised = any_raised or raised
    if any_raised:
        # At least one probe could not answer. "Everything looks unavailable"
        # is then an artefact of the fault, not a statement about the
        # configuration — and rescuing would narrow the offering (404 on the
        # operator's intended door) for its duration. Leave the allowlist
        # alone; each provider is still gated by its own is_available() at the
        # route, so nothing broken becomes reachable, and the offering returns
        # to normal by itself when the fault clears.
        return allowlist
    global _LOCKOUT_RESCUE_LOGGED
    state = (cache_key, tuple(allowlist))
    if _LOCKOUT_RESCUE_LOGGED != state:
        _LOCKOUT_RESCUE_LOGGED = state
        # Say what the fallback can actually do, rather than asserting
        # reachability. Both rescue providers authenticate only EXISTING
        # accounts, and on an instance with no mail transport that leaves
        # password — which needs a row that already carries a hash. An
        # OAuth-only instance may therefore have no usable door until the
        # configuration is fixed, and the operator has to hear that here
        # rather than discover it on the login page.
        if _provider_available("email"):
            logger.error(
                "auth.providers names only unconfigured providers (%s) — no login method "
                "would be usable; falling back to %s. Neither can self-provision an "
                "account, so only EXISTING users can sign in until the configuration "
                "is fixed.",
                ", ".join(allowlist),
                ", ".join(_RESCUE_PROVIDERS),
            )
        else:
            logger.error(
                "auth.providers names only unconfigured providers (%s) AND no mail "
                "transport is configured — the fallback is password sign-in alone, "
                "which works only for accounts that already hold a password. If none "
                "do, NOBODY can sign in until the configuration is fixed; recover with "
                "`agnes admin break-glass grant-admin` (operates on the database "
                "directly, no login) or SEED_ADMIN_EMAIL/SEED_ADMIN_PASSWORD.",
                ", ".join(allowlist),
            )
    return list(_RESCUE_PROVIDERS)


def _parse_allowlist(source: Optional[object]) -> Optional[list[str]]:
    """Parse the raw ``auth.providers`` value into a known-provider allowlist,
    logging misconfiguration exactly once per distinct value (the caller caches
    on the raw value). ``None`` (unset, or set-but-all-unknown) ⇒ all providers."""
    if source is None:
        return None
    if isinstance(source, str):
        values = [v.strip() for v in source.split(",") if v.strip()]
    elif isinstance(source, (list, tuple, set)):
        values = [str(v).strip() for v in source if str(v).strip()]
    else:
        # A YAML scalar (auth.providers: true / 5) is not iterable. Treat it as
        # unset (all providers) with a loud error rather than letting a TypeError
        # propagate out of the per-request router dependency and 500 every login
        # page — the same fail-open contract as an all-unknown list.
        logger.error(
            "auth.providers must be a list or comma-separated string, got %s — treating as unset",
            type(source).__name__,
        )
        return None
    unknown = [v for v in values if v not in KNOWN_PROVIDERS]
    for name in unknown:
        logger.warning("auth.providers: unknown provider %r ignored", name)
    known = [v for v in values if v in KNOWN_PROVIDERS]
    if not known:
        logger.error(
            "auth.providers is set but names no known provider — treating as unset "
            "(all providers) so the instance stays reachable; fix the configuration"
        )
        return None
    return known


def _has_usable_password_holder() -> bool:
    """At least one real (non-scheduler) user holds a password hash.

    Mirrors the door computation in
    ``app.services.instance_doctor.check_login_door`` exactly (same
    scheduler-user exclusion — a synthetic account that cannot sign in
    interactively must not count as a working door), so the doctor's
    login-door check and this rescue can never disagree about whether
    password sign-in is actually usable. Not imported from there directly:
    ``instance_doctor`` already imports FROM this module
    (:func:`probe_providers`), so importing back would be circular.

    On any error this reads as "no holder" — a fail-open direction on
    purpose: this function backs a rescue whose whole job is to avoid
    losing a working door, so a transient DB fault should widen (keep email
    enabled) rather than narrow (exclude it) the offering. It is a Python
    exception in the DB read itself, so it's the same kind of failure
    :func:`_provider_available` reads as unavailable, not a resolved answer.
    """
    from app.auth.scheduler_token import SCHEDULER_USER_EMAIL
    from src.repositories import users_repo

    try:
        return any(u.get("password_hash") and u.get("email") != SCHEDULER_USER_EMAIL for u in users_repo().list_all())
    except Exception:
        logger.warning(
            "could not check for password holders (zero-door email rescue) — treating as none", exc_info=True
        )
        return False


def _other_login_door_usable() -> bool:
    """True when some door OTHER than email is genuinely usable under the
    unset default: a configured OAuth provider, or password sign-in with at
    least one holder.

    Deliberately does not call :func:`provider_allowed` (would recurse into
    the email rescue this function backs) or :func:`probe_providers` (would
    recompute email's own answer as a side effect); it probes only the
    non-email providers directly. Short-circuits on the first configured
    OAuth provider, so the DB read only ever runs when none is configured.
    """
    for oauth in ("google", "microsoft", "keboola"):
        if _provider_available(oauth):
            return True
    return _has_usable_password_holder()


# Tracks whether the zero-door email rescue is CURRENTLY active, so the
# warning logs once per activation rather than once ever — if the rescue
# later deactivates (an OAuth provider gets configured, a user sets a
# password) and then reactivates, the operator should hear about it again.
_ZERO_DOOR_EMAIL_RESCUE_ACTIVE: bool = False


def _email_default_offering() -> bool:
    """Whether the unset default excludes or keeps ``email``.

    Excludes it (the B6 contract) UNLESS ``email`` is both configured and
    the instance's only usable login door, in which case it stays enabled
    — see the module docstring's third rescue. The moment another door
    becomes usable the default exclusion re-applies on the very next call;
    there is nothing to "undo" since this holds no state beyond the log
    dedup marker above.
    """
    if not _provider_available("email"):
        return False
    if _other_login_door_usable():
        return False
    global _ZERO_DOOR_EMAIL_RESCUE_ACTIVE
    if not _ZERO_DOOR_EMAIL_RESCUE_ACTIVE:
        _ZERO_DOOR_EMAIL_RESCUE_ACTIVE = True
        logger.warning(
            "email magic link kept enabled as the only usable login door (no OAuth "
            "provider is configured and no user holds a password) — set auth.providers "
            "explicitly to silence this warning and control the offering yourself."
        )
    return True


def provider_allowed(name: str) -> bool:
    """Whether ``name`` is offered under the current ``auth.providers``.

    Unset allowlist (``None``) offers every provider EXCEPT ``email`` — see
    the module docstring for why the magic link is opt-in only, and
    :func:`_email_default_offering` for the narrow rescue when email is the
    only usable door. A configured allowlist (including the lockout
    rescue's own resolved list, which already names ``email`` when it
    applies) is checked by membership as before — the zero-door rescue
    never fires there, only on the unset path.
    """
    allowlist = configured_allowlist()
    if allowlist is None:
        if name == "email":
            return _email_default_offering()
        return True
    return name in allowlist


def probe_providers() -> list[dict]:
    """``[{name, allowed, available}]`` for every known provider.

    The offering the login page computes inline (five try/except blocks in
    ``app.web.router.login_page``), exposed as data so diagnostic surfaces
    (the new-instance doctor) can answer "which login doors are open" without
    growing another copy of the enumeration. A provider is *offered* when it
    is both allowed (post-rescue allowlist, same as the login page sees) and
    available (config-completeness probe; a raising probe reads as
    unavailable, matching :func:`_provider_available`).
    """
    return [
        {
            "name": name,
            "allowed": provider_allowed(name),
            "available": _probe_availability(name)[0],
        }
        for name in KNOWN_PROVIDERS
    ]


def require_provider(name: str) -> Callable[[], None]:
    """Router-level dependency: excluded provider endpoints return 404
    (not 403 — an excluded method should not advertise its existence)."""

    def _dep() -> None:
        if not provider_allowed(name):
            raise HTTPException(status_code=404, detail="Not Found")

    return _dep
