"""Microsoft Graph change-notification SUBSCRIPTION lifecycle for SharePoint
connections — create, renew, delete.

Agnes has had the receiving half of near-real-time crawling for a while
(``app/api/sharepoint_webhooks.py``: the validation handshake, the
constant-time ``clientState`` check, the debounced ``corpus-extraction``
enqueue) and the secret-minting half (``app/api/admin_sharepoint.py::
rotate_webhook_secret``). What it did NOT have is the half that makes those
two reachable at all: something that actually tells Graph "push changes on
this drive to this URL". That was the retired external producer's
``subscriptions.py``, run by hand by an operator, which means on current
``main`` near-real-time crawling has a mailbox and a key but no way to have
the mail sent. THIS module is that missing half, moved inside Agnes.

**The contract.** :func:`ensure_subscriptions` is the single idempotent verb:
for every drive named by one of the connection's confirmed scopes it makes
the world match the intent — create a Graph subscription where there is
none, renew the one on record when it is nearing expiry, leave a healthy one
alone, and delete a subscription whose drive is no longer in scope.
:func:`remove_subscriptions` is the teardown. Both are safe to call
repeatedly; neither is ever partially fatal, because a drive is the unit of
failure (see "Per-drive isolation" below).

**State lives on the connection row**, in ``config["webhook_subscriptions"]``
— a list of ``{drive_id, subscription_id, expires_at, client_state_hash}``,
server-written, no new table (the same JSON column ``config.scopes`` /
``config.extraction`` / ``config.acl_sync_last_run`` already use). It NEVER
carries the ``clientState`` secret — only a one-way fingerprint of it, which
is how a secret ROTATION is detected and repaired (see
:func:`client_state_fingerprint`); the secret itself lives once, in
``config.webhook_secret``, and is sent to Graph at create time only. Writes go
through
``source_connections_repo().config_patch`` — which re-reads ``config`` inside
its own transaction — so a renewal sweep landing at the same moment as an
ACL sync can never drop the other's just-written key.

**Expiry.** Graph caps a drive subscription at 43 200 minutes (30 days). This
module asks for :data:`DEFAULT_SUBSCRIPTION_DAYS` (25) and renews anything
within :data:`DEFAULT_RENEW_WITHIN_HOURS` (72) of expiring — a five-day
margin, so a renewal sweep can miss several consecutive daily runs (a weekend
outage, a stuck scheduler) before a subscription actually lapses. An expired
record is not renewed but re-CREATED: Graph will not ``PATCH`` a subscription
it has already reaped, and a 404 on renewal is likewise re-created rather
than reported as a failure.

**Per-drive isolation.** One drive failing (a permission gap on that library,
a transient Graph 5xx, a malformed stored ``drive_id``) is recorded against
that drive and the walk continues. The return value counts every outcome
(``created``/``renewed``/``unchanged``/``removed``/``failed``/``skipped``) and
carries a per-drive row, so an admin sees exactly which library did not get a
subscription — never a whole-connection error that hides four successes.

**Gating mirrors the receiver.** Creating a subscription while
``sharepoint.enabled`` is off is refused (``sharepoint_disabled``),
because a notification Graph delivers to a 404 route is a
guaranteed-dead subscription that Graph will itself tear down after repeated
failures. Teardown carries no gate of its own — though through HTTP the
admin router's single `sharepoint.enabled` gate answers first, so an
operator cleans up before turning the connector off (or re-enables it
to do so); Graph itself reaps subscriptions whose receiver stops
answering.

**Graph facts this module encodes** (no live tenant exists in CI, so each is
stated rather than observed): a drive subscription is ``POST /subscriptions``
with ``changeType: "updated"`` over ``resource: "/drives/{id}/root"``;
renewal is ``PATCH /subscriptions/{id}`` carrying only a new
``expirationDateTime``; teardown is ``DELETE /subscriptions/{id}``; and Graph
validates ``notificationUrl`` SYNCHRONOUSLY inside the create call by issuing
the handshake ``app/api/sharepoint_webhooks.py`` already answers — which is
why the receiver's feature flag being on is a precondition for create, and
why the notification URL must be a public HTTPS origin
(:func:`resolve_notification_base_url`).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from connectors.sharepoint import graph_client
from connectors.sharepoint.graph_client import SharePointGraphError
from connectors.sharepoint.settings import SharePointSettingsError, resolve_sharepoint_settings
from src.repositories import source_connections_repo

logger = logging.getLogger(__name__)


#: Key THIS module writes into a SharePoint connection's ``config``. Carried
#: forward by hand in ``app/api/admin_source_connections.py::
#: update_connection`` — imported from here so the two lists cannot drift —
#: for exactly the reason ``connectors.sharepoint.acl_sync
#: .ACL_SYNC_SERVER_WRITTEN_CONFIG_KEYS`` is: that endpoint replaces
#: ``config`` WHOLESALE, so an unrelated edit (a rename, a certificate swap)
#: would otherwise silently erase this connection's subscription bookkeeping,
#: orphaning live Graph subscriptions Agnes could then neither renew nor
#: delete. Deliberately NOT added to ``app/api/admin_sharepoint.py``'s
#: ``SHAREPOINT_SERVER_WRITTEN_CONFIG_KEYS``: that tuple is ratcheted by a
#: static scan of THAT file's own writers
#: (``tests/test_sharepoint_config_carry_forward_ratchet.py``), and a key
#: written from this module would fail it in the "declared but no writer
#: found" direction.
SUBSCRIPTION_SERVER_WRITTEN_CONFIG_KEYS = ("webhook_subscriptions",)

#: The config key itself, named once.
SUBSCRIPTION_STATE_KEY = "webhook_subscriptions"

#: Requested subscription lifetime. Graph's ceiling for a drive subscription
#: is 30 days; 25 leaves the renewal sweep five days of headroom to be down
#: without anything lapsing. Override per deployment with
#: ``AGNES_SP_SUBSCRIPTION_DAYS`` — deliberately an ENV knob rather than an
#: ``sharepoint:`` yaml key, because that section is declared in
#: ``app/api/admin.py::_SECTION_BASELINE_EFFECT`` as having exactly one key
#: (its switch) and the settings panel renders it from that declaration.
DEFAULT_SUBSCRIPTION_DAYS = 25

#: Renew anything expiring within this window. The sweep runs daily, so this
#: is "three consecutive misses are survivable", not a tuning knob.
DEFAULT_RENEW_WITHIN_HOURS = 72

#: Graph's own documented maximum for a drive subscription (43 200 minutes).
#: A configured lifetime above this is clamped rather than sent — Graph would
#: reject the create outright, and a refused create reads to an admin as
#: "subscriptions are broken" rather than "your knob is out of range".
_GRAPH_MAX_SUBSCRIPTION_DAYS = 30

#: A stored ``drive_id`` is admin-supplied (it arrives on a confirmed scope
#: row) and goes straight into a Graph resource path, so it is untrusted in
#: exactly the sense the security playbook means. Same character class as
#: ``app/api/admin_sharepoint.py::_GRAPH_ID_RE`` — notably never ``/``, the
#: one character that would let a value escape its own path segment.
_GRAPH_ID_RE = re.compile(r"^[A-Za-z0-9!_.,:=-]+$")

#: Hosts that can never be what Graph calls back. A ``notificationUrl`` on
#: one of these produces a create that fails opaquely inside Graph's
#: validation call, minutes after the admin clicked the button, so it is
#: refused up front with a message naming the fix.
_NON_PUBLIC_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"})


class SubscriptionError(RuntimeError):
    """A subscription operation was REFUSED — a configuration fact the
    caller must fix, never a transport fault (those are per-drive rows in
    the result, or :class:`SharePointGraphError`).

    ``error`` is the stable machine code an API layer returns as
    ``detail["error"]``; ``message`` names the exact fix; ``status_code`` is
    the HTTP status that code deserves (409 for "configure something first",
    502 for "the upstream rejected us").
    """

    def __init__(self, error: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.error = error
        self.message = message
        self.status_code = status_code

    def as_detail(self) -> Dict[str, str]:
        return {"error": self.error, "message": self.message}


# ---------------------------------------------------------------------------
# Configuration / URL resolution
# ---------------------------------------------------------------------------


def _int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    """A positive-int env override, clamped, never raising on garbage — a bad
    value falls back to the default rather than taking the renewal sweep
    down with it."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("sharepoint subscriptions: ignoring non-integer %s=%r", name, raw)
        return default
    return max(minimum, min(maximum, value))


