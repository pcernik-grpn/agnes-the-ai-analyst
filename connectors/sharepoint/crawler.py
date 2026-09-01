"""Built-in SharePoint document crawler — THE extraction pipeline.

Owner decision 2026-08-31 overrides the "the producer is never vendored"
rule this connector was originally written under: the crawl -> convert ->
(anonymize) -> ingest pipeline is a FIRST-CLASS Agnes pipeline, and it is
the ONLY one. The external-producer subprocess seam that used to sit in
``app/worker/kinds.py::_run_corpus_extraction`` — with its
``extraction.producer.*`` config, its curated child env and its Admin-grade
callback credential — was removed with that decision; that handler is now a
thin delegate to :func:`run_builtin_crawl` below.

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
  HONORED here (the external producer only ever received the list and was
  trusted to obey it): each excluded root is resolved once to its
  drive-relative path and every file under that prefix is skipped.
  Fail-closed — a scope whose exclusion roots cannot be resolved is not
  crawled at all.
* ``GraphTransport.download_to_temp`` follows a file download's 302 to its
  pre-authenticated URL BY HAND, on a separate request that carries no
  ``Authorization`` header — the bearer token must never reach the
  non-Graph host that redirect points at. See its own docstring.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import os
import random
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import httpx

from connectors.sharepoint import graph_client
from connectors.sharepoint.acl_sync import active_zone_rows
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
#: Default in-page item concurrency (``extraction.crawler.concurrency``).
#: Six is deliberately modest: the bound that matters on a real tenant is
#: Graph's own throttling, and the point of this knob is to stop OUR loop
#: from being the narrower one — not to race the tenant into a 429 storm.
#: ``1`` is the pre-parallel behaviour, exactly (see :func:`_process_page`).
_DEFAULT_CONCURRENCY = 6
#: Hard ceiling on the configured value. Past this the extra parallelism buys
#: 429s, temp-file pressure and RAM, never throughput.
_MAX_CONCURRENCY = 32
#: Ceiling on a PER-RUN ``payload["concurrency"]`` override. Lower than the
#: configured ceiling on purpose: an ad-hoc run (an admin pressing "run now")
#: is the wrong place to go looking for a tenant's throttling limit.
_MAX_PAYLOAD_CONCURRENCY = 16
#: Adaptive downshift trigger. A delta page that met MORE than this many
#: throttled responses — or spent more than :data:`_THROTTLE_BURST_WAIT_S`
#: waiting on them — is a tenant pushing back, not one stray 429.
_THROTTLE_BURST_429S = 2
_THROTTLE_BURST_WAIT_S = 30.0
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


class CrawlTimeout(CrawlError):
    """The run exceeded ``extraction.timeout_s``.

    v1 has no other stop mechanism — no UI cancel, and (since the external
    producer was removed) no subprocess to kill — so this bound is the only
    thing standing between a pathological estate and a worker slot occupied
    forever. Raised between files and between delta pages, i.e. always at a
    point where the crawl state on disk is consistent: the run aborts through
    the same path a :class:`GraphThrottled` abort takes, which persists the
    state and the (interrupted) report, and the next run resumes from the
    deltaLink + cTags already written.
    """


class GraphThrottled(CrawlError):
    """429s exceeded this request's attempt / total-wait budget."""


#: ``interrupted_reason`` for a run that stopped early at a KNOWN-CONSISTENT
#: point — the deltaLink/cTag state on disk describes exactly what was
#: ingested, so a caller may tell the operator the next run resumes from
#: there. Anything else records ``"error"``: after an unexpected exception
#: nothing is known about how far the state file got, and "your work is safe"
#: must never be guessed.
#:
#: Order is irrelevant (the two classes are disjoint siblings), but the
#: mapping is a tuple rather than a dict because ``isinstance`` — not an
#: exact type lookup — is what has to decide, so a future subclass of either
#: inherits the right reason instead of silently falling through to "error".
_STOP_REASONS: tuple[tuple[type[BaseException], str], ...] = (
    (CrawlTimeout, "timeout"),
    (GraphThrottled, "throttled"),
)


def _stop_reason(exc: BaseException) -> str:
    """``"timeout"`` / ``"throttled"`` / ``"error"`` for an aborted run."""
    return next((reason for cls, reason in _STOP_REASONS if isinstance(exc, cls)), "error")


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
#: ONE writer for the state file, and one mutator for the in-memory state it
#: is serialized from. Under in-page concurrency several items finish inside
#: one page and each writes its own cTag; the page boundary then serializes
#: the whole dict. A `json.dumps` racing a `dict.__setitem__` is a
#: "dictionary changed size during iteration" crash on the exact write the
#: resume guarantee depends on, so both sides take this lock. Re-entrant
#: because the 410-resync path mutates `delta_links` and then calls
#: :func:`save_state` while still holding it.
_state_lock = threading.RLock()
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
    """Atomically replace this connection's state file (tmp + ``os.replace``).

    Serialized on :data:`_state_lock`: one writer at a time, and never
    concurrent with an in-page cTag write (which takes the same lock), so the
    bytes on disk are always a whole, self-consistent snapshot.
    """
    with _state_lock:
        path = state_path(connection_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)


# --------------------------------------------------------------------------
# Run recording — the second destination for the checkpoint this crawl
# already writes (2026-08-31 extraction-observability-ui design §7.1).
#
# Everything in this section is OBSERVABILITY, never load-bearing: the
# recorder swallows every failure of its own (including the typed
# `RequiresPostgresBackend` a DuckDB-backed instance raises the moment the
# PG-only repo is resolved) and the crawl runs on unchanged. A crawl that
# cannot be watched is worse than one that is; a crawl that FAILS because
# nobody could watch it is worse still.
# --------------------------------------------------------------------------


