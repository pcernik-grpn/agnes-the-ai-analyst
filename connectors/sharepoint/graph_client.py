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
import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional

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
    and upstream error bodies, which Entra/Graph document as safe to log."""


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

    thumbprint = base64.urlsafe_b64encode(cert.fingerprint(hashes.SHA1())).decode().rstrip("=")
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
        raise SharePointGraphError(f"token request failed: HTTP {resp.status_code}")
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
        raise SharePointGraphError(f"Graph request to {path} failed: HTTP {resp.status_code}")
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


async def list_root_children(access_token: str, drive_id: str) -> List[Dict[str, Any]]:
    """Root-level items of one drive — one level, per spec §13.2 ("sites ->
    drives -> root children, one level per call"). The wizard's scope rows
    are picked from this level; there is no deeper recursive browse here."""
    body = await _graph_get(
        access_token,
        f"/drives/{drive_id}/root/children",
        params={"$select": "id,name,folder,file"},
    )
    return [
        {
            "id": item["id"],
            "name": item.get("name") or item["id"],
            "is_folder": "folder" in item,
            "child_count": (item.get("folder") or {}).get("childCount"),
        }
        for item in body.get("value", [])
    ]