def subscription_days() -> int:
    """Requested lifetime in days — :data:`DEFAULT_SUBSCRIPTION_DAYS`, or
    ``AGNES_SP_SUBSCRIPTION_DAYS``, clamped to Graph's own 30-day ceiling."""
    return _int_env(
        "AGNES_SP_SUBSCRIPTION_DAYS", DEFAULT_SUBSCRIPTION_DAYS, minimum=1, maximum=_GRAPH_MAX_SUBSCRIPTION_DAYS
    )


def renew_within_hours() -> int:
    """How close to expiry a subscription must be before a sweep renews it —
    :data:`DEFAULT_RENEW_WITHIN_HOURS`, or
    ``AGNES_SP_SUBSCRIPTION_RENEW_WITHIN_HOURS``."""
    return _int_env("AGNES_SP_SUBSCRIPTION_RENEW_WITHIN_HOURS", DEFAULT_RENEW_WITHIN_HOURS, minimum=1, maximum=24 * 29)


def resolve_notification_base_url(request: Any = None) -> str:
    """The public origin Graph will call back, with no trailing slash.

    Reuses the ONE public-origin resolution this codebase already has —
    ``app.auth.public_url.public_base_url`` (``AGNES_BASE_URL`` →
    ``SERVER_URL`` → the incoming request's own base URL → a localhost dev
    fallback), the same function that builds OAuth redirect URIs, invite-mail
    links and the ``webhook_url`` ``rotate_webhook_secret`` already hands an
    operator — and then REFUSES what Graph cannot reach.

    The refusal is the point. ``public_base_url`` is designed to always
    answer something so a dev box still renders links; a
    ``notificationUrl`` of ``http://localhost:8000/...`` is not a degraded
    link, it is a subscription that Graph fails to validate several seconds
    into the create call with an error that names none of this. So: HTTPS
    only, a real host, never a loopback, never a single-label internal name.
    """
    from app.auth.public_url import public_base_url

    base = public_base_url(request=request).rstrip("/")
    parsed = urlsplit(base)
    host = (parsed.hostname or "").lower()
    fix = (
        "Set AGNES_BASE_URL (or SERVER_URL) to this instance's public HTTPS origin — "
        "Microsoft Graph calls the notification URL back during subscription creation, "
        "so it must be publicly reachable from the internet."
    )
    if parsed.scheme != "https":
        raise SubscriptionError("public_url_not_configured", f"public URL {base!r} is not https. {fix}")
    if not host or host in _NON_PUBLIC_HOSTS or host.endswith(".localhost") or "." not in host:
        raise SubscriptionError("public_url_not_configured", f"public URL {base!r} is not publicly resolvable. {fix}")
    return base


