"""Outbound OAuth client for upstream MCP sources (2026-07-30 spec, PR 1).

Agnes acting as an OAuth **client** against an upstream MCP server's
authorization server — the mirror image of ``app/auth/mcp_oauth.py``
(Agnes acting as the *issuer* on the inbound side). Implements:

* RFC 9728 — OAuth 2.0 Protected Resource Metadata discovery.
* RFC 8414 — OAuth 2.0 Authorization Server Metadata discovery.
* RFC 7591 — OAuth 2.0 Dynamic Client Registration.
* RFC 6749 §4.1 + RFC 7636 (PKCE) — authorization-code token exchange +
  refresh.

Every outbound call in this module MUST go through an
``httpx.AsyncClient`` built by :func:`build_oauth_http_client` (SSRF-safe,
https-only, per-hop redirect re-validation — see
``src.net.ssrf_safe_client``). Token exchange/refresh additionally disable
redirect-following altogether (``follow_redirects=False`` per call) — an AS
redirecting a token response is never a legitimate flow.

**Mix-up defense (RFC 9700 §4.4):** every function here that needs a token
endpoint or client identity takes them as EXPLICIT parameters. Callers
(``connectors.mcp.client``, the admin registration endpoints) must source
those parameters from the stored ``mcp_source_oauth_clients`` row for the
target ``source_id`` — never from request/callback data or an AS response —
so a malicious or compromised second AS can never redeem a code/refresh
token against a different source's client identity. This module has no way
to look up that row itself (no DB access here) — the calling convention
itself is the defense.
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

import httpx
from authlib.oauth2.rfc7636 import create_s256_code_challenge

from connectors.mcp.client import exc_summary
from src.net.ssrf_safe_client import build_async_client

logger = logging.getLogger(__name__)

USER_AGENT = "Agnes-MCP-OAuth-Client/1.0 (+https://github.com/keboola/agnes-the-ai-analyst; agnes-mcp-oauth)"

DEFAULT_TIMEOUT_SEC = 30.0

#: RFC 9700 §4.1 — PKCE S256 is mandatory; downgrading to "plain" or no PKCE
#: at all is a fail-closed error, never a silent fallback.
REQUIRED_CODE_CHALLENGE_METHOD = "S256"

#: Single non-ambiguous capture group over a bounded run of non-`"` chars —
#: linear time regardless of input size (security playbook F5).
_RESOURCE_METADATA_RE = re.compile(r'resource_metadata="([^"]*)"')

#: The ``token_endpoint_auth_method`` values :func:`_post_token_request`
#: can actually satisfy. Anything else must fail closed at registration
#: rather than at the first token call.
_IMPLEMENTED_AUTH_METHODS = ("client_secret_basic", "client_secret_post", "none")

#: The subset of those that carry a client secret. A registration recorded
#: as one of these but issued no secret is unusable — see
#: :func:`register_dynamic_client`.
_CONFIDENTIAL_AUTH_METHODS = ("client_secret_basic", "client_secret_post")

#: RFC 6749 §5.2 — the AS's way of saying "I did not accept your client
#: authentication". The one error worth re-presenting the same credential
#: for, in the other style §2.3.1 permits.
_CLIENT_AUTH_REJECTED = "invalid_client"


class OAuthDiscoveryError(Exception):
    """Raised when RFC 9728 / RFC 8414 discovery or RFC 7591 registration
    cannot produce a usable client registration."""


class OAuthTokenError(Exception):
    """Raised when a token exchange/refresh call fails, or the AS's
    response cannot be parsed into a usable token set."""


class OAuthTransportError(OAuthTokenError):
    """The token endpoint could not be reached at all (DNS/TCP/TLS/timeout).

    Subclass of :class:`OAuthTokenError` so existing catch-alls keep
    working, but distinguishable where the user-facing message matters —
    "could not reach the authorization server" is actionable in a way
    "token exchange failed" is not (Devin Review on #1130)."""


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


def build_oauth_http_client(*, timeout: float = DEFAULT_TIMEOUT_SEC) -> httpx.AsyncClient:
    """SSRF-safe, https-only ``httpx.AsyncClient`` for all outbound OAuth
    traffic (discovery, DCR, token exchange, refresh, best-effort revoke).

    ``https_only=True`` — outbound MCP OAuth traffic must never downgrade to
    cleartext, even mid-redirect-chain (spec §6 SSRF checklist).
    """
    return build_async_client(
        timeout=timeout,
        https_only=True,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )


# ---------------------------------------------------------------------------
# RFC 9728 — protected resource metadata discovery
# ---------------------------------------------------------------------------


def _join_well_known(base_url: str, well_known_name: str) -> str:
    """RFC 9728 §3.1 well-known URI construction: insert the well-known path
    segment between the authority and the resource's own path."""
    parts = urlparse(base_url)
    suffix = parts.path.rstrip("/")
    path = f"/.well-known/{well_known_name}{suffix}"
    return urlunparse((parts.scheme, parts.netloc, path, "", "", ""))


def _json_or_discovery_error(resp: httpx.Response, url: str) -> Dict[str, Any]:
    """Parse a 200 discovery response as JSON, or raise a translated
    ``OAuthDiscoveryError`` — a junk body must surface as an actionable
    message, not an unhandled ``ValueError``/500 (Devin Review on #1124)."""
    try:
        body = resp.json()
    except ValueError as exc:
        raise OAuthDiscoveryError(f"metadata document at {url!r} is not valid JSON") from exc
    if not isinstance(body, dict):
        raise OAuthDiscoveryError(f"metadata document at {url!r} is not a JSON object")
    return body


def _extract_resource_metadata_url(www_authenticate: str) -> Optional[str]:
    """Pull the ``resource_metadata`` challenge parameter out of a
    ``WWW-Authenticate`` header value, or ``None`` if absent."""
    m = _RESOURCE_METADATA_RE.search(www_authenticate or "")
    return m.group(1) if m and m.group(1) else None


def _simple_well_known(base_url: str, well_known_name: str) -> str:
    """Suffix ``base_url`` with ``/.well-known/<name>`` (spec §2's literal
    ``{source.url}/.well-known/oauth-protected-resource`` notation) — the
    resource's *own* well-known document lives directly under its own URL,
    unlike the authorization server's (see :func:`_join_well_known`, which
    implements RFC 8414's host/path-insertion rule for that case)."""
    return base_url.rstrip("/") + "/.well-known/" + well_known_name


async def discover_protected_resource_metadata(
    source_url: str,
    *,
    client: httpx.AsyncClient,
) -> Dict[str, Any]:
    """RFC 9728 protected-resource metadata for ``source_url``.

    Primary path: GET the well-known document directly. Fallback: probe
    ``source_url`` bare and read the ``resource_metadata`` challenge
    parameter off a ``401`` ``WWW-Authenticate`` header, then fetch THAT
    URL. Both hops run through ``client`` (the SSRF-safe transport) — the
    fallback URL is attacker-influenceable (comes from a live upstream
    response) if the upstream is later compromised.
    """
    # RFC 9728 §3.1 puts the well-known segment between the authority and
    # the resource's own path (path-insertion) — that form goes first. The
    # suffix form ({url}/.well-known/…) is kept as a lenient fallback for
    # servers that publish it there; the two collapse to the same URL when
    # the resource lives at the origin root. (Devin Review on #1124.)
    candidates: List[str] = []
    for url in (
        _join_well_known(source_url, "oauth-protected-resource"),
        _simple_well_known(source_url, "oauth-protected-resource"),
    ):
        if url not in candidates:
            candidates.append(url)
    primary_url = candidates[0]
    for candidate in candidates:
        try:
            resp = await client.get(candidate)
            if resp.status_code == 200:
                body = _json_or_discovery_error(resp, candidate)
                if not body.get("authorization_servers"):
                    # Parses as a JSON object, but is not RFC 9728 metadata —
                    # the same not-the-document case as the HTML page below,
                    # just wearing a content type. API gateways answer unknown
                    # paths with 200 {"error": ...} envelopes, and accepting
                    # one here skipped the remaining candidate and the 401
                    # fallback, then surfaced resolve_issuer's "carries no
                    # 'authorization_servers'" — which points the admin at the
                    # document rather than at the missing discovery
                    # (Devin Review on #1124).
                    logger.debug(
                        "protected-resource well-known at %s has no 'authorization_servers'; trying the next route",
                        candidate,
                    )
                    continue
                return body
        except httpx.HTTPError as exc:
            logger.debug("protected-resource well-known fetch failed for %s: %s", candidate, exc_summary(exc))
        except OAuthDiscoveryError as exc:
            # A 200 carrying a junk body is not the metadata document — these
            # URLs are PROBED, not advertised, and a host that answers unknown
            # paths with a catch-all HTML page (SPA, edge proxy) returns 200
            # for both. Letting that abort the search skipped the remaining
            # candidate AND the 401-challenge fallback, which is the path the
            # design spec calls out as the observed real-world case — so the
            # one shape most likely to need the fallback was the one shape
            # that never reached it. The hard error is kept below for the
            # resource_metadata URL the server explicitly advertised, where a
            # junk body IS the actionable answer (Devin Review on #1124).
            logger.debug("protected-resource well-known at %s is not a metadata document: %s", candidate, exc)

    try:
        probe = await client.get(source_url)
    except httpx.HTTPError as exc:
        raise OAuthDiscoveryError(f"protected-resource discovery failed: {exc_summary(exc)}") from exc
    if probe.status_code != 401:
        raise OAuthDiscoveryError(
            "protected-resource discovery failed: no metadata document at "
            f"{primary_url!r} and no 401 challenge from {source_url!r} "
            f"(got HTTP {probe.status_code})"
        )
    meta_url = _extract_resource_metadata_url(probe.headers.get("WWW-Authenticate", ""))
    if not meta_url:
        raise OAuthDiscoveryError(
            "protected-resource discovery failed: 401 response from "
            f"{source_url!r} carries no resource_metadata challenge"
        )
    try:
        resp = await client.get(meta_url)
    except httpx.HTTPError as exc:
        raise OAuthDiscoveryError(
            f"failed to fetch resource_metadata document {meta_url!r}: {exc_summary(exc)}"
        ) from exc
    if resp.status_code != 200:
        raise OAuthDiscoveryError(f"resource_metadata document fetch {meta_url!r} returned HTTP {resp.status_code}")
    return _json_or_discovery_error(resp, meta_url)


def resolve_issuer(protected_resource_metadata: Dict[str, Any]) -> str:
    """Pick the authorization server issuer from RFC 9728 metadata.

    ``authorization_servers`` is a list; Agnes has no UI (yet) to choose
    among several, so the first entry wins — matches every other outbound
    connector's "first viable option" pattern (e.g. BigQuery IPv4
    preference in the SSRF resolver).
    """
    servers = protected_resource_metadata.get("authorization_servers") or []
    if not servers or not isinstance(servers, list):
        raise OAuthDiscoveryError("protected-resource metadata carries no 'authorization_servers' entry")
    issuer = servers[0]
    if not isinstance(issuer, str) or not issuer:
        raise OAuthDiscoveryError("protected-resource metadata's authorization_servers[0] is not a URL")
    return issuer


# ---------------------------------------------------------------------------
# RFC 8414 — authorization server metadata discovery
# ---------------------------------------------------------------------------


async def discover_as_metadata(issuer: str, *, client: httpx.AsyncClient) -> Dict[str, Any]:
    """RFC 8414 authorization-server metadata for ``issuer``.

    Enforces RFC 8414 §3.3: the document's ``issuer`` MUST be present and
    MUST match the issuer the metadata was requested for (trailing-slash
    tolerant). Beyond spec compliance this guarantees the stored client row
    and its audit record always carry a real provider identity, and adds a
    defense-in-depth layer against a compromised AS answering for a foreign
    issuer (Devin Review on #1124).
    """
    url = _join_well_known(issuer, "oauth-authorization-server")
    try:
        resp = await client.get(url)
    except httpx.HTTPError as exc:
        raise OAuthDiscoveryError(f"authorization-server metadata fetch failed: {exc_summary(exc)}") from exc
    if resp.status_code != 200:
        raise OAuthDiscoveryError(f"authorization-server metadata fetch {url!r} returned HTTP {resp.status_code}")
    body = _json_or_discovery_error(resp, url)
    meta_issuer = body.get("issuer")
    if not isinstance(meta_issuer, str) or not meta_issuer:
        raise OAuthDiscoveryError(f"authorization-server metadata at {url!r} carries no 'issuer' (RFC 8414 §3.3)")
    if meta_issuer.rstrip("/") != issuer.rstrip("/"):
        raise OAuthDiscoveryError(
            f"authorization-server metadata issuer mismatch: requested {issuer!r}, document says {meta_issuer!r}"
        )
    return body


def require_pkce_s256(as_metadata: Dict[str, Any]) -> None:
    """Fail closed (RFC 9700 §4.1) unless the AS advertises PKCE S256.

    Never downgrade to ``plain`` or no PKCE — raises
    :class:`OAuthDiscoveryError` with an explanatory message instead.
    """
    methods = as_metadata.get("code_challenge_methods_supported") or []
    if REQUIRED_CODE_CHALLENGE_METHOD not in methods:
        raise OAuthDiscoveryError(
            "authorization server does not advertise PKCE S256 support "
            f"(code_challenge_methods_supported={methods!r}); refusing to "
            "register — Agnes never downgrades to 'plain' or no PKCE (RFC 9700 §4.1)"
        )


def require_https_endpoints(as_metadata: Dict[str, Any]) -> None:
    """Fail closed unless every endpoint URL the AS advertises is https.

    Defense-in-depth alongside the SSRF-safe client's own https-only
    enforcement — this catches a misconfigured/malicious AS advertising a
    plain-http endpoint before we ever try to reach it.
    """
    for key in ("issuer", "authorization_endpoint", "token_endpoint", "registration_endpoint"):
        url = as_metadata.get(key)
        if url and urlparse(url).scheme != "https":
            raise OAuthDiscoveryError(f"authorization server metadata field {key!r} is not https: {url!r}")


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


def generate_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` — S256 always.

    ``secrets.token_urlsafe(64)`` yields an unpadded base64url string built
    from ``[A-Za-z0-9_-]`` — a subset of RFC 7636's
    ``code-verifier`` charset (``[A-Za-z0-9-._~]``) at ~86 chars, comfortably
    inside the mandated 43-128 length window.
    """
    verifier = secrets.token_urlsafe(64)
    challenge = create_s256_code_challenge(verifier)
    return verifier, challenge


# ---------------------------------------------------------------------------
# RFC 7591 — dynamic client registration
# ---------------------------------------------------------------------------


@dataclass
class RegisteredOAuthClient:
    """Result of a successful RFC 7591 dynamic registration (or a manually
    configured client — see ``PUT …/oauth/client``)."""

    issuer: str
    client_id: str
    client_secret: Optional[str]
    registration_access_token: Optional[str]
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: Optional[str]
    scopes: Optional[str] = None


def _choose_token_endpoint_auth_method(as_metadata: Dict[str, Any]) -> str:
    """Pick the client-auth style to ANNOUNCE at registration.

    Only styles the token-call path actually implements may be announced —
    :func:`_post_token_request` speaks HTTP Basic and ``client_secret_post``
    (confidential) or public-client (``client_id`` in the body). Announcing
    anything else would register a contract the token calls then violate, and
    the AS would reject every exchange/refresh (Devin Review on #1124) — fail
    closed with an actionable message instead.

    Basic comes first because RFC 6749 §2.3.1 requires every AS to support
    it; ``client_secret_post`` is the one it may offer *instead*.
    """
    supported = as_metadata.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    for method in ("client_secret_basic", "client_secret_post", "none"):
        if method in supported:
            return method
    raise OAuthDiscoveryError(
        "authorization server supports only these client-auth methods at the token endpoint: "
        f"{supported!r}; Agnes implements 'client_secret_basic', 'client_secret_post' and 'none'. "
        "Configure the client manually via PUT …/oauth/client if the server offers another "
        "compatible option."
    )


async def register_dynamic_client(
    as_metadata: Dict[str, Any],
    *,
    redirect_uri: str,
    client_name: str = "Agnes",
    scopes: Optional[str] = None,
    client: httpx.AsyncClient,
) -> RegisteredOAuthClient:
    """RFC 7591 dynamic client registration against ``as_metadata``'s
    ``registration_endpoint``.

    Raises :class:`OAuthDiscoveryError` when the AS has no
    ``registration_endpoint`` (the caller should fall back to the manual
    ``PUT …/oauth/client`` escape hatch) or the response carries no
    ``client_id``.
    """
    require_https_endpoints(as_metadata)
    registration_endpoint = as_metadata.get("registration_endpoint")
    if not registration_endpoint:
        raise OAuthDiscoveryError(
            "authorization server has no 'registration_endpoint' — dynamic "
            "registration is unsupported; use PUT …/oauth/client to configure "
            "a manually-provisioned client instead"
        )
    auth_method = _choose_token_endpoint_auth_method(as_metadata)
    payload: Dict[str, Any] = {
        "client_name": client_name,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": auth_method,
    }
    if scopes:
        payload["scope"] = scopes
    try:
        resp = await client.post(registration_endpoint, json=payload)
    except httpx.HTTPError as exc:
        raise OAuthDiscoveryError(f"dynamic client registration failed: {exc_summary(exc)}") from exc
    if resp.status_code not in (200, 201):
        raise OAuthDiscoveryError(
            f"dynamic client registration at {registration_endpoint!r} returned "
            f"HTTP {resp.status_code}: {resp.text[:500]}"
        )
    try:
        body = resp.json()
    except ValueError as exc:
        raise OAuthDiscoveryError("dynamic client registration response is not valid JSON") from exc
    client_id = body.get("client_id")
    if not client_id:
        raise OAuthDiscoveryError("dynamic client registration response carries no 'client_id'")
    # RFC 7591 §3.2.1: the AS MAY register a different auth method than the
    # one asked for, and its response — not the request — is authoritative.
    # Same fail-closed reasoning as _choose_token_endpoint_auth_method, on
    # the one path that check cannot see (Devin Review on #1124).
    granted_auth_method = body.get("token_endpoint_auth_method")
    if granted_auth_method and granted_auth_method not in _IMPLEMENTED_AUTH_METHODS:
        raise OAuthDiscoveryError(
            f"authorization server registered the client with token_endpoint_auth_method="
            f"{granted_auth_method!r}; Agnes implements "
            f"{', '.join(repr(m) for m in _IMPLEMENTED_AUTH_METHODS)}. "
            "Configure the client manually via PUT …/oauth/client instead."
        )
    # A registration recorded as confidential but issued no secret is unusable:
    # _post_token_request keys off secret PRESENCE, so it would send no client
    # authentication at all against a client the AS has on file as Basic, and
    # every exchange and refresh would come back invalid_client. Omitting
    # token_endpoint_auth_method means the RFC 7591 default, which is
    # client_secret_basic — so the ambiguous "neither field" response is the
    # same broken shape. Fail at registration with something the admin can act
    # on, rather than silently downgrading to a method the AS did not register
    # and hitting the identical error later with no explanation
    # (Devin Review on #1124).
    effective_auth_method = granted_auth_method or auth_method
    client_secret = body.get("client_secret")
    if effective_auth_method in _CONFIDENTIAL_AUTH_METHODS and not client_secret:
        raise OAuthDiscoveryError(
            f"authorization server registered the client for {effective_auth_method!r} but issued "
            "no client_secret, so no client authentication could ever be sent. Configure the "
            "client manually via PUT …/oauth/client, or use an authorization server that "
            "advertises 'none' for public clients."
        )
    if effective_auth_method == "none" and client_secret:
        # The mirror case, and the reason both are worth handling: token calls
        # select client auth by secret PRESENCE, not by the registered method,
        # so a stray secret on a public registration would send HTTP Basic to a
        # client the AS has on file as public — invalid_client, again with
        # nothing pointing at why. Dropping the secret rather than refusing,
        # because the registration itself is perfectly usable as what the AS
        # says it is; loudly, because discarding credential material silently
        # is its own trap. With both guards the stored secret's presence now
        # AGREES with the registered method by construction, which is what
        # makes selecting on presence correct (Devin Review on #1124).
        logger.warning(
            "mcp oauth DCR: authorization server registered client %s as public "
            "(token_endpoint_auth_method='none') yet returned a client_secret; discarding it — "
            "sending Basic auth to a public client would fail invalid_client",
            client_id,
        )
        client_secret = None
    authorization_endpoint = as_metadata.get("authorization_endpoint")
    token_endpoint = as_metadata.get("token_endpoint")
    if not authorization_endpoint or not token_endpoint:
        raise OAuthDiscoveryError("authorization server metadata is missing authorization_endpoint/token_endpoint")
    issuer = as_metadata.get("issuer")
    if not issuer:
        # Unreachable via discover_as_metadata (which enforces RFC 8414 §3.3),
        # but direct callers with hand-built metadata must not record a blank
        # provider identity either.
        raise OAuthDiscoveryError("authorization server metadata carries no 'issuer'")
    return RegisteredOAuthClient(
        issuer=issuer,
        client_id=client_id,
        client_secret=client_secret,
        registration_access_token=body.get("registration_access_token"),
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        registration_endpoint=registration_endpoint,
        # RFC 7591 §3.2.1: the AS MAY return a different `scope` than the one
        # requested, and its answer is the authoritative one. Recording what we
        # ASKED for would put a scope the client does not hold into the stored
        # row — and straight into the authorize URL PR 2 builds from it, where
        # the AS answers invalid_scope (Devin Review on #1124).
        scopes=body.get("scope") or scopes,
    )


async def best_effort_revoke_registration(
    *,
    registration_endpoint: Optional[str],
    client_id: str,
    registration_access_token: Optional[str],
    client: httpx.AsyncClient,
) -> None:
    """Best-effort RFC 7591 client deregistration ahead of a re-register.

    RFC 7591 doesn't mandate a URL shape for a client's configuration
    endpoint — the canonical way is the AS-returned ``registration_client_uri``,
    which Agnes does not persist (schema v109 keeps only the registration
    access token). Heuristic fallback: ``DELETE {registration_endpoint}/{client_id}``
    with the stored registration access token as bearer auth — the
    convention several AS implementations follow. Any failure (network,
    404/405 from an AS that doesn't support this shape, missing
    prerequisites) is swallowed — the caller proceeds to register the new
    client regardless.
    """
    if not registration_endpoint or not registration_access_token:
        return
    url = registration_endpoint.rstrip("/") + "/" + client_id
    try:
        await client.delete(url, headers={"Authorization": f"Bearer {registration_access_token}"})
    except Exception:
        logger.warning(
            "best-effort OAuth client deregistration failed for client_id=%s at %s",
            client_id,
            url,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Token exchange + refresh
# ---------------------------------------------------------------------------


@dataclass
class TokenSet:
    access_token: str
    refresh_token: Optional[str]
    expires_in: Optional[int]  # seconds; caller converts to an absolute expires_at
    scopes: Optional[str]


async def _raise_as_error(
    resp: httpx.Response, *, action: str, redact: Optional[str] = None
) -> None:
    """Turn a non-200 token response into an ``OAuthTokenError``.

    ``redact`` is scrubbed from the message before it is raised. It matters
    because this detail comes from the authorization server and ends up in
    logs and in an admin-facing error: while the client secret only ever
    travelled in the ``Authorization`` header, no response body could contain
    it, but ``client_secret_post`` puts it in the POST body — and a server
    that echoes the submitted parameters into its error page (or into
    ``error_description``) would hand the secret straight back to us to log.
    Scrubbed BEFORE the 500-character cut, so a truncation cannot leave half
    of one behind.
    """
    detail = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            err = body.get("error", "")
            desc = body.get("error_description", "")
            detail = f"{err}: {desc}" if desc else err
    except ValueError:
        pass
    if not detail:
        detail = resp.text
    if redact:
        detail = detail.replace(redact, "***")
    raise OAuthTokenError(f"{action} failed (HTTP {resp.status_code}): {detail[:500]}")


def _coerce_expires_in(raw: Any) -> Optional[int]:
    """Seconds as an int, or None when the server did not give a usable one.

    RFC 6749 says this is a number, but real servers ship it as a string.
    The value was passed through untyped and later reached
    ``timedelta(seconds=...)``, which raises TypeError — and that happens
    AFTER the refresh has already rotated the tokens, outside the
    OAuthTokenError boundary, so the rotated refresh token is lost and the
    connection breaks permanently rather than for one call (review finding
    on #1124). An unusable value simply means "no known expiry".
    """
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        logger.warning("mcp oauth: ignoring non-numeric expires_in %r from the token endpoint", raw)
        return None


def _token_set_from_response(body: Dict[str, Any]) -> TokenSet:
    access_token = body.get("access_token")
    if not access_token:
        raise OAuthTokenError("token response carries no 'access_token'")
    scope = body.get("scope")
    return TokenSet(
        access_token=access_token,
        refresh_token=body.get("refresh_token"),
        expires_in=_coerce_expires_in(body.get("expires_in")),
        scopes=scope if isinstance(scope, str) else None,
    )


def _is_client_auth_rejection(resp: httpx.Response) -> bool:
    """True iff ``resp`` is the AS refusing our *client authentication*
    (RFC 6749 §5.2 ``invalid_client``), as opposed to refusing the grant.

    Only that one error justifies re-sending the secret the other way; a
    bad code or a dead refresh token must surface on the first answer.
    """
    if resp.status_code not in (400, 401):
        return False
    try:
        body = resp.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("error") == _CLIENT_AUTH_REJECTED


async def _post_token_request(
    *,
    token_endpoint: str,
    client_id: str,
    client_secret: Optional[str],
    data: Dict[str, str],
    client: httpx.AsyncClient,
    action: str,
) -> Dict[str, Any]:
    """POST a grant to ``token_endpoint``, presenting the client secret the
    way this authorization server accepts it, and return the parsed body.

    RFC 6749 §2.3.1 lets a confidential client authenticate with HTTP Basic
    *or* with body parameters, and leaves the choice to the server. Agnes
    leads with Basic — §2.3.1 requires every AS to support it — and, for a
    confidential client only, retries once with ``client_secret`` in the body
    when the AS answers ``invalid_client``. That makes an AS advertising only
    ``client_secret_post`` work without Agnes storing a per-client auth
    method: there is nowhere to put one, since the DuckDB app-state schema is
    frozen and a stored column would be Postgres-only — the feature would
    then silently not work on a DuckDB instance.

    A public (PKCE-only) client has no secret to re-present, so it never
    retries; neither does any failure that is not ``invalid_client``.

    Redirects are never followed (an AS redirecting a token response is
    never legitimate).
    """
    attempts: List[Dict[str, Any]] = [{"auth": (client_id, client_secret)} if client_secret else {}]
    if client_secret:
        attempts.append({"form": {"client_secret": client_secret}})

    resp: Optional[httpx.Response] = None
    for index, style in enumerate(attempts):
        body_params = {**data, **style.pop("form", {})}
        try:
            resp = await client.post(
                token_endpoint,
                data=body_params,
                follow_redirects=False,
                **style,
            )
        except httpx.HTTPError as exc:
            raise OAuthTransportError(f"{action} failed: {exc_summary(exc)}") from exc
        if resp.status_code == 200:
            break
        if index + 1 < len(attempts) and _is_client_auth_rejection(resp):
            logger.info(
                "mcp oauth: %s — authorization server rejected HTTP Basic client auth; "
                "retrying once with client_secret_post",
                action,
            )
            continue
        await _raise_as_error(resp, action=action, redact=client_secret)

    assert resp is not None  # every loop path either breaks, continues or raises
    try:
        parsed = resp.json()
    except ValueError as exc:
        raise OAuthTokenError(f"{action} response is not valid JSON") from exc
    return parsed


async def exchange_code_for_token(
    *,
    token_endpoint: str,
    client_id: str,
    client_secret: Optional[str],
    code: str,
    redirect_uri: str,
    code_verifier: str,
    client: httpx.AsyncClient,
) -> TokenSet:
    """RFC 6749 §4.1.3 authorization-code token exchange with PKCE.

    ``token_endpoint``/``client_id``/``client_secret`` MUST come from the
    caller's ``mcp_source_oauth_clients`` row for the target source — see
    the module docstring's mix-up-defense note. Redirects are never
    followed on this call (an AS redirecting a token response is never
    legitimate).
    """
    body = await _post_token_request(
        token_endpoint=token_endpoint,
        client_id=client_id,
        client_secret=client_secret,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": code_verifier,
        },
        client=client,
        action="token exchange",
    )
    return _token_set_from_response(body)


async def refresh_access_token(
    *,
    token_endpoint: str,
    client_id: str,
    client_secret: Optional[str],
    refresh_token: str,
    client: httpx.AsyncClient,
) -> TokenSet:
    """RFC 6749 §6 refresh-token grant.

    Same mix-up-defense contract as :func:`exchange_code_for_token` —
    ``token_endpoint``/``client_id``/``client_secret`` MUST come from the
    stored ``mcp_source_oauth_clients`` row, never from caller-supplied
    request data.
    """
    body = await _post_token_request(
        token_endpoint=token_endpoint,
        client_id=client_id,
        client_secret=client_secret,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
        client=client,
        action="token refresh",
    )
    return _token_set_from_response(body)


def is_invalid_grant_error(exc: BaseException) -> bool:
    """True iff ``exc`` (an :class:`OAuthTokenError`) represents the AS's
    ``invalid_grant`` error — the signal that the stored refresh token is
    dead and the row should be deleted (forces re-connect) rather than
    retried."""
    return isinstance(exc, OAuthTokenError) and "invalid_grant" in str(exc)


__all__: List[str] = [
    "OAuthDiscoveryError",
    "OAuthTokenError",
    "TokenSet",
    "RegisteredOAuthClient",
    "build_oauth_http_client",
    "discover_protected_resource_metadata",
    "resolve_issuer",
    "discover_as_metadata",
    "require_pkce_s256",
    "require_https_endpoints",
    "generate_pkce_pair",
    "register_dynamic_client",
    "best_effort_revoke_registration",
    "exchange_code_for_token",
    "refresh_access_token",
    "is_invalid_grant_error",
]
