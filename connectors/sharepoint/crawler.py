"""Built-in SharePoint document crawler — the in-process producer.

Owner decision 2026-08-31 overrides the "the producer is never vendored"
rule this connector was originally written under: the crawl -> convert ->
(anonymize) -> ingest pipeline is now a FIRST-CLASS Agnes pipeline, selected
with ``extraction.producer.mode: builtin``. The external-producer subprocess
seam (``app/worker/kinds.py::_run_corpus_extraction``) stays exactly as it
was and remains the default whenever a producer command/module is
configured — this module is the other branch of that fork, not its
replacement.

What it does, per confirmed scope of one ``source_connections`` row:

1. Resolve the scope to one or more DRIVES (a site scope fans out to every
   document library; a drive scope is itself; a folder scope needs the
   scope row's ``drive_id`` and delta-enumerates that folder's subtree).
2. Enumerate each drive with Graph's ``delta`` feed, resuming from the
   persisted ``@odata.deltaLink`` so a re-run only sees what changed.
3. Per file: stream it to a TEMP file, convert to markdown, anonymize when
   the scope is marked for it, ingest the markdown into the scope's
   collection through the SAME internal path a wizard upload takes, and
   delete the temp file. The original bytes never persist on this host.

Hardening (all of it load-bearing on a ~1.6 TB / 100k-file estate — a run
outlives its access token, meets 429/503, and gets killed mid-pass):

* **Token refresh.** :class:`GraphAuth` re-acquires the app-only token a few
  minutes before expiry, through ``graph_client.get_app_token`` — this
  module never reimplements the certificate-credential flow.
* **410 Gone.** A dead ``deltaLink`` is dropped WITHOUT being persisted and
  that drive is re-enumerated from scratch, once. Persisting a dead link is
  how change detection silently stops forever.
* **Bounded 429.** ``Retry-After`` is honored but capped, per attempt AND in
  total per request; the budget being exhausted raises rather than sleeping
  forever, and every second waited lands in the run report.
* **5xx / transport retry.** 500/502/503/504 and connect/read timeouts retry
  with exponential backoff + jitter; 401 forces one token refresh.
* **Resume without skips or duplicates.** Crawl state (per-drive deltaLink,
  per-item cTag) is persisted at every delta-page boundary, and a drive's
  deltaLink is written only AFTER the rows that page produced were ingested
  — a crash costs re-work, never coverage. Re-ingesting an already-ingested
  item is a no-op: the ingest path matches on ``(collection, stable_id)``
  and an unchanged sha256 against an indexed row skips re-chunking.
* **Oversize accounting.** A file over the size cap is skipped, counted, and
  its bytes reported — "we indexed the corpus" and "we indexed the small
  half of it" must never look the same in the report. Every other refusal
  (a drive this app registration cannot read, a file that would not convert,
  a document that could not be anonymized) gets its own counter for the same
  reason: nothing goes un-indexed invisibly.

Security posture:

* Credentials come from :func:`connectors.sharepoint.settings.
  resolve_sharepoint_settings` (vault slot, else the deployment's env var)
  and the anonymization key from ``app.worker.kinds._resolve_anonymization_key``
  — the existing, allowlist-gated resolvers. Nothing here reads a secret
  from anywhere else, and no token, key, or certificate is ever logged.
* Every URL this module sends the bearer token to is checked against
  :data:`_GRAPH_HOST` first (:func:`_require_graph_url`). ``@odata.nextLink``
  / ``@odata.deltaLink`` are values taken from a response body and replayed
  on the NEXT request with the ``Authorization`` header attached, so they are
  credential-egress destinations in the sense of the security playbook §8 —
  gated, never trusted because of where they came from.
* Broken-inheritance subtrees found by ``sharepoint-subtree-sweep`` are
  HONORED here (the external producer only ever received the list): each
  excluded root is resolved once to its drive-relative path and every file
  under that prefix is skipped. Fail-closed — a scope whose exclusion roots
  cannot be resolved is not crawled at all.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import httpx

from connectors.sharepoint import graph_client
from connectors.sharepoint.graph_client import GRAPH_BASE, SharePointGraphError
from connectors.sharepoint.settings import SharePointSettingsError, resolve_sharepoint_settings

logger = logging.getLogger(__name__)

#: The ONLY host this module will attach an Authorization header to. Graph
#: hands back absolute ``@odata.nextLink``/``@odata.deltaLink`` URLs that we
#: replay verbatim; a redirect or a poisoned stored state file must not be
#: able to turn one into a credential-exfiltration destination.
_GRAPH_HOST = "graph.microsoft.com"

#: Names never worth crawling (OS/Office droppings), verbatim from the
#: reference crawler.
_SKIP_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
_SKIP_PREFIXES = ("~$",)  # Office lock files

#: A 403/404 on one drive or one item is a routine permissions fact, not a
#: broken connection — app-only access across a real tenant is never uniform.
#: Same set, and the same reasoning, as
#: ``graph_client._PERMISSION_SKIP_STATUS_CODES``.
_PERMISSION_SKIP_STATUS_CODES = frozenset({403, 404})

#: Transport policy. A 100k-file crawl meets every one of these.
_RETRY_STATUS = frozenset({500, 502, 503, 504})
_MAX_ATTEMPTS = 8  # per request, including the first try
_MAX_THROTTLE_WAIT_S = 900.0  # 429 sleep allowed per request, in total
_MAX_SINGLE_WAIT_S = 300.0  # cap on any ONE sleep, Retry-After included
_BACKOFF_BASE_S = 2.0  # doubled per attempt, plus jitter
_REQUEST_TIMEOUT_S = 120.0

#: Default per-file size cap (``extraction.crawler.max_file_mb``; 0 disables).
_DEFAULT_MAX_FILE_MB = 50
#: Largest skipped files kept in the report.
_OVERSIZE_SAMPLE = 20
#: Delta page size asked of Graph — also the state-checkpoint granularity.
_DELTA_PAGE_SIZE = 200
#: Streaming download chunk.
_DOWNLOAD_CHUNK = 1 << 20

#: ``parentReference.path`` prefix Graph puts in front of a drive-relative
#: path. Linear-time, anchored, bounded — no ReDoS surface (playbook §5).
_DRIVE_ROOT_PREFIX_RE = re.compile(r"^/drives/[^/]+/root:?")


class CrawlError(RuntimeError):
    """The crawl cannot run or cannot be trusted to be complete.

    Raised for configuration faults (no resolvable scope, an exclusion list
    that cannot be applied) and for a transport failure that exhausted its
    budget. Never carries a token, key, or certificate in its message.
    """


class GraphGone(Exception):
    """HTTP 410 — a ``deltaLink`` expired; that drive needs a full resync."""


class GraphThrottled(CrawlError):
    """429s exceeded this request's attempt / total-wait budget."""