def receiver_url(connection_id: str, *, base_url: str) -> str:
    """The ``app/api/sharepoint_webhooks.py`` route for THIS connection —
    assembled in one place so the URL Graph is told about and the URL the
    receiver actually serves can never drift apart."""
    return f"{base_url}/api/webhooks/sharepoint/{connection_id}"


# ---------------------------------------------------------------------------
# State on the connection row
# ---------------------------------------------------------------------------


def subscription_state(connection: Dict[str, Any]) -> List[Dict[str, Any]]:
    """This connection's recorded subscriptions, defensively normalized —
    the value is a JSON column an older/hand-edited row may have anything
    in, and a malformed entry must degrade to "no record for that drive"
    (which ensure re-creates) rather than crash the sweep."""
    raw = (connection.get("config") or {}).get(SUBSCRIPTION_STATE_KEY)
    if not isinstance(raw, list):
        return []
    rows: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        drive_id = item.get("drive_id")
        subscription_id = item.get("subscription_id")
        if not isinstance(drive_id, str) or not isinstance(subscription_id, str):
            continue
        expires_at = item.get("expires_at")
        fingerprint = item.get("client_state_hash")
        rows.append(
            {
                "drive_id": drive_id,
                "subscription_id": subscription_id,
                "expires_at": expires_at if isinstance(expires_at, str) else None,
                "client_state_hash": fingerprint if isinstance(fingerprint, str) else None,
            }
        )
    return rows


