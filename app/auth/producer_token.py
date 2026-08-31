"""Producer-scoped callback credential for corpus-extraction runs.

Replaces the historical over-grant documented (and now removed) from
``app/worker/kinds.py::_agnes_producer_callback_env``'s docstring: forwarding
the scheduler shared secret (``app.auth.scheduler_token``) to the external
producer subprocess, which resolves to a synthetic ``Admin``-group user —
god-mode on every RBAC check — while the producer only ever needs to read
ONE connection's own scope map, upload into its own scope collections, and
call the facts-ingest/corrections endpoints.

Unlike the scheduler token (an opaque shared secret compared with
``hmac.compare_digest``), this is a genuine short-lived JWT
(``app.auth.jwt``, ``typ="producer"``) minted fresh for one extraction run,
carrying ``connection_id`` and ``collection_ids`` claims. It resolves to a
restricted ``ProducerPrincipal`` (``app.auth.session_principal``) — never a
real user row, never Admin — via :func:`resolve_producer_principal`, the
bearer-auth resolution branch ``app.auth.dependencies.get_current_user``
checks right alongside ``app.auth.scheduler_token.is_scheduler_token``
(same position in the chain, same reason: this ``typ`` carries no ``sub``
naming a real user, so feeding it to the normal session/PAT chain would
either mis-decode or 401 with ``user_not_found``).

Fail-closed surface allowlist: the JWT decodes and verifies fine on ANY
route (it is a normal signed token) but is only ACCEPTED on the small,
fixed set of endpoints the producer subprocess actually calls
(``_PRODUCER_ALLOWED_SURFACE`` below) — mirroring
``app.auth.pat_resolver``'s ``_AGENT_PAT_ALLOWED_PREFIXES`` /
``_DATA_APP_ALLOWED_EXACT`` gates for the same reason: without this, a
producer token would additionally authenticate as "any authenticated
caller" on every route gated on nothing more than
``Depends(get_current_user)`` (e.g. the facts read surface,
``app/api/facts.py``'s ``/facets``/``/search``/``/neighbors``/
``/{subject_id}/claims``) — a far wider grant than the producer subprocess
is actually meant to have. Checked HERE, at resolution time, rather than
trusting each endpoint's own gate to reject it correctly: one fail-closed
choke point instead of N independently-reasoned-about ones.

Off-surface answers ``403`` (Forbidden — the token IS a valid credential,
it simply has no authority here), matching the ``403`` each ALLOWED
endpoint's own fine-grained scope check (``connection_id``/
``collection_ids`` mismatch) also answers with — one consistent status for
"this producer token cannot do that", however it was refused.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Iterable, Optional

from fastapi import HTTPException, Request, status

from app.auth.jwt import create_access_token, verify_token
from app.auth.session_principal import ProducerPrincipal

#: JWT ``typ`` claim minted by :func:`mint_producer_token`.
PRODUCER_TOKEN_TYP = "producer"

#: (HTTP method, path template) exactly as registered on the router — see
#: the module docstring. A request whose (method, path) doesn't reconstruct
#: EXACTLY one of these templates from ``request.path_params`` is refused
#: before a :class:`ProducerPrincipal` is ever constructed. Each of the
#: five endpoints ALSO applies its own scope check (connection_id /
#: collection_ids) — this allowlist only says WHICH endpoints are even
#: eligible to try.
_PRODUCER_ALLOWED_SURFACE: tuple[tuple[str, str], ...] = (
    ("GET", "/api/admin/sharepoint/connections/{connection_id}/corpus-map"),
    ("GET", "/api/admin/sharepoint/connections/{connection_id}/scopes"),
    ("POST", "/api/collections/{collection_id}/files"),
    ("POST", "/api/facts/ingest"),
    ("GET", "/api/facts/corrections"),
)


def mint_producer_token(*, connection_id: str, collection_ids: Iterable[str], ttl_seconds: int) -> str:
    """Mint a short-lived producer-scoped JWT (``typ="producer"``) for one
    corpus-extraction run.

    ``collection_ids`` is stored SORTED — a deterministic claim shape (not
    a security requirement) so two mints for the same connection are
    byte-identical modulo ``jti``/``exp``.

    No DB row, no revocation list: the token IS its own bounded authority,
    expiring ``ttl_seconds`` from now. The caller
    (``app.worker.kinds._agnes_producer_callback_env``) sizes that to the
    job's own ``extraction.timeout_s`` plus a fixed grace window, so a run
    that legitimately takes the full configured timeout never has its
    callback credential expire out from under it mid-run.
    """
    return create_access_token(
        user_id="",
        email="",
        typ=PRODUCER_TOKEN_TYP,
        expires_delta=timedelta(seconds=ttl_seconds),
        extra_claims={
            "connection_id": connection_id,
            "collection_ids": sorted({str(c) for c in collection_ids}),
        },
    )


def _producer_claims(token: str) -> Optional[dict]:
    """Decode+verify ``token``; return its payload iff it is a well-formed
    producer JWT (``typ="producer"`` with ``connection_id`` +
    ``collection_ids`` claims), else ``None`` — the "not one of mine, fall
    through" signal ``get_current_user`` relies on (same contract as
    ``app.auth.scheduler_token.is_scheduler_token``, minus the opaque-secret
    comparison since this is a real, signature-verified JWT). ``None`` also
    covers an expired or tampered producer token — ``verify_token`` already
    returns ``None`` for those, so they fall through to the normal PAT/
    session chain and 401 there as ``invalid_token``/``user_not_found``.
    """
    payload = verify_token(token)
    if not payload or payload.get("typ") != PRODUCER_TOKEN_TYP:
        return None
    connection_id = payload.get("connection_id")
    collection_ids = payload.get("collection_ids")
    if not connection_id or not isinstance(collection_ids, list):
        return None
    return payload


def _on_allowed_surface(request: Optional[Request]) -> bool:
    """True iff ``request`` is one of the five (method, path) combinations
    a producer token may ever authenticate. ``request is None`` (no HTTP
    request context — should not happen for this REST-only credential) is
    treated as off-surface, fail-closed."""
    if request is None:
        return False
    method = request.method.upper()
    path = request.url.path
    for allowed_method, template in _PRODUCER_ALLOWED_SURFACE:
        if allowed_method != method:
            continue
        try:
            expected = template.format(**request.path_params)
        except KeyError:
            continue
        if expected == path:
            return True
    return False


def resolve_producer_principal(token: str, request: Optional[Request]) -> Optional[ProducerPrincipal]:
    """Bearer-auth resolution branch for a producer token — the
    corpus-extraction analogue of ``app.auth.scheduler_token.
    get_scheduler_user``.

    Returns ``None`` when ``token`` is not a producer JWT at all (wrong
    ``typ``, malformed claims, bad signature, or expired), so the caller
    falls through to the normal session/PAT chain.

    Raises ``403`` when the JWT decodes as a genuine, live producer token
    but the CURRENT request is off its fixed allowed surface (see module
    docstring) — this is authentication succeeding and authorization
    failing outright, not "not a producer token", so it must not fall
    through to a chain that would try to resolve it as a user and 401.
    """
    payload = _producer_claims(token)
    if payload is None:
        return None
    if not _on_allowed_surface(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="producer_token_wrong_surface",
        )
    return ProducerPrincipal(
        connection_id=str(payload["connection_id"]),
        collection_ids=frozenset(str(c) for c in payload["collection_ids"]),
        jti=str(payload.get("jti") or ""),
    )