# --------------------------------------------------------------------------
# Crawl state (per connection): the per-drive deltaLink + per-item cTag map.
#
# Persisted as a file under the state dir rather than on the connection row's
# `config` (where `acl_sync` keeps its own bookkeeping) for one reason: the
# cTag map is one entry per crawled document — six figures on a real estate —
# and a JSON column re-serialized on every checkpoint is the wrong home for
# that. The deltaLinks alone would fit; splitting the two halves across two
# stores would just create a way for them to disagree.
# --------------------------------------------------------------------------

_STATE_SUBDIR = "sharepoint_crawl"
#: Connection ids are repo-minted, but this value reaches a filesystem path,
#: so it is validated as a single safe segment before use (playbook §6).
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def state_path(connection_id: str) -> Path:
    """``<state dir>/sharepoint_crawl/<connection_id>.json``.

    Validates ``connection_id`` as a single safe path segment AND contains
    the resolved path inside the state directory — both layers, per the
    security playbook's filesystem rule.
    """
    if not _SAFE_SEGMENT_RE.match(connection_id or "") or connection_id in (".", ".."):
        raise CrawlError(f"unsafe connection id for a state file: {connection_id!r}")
    from src.db import _get_state_dir

    base = (_get_state_dir() / _STATE_SUBDIR).resolve()
    base.mkdir(parents=True, exist_ok=True)
    resolved = (base / f"{connection_id}.json").resolve()
    resolved.relative_to(base)  # containment assertion; raises ValueError if escaped
    return resolved


def load_state(connection_id: str) -> Dict[str, Any]:
    """Read this connection's crawl state, tolerating a torn/absent file.

    A state file that cannot be parsed must not wedge every future run: the
    worst case of starting over is re-work (already-ingested documents
    upsert to a no-op), while refusing to run is a permanent outage.
    """
    path = state_path(connection_id)
    state: Dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state = loaded
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "sharepoint crawl: state for connection %s unreadable (%s) — starting from a full crawl",
                connection_id,
                exc,
            )
    state.setdefault("delta_links", {})
    state.setdefault("ctags", {})
    return state