def client_state_fingerprint(secret: str) -> str:
    """A non-reversible fingerprint of the ``clientState`` a subscription was
    created with, recorded alongside it.

    Why it has to exist: ``POST .../connections/{id}/webhook`` always mints a
    FRESH secret, and Graph treats a subscription's ``clientState`` as
    immutable — a rotation therefore leaves every live subscription signing
    with a value the receiver will now silently drop, and Agnes would have no
    way to notice: the subscription is still there, still unexpired, still
    "healthy" by every other measure. Comparing this fingerprint is what
    turns a rotation into a re-create instead of a slow, silent outage that
    only shows up as "the webhook stopped working".

    A hash, never the value — an audit/config surface records identifiers and
    hashes, never secrets (``src.audit_helpers.hash_args``' own rule).
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:32]


def _client_state_stale(record: Dict[str, Any], secret: str) -> bool:
    """True only for a KNOWN mismatch. A record written before this field
    existed carries ``None``, which is read as "created with whatever secret
    was current" — an upgrade must not re-create every subscription on the
    instance just because it cannot prove otherwise."""
    stored = record.get("client_state_hash")
    return isinstance(stored, str) and stored != client_state_fingerprint(secret)


def _persist_state(connection_id: str, records: List[Dict[str, Any]]) -> None:
    """Write the whole record list back through ``config_patch`` (never a
    wholesale ``config=`` replace — see the module docstring)."""
    source_connections_repo().config_patch(connection_id, {SUBSCRIPTION_STATE_KEY: records})


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse a stored/Graph-returned timestamp to an aware UTC datetime.
    Graph writes ``...Z``; this module writes ``...+00:00``; a naive value
    (hand-edited row) is read as UTC. Unparseable → ``None``, which every
    caller treats as "expiry unknown, renew it"."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _graph_expiry(now: datetime) -> str:
    """The ``expirationDateTime`` to request, in the ``...Z`` form Graph's
    own examples use."""
    return (now + timedelta(days=subscription_days())).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _needs_renewal(expires_at: Optional[str], now: datetime) -> bool:
    """True when a record is inside the renewal window — or when its expiry
    cannot be read at all, which is the fail-safe direction: a needless
    ``PATCH`` costs one call, a skipped one costs the subscription."""
    parsed = _parse_iso(expires_at)
    if parsed is None:
        return True
    return parsed <= now + timedelta(hours=renew_within_hours())


def _is_expired(expires_at: Optional[str], now: datetime) -> bool:
    """True when the record is already past its expiry — Graph has reaped
    the subscription, so it must be re-CREATED, not patched."""
    parsed = _parse_iso(expires_at)
    return parsed is not None and parsed <= now


# ---------------------------------------------------------------------------
# Scope -> drive resolution
# ---------------------------------------------------------------------------


def _scope_drive_ids(connection: Dict[str, Any]) -> Tuple[List[str], List[Dict[str, Any]]]:
    """The DISTINCT, structurally-valid drive ids named by this connection's
    confirmed scopes, in first-seen order, plus a skip row per scope that
    could not contribute one.

    Several scopes commonly sit in the same document library, and one Graph
    subscription covers a whole drive — so the unit here is the drive, not
    the scope, and a second scope on the same drive is not a second
    subscription.
    """
    scopes = (connection.get("config") or {}).get("scopes")
    ordered: List[str] = []
    seen = set()
    skipped: List[Dict[str, Any]] = []
    if not isinstance(scopes, list):
        return ordered, skipped
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        drive_id = scope.get("drive_id")
        scope_id = scope.get("source_scope_id")
        if not isinstance(drive_id, str) or not drive_id:
            # A scope confirmed before `drive_id` existed on the row (see
            # acl_sync's same degradation): reported, never fatal.
            skipped.append({"source_scope_id": scope_id, "reason": "missing_drive_id"})
            continue
        if not _GRAPH_ID_RE.match(drive_id):
            skipped.append({"source_scope_id": scope_id, "reason": "invalid_drive_id"})
            continue
        if drive_id in seen:
            continue
        seen.add(drive_id)
        ordered.append(drive_id)
    return ordered, skipped


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------


def _require_receiver_enabled() -> None:
    from app.instance_config import feature_enabled

    if not feature_enabled("sharepoint", "enabled", env_var="AGNES_SHAREPOINT_ENABLED", default=False):
        raise SubscriptionError(
            "sharepoint_disabled",
            "sharepoint.enabled is false — the receiver route answers 404, so any subscription "
            "created now would be torn down by Graph after its delivery failures. Enable it in "
            "instance.yaml (or AGNES_SHAREPOINT_ENABLED) first.",
        )


def _require_secret(connection: Dict[str, Any]) -> str:
    secret = (connection.get("config") or {}).get("webhook_secret")
    if not isinstance(secret, str) or not secret:
        raise SubscriptionError(
            "webhook_secret_missing",
            "no webhook secret minted for this connection — POST /api/admin/sharepoint/connections/"
            f"{connection.get('id')}/webhook first; its value becomes the subscription's clientState.",
        )
    return secret


async def _app_token(connection: Dict[str, Any]) -> str:
    """Certificate/secret → Graph app-only token, with the two failures
    typed apart the way ``admin_sharepoint._resolved_token`` does (409
    "configure the credential" vs. 502 "Entra rejected it")."""
    try:
        settings = resolve_sharepoint_settings(connection)
    except SharePointSettingsError as exc:
        raise SubscriptionError("sharepoint_cert_unresolved", str(exc)) from exc
    try:
        return await graph_client.get_app_token(
            settings.tenant_id, settings.client_id, settings.private_key, client_secret=settings.client_secret
        )
    except SharePointGraphError as exc:
        raise SubscriptionError("sharepoint_graph_error", str(exc), status_code=502) from exc


# ---------------------------------------------------------------------------
# The two public verbs
# ---------------------------------------------------------------------------


async def ensure_subscriptions(
    connection: Dict[str, Any],
    *,
    request: Any = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Make Graph's subscriptions match this connection's confirmed scopes.

    Idempotent: a second call moments after a first makes NO Graph writes at
    all (every record is outside the renewal window, so every drive reports
    ``unchanged``).

    Per drive, exactly one of:

    - ``created`` — no usable record; ``POST /subscriptions``.
    - ``renewed`` — record inside the renewal window; ``PATCH``. A record
      already past its expiry, or a ``PATCH`` that 404s (Graph reaped it),
      falls through to a create instead, still reported as ``created``.
    - ``unchanged`` — record healthy and not near expiry; no Graph call.
    - ``failed`` — this drive's Graph call raised. Recorded, walk continues,
      the drive's PREVIOUS record (if any) is preserved so the next sweep
      retries rather than losing the subscription id.

    A record created against a ``clientState`` that has since been rotated is
    deleted and re-created (reported as ``created``) rather than renewed —
    see :func:`client_state_fingerprint` for why an otherwise-healthy
    subscription can be silently dead.

    Plus, for records whose drive is no longer in any confirmed scope,
    ``removed`` — ``DELETE /subscriptions/{id}`` and drop the record (a
    delete that fails keeps the record, so the next sweep retries).

    Raises :class:`SubscriptionError` — never a partial result — for the
    four whole-connection preconditions: the receiver flag is off, no
    webhook secret has been minted, no public HTTPS origin is configured,
    or the connection's credential does not resolve/authenticate.

    The public-URL precondition is checked EAGERLY only when this call will
    actually create something. A pure renewal pass needs no notification URL
    at all (``PATCH`` carries only an expiry), and the nightly sweep runs
    with no inbound request to derive an origin from — so refusing a renewal
    because ``AGNES_BASE_URL`` is unset would let subscriptions lapse over a
    configuration detail they do not depend on. A renewal that unexpectedly
    falls through to a create still needs it, and reports
    ``public_url_not_configured`` against that one drive.
    """
    _require_receiver_enabled()
    secret = _require_secret(connection)
    token = await _app_token(connection)

    now = now or datetime.now(timezone.utc)
    wanted, skipped = _scope_drive_ids(connection)
    existing = {rec["drive_id"]: rec for rec in subscription_state(connection)}

    resolve_url = _notification_url_resolver(connection["id"], request)
    if any(
        drive_id not in existing
        or _is_expired(existing[drive_id].get("expires_at"), now)
        or _client_state_stale(existing[drive_id], secret)
        for drive_id in wanted
    ):
        resolve_url()  # eager refusal: this call is definitely creating

    results: List[Dict[str, Any]] = []
    records: List[Dict[str, Any]] = []
    counts = {"created": 0, "renewed": 0, "unchanged": 0, "removed": 0, "failed": 0}

    for drive_id in wanted:
        record = existing.get(drive_id)
        outcome = await _ensure_one_drive(
            token,
            drive_id=drive_id,
            record=record,
            resolve_url=resolve_url,
            client_state=secret,
            now=now,
        )
        results.append(outcome["result"])
        counts[outcome["result"]["action"]] += 1
        if outcome["record"] is not None:
            records.append(outcome["record"])

    # Drives that fell out of scope: tear their subscription down rather
    # than leaving Graph pushing notifications Agnes no longer wants (and
    # paying the receiver's dedup cost for them forever).
    wanted_set = set(wanted)
    for drive_id, record in existing.items():
        if drive_id in wanted_set:
            continue
        removed, error = await _delete_one(token, record["subscription_id"])
        if removed:
            counts["removed"] += 1
            results.append({"drive_id": drive_id, "action": "removed", "subscription_id": record["subscription_id"]})
        else:
            counts["failed"] += 1
            records.append(record)
            results.append({"drive_id": drive_id, "action": "failed", "error": error})

    _persist_state(connection["id"], records)

    logger.info(
        "sharepoint subscriptions: connection %s -> created=%s renewed=%s unchanged=%s removed=%s failed=%s",
        connection["id"],
        counts["created"],
        counts["renewed"],
        counts["unchanged"],
        counts["removed"],
        counts["failed"],
    )
    return {
        "connection_id": connection["id"],
        "notification_url": resolve_url.resolved,
        "drives": results,
        "skipped_scopes": skipped,
        **counts,
    }


