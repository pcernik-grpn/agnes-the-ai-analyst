"""Microsoft Graph app-only client for the SharePoint connect wizard's
folder-tree browser (spec 2026-08-27 §13.2).

Not a producer — the actual crawl lives outside Agnes (the producer
pipeline, §7.1). This module exists only so the wizard's step-2 tree endpoint
(``GET /api/admin/sharepoint/connections/{id}/tree``) can show an admin real
sites/drives/folders to pick a scope from, using the same certificate the
producer will eventually use.

Auth: Entra ID's certificate-credential ``client_credentials`` flow — a JWT
"client assertion" signed with the connection's private key, presented to the
tenant's token endpoint. The assertion's ``x5t`` header (the certificate's
SHA-1 thumbprint, base64url) is how Entra matches the signature to the
certificate uploaded on the app registration, so the resolved certificate
material (:func:`connectors.sharepoint.settings.resolve_sharepoint_settings`)
must be a combined PEM — the certificate followed by its private key, exactly
what an admin generates for an Entra app registration's certificate
credential — not the private key alone.
"""

from __future__ import annotations

import base64
import fnmatch
import logging
import re
import time
import unicodedata
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
import jwt as pyjwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import load_pem_private_key

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
LOGIN_BASE = "https://login.microsoftonline.com"

_TOKEN_TIMEOUT_S = 15.0
_GRAPH_TIMEOUT_S = 20.0

# One Graph page is enough for the wizard's browse call — this is an admin
# picking a scope interactively, not the producer's durable site index.
# `/sites?search=*` is known to under-return for app-only tokens (§7.1); the
# producer's crawler works around that with `getAllSites`, a hardening this
# one-shot browse call deliberately does not replicate.
_SITES_PAGE_SIZE = 50

_PEM_BLOCK_RE = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----.*?-----END \1-----", re.DOTALL)


class SharePointGraphError(RuntimeError):
    """A Graph/Entra call failed, or the certificate material could not be
    parsed into a signable assertion. Never carries the certificate, the
    signed assertion, or an access token in its message — only status codes
    and upstream error bodies, which Entra/Graph document as safe to log.

    ``status_code`` is the upstream HTTP status when known (``None`` for a
    parse-time failure, e.g. malformed certificate material, that never made
    a Graph/Entra call). :func:`search_folders` reads it to classify a
    failure: a 403/404 on one site or folder is a routine permissions fact
    to skip past, everything else (network failure, 401, 429, 5xx) means the
    whole walk is broken and must propagate.
    """

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _http_client() -> httpx.AsyncClient:
    """Test seam: monkeypatched to return an ``httpx.AsyncClient`` wired to
    an ``httpx.MockTransport`` (see ``tests/test_teams_sigverify.py`` for the
    same idiom — no live network in unit tests)."""
    return httpx.AsyncClient(timeout=_GRAPH_TIMEOUT_S)


def _split_pem_blocks(pem_text: str) -> Dict[str, str]:
    """First PEM block per label, keyed to ``CERTIFICATE`` / ``PRIVATE KEY``.

    Any private-key label (``PRIVATE KEY``, ``RSA PRIVATE KEY``, ``EC
    PRIVATE KEY``) collapses to the one key ``PRIVATE KEY`` — callers only
    ever need "the" private key block, never which flavor produced it.
    """
    blocks: Dict[str, str] = {}
    for match in _PEM_BLOCK_RE.finditer(pem_text or ""):
        label = match.group(1)
        key = "PRIVATE KEY" if label.endswith("PRIVATE KEY") else label
        blocks.setdefault(key, match.group(0))
    return blocks


def _x5t_thumbprint(cert: x509.Certificate) -> str:
    """Base64url SHA-1 thumbprint of ``cert``, no padding — the exact value
    Entra's certificate-credential flow expects in the JWT assertion's
    ``x5t`` header, and so the value an admin should compare against the
    certificate uploaded on the app registration. Canonical computation —
    :func:`build_client_assertion` and :func:`certificate_metadata` both
    call this rather than each hashing the certificate their own way.
    """
    return base64.urlsafe_b64encode(cert.fingerprint(hashes.SHA1())).decode().rstrip("=")