class _RunRecorder:
    """Writes this run's row: open at start, update at every existing
    checkpoint, finalize on done / interrupt / crash.

    Outcome precedence is severity-first — ``failed`` beats ``interrupted``.
    A crashed crawl is BOTH "did not finish" and "broke"; recording it as
    the benign outcome (and inviting the operator to trust the resume copy
    that attaches to it) is exactly the unverified-renders-healthy failure
    the design forbids. Only a cancellation/shutdown signal
    (``CancelledError``, ``KeyboardInterrupt``, ``SystemExit``) records as
    ``interrupted``; every other exception records as ``failed`` with its
    message.
    """

    def __init__(self, connection_id: str, *, job_id: Optional[str] = None) -> None:
        self.connection_id = connection_id
        self.job_id = job_id
        self.run_id: Optional[str] = None
        self._repo: Any = None

    def _resolve(self) -> Any:
        if self._repo is None:
            from src.repositories import extraction_runs_repo

            self._repo = extraction_runs_repo()
        return self._repo

    def start(self) -> None:
        try:
            self.run_id = self._resolve().start(
                connection_id=self.connection_id,
                job_id=self.job_id,
                phase="crawl",
            )
        except Exception as exc:  # noqa: BLE001 — recording is never load-bearing
            self.run_id = None
            logger.info(
                "sharepoint crawl: run recording unavailable for connection %s (%s) — crawling anyway",
                self.connection_id,
                type(exc).__name__,
            )

    def checkpoint(self, stats: "CrawlStats") -> None:
        if not self.run_id:
            return
        try:
            self._resolve().checkpoint(
                self.run_id,
                phase="crawl",
                files_seen=stats.items_seen,
                files_done=stats.items_done,
                # The delta feed can always hand back another page, so
                # enumeration is never "done" until the run itself is.
                enumeration_done=False,
                progress=_progress_snapshot(stats),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("sharepoint crawl: run checkpoint failed (%s) — continuing", type(exc).__name__)

    def finish(
        self,
        stats: "CrawlStats",
        *,
        status: str,
        report: Optional[Dict[str, Any]] = None,
        usage: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        if not self.run_id:
            return
        try:
            from src.repositories.extraction_runs_pg import cap_skips

            self._resolve().finish(
                self.run_id,
                status=status,
                report=report or {},
                skips=cap_skips(_skip_rows(stats), total=_skip_total(stats)),
                # Per-stage token accounting, keyed by stage: `ner` (the
                # anonymize seam's LLM detector, read via `_detector_usage`)
                # and `facts` (the LLM extraction stage, when switched on).
                # `{}` means "no tokens spent" — a different claim from
                # "$0.00", and the UI must keep saying so. An absent stage
                # spent nothing (regex tier, stage off), never $0.
                usage=usage or {},
                files_seen=stats.items_seen,
                files_done=stats.items_done,
                error=error,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("sharepoint crawl: run finalize failed (%s) — continuing", type(exc).__name__)


def _progress_snapshot(stats: "CrawlStats") -> Dict[str, Any]:
    """The ABSOLUTE counters a live run may honestly show. No fraction, no
    percentage, no ETA: ``files_per_s`` counts only new+changed documents,
    so a remaining-time figure derived from it is wrong by construction on
    any run with a meaningful `unchanged` share."""
    return {
        "files_done": stats.items_done,
        "new": stats.new,
        "changed": stats.changed,
        "unchanged": stats.unchanged,
        "deleted": stats.deleted,
        "downloads": stats.downloads,
        "bytes_downloaded": stats.bytes_downloaded,
        "bytes_downloaded_human": human_bytes(stats.bytes_downloaded),
        "errors": stats.errors,
        "http_429": stats.http_429,
        "throttle_wait_s": round(stats.throttle_wait_s, 1),
        "oversize_files": stats.oversize_files,
        "elapsed_s": round(max(time.monotonic() - stats.started, 0.0), 1),
    }


def _skip_rows(stats: "CrawlStats") -> List[Dict[str, Any]]:
    """The skips this run can name a PATH for. Only oversize skips keep
    paths (`note_oversize`); convert/anonymize/permission refusals keep
    counts alone, and are reported as counts by :func:`_skip_total` rather
    than invented as rows."""
    return [
        {
            "path": entry.get("path"),
            "reason": "oversize",
            "detail": f"{human_bytes(int(entry.get('size') or 0))} — over the size cap, never downloaded",
        }
        for entry in stats.oversize_largest
    ]


def _skip_total(stats: "CrawlStats") -> int:
    """Every document this run did NOT index, path or no path — so "20
    listed" can never be mistaken for "20 skipped"."""
    return (
        stats.oversize_files
        + stats.convert_failed
        + stats.anonymize_failed
        + stats.excluded_subtree_skips
        + stats.permission_skips
    )


def _record_status_for(exc: BaseException) -> str:
    """Severity-first outcome for a crawl that raised.

    A cancellation or a shutdown signal is an ``interrupted`` run — it did
    what it did and the next run resumes from the persisted cTags. Anything
    else is a ``failed`` run, and must never be softened into the benign
    outcome.
    """
    if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
        return "interrupted"
    return "failed"


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
    an unindexed document must never be invisible in the report.

    **Every counter here is mutated concurrently.** In-page item concurrency
    means several pipelines increment ``new`` / ``errors`` / ``bytes_
    downloaded`` at once, and the blocking convert/anonymize/ingest section of
    each runs on a worker THREAD (a bounded pool, see
    :func:`_run_blocking`), so "the event loop
    only switches at await points" is not the guarantee it would be for a pure
    coroutine. ``x += 1`` is a read-modify-write and loses increments under
    that; an undercounted skip is a document that silently did not get indexed,
    which is the one thing this report exists to make impossible. So all
    accumulation goes through :meth:`add` (and :meth:`note_oversize`) under
    :attr:`_lock` — never a bare ``+=`` from crawl code.
    """

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
    #: Delta rows this run has ENUMERATED and rows it has FINISHED handling.
    #: Not in :meth:`report` — the report's contract is unchanged — but read
    #: by the run recorder so a live run has honest ABSOLUTE counters (a
    #: fraction would be a lie here: this crawl enumerates and processes in
    #: lockstep per 200-row delta page, so the two are equal at every
    #: checkpoint and there is no meaningful denominator to divide by).
    items_seen: int = 0
    items_done: int = 0
    #: In-page item concurrency CEILING for this run (1 = sequential): the
    #: configured value, or the per-run payload override that replaced it.
    concurrency: int = 1
    #: What ``extraction.crawler.concurrency`` alone said — kept next to the
    #: effective ceiling so a payload override is visible as an override.
    concurrency_configured: int = 1
    #: "config" | "payload" | "adaptive" — where the EFFECTIVE in-flight
    #: target came from. "adaptive" wins when throttling pulled it below the
    #: ceiling, because that is the number that actually governed the run.
    concurrency_source: str = "config"
    #: The adaptive target the run ENDED on, the lowest it ever reached, how
    #: many times it was halved, and whether it bottomed out at 1. Together
    #: these are the operator's view of the tenant pushing back — the run is
    #: never aborted by a downshift, so without them it would be invisible.
    concurrency_effective_max: int = 1
    concurrency_min_target: int = 1
    concurrency_downshifts: int = 0
    concurrency_floor_hit: bool = False
    #: Peak number of item pipelines in flight at once, observed. Read next to
    #: `concurrency` it answers the only question the knob raises: did the pool
    #: actually fill, or is something else (page size, tenant throttling) the
    #: real bound?
    max_in_flight: int = 0
    #: Live in-flight count — bookkeeping for `max_in_flight`, not reported.
    in_flight: int = 0
    #: Summed wall time spent inside the per-item pipeline across all workers.
    #: Against the run's own `duration_s` this is the honest overlap figure: a
    #: perfectly serial run has item_seconds ≈ duration_s, a run at N-way
    #: overlap approaches N × duration_s.
    item_seconds: float = 0.0
    #: Guards every counter above. Not compared, not printed — it is machinery.
    _lock: Any = field(default_factory=threading.RLock, repr=False, compare=False)

    def add(self, **deltas: float) -> None:
        """Atomically accumulate one or more counters: ``stats.add(new=1)``.

        Unknown names raise rather than quietly minting an attribute — a typo
        here would be an invisible, permanently-zero counter in the report.
        """
        with self._lock:
            for name, delta in deltas.items():
                current = getattr(self, name)  # AttributeError on a typo, deliberately
                setattr(self, name, current + delta)

    def enter_item(self) -> None:
        """One item pipeline started — track the pool's observed peak."""
        with self._lock:
            self.in_flight += 1
            if self.in_flight > self.max_in_flight:
                self.max_in_flight = self.in_flight

    def exit_item(self, seconds: float) -> None:
        """One item pipeline finished (successfully or not)."""
        with self._lock:
            self.in_flight -= 1
            self.item_seconds += max(0.0, seconds)

    def throttle_snapshot(self) -> Tuple[int, float]:
        """``(http_429, throttle_wait_s)`` read atomically together.

        The adaptive governor diffs two of these across a page; reading the
        two counters separately could straddle a concurrent update and
        manufacture a burst that never happened.
        """
        with self._lock:
            return self.http_429, self.throttle_wait_s

    def note_concurrency(self, *, target: int, downshift: bool = False) -> None:
        """Record where the adaptive in-flight target has moved to."""
        with self._lock:
            self.concurrency_effective_max = target
            self.concurrency_min_target = min(self.concurrency_min_target, target)
            if downshift:
                self.concurrency_downshifts += 1
                self.concurrency_source = "adaptive"
            if target <= 1 and self.concurrency > 1:
                self.concurrency_floor_hit = True

    def note_oversize(self, path: str, size: int) -> None:
        size = int(size or 0)
        with self._lock:
            self.oversize_files += 1
            self.oversize_bytes += size
            self.oversize_largest.append({"path": path, "size": size})
            self.oversize_largest.sort(key=lambda e: -int(e["size"]))
            del self.oversize_largest[_OVERSIZE_SAMPLE:]

    def report(
        self,
        *,
        max_file_mb: int,
        interrupted: bool = False,
        interrupted_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        elapsed = max(time.monotonic() - self.started, 1e-6)
        processed = self.new + self.changed
        return {
            "mode": "builtin",
            "started_at": self.started_at,
            "finished_at": _now_iso(),
            "duration_s": round(elapsed, 1),
            "interrupted": interrupted,
            # Why the run stopped early: one of `_STOP_REASONS`' values
            # ("timeout" when extraction.timeout_s expired, "throttled" when
            # the tenant's 429 budget ran out), "error" for anything else, or
            # None on a clean pass. An operator reading a short report must be
            # able to tell "this is all there was" from "this is where we ran
            # out of clock". The named reasons are exactly the stops that
            # leave consistent state on disk, so a reader may say "the next
            # run resumes" for them and must not for "error".
            "interrupted_reason": interrupted_reason,
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
            # What the pool was ALLOWED to do, what the tenant let it do, and
            # what it actually did. `max_in_flight` below `effective_max`
            # means something other than the knob bounded the run; a non-zero
            # `downshifts` means the tenant did, which is a fact an operator
            # has to be able to SEE rather than infer from a slow run.
            "concurrency": {
                "configured": self.concurrency_configured,
                "requested": self.concurrency,
                "source": self.concurrency_source,
                "effective_max": self.concurrency_effective_max,
                "min_target": self.concurrency_min_target,
                "downshifts": self.concurrency_downshifts,
                "floor_hit": self.concurrency_floor_hit,
            },
            "max_in_flight": self.max_in_flight,
            "item_seconds": round(self.item_seconds, 1),
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


async def _stream_body_to_temp(resp: httpx.Response, tmp_path: Path, max_bytes: int, item_id: str) -> int:
    """Stream ``resp``'s body into ``tmp_path``, enforcing ``max_bytes`` as it
    goes. Shared by :meth:`GraphTransport.download_to_temp`'s direct-200 and
    followed-redirect-200 paths, so the size cap applies identically to
    either — a redirect must never become a way around it."""
    written = 0
    with open(tmp_path, "wb") as fh:
        async for chunk in resp.aiter_bytes(_DOWNLOAD_CHUNK):
            written += len(chunk)
            if max_bytes and written > max_bytes:
                raise CrawlError(f"download exceeded the {max_bytes}-byte cap for item {item_id}")
            fh.write(chunk)
    return written


class GraphAuth:
    """App-only token with proactive mid-crawl refresh.

    A client-credentials token lives ~1h; a full crawl outlives it several
    times over. Re-acquired once the live one is inside :data:`REFRESH_MARGIN`
    of expiry — before requests start failing, not after. The exchange itself
    is ``graph_client.get_app_token``: this class holds a token's lifetime,
    it does not reimplement the certificate-credential flow.

    **One refresh, not N.** With concurrent in-flight requests the expiry
    window is crossed by every one of them at once, and a 401 storm arrives
    at every one of them at once. Both paths therefore go through
    :attr:`_lock` and re-check under it: the first caller performs the
    exchange, the rest observe the fresh token and reuse it. Without that,
    a crawl at concurrency N burns N certificate exchanges per expiry and
    per 401 — against a tenant that is already rate-limiting it.

    The lock is an ``asyncio.Lock`` rather than a ``threading.Lock`` because
    every token holder is a coroutine on this run's single event loop; the
    worker threads this crawl uses run the convert/ingest section, which
    never touches a token.
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
        self._lock = asyncio.Lock()

    def _fresh(self) -> bool:
        return self._token is not None and self._clock() < self.expires_at - self.REFRESH_MARGIN

    async def token(self) -> str:
        if self._fresh():
            assert self._token is not None
            return self._token
        async with self._lock:
            # Re-checked under the lock: while this caller waited, another
            # may have done the exchange already.
            if self._fresh():
                assert self._token is not None
                return self._token
            return await self._acquire_locked()

    async def refresh(self) -> str:
        """Force an exchange (the 401 path). Callers that know WHICH token
        they saw fail should prefer :meth:`refresh_stale`, which coalesces a
        concurrent 401 storm into one exchange."""
        async with self._lock:
            return await self._acquire_locked()

    async def refresh_stale(self, observed: Optional[str]) -> str:
        """Refresh only if ``observed`` is still the live token.

        Under concurrency, N in-flight requests can each meet a 401 against
        the SAME expired token. The first one through replaces it; the others
        would otherwise each buy an identical new token (and each count a
        `token_refreshes`, overstating what the run actually did). Seeing a
        token other than the one they failed with is proof the refresh they
        needed has already happened.
        """
        async with self._lock:
            if self._token is not None and observed is not None and self._token != observed:
                return self._token
            return await self._acquire_locked()

    async def _acquire_locked(self) -> str:
        """The exchange itself. Callers hold :attr:`_lock`."""
        self._token = str(await self._acquire())
        self.expires_at = self._clock() + self._DEFAULT_TTL
        self._stats.add(token_refreshes=1)
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

    async def _authorized(self) -> Tuple[Dict[str, str], str]:
        """``(headers, token)``. The token is returned as well as stamped so
        a 401 can name WHICH token failed — :meth:`GraphAuth.refresh_stale`
        needs that to coalesce a concurrent 401 storm into one exchange."""
        token = await self.auth.token()
        return {"Authorization": f"Bearer {token}"}, token

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
        used_token: Optional[str] = None
        while True:
            attempt += 1
            self.stats.add(requests=1)
            try:
                headers, used_token = await self._authorized()
                async with graph_client._http_client() as client:
                    resp = await client.get(url, headers=headers, timeout=_REQUEST_TIMEOUT_S)
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
                self.stats.add(retries=1)
                logger.info("sharepoint crawl: 401 — forcing a token refresh")
                await self.auth.refresh_stale(used_token)
                continue
            if action == "throttle":
                throttle_wait += wait
                self.stats.add(http_429=1, throttle_wait_s=wait, retries=1)
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
                self.stats.add(http_429=1)
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
        self.stats.add(retries=1, retry_wait_s=wait)
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

        **The 302.** Graph answers ``GET .../content`` with a redirect to a
        pre-authenticated URL on a DIFFERENT host (blob storage, never
        ``graph.microsoft.com``) rather than the bytes themselves. httpx does
        not follow redirects by default, and turning that on
        (``follow_redirects=True``) would not be safe here even so: httpx
        replays the ``Authorization`` header across a redirect, which would
        hand the Graph bearer token to that other host. So a 3xx with a
        ``Location`` is followed by hand, with a SEPARATE, unauthenticated
        request — never through :func:`_require_graph_url`, deliberately,
        since that guard exists to keep the bearer token off a non-Graph
        host, and this second request carries no token to protect. The size
        cap and the temp-file cleanup guarantee apply identically to
        whichever response actually carried the bytes.
        """
        url = _require_graph_url(f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/content")
        fd, tmp_name = tempfile.mkstemp(suffix=Path(name or "").suffix)
        os.close(fd)
        tmp_path = Path(tmp_name)
        attempt = 0
        throttle_wait = 0.0
        refreshed = False
        used_token: Optional[str] = None
        try:
            while True:
                attempt += 1
                self.stats.add(requests=1)
                try:
                    headers, used_token = await self._authorized()
                    async with graph_client._http_client() as client:
                        redirect_location: Optional[str] = None
                        async with client.stream("GET", url, headers=headers, timeout=_REQUEST_TIMEOUT_S) as resp:
                            if 300 <= resp.status_code < 400:
                                redirect_location = resp.headers.get("Location")
                            if redirect_location is None:
                                action, wait = self._classify(resp, attempt, throttle_wait, refreshed)
                                if action == "ok":
                                    written = await _stream_body_to_temp(resp, tmp_path, max_bytes, item_id)
                                    self.stats.add(downloads=1, bytes_downloaded=written)
                                    return tmp_path
                                await resp.aread()  # drain before deciding, so the connection is reusable
                            else:
                                await resp.aread()  # drain the redirect body before following it

                        if redirect_location is not None:
                            # No Authorization header on this one — see the docstring.
                            async with client.stream("GET", redirect_location, timeout=_REQUEST_TIMEOUT_S) as resp:
                                action, wait = self._classify(resp, attempt, throttle_wait, refreshed)
                                if action == "ok":
                                    written = await _stream_body_to_temp(resp, tmp_path, max_bytes, item_id)
                                    self.stats.add(downloads=1, bytes_downloaded=written)
                                    return tmp_path
                                await resp.aread()
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
                    self.stats.add(retries=1)
                    await self.auth.refresh_stale(used_token)
                    continue
                if action == "throttle":
                    throttle_wait += wait
                    self.stats.add(http_429=1, throttle_wait_s=wait, retries=1)
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


def anonymize_markdown(text: str, *, key: bytes, detector: Any = None) -> Any:
    """``src.anonymization.anonymize_markdown`` — the seam.

    Returns that module's ``AnonymizeResult`` (``.text``, ``.replaced``).
    ``detector`` is ``None`` for the deterministic regex tier (the module's
    own default) or the hybrid regex+LLM detector built by
    :func:`_entity_detector`. Callers treat ANY failure here — ``ImportError``
    and the LLM tier's ``DetectionUnavailable`` included — as "this document
    cannot be anonymized", which for an anonymize-marked scope means it is
    skipped, never ingested raw.
    """
    from src.anonymization import anonymize_markdown as _anonymize

    return _anonymize(text, key=key, detector=detector)


# --------------------------------------------------------------------------
# Scope -> drives
# --------------------------------------------------------------------------


class _Deadline:
    """The run's wall-clock bound (``extraction.timeout_s``).

    Checked at the two points where stopping is free — between files and
    between delta pages — rather than interrupting mid-download, because the
    resume guarantee depends on state being written at exactly those
    boundaries. ``timeout_s <= 0`` means unbounded.
    """

    def __init__(self, timeout_s: float, *, clock: Optional[Callable[[], float]] = None) -> None:
        # Resolved through the module attribute at call time, not bound as a
        # default argument at import time — the same reason every other seam
        # in this module defers: a test must be able to substitute it.
        self._clock = clock or (lambda: time.monotonic())
        self.timeout_s = float(timeout_s or 0)
        self.expires_at: Optional[float] = self._clock() + self.timeout_s if self.timeout_s > 0 else None

    def expired(self) -> bool:
        return self.expires_at is not None and self._clock() >= self.expires_at

    def check(self) -> None:
        """Raise :class:`CrawlTimeout` if the budget is spent."""
        if self.expired():
            raise CrawlTimeout(f"extraction.timeout_s ({self.timeout_s:.0f}s) elapsed — stopping; the next run resumes")


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


@dataclass(frozen=True)
class _ExclusionIndex:
    """One scope's ``excluded_subtrees`` entries, split by the TWO different
    match rules a ``kind`` demands (2026-08-31 sweep v2 / TCRD-284):

    * ``kind=="folder"`` (or a legacy entry with no ``kind`` at all — the
      exclusion semantics every entry had before kinds existed) excludes
      everything AT OR UNDER its path — component-safe prefix, same rule
      :func:`_under_prefix` already applied.
    * ``kind=="file"`` excludes EXACTLY that one item — matched by
      drive-relative path AND by its stable id, never as a prefix, so a
      sibling whose name merely starts with the excluded file's name is
      never swept up with it (the bug a bare prefix test would have had).

    Empty by default — a scope with nothing excluded, or
    ``include_excluded_subtrees``, gets one with no members and every match
    below is trivially false.
    """

    folder_prefixes: Tuple[str, ...] = ()
    file_paths: "frozenset[str]" = frozenset()
    file_ids: "frozenset[str]" = frozenset()


def _excluded_file(path: str, stable_id: str, index: _ExclusionIndex) -> bool:
    """Whether one crawled item is a ``kind=="file"`` exclusion — checked by
    stable id (the entry's own ``item_id``, the identifier this crawler
    already keys everything on) OR by exact drive-relative path, per the
    module's matching semantics. Never a prefix test: that is
    :func:`_under_prefix`'s job, reserved for folder-kind entries."""
    return stable_id in index.file_ids or (bool(path) and path in index.file_paths)


async def _excluded_path_prefixes(transport: GraphTransport, scope: Dict[str, Any]) -> _ExclusionIndex:
    """This scope's exclusions, resolved to drive-relative paths and split
    into folder-prefix vs. file-exact matches (see :class:`_ExclusionIndex`).

    Fast path (TCRD-284): an entry already carrying ``rel_path`` — every
    entry the sweep has written since the rel-path/kind fields shipped — is
    used AS-IS, no Graph call. A legacy entry (pre those fields, no
    ``rel_path``) falls back to resolving its ``item_id`` against Graph
    exactly as before, and is treated as a folder prefix (it predates
    ``kind`` too).

    Fail-closed: a legacy entry whose Graph resolution fails raises, and the
    caller skips the whole scope. Crawling a scope whose exclusions could not
    be applied would ingest exactly the content an admin excluded.

    A scope carrying ``include_excluded_subtrees`` is exempt — the admin
    decided that audience may see the content — matching the same override
    ``app/api/admin_sharepoint.py::confirm_scope`` writes (spec §6.3's
    ``should_not`` per-subtree override).
    """
    if scope.get("include_excluded_subtrees"):
        return _ExclusionIndex()
    excluded = scope.get("excluded_subtrees")
    if not isinstance(excluded, list) or not excluded:
        return _ExclusionIndex()
    drive_id = scope.get("drive_id")

    folder_prefixes: List[str] = []
    file_paths: set = set()
    file_ids: set = set()

    for entry in excluded:
        if not isinstance(entry, dict) or not entry.get("item_id"):
            continue
        item_id = str(entry["item_id"])
        rel_path = entry.get("rel_path")

        if not rel_path:
            if not drive_id:
                raise CrawlError(
                    f"scope {scope.get('source_scope_id')!r} has excluded subtrees but no drive_id — "
                    "cannot resolve them to paths, refusing to crawl it"
                )
            body = await transport.get_json(
                f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}?$select=id,name,parentReference"
            )
            rel_path = _drive_relative_path(body.get("parentReference") or {}, str(body.get("name") or ""))
        rel_path = str(rel_path)
        if not rel_path:
            continue

        if entry.get("kind") == "file":
            file_paths.add(rel_path)
            file_ids.add(f"graph:{item_id}")
        else:
            folder_prefixes.append(rel_path)

    return _ExclusionIndex(
        folder_prefixes=tuple(folder_prefixes), file_paths=frozenset(file_paths), file_ids=frozenset(file_ids)
    )


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


def _zone_routes_for_scope(connection: Dict[str, Any], source_scope_id: str) -> Dict[str, List[Tuple[str, str]]]:
    """This scope's ACTIVE permission zones (2026-08-31 plan, Task 3/4;
    TCRD-284 wires them into the builtin crawler's own routing), grouped by
    drive and sorted DEEPEST-``rel_path``-first — so :func:`_route_collection`
    can return the first match and get "nested zones: deepest wins" for
    free. A dissolved zone is never in here (:func:`active_zone_rows`
    already filtered it out), so its content falls straight through to the
    scope's own collection — the exact re-homing-on-dissolve behavior the
    sweep's own retroactive cleanup (``acl_sync._cleanup_connection_content``)
    already assumes.

    Keyed by ``zone["drive_id"]``, not the scope row's own (frequently
    absent, and meaningless for a site scope that fans out to many drives):
    a zone always carries the same drive id its root folder lives on
    (``acl_sync._reconcile_zones`` copies it straight from the parent scope
    at zone-creation time) — the same drive a :class:`DriveTarget` crawls —
    so matching on ``target.drive_id`` at lookup time is the correct join
    key regardless of the scope's own kind.
    """
    by_drive: Dict[str, List[Tuple[str, str]]] = {}
    for zone in active_zone_rows(connection):
        if zone.get("parent_scope_id") != source_scope_id:
            continue
        drive_id = zone.get("drive_id")
        rel_path = zone.get("rel_path")
        collection_id = zone.get("collection_id")
        if not drive_id or not rel_path or not collection_id:
            continue
        by_drive.setdefault(str(drive_id), []).append((str(rel_path), str(collection_id)))
    for routes in by_drive.values():
        routes.sort(key=lambda pair: -len(pair[0]))
    return by_drive


@dataclass
class _ScopeContext:
    """Everything the per-item pipeline needs about the scope it is in."""

    source_scope_id: str
    collection_id: str
    anonymize: bool
    exclusions: _ExclusionIndex
    zone_routes_by_drive: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)

    def candidate_collection_ids(self, drive_id: str) -> List[str]:
        """This scope's own collection, then every zone collection this
        drive routes to (deepest first) — the set a DELETED item (which
        carries no ``parentReference`` to route by path) might have been
        ingested into, so a deletion can find it wherever it actually
        landed."""
        ids = [self.collection_id]
        ids.extend(collection_id for _prefix, collection_id in self.zone_routes_by_drive.get(drive_id, ()))
        return ids


def _route_collection(path: str, drive_id: str, ctx: _ScopeContext) -> str:
    """The collection one crawled file lands in (TCRD-284): the DEEPEST
    active permission zone whose ``rel_path`` contains ``path``
    (component-safe — ``path == prefix or path.startswith(prefix + "/")``),
    else the scope's own collection. ``ctx.zone_routes_by_drive`` is
    pre-sorted deepest-prefix-first, so the first match wins.

    Agreement with :mod:`connectors.sharepoint.ingest_gate` is load-bearing,
    not incidental: that module refuses, server-side, any document whose
    path falls under an ACTIVE zone but landed in a DIFFERENT collection
    than that zone's own (``source_acl_zone_mismatch``) — this function is
    what keeps a correctly-routed crawl from EVER tripping it.
    """
    for prefix, collection_id in ctx.zone_routes_by_drive.get(drive_id, ()):
        if path == prefix or path.startswith(prefix + "/"):
            return collection_id
    return ctx.collection_id


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


async def _run_blocking(pool: Optional[ThreadPoolExecutor], fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a BLOCKING step, on ``pool`` when the run is parallel.

    Convert, anonymize, hash and ingest are synchronous CPU/DB work. On the
    event loop they stall every other item's download for their whole
    duration, which would make "concurrency: 6" buy almost nothing — the six
    downloads would queue behind one markitdown call. Off the loop they
    overlap with downloads and with each other.

    The pool is passed in rather than taken from ``asyncio.to_thread``'s
    default executor on purpose: that one is sized ``min(32, cpu+4)``, so on
    a 4-core host a configured concurrency above 8 would silently not
    happen — the knob would stop meaning what it says.

    ``pool=None`` (concurrency 1) makes the call INLINE, so the sequential
    path is exactly the pre-parallel one: same call, same thread, same
    ordering, no executor in the picture at all.
    """
    if pool is None:
        return fn(*args, **kwargs)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(pool, functools.partial(fn, *args, **kwargs))


@dataclass
class _PreparedDocument:
    """Outcome of the blocking half of one item: hash -> convert -> anonymize.

    A value rather than in-place counter bumps, because this runs on a worker
    thread: the caller (on the event loop) turns the outcome into the exact
    same counters the sequential pipeline recorded, so a fail-closed refusal
    is still counted once, per item, whatever thread noticed it.
    """

    outcome: str  # "ok" | "convert_failed" | "convert_empty" | "anonymize_failed"
    markdown: str = ""
    source_sha256: str = ""


def _prepare_document(
    tmp_path: Path,
    *,
    mime: str,
    path: str,
    anonymize: bool,
    anonymization_key: Optional[bytes],
    detector: Any,
) -> _PreparedDocument:
    """Hash, convert and (for an anonymize-marked scope) anonymize one file.

    Pure with respect to crawl state — it touches no counters, no cTags and
    no state file — which is what makes it safe to run on a worker thread.
    An unexpected failure of the HASH itself still propagates (as it did
    before): a file we cannot read is not a convert failure.
    """
    source_sha256 = _sha256_file(tmp_path)
    try:
        converted = convert_to_markdown(tmp_path, mime)
        markdown = str(getattr(converted, "markdown", "") or "")
    except Exception as exc:  # noqa: BLE001 — one unconvertible file, not a broken run
        logger.warning("sharepoint crawl: conversion failed for %s: %s", path, type(exc).__name__)
        return _PreparedDocument("convert_failed")
    if not markdown.strip():
        logger.info("sharepoint crawl: conversion produced no text for %s", path)
        return _PreparedDocument("convert_empty")

    if anonymize:
        # FAIL CLOSED. An anonymize-marked scope promised its audience that
        # no raw identifier reaches the collection; a document that cannot be
        # anonymized is therefore counted and dropped, never ingested in its
        # original form. Unchanged by concurrency: the refusal is decided per
        # document, inside this function, before any caller can ingest it.
        if anonymization_key is None:
            return _PreparedDocument("anonymize_failed")
        try:
            markdown = str(anonymize_markdown(markdown, key=anonymization_key, detector=detector).text)
        except Exception as exc:  # noqa: BLE001 — incl. ImportError / DetectionUnavailable
            logger.warning("sharepoint crawl: anonymization failed for %s: %s", path, type(exc).__name__)
            return _PreparedDocument("anonymize_failed")
    return _PreparedDocument("ok", markdown=markdown, source_sha256=source_sha256)


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
    detector: Any = None,
    pool: Optional[ThreadPoolExecutor] = None,
) -> None:
    """One delta row -> at most one ingested document. Never raises for a
    per-file fault: a locked, vanished, unconvertible, or un-anonymizable
    document is COUNTED and skipped, because one bad file must not cost a
    100k-file pass.

    Safe to run concurrently with itself: every counter goes through
    :meth:`CrawlStats.add`, and the two state mutations (this item's cTag) are
    taken under :data:`_state_lock`, which is the same lock
    :func:`save_state` serializes on. ``pool`` puts the blocking
    convert/anonymize/ingest section on a worker thread; at ``None`` the
    calls are inline, i.e. the pre-parallel path exactly.
    """
    name = str(item.get("name") or "")
    stable_id = f"graph:{item['id']}"
    ctags: Dict[str, Any] = state["ctags"]

    if item.get("deleted"):
        for candidate in ctx.candidate_collection_ids(target.drive_id):
            if await _run_blocking(pool, ingestor.delete, candidate, stable_id):
                stats.add(deleted=1)
                break
        with _state_lock:
            ctags.pop(stable_id, None)
        return
    if "file" not in item or _should_skip_name(name):
        return

    path = _drive_relative_path(item.get("parentReference") or {}, name)
    if _excluded_file(path, stable_id, ctx.exclusions):
        stats.add(excluded_subtree_skips=1)
        return
    if ctx.exclusions.folder_prefixes and _under_prefix(path, ctx.exclusions.folder_prefixes):
        stats.add(excluded_subtree_skips=1)
        return

    collection_id = _route_collection(path, target.drive_id, ctx)

    ctag = item.get("cTag") or item.get("eTag")
    with _state_lock:
        already = bool(ctag) and ctags.get(stable_id) == ctag
    if already:
        stats.add(unchanged=1)
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
        # deltaLink + cTags. Under concurrency the page driver also stops
        # feeding the pool the moment this escapes, so a 429 storm costs one
        # budget per IN-FLIGHT item, never one per remaining file.
        raise
    except (CrawlError, SharePointGraphError, httpx.HTTPError) as exc:
        stats.add(errors=1)
        logger.warning("sharepoint crawl: download failed for %s: %s", path, type(exc).__name__)
        return

    try:
        prepared: _PreparedDocument = await _run_blocking(
            pool,
            _prepare_document,
            tmp_path,
            mime=mime,
            path=path,
            anonymize=ctx.anonymize,
            anonymization_key=anonymization_key,
            detector=detector,
        )
    finally:
        # The local copy never persists — success, skip, or failure.
        tmp_path.unlink(missing_ok=True)

    if prepared.outcome == "convert_failed":
        stats.add(convert_failed=1, errors=1)
        return
    if prepared.outcome == "convert_empty":
        stats.add(convert_failed=1)
        return
    if prepared.outcome == "anonymize_failed":
        stats.add(anonymize_failed=1)
        return

    try:
        _file_id, was_new = await _run_blocking(
            pool,
            ingestor.ingest,
            collection_id=collection_id,
            stable_id=stable_id,
            path=path,
            filename=f"{Path(name).stem or name}.md",
            markdown=prepared.markdown,
            source_sha256=prepared.source_sha256,
        )
    except Exception as exc:  # noqa: BLE001 — one file's ingest, not the run
        stats.add(errors=1)
        logger.warning("sharepoint crawl: ingest failed for %s: %s", path, type(exc).__name__)
        return

    if was_new:
        stats.add(new=1)
    else:
        stats.add(changed=1)
    # Written only AFTER the document is durably ingested: a cTag recorded
    # before the ingest would make a resumed run skip a file it never landed.
    # Per ITEM, not per page — a slow neighbour in the same page must not
    # hold this one's cTag hostage — but always under the state lock, so it
    # can never land inside a `save_state` serialization.
    if ctag:
        with _state_lock:
            ctags[stable_id] = ctag


class _ConcurrencyGovernor:
    """The in-flight target, and the AIMD that moves it when Graph pushes back.

    Multiplicative decrease, additive increase, evaluated ONCE per delta page
    — the boundary where the crawl is already quiescent, so a change of
    target never splits a page across two policies:

    * a page that met a THROTTLE BURST (more than
      :data:`_THROTTLE_BURST_429S` throttled responses, or more than
      :data:`_THROTTLE_BURST_WAIT_S` of honored ``Retry-After``) halves the
      target, floor 1;
    * a clean page adds 1 back, ceiling the configured cap.

    A downshift NEVER aborts anything: the run keeps going, more slowly. The
    hard stop stays where it was — the per-request 429 budget raising
    :class:`GraphThrottled`. Backing off and giving up are different answers
    and the tenant is telling us the first one.

    Adaptive only when the cap is > 1: an operator who pinned concurrency to
    1 asked for the sequential crawl, not for a governor that agrees with them.
    """

    def __init__(
        self,
        cap: int,
        *,
        stats: Optional[CrawlStats] = None,
        burst_429s: int = _THROTTLE_BURST_429S,
        burst_wait_s: float = _THROTTLE_BURST_WAIT_S,
    ) -> None:
        self.cap = max(1, int(cap))
        self.target = self.cap
        self.adaptive = self.cap > 1
        self.burst_429s = burst_429s
        self.burst_wait_s = burst_wait_s
        self.downshifts = 0
        self.floor_hit = False
        self._stats = stats
        # Its own lock, not the stats one: the target is read on the event
        # loop at every page start and could be written from anywhere a
        # future caller decides to observe from.
        self._lock = threading.Lock()
        if stats is not None:
            stats.note_concurrency(target=self.target)

    def current(self) -> int:
        with self._lock:
            return self.target

    def observe_page(self, throttled_429s: int, throttle_wait_s: float) -> int:
        """Fold ONE page's throttling into the target; return the new target.

        Arguments are the page's own deltas (see
        :meth:`CrawlStats.throttle_snapshot`), never running totals — a
        cumulative count would keep halving forever after a single bad page.
        """
        if not self.adaptive:
            return self.current()
        burst = throttled_429s > self.burst_429s or throttle_wait_s > self.burst_wait_s
        with self._lock:
            before = self.target
            if burst:
                self.target = max(1, self.target // 2)
                if self.target < before:
                    self.downshifts += 1
                if self.target == 1:
                    self.floor_hit = True
            else:
                self.target = min(self.cap, self.target + 1)
            target, downshifted = self.target, burst and self.target < before
        if self._stats is not None:
            self._stats.note_concurrency(target=target, downshift=downshifted)
        if downshifted:
            logger.info(
                "sharepoint crawl: %d throttled responses in one page (%.0fs waited) — "
                "halving in-flight target to %d (cap %d)",
                throttled_429s,
                throttle_wait_s,
                target,
                self.cap,
            )
        return target


#: How severely an exception escaping one item's pipeline ends the run.
#: Higher wins when several workers fail in the same page — the run reports
#: the WORST thing that happened to it, never the first one to be noticed.
#: A tenant-wide throttle outranks the clock: "we ran out of time" invites a
#: retry, "the tenant is refusing us" is what an operator has to act on.
_ABORT_SEVERITY: Tuple[type, ...] = (GraphThrottled, CrawlTimeout, GraphGone)


def _abort_rank(exc: BaseException) -> int:
    for rank, kind in enumerate(_ABORT_SEVERITY):
        if isinstance(exc, kind):
            return len(_ABORT_SEVERITY) - rank
    return 0


async def _process_page(
    items: Sequence[Dict[str, Any]],
    *,
    target: DriveTarget,
    ctx: _ScopeContext,
    transport: GraphTransport,
    ingestor: _Ingestor,
    state: Dict[str, Any],
    stats: CrawlStats,
    max_file_mb: int,
    anonymization_key: Optional[bytes],
    detector: Any = None,
    deadline: Optional[_Deadline] = None,
    concurrency: int = 1,
) -> None:
    """Run ONE delta page's rows, up to ``concurrency`` items at a time.

    The page is the unit of the resume contract, and this function is what
    keeps that true under parallelism:

    * every item's cTag is still written by the item itself, right after its
      own durable ingest — a slow neighbour cannot delay it, and a fast
      neighbour cannot claim it;
    * this function does not return until every worker has finished, so the
      caller's ``deltaLink`` persist + recorder checkpoint still happen after
      *all* of the page's rows, never in the middle of it;
    * the deadline is re-checked before each item is picked up, so an expired
      budget stops FEEDING the pool and then drains it, rather than starting
      work it has no time to finish;
    * an exception in one item stops further pickups and is re-raised only
      after the in-flight ones are done — their completed work is already
      recorded (counters, cTags) and is not thrown away.

    ``concurrency <= 1`` takes the sequential branch: the same loop, the same
    inline calls and the same ordering the crawl had before this existed, so
    "1 == today's behaviour" is a property of the code, not a hope.
    """
    workers = max(1, int(concurrency))
    if workers == 1:
        for item in items:
            stats.add(items_seen=1)
            # Between files. The cTag of the item just finished is already in
            # `state`; the page's deltaLink is not written until the page
            # completes, so a stop here re-reads this page next run and every
            # already-ingested item in it upserts to a no-op.
            if deadline is not None:
                deadline.check()
            # Pure bookkeeping, so the report says `max_in_flight: 1` here
            # rather than a 0 that reads as "nothing ever ran".
            stats.enter_item()
            started = time.monotonic()
            try:
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
                    detector=detector,
                )
            finally:
                stats.exit_item(time.monotonic() - started)
            stats.add(items_done=1)
        return

    if not items:
        return

    cursor = 0
    aborts: List[BaseException] = []
    # Bounded, and sized to the target the governor handed us — not the event
    # loop's default executor, whose min(32, cpu+4) ceiling would quietly cap
    # a configured concurrency above it. The `shutdown(wait=True)` below joins
    # every worker thread, a second and independent guarantee that no blocking
    # step of this page is still running when the caller persists the page's
    # deltaLink.
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sp-crawl")

    def _next_item() -> Optional[Dict[str, Any]]:
        # Single-threaded by construction: every worker below is a coroutine
        # on this run's event loop and there is no await between the read and
        # the write, so the index cannot be handed out twice.
        nonlocal cursor
        if aborts or cursor >= len(items):
            return None
        item = items[cursor]
        cursor += 1
        return item

    async def _worker() -> None:
        while True:
            if deadline is not None:
                try:
                    deadline.check()
                except CrawlTimeout as exc:
                    aborts.append(exc)
                    return
            item = _next_item()
            if item is None:
                return
            stats.add(items_seen=1)
            stats.enter_item()
            started = time.monotonic()
            try:
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
                    detector=detector,
                    pool=pool,
                )
            except BaseException as exc:  # noqa: BLE001 — re-raised after the drain
                # Anything escaping `_process_item` is by definition NOT a
                # per-file fault (those are counted inside it): a throttle
                # budget spent, the deadline, a dead deltaLink. Stop taking
                # new items; the drain below still lets the peers finish.
                aborts.append(exc)
                return
            finally:
                stats.exit_item(time.monotonic() - started)
            stats.add(items_done=1)

    try:
        # `gather` without `return_exceptions` would cancel the peers on the
        # first failure — exactly the "one bad future loses the others'
        # completed work" this must not do. Every worker returns normally and
        # parks its exception in `aborts` instead.
        await asyncio.gather(*[_worker() for _ in range(min(workers, len(items)))])
    finally:
        pool.shutdown(wait=True)

    if aborts:
        raise max(aborts, key=_abort_rank)


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
    recorder: Optional["_RunRecorder"] = None,
    detector: Any = None,
    deadline: Optional[_Deadline] = None,
    governor: Optional[_ConcurrencyGovernor] = None,
) -> None:
    """Delta-enumerate one drive (or one folder subtree), resuming from its
    persisted ``deltaLink``.

    ``recorder`` (optional, defaults to no recording) rides the checkpoint
    this function already writes — see :class:`_RunRecorder`.

    ``governor`` supplies the WITHIN-PAGE item concurrency (see
    :func:`_process_page`) and is fed this drive's throttling at every page
    boundary, so a tenant pushing back shrinks the target for the pages that
    follow. Pages themselves remain strictly sequential: the next page's URL
    is only known once this page's response has been read, and its deltaLink
    must not be persisted before the current page's rows are on disk. Drives,
    likewise, remain sequential — see the note in :func:`_run_crawl_async`."""
    governor = governor or _ConcurrencyGovernor(1)
    delta_links: Dict[str, Any] = state["delta_links"]
    base = f"{target.delta_url}?$top={_DELTA_PAGE_SIZE}"
    url: Optional[str] = delta_links.get(target.state_key) or base
    resynced = False
    stats.add(drives=1)
    # Where this page's throttle accounting starts. Taken BEFORE the delta
    # fetch, so a 429 storm on the page request itself counts as the tenant
    # pushing back too — and deliberately not reset by a 410 resync, so that
    # detour's throttling carries into the next observation instead of
    # vanishing.
    page_throttle_mark: Optional[Tuple[int, float]] = None

    while url:
        # Between pages: the previous page's rows are ingested and its
        # deltaLink/cTags are on disk, so stopping here costs nothing.
        if deadline is not None:
            deadline.check()
        if page_throttle_mark is None:
            page_throttle_mark = stats.throttle_snapshot()
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
            with _state_lock:
                delta_links.pop(target.state_key, None)
                save_state(connection_id, state)
            stats.add(delta_resyncs=1)
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
            stats.add(permission_skips=1)
            logger.warning(
                "sharepoint crawl: HTTP %s on drive %s — skipping it (no access for this app registration)",
                exc.status_code,
                target.drive_name or target.drive_id,
            )
            return

        await _process_page(
            [item for item in page.get("value", []) if isinstance(item, dict) and item.get("id")],
            target=target,
            ctx=ctx,
            transport=transport,
            ingestor=ingestor,
            state=state,
            stats=stats,
            max_file_mb=max_file_mb,
            anonymization_key=anonymization_key,
            detector=detector,
            deadline=deadline,
            concurrency=governor.current(),
        )
        # The page's OWN throttling, not the run's running total: the
        # governor folds a DELTA, so one bad page cannot keep halving the
        # target for the rest of the crawl.
        throttled_after, waited_after = stats.throttle_snapshot()
        governor.observe_page(throttled_after - page_throttle_mark[0], waited_after - page_throttle_mark[1])
        page_throttle_mark = None

        # Rows first, then the link: the deltaLink is persisted only after
        # everything the page produced is ingested and its cTags are on disk,
        # so a crash between the two costs re-work, never coverage.
        delta_link = page.get("@odata.deltaLink")
        if delta_link:
            # Under the state lock like every other mutation of `state`, even
            # though the pool is provably drained by here — the invariant is
            # enforced by the lock, not by an argument about who is running.
            with _state_lock:
                delta_links[target.state_key] = _require_graph_url(str(delta_link))
                save_state(connection_id, state)
            url = None
        else:
            next_link = page.get("@odata.nextLink")
            save_state(connection_id, state)
            url = _require_graph_url(str(next_link)) if next_link else None
        # The SAME checkpoint boundary, a second destination — no new write
        # loop and no new frequency (design §7.1). It runs after the state
        # file, so a recorder failure can never cost the crawl its resume
        # point.
        if recorder is not None:
            recorder.checkpoint(stats)


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


def _crawl_concurrency() -> int:
    """``extraction.crawler.concurrency`` — how many items of ONE delta page
    the crawl pipelines at a time.

    Clamped to ``[1, _MAX_CONCURRENCY]``; a missing or unparseable value is
    the default. ``1`` is the pre-parallel behaviour exactly — the escape
    hatch for an operator whose tenant is throttling hard, and the setting
    the crawl's own golden test pins.

    NOT to be confused with its neighbour ``extraction.concurrency``
    (``app/worker/runtime.py::_extraction_concurrency``), which sizes the
    worker's extraction LANE — how many crawl jobs run at once. The two
    multiply: two concurrent crawls at 6 put twelve files in flight against
    the same tenant.
    """
    from app.instance_config import get_value

    raw = get_value("extraction", "crawler", "concurrency", default=_DEFAULT_CONCURRENCY)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_CONCURRENCY
    return max(1, min(_MAX_CONCURRENCY, value))


def _resolve_concurrency(override: Any) -> Tuple[int, int, str]:
    """``(effective_cap, configured, source)`` for one run.

    ``payload["concurrency"]`` overrides the configured value for THIS run
    only — the same shape ``payload["timeout_s"]`` already has — clamped to
    ``[1, _MAX_PAYLOAD_CONCURRENCY]``. An unparseable override is ignored in
    favour of config rather than guessed at: a typo in an ad-hoc payload must
    not silently re-tune the crawl.
    """
    configured = _crawl_concurrency()
    if override is None:
        return configured, configured, "config"
    try:
        requested = int(override)
    except (TypeError, ValueError):
        logger.warning("sharepoint crawl: ignoring unparseable payload concurrency %r — using config", override)
        return configured, configured, "config"
    return max(1, min(_MAX_PAYLOAD_CONCURRENCY, requested)), configured, "payload"


def _timeout_seconds() -> int:
    """``extraction.timeout_s`` — the run's wall-clock bound.

    Delegates to ``app.worker.kinds._extraction_timeout_seconds``, the single
    existing reader of that key (it also derives the job's lease from it, so
    the two can never disagree about what the ceiling is). Imported lazily,
    same posture as :func:`_resolve_anonymization_key` below.
    """
    from app.worker.kinds import _extraction_timeout_seconds

    return _extraction_timeout_seconds()


#: ``extraction.anonymization.detector`` values. ``"regex"`` is the default
#: and it is a COST decision, not a legacy one: the deterministic tier is
#: free and runs over every document of every crawl, while ``"llm"`` sends
#: each document to the instance's model — order ~$5 per 1,000 documents on
#: the default Haiku-class model. An operator who wants the recall an LLM
#: reader adds opts into paying for it.
_DETECTOR_REGEX = "regex"
_DETECTOR_LLM = "llm"


def _entity_detector() -> Any:
    """The entity detector for this instance's anonymize passes, or ``None``
    for the anonymizer's own deterministic default.

    ``extraction.anonymization.detector: "llm"`` builds
    ``hybrid_detector(LLMDetector())`` from :mod:`src.anonymization_ner` —
    regex first (free, exact by construction), then the LLM adds what only a
    reader can find. That tier's ``DetectionUnavailable`` deliberately
    propagates: the caller counts the document in ``anonymize_failed`` and
    drops it, rather than ingesting a document whose redaction quietly
    degraded to regex-only. Anything else, including unset, is the regex
    tier — see :data:`_DETECTOR_REGEX` for why that is the default.

    Built ONCE per run (the detector carries its own client + running token
    accounting) and imported lazily, so an instance on the regex tier never
    imports the LLM stack at all.
    """
    from app.instance_config import get_value

    choice = str(get_value("extraction", "anonymization", "detector", default="") or "").strip().lower()
    if choice != _DETECTOR_LLM:
        return None

    from src.anonymization_ner import LLMDetector, hybrid_detector

    llm = LLMDetector()
    detect = hybrid_detector(llm)
    # Expose the LLM tier so the run recorder can read its token accounting
    # (`total_usage`) at finish time — function objects take attributes, and
    # this keeps the detector contract itself a plain callable. Best-effort:
    # a detector double that refuses attributes just loses usage reporting.
    try:
        detect.llm = llm  # type: ignore[attr-defined]
    except AttributeError:
        pass
    return detect


def _detector_usage(detector: Any) -> Dict[str, Any]:
    """The LLM tier's running token accounting for this run, or ``{}``.

    ``{}`` means "no tokens were spent" — the regex tier, or no anonymize
    scope in the run — which the UI keeps distinct from a computed $0.00.
    Never raises: usage is observability, not a gate."""
    llm = getattr(detector, "llm", None)
    usage = getattr(llm, "total_usage", None)
    if not isinstance(usage, dict):
        return {}
    model = getattr(llm, "model", None)
    out: Dict[str, Any] = {k: v for k, v in usage.items() if isinstance(v, (int, float)) and v}
    if out and model:
        out["model"] = str(model)
    return out


def _ocr_run_usage(scan_ocr_module: Any) -> Dict[str, Any]:
    """The scan-OCR tier's run totals, or ``{}`` — the same zero-collapse
    honesty as :func:`_detector_usage`: an all-zero block (the switch off,
    or no scanned document met) reads as "no tokens spent", never as a
    measured $0.00. Never raises: usage is observability, not a gate."""
    if scan_ocr_module is None:
        return {}
    try:
        usage = scan_ocr_module.run_usage()
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(usage, dict):
        return {}
    return {k: v for k, v in usage.items() if isinstance(v, (int, float)) and v}


def maybe_run_facts_extraction(
    connection: Dict[str, Any], *, deadline: Optional[_Deadline] = None
) -> Optional[Dict[str, Any]]:
    """Run the LLM fact-extraction stage over what this crawl just ingested,
    or return ``None`` when it is switched off.

    Delegates to ``connectors.sharepoint.facts_extraction`` — the walk, the
    prompt, the verbatim retry, the batching and the ingest all live there;
    this module owns only the seam. Imported lazily so an instance with
    ``extraction.facts.enabled`` off never imports the LLM stack, the same
    posture :func:`_entity_detector` takes for the anonymizer's own.

    Synchronous inside the async crawl, exactly like the per-file
    ``ingestor.ingest`` call: this whole coroutine owns its worker's
    EXTRACTION lane slot, and there is no concurrent work for an event loop
    to interleave.
    """
    from connectors.sharepoint.facts_extraction import maybe_run_after_crawl

    return maybe_run_after_crawl(connection, deadline=deadline)


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
    job_id: Optional[str] = None,
    timeout_s: Optional[float] = None,
    concurrency: Optional[int] = None,
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
    # Built once per run, and only when something in this run will actually
    # anonymize — an instance on the regex tier never imports the LLM stack,
    # and an instance on the LLM tier never pays for a detector no scope uses.
    detector = _entity_detector() if anonymization_key is not None else None
    max_file_mb = _max_file_mb()
    cap, configured_concurrency, concurrency_source = _resolve_concurrency(concurrency)
    deadline = _Deadline(_timeout_seconds() if timeout_s is None else timeout_s)

    # Recorded on the stats object rather than threaded through `report()`:
    # the report is assembled in one place at the end of this function and
    # that block is deliberately left alone.
    stats = CrawlStats(
        concurrency=cap,
        concurrency_configured=configured_concurrency,
        concurrency_source=concurrency_source,
        concurrency_effective_max=cap,
        concurrency_min_target=cap,
    )
    # ONE governor for the whole run: the tenant throttles an app
    # registration, not a drive, so what one drive learns about backing off
    # must carry to the next.
    governor = _ConcurrencyGovernor(cap, stats=stats)
    auth = GraphAuth(
        acquire=lambda: graph_client.get_app_token(settings.tenant_id, settings.client_id, settings.private_key),
        stats=stats,
    )
    transport = GraphTransport(auth, stats)
    ingestor = _Ingestor()
    state = load_state(connection_id)
    scope_errors: List[Dict[str, Any]] = []
    # Opened BEFORE the first request so a run that dies in its first scope
    # is still a rendered row rather than a silence (design §4.3).
    recorder = _RunRecorder(connection_id, job_id=job_id)
    recorder.start()
    # The scan-OCR tier keeps module-level run totals (it is called from
    # deep inside the converter, which has no channel back to this run) —
    # zeroed here so a run's `ocr_usage` can never carry a predecessor's
    # spend. Import guarded: the module is import-light, but a broken
    # optional install must degrade to "no OCR accounting", not a dead crawl.
    try:
        from connectors.sharepoint import scan_ocr as _scan_ocr

        _scan_ocr.reset_run_usage()
    except Exception:  # noqa: BLE001
        _scan_ocr = None
    #: The LLM stage's own sub-report, or None when it is switched off.
    facts_report: Optional[Dict[str, Any]] = None

    try:
        for scope in scopes:
            source_scope_id = str(scope.get("source_scope_id"))
            try:
                targets = await _drive_targets(transport, scope)
                exclusions = await _excluded_path_prefixes(transport, scope)
            except (CrawlError, SharePointGraphError) as exc:
                # One scope's misconfiguration (or one site's outage) must not
                # cost the connection's other scopes their pass — the same
                # per-unit failure isolation `acl_sync` applies per connection.
                scope_errors.append({"scope": source_scope_id, "error": str(exc)})
                stats.add(errors=1)
                continue

            ctx = _ScopeContext(
                source_scope_id=source_scope_id,
                collection_id=str(scope["collection_id"]),
                anonymize=bool(scope.get("anonymize")),
                exclusions=exclusions,
                zone_routes_by_drive=_zone_routes_for_scope(connection, source_scope_id),
            )
            stats.add(scopes=1)
            # Drives stay SEQUENTIAL, deliberately. Parallelising them is the
            # second axis and it is not worth its risk here: every drive
            # shares one state file whose per-drive deltaLink is the resume
            # contract, and the 410-resync path mutates that file mid-drive;
            # the 429 budget and the deadline are likewise run-wide, so a
            # second axis mostly converts into 429s against the same tenant
            # rather than into throughput. In-page concurrency already
            # saturates a 200-row page. Correctness beats the second axis.
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
                    recorder=recorder,
                    detector=detector,
                    deadline=deadline,
                    governor=governor,
                )

        # ---- the LLM stage (owner decision 2026-09-01) -------------------
        # Chained HERE, not in the worker handler, for three reasons: it
        # needs this run's remaining `deadline`, its numbers belong in this
        # run's report and `extraction_runs` row, and it can only run over
        # documents this crawl has already ingested and indexed. Off unless
        # `extraction.facts.enabled` — `maybe_run_after_crawl` returns None
        # and this is a no-op. A hard stop inside it (no credential, model
        # unreachable, no ontology) propagates into the handler below and
        # records the run as FAILED, exactly like any other crash: the
        # crawl's own work is already durable, and the facts pass resumes
        # from its per-document state next run.
        facts_report = maybe_run_facts_extraction(connection, deadline=deadline)
    except BaseException as exc:
        # A crashed — or deliberately stopped — run still owes the operator
        # its numbers and its state: the rows are already ingested, so record
        # what got done instead of losing the pass. A timeout and a throttle
        # abort are not crashes, so each is NAMED in the report rather than
        # left looking like one (see `_STOP_REASONS`).
        reason = _stop_reason(exc)
        interrupted_report = stats.report(max_file_mb=max_file_mb, interrupted=True, interrupted_reason=reason)
        interrupted_report["connection_id"] = connection_id
        interrupted_report["scope_errors"] = scope_errors
        state["last_run"] = interrupted_report
        save_state(connection_id, state)
        if reason != "error":
            logger.warning(
                "sharepoint crawl: connection %s stopped early (%s) after %d new / %d changed — "
                "state saved, the next run resumes",
                connection_id,
                reason,
                stats.new,
                stats.changed,
            )
        # Severity-first: a timeout is the sanctioned stop (records as
        # `interrupted`, reason "timeout"); anything else records via
        # `_record_status_for` so a crash can never dress up as benign.
        ner_usage = _detector_usage(detector)
        if ner_usage:
            interrupted_report["ner_usage"] = ner_usage
        ocr_usage = _ocr_run_usage(_scan_ocr)
        if ocr_usage:
            interrupted_report["ocr_usage"] = ocr_usage
        stopped_usage: Dict[str, Any] = {}
        if ner_usage:
            stopped_usage["ner"] = ner_usage
        if ocr_usage:
            stopped_usage["ocr"] = ocr_usage
        recorder.finish(
            stats,
            status="interrupted" if reason in {r for _, r in _STOP_REASONS} else _record_status_for(exc),
            report=interrupted_report,
            error=f"{type(exc).__name__}: {exc}",
            usage=stopped_usage,
        )
        raise

    report = stats.report(max_file_mb=max_file_mb)
    report["connection_id"] = connection_id
    report["scope_errors"] = scope_errors
    # Both LLM stages' numbers ride in the SAME report and the SAME
    # `extraction_runs` row: one run, one set of counters. `ner_usage` and
    # `facts_usage` are promoted to the top level (next to the crawl's own
    # totals) because they are what the cost surfaces read; the facts
    # sub-report stays nested so the crawl report's existing contract is
    # unchanged. The run row's `usage` is keyed by stage; `{}` still means
    # "no tokens spent" — a different claim from "$0.00" — whenever a
    # stage is off or the regex tier answered.
    ner_usage = _detector_usage(detector)
    if ner_usage:
        report["ner_usage"] = ner_usage
    ocr_usage = _ocr_run_usage(_scan_ocr)
    if ocr_usage:
        report["ocr_usage"] = ocr_usage
    facts_usage: Dict[str, Any] = {}
    if facts_report is not None:
        report["facts"] = facts_report
        facts_usage = facts_report.get("facts_usage") or {}
        report["facts_usage"] = facts_usage
    state["last_run"] = report
    save_state(connection_id, state)
    run_usage: Dict[str, Any] = {}
    if ner_usage:
        run_usage["ner"] = ner_usage
    if ocr_usage:
        run_usage["ocr"] = ocr_usage
    if facts_usage:
        run_usage["facts"] = facts_usage
    recorder.finish(stats, status="done", report=report, usage=run_usage)
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
    """Entry point for the ``corpus-extraction`` job kind
    (``app/worker/kinds.py::_run_corpus_extraction``, a thin delegate to
    this).

    Bounded by ``extraction.timeout_s`` — v1's ONLY deliberate stop mechanism
    (no UI cancel, and since the external producer was removed, no subprocess
    to kill). On expiry the run saves its state, reports
    ``interrupted_reason="timeout"``, and fails the job; the next run resumes
    from the persisted deltaLinks and cTags. ``payload["timeout_s"]``
    overrides the configured value for one run (0 = unbounded).

    An exhausted 429 budget stops a run the same way, reporting
    ``interrupted_reason="throttled"`` — see :data:`_STOP_REASONS` for why
    those two, and only those two, license a caller to tell the operator the
    next run picks up where this one stopped.

    ``payload``: ``connection_id`` (required — a ``source_connections`` row
    with ``source_type='sharepoint'``) and optionally ``scopes`` (a list of
    ``source_scope_id``s to narrow the run to; every confirmed scope
    otherwise) and ``concurrency`` (this run's in-page item concurrency,
    overriding ``extraction.crawler.concurrency``, clamped to
    ``[1, 16]`` — the same one-run override shape ``timeout_s`` has; the
    report's ``concurrency`` block names the effective value and its source).
    Credentials are resolved from the row, never from the payload.

    Returns the crawl report — the same dict persisted as ``last_run`` in
    this connection's crawl state, so the job result and the state file can
    never disagree about what a run did.

    An optional ``job_id`` in the payload is recorded on the run row
    (``extraction_runs.job_id``) so the card can join a run to its job's
    lifecycle. Nothing supplies it today — the worker hands this handler the
    job's PAYLOAD, not its id — so the column is honestly null rather than
    guessed, and the liveness check falls back to checkpoint age.
    """
    connection_id = payload.get("connection_id")
    if not connection_id:
        raise CrawlError("sharepoint crawl: payload missing connection_id")

    from src.repositories import source_connections_repo

    connection = source_connections_repo().get(connection_id)
    if connection is None or connection.get("source_type") != "sharepoint":
        raise CrawlError(f"sharepoint crawl: connection {connection_id!r} not found or not a sharepoint connection")

    try:
        return asyncio.run(
            _run_crawl_async(
                connection,
                only_scope_ids=payload.get("scopes"),
                job_id=payload.get("job_id"),
                timeout_s=payload.get("timeout_s"),
                concurrency=payload.get("concurrency"),
            )
        )
    except SharePointSettingsError as exc:
        # Named cause, not a bare traceback — the same typed handling
        # `app/api/admin_sharepoint.py::_resolved_token` gives this error.
        raise CrawlError(f"sharepoint crawl: {exc}") from exc