class _NotificationUrlResolver:
    """Resolve this connection's receiver URL AT MOST ONCE, on first use.

    A callable rather than a plain string so :func:`ensure_subscriptions` can
    demand it exactly when a create is on the cards — see that function's
    docstring for why a renewal-only pass must not need a public origin at
    all. ``resolved`` exposes what was actually computed (``None`` when
    nothing ever needed it) for the result payload.
    """

    def __init__(self, connection_id: str, request: Any) -> None:
        self._connection_id = connection_id
        self._request = request
        self.resolved: Optional[str] = None

    def __call__(self) -> str:
        if self.resolved is None:
            self.resolved = receiver_url(self._connection_id, base_url=resolve_notification_base_url(self._request))
        return self.resolved


def _notification_url_resolver(connection_id: str, request: Any) -> _NotificationUrlResolver:
    return _NotificationUrlResolver(connection_id, request)


async def _ensure_one_drive(
    token: str,
    *,
    drive_id: str,
    record: Optional[Dict[str, Any]],
    resolve_url: _NotificationUrlResolver,
    client_state: str,
    now: datetime,
) -> Dict[str, Any]:
    """One drive's create-or-renew decision, isolated so a raise here can
    only ever cost this drive. Returns ``{"result": <per-drive row>,
    "record": <state row or None>}``."""
    expiration = _graph_expiry(now)

    if record is not None and _client_state_stale(record, client_state):
        # The webhook secret was rotated since this subscription was created.
        # Graph will not let us change a subscription's `clientState`, and
        # the receiver drops every notification signed with the old one — so
        # the only repair is delete + create. Best-effort delete: an orphan
        # left pushing an unverifiable clientState is dropped silently by the
        # receiver, which is wasteful but never wrong, and must not stop the
        # replacement from being created.
        logger.info("sharepoint subscriptions: clientState rotated for drive %s; re-creating", drive_id)
        await _delete_one(token, record["subscription_id"])
        record = None

    if record is not None and not _needs_renewal(record.get("expires_at"), now):
        return {
            "result": {
                "drive_id": drive_id,
                "action": "unchanged",
                "subscription_id": record["subscription_id"],
                "expires_at": record.get("expires_at"),
            },
            "record": record,
        }

    if record is not None and not _is_expired(record.get("expires_at"), now):
        try:
            body = await graph_client.renew_subscription(token, record["subscription_id"], expiration)
        except SharePointGraphError as exc:
            if exc.status_code not in (404, 410):
                logger.warning("sharepoint subscriptions: renew failed for drive %s: %s", drive_id, exc)
                return {
                    "result": {"drive_id": drive_id, "action": "failed", "error": str(exc)},
                    "record": record,
                }
            # Graph no longer has it — fall through and create a new one.
            logger.info("sharepoint subscriptions: subscription for drive %s is gone upstream; re-creating", drive_id)
        else:
            expires_at = _returned_expiry(body, expiration)
            new_record = {
                "drive_id": drive_id,
                "subscription_id": record["subscription_id"],
                "expires_at": expires_at,
                "client_state_hash": record.get("client_state_hash") or client_state_fingerprint(client_state),
            }
            return {
                "result": {
                    "drive_id": drive_id,
                    "action": "renewed",
                    "subscription_id": record["subscription_id"],
                    "expires_at": expires_at,
                },
                "record": new_record,
            }

    try:
        notification_url = resolve_url()
    except SubscriptionError as exc:
        # Only reachable on the renewal-fell-through-to-create path: the
        # eager check in `ensure_subscriptions` already refused the whole
        # call when a create was foreseeable. One drive's problem, reported
        # as one drive's problem.
        return {"result": {"drive_id": drive_id, "action": "failed", "error": exc.error}, "record": record}

    try:
        body = await graph_client.create_subscription(
            token,
            {
                "changeType": "updated",
                "notificationUrl": notification_url,
                "resource": f"/drives/{drive_id}/root",
                "expirationDateTime": expiration,
                "clientState": client_state,
            },
        )
    except SharePointGraphError as exc:
        logger.warning("sharepoint subscriptions: create failed for drive %s: %s", drive_id, exc)
        return {"result": {"drive_id": drive_id, "action": "failed", "error": str(exc)}, "record": record}

    subscription_id = body.get("id")
    if not isinstance(subscription_id, str) or not subscription_id:
        # A 201 with no id is not a subscription Agnes can ever renew or
        # delete; treat it as a failure rather than persisting a record
        # that can only produce confusing errors later.
        return {
            "result": {"drive_id": drive_id, "action": "failed", "error": "graph_create_returned_no_id"},
            "record": record,
        }
    expires_at = _returned_expiry(body, expiration)
    return {
        "result": {
            "drive_id": drive_id,
            "action": "created",
            "subscription_id": subscription_id,
            "expires_at": expires_at,
        },
        "record": {
            "drive_id": drive_id,
            "subscription_id": subscription_id,
            "expires_at": expires_at,
            "client_state_hash": client_state_fingerprint(client_state),
        },
    }