def build_client_assertion(tenant_id: str, client_id: str, private_key_pem: str) -> str:
    """Sign the JWT bearer client assertion for Entra's certificate-credential
    ``client_credentials`` flow.

    Raises :class:`SharePointGraphError` — never a bare parser exception —
    if the resolved certificate material has no ``CERTIFICATE`` or private
    key PEM block, or either block fails to parse.
    """
    blocks = _split_pem_blocks(private_key_pem)
    cert_pem = blocks.get("CERTIFICATE")
    key_pem = blocks.get("PRIVATE KEY")
    if not cert_pem or not key_pem:
        raise SharePointGraphError(
            "sharepoint certificate material is missing a CERTIFICATE or PRIVATE KEY PEM block — "
            "Entra's certificate-credential flow needs both: the public certificate (for the JWT "
            "assertion's x5t thumbprint) and the private key (to sign it), concatenated in one PEM"
        )
    try:
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        private_key = load_pem_private_key(key_pem.encode(), password=None)
    except Exception as exc:  # noqa: BLE001 — malformed PEM; message names no secret material
        raise SharePointGraphError(f"sharepoint certificate material could not be parsed: {exc}") from exc

    thumbprint = _x5t_thumbprint(cert)
    now = int(time.time())
    audience = f"{LOGIN_BASE}/{tenant_id}/oauth2/v2.0/token"
    claims = {
        "iss": client_id,
        "sub": client_id,
        "aud": audience,
        "jti": str(uuid.uuid4()),
        "nbf": now,
        "exp": now + 300,  # Entra ignores anything over ~10 minutes; 5 is comfortable for one call.
    }
    return pyjwt.encode(claims, private_key, algorithm="RS256", headers={"x5t": thumbprint})


#: A certificate within this many days of ``not_after`` is flagged
#: ``expiring_soon`` rather than ``ok`` — the admin's early-warning window
#: for the "certificate expires silently, auth breaks with no warning"
#: failure mode :func:`certificate_metadata` exists to catch.
_EXPIRING_SOON_DAYS = 30


def certificate_metadata(private_key_pem: str) -> Dict[str, Any]:
    """Read-only metadata about the CERTIFICATE half of ``private_key_pem``
    (the combined cert+key PEM :func:`connectors.sharepoint.settings.
    resolve_sharepoint_settings` returns) — for an admin to compare the
    thumbprint against the identity provider's app registration and catch
    an expiring certificate before auth breaks.

    SECURITY: the private key half is never parsed, touched, or referenced
    here — only the ``CERTIFICATE`` PEM block is read out of ``blocks``, so
    no key material can reach the return value even if a caller serializes
    it verbatim into an API response.

    Never raises. A missing ``CERTIFICATE`` block or a block that fails to
    parse both come back as ``{"certificate": None, "reason": "..."}`` — a
    typed absence, not a 500 — so a caller (the admin API, the source card)
    never needs its own try/except to stay off one.
    """
    blocks = _split_pem_blocks(private_key_pem)
    cert_pem = blocks.get("CERTIFICATE")
    if not cert_pem:
        return {"certificate": None, "reason": "no_certificate_configured"}
    try:
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
    except Exception as exc:  # noqa: BLE001 — malformed PEM; message names no secret material
        return {"certificate": None, "reason": f"certificate_unparseable: {exc}"}

    not_before = cert.not_valid_before_utc
    not_after = cert.not_valid_after_utc
    now = datetime.now(timezone.utc)
    expires_in_days = int((not_after - now).total_seconds() // 86400)
    if not_after <= now:
        status = "expired"
    elif expires_in_days <= _EXPIRING_SOON_DAYS:
        status = "expiring_soon"
    else:
        status = "ok"

    return {
        "certificate": {
            # The value the client actually presents as the JWT `x5t`
            # header — what an admin compares against the identity
            # provider's app registration (module docstring).
            "thumbprint_x5t": _x5t_thumbprint(cert),
            # The conventional uppercase-hex SHA-1 fingerprint, the form
            # most identity-provider UIs (incl. Entra's app registration
            # certificate list) display next to an uploaded certificate.
            "thumbprint_sha1_hex": cert.fingerprint(hashes.SHA1()).hex().upper(),
            "subject": cert.subject.rfc4514_string(),
            "issuer": cert.issuer.rfc4514_string(),
            "not_before": not_before.isoformat(),
            "not_after": not_after.isoformat(),
            "expires_in_days": expires_in_days,
            "status": status,
        },
        "reason": None,
    }


async def get_app_token(tenant_id: str, client_id: str, private_key_pem: str) -> str:
    """Fetch an app-only Graph access token via the certificate-credential
    flow. Raises :class:`SharePointGraphError` on any non-200 response or a
    response with no ``access_token`` — never returns a falsy token."""
    assertion = build_client_assertion(tenant_id, client_id, private_key_pem)
    url = f"{LOGIN_BASE}/{tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "scope": "https://graph.microsoft.com/.default",
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": assertion,
        "grant_type": "client_credentials",
    }
    async with _http_client() as client:
        resp = await client.post(url, data=data, timeout=_TOKEN_TIMEOUT_S)
    if resp.status_code != 200:
        # Entra's token-endpoint error bodies name the failure (invalid_client,
        # unauthorized_client, ...) without ever echoing the assertion we sent.
        logger.warning(
            "sharepoint token request failed for tenant %s: HTTP %s %s",
            tenant_id,
            resp.status_code,
            resp.text[:500],
        )
        raise SharePointGraphError(f"token request failed: HTTP {resp.status_code}", status_code=resp.status_code)
    body = resp.json()
    token = body.get("access_token")
    if not token:
        raise SharePointGraphError("token response had no access_token")
    return str(token)