def save_state(connection_id: str, state: Dict[str, Any]) -> None:
    """Atomically replace this connection's state file (tmp + ``os.replace``)."""
    path = state_path(connection_id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Run report
# --------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def human_bytes(n: int) -> str:
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"  # pragma: no cover — the loop always returns


@dataclass
class CrawlStats:
    """What the run cost and what it refused to do. Every skip is counted:
    an unindexed document must never be invisible in the report."""

    started: float = field(default_factory=time.monotonic)
    started_at: str = field(default_factory=_now_iso)
    requests: int = 0
    retries: int = 0
    http_429: int = 0
    throttle_wait_s: float = 0.0
    retry_wait_s: float = 0.0
    token_refreshes: int = 0
    delta_resyncs: int = 0
    scopes: int = 0
    drives: int = 0
    downloads: int = 0
    bytes_downloaded: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    deleted: int = 0
    errors: int = 0
    convert_failed: int = 0
    anonymize_failed: int = 0
    excluded_subtree_skips: int = 0
    permission_skips: int = 0
    oversize_files: int = 0
    oversize_bytes: int = 0
    oversize_largest: List[Dict[str, Any]] = field(default_factory=list)

    def note_oversize(self, path: str, size: int) -> None:
        size = int(size or 0)
        self.oversize_files += 1
        self.oversize_bytes += size
        self.oversize_largest.append({"path": path, "size": size})
        self.oversize_largest.sort(key=lambda e: -int(e["size"]))
        del self.oversize_largest[_OVERSIZE_SAMPLE:]

    def report(self, *, max_file_mb: int, interrupted: bool = False) -> Dict[str, Any]:
        elapsed = max(time.monotonic() - self.started, 1e-6)
        processed = self.new + self.changed
        return {
            "mode": "builtin",
            "started_at": self.started_at,
            "finished_at": _now_iso(),
            "duration_s": round(elapsed, 1),
            "interrupted": interrupted,
            "scopes": self.scopes,
            "drives": self.drives,
            "new": self.new,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "deleted": self.deleted,
            "errors": self.errors,
            "convert_failed": self.convert_failed,
            "anonymize_failed": self.anonymize_failed,
            "excluded_subtree_skips": self.excluded_subtree_skips,
            "permission_skips": self.permission_skips,
            "files_per_s": round(processed / elapsed, 3),
            "requests": self.requests,
            "retries": self.retries,
            "http_429": self.http_429,
            "throttle_wait_s": round(self.throttle_wait_s, 1),
            "retry_wait_s": round(self.retry_wait_s, 1),
            "token_refreshes": self.token_refreshes,
            "delta_resyncs": self.delta_resyncs,
            "downloads": self.downloads,
            "bytes_downloaded": self.bytes_downloaded,
            "bytes_downloaded_human": human_bytes(self.bytes_downloaded),
            "max_file_mb": max_file_mb or "unlimited",
            "skipped_oversize": {
                "files": self.oversize_files,
                "bytes": self.oversize_bytes,
                "bytes_human": human_bytes(self.oversize_bytes),
                "largest": list(self.oversize_largest),
            },
        }


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------


async def _sleep(seconds: float) -> None:
    """The crawl's only wall-clock wait, behind one seam.

    Every backoff and every honored ``Retry-After`` goes through here, so a
    test can assert the BOUNDS of the 429/retry policy — the part that
    matters — without spending those seconds.
    """
    await asyncio.sleep(seconds)


def _require_graph_url(url: str) -> str:
    """Refuse to attach the bearer token to anything but Graph.

    ``@odata.nextLink``/``@odata.deltaLink`` are absolute URLs read out of a
    response body (and, after a resume, out of a file on disk) and replayed
    with the ``Authorization`` header — i.e. a credential-egress destination
    taken from data, exactly what the security playbook's host-allowlist rule
    covers.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != _GRAPH_HOST:
        raise CrawlError(f"refusing to send a Graph token to a non-Graph URL: {parts.scheme}://{parts.hostname}")
    return url


class GraphAuth:
    """App-only token with proactive mid-crawl refresh.

    A client-credentials token lives ~1h; a full crawl outlives it several
    times over. Re-acquired once the live one is inside :data:`REFRESH_MARGIN`
    of expiry — before requests start failing, not after. The exchange itself
    is ``graph_client.get_app_token``: this class holds a token's lifetime,
    it does not reimplement the certificate-credential flow.
    """

    #: Refresh 5 min early: clock skew plus requests already in flight.
    REFRESH_MARGIN = 300.0
    #: Graph's app-only tokens are ~1h; assume that when nothing says.
    _DEFAULT_TTL = 3600.0

    def __init__(
        self,
        *,
        acquire: Callable[[], Any],
        stats: CrawlStats,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._acquire = acquire
        self._stats = stats
        self._clock = clock
        self._token: Optional[str] = None
        self.expires_at = 0.0

    async def token(self) -> str:
        if self._token is None or self._clock() >= self.expires_at - self.REFRESH_MARGIN:
            await self.refresh()
        assert self._token is not None  # refresh() never leaves it None
        return self._token

    async def refresh(self) -> str:
        self._token = str(await self._acquire())
        self.expires_at = self._clock() + self._DEFAULT_TTL
        self._stats.token_refreshes += 1
        return self._token


class GraphTransport:
    """Every Graph call the crawl makes, under one retry policy.

    Keeping the policy here is what lets the crawl body stay a plain loop:
    the ``Authorization`` header is re-stamped per request from
    :class:`GraphAuth` (a token expiring mid-crawl is a non-event), 429 is
    honored but bounded, 5xx/transport faults retry with jittered backoff,
    401 forces exactly one refresh, and 410 raises :class:`GraphGone`
    because a dead deltaLink is a caller decision, never a retry.
    """

    def __init__(
        self,
        auth: GraphAuth,
        stats: CrawlStats,
        *,
        sleep: Optional[Callable[[float], Any]] = None,
        max_attempts: int = _MAX_ATTEMPTS,
        max_throttle_wait_s: float = _MAX_THROTTLE_WAIT_S,
        max_single_wait_s: float = _MAX_SINGLE_WAIT_S,
    ) -> None:
        self.auth = auth
        self.stats = stats
        self._sleep_fn = sleep
        self.max_attempts = max(1, int(max_attempts))
        self.max_throttle_wait_s = float(max_throttle_wait_s)
        self.max_single_wait_s = float(max_single_wait_s)

    def _backoff_seconds(self, attempt: int) -> float:
        return min(_BACKOFF_BASE_S * (2 ** (attempt - 1)), self.max_single_wait_s) + random.uniform(0, 1)

    def _retry_after(self, response: httpx.Response, attempt: int) -> float:
        raw = response.headers.get("Retry-After")
        try:
            wait = float(raw) if raw is not None else self._backoff_seconds(attempt)
        except (TypeError, ValueError):
            wait = self._backoff_seconds(attempt)  # an HTTP-date, or garbage
        return max(0.0, min(wait, self.max_single_wait_s))

    async def _authorized(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {await self.auth.token()}"}

    async def _wait(self, seconds: float) -> None:
        """The crawl's ONLY wall-clock wait, resolved at call time through
        the module-level :func:`_sleep` seam — so a test can make a bounded
        429/backoff policy assertion without actually sleeping through it."""
        await (self._sleep_fn or _sleep)(seconds)

    async def get_json(self, url: str) -> Dict[str, Any]:
        """One GET returning a JSON object, under the full retry policy."""
        _require_graph_url(url)
        attempt = 0
        throttle_wait = 0.0
        refreshed = False
        while True:
            attempt += 1
            self.stats.requests += 1
            try:
                async with graph_client._http_client() as client:
                    resp = await client.get(url, headers=await self._authorized(), timeout=_REQUEST_TIMEOUT_S)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt >= self.max_attempts:
                    raise CrawlError(f"graph GET failed after {attempt} attempts: {type(exc).__name__}") from exc
                await self._sleep_backoff(attempt, type(exc).__name__)
                continue

            action, wait = self._classify(resp, attempt, throttle_wait, refreshed)
            if action == "ok":
                body = resp.json()
                return body if isinstance(body, dict) else {"value": body}
            if action == "gone":
                raise GraphGone(url)
            if action == "throttled":
                raise GraphThrottled(f"429 budget exhausted after {attempt} attempts / {throttle_wait:.0f}s of waiting")
            if action == "refresh":
                refreshed = True
                self.stats.retries += 1
                logger.info("sharepoint crawl: 401 — forcing a token refresh")
                await self.auth.refresh()
                continue
            if action == "throttle":
                throttle_wait += wait
                self.stats.http_429 += 1
                self.stats.throttle_wait_s += wait
                self.stats.retries += 1
                logger.info("sharepoint crawl: 429 — pausing %.0fs (attempt %d)", wait, attempt)
                await self._wait(wait)
                continue
            if action == "retry":
                await self._sleep_backoff(attempt, f"HTTP {resp.status_code}")
                continue
            # The existing typed Graph error, not a bare CrawlError: it carries
            # the status, which is how the caller tells a routine permissions
            # fact (403/404 on one drive of a many-drive site) from a broken
            # connection — the same distinction `graph_client.search_folders`
            # already draws.
            raise SharePointGraphError(f"graph GET failed: HTTP {resp.status_code}", status_code=resp.status_code)

    def _classify(self, resp: httpx.Response, attempt: int, throttle_wait: float, refreshed: bool) -> Tuple[str, float]:
        """``(action, wait)`` for one response — the whole status policy in
        one place, so :meth:`get_json` and :meth:`download_to_temp` cannot
        drift on what a 429 or a 410 means."""
        status = resp.status_code
        if 200 <= status < 300:
            return "ok", 0.0
        if status == 429:
            wait = self._retry_after(resp, attempt)
            if attempt >= self.max_attempts or throttle_wait + wait > self.max_throttle_wait_s:
                self.stats.http_429 += 1
                return "throttled", 0.0
            return "throttle", wait
        if status == 410:
            return "gone", 0.0
        if status == 401 and not refreshed and attempt < self.max_attempts:
            return "refresh", 0.0
        if status in _RETRY_STATUS and attempt < self.max_attempts:
            return "retry", 0.0
        return "fail", 0.0

    async def _sleep_backoff(self, attempt: int, why: str) -> None:
        wait = self._backoff_seconds(attempt)
        self.stats.retries += 1
        self.stats.retry_wait_s += wait
        logger.info("sharepoint crawl: %s — retry %d/%d in %.1fs", why, attempt, self.max_attempts, wait)
        await self._wait(wait)

    async def download_to_temp(self, drive_id: str, item_id: str, name: str, *, max_bytes: int) -> Path:
        """Stream one item's bytes to a temp file and return its path.

        Streaming — never buffering the body — is what makes an unlimited
        size cap safe: a 4 GB file costs a temp file, not 4 GB of RSS. The
        caller ALWAYS deletes the returned path; on any failure in here the
        partial file is removed before the exception propagates, so a local
        copy never outlives the attempt that made it.

        ``max_bytes`` is a second, independent guard on top of the caller's
        ``size``-based skip: Graph's reported ``size`` is metadata, and a
        response that disagrees with it must not be able to fill the disk.
        """
        url = _require_graph_url(f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/content")
        fd, tmp_name = tempfile.mkstemp(suffix=Path(name or "").suffix)
        os.close(fd)
        tmp_path = Path(tmp_name)
        attempt = 0
        throttle_wait = 0.0
        refreshed = False
        try:
            while True:
                attempt += 1
                self.stats.requests += 1
                written = 0
                try:
                    async with graph_client._http_client() as client:
                        async with client.stream(
                            "GET", url, headers=await self._authorized(), timeout=_REQUEST_TIMEOUT_S
                        ) as resp:
                            action, wait = self._classify(resp, attempt, throttle_wait, refreshed)
                            if action == "ok":
                                with open(tmp_path, "wb") as fh:
                                    async for chunk in resp.aiter_bytes(_DOWNLOAD_CHUNK):
                                        written += len(chunk)
                                        if max_bytes and written > max_bytes:
                                            raise CrawlError(
                                                f"download exceeded the {max_bytes}-byte cap for item {item_id}"
                                            )
                                        fh.write(chunk)
                                self.stats.downloads += 1
                                self.stats.bytes_downloaded += written
                                return tmp_path
                            await resp.aread()  # drain before deciding, so the connection is reusable
                except (httpx.TransportError, httpx.TimeoutException) as exc:
                    if attempt >= self.max_attempts:
                        raise CrawlError(f"download failed after {attempt} attempts: {type(exc).__name__}") from exc
                    await self._sleep_backoff(attempt, type(exc).__name__)
                    continue

                if action == "gone":
                    raise GraphGone(url)
                if action == "throttled":
                    raise GraphThrottled(f"429 budget exhausted downloading item {item_id}")
                if action == "refresh":
                    refreshed = True
                    self.stats.retries += 1
                    await self.auth.refresh()
                    continue
                if action == "throttle":
                    throttle_wait += wait
                    self.stats.http_429 += 1
                    self.stats.throttle_wait_s += wait
                    self.stats.retries += 1
                    await self._wait(wait)
                    continue
                if action == "retry":
                    await self._sleep_backoff(attempt, "download HTTP error")
                    continue
                raise SharePointGraphError(
                    f"download of item {item_id} failed: HTTP {resp.status_code}", status_code=resp.status_code
                )
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise


# --------------------------------------------------------------------------
# Convert / anonymize seams
#
# Both modules are written independently of this one. They are imported
# LAZILY through these two wrappers so (a) this module imports cleanly on an
# instance where they are absent, and (b) a test has exactly one attribute to
# substitute instead of patching an import machinery.
# --------------------------------------------------------------------------


def convert_to_markdown(path: Path, mime: str) -> Any:
    """``connectors.sharepoint.convert.convert_to_markdown`` — the seam.

    Returns that module's ``ConvertResult`` (``.markdown``, ``.engine``).
    An ``ImportError`` propagates: with no converter there is nothing to
    ingest, so the run fails clean rather than silently indexing nothing.
    """
    from connectors.sharepoint.convert import convert_to_markdown as _convert

    return _convert(path, mime)


def anonymize_markdown(text: str, *, key: bytes) -> Any:
    """``src.anonymization.anonymize_markdown`` — the seam.

    Returns that module's ``AnonymizeResult`` (``.text``, ``.replaced``).
    Callers treat ANY failure here — ``ImportError`` included — as
    "this document cannot be anonymized", which for an anonymize-marked
    scope means it is skipped, never ingested raw.
    """
    from src.anonymization import anonymize_markdown as _anonymize

    return _anonymize(text, key=key)


# --------------------------------------------------------------------------
# Scope -> drives
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DriveTarget:
    """One delta-enumerable unit of work: a drive, optionally rooted at a
    folder inside it (a folder scope), plus the state key its deltaLink is
    stored under."""

    drive_id: str
    drive_name: Optional[str]
    #: ``None`` for a whole drive; a folder item id for a folder scope.
    root_item_id: Optional[str] = None

    @property
    def state_key(self) -> str:
        return f"{self.drive_id}:{self.root_item_id}" if self.root_item_id else self.drive_id

    @property
    def delta_url(self) -> str:
        if self.root_item_id:
            return f"{GRAPH_BASE}/drives/{self.drive_id}/items/{self.root_item_id}/delta"
        return f"{GRAPH_BASE}/drives/{self.drive_id}/root/delta"


def _scope_kind(source_scope_id: str) -> str:
    """Site / drive / folder, decided STRUCTURALLY from the Graph id shape —
    the same rule ``connectors/sharepoint/corpus_map.py`` uses, and for the
    same reason: it must work for scope rows confirmed before this module
    existed, with no Graph round trip."""
    if "," in source_scope_id:
        return "site"
    if source_scope_id.startswith("b!"):
        return "drive"
    return "folder"


async def _drive_targets(transport: GraphTransport, scope: Dict[str, Any]) -> List[DriveTarget]:
    """Resolve one confirmed scope row to the drives to delta-enumerate."""
    source_scope_id = str(scope.get("source_scope_id") or "")
    kind = _scope_kind(source_scope_id)
    if kind == "drive":
        return [DriveTarget(drive_id=source_scope_id, drive_name=scope.get("display_path"))]
    if kind == "folder":
        drive_id = scope.get("drive_id")
        if not drive_id:
            # Same posture as `acl_sync._sync_scope`: a folder scope without a
            # persisted drive_id is a wizard gap, reported per-scope rather
            # than crashing the connection's whole run.
            raise CrawlError(f"folder scope {source_scope_id!r} has no drive_id on its scope row")
        return [DriveTarget(drive_id=str(drive_id), drive_name=scope.get("display_path"), root_item_id=source_scope_id)]

    body = await transport.get_json(f"{GRAPH_BASE}/sites/{source_scope_id}/drives?$select=id,name")
    return [
        DriveTarget(drive_id=drive["id"], drive_name=drive.get("name"))
        for drive in body.get("value", [])
        if drive.get("id")
    ]


async def _excluded_path_prefixes(transport: GraphTransport, scope: Dict[str, Any]) -> List[str]:
    """Drive-relative path prefixes this scope must NOT crawl.

    ``sharepoint-subtree-sweep`` records broken-inheritance roots as
    ``{item_id, path}`` where ``path`` is rooted at the scope's wizard
    breadcrumb — not comparable with a crawler row's drive-relative path. So
    each root is resolved ONCE against Graph to its own drive-relative path,
    and containment is then an exact path-prefix test rather than a guess
    about delta ordering.

    Fail-closed: a root that cannot be resolved raises, and the caller skips
    the whole scope. Crawling a scope whose exclusions could not be applied
    would ingest exactly the content an admin excluded.

    A scope carrying ``include_excluded_subtrees`` is exempt — the admin
    decided that audience may see the content — matching
    ``app/worker/kinds.py::_excluded_subtree_scope_map``'s own omission.
    """
    if scope.get("include_excluded_subtrees"):
        return []
    excluded = scope.get("excluded_subtrees")
    if not isinstance(excluded, list) or not excluded:
        return []
    drive_id = scope.get("drive_id")
    prefixes: List[str] = []
    for entry in excluded:
        if not isinstance(entry, dict) or not entry.get("item_id"):
            continue
        if not drive_id:
            raise CrawlError(
                f"scope {scope.get('source_scope_id')!r} has excluded subtrees but no drive_id — "
                "cannot resolve them to paths, refusing to crawl it"
            )
        item_id = str(entry["item_id"])
        body = await transport.get_json(
            f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}?$select=id,name,parentReference"
        )
        prefixes.append(_drive_relative_path(body.get("parentReference") or {}, str(body.get("name") or "")))
    return [p for p in prefixes if p]


# --------------------------------------------------------------------------
# Item handling
# --------------------------------------------------------------------------


def _should_skip_name(name: str) -> bool:
    return name in _SKIP_NAMES or name.startswith(_SKIP_PREFIXES)


def _drive_relative_path(parent_reference: Dict[str, Any], name: str) -> str:
    """``parentReference.path`` + item name, with Graph's
    ``/drives/<id>/root:`` prefix stripped — the DRIVE-relative path the
    corpus-map resolver and the collections ``path`` key both speak."""
    parent = str(parent_reference.get("path") or "")
    rel = _DRIVE_ROOT_PREFIX_RE.sub("", parent).strip("/")
    return f"{rel}/{name}".strip("/") if rel else name.strip("/")


def _under_prefix(path: str, prefixes: Sequence[str]) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


@dataclass
class _ScopeContext:
    """Everything the per-item pipeline needs about the scope it is in."""

    source_scope_id: str
    collection_id: str
    anonymize: bool
    excluded_prefixes: List[str]


class _Ingestor:
    """Writes a converted document into a collection through the SAME
    internal path a wizard upload takes.

    ``app.api.collections._upsert_corpus_file`` is the whole point: it
    matches on ``(collection, source_stable_id)`` FIRST and ``(collection,
    path)`` second, preserves the row id on any match, purges derived data
    only when content actually changed, and leaves an unchanged, already
    indexed row alone. That is what makes a re-crawl (and a resume) idempotent
    without this module knowing anything about chunks, claims, or blobs.
    """

    def __init__(self) -> None:
        # Resolved ONCE, up front: the mapping table is Postgres-only, so a
        # DuckDB-backed instance fails before a single file is downloaded
        # rather than partway through a crawl — the same posture
        # `upload_files` takes for a source-anchored batch.
        from src.repositories import corpus_file_sources_repo

        self._sources_repo = corpus_file_sources_repo()

    def ingest(
        self,
        *,
        collection_id: str,
        stable_id: str,
        path: str,
        filename: str,
        markdown: str,
        source_sha256: str,
    ) -> Tuple[str, bool]:
        """Store + upsert + (re)ingest one converted document.

        Returns ``(file_id, was_new)``.
        """
        from app.api.collections import _upsert_corpus_file
        from src.file_storage import store_corpus_bytes

        data = markdown.encode("utf-8")
        stored = store_corpus_bytes(collection_id, filename, data)
        existed = self._sources_repo.resolve(collection_id, stable_id) is not None
        file_id, needs_processing, _claims_purged = _upsert_corpus_file(
            collection_id,
            path=path,
            stable_id=stable_id,
            # The content hash of the ORIGINAL source bytes, not of the
            # markdown — `corpus_files.sha256` already holds the latter.
            source_doc_id=source_sha256[:16],
            source_sha256_meta=source_sha256,
            filename=filename,
            sha256=stored.sha256,
            file_type=stored.ext.lstrip(".") or None,
            size_bytes=stored.size_bytes,
            storage_path=stored.storage_path,
            sources_repo=self._sources_repo,
        )
        if needs_processing:
            from src.ingest.runner import ingest_file

            # Inline, not a background task: this runs inside the worker's
            # own EXTRACTION lane slot, which is exactly where chunking is
            # supposed to happen, and finishing each document before fetching
            # the next keeps the crawl's memory flat.
            ingest_file(file_id)
        return file_id, not existed

    def delete(self, collection_id: str, stable_id: str) -> bool:
        """Remove the file a deleted source item anchors, if any.

        Same two steps ``DELETE /api/collections/{id}/files/{file_id}`` takes
        — purge the row (blobs, chunks, derived tables, bundle children) and
        then sweep fact-graph subjects the cascade left with zero claims —
        so a document deleted at the source leaves nothing behind that a
        hand delete would have cleaned up.
        """
        from app.api.collections import _purge_file_row, _sweep_facts_orphans_after_delete
        from src.repositories import corpus_files_repo

        file_id = self._sources_repo.resolve(collection_id, stable_id)
        if not file_id:
            return False
        row = corpus_files_repo().get(file_id)
        if row is None:
            return False
        _purge_file_row(collection_id, row)
        _sweep_facts_orphans_after_delete(trigger="sharepoint_crawl_delete")
        return True


# --------------------------------------------------------------------------
# The crawl
# --------------------------------------------------------------------------


def _max_file_bytes(max_file_mb: int) -> int:
    """0 (or negative) means unlimited — downloads stream to a temp file."""
    return max(0, int(max_file_mb)) * 1024 * 1024


async def _process_item(
    item: Dict[str, Any],
    *,
    target: DriveTarget,
    ctx: _ScopeContext,
    transport: GraphTransport,
    ingestor: _Ingestor,
    state: Dict[str, Any],
    stats: CrawlStats,
    max_file_mb: int,
    anonymization_key: Optional[bytes],
) -> None:
    """One delta row -> at most one ingested document. Never raises for a
    per-file fault: a locked, vanished, unconvertible, or un-anonymizable
    document is COUNTED and skipped, because one bad file must not cost a
    100k-file pass."""
    name = str(item.get("name") or "")
    stable_id = f"graph:{item['id']}"
    ctags: Dict[str, Any] = state["ctags"]

    if item.get("deleted"):
        if ingestor.delete(ctx.collection_id, stable_id):
            stats.deleted += 1
        ctags.pop(stable_id, None)
        return
    if "file" not in item or _should_skip_name(name):
        return

    path = _drive_relative_path(item.get("parentReference") or {}, name)
    if ctx.excluded_prefixes and _under_prefix(path, ctx.excluded_prefixes):
        stats.excluded_subtree_skips += 1
        return

    ctag = item.get("cTag") or item.get("eTag")
    if ctag and ctags.get(stable_id) == ctag:
        stats.unchanged += 1
        return

    size = int(item.get("size") or 0)
    if max_file_mb and size > _max_file_bytes(max_file_mb):
        stats.note_oversize(path, size)
        logger.info("sharepoint crawl: skipping %s — %s over the %dMB cap", path, human_bytes(size), max_file_mb)
        return

    mime = str((item.get("file") or {}).get("mimeType") or "")
    try:
        tmp_path = await transport.download_to_temp(
            target.drive_id, str(item["id"]), name, max_bytes=_max_file_bytes(max_file_mb)
        )
    except GraphThrottled:
        # NOT a per-file fault, despite being a CrawlError: the tenant is
        # throttling this app registration as a whole, so absorbing it here
        # would turn "back off" into "keep hammering, one 429 budget per
        # file". Aborts the run; the next one resumes from the persisted
        # deltaLink + cTags.
        raise
    except (CrawlError, SharePointGraphError, httpx.HTTPError) as exc:
        stats.errors += 1
        logger.warning("sharepoint crawl: download failed for %s: %s", path, type(exc).__name__)
        return

    try:
        source_sha256 = _sha256_file(tmp_path)
        try:
            converted = convert_to_markdown(tmp_path, mime)
            markdown = str(getattr(converted, "markdown", "") or "")
        except Exception as exc:  # noqa: BLE001 — one unconvertible file, not a broken run
            stats.convert_failed += 1
            stats.errors += 1
            logger.warning("sharepoint crawl: conversion failed for %s: %s", path, type(exc).__name__)
            return
        if not markdown.strip():
            stats.convert_failed += 1
            logger.info("sharepoint crawl: conversion produced no text for %s", path)
            return

        if ctx.anonymize:
            # FAIL CLOSED. An anonymize-marked scope promised its audience
            # that no raw identifier reaches the collection; a document that
            # cannot be anonymized is therefore counted and dropped, never
            # ingested in its original form.
            if anonymization_key is None:
                stats.anonymize_failed += 1
                return
            try:
                markdown = str(anonymize_markdown(markdown, key=anonymization_key).text)
            except Exception as exc:  # noqa: BLE001 — incl. ImportError: module not present
                stats.anonymize_failed += 1
                logger.warning("sharepoint crawl: anonymization failed for %s: %s", path, type(exc).__name__)
                return
    finally:
        # The local copy never persists — success, skip, or failure.
        tmp_path.unlink(missing_ok=True)

    try:
        _file_id, was_new = ingestor.ingest(
            collection_id=ctx.collection_id,
            stable_id=stable_id,
            path=path,
            filename=f"{Path(name).stem or name}.md",
            markdown=markdown,
            source_sha256=source_sha256,
        )
    except Exception as exc:  # noqa: BLE001 — one file's ingest, not the run
        stats.errors += 1
        logger.warning("sharepoint crawl: ingest failed for %s: %s", path, type(exc).__name__)
        return

    if was_new:
        stats.new += 1
    else:
        stats.changed += 1
    # Written only AFTER the document is durably ingested: a cTag recorded
    # before the ingest would make a resumed run skip a file it never landed.
    if ctag:
        ctags[stable_id] = ctag


def _sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


async def _crawl_drive(
    target: DriveTarget,
    *,
    ctx: _ScopeContext,
    transport: GraphTransport,
    ingestor: _Ingestor,
    connection_id: str,
    state: Dict[str, Any],
    stats: CrawlStats,
    max_file_mb: int,
    anonymization_key: Optional[bytes],
) -> None:
    """Delta-enumerate one drive (or one folder subtree), resuming from its
    persisted ``deltaLink``."""
    delta_links: Dict[str, Any] = state["delta_links"]
    base = f"{target.delta_url}?$top={_DELTA_PAGE_SIZE}"
    url: Optional[str] = delta_links.get(target.state_key) or base
    resynced = False
    stats.drives += 1

    while url:
        try:
            page = await transport.get_json(url)
        except GraphGone:
            # The deltaLink expired or was invalidated. Drop it WITHOUT
            # persisting it and restart this drive from a full enumeration.
            # Persisting a dead link is how change detection silently stops
            # forever; resyncing twice in one pass means something else is
            # wrong, so the second 410 propagates.
            if resynced:
                raise
            delta_links.pop(target.state_key, None)
            save_state(connection_id, state)
            stats.delta_resyncs += 1
            resynced = True
            logger.info(
                "sharepoint crawl: 410 Gone — dropped dead deltaLink, full resync of %s",
                target.drive_name or target.drive_id,
            )
            url = base
            continue
        except SharePointGraphError as exc:
            if exc.status_code not in _PERMISSION_SKIP_STATUS_CODES:
                raise
            # App-only access across a real tenant is never uniform: a site
            # scope fans out to every library, and some of them legitimately
            # refuse this app registration. That is a fact to record and walk
            # past, not a reason to fail the whole connection's crawl — the
            # same posture `graph_client.search_folders` takes. Counted, never
            # silent: a drive nobody could read must be visible in the report.
            stats.permission_skips += 1
            logger.warning(
                "sharepoint crawl: HTTP %s on drive %s — skipping it (no access for this app registration)",
                exc.status_code,
                target.drive_name or target.drive_id,
            )
            return

        for item in page.get("value", []):
            if not isinstance(item, dict) or not item.get("id"):
                continue
            await _process_item(
                item,
                target=target,
                ctx=ctx,
                transport=transport,
                ingestor=ingestor,
                state=state,
                stats=stats,
                max_file_mb=max_file_mb,
                anonymization_key=anonymization_key,
            )

        # Rows first, then the link: the deltaLink is persisted only after
        # everything the page produced is ingested and its cTags are on disk,
        # so a crash between the two costs re-work, never coverage.
        delta_link = page.get("@odata.deltaLink")
        if delta_link:
            delta_links[target.state_key] = _require_graph_url(str(delta_link))
            save_state(connection_id, state)
            url = None
        else:
            next_link = page.get("@odata.nextLink")
            save_state(connection_id, state)
            url = _require_graph_url(str(next_link)) if next_link else None


def _confirmed_scopes(connection: Dict[str, Any]) -> List[Dict[str, Any]]:
    """This connection's confirmed scope rows — the ones the wizard wrote a
    ``collection_id`` for. A scope with no collection has nowhere to route
    documents, so it is not crawlable."""
    scopes = (connection.get("config") or {}).get("scopes")
    if not isinstance(scopes, list):
        return []
    return [s for s in scopes if isinstance(s, dict) and s.get("source_scope_id") and s.get("collection_id")]


def _max_file_mb() -> int:
    from app.instance_config import get_value

    raw = get_value("extraction", "crawler", "max_file_mb", default=_DEFAULT_MAX_FILE_MB)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_FILE_MB


def _resolve_anonymization_key(scopes: Sequence[Dict[str, Any]]) -> Optional[bytes]:
    """The per-instance anonymization HMAC key, or ``None`` when no scope in
    this run needs one.

    Delegates to ``app.worker.kinds._resolve_anonymization_key`` — the single
    existing owner of that resolution, including the allowlist check on the
    admin-writable env var NAME. Duplicating it here would duplicate that
    gate, which is exactly the kind of second copy a security control must
    not have. Imported lazily so this connector carries no import-time
    dependency on the worker module (which imports this one).
    """
    if not any(s.get("anonymize") for s in scopes):
        return None
    from app.worker.kinds import _resolve_anonymization_key as resolve

    return str(resolve()).encode("utf-8")


async def _run_crawl_async(
    connection: Dict[str, Any],
    *,
    only_scope_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    connection_id = str(connection["id"])
    scopes = _confirmed_scopes(connection)
    if only_scope_ids:
        wanted = set(only_scope_ids)
        scopes = [s for s in scopes if str(s.get("source_scope_id")) in wanted]
    if not scopes:
        raise CrawlError(
            f"connection {connection_id!r} has no confirmed scope to crawl — "
            "confirm at least one scope in the SharePoint connect wizard first"
        )

    settings = resolve_sharepoint_settings(connection)
    anonymization_key = _resolve_anonymization_key(scopes)
    max_file_mb = _max_file_mb()

    stats = CrawlStats()
    auth = GraphAuth(
        acquire=lambda: graph_client.get_app_token(settings.tenant_id, settings.client_id, settings.private_key),
        stats=stats,
    )
    transport = GraphTransport(auth, stats)
    ingestor = _Ingestor()
    state = load_state(connection_id)
    scope_errors: List[Dict[str, Any]] = []

    try:
        for scope in scopes:
            source_scope_id = str(scope.get("source_scope_id"))
            try:
                targets = await _drive_targets(transport, scope)
                excluded_prefixes = await _excluded_path_prefixes(transport, scope)
            except (CrawlError, SharePointGraphError) as exc:
                # One scope's misconfiguration (or one site's outage) must not
                # cost the connection's other scopes their pass — the same
                # per-unit failure isolation `acl_sync` applies per connection.
                scope_errors.append({"scope": source_scope_id, "error": str(exc)})
                stats.errors += 1
                continue

            ctx = _ScopeContext(
                source_scope_id=source_scope_id,
                collection_id=str(scope["collection_id"]),
                anonymize=bool(scope.get("anonymize")),
                excluded_prefixes=excluded_prefixes,
            )
            stats.scopes += 1
            for target in targets:
                await _crawl_drive(
                    target,
                    ctx=ctx,
                    transport=transport,
                    ingestor=ingestor,
                    connection_id=connection_id,
                    state=state,
                    stats=stats,
                    max_file_mb=max_file_mb,
                    anonymization_key=anonymization_key,
                )
    except BaseException:
        # A crashed run still owes the operator its numbers and its state —
        # the rows are already ingested, so record what got done instead of
        # losing the pass.
        state["last_run"] = stats.report(max_file_mb=max_file_mb, interrupted=True)
        save_state(connection_id, state)
        raise

    report = stats.report(max_file_mb=max_file_mb)
    report["connection_id"] = connection_id
    report["scope_errors"] = scope_errors
    state["last_run"] = report
    save_state(connection_id, state)
    logger.info(
        "sharepoint crawl: connection %s — %d new, %d changed, %d unchanged, %d deleted, "
        "%d errors, %d oversize skipped",
        connection_id,
        stats.new,
        stats.changed,
        stats.unchanged,
        stats.deleted,
        stats.errors,
        stats.oversize_files,
    )
    return report


def run_builtin_crawl(payload: dict) -> dict:
    """Entry point for the ``corpus-extraction`` job kind's ``builtin``
    producer mode (``extraction.producer.mode: builtin``).

    ``payload``: ``connection_id`` (required — a ``source_connections`` row
    with ``source_type='sharepoint'``) and optionally ``scopes`` (a list of
    ``source_scope_id``s to narrow the run to; every confirmed scope
    otherwise). Credentials are resolved from the row, never from the
    payload.

    Returns the crawl report — the same dict persisted as ``last_run`` in
    this connection's crawl state, so the job result and the state file can
    never disagree about what a run did.
    """
    connection_id = payload.get("connection_id")
    if not connection_id:
        raise CrawlError("sharepoint crawl: payload missing connection_id")

    from src.repositories import source_connections_repo

    connection = source_connections_repo().get(connection_id)
    if connection is None or connection.get("source_type") != "sharepoint":
        raise CrawlError(f"sharepoint crawl: connection {connection_id!r} not found or not a sharepoint connection")

    try:
        return asyncio.run(_run_crawl_async(connection, only_scope_ids=payload.get("scopes")))
    except SharePointSettingsError as exc:
        # Named cause, not a bare traceback — the same typed handling
        # `app/api/admin_sharepoint.py::_resolved_token` gives this error.
        raise CrawlError(f"sharepoint crawl: {exc}") from exc