def _returned_expiry(body: Dict[str, Any], requested: str) -> str:
    """Graph's own ``expirationDateTime`` when it echoed one (authoritative —
    it may have clamped what was asked for), else what was requested."""
    value = body.get("expirationDateTime")
    return value if isinstance(value, str) and value.strip() else requested


async def _delete_one(token: str, subscription_id: str) -> Tuple[bool, Optional[str]]:
    """Delete one subscription. ``404``/``410`` counts as SUCCESS — the goal
    is "Graph is not pushing to us for this any more", and already-gone
    satisfies it. Returns ``(deleted, error_message)``."""
    try:
        await graph_client.delete_subscription(token, subscription_id)
    except SharePointGraphError as exc:
        if exc.status_code in (404, 410):
            return True, None
        return False, str(exc)
    return True, None


async def remove_subscriptions(connection: Dict[str, Any]) -> Dict[str, Any]:
    """Delete every Graph subscription recorded for this connection.

    Deliberately NOT gated on ``sharepoint.enabled``: the moment an
    operator most needs teardown is right after turning the receiver off, and
    a flag that blocked cleanup would strand live subscriptions pushing at a
    404 until Graph's own failure count reaped them.

    A record whose delete fails is KEPT (so the next call retries); a record
    Graph no longer knows about is dropped. Returns per-subscription rows
    plus ``removed``/``failed`` counts. A connection with no records is a
    clean no-op — it does not even fetch a token.
    """
    records = subscription_state(connection)
    if not records:
        return {"connection_id": connection["id"], "subscriptions": [], "removed": 0, "failed": 0}

    token = await _app_token(connection)

    remaining: List[Dict[str, Any]] = []
    rows: List[Dict[str, Any]] = []
    removed = failed = 0
    for record in records:
        ok, error = await _delete_one(token, record["subscription_id"])
        if ok:
            removed += 1
            rows.append(
                {"drive_id": record["drive_id"], "subscription_id": record["subscription_id"], "action": "removed"}
            )
        else:
            failed += 1
            remaining.append(record)
            rows.append(
                {
                    "drive_id": record["drive_id"],
                    "subscription_id": record["subscription_id"],
                    "action": "failed",
                    "error": error,
                }
            )

    _persist_state(connection["id"], remaining)
    logger.info(
        "sharepoint subscriptions: connection %s teardown -> removed=%s failed=%s",
        connection["id"],
        removed,
        failed,
    )
    return {"connection_id": connection["id"], "subscriptions": rows, "removed": removed, "failed": failed}