async def _graph_get(access_token: str, path: str, *, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    async with _http_client() as client:
        resp = await client.get(
            f"{GRAPH_BASE}{path}",
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code != 200:
        logger.warning("sharepoint graph call %s failed: HTTP %s %s", path, resp.status_code, resp.text[:500])
        raise SharePointGraphError(
            f"Graph request to {path} failed: HTTP {resp.status_code}", status_code=resp.status_code
        )
    result: Dict[str, Any] = resp.json()
    return result


async def list_sites(access_token: str) -> List[Dict[str, Any]]:
    """Sites reachable by this app registration — one Graph call, first page
    only (see the module docstring's under-return caveat)."""
    body = await _graph_get(
        access_token,
        "/sites",
        params={"search": "*", "$select": "id,name,displayName,webUrl", "$top": _SITES_PAGE_SIZE},
    )
    return [
        {
            "id": site["id"],
            "name": site.get("displayName") or site.get("name") or site["id"],
            "web_url": site.get("webUrl"),
        }
        for site in body.get("value", [])
    ]


async def list_drives(access_token: str, site_id: str) -> List[Dict[str, Any]]:
    """Document libraries (drives) of one site."""
    body = await _graph_get(
        access_token,
        f"/sites/{site_id}/drives",
        params={"$select": "id,name,driveType"},
    )
    return [
        {"id": drive["id"], "name": drive.get("name") or drive["id"], "drive_type": drive.get("driveType")}
        for drive in body.get("value", [])
    ]


def _map_children(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Shared item shape for :func:`list_root_children` and
    :func:`list_item_children` — the same ``{id, name, is_folder,
    child_count}`` mapping regardless of which Graph path produced it."""
    return [
        {
            "id": item["id"],
            "name": item.get("name") or item["id"],
            "is_folder": "folder" in item,
            "child_count": (item.get("folder") or {}).get("childCount"),
        }
        for item in body.get("value", [])
    ]


async def list_root_children(access_token: str, drive_id: str) -> List[Dict[str, Any]]:
    """Root-level items of one drive."""
    body = await _graph_get(
        access_token,
        f"/drives/{drive_id}/root/children",
        params={"$select": "id,name,folder,file"},
    )
    return _map_children(body)


async def list_item_children(access_token: str, drive_id: str, item_id: str) -> List[Dict[str, Any]]:
    """Children of an arbitrary folder within one drive (TCRD-240) — the same
    item shape as :func:`list_root_children`, generalized past the drive root
    so the wizard's tree can be browsed (and searched) at any depth.

    ``drive_id``/``item_id`` are sent as opaque Graph path segments only —
    callers must structurally validate them first (see
    ``app.api.admin_sharepoint._validate_graph_id``); this function never
    builds a filesystem path from either.
    """
    body = await _graph_get(
        access_token,
        f"/drives/{drive_id}/items/{item_id}/children",
        params={"$select": "id,name,folder,file"},
    )
    return _map_children(body)


async def _list_children(access_token: str, drive_id: str, item_id: Optional[str]) -> List[Dict[str, Any]]:
    """One level of children, at the drive root (``item_id is None``) or of
    an arbitrary folder — the single seam :func:`search_folders` walks
    through, so the BFS never has to know which Graph path it is calling."""
    if item_id is None:
        return await list_root_children(access_token, drive_id)
    return await list_item_children(access_token, drive_id, item_id)


# ---------------------------------------------------------------------------
# Folder search (TCRD-240): bounded BFS over children enumeration.
#
# Graph's own `/search` endpoint is known to silently under-return under
# app-only auth (module docstring's `_SITES_PAGE_SIZE` note, and the
# fact-graph spec §7); this module never calls it. "Search" here means a
# bounded breadth-first walk over the same `list_sites` / `list_drives` /
# `list_root_children` / `list_item_children` calls the tree browser already
# makes — capped by depth and by the number of folders expanded, and honest
# (`truncated: true`) whenever either cap is what stopped it.
# ---------------------------------------------------------------------------


def _fold(text: str) -> str:
    """Unicode-NFC-normalize then casefold — the comparison key both sides
    of every search match go through, so composed vs. decomposed input
    (e.g. a precomposed ``ř`` vs. ``r`` + combining caron) and case never
    change whether two folder names are "the same" for matching purposes.
    Does NOT strip diacritics — ``e`` does not match ``é`` here, only the
    same letter in a different case or composition does; a laxer,
    diacritics-stripping compare is the CLIENT-side instant filter's job,
    not this server-side search's."""
    return unicodedata.normalize("NFC", text or "").casefold()


def build_folder_matcher(query: str, mode: str) -> Callable[[str], bool]:
    """A ``name -> bool`` predicate for one search request.

    ``mode``: ``prefix`` (default), ``contains``, or ``glob`` (``fnmatch``
    syntax, case/composition-insensitive via :func:`_fold`). Raises
    :class:`ValueError` — never lets a bad pattern reach Graph — for an
    unknown mode or a glob pattern with unbalanced ``[``/``]`` brackets.
    ``fnmatch.translate`` itself never raises (it treats a stray bracket as
    a literal character), so this bracket-balance check is this module's
    own definition of "malformed": the one case a typo plausibly produces
    that ``fnmatch`` would otherwise silently accept.
    """
    folded_query = _fold(query)
    if mode == "prefix":
        return lambda name: _fold(name).startswith(folded_query)
    if mode == "contains":
        return lambda name: folded_query in _fold(name)
    if mode == "glob":
        if query.count("[") != query.count("]"):
            raise ValueError(f"malformed glob pattern: unbalanced '[' / ']' in {query!r}")
        pattern = re.compile(fnmatch.translate(folded_query))
        return lambda name: pattern.match(_fold(name)) is not None
    raise ValueError(f"unknown search mode: {mode!r}")


#: A 403 (Forbidden) or 404 (Not Found) on one site/folder is a routine
#: permissions fact — app-only Graph access across a real tenant is never
#: uniform (`Sites.Selected` grants, departmental sites, restricted
#: libraries) — so :func:`search_folders` skips past it and keeps walking.
#: Anything else (network failure, 401 unauthorized, 429 rate-limited, a
#: 5xx) means the call itself is broken, not merely refused, and must
#: propagate — swallowing it per-site would turn a broken connection into an
#: empty, successful-looking search across the whole tenant.
_PERMISSION_SKIP_STATUS_CODES = frozenset({403, 404})


def _skip_reason(status_code: int) -> str:
    return "forbidden" if status_code == 403 else "not_found"


async def search_folders(
    access_token: str,
    *,
    matcher: Callable[[str], bool],
    drive_id: Optional[str] = None,
    item_id: Optional[str] = None,
    max_depth: int = 5,
    max_visited: int = 500,
) -> Dict[str, Any]:
    """Bounded BFS for folders satisfying ``matcher``.

    Root selection:
    - ``drive_id`` + ``item_id`` given: search that folder's own subtree.
    - ``drive_id`` given, ``item_id`` omitted: search that whole drive.
    - Neither given: search every drive of every reachable site (the same
      breadth :func:`list_sites`/:func:`list_drives` already expose, walked
      past their root level) — the wizard's "search everywhere" default.

    Depth counts folder levels below the search root(s): a root's own
    direct children are depth 0. ``max_visited`` counts every "list
    children" Graph call the walk makes (site/drive expansion when
    enumerating "every reachable site", plus every folder expansion) — the
    one budget that bounds a walk regardless of how it is rooted.

    **Permissions are not errors.** A site (during "search everywhere" root
    selection) or a folder (mid-walk) that answers 403/404 is skipped, not
    fatal — see :data:`_PERMISSION_SKIP_STATUS_CODES`. Skipping a folder
    never discards matches already found: a folder itself is recorded as a
    match (if it satisfies ``matcher``) when Graph lists it as a CHILD of
    its parent, before the walk ever tries to descend into it, so a 403 on
    its own children listing only stops descent, not the match already on
    the list. Anything else Graph/`httpx` can raise (network failure, 401,
    429, 5xx) means the whole walk is broken and propagates uncaught — see
    :class:`SharePointGraphError`.

    Returns ``{"matches": [{"item_id", "drive_id", "display_path"}, ...],
    "visited": int, "truncated": bool, "skipped": [...]}``.

    - ``truncated`` is ``True`` whenever ``max_depth`` or ``max_visited`` is
      what stopped the walk short of covering everything reachable from the
      root(s) — never a silent partial result. Deliberately distinct from
      permission gaps below: a cap and a permission refusal are different
      facts to a caller, and conflating them into one flag would make
      "scope the search narrower" (the cap's fix) look like the right
      response to "ask for access to this site" (the permission fix).
    - ``skipped`` lists every site/folder the walk could not enter for
      permissions reasons, each as ``{"scope": "site"|"folder", "reason":
      "forbidden"|"not_found", "status_code": int, "site_id", "site_name",
      "drive_id", "item_id", "display_path"}`` (the fields not known at that
      scope are ``None``) — so a caller can tell "searched everything" from
      "searched what it could reach" and name what it skipped, honoring the
      no-silent-partial-result contract the same way ``truncated`` does.
    """
    roots: List[Tuple[str, Optional[str], List[str]]] = []
    visited = 0
    truncated = False
    skipped: List[Dict[str, Any]] = []

    if drive_id:
        roots.append((drive_id, item_id, []))
    else:
        sites = await list_sites(access_token)
        for site in sites:
            visited += 1
            if visited > max_visited:
                truncated = True
                break
            try:
                drives = await list_drives(access_token, site["id"])
            except SharePointGraphError as exc:
                if exc.status_code not in _PERMISSION_SKIP_STATUS_CODES:
                    raise
                skipped.append(
                    {
                        "scope": "site",
                        "reason": _skip_reason(exc.status_code),
                        "status_code": exc.status_code,
                        "site_id": site["id"],
                        "site_name": site.get("name"),
                        "drive_id": None,
                        "item_id": None,
                        "display_path": None,
                    }
                )
                continue
            for drv in drives:
                roots.append((drv["id"], None, [site["name"], drv["name"]]))

    matches: List[Dict[str, str]] = []
    # BFS queue entries: (drive_id, item_id_or_None, depth, path_prefix).
    queue: "deque[Tuple[str, Optional[str], int, List[str]]]" = deque((r[0], r[1], 0, r[2]) for r in roots)

    while queue:
        if visited >= max_visited:
            truncated = True
            break
        cur_drive, cur_item, depth, prefix = queue.popleft()
        try:
            children = await _list_children(access_token, cur_drive, cur_item)
        except SharePointGraphError as exc:
            visited += 1
            if exc.status_code not in _PERMISSION_SKIP_STATUS_CODES:
                raise
            skipped.append(
                {
                    "scope": "folder",
                    "reason": _skip_reason(exc.status_code),
                    "status_code": exc.status_code,
                    "site_id": None,
                    "site_name": None,
                    "drive_id": cur_drive,
                    "item_id": cur_item,
                    "display_path": " / ".join(prefix) if prefix else None,
                }
            )
            continue
        visited += 1
        for child in children:
            name = child["name"]
            path = prefix + [name]
            if child["is_folder"] and matcher(name):
                matches.append({"item_id": child["id"], "drive_id": cur_drive, "display_path": " / ".join(path)})
            if child["is_folder"]:
                if depth + 1 > max_depth:
                    truncated = True
                    continue
                queue.append((cur_drive, child["id"], depth + 1, path))

    if queue:
        truncated = True

    return {"matches": matches, "visited": visited, "truncated": truncated, "skipped": skipped}


# ---------------------------------------------------------------------------
# Unique-permissions advisory probe (design spec §13.1/§13.2). Decision #2
# stands: Agnes does not derive or enforce anything from SharePoint ACLs —
# this exists ONLY to warn an admin in the connect wizard that a folder
# breaks permission inheritance from its parent in the source, never to
# gate, filter, or imply Agnes reads/respects that inheritance itself.
# ---------------------------------------------------------------------------

#: Graph's own hard cap on sub-requests in one ``POST /$batch`` call — not a
#: tuning knob, the actual ceiling Graph enforces.
_GRAPH_BATCH_SIZE_CAP = 20


async def probe_unique_permissions(access_token: str, drive_id: str, item_ids: List[str]) -> Dict[str, Optional[bool]]:
    """Best-effort, per-item "does this folder break permission inheritance
    from its parent" signal for the wizard's unique-permissions badge —
    ADVISORY ONLY (module header + design spec §13.1: Agnes does not derive
    or enforce anything from SharePoint ACLs today; this exists to warn an
    admin, never to gate a scope).

    **Graph shape, and the uncertainty around it.** SharePoint's classic API
    exposes ``ListItem.HasUniqueRoleAssignments`` — a plain boolean, "this
    item's role assignments are not inherited from its parent" — which the
    design spec's own §13.1 names as the intended later signal
    (``per-item only where HasUniqueRoleAssignments``). Microsoft Graph does
    not document a top-level ``driveItem`` property for it, but the
    underlying SharePoint list item is reachable by expanding a driveItem's
    ``listItem`` navigation property and selecting the field:
    ``GET /drives/{drive}/items/{item}?$expand=listItem($select=
    hasUniqueRoleAssignments)``. Chosen over enumerating the item's full
    ``permissions`` collection (``/drives/{drive}/items/{item}/permissions``)
    and inferring uniqueness from the absence of ``inheritedFrom`` on an
    entry — that needs no extra scope beyond what browsing the tree already
    uses, but is a much larger, harder-to-reason-about response per item, for
    a page of results that already needs to stay cheap and batched. If Graph
    ever stops returning this field, or returns it under a different shape,
    every affected item degrades to ``None`` (unknown) below — never a wrong
    ``False``.

    **Batching.** One ``POST /$batch`` per up-to-:data:`_GRAPH_BATCH_SIZE_CAP`
    (20, Graph's own limit) items; ``item_ids`` longer than that are split
    into sequential batches.

    **Never raises.** A failure at the whole-batch level (network error,
    non-200 on ``/$batch`` itself) or at the per-item level (a missing
    sub-response, a non-200 sub-status, or a 200 whose body has no
    ``listItem.hasUniqueRoleAssignments``) maps the affected item id(s) to
    ``None`` — "unknown", the only honest answer when the probe itself could
    not run. Callers (the tree endpoint) must never render ``None`` as "no
    unique permissions" — only a bare ``True`` earns the badge.
    """
    result: Dict[str, Optional[bool]] = {}
    ids = list(item_ids)
    for start in range(0, len(ids), _GRAPH_BATCH_SIZE_CAP):
        chunk = ids[start : start + _GRAPH_BATCH_SIZE_CAP]
        requests = [
            {
                "id": str(i),
                "method": "GET",
                "url": (
                    f"/drives/{drive_id}/items/{item_id}?$expand=listItem($select=hasUniqueRoleAssignments)&$select=id"
                ),
            }
            for i, item_id in enumerate(chunk)
        ]
        try:
            async with _http_client() as client:
                resp = await client.post(
                    f"{GRAPH_BASE}/$batch",
                    json={"requests": requests},
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=_GRAPH_TIMEOUT_S,
                )
            if resp.status_code != 200:
                logger.warning(
                    "sharepoint unique-permissions batch probe failed: HTTP %s %s",
                    resp.status_code,
                    resp.text[:500],
                )
                for item_id in chunk:
                    result[item_id] = None
                continue
            body = resp.json()
        except Exception:  # noqa: BLE001 — advisory probe, must never block/fail browsing
            logger.warning("sharepoint unique-permissions batch probe raised", exc_info=True)
            for item_id in chunk:
                result[item_id] = None
            continue

        by_request_id = {str(entry.get("id")): entry for entry in body.get("responses", []) if isinstance(entry, dict)}
        for i, item_id in enumerate(chunk):
            sub = by_request_id.get(str(i))
            value: Optional[bool] = None
            if sub is not None and sub.get("status") == 200:
                list_item = (sub.get("body") or {}).get("listItem") or {}
                raw = list_item.get("hasUniqueRoleAssignments")
                if isinstance(raw, bool):
                    value = raw
            result[item_id] = value
    return result


# ---------------------------------------------------------------------------
# ACL mirroring readers (design spec 2026-08-28 §6 link 1): unlike the probe
# above, these two ARE read for enforcement — the ACL sync job (2026-08-30
# plan, Task 4) classifies their output into Agnes groups/grants. Both page
# via ``@odata.nextLink``, the standard Graph collection convention.
# ---------------------------------------------------------------------------


async def _graph_get_all_pages(
    access_token: str, path: str, *, params: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """Collect ``value`` across ``@odata.nextLink`` pages (permissions and
    transitiveMembers both page; a single-page read costs nothing extra)."""
    items: List[Dict[str, Any]] = []
    body = await _graph_get(access_token, path, params=params)
    while True:
        items.extend(body.get("value") or [])
        next_link = body.get("@odata.nextLink")
        if not next_link:
            return items
        # nextLink is absolute and already carries the query string; _graph_get
        # prepends GRAPH_BASE itself, so strip that same prefix back off.
        body = await _graph_get(access_token, next_link.split("/v1.0", 1)[-1])


async def list_item_permissions(access_token: str, drive_id: str, item_id: str) -> List[Dict[str, Any]]:
    """Raw Graph ``permission`` objects on one drive item (the scope root) —
    the sync's per-run read of who currently has access. App-only requires
    ``Sites.FullControl.All`` or a per-site ``Sites.Selected`` full-control
    role; a Graph failure surfaces as :class:`SharePointGraphError`, never
    swallowed (the caller marks the run/scope failed and audits it)."""
    return await _graph_get_all_pages(access_token, f"/drives/{drive_id}/items/{item_id}/permissions")


async def list_group_transitive_members(access_token: str, group_id: str) -> List[Dict[str, Any]]:
    """Transitive USER members of an Entra group — nested groups are
    flattened by Graph itself; non-user directory objects (nested groups,
    service principals, ...) are dropped here so callers never have to
    re-check ``@odata.type``. Requires ``GroupMember.Read.All`` (NOT
    included in ``Sites.FullControl.All`` — a separate app-registration
    permission grant)."""
    rows = await _graph_get_all_pages(
        access_token,
        f"/groups/{group_id}/transitiveMembers",
        params={"$select": "id,mail,userPrincipalName", "$top": "999"},
    )
    return [r for r in rows if r.get("@odata.type") == "#microsoft.graph.user"]
