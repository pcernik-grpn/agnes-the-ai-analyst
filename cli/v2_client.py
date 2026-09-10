"""HTTP client helpers for /api/v2/* endpoints (CLI side)."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any
import io

import httpx
import pyarrow as pa

from cli.config import get_server_url, get_token
from cli.error_render import render_error
from cli.server_moved import is_redirect, redirect_body


@dataclass
class V2ClientError(Exception):
    status_code: int
    body: Any
    # `message` retained for backwards compat with any existing caller
    # that reads `.message`. Renderer is the canonical str path now.
    message: str = ""

    def __str__(self) -> str:
        # Prefer the structured renderer — it pretty-prints typed BQ errors
        # (cross_project_forbidden, remote_scan_too_large, etc.) instead
        # of the historical truncate-and-flatten form. Falls back to
        # truncated form for unrecognized bodies, so we never make output
        # WORSE than the status-quo (#160 §4.7).
        return render_error(self.status_code, self.body)


def _headers() -> dict:
    token = get_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _parse_error_body(r: httpx.Response) -> Any:
    if "json" in r.headers.get("content-type", ""):
        try:
            return r.json()
        except Exception:
            return r.text
    return r.text


def _raise_for_status(r: httpx.Response) -> None:
    """Turn a non-success response into a V2ClientError.

    A 3xx is checked FIRST and separately: these helpers only ever guarded
    `>= 400`, so a redirect fell through to `r.json()` on an empty body and
    surfaced as `internal CLI error (JSONDecodeError)` — no status, no
    destination, no remedy. See `cli/server_moved.py` for why the redirect
    is explained rather than followed.
    """
    if is_redirect(r.status_code):
        raise V2ClientError(status_code=r.status_code, body=redirect_body(r, get_server_url()))
    if r.status_code >= 400:
        raise V2ClientError(status_code=r.status_code, body=_parse_error_body(r))


def api_get_json(path: str, *, headers: dict | None = None, **params) -> dict:
    url = f"{get_server_url().rstrip('/')}{path}"
    r = httpx.get(url, headers={**_headers(), **(headers or {})}, params=params or None, timeout=30)
    _raise_for_status(r)
    return r.json()


def api_post_json(path: str, payload: dict, *, headers: dict | None = None) -> dict:
    url = f"{get_server_url().rstrip('/')}{path}"
    r = httpx.post(url, json=payload, headers={**_headers(), **(headers or {})}, timeout=120)
    _raise_for_status(r)
    return r.json()


def api_delete(path: str) -> dict:
    url = f"{get_server_url().rstrip('/')}{path}"
    r = httpx.delete(url, headers=_headers(), timeout=30)
    _raise_for_status(r)
    if not r.content:
        return {}
    if "json" in r.headers.get("content-type", ""):
        return r.json()
    return {}


def api_put_json(path: str, payload: dict) -> dict:
    url = f"{get_server_url().rstrip('/')}{path}"
    r = httpx.put(url, json=payload, headers=_headers(), timeout=30)
    _raise_for_status(r)
    if not r.content:
        return {}
    return r.json()


def api_patch_json(path: str, payload: dict) -> dict:
    url = f"{get_server_url().rstrip('/')}{path}"
    r = httpx.patch(url, json=payload, headers=_headers(), timeout=30)
    _raise_for_status(r)
    if not r.content:
        return {}
    return r.json()


def api_post_multipart(
    path: str,
    *,
    files: dict | None = None,
    data: dict | None = None,
) -> dict:
    """POST a multipart/form-data request — used for Store ZIP/photo uploads.

    `files` mirrors httpx.post(..., files=...): each value is an
    (filename, bytes, content_type) tuple or an open file-like object.
    `data` is the form fields. Returns parsed JSON.
    """
    url = f"{get_server_url().rstrip('/')}{path}"
    r = httpx.post(
        url,
        files=files or None,
        data=data or None,
        headers=_headers(),
        timeout=600,
    )
    _raise_for_status(r)
    return r.json()


def api_put_multipart(
    path: str,
    *,
    files: dict | None = None,
    data: dict | None = None,
) -> dict:
    url = f"{get_server_url().rstrip('/')}{path}"
    r = httpx.put(
        url,
        files=files or None,
        data=data or None,
        headers=_headers(),
        timeout=600,
    )
    _raise_for_status(r)
    return r.json()


def api_get_stream(path: str, dest: "io.IOBase | str", **params) -> int:
    """Stream a binary response (e.g. /bundle.zip) into ``dest``.

    ``dest`` is either a writable binary file-like or a filesystem path.
    Returns the byte count written. Raises V2ClientError on non-2xx with
    the parsed error body.
    """
    url = f"{get_server_url().rstrip('/')}{path}"
    with httpx.stream(
        "GET",
        url,
        headers=_headers(),
        params=params or None,
        timeout=600,
    ) as r:
        # A redirect here is worse than elsewhere: without this the 3xx is
        # not an error, so the loop below writes the redirect's (empty) body
        # to `dest` and reports success — a downloaded file that is not the
        # file. Checked before the `>= 400` branch, which never saw it.
        if is_redirect(r.status_code):
            raise V2ClientError(status_code=r.status_code, body=redirect_body(r, get_server_url()))
        if r.status_code >= 400:
            # Read the (likely small) error body before raising.
            body = b"".join(r.iter_bytes())
            try:
                parsed = httpx.Response(r.status_code, content=body, headers=r.headers)
                raise V2ClientError(status_code=r.status_code, body=_parse_error_body(parsed))
            except V2ClientError:
                raise
        owns = isinstance(dest, str)
        fh = open(dest, "wb") if owns else dest
        total = 0
        try:
            for chunk in r.iter_bytes():
                fh.write(chunk)
                total += len(chunk)
        finally:
            if owns:
                fh.close()
        return total


def _post_arrow(path: str, payload: dict) -> "tuple[pa.Table, httpx.Headers]":
    url = f"{get_server_url().rstrip('/')}{path}"
    r = httpx.post(url, json=payload, headers=_headers(), timeout=600)
    _raise_for_status(r)
    reader = pa.ipc.open_stream(io.BytesIO(r.content))
    return reader.read_all(), r.headers


def api_post_arrow(path: str, payload: dict) -> pa.Table:
    """Post JSON, expect Arrow IPC stream response."""
    table, _headers = _post_arrow(path, payload)
    return table


def api_post_arrow_with_headers(path: str, payload: dict) -> "tuple[pa.Table, httpx.Headers]":
    """Like :func:`api_post_arrow`, but also returns the response headers.

    ``/api/v2/scan`` has no JSON body to carry values like
    ``X-Agnes-Row-Scope``, ``X-Agnes-Policy-Fingerprint`` or
    ``X-Agnes-Policy-Table-Id`` (table access policies §10.3, §3.4) -- a
    caller that needs them (``agnes snapshot create``/``refresh``,
    ``cli/commands/snapshot.py``) reads this instead of the header-blind
    :func:`api_post_arrow`.
    """
    return _post_arrow(path, payload)