# ---------------------------------------------------------------------------
# The renewal sweep
# ---------------------------------------------------------------------------


def connection_needs_renewal(connection: Dict[str, Any], now: datetime) -> bool:
    """Whether the daily sweep should touch this connection at all — true
    when it has a confirmed scope drive with no record, a record inside the
    renewal window, a record whose drive left scope, or a record created
    against a ``clientState`` that has since been rotated (which the sweep
    repairs, so the fix does not wait on someone noticing the receiver went
    quiet).

    Cheap and purely local (no Graph call), so the sweep can walk every
    SharePoint connection on an instance and only pay for the ones that
    actually have work — the same "walk + per-row due-check" shape
    ``run_due_extraction`` uses.
    """
    wanted, _skipped = _scope_drive_ids(connection)
    records = {rec["drive_id"]: rec for rec in subscription_state(connection)}
    if any(drive_id not in records for drive_id in wanted):
        return True
    if any(drive_id not in set(wanted) for drive_id in records):
        return True
    secret = (connection.get("config") or {}).get("webhook_secret")
    if isinstance(secret, str) and secret and any(_client_state_stale(rec, secret) for rec in records.values()):
        return True
    return any(_needs_renewal(records[drive_id].get("expires_at"), now) for drive_id in wanted)


async def renew_due_subscriptions(*, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Sweep every SharePoint connection and :func:`ensure_subscriptions`
    the ones with due work.

    A clean, typed no-op (never an error) when the receiver flag is off —
    the scheduler row, once registered, fires unconditionally on its own
    cadence, so "nothing to do" must be cheap and harmless (same posture as
    ``run_due_extraction``).

    A connection whose PRECONDITIONS fail (no secret minted yet, no public
    URL, an expired certificate) is recorded in ``errors`` and the sweep
    continues — one misconfigured connection never stops another's renewal,
    which is the whole reason this is a sweep and not a loop of raises.

    Note this sweep never CREATES the first subscription for a connection
    that has none *by accident*: it does, deliberately, because ensure is
    ensure — an admin who confirmed a new scope yesterday gets its
    subscription from tonight's sweep without a second button press.
    """
    from app.instance_config import feature_enabled

    if not feature_enabled("sharepoint", "enabled", env_var="AGNES_SHAREPOINT_ENABLED", default=False):
        return {"processed": [], "count": 0, "skipped": True, "reason": "sharepoint_disabled", "errors": []}

    now = now or datetime.now(timezone.utc)
    processed: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for row in source_connections_repo().list(source_type="sharepoint"):
        try:
            if not connection_needs_renewal(row, now):
                continue
            summary = await ensure_subscriptions(row, now=now)
        except SubscriptionError as exc:
            errors.append({"connection_id": row.get("id"), "error": exc.error})
            continue
        except Exception:  # noqa: BLE001 — one bad connection never aborts the sweep
            logger.exception("sharepoint subscriptions: renewal sweep failed for connection %s", row.get("id"))
            errors.append({"connection_id": row.get("id"), "error": "unexpected_error"})
            continue
        processed.append(
            {
                "connection_id": row["id"],
                "created": summary["created"],
                "renewed": summary["renewed"],
                "unchanged": summary["unchanged"],
                "removed": summary["removed"],
                "failed": summary["failed"],
            }
        )
    return {"processed": processed, "count": len(processed), "errors": errors}
