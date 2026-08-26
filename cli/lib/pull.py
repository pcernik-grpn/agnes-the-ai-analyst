"""`run_pull` — pure data-refresh primitive lifted from `cli/commands/sync.py`.

Pulls the RBAC-filtered manifest from the server, downloads parquets whose
MD5 hash differs from local state, rebuilds DuckDB views, and syncs the
corporate memory bundle to `<workspace>/.claude/rules/km_*.md`.

Contract — Task 8:
- Pure function: no Typer, no stdout, no `sys.exit`. Caller decides what to print.
- Returns a `PullResult` dataclass.
- `dry_run=True` -> no disk writes anywhere (no DB file, no parquet dir,
  no rules dir, no sync_state).
- Lazy mkdir: `server/parquet/` is created inside the per-table loop on
  first write; `.claude/rules/` is only created when the bundle has at
  least one mandatory item or non-empty approved list. Empty inputs leave
  the workspace tree alone.
- The DuckDB file at `<workspace>/user/duckdb/analytics.duckdb` is the
  load-bearing artifact for every downstream reader (CLI query, hooks),
  so it gets created even with zero parquets.

The api_get/stream_download helpers in `cli/client.py` read server URL and
token from `cli.config` (via the `AGNES_SERVER` and `AGNES_TOKEN` env
overrides). To keep `run_pull` callable with explicit `server_url` /
`token` arguments without rewriting the HTTP layer, this module sets those
env vars for the duration of the call and restores the prior values on
exit. That's the cheapest adapter that doesn't bleed into client.py.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import os
import re
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import httpx

from cli.client import api_get, api_post, stream_download
from cli.config import get_sync_state, save_sync_state
from cli.snapshot_meta import list_snapshots
from src.distribution import CONTENT_MD5_HEADER
from src.object_store import OBJECT_STORE_MD5_METADATA_HEADER
from src.sql_ident import quote_ident

logger = logging.getLogger(__name__)


@dataclass
class PullResult:
    """Outcome of a `run_pull` invocation.

    Fields:
    - `tables_updated`: count of parquets actually re-downloaded this run.
    - `tables_removed`: count of local `server/parquet/<name>.parquet` files
      pruned this run because the table left the authorized typed (v49)
      stack. Always 0 against a pre-v49 server that emits no typed sections.
    - `parquets_total`: count of non-remote tables visible in the manifest.
    - `materialized_skipped`: how many `query_mode='materialized'` rows this
      run left alone because `skip_materialize` was set. Deliberately NOT part
      of `parquets_total` — a caller reporting "N fetched of M" must not count
      rows it never attempted.
    - `rules_count`: number of `km_*.md` files written to `.claude/rules/`.
    - `knowledge_updated`: count of per-collection `user/knowledge/<corpus_id>.duckdb`
      artifacts actually re-downloaded this run (K3, #798).
    - `knowledge_removed`: count of local knowledge artifacts pruned this run
      because the corpus left the manifest's `knowledge_artifacts` section
      (de-authorization or corpus deletion). Always 0 against a pre-K3
      server that emits no `knowledge_artifacts` key.
    - `digests_updated`: count of maintained-digest `.claude/rules/ka_<slug>.md`
      files written this run (K4, #799) — new content, staleness flip, or
      first-time delivery.
    - `digests_removed`: count of `ka_<slug>.md` files pruned this run
      because the digest left the manifest's `knowledge_artifacts`
      `kind=="digest"` entries (de-authorization or digest deletion).
      Always 0 against a pre-K4 server that emits no `knowledge_artifacts`
      key at all.
    - `access_policy_tables`: count of access-policied tables named in
      `.claude/rules/access_policies.md` this run (table access policies
      §10 item 4) — 0 when the analyst's stack has none, or against a
      pre-this-feature server whose manifest carries no `data_packages[]
      .tables[].access_policy` marker at all.
    - `semantic_models_updated`: count of read-only cache files written
      under `<workspace>/semantic/<slug>/…` this run (Fáze 1 physical
      distribution — semantic-layer follow-up plan). 0 when the caller has
      no accessible `status='valid'` semantic model, or against a
      pre-this-feature server with no `/api/semantic-models/bundle` route.
    - `semantic_models_removed`: count of model directories pruned under
      `<workspace>/semantic/` this run because the model left the caller's
      accessible set (revoked grant, deleted model, source re-sync).
    - `duration_s`: wall time of the call.
    - `errors`: list of `{"table": ..., "error": ...}` (or
      `{"stage": "memory_bundle", "error": ...}` /
      `{"stage": "knowledge_artifacts", "corpus_id": ..., "error": ...}` /
      `{"stage": "knowledge_digests", "digest": ..., "error": ...}`) —
      best-effort flow, individual failures don't abort the whole pull.
    - `tables_via_signed_url`: of `tables_updated`, how many landed via the
      manifest's direct-to-object-storage `signed_url` (WF-4, wave 2H)
      rather than the app-served `/api/data/{tid}/download` route. Always
      0 against a manifest that never carries `signed_url` (no object
      store configured, or `distribution.signed_urls: off`).
    - `tables_via_app`: of `tables_updated`, how many landed via the
      app-served route — either because the manifest entry had no
      `signed_url`, or because the signed-URL attempt failed (network
      error, non-2xx, md5 mismatch, SSRF-guard rejection) and fell back.
    """

    tables_updated: int = 0
    tables_removed: int = 0
    parquets_total: int = 0
    materialized_skipped: int = 0
    rules_count: int = 0
    knowledge_updated: int = 0
    knowledge_removed: int = 0
    digests_updated: int = 0
    digests_removed: int = 0
    access_policy_tables: int = 0
    semantic_models_updated: int = 0
    semantic_models_removed: int = 0
    tables_via_signed_url: int = 0
    tables_via_app: int = 0
    duration_s: float = 0.0
    errors: list[dict] = field(default_factory=list)
    # #1129 review — snapshot view names withheld this run because the id
    # names a table the analyst is no longer authorized for (or that turned
    # server_only). Empty on every ordinary pull; non-empty is the audit
    # trail for a name that would otherwise have resolved to stale rows.
    snapshot_views_blocked: list[str] = field(default_factory=list)
    # v49 (Phase 7, Task 7.5) — per-type stack-sync result. Populated when
    # the manifest carries any of ``direct_tables`` / ``data_packages`` /
    # ``memory_domains``. Kept off the constructor signature (None default)
    # so older callers reading ``tables_updated`` keep compiling.
    stack_sync: object = None


# Knowledge corpus ids, digest slugs and memory item ids: no dots. Each is
# spliced into a request path (`/api/knowledge/artifacts/{cid}/download`) as well
# as a local filename, and none of them has a legitimate dotted spelling.
_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,128}$")

# Registered table ids DO have a legitimate dotted spelling: `app/api/admin.py`
# derives a table_id from an admin-supplied display name via
# `.strip().lower().replace(" ", "_")`, which preserves dots, so `orders.v2` is a
# real registered id. Kept separate from `_SAFE_ID_RE` rather than widening it —
# that regex is shared by three unrelated validation sites, and dot-tolerance
# there buys nothing while letting `..`-style values into a URL path segment.
# `_safe_manifest_tables` still rejects the path-meaningful dot spellings
# (`.`, `..`, leading dot) separately; the charset alone would admit them.
_SAFE_TABLE_ID_RE = re.compile(r"^[a-zA-Z0-9_.\-]{1,128}$")


def _safe_manifest_tables(raw: dict) -> tuple[dict, list[str]]:
    """Split a manifest's ``tables`` dict into (kept, dropped_ids).

    A table id from the manifest becomes BOTH a filesystem path segment
    (``<tid>.parquet`` and its ``.verify.tmp`` sidecar under
    ``<workspace>/server/parquet/``) and a DuckDB view identifier. This file
    already gates collection ids, doc slugs and item ids through
    their own charset regexes; the manifest table id was the one that was missed
    (2026-08-05 audit, F-4b).

    Honest severity: the server is the analyst's own authenticated Agnes
    instance, which already ships executable plugin content to this laptop via
    the marketplace bundle — so this narrows a trust boundary rather than
    closing an open door. It is a one-line gate either way.

    Returns dropped ids rather than raising or collecting into
    ``PullResult.errors``: ``cli/commands/pull.py`` turns a non-empty ``errors``
    list into ``typer.Exit(1)``, including on the ``--quiet`` SessionStart hook
    path, so one odd id would make every subsequent ``agnes pull`` fail.
    """
    kept: dict = {}
    dropped: list[str] = []
    for tid, info in (raw or {}).items():
        # `.`/`..`/leading-dot pass the charset check but are path-meaningful.
        if not isinstance(tid, str) or tid.startswith(".") or not _SAFE_TABLE_ID_RE.match(tid):
            dropped.append(tid)
            continue
        kept[tid] = info
    return kept, dropped


# #596 — hash-mismatch recovery in `_download_one`. A download whose bytes
# don't match the manifest hash is treated as transient (corrupt mid-flight
# transfer, a server-side parquet rewrite that raced the manifest read) and
# re-downloaded up to this many extra times before the table is recorded as
# a hard error. The prior good `<tid>.parquet` is preserved across the whole
# loop (download lands in a sidecar; only a verified sidecar is promoted), so
# even a persistent mismatch never leaves the table missing from disk.
_DOWNLOAD_RETRIES = 2
_DOWNLOAD_RETRY_BACKOFFS_S = (0.5, 1.0)


def _retry_backoff(attempt: int) -> float:
    """Seconds to wait before re-attempting a failed download. Clamps to the
    tail of the schedule so `_DOWNLOAD_RETRIES` can be raised (or monkeypatched
    in tests) without having to extend `_DOWNLOAD_RETRY_BACKOFFS_S` in step."""
    return _DOWNLOAD_RETRY_BACKOFFS_S[min(attempt, len(_DOWNLOAD_RETRY_BACKOFFS_S) - 1)]


# WF-4 (wave 2H) — direct-to-object-storage fetch of a manifest `signed_url`.
# Bounded connect/read timeouts so a stalled object-store endpoint doesn't
# hang the whole pull; the app-served fallback below has its own budget.
_SIGNED_URL_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=10.0)
# Chunk size matches `_file_md5`'s read chunking — no functional requirement
# (the md5 is computed over the whole file after it lands), just keeps the
# streaming discipline visibly consistent with the rest of this module.
_SIGNED_URL_CHUNK_BYTES = 8192


def _fetch_signed_url(url: str, target_path: str, progress_callback=None, headers_out: dict | None = None) -> None:
    """SSRF-guarded direct-to-object-storage download of a manifest
    `signed_url` (WF-4, wave 2H) into `target_path`.

    Raises on ANY failure — disallowed scheme, private/loopback/
    link-local/metadata-range IP, a redirect, a non-2xx response, or a
    transport error. `_download_one` treats any exception raised here as
    "fall back to the app-served `/api/data/{tid}/download` path"; this
    function never promotes a file itself — md5 verification against the
    manifest hash happens afterwards, unconditionally, via
    `_verify_and_promote`, on whichever path's bytes end up in the
    sidecar.

    `headers_out`, when given, is populated with the response headers on a
    successful (< 300) response — same shape as `cli/client.py::stream_download`'s
    own `headers_out` (clear-then-update with `httpx.Headers(response.headers)`,
    readers re-wrap in `httpx.Headers` for case-insensitive lookup). The two
    paths carry genuinely different headers — `stream_download`'s caller
    reads `X-Agnes-Content-MD5` (`src.distribution.CONTENT_MD5_HEADER`, an
    Agnes-defined header on the app-served part route), this one reads S3's
    own `x-amz-meta-md5` (`src.object_store.OBJECT_STORE_MD5_METADATA_HEADER`
    — whatever an object was stamped with by `S3ObjectStore.put_file`,
    echoed back on GET) — but the shape a caller reads them through is the
    same seam on purpose: `_download_one` uses it to tell "the object moved
    on since the manifest was built" from "the bytes were damaged in
    transit" when a signed-URL fetch fails `_verify_and_promote`, issue
    #1360. Not populated on a raised exception (SSRF rejection, transport
    error, non-2xx) — there is no response to read headers off.

    SSRF guard: reuses `_resolve_safe` *and* `_SSRFGuardTransport` from
    `src.marketplace_asset_mirror` — the same DNS-rebinding-aware
    scheme/host/private-IP check (plus IP-pinned connection) the
    curated-marketplace asset mirror already relies on — rather than
    hand-rolling a second implementation for a narrower case. Imported
    lazily (matching this module's other lazy imports, e.g.
    `_rebuild_duckdb_views`'s `src.duckdb_conn` import) so a plain `agnes
    pull` that never sees a `signed_url` in its manifest doesn't pay for
    the import.

    A prior version of this function ran the `_resolve_safe` pre-flight
    check and then connected with a plain `httpx.Client()`, which lets
    httpcore re-resolve the hostname at connect time — a compromised or
    malicious signed-URL host could resolve to a public IP for the
    pre-flight check and a private/metadata IP (e.g. `169.254.169.254`)
    for the actual connection (DNS rebinding), completely defeating the
    guard. `_SSRFGuardTransport.handle_request` closes that gap: it
    re-validates the URL, rewrites `request.url.host` to the *exact* IP
    `_resolve_safe` just resolved (so httpcore connects there directly,
    with no further hostname resolution possible), and stashes the
    original hostname in the `Host` header + the `sni_hostname` extension.
    That header/SNI preservation is load-bearing for presigned
    object-storage URLs specifically: an S3-style V4 signature is computed
    over a canonical request that includes the `Host` header, so if we
    connected to the IP *and* sent `Host: <ip>` the signature would no
    longer match what the server re-derives — preserving the original
    hostname in `Host` (while physically connecting to the pinned IP)
    keeps the presigned signature valid.

    Redirects are still refused outright (`follow_redirects=False` on this
    one-off `httpx.Client`, not the module's shared `follow_redirects=True`
    client) — a presigned object-storage GET URL is a single, final
    location by construction (the signature covers exactly the request it
    was issued for), so a 3xx response here means either a misconfigured
    store or something worth treating with suspicion; "refuse, fall back
    to the app path" is the right reaction either way. Because redirects
    are disabled, the transport only ever runs once per fetch — no
    redirect-hop revalidation loop is needed here, unlike the marketplace
    module's own multi-hop use of the same transport.

    Streams the body to `target_path` in `_SIGNED_URL_CHUNK_BYTES`-sized
    chunks — nothing is buffered fully in memory even for a multi-GB
    parquet.
    """
    from src.marketplace_asset_mirror import _resolve_safe, _SSRFGuardTransport, _SSRFRejected

    safe, reason, _ip = _resolve_safe(url)
    if not safe:
        raise ValueError(f"signed_url rejected: {reason}")

    with httpx.Client(
        transport=_SSRFGuardTransport(),
        timeout=_SIGNED_URL_TIMEOUT,
        follow_redirects=False,
    ) as client:
        try:
            with client.stream("GET", url) as resp:
                if resp.status_code >= 300:
                    raise ValueError(f"signed_url http_{resp.status_code}")
                if headers_out is not None:
                    headers_out.clear()
                    headers_out.update(httpx.Headers(resp.headers))
                with open(target_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=_SIGNED_URL_CHUNK_BYTES):
                        f.write(chunk)
                        if progress_callback and chunk:
                            progress_callback(len(chunk))
        except _SSRFRejected as e:
            raise ValueError(f"signed_url rejected: {e.reason}") from e


def _signed_url_mismatch_reason(headers: dict, expected_hash: str) -> str | None:
    """After a signed-URL fetch fails `_verify_and_promote`, explain WHY —
    purely diagnostic (issue #1360), never changes what `_download_one`
    does: a mismatch still falls back to the app-served route
    unconditionally regardless of this function's answer, and
    `_verify_and_promote` stays the only promotion gate either way.

    Reads `headers` (the `headers_out` `_fetch_signed_url` populated) for
    `OBJECT_STORE_MD5_METADATA_HEADER` — the object's own stamp, echoed
    back by S3 on a plain GET — the same "absent means an older/unlabeled
    store, say nothing" contract `cli/client.py::stream_download`'s
    `headers_out` + `CONTENT_MD5_HEADER` already established for the
    app-served part route: returns `None` when the header is missing, same
    as before this existed.

    When present, distinguishes the two things that otherwise look
    identical (both are just "hash mismatch" to `_verify_and_promote`):

    - the store's stamp disagrees with the manifest's `expected_hash` too
      -> the object moved on (a newer sync landed after the manifest was
      built) — self-consistent, not corruption.
    - the store's stamp agrees with `expected_hash` -> the object was
      exactly what was expected, yet what arrived did not hash to it —
      the bytes were damaged in transit.
    """
    served = httpx.Headers(headers).get(OBJECT_STORE_MD5_METADATA_HEADER, "")
    if not served:
        return None
    if served != expected_hash:
        return (
            f"object moved on: the store holds {served[:12]}, the manifest expected "
            f"{expected_hash[:12]} — a newer sync likely landed after the manifest was built"
        )
    return (
        f"possible transfer corruption: the store's stamp ({served[:12]}) matches the "
        "manifest, but the downloaded bytes did not hash to it"
    )


def _verify_and_promote(sidecar: Path, target: Path, expected_hash: str) -> tuple[bool, str | None]:
    """md5-verify `sidecar` against `expected_hash` and atomically promote
    it to `target` on success. Returns `(promoted, error)`.

    Shared tail of both download paths (WF-4, wave 2H) so verify/promote
    semantics are identical regardless of whether the bytes came from the
    manifest's `signed_url` or the app-served route — only the byte
    source differs between callers.

    On a hash-less legacy manifest (`expected_hash` empty), falls back to
    the structural PAR1 check. On failure the sidecar is removed and
    `target` is left untouched — the prior good parquet, if any, survives
    a failed refresh; the caller decides whether to retry or fall back.
    """
    if expected_hash:
        actual_hash = _file_md5(sidecar)
        if actual_hash != expected_hash:
            err = f"hash mismatch: expected {expected_hash[:12]}, got {actual_hash[:12]}"
            sidecar.unlink(missing_ok=True)
            return False, err
    elif not _is_valid_parquet(sidecar):
        sidecar.unlink(missing_ok=True)
        return False, "not a valid parquet (missing PAR1 magic)"
    os.replace(sidecar, target)
    return True, None


def _read_progress_interval_seconds() -> float:
    """Seconds between forced progress emissions per file. Default 5 s.

    Tighter cadence than the original 30 s default keeps non-TTY consumers
    (Claude Code sub-agent watchdogs, CI runners) from killing the process
    on apparent silence during a slow chunk. Override via
    `AGNES_PULL_PROGRESS_INTERVAL_SECONDS`. Issue #203.
    """
    raw = os.environ.get("AGNES_PULL_PROGRESS_INTERVAL_SECONDS", "")
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return 5.0


def _read_progress_interval_bytes() -> int:
    """Bytes between forced progress emissions per file. Default 1 MiB.

    Complements the time-based cadence so fast downloads also emit at a
    reasonable rate (the original "every 10% of total" boundary went
    unobserved on multi-GB parquets where 10% is tens of seconds of bytes).
    Override via `AGNES_PULL_PROGRESS_INTERVAL_BYTES`. Issue #203.
    """
    raw = os.environ.get("AGNES_PULL_PROGRESS_INTERVAL_BYTES", "")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return 1024 * 1024


class _TextualProgress:
    """Plain-text progress emitter for non-TTY stderr.

    When `agnes pull` is invoked from a Claude Code SessionStart hook,
    a CI runner, or any pipe consumer, stderr is not a terminal. Rich's
    progress bar in that mode either suppresses output (silent for
    minutes on a multi-GB parquet) or emits raw ANSI noise. This class
    instead emits one terse line per file at sensible cadence.

    Cadence policy: emit when *any* of:
      - per-file bytes-downloaded crosses a 10%-of-total boundary, OR
      - more than ``AGNES_PULL_PROGRESS_INTERVAL_BYTES`` bytes (default
        1 MiB) since this file's last emission, OR
      - more than ``AGNES_PULL_PROGRESS_INTERVAL_SECONDS`` (default 5 s)
        since this file's last emission.

    The byte+second floor exists because sub-agent / CI watchdogs read
    "no output for N seconds" as a hung process and kill it (issue #203);
    the original 30 s / 10% policy was silent enough to trip those gates
    on slow links.

    Always emits one final "done" line per file via `finish()` so the
    operator sees a confirmed completion even on tiny files.

    Format: `[N/T files] <tid>: 25% (16 MB / 66 MB) at 1.5 MB/s` — the
    "[N/T files]" prefix lets the operator see overall pull progress
    in a multi-table run without buffering all per-file lines.

    Thread-safe — `advance` is called from the chunked-download worker
    threads; an internal lock serializes the update + emit.
    """

    _HUMAN_UNITS = (
        (1024 * 1024 * 1024 * 1024, "TB"),
        (1024 * 1024 * 1024, "GB"),
        (1024 * 1024, "MB"),
        (1024, "KB"),
    )

    def __init__(self, *, stream, total_files: int, file_sizes: dict[str, int]):
        import threading

        self._stream = stream
        self._total_files = total_files
        self._file_sizes = file_sizes
        self._lock = threading.Lock()
        self._interval_seconds = _read_progress_interval_seconds()
        self._interval_bytes = _read_progress_interval_bytes()
        # Per-file state.
        self._bytes: dict[str, int] = {tid: 0 for tid in file_sizes}
        self._started_at: dict[str, float] = {}
        self._last_emit_at: dict[str, float] = {}
        self._last_emit_pct: dict[str, int] = {}
        self._last_emit_bytes: dict[str, int] = {}
        self._finished_idx: int = 0  # files whose `finish` line has been emitted
        # Files the CALLER knows did not land. The byte counter cannot see
        # this: on a persistent hash mismatch every byte arrives, so the
        # counter reaches the manifest size and the finalizer would print
        # "100% done" for a table that was never promoted to disk — the same
        # green-line-for-a-failure this class exists to stop, on what is in
        # fact the most common download failure. (Devin Review.)
        self._failed: dict[str, str] = {}

    def advance(self, tid: str, n: int) -> None:
        """Add `n` bytes to the file's total. Emit a textual update if
        the cadence policy allows."""
        with self._lock:
            now = time.monotonic()
            if tid not in self._started_at:
                self._started_at[tid] = now
                self._last_emit_at[tid] = now
                self._last_emit_pct[tid] = 0
                self._last_emit_bytes[tid] = 0
            self._bytes[tid] = self._bytes.get(tid, 0) + n

            total = self._file_sizes.get(tid, 0)
            current = self._bytes[tid]
            pct = int((current * 100) / total) if total > 0 else 0
            elapsed = now - self._last_emit_at[tid]
            bytes_since_emit = current - self._last_emit_bytes.get(tid, 0)
            crossed_10 = pct >= self._last_emit_pct[tid] + 10
            if crossed_10 or elapsed >= self._interval_seconds or bytes_since_emit >= self._interval_bytes:
                self._last_emit_at[tid] = now
                self._last_emit_pct[tid] = pct - (pct % 10)
                self._last_emit_bytes[tid] = current
                self._emit_line(tid, current, total, now)

    def reset(self, tid: str) -> None:
        """Zero a file's progress before a retry attempt. Without this the
        retry's bytes stack on top of the failed attempt's and the display
        inflates past the file's total (e.g. "200.0 MB / 100.0 MB")."""
        with self._lock:
            self._bytes[tid] = 0
            self._started_at.pop(tid, None)
            self._last_emit_at.pop(tid, None)
            self._last_emit_pct.pop(tid, None)
            self._last_emit_bytes.pop(tid, None)

    def fail(self, tid: str, reason: str = "") -> None:
        """Record that this file did not land, whatever the byte count says."""
        with self._lock:
            self._failed[tid] = reason or "download failed"

    def finish(self) -> None:
        """Emit a final `done` line for any file we never closed out."""
        with self._lock:
            now = time.monotonic()
            for tid, total in self._file_sizes.items():
                # Treat any file we observed bytes for as needing a
                # final line. Files that errored out before any callback
                # are still announced (operator wants visibility even on
                # zero-byte attempts).
                self._finished_idx += 1
                bytes_ = self._bytes.get(tid, 0)
                started = self._started_at.get(tid, now)
                duration = max(0.001, now - started)
                rate = bytes_ / duration
                # A file we never received all the bytes for did NOT finish.
                # This line used to read "100% done (0 B in 0.0s, 0 B/s)" for a
                # download that had 403'd — a green completion line for a
                # failure, with the real error buried further down. The
                # progress printer can't see the exception, but it can see that
                # the transfer fell short of the manifest size, which is enough
                # to stop claiming success.
                #
                # Two shapes of "did not finish", because the manifest does
                # not always carry a size: `total` is 0 for a row the server
                # reported without one, and `total and …` is inert there — so
                # the guard above skipped exactly the files it could say the
                # least about, and they kept printing "100% done (0 B in
                # 0.0s)". Receiving nothing at all is not a completed download
                # under any size, known or not. (Devin Review on this PR.)
                #
                # Wording differs between the two because the CONFIDENCE does.
                # Nothing received is unambiguous whatever the manifest says.
                # A short-but-nonzero transfer is inferred from `size_bytes`,
                # and this file already documents that the manifest and the
                # streamed length can disagree — observed in the over-count
                # direction ("174%" lines, compressed vs decompressed). Should
                # it ever disagree the other way, a hash-verified parquet that
                # promoted fine would print a hard "FAILED … see the error
                # below" with no error below it and an exit code of 0 — a lie
                # in the opposite direction to the one this guard removes. So
                # the short case reports what was observed and points at the
                # error rather than asserting one exists. (Devin Review.)
                if tid in self._failed:
                    # The caller's verdict beats the byte count — see `fail`.
                    line = (
                        f"[{self._finished_idx}/{self._total_files} files] "
                        f"{tid}: FAILED ({self._failed[tid]}"
                        f" after {self._fmt_bytes(bytes_)} in {duration:.1f}s)"
                        f" — see the error below\n"
                    )
                elif not bytes_:
                    # Checked FIRST, and phrased as a hard failure: nothing
                    # arrived, which is unambiguous whether or not a size was
                    # declared. (Ordered after the short-transfer branch it
                    # would have been swallowed by it for any file that DID
                    # declare a size — i.e. almost all of them.)
                    line = (
                        f"[{self._finished_idx}/{self._total_files} files] "
                        f"{tid}: FAILED (no data received"
                        f"{'' if total else ', expected size unknown'}"
                        f" in {duration:.1f}s) — see the error below\n"
                    )
                elif total and bytes_ < total:
                    line = (
                        f"[{self._finished_idx}/{self._total_files} files] "
                        f"{tid}: INCOMPLETE "
                        f"({self._fmt_bytes(bytes_)} of {self._fmt_bytes(total)} "
                        f"in {duration:.1f}s) — check for an error below\n"
                    )
                else:
                    line = (
                        f"[{self._finished_idx}/{self._total_files} files] "
                        f"{tid}: 100% done "
                        f"({self._fmt_bytes(bytes_)} in {duration:.1f}s, "
                        f"{self._fmt_bytes(int(rate))}/s)\n"
                    )
                self._stream.write(line)
            try:
                self._stream.flush()
            except Exception:
                pass

    def _emit_line(self, tid: str, current: int, total: int, now: float) -> None:
        started = self._started_at.get(tid, now)
        duration = max(0.001, now - started)
        rate = current / duration
        if total > 0:
            # Clamp displayed percentage to [0, 100]. When `current`
            # exceeds the advertised `total` (range/chunked transfer
            # over-counts, manifest size is compressed vs response is
            # decompressed, server retransmits a chunk, etc.) the raw
            # percentage would creep past 100% and snap back at
            # `finish()`, which surfaced in 2026-05-12 sub-agent perf
            # tests as confusing "174%" lines. Issue #258.
            raw_pct = int((current * 100) / total)
            pct_display = min(raw_pct, 100)
            pct_str = f"{pct_display}%"
            size_str = f"({self._fmt_bytes(current)} / {self._fmt_bytes(total)})"
        else:
            pct_str = "?"
            size_str = f"({self._fmt_bytes(current)})"
        idx = self._finished_idx + 1  # 1-based "currently working on file N"
        line = f"[{idx}/{self._total_files} files] {tid}: {pct_str} {size_str} at {self._fmt_bytes(int(rate))}/s\n"
        self._stream.write(line)
        try:
            self._stream.flush()
        except Exception:
            pass

    @classmethod
    def _fmt_bytes(cls, n: int) -> str:
        for divisor, suffix in cls._HUMAN_UNITS:
            if n >= divisor:
                return f"{n / divisor:.1f} {suffix}"
        return f"{n} B"


@contextmanager
def _override_server_env(server_url: str, token: str) -> Iterator[None]:
    """Set AGNES_SERVER + scoped token override for the duration of the call.

    `cli.config.get_server_url` honors `AGNES_SERVER`, so the server URL is
    swapped via env-var. The TOKEN override is routed through
    `cli.config._with_token_override` (a ContextVar), which is checked by
    `get_token()` BEFORE the on-disk `~/.config/agnes/token.json`. This is
    load-bearing: `agnes init --token NEW` runs the verify call in step 2
    while the file still holds an OLD token from a prior install — without
    the override, the verify uses the stale on-disk token and fails 401.

    `AGNES_TOKEN` env var is also set as a back-compat hint for any code
    path that bypasses `get_token()` (none in `cli/` at last audit, but
    third-party hooks may), but the contextvar is the authoritative source.

    Restores prior values on exit so the caller's environment isn't
    mutated permanently. Not safe for concurrent invocation across threads;
    single-threaded use only.
    """
    from cli.config import _with_token_override

    prev_server = os.environ.get("AGNES_SERVER")
    prev_token = os.environ.get("AGNES_TOKEN")
    os.environ["AGNES_SERVER"] = server_url
    if token:
        os.environ["AGNES_TOKEN"] = token
    try:
        with _with_token_override(token):
            yield
    finally:
        if prev_server is None:
            os.environ.pop("AGNES_SERVER", None)
        else:
            os.environ["AGNES_SERVER"] = prev_server
        if prev_token is None:
            os.environ.pop("AGNES_TOKEN", None)
        else:
            os.environ["AGNES_TOKEN"] = prev_token


def _diff_parts(server_parts: list[dict], local_parts: dict, table_dir: Path) -> tuple[list[dict], set[str]]:
    """Compute ``(fetch, prune)`` for a partitioned table.

    ``fetch`` = server part dicts whose local hash differs OR whose file is
    missing on disk (a matching local hash is NOT proof the file is present —
    same existence-guard rationale as the single-file path). ``prune`` =
    relpaths present locally (on disk or in prior state) that the server no
    longer lists.
    """
    server_by_path = {p["path"]: p for p in server_parts}
    fetch = [
        p for path, p in server_by_path.items() if local_parts.get(path) != p["hash"] or not (table_dir / path).exists()
    ]
    on_disk: set[str] = set()
    if table_dir.is_dir():
        for f in table_dir.rglob("*.parquet"):
            on_disk.add(f.relative_to(table_dir).as_posix())
    prune = (on_disk | set(local_parts)) - set(server_by_path)
    return fetch, prune


def _drop_stale_layout(parquet_dir: Path, tid: str, *, partitioned: bool) -> None:
    """Remove the local copy of the OTHER storage layout after a table
    switches single-file <-> partitioned on the server.

    Without this, both ``{tid}.parquet`` (single-file) and ``{tid}/`` (parts)
    can coexist locally; the view rebuild would then build a view from
    whichever it iterates first and could serve the abandoned layout's stale
    rows. Called after a successful sync in each direction.
    """
    if partitioned:
        # Now a directory of parts → drop the stale single-file copy.
        (parquet_dir / f"{tid}.parquet").unlink(missing_ok=True)
    else:
        # Now a single file → drop the stale parts directory.
        stale_dir = parquet_dir / tid
        if stale_dir.is_dir():
            shutil.rmtree(stale_dir, ignore_errors=True)


def _fetch_part_via_download(table_id: str, relpath: str, dest: Path) -> str:
    """Fetch one part; return the md5 the SERVER says it sent, or "".

    Empty when the response carries no `X-Agnes-Content-MD5` — an older server,
    or the chunked transport, which splices N range responses so no single one
    describes the whole file. The caller falls back to the manifest hash there,
    exactly as before this header existed.

    Module-level (not a closure in `run_pull`) so tests can exercise the header
    seam directly — the test file's banner carries the full why.
    """
    headers: dict = {}
    stream_download(
        f"/api/data/{table_id}/download?part={quote(relpath)}",
        str(dest),
        headers_out=headers,
    )
    return httpx.Headers(headers).get(CONTENT_MD5_HEADER, "")


def _fetch_part_with_retry(fetch_part, relpath: str, dest: Path, expected: str) -> str | None:
    """Fetch ONE part into ``dest`` and md5-verify it, retrying a bad fetch on
    the same bounded budget ``_download_one`` gives a single-file table
    (``_DOWNLOAD_RETRIES`` extra attempts, ``_DOWNLOAD_RETRY_BACKOFFS_S``
    between them). Returns ``None`` on success, else the last error string.

    ``expected`` (the manifest hash) is a CACHE KEY, not the integrity arbiter.
    The manifest is a snapshot taken at rebuild time and is fetched in a separate
    request from the parts, so on a dataset being rewritten in between the two can
    legitimately disagree — which is not corruption. When the server tags the
    response with ``X-Agnes-Content-MD5`` (the md5 of the bytes it actually sent,
    hashed from the same open descriptor it streamed) and the received bytes match
    THAT, the transfer was perfect and the manifest was merely stale, so the part is
    accepted instead of failing the whole table.

    The caller still records the MANIFEST hash for this part, not the served one.
    That record is compared only against the next manifest (`_diff_parts`), so it
    is answering "which published version have I reconciled against" — recording
    the served hash instead would make every subsequent pull see local != server
    and re-fetch the same part until the server happened to rebuild.

    Exists for parity with `_download_one` (#596/#626): the partitioned path
    added later fetched each part exactly once, so one bad part aborted the
    whole table's sync.

    Both failure kinds retry, matching `_download_one`: a transport error and a
    hash mismatch are equally plausible symptoms of one flaky transfer, and the
    caller cannot tell them apart anyway.

    No empty-hash PAR1 fallback, unlike `_verify_and_promote`: the server
    derives every part hash from file content in `_hash_table_parts`, so an
    empty ``expected`` is a malformed manifest and should fail loudly rather
    than downgrade to a structural check. Verified parts are staged, not
    promoted — the all-or-nothing swap lives in the caller.

    Note this sits ON TOP of `stream_download`'s own transient-error retries,
    so a persistently failing part now costs up to (its attempts x these) round
    trips, serially, before the table gives up.

    Two consecutive reads yielding the SAME wrong hash stop the loop early: the
    server is serving stable bytes that simply are not the ones the manifest
    describes, and re-reading identical bytes a third time cannot change that.
    That early exit is gated on the server NOT having sent a content hash: with one,
    a stale manifest is no longer an error at all, and bytes that match neither the
    manifest nor the served hash are genuine corruption — which must not be reported
    as "the published hash is stale", and gets the full retry budget instead.
    """
    last_err: str | None = None
    prev_got: str | None = None
    for attempt in range(_DOWNLOAD_RETRIES + 1):
        try:
            served = fetch_part(relpath, dest)
            got = _file_md5(dest)
            if got == expected:
                return None
            if served and got == served:
                # The bytes are exactly what the server sent; only the manifest was
                # out of date. Not a transfer problem, so not an error.
                return None
            if not served and got == prev_got:
                # Deterministic: same bytes, twice. Not a transfer problem.
                dest.unlink(missing_ok=True)
                return (
                    f"part {relpath} hash mismatch: expected {expected[:12]}, got {got[:12]} "
                    f"— identical on {attempt + 1} reads, so the server's bytes are stable and "
                    f"the published hash is stale. A transfer retry cannot fix this; the source "
                    f"needs a rebuild to re-hash it."
                )
            prev_got = got
            # Truncated like `_verify_and_promote`'s message — same failure,
            # same shape, whichever layout the table happens to use.
            last_err = f"part {relpath} hash mismatch: expected {expected[:12]}, got {got[:12]}"
        except Exception as exc:
            # Transport errors keep the full budget: unlike a stable hash, a
            # second connection failure is not evidence the third will fail.
            last_err = f"part {relpath} fetch failed: {exc}"
        # Never leave a rejected part's bytes in staging: the next attempt must
        # not be able to verify a stale file if its fetch dies before writing.
        dest.unlink(missing_ok=True)
        if attempt < _DOWNLOAD_RETRIES:
            time.sleep(_retry_backoff(attempt))
    return last_err


def _sync_partitioned_table(
    tid: str,
    server_parts: list[dict],
    local_parts: dict,
    parquet_dir: Path,
    fetch_part,
    rollup_hash: str,
    rows: int = 0,
) -> tuple[dict | None, bool, str | None]:
    """Incrementally sync one partitioned table into ``parquet_dir/{tid}/``.

    Staged-then-swapped: changed parts are fetched into a staging dir and
    md5-verified there; only when EVERY fetched part verifies are they moved
    into the table dir (unchanged parts stay put) and server-dropped parts
    pruned. Each part is fetched through ``_fetch_part_with_retry``; only when
    a part fails every attempt does the table abort, moving nothing and leaving
    the prior table dir intact. The per-part moves themselves are not one
    atomic unit, so a
    process crash *during* the swap can leave a mix of old/new parts; that is
    self-healing — ``local_tables`` is only updated on success, so the next
    pull re-detects and re-syncs the affected parts.

    ``fetch_part(relpath, dest)`` fetches one part's bytes to ``dest`` (its
    parent dir already exists) — injected so the download transport is
    testable. Returns ``(local_entry, changed, None)`` or
    ``(None, False, error)``. ``changed`` is True only when at least one part
    was fetched or pruned — so a no-op sync is not over-counted as an update.
    """
    table_dir = parquet_dir / tid
    fetch, prune = _diff_parts(server_parts, local_parts, table_dir)
    staging = parquet_dir / f".staging-{tid}"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    try:
        staged: dict[str, Path] = {}
        for part in fetch:
            relpath, expected = part["path"], part["hash"]
            dest = staging / relpath
            dest.parent.mkdir(parents=True, exist_ok=True)
            err = _fetch_part_with_retry(fetch_part, relpath, dest, expected)
            if err is not None:
                return None, False, err
            staged[relpath] = dest
        # Every fetched part verified → promote atomically, then prune.
        for relpath, dest in staged.items():
            final = table_dir / relpath
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(dest, final)
        for relpath in prune:
            (table_dir / relpath).unlink(missing_ok=True)
        return (
            {
                "hash": rollup_hash,
                "parts": {p["path"]: p["hash"] for p in server_parts},
                "rows": rows,
                "size_bytes": sum(int(p.get("size_bytes") or 0) for p in server_parts),
            },
            bool(fetch or prune),
            None,
        )
    except Exception as exc:
        # Staging/promote/prune IO failures land here; per-part fetch errors do
        # not (`_fetch_part_with_retry` catches those to retry them, and returns
        # the last one). Either way the failure must be RETURNED as a per-table
        # error, not raised — otherwise one flaky partitioned table would abort
        # the whole pull and discard tables that already downloaded fine.
        # All-or-nothing still holds: nothing was promoted, the prior table dir
        # is intact.
        return None, False, f"partitioned sync failed: {exc}"
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def run_pull(
    server_url: str,
    token: str,
    workspace: Path,
    *,
    dry_run: bool = False,
    skip_materialize: bool = False,
    show_progress: bool = False,
) -> PullResult:
    """Refresh local parquets + corporate memory rules from the server.

    Mirrors the `_sync_quiet` flow in `cli/commands/sync.py`, minus all
    Typer/Rich UI. Returns a `PullResult` summary; never raises for
    network/server errors (records them under `errors` instead) so the
    caller can decide whether a partial pull is fatal.

    Args:
        skip_materialize: When True, omit `query_mode='materialized'`
            tables from the download set. Use for analysts who only
            care about `--remote` access on the workspace and don't
            want to wait on multi-GB scheduled-query parquets at first
            init. Pavel's #185 Phase 1: a 6.3 GB `order_economics`
            parquet kept first init silent for 44 minutes.
        show_progress: When True, render a per-file progress bar to
            stderr via Rich during the parallel download phase. Pass
            False from `--quiet` callers (SessionStart hooks).
    """
    started = time.monotonic()
    result = PullResult()
    workspace = Path(workspace)

    with _override_server_env(server_url, token):
        # 1. Fetch manifest. A failure here means we can't tell what to
        # download at all — record the error and bail out empty-handed.
        try:
            resp = api_get("/api/sync/manifest")
            resp.raise_for_status()
            manifest = resp.json()
        except Exception as exc:
            result.errors.append({"stage": "manifest", "error": str(exc)})
            result.duration_s = time.monotonic() - started
            return result

        server_tables, unsafe_tids = _safe_manifest_tables(manifest.get("tables", {}) or {})
        if unsafe_tids:
            # stderr, not result.errors: a non-empty errors list makes
            # `agnes pull` exit 1 (cli/commands/pull.py), including on the
            # --quiet SessionStart hook path. Visible, but not fatal.
            import sys as _sys

            print(
                f"warning: skipped {len(unsafe_tids)} table(s) with an unsafe id: "
                f"{', '.join(repr(t) for t in unsafe_tids[:5])}",
                file=_sys.stderr,
            )
        local_state = get_sync_state(workspace)
        local_tables = local_state.get("tables", {})
        # Which ids resolved LOCALLY as of the previous pull, captured before
        # the prune below mutates the dict. This is the only honest answer to
        # "could a snapshot under this name shadow real local rows?" — and it
        # has to be read here, because the prune pops exactly the entries we
        # need (#1129 review).
        previously_local = set(local_tables)

        # #506 — make the legacy flat `server/parquet/` tree obey the stack.
        #
        # `agnes query` reads <workspace>/user/duckdb/analytics.duckdb whose
        # views are rebuilt over <workspace>/server/parquet/*.parquet. The
        # legacy flat `manifest["tables"]` dict is gated server-side by
        # `can_access_table`, whose Admin short-circuit bypasses the stack —
        # so for an admin it over-lists every accessible table regardless of
        # subscription, and for everyone there is no prune on authorization
        # loss. The typed v49 sections (``data_packages[].tables[]`` +
        # ``direct_tables[]``) ARE stack-scoped via StackResolver, but
        # historically run_pull consumed only the flat dict. Net: removing a
        # data package dropped it from ``data_packages[]`` yet left its
        # parquet + DuckDB view locally queryable.
        #
        # When the manifest carries the query-table typed sections, the authorized
        # table-name set is the union of every typed entry's ``name`` field —
        # which equals the flat parquet stem == sync_state.table_id ==
        # registry name == _meta.table_name. We use that set both to (1) filter
        # the download set (kills admin over-listing without touching server
        # authz) and (2) prune already-downloaded parquets that left the stack.
        #
        # A pre-v49 server emits none of these keys → fall back to the flat
        # dict exactly as before (no filter, no prune). A typed-sections-present
        # but empty stack is a legitimate "subscribed to zero packages" state:
        # the authorized set is empty and ALL flat parquets are pruned, which is
        # the intended behavior (the server wraps each section builder in
        # try/except returning [] on error, and StackResolver returns [] only
        # for a genuinely empty stack — so an empty typed set is never an error
        # signal that would wrongly nuke the local tree).
        # Gate on the query-table typed sections only (``data_packages`` /
        # ``direct_tables``) — NOT ``memory_domains``. Memory domains carry no
        # query tables (no flat parquet), so a manifest that arrives with only
        # ``memory_domains`` (a partial or hand-crafted delivery) must NOT build
        # an empty authorized set and prune every local parquet. The end-of-run
        # stack-sync gate keeps ``memory_domains`` (see below) — that path
        # legitimately fires on memory domains alone.
        has_query_table_sections = any(k in manifest for k in ("direct_tables", "data_packages"))
        authorized_names: set[str] | None = None
        if has_query_table_sections:
            authorized_names = set()
            for pkg in manifest.get("data_packages", []) or []:
                for t in pkg.get("tables", []) or []:
                    name = t.get("name")
                    if name:
                        authorized_names.add(name)
            for t in manifest.get("direct_tables", []) or []:
                name = t.get("name")
                if name:
                    authorized_names.add(name)

        # 2. Compute the download set, skipping remote-mode tables (no
        # parquet on the server) and unchanged hashes.
        #
        # The parquet-existence check is load-bearing: a stale `sync_state.json`
        # entry (hash matches server) is NOT proof the file is on disk. The
        # file can disappear between runs — manual rm, disk corruption, an
        # operator nuking `server/parquet/` during cleanup, or (#1311) the
        # one-time legacy->workspace state migration seeding this workspace's
        # hash from another workspace's last write before either of them had
        # its own scoped state. Without the existence guard, `agnes pull`
        # would skip the download and the downstream DuckDB view rebuild
        # fails on a missing file. Hash-equal-but-file-missing → force
        # re-download.
        to_download: list[str] = []
        partitioned_tids: list[str] = []
        non_remote_total = 0
        materialized_skipped = 0
        parquet_dir = workspace / "server" / "parquet"
        for tid, info in server_tables.items():
            if info.get("query_mode") == "remote":
                continue
            # #506 — when typed sections are present, the stack is the unit of
            # access: never download a flat-dict table the typed stack omits
            # (admin god-mode over-list). Pre-v49 servers have
            # `authorized_names is None` → no filter.
            if authorized_names is not None and tid not in authorized_names:
                continue
            if skip_materialize and info.get("query_mode") == "materialized":
                # Operator opt-out for first-init. Materialized rows are
                # still discoverable via `agnes catalog` and queryable
                # the next time `agnes pull` runs without --skip-materialize.
                #
                # Counted separately, NOT into `non_remote_total`: that counter
                # feeds `parquets_total`, which means "non-remote tables this
                # run considered", and adding skipped rows to it would make the
                # X/Y summary claim they were fetched. The caller needs the two
                # apart to say what happened (`cli/commands/init.py`).
                #
                # Placement is load-bearing. This sits AFTER the stack filter
                # and excludes `server_only`, because the number is printed as
                # "re-run with --materialize to fetch these": a row outside the
                # analyst's stack, or one the server never distributes, would
                # not be fetched by that re-run either, so counting it sends
                # them after data they cannot have.
                if not info.get("server_only"):
                    materialized_skipped += 1
                continue
            non_remote_total += 1
            # #607 — server_only tables are kept fresh server-side and stay
            # queryable via `agnes query --remote`, but their parquet is NOT
            # distributed to laptops. Count them as listed (they're part of
            # parquets_total above, like a hash-unchanged row) but never add
            # them to the download set. Mirrors the remote-skip's
            # listed-but-not-downloaded behavior, except remote rows aren't
            # even counted (no server parquet exists at all); a server_only
            # row HAS a server parquet, we just don't ship it.
            #
            # This flag is now ALSO set per-user (not just via the registry's
            # global admin flag) by the auto-membership stack model: a table
            # in a granted-but-not-subscribed ``available`` data package is
            # authorized (listed in `authorized_names` above) but not yet
            # materialized, so the server OR's `server_only` into its flat
            # manifest entry for this caller (`app/api/sync.py:
            # _build_data_packages_section`). Subscribing (`agnes stack add`)
            # clears it on the next manifest fetch.
            if info.get("server_only"):
                continue
            # Partitioned tables (partitioned distribution) are a directory of
            # parts under parquet_dir/{tid}/, synced per-part below — NOT via
            # the single-file `_download_one` path (which fetches one
            # {tid}.parquet). Always attempt the sync; `_diff_parts` makes it a
            # no-op when every part is already current.
            if info.get("parts") is not None:
                partitioned_tids.append(tid)
                continue
            local_hash = local_tables.get(tid, {}).get("hash", "")
            server_hash = info.get("hash", "")
            target = parquet_dir / f"{tid}.parquet"
            if server_hash != local_hash or tid not in local_tables or not server_hash or not target.exists():
                to_download.append(tid)
        result.parquets_total = non_remote_total
        result.materialized_skipped = materialized_skipped

        # 3. Dry-run short-circuit — touch nothing on disk.
        if dry_run:
            result.tables_updated = 0  # by definition no writes happened
            result.duration_s = time.monotonic() - started
            return result

        # 4. Download parquets in parallel. Lazy mkdir: only create
        # server/parquet/ when we have at least one table to write into it.
        # Concurrency capped by `AGNES_PULL_PARALLELISM` (default 4) so a
        # registry of 50+ tables doesn't open 50+ TCP connections + saturate
        # the analyst's NIC; 4 matches typical home-broadband saturation
        # without over-subscribing the server's caddy file_server (each
        # request is a separate goroutine + sendfile, but the analyst's
        # downlink is the more frequent bottleneck). Set to 1 to restore
        # the pre-PR serial behavior for debug repro. The server-side
        # bypass-uvicorn fix (Caddy file_server) is the other half —
        # without it, parallel downloads would still queue on the single
        # uvicorn worker.
        if (to_download or partitioned_tids) and not parquet_dir.exists():
            parquet_dir.mkdir(parents=True, exist_ok=True)

        try:
            workers = max(1, int(os.environ.get("AGNES_PULL_PARALLELISM", "4")))
        except ValueError:
            workers = 4
        # Drop to serial when there's only one (or zero) tables — avoids
        # the executor + thread overhead for the common single-update case.
        workers = min(workers, len(to_download)) if to_download else 1

        # Optional progress reporting — two paths.
        #
        # 1. Rich progress bar: per-file bytes-streamed bar with speed +
        #    ETA. Rendered to stderr when stderr is a TTY. Aggregates
        #    across the parallel ThreadPoolExecutor workers and across
        #    chunked-download chunks (all chunks call the same callback
        #    advancing the same task).
        # 2. Textual fallback: when `show_progress=True` but stderr is
        #    NOT a TTY (Claude Code SessionStart hook, CI run, Docker
        #    log capture), Rich would either suppress the bar or emit
        #    raw control sequences. Instead we emit one plain-text line
        #    per file at most every 10% or 30 s — enough signal to know
        #    the pull isn't frozen on a multi-GB parquet, terse enough
        #    not to spam the consumer's log.
        #
        # Both paths receive the same per-file callback so the chunked-
        # download contract ("one file = one task, sum-of-chunks bytes")
        # is honored uniformly.
        import sys as _sys

        progress = None
        progress_tasks: dict[str, int] = {}
        textual = None
        use_textual_fallback = show_progress and to_download and not _sys.stderr.isatty()
        if show_progress and to_download and not use_textual_fallback:
            from rich.progress import (
                BarColumn,
                DownloadColumn,
                Progress,
                TextColumn,
                TimeRemainingColumn,
                TransferSpeedColumn,
            )

            progress = Progress(
                TextColumn("[bold]{task.fields[label]}[/]"),
                BarColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                transient=False,
            )
            progress.start()
            for tid in to_download:
                size = int(server_tables[tid].get("size_bytes") or 0)
                # Some manifest entries don't carry size — Rich shows
                # an indeterminate bar in that case.
                progress_tasks[tid] = progress.add_task(
                    "download",
                    label=tid,
                    total=size if size > 0 else None,
                )
        elif use_textual_fallback:
            textual = _TextualProgress(
                stream=_sys.stderr,
                total_files=len(to_download),
                file_sizes={tid: int(server_tables[tid].get("size_bytes") or 0) for tid in to_download},
            )

        def _download_one(tid: str) -> tuple[str, dict | None, str | None, str | None]:
            """Returns (tid, local_table_entry_or_None, error_or_None,
            source_or_None). ``source`` is ``"signed_url"`` when the
            parquet landed via the manifest's direct-to-object-storage
            ``signed_url`` (WF-4, wave 2H), ``"app"`` when it landed via
            the app-served route (the default, and the fallback whenever
            the signed URL is absent, unreachable, rejected by the SSRF
            guard, or md5-mismatches), and ``None`` when the table never
            landed at all. One bound thread per call; stream_download is
            sync I/O so a ThreadPoolExecutor (not asyncio) is the right
            tool. The progress callback is thread-safe — Rich's
            Progress.update and the textual fallback's lock both
            serialize internally.

            Durability contract (#596): the prior good `<tid>.parquet`
            (if any) is NEVER unlinked before a fresh download has
            verified. The download lands in a sidecar
            `<tid>.parquet.verify.tmp`, the hash (or, on a hash-less
            legacy manifest, the PAR1 structural check) is checked
            there, and only on success is the sidecar `os.replace`d into
            the final target — atomic, so a reader never sees a
            half-written or mismatched file. A hash mismatch is treated
            as transient: the download+verify is retried up to
            ``_DOWNLOAD_RETRIES`` times (small backoff between attempts)
            before giving up. On persistent failure the sidecar is
            removed, the OLD good parquet stays in place, and the table
            is recorded under ``result.errors`` — the table is never
            left missing from disk.

            WF-4 (wave 2H): when the manifest entry carries a
            ``signed_url``, a single direct-to-object-storage attempt
            runs first — no internal retry, since ANY failure (SSRF
            rejection, transport error, non-2xx, md5 mismatch) falls
            straight through to the app-served retry loop below, which
            remains the durability safety net. md5 verification against
            the manifest hash gates BOTH paths unconditionally via the
            shared ``_verify_and_promote`` helper — a signed-URL download
            that mismatches is never promoted, only ever falls back."""
            target = parquet_dir / f"{tid}.parquet"
            sidecar = parquet_dir / f"{tid}.parquet.verify.tmp"
            info = server_tables[tid]
            expected_hash = info.get("hash", "")
            signed_url = info.get("signed_url") or ""
            cb = None
            reset_progress = None
            fail_progress = None
            if progress is not None and tid in progress_tasks:
                task_id = progress_tasks[tid]

                def cb(n: int, _tid=tid, _task=task_id):
                    progress.update(_task, advance=n)

                def reset_progress(_task=task_id):
                    progress.update(_task, completed=0)
            elif textual is not None:

                def cb(n: int, _tid=tid):
                    textual.advance(_tid, n)

                def reset_progress(_tid=tid):
                    textual.reset(_tid)

                def fail_progress(reason: str, _tid=tid):
                    textual.fail(_tid, reason)

            def _entry() -> dict:
                return {
                    "hash": expected_hash,
                    "rows": info.get("rows", 0),
                    "size_bytes": info.get("size_bytes", 0),
                }

            try:
                if signed_url:
                    signed_url_headers: dict = {}
                    try:
                        _fetch_signed_url(
                            signed_url, str(sidecar), progress_callback=cb, headers_out=signed_url_headers
                        )
                        ok, _verify_err = _verify_and_promote(sidecar, target, expected_hash)
                        if ok:
                            return tid, _entry(), None, "signed_url"
                        # md5 mismatch (or, on a hash-less legacy manifest, a
                        # failed PAR1 check) — fall through to the app path.
                        # Diagnostic only (issue #1360): explain, don't decide —
                        # the fallback below runs unconditionally either way.
                        reason = _signed_url_mismatch_reason(signed_url_headers, expected_hash)
                        if reason:
                            logger.debug("signed-url fetch for %s fell back to the app path: %s", tid, reason)
                    except Exception:
                        sidecar.unlink(missing_ok=True)
                    if reset_progress is not None:
                        reset_progress()

                last_err: str | None = None
                for attempt in range(_DOWNLOAD_RETRIES + 1):
                    # A failed attempt already reported its bytes; zero the
                    # bar so the retry doesn't display 2x/3x the file size.
                    if attempt and reset_progress is not None:
                        reset_progress()
                    try:
                        # Download into a sidecar — the real target keeps
                        # the prior good bytes until verification passes.
                        stream_download(
                            f"/api/data/{tid}/download",
                            str(sidecar),
                            progress_callback=cb,
                        )
                        ok, verify_err = _verify_and_promote(sidecar, target, expected_hash)
                        if ok:
                            return tid, _entry(), None, "app"
                        last_err = verify_err
                        if attempt < _DOWNLOAD_RETRIES:
                            time.sleep(_DOWNLOAD_RETRY_BACKOFFS_S[min(attempt, len(_DOWNLOAD_RETRY_BACKOFFS_S) - 1)])
                            continue
                        # Persistent mismatch: prior good target (if any)
                        # is untouched; record + bail. Tell the progress
                        # printer too — every byte arrived, so its counter
                        # says "done" and only this call knows better.
                        if fail_progress is not None:
                            fail_progress(last_err or "integrity check failed")
                        return tid, None, last_err, None
                    except Exception as exc:
                        last_err = str(exc)
                        sidecar.unlink(missing_ok=True)
                        if attempt < _DOWNLOAD_RETRIES:
                            time.sleep(_DOWNLOAD_RETRY_BACKOFFS_S[min(attempt, len(_DOWNLOAD_RETRY_BACKOFFS_S) - 1)])
                            continue
                        if fail_progress is not None:
                            fail_progress(last_err or "download failed")
                        return tid, None, last_err, None
                # Loop exhausted without an explicit return (defensive).
                if fail_progress is not None:
                    fail_progress(last_err or "download failed")
                return tid, None, last_err or "download failed", None
            finally:
                sidecar.unlink(missing_ok=True)

        try:
            if workers <= 1:
                outcomes = [_download_one(tid) for tid in to_download]
            else:
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=workers) as ex:
                    outcomes = list(ex.map(_download_one, to_download))
        finally:
            if progress is not None:
                progress.stop()
            if textual is not None:
                textual.finish()

        for tid, entry, err, source in outcomes:
            if err is not None:
                result.errors.append({"table": tid, "error": err})
            else:
                local_tables[tid] = entry
                # Drop a stale parts dir if this table just switched
                # partitioned -> single-file.
                _drop_stale_layout(parquet_dir, tid, partitioned=False)
                result.tables_updated += 1
                if source == "signed_url":
                    result.tables_via_signed_url += 1
                elif source == "app":
                    result.tables_via_app += 1

        # 4a-parts. Partitioned tables — per-part incremental sync into
        # parquet_dir/{tid}/. Only changed parts are fetched; the swap is
        # all-or-nothing (a failed part leaves the prior dir intact, never a
        # silently-partial view); server-dropped parts are pruned.

        for tid in partitioned_tids:
            info = server_tables[tid]
            server_parts = info.get("parts") or []
            local_parts = (local_tables.get(tid) or {}).get("parts") or {}

            entry, changed, err = _sync_partitioned_table(
                tid,
                server_parts,
                local_parts,
                parquet_dir,
                functools.partial(_fetch_part_via_download, tid),
                info.get("hash", ""),
                rows=info.get("rows", 0),
            )
            if err is not None:
                result.errors.append({"table": tid, "error": err})
            else:
                local_tables[tid] = entry
                # Drop a stale single-file copy if this table just switched
                # single-file -> partitioned.
                _drop_stale_layout(parquet_dir, tid, partitioned=True)
                # Only count a real change — a no-op sync (every part already
                # current) must not inflate the "tables updated" summary.
                if changed:
                    result.tables_updated += 1
                    # Parts are fetched via the app-served `?part=` route, so
                    # keep the per-route breakdown summing to tables_updated.
                    result.tables_via_app += 1

        # 4b. #506 — prune local parquets that left the authorized typed
        # stack. Runs only when the manifest carries typed sections (else
        # ``authorized_names is None`` and this is a no-op — pre-v49 servers
        # are untouched). For any ``server/parquet/<stem>.parquet`` on disk
        # whose stem is not authorized, unlink the file and drop its
        # ``local_tables[stem]`` sync_state row. The unconditional view
        # rebuild in step 6 then drops the now-orphaned view automatically
        # (it DROPs all views, then recreates only from parquets still on
        # disk). Remote tables have no flat parquet so they're untouched;
        # materialized tables DO have a flat parquet and are pruned like any
        # other table when they leave the stack (intended). User-created BASE
        # TABLEs live in analytics.duckdb (not under server/parquet/) so they're
        # never pruned. Done before
        # save_sync_state so the dropped rows persist, and before
        # _rebuild_duckdb_views so the orphaned views disappear.
        # #607 (#630 review) — also prune parquets the manifest now marks
        # server_only: the table stays authorized (listed, RBAC intact) but
        # its parquet must leave the laptop, otherwise a copy downloaded
        # before the admin flipped the flag keeps a local view alive and the
        # table stays locally queryable despite server-only distribution.
        server_only_names = {tid for tid, info in server_tables.items() if info.get("server_only")}
        # #1129 review — names a snapshot view must NOT take. The prune above
        # makes a de-authorized or server_only id unresolvable by deleting its
        # parquet and letting the step 6 rebuild skip it. That leaves the name
        # free, and `_register_snapshot_views` would then hand it to
        # `user/snapshots/<table_id>.parquet` (what `agnes snapshot create`
        # writes with no `--as`), so `agnes query "... FROM <table_id>"` would
        # answer from stale snapshot rows instead of erroring. Not a new
        # disclosure — that parquet was downloaded while authorized, is on the
        # laptop either way, and `read_parquet` over its path never stopped
        # working — but it makes the sentence above ("the view rebuild would
        # resurrect it") true of the server copy and false of the name.
        #
        if parquet_dir.exists() and (authorized_names is not None or server_only_names):
            for pq_file in sorted(parquet_dir.glob("*.parquet")):
                stem = pq_file.stem
                authorized = authorized_names is None or stem in authorized_names
                if authorized and stem not in server_only_names:
                    continue
                pq_file.unlink(missing_ok=True)
                local_tables.pop(stem, None)
                result.tables_removed += 1
            # Same prune for partitioned tables, which live as a DIRECTORY of
            # parts (parquet_dir/{tid}/) rather than a top-level file — a
            # de-authorized or now-server_only partitioned table must have its
            # whole dir removed, else the view rebuild would resurrect it and
            # leak data the analyst no longer has access to.
            for tdir in sorted(p for p in parquet_dir.iterdir() if p.is_dir()):
                if tdir.name.startswith(".staging-"):
                    continue
                tid = tdir.name
                authorized = authorized_names is None or tid in authorized_names
                if authorized and tid not in server_only_names:
                    continue
                shutil.rmtree(tdir, ignore_errors=True)
                local_tables.pop(tid, None)
                result.tables_removed += 1

        # Computed AFTER the prune, so "still resolves locally" is the truth and
        # not a prediction — and persisted, because the signal it is derived
        # from does not survive: the prune pops the very sync_state rows that
        # say an id used to be local. A set recomputed from scratch each run
        # would withhold the name on the pull that removes the parquet and
        # release it on every pull after, which is how a snapshot ends up
        # answering for a table the analyst can no longer read (#1129 review).
        blocked_snapshot_names = _blocked_snapshot_names(
            server_tables,
            authorized_names,
            server_only_names,
            previously_local=previously_local,
            still_local=set(local_tables),
            remembered=set(local_state.get("snapshot_blocked") or []),
        )
        local_state["snapshot_blocked"] = sorted(blocked_snapshot_names)

        # 4c. K3 (#798) — knowledge artifacts: same download/verify/promote/
        # prune lifecycle as parquets, filtered by the manifest's own
        # collection-grant RBAC. Runs before save_sync_state so the
        # per-corpus md5s persist in the same on-disk state file.
        # Best-effort: a broken artifact channel must not fail the pull.
        try:
            _sync_knowledge_artifacts(manifest, workspace, local_state, result)
        except Exception as exc:
            result.errors.append({"stage": "knowledge_artifacts", "error": str(exc)})

        # 4d. K4 (#799) — maintained digests: writes/prunes
        # `.claude/rules/ka_<slug>.md`, the same delivery channel as the
        # corporate-memory `km_*.md` bundle. Runs before save_sync_state so
        # the per-digest md5s persist in the same on-disk state file.
        # Best-effort: a broken digest channel must not fail the pull.
        try:
            _sync_knowledge_digests(manifest, workspace, local_state, result)
        except Exception as exc:
            result.errors.append({"stage": "knowledge_digests", "error": str(exc)})

        # 5. Persist sync state (only on real runs). Workspace-scoped
        # (#1311) — `<workspace>/.claude/agnes/sync_state.json` — so two
        # workspaces on the same machine no longer share one hash record;
        # see `cli.config.get_sync_state`/`save_sync_state` for the
        # migration from the legacy machine-global file.
        local_state["tables"] = local_tables
        local_state["last_sync"] = datetime.now(UTC).isoformat()
        save_sync_state(local_state, workspace)

        # 6. Fetch corporate-memory bundle and lazily write
        # `.claude/rules/km_*.md`. Best-effort: a server outage on this
        # endpoint must not fail the whole pull.
        try:
            written = _fetch_and_write_rules(workspace)
            result.rules_count = written
        except Exception as exc:
            result.errors.append({"stage": "memory_bundle", "error": str(exc)})

        # 6b. Table access policies (§10 item 4): write/prune
        # `.claude/rules/access_policies.md` naming every policied table in
        # the analyst's stack -- the one link in the disclosure chain that
        # reaches an agent's context BEFORE it writes a query. Sourced from
        # the manifest already fetched in step 1, no extra round-trip.
        # Best-effort, same posture as the memory bundle above.
        try:
            result.access_policy_tables = _write_access_policy_rules(manifest, workspace)
        except Exception as exc:
            result.errors.append({"stage": "access_policy_rules", "error": str(exc)})

        # 6c. Semantic-layer physical cache (Fáze 1 — "distribuce jako
        # fyzická cache s TTL", semantic-layer follow-up plan): write/prune
        # <workspace>/semantic/<slug>/{_brief.md,tables/*.yml,metrics/*.yml,
        # glossary.md} for every RBAC-visible `status='valid'` semantic
        # model, each file read-only and hash+TTL-stamped in its header.
        # Best-effort, same posture as the memory bundle above — a server
        # outage or a pre-this-feature server (404) must not fail the pull.
        try:
            updated, removed = _sync_semantic_cache(workspace)
            result.semantic_models_updated = updated
            result.semantic_models_removed = removed
        except Exception as exc:
            result.errors.append({"stage": "semantic_cache", "error": str(exc)})

        # 7. v49 stack sync — per-type loop into ``<workspace>/.claude/data/``
        # and ``<workspace>/.claude/memory/`` with reference-counted dedup.
        # Runs only when the manifest carries the v49 fields (older servers /
        # backward-compat workspaces are untouched). Best-effort: failure
        # here records under ``result.errors`` but doesn't abort the rest of
        # the pull. MUST run before step 8's view rebuild (#1325): the
        # rebuild now also registers views over this tree, so a table synced
        # for the FIRST time by this very call needs its reference file on
        # disk before the rebuild walks it, or it stays unqueryable until
        # the next `agnes pull`.
        if any(k in manifest for k in ("direct_tables", "data_packages", "memory_domains")):
            try:
                result.stack_sync = _run_stack_sync_from_manifest(
                    manifest,
                    workspace,
                    skip_materialize=skip_materialize,
                    show_progress=show_progress,
                )
            except Exception as exc:
                result.errors.append({"stage": "stack_sync", "error": str(exc)})

        # 8. Rebuild DuckDB views — unconditional. The DB file is the
        # load-bearing artifact for downstream readers. Runs LAST (after
        # step 7) so a table the stack sync just fetched already has its
        # reference file on disk when `_rebuild_duckdb_views` walks
        # `.claude/data/` (#1325) — running this any earlier would leave a
        # freshly-subscribed table unqueryable for one whole pull cycle.
        #
        # Table access policies (§3.4, §10.3): a local snapshot whose
        # stored `policy_fingerprint` no longer matches the fingerprint the
        # manifest (fetched fresh in step 1) reports for its source table
        # RIGHT NOW must not keep serving pre-change rows — reuses this
        # SAME `blocked_names` mechanism #1129 built for a de-authorized or
        # newly-`server_only` table, so `snapshot_views_blocked` stays the
        # one audit trail for "why did this name stop resolving" regardless
        # of cause. Computed fresh every run (never merged into
        # `local_state["snapshot_blocked"]` above): unlike the de-auth
        # case, both comparison inputs (the snapshot's own meta.json, the
        # manifest) are already durable, so there is nothing to "remember"
        # across pulls — and a reverted policy's fingerprint matching again
        # correctly un-blocks the view on the very next pull.
        result.snapshot_views_blocked = _rebuild_duckdb_views(
            workspace,
            parquet_dir,
            blocked_names=blocked_snapshot_names | _stale_policy_snapshot_names(workspace, manifest),
        )

    result.duration_s = time.monotonic() - started

    # 9. Pull-confirm telemetry — fire-and-forget POST so the server can
    # close the loop on the ``sync.pull_started`` event from Phase 6.
    try:
        _emit_pull_confirm(server_url, token, result)
    except Exception:
        pass

    return result


def _run_stack_sync_from_manifest(
    manifest: dict,
    workspace: Path,
    *,
    skip_materialize: bool = False,
    show_progress: bool = False,
):
    """Build a ``pull_sync.PullStackOptions`` from the manifest payload
    and invoke ``run_stack_sync``. The local sync root is the
    ``<workspace>/.claude/`` dir so the stack-sync artifacts live next
    to the existing ``<workspace>/.claude/rules/`` / ``<workspace>/.claude/
    settings.json`` tree (workspace-scoped, not user-home, matching
    Section 5.3 of the spec for analyst workspaces).

    ``skip_materialize`` (#1304) mirrors the flag `run_pull` already
    honors in its step-4 flat-``tables`` download loop — threaded through
    to ``PullStackOptions`` so a ``query_mode='materialized'`` row in the
    typed ``data_packages``/``direct_tables`` sections is skipped here
    too, not just in the legacy dict.

    ``show_progress`` (#1308) gates a per-table stderr line emitted
    before/after each real fetch — same quiet-mode contract as step 4
    (``run_pull`` passes through the same value it derived from
    ``--quiet``/``--json``). Also wires `stream_download`'s
    `progress_callback` so the reported size reflects bytes actually
    transferred rather than only the manifest's declared size.
    """
    from cli.lib.pull_sync import PullStackOptions, run_stack_sync

    local_root = workspace / ".claude"

    # id -> declared size, purely for the human-readable progress line
    # below. Best-effort: falls back to "0 B" for a row with no
    # `size_bytes`, or when `target.stem` (the shared-store filename,
    # derived from `_safe_segment(table_id)`) doesn't line up with the
    # raw id here — display-only, never used as a lookup/cache key.
    sizes_by_id: dict[str, int] = {}
    for t in manifest.get("direct_tables") or []:
        tid = t.get("id")
        if tid:
            sizes_by_id[tid] = int(t.get("size_bytes") or 0)
    for pkg in manifest.get("data_packages") or []:
        for t in pkg.get("tables") or []:
            tid = t.get("id")
            if tid:
                sizes_by_id[tid] = int(t.get("size_bytes") or 0)

    def _fetcher(url: str, target: Path) -> None:
        import sys as _sys

        tid = target.stem
        if show_progress:
            size = sizes_by_id.get(tid, 0)
            label = f" ({_TextualProgress._fmt_bytes(size)})" if size else ""
            _sys.stderr.write(f"stack sync: fetching {tid}{label}...\n")
            _sys.stderr.flush()

        started = time.monotonic()
        downloaded = 0

        def _cb(n: int) -> None:
            nonlocal downloaded
            downloaded += n

        stream_download(url, str(target), progress_callback=_cb if show_progress else None)

        if show_progress:
            duration = max(0.001, time.monotonic() - started)
            _sys.stderr.write(
                f"stack sync: {tid} done ({_TextualProgress._fmt_bytes(downloaded)} in {duration:.1f}s)\n"
            )
            _sys.stderr.flush()

    def _bundle_fetcher(slug: str) -> bytes:
        resp = api_get("/api/memory/bundle", params={"domain": slug})
        resp.raise_for_status()
        return resp.content

    opts = PullStackOptions(
        manifest=manifest,
        local_dir=local_root,
        fetcher=_fetcher,
        md5_of=_file_md5,
        bundle_fetcher=_bundle_fetcher,
        skip_materialize=skip_materialize,
    )
    return run_stack_sync(opts)


def _emit_pull_confirm(server_url: str, token: str, result: PullResult) -> None:
    """POST /api/sync/pull-confirm with the per-type aggregate counts.

    Fire-and-forget — the parent already swallows exceptions but the
    helper has its own ``try/except`` so a 404 (older server without
    the endpoint) is silent rather than logged as a warning."""
    stack = result.stack_sync
    direct = getattr(stack, "direct_tables", None) if stack else None
    dp = getattr(stack, "data_packages", None) if stack else None
    md = getattr(stack, "memory_domains", None) if stack else None
    payload = {
        "duration_ms": int(result.duration_s * 1000),
        "direct_tables": {
            "added": getattr(direct, "added", 0),
            "updated": getattr(direct, "updated", 0),
            "removed": getattr(direct, "removed", 0),
        },
        "data_packages": {
            "added": getattr(dp, "added", 0),
            "updated": getattr(dp, "updated", 0),
            "removed": getattr(dp, "removed", 0),
        },
        "memory_domains": {
            "added": getattr(md, "added", 0),
            "updated": getattr(md, "updated", 0),
            "removed": getattr(md, "removed", 0),
        },
        "errors": len(result.errors),
    }
    try:
        api_post("/api/sync/pull-confirm", json=payload)
    except Exception:
        # Endpoint may not exist on older servers; silent skip.
        pass


# ---------------------------------------------------------------------------
# Helpers — copied verbatim from cli/commands/sync.py with the lazy-mkdir
# fix in `_fetch_and_write_rules`. Task 18 deletes sync.py; until then the
# two copies coexist (no behavior drift, copy not move).
# ---------------------------------------------------------------------------


def _file_md5(path: Path) -> str:
    """MD5 of a file, same chunking as app/api/sync.py:_file_hash so the
    client-side verification matches the manifest hash byte-for-byte."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _sync_knowledge_artifacts(manifest: dict, workspace: Path, local_state: dict, result: PullResult) -> None:
    """K3 (#798): download/verify/promote/prune per-collection knowledge.duckdb.

    Same lifecycle as parquets: sidecar download -> md5 verify -> os.replace
    promotion (a reader never sees a torn file; the prior good artifact
    survives a failed refresh), prune anything the manifest no longer lists
    (de-authorization / deleted corpus). Gate: the ``knowledge_artifacts``
    KEY must be present — a pre-K3 server that omits it must not nuke the
    local tree; a present-but-empty list is a legitimate zero-grants state
    and prunes everything (the #506 typed-sections posture).

    Simpler than the parquet loop by design: no retry-on-mismatch loop, no
    parallel download pool — artifacts are far smaller and less frequent
    than the parquet set. A persistent hash mismatch heals on the next
    pull; it is not silently ignored (recorded under ``result.errors``).
    """
    section = manifest.get("knowledge_artifacts")
    if section is None:
        return
    kdir = Path(workspace) / "user" / "knowledge"
    known = local_state.setdefault("knowledge_artifacts", {})
    listed: set[str] = set()
    for entry in section or []:
        # K4 (#799) — the same ``knowledge_artifacts`` list also carries
        # ``kind:"digest"`` entries (``_sync_knowledge_digests`` below).
        # Explicit gate rather than relying on the empty ``corpus_id`` read
        # below to fail ``_SAFE_ID_RE`` by accident.
        if entry.get("kind") not in (None, "chunks"):
            continue
        cid = entry.get("corpus_id") or ""
        md5 = entry.get("md5") or ""
        if not _SAFE_ID_RE.match(cid):
            continue
        listed.add(cid)
        target = kdir / f"{cid}.duckdb"
        if md5 and known.get(cid, {}).get("md5") == md5 and target.exists():
            continue  # hash-equal AND file present — same guard as parquets
        kdir.mkdir(parents=True, exist_ok=True)  # lazy mkdir
        sidecar = kdir / f"{cid}.duckdb.verify.tmp"
        try:
            stream_download(
                entry.get("url") or f"/api/knowledge/artifacts/{cid}/download",
                str(sidecar),
            )
            actual = _file_md5(sidecar)
            if md5 and actual != md5:
                raise ValueError(f"hash mismatch: expected {md5[:12]}, got {actual[:12]}")
            os.replace(sidecar, target)
            known[cid] = {"md5": md5, "size_bytes": entry.get("size_bytes", 0)}
            result.knowledge_updated += 1
        except Exception as exc:
            result.errors.append({"stage": "knowledge_artifacts", "corpus_id": cid, "error": str(exc)})
        finally:
            sidecar.unlink(missing_ok=True)
    if kdir.exists():
        for f in sorted(kdir.glob("*.duckdb")):
            if f.stem in listed:
                continue
            f.unlink(missing_ok=True)
            known.pop(f.stem, None)
            result.knowledge_removed += 1


#: Bump when `_digest_to_md`'s wrapper text changes, so already-synced
#: workspaces re-render once instead of keeping the old wording until the
#: digest's own content happens to change.
_DIGEST_RENDER_VERSION = 2


def _digest_to_md(body: dict) -> str:
    """Render one maintained-digest content response as `ka_<slug>.md`.

    Title h1 + a maintained-note (server-managed, don't edit) + a visible
    STALE banner blockquote when ``status == "stale"`` (the never-silent
    invariant, K4 #799) + the digest's ``output_md`` body.
    """
    slug = body.get("slug") or ""
    lines = [f"# {body.get('title') or slug}", ""]
    # Same channel as km_*.md, same reason for saying where this came from: a
    # digest is generated *from* source material, so any imperative in the body
    # belongs to that material rather than to this session.
    lines.append(
        f"_Maintained digest `ka_{slug}` — regenerated by Agnes from its source "
        f"material when that changes (last generated: {body.get('generated_at') or 'never'}). "
        "Server-managed; do not edit._"
    )
    if body.get("status") == "stale":
        lines += [
            "",
            f"> ⚠ **STALE** — {body.get('status_reason') or 'regeneration failed'}. "
            "Content below is the last successful generation.",
        ]
    lines += ["", body.get("output_md") or ""]
    return "\n".join(lines)


def _sync_knowledge_digests(manifest: dict, workspace: Path, local_state: dict, result: PullResult) -> None:
    """K4 (#799): write/prune maintained digests as `.claude/rules/ka_<slug>.md`.

    Same delivery channel as the corporate-memory `km_*.md` bundle — the
    digest is in the agent's context at session start. Gate: the
    `knowledge_artifacts` KEY must be present (a pre-K3/K4 server that omits
    it must not nuke the local tree); a present list with zero
    `kind=="digest"` entries prunes all `ka_*.md` (de-authorization or
    digest deletion) — the same #506 typed-sections posture the K3 chunk
    loop uses. The manifest `md5` is a change-token covering content AND
    staleness (`app.api.sync._digest_entries`), so a digest going stale
    re-fetches and the banner below lands on the laptop — staleness is
    never silent.

    Unlike `_sync_knowledge_artifacts` (binary `.duckdb` via
    `stream_download`), digest content is JSON — fetched via `api_get`,
    the same idiom `_fetch_and_write_rules` uses for the memory bundle.

    Never touches the `km_*.md` namespace — `ka_*.md` is this function's
    own, separately server-managed namespace.
    """
    section = manifest.get("knowledge_artifacts")
    if section is None:
        return
    entries = [e for e in (section or []) if e.get("kind") == "digest"]
    rules_dir = Path(workspace) / ".claude" / "rules"
    known = local_state.setdefault("knowledge_digests", {})
    listed_files: set[str] = set()
    for entry in entries:
        slug = entry.get("slug") or ""
        did = entry.get("id") or ""
        md5 = entry.get("md5") or ""
        if not _SAFE_ID_RE.match(slug) or not _SAFE_ID_RE.match(did):
            continue
        fname = f"ka_{slug}.md"
        listed_files.add(fname)
        target = rules_dir / fname
        # The stored md5 covers the digest's CONTENT, not the wording this
        # module wraps it in — so a change to the template (like the provenance
        # line added here) would never reach a workspace whose digests happen
        # not to change. The render version rides along, and a bump re-writes
        # every digest once. (Adversarial review of this PR: the CHANGELOG
        # claimed `ka_*.md` gets the header; for already-synced digests it did
        # not.)
        cached = known.get(did, {})
        if md5 and cached.get("md5") == md5 and cached.get("render") == _DIGEST_RENDER_VERSION and target.exists():
            continue  # hash-equal, same template, file present
        try:
            resp = api_get(entry.get("url") or f"/api/knowledge/digests/{did}/content")
            resp.raise_for_status()
            body = resp.json()
            rules_dir.mkdir(parents=True, exist_ok=True)  # lazy mkdir, km_ contract
            target.write_text(_digest_to_md(body), encoding="utf-8")
            known[did] = {"md5": md5, "slug": slug, "render": _DIGEST_RENDER_VERSION}
            result.digests_updated += 1
        except Exception as exc:
            result.errors.append({"stage": "knowledge_digests", "digest": slug, "error": str(exc)})
    if rules_dir.exists():
        for f in sorted(rules_dir.glob("ka_*.md")):
            if f.name in listed_files:
                continue
            f.unlink(missing_ok=True)
            result.digests_removed += 1
        known_ids = {d for d, meta in known.items() if f"ka_{meta.get('slug')}.md" in listed_files}
        for gone in set(known) - known_ids:
            known.pop(gone, None)


def _is_valid_parquet(path: Path) -> bool:
    """Cheap structural check — parquet files begin and end with `PAR1`.

    Used as a fallback when the manifest has no hash (legacy snapshots) and
    during view rebuild to skip obviously-broken files. Does not guarantee
    the footer is well-formed — that's DuckDB's job at CREATE VIEW time.
    """
    try:
        size = path.stat().st_size
        if size < 8:
            return False
        with open(path, "rb") as f:
            head = f.read(4)
            f.seek(-4, 2)
            tail = f.read(4)
        return head == b"PAR1" and tail == b"PAR1"
    except OSError:
        return False


def _blocked_snapshot_names(
    server_tables: dict,
    authorized_names: set[str] | None,
    server_only_names: set[str],
    *,
    previously_local: set[str],
    still_local: set[str],
    remembered: set[str],
) -> set[str]:
    """Names a snapshot view must NOT take, remembered across pulls.

    ``agnes snapshot create <table>`` with no ``--as`` writes
    ``user/snapshots/<table_id>.parquet``. Once the server copy of that table
    stops resolving locally, the bare id is free again, and
    ``_register_snapshot_views`` would hand it to that file — so a query for
    the table answers from a snapshot taken while it was still readable,
    instead of erroring.

    Three rules, and each exists because of a way the obvious version is wrong:

    * **Only ids that actually resolved locally.** ``authorized_names`` holds
      the analyst's data-package tables only, while ``server_tables`` lists
      everything they can see — for an admin, ``get_accessible_tables``
      resolves to ``None`` and that is the whole instance. Judging by "in the
      manifest but not in my packages" therefore withheld every table outside
      the caller's own stack, killing admins' snapshots on every pull. An id
      that was never downloaded has no local rows to shadow.

    * **Ids that vanished from the manifest count too.** Full revocation
      removes the row entirely, so a rule that only iterates ``server_tables``
      never sees the strongest case — while the prune still deletes its
      parquet and frees the name.

    * **Remembered, not recomputed.** The evidence that an id used to be local
      is its ``sync_state`` row, and the prune deletes that row. A set derived
      fresh each run would block the name on the pull that removes the file and
      release it on the next one, which is worse than not fixing it: whether
      stale rows answer would depend on how many times you had pulled. So the
      decision is persisted and carried forward.

    An id is released once it resolves locally again (re-authorized and
    re-downloaded): the registered table owns the name at that point, and
    keeping it blocked would withhold a name nothing is competing for.
    """
    newly_revoked = {
        tid
        for tid in previously_local
        if tid in server_only_names
        or tid not in server_tables
        or (authorized_names is not None and tid not in authorized_names)
    }
    return (remembered | newly_revoked) - still_local


def _manifest_policy_fingerprints(manifest: dict) -> dict:
    """``table_id -> access_policy_fingerprint`` read from
    ``data_packages[].tables[]`` (``app/api/sync.py::_table_manifest_entry``,
    table access policies §3.4/§10.3, plan Task 18) — the only manifest
    section carrying it, since a policied table is only ever reachable
    through a data package under the unified-stack model (attaching a
    policy requires ``server_only=True``, and ``server_only`` tables
    surface exclusively via packages).

    Keyed by BOTH the registry ``id`` and ``name`` pointing at the same
    fingerprint: ``SnapshotMeta.table_id`` is whatever the analyst typed as
    the ``agnes snapshot create <table_id>`` argument, which — mirroring
    the server-side resolver's own id-or-name fallback
    (``src/access_policy.py::_resolve_table_row``) — may be either form.
    Keying on only one would false-positive-block a snapshot of a table
    whose id and name differ.

    A table id/name absent from the map — outside the puller's current
    stack, or a pre-this-feature server that never emits the key at all —
    is a table whose current policy state this manifest simply does not
    describe. The caller (:func:`_stale_policy_snapshot_names`)
    distinguishes that from "the manifest says this table has no policy":
    presence in the map, not the value read out of it, is what makes a
    comparison meaningful.
    """
    out: dict = {}
    for pkg in manifest.get("data_packages", []) or []:
        for t in pkg.get("tables", []) or []:
            fingerprint = t.get("access_policy_fingerprint")
            for key in (t.get("id"), t.get("name")):
                if key:
                    out[key] = fingerprint
    return out


def _stale_policy_snapshot_names(workspace: Path, manifest: dict) -> set[str]:
    """Local snapshot VIEW names (table access policies §3.4, §10.3; plan
    Task 18) whose stored ``SnapshotMeta.policy_fingerprint`` no longer
    matches the CURRENT fingerprint the manifest reports for that
    snapshot's source table — the policy SQL changed, or the puller's own
    group membership changed, since ``agnes snapshot create``/``refresh``
    last ran.

    Keyed off ``SnapshotMeta.name`` (the actual registered view name — may
    differ from ``table_id`` under ``--as``), unlike ``_blocked_snapshot_
    names`` above, which only ever withholds the bare ``table_id`` (the
    one collision a nameless ``agnes snapshot create`` produces): a policy
    can go stale under ANY snapshot name.

    A snapshot with no recorded fingerprint (created before this feature
    existed, or of a table that carried no policy at fetch time) compares
    against ``None`` — so a policy newly ATTACHED to that table after the
    fact also goes stale, not only an edited one. Both sides ``None`` (no
    policy then, none now) compares equal and stays resolvable — the "no
    policy = no behaviour change" invariant holds for snapshots too.

    **The comparison only fires for a snapshot whose source table this
    manifest actually describes.** Which table that is comes from
    ``SnapshotMeta.policy_table_id`` (the ``X-Agnes-Policy-Table-Id``
    ``/api/v2/scan`` stamps beside the fingerprint) when present, falling
    back to ``table_id``. It has to: on the ``--from-query`` path —
    ``agnes snapshot create <name> --from-query …`` and every ``agnes
    query --remote --auto-snapshot`` — ``table_id`` holds the snapshot
    NAME the caller passed positionally, never a registry id, so it
    resolves to nothing in the manifest map. Treating that unresolvable
    lookup as ``None`` and calling ``None != <stored hash>`` "stale"
    withheld such a snapshot on EVERY subsequent pull, permanently, with
    no way to recover it. Unknown is not stale: a snapshot whose source
    table is absent from the map is left alone. Every snapshot that DOES
    resolve stays fail-closed exactly as before — including one whose
    table is present but now reports no fingerprint at all.
    """
    snapshots_dir = workspace / "user" / "snapshots"
    if not snapshots_dir.exists():
        return set()

    current = _manifest_policy_fingerprints(manifest)
    stale: set[str] = set()
    for meta in list_snapshots(snapshots_dir):
        source_table = getattr(meta, "policy_table_id", None) or meta.table_id
        if source_table not in current:
            continue
        if meta.policy_fingerprint != current[source_table]:
            stale.add(meta.name)
    return stale


def _rebuild_duckdb_views(workspace: Path, parquet_dir: Path, blocked_names: set[str] | None = None) -> list[str]:
    """Recreate DuckDB views from downloaded parquets. Preserve user tables.

    The DuckDB file at `<workspace>/user/duckdb/analytics.duckdb` is
    created unconditionally (even on an empty pull) — downstream readers
    expect the file to exist. The parquet rebuild loop is a no-op when
    `parquet_dir` is missing.

    Three sources are registered, in this precedence order (a later source
    yields to a name an earlier one already took):

    1. `parquet_dir` (`<workspace>/server/parquet/`) — the legacy flat flow.
    2. The stack-sync tree (`<workspace>/.claude/data/_direct/` +
       `<workspace>/.claude/data/<package_slug>/`, written by step 8 /
       `cli/lib/pull_sync.py`) — see `_register_stack_views` (#1325).
    3. `agnes snapshot create` output (`_register_snapshot_views`) — always
       last, so a registered table of either kind above always wins a name
       collision with a snapshot.

    `blocked_names` (#1129 review) are table ids the analyst is no longer
    authorized for, or that turned `server_only`. A snapshot must not take
    such a name; see `_register_snapshot_views`. Defaults to None so callers
    outside `run_pull` (tests, ad-hoc repair) keep working unchanged.

    Returns the snapshot view names withheld for that reason — empty on every
    ordinary pull.
    """
    import duckdb

    from src.duckdb_conn import _open_duckdb

    db_path = workspace / "user" / "duckdb" / "analytics.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _open_duckdb(str(db_path))
    try:
        # Existing user-created BASE TABLEs we must not shadow with views.
        try:
            existing_tables = {
                row[0]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_type='BASE TABLE'"
                ).fetchall()
            }
        except Exception:
            existing_tables = set()

        # Drop all current views so the rebuild is from a clean slate.
        try:
            views = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_type='VIEW'").fetchall()
            for (view_name,) in views:
                conn.execute(f"DROP VIEW IF EXISTS {quote_ident(view_name)}")
        except Exception:
            pass

        # Recreate views for each parquet file. One broken file (corrupt
        # download, partial write left over from a previous run, ...) must
        # not abort the whole rebuild — skip and keep going.
        if parquet_dir.exists():
            for entry in sorted(parquet_dir.iterdir()):
                # Interrupted partitioned syncs leave a `.staging-<tid>` dir;
                # never expose it as a view.
                if entry.name.startswith(".staging-"):
                    continue
                if entry.is_dir():
                    # Partitioned table: ONE view over all parts (Jira hive
                    # `month=*/data.parquet`, Keboola flat `<key>.parquet`),
                    # unioned + hive-partitioned so it reads byte-identically
                    # to the server-side view and a per-month schema drift is
                    # tolerated. View name = table id (dir name), NOT the part
                    # file stems (which all collide on `data`).
                    view_name = entry.name
                    if view_name in existing_tables:
                        continue
                    if not any(_is_valid_parquet(p) for p in entry.rglob("*.parquet")):
                        continue
                    glob_lit = str((entry / "**" / "*.parquet").resolve()).replace("'", "''")
                    try:
                        conn.execute(
                            f"CREATE VIEW {quote_ident(view_name)} AS SELECT * FROM "
                            f"read_parquet('{glob_lit}', union_by_name=true, hive_partitioning=true)"
                        )
                    except duckdb.Error:
                        continue
                elif entry.suffix == ".parquet":
                    # Single-file table.
                    view_name = entry.stem
                    if view_name in existing_tables:
                        continue
                    if not _is_valid_parquet(entry):
                        continue
                    abs_path = str(entry.resolve()).replace("'", "''")
                    try:
                        conn.execute(
                            f"CREATE VIEW {quote_ident(view_name)} AS SELECT * FROM read_parquet('{abs_path}')"
                        )
                    except duckdb.Error:
                        continue

        # Stack-sync tree (#1325) — `parquet_dir` views are in place, so
        # re-derive what's actually registered (BASE TABLEs from before the
        # rebuild + the views the loop above just created) rather than reuse
        # `existing_tables`, which still only holds the pre-rebuild BASE
        # TABLEs. That fresh set is `claimed`: a name already in it is
        # `server/parquet/`'s (or a user table's) and the stack tree yields.
        try:
            claimed = {row[0] for row in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()}
        except Exception:
            claimed = set(existing_tables)
        _register_stack_views(conn, workspace, claimed)

        # Workspace-local uploaded tables (chat "+" upload → register_as_table):
        # a self-contained `uploads/extract.duckdb` holds materialized tables.
        # ATTACH it read-only and copy each table into analytics.duckdb so
        # `agnes query` reaches it in-session — the extract.duckdb's tables are
        # materialized (not views over external files), so this survives the
        # workspace→sandbox sync. A view referencing the attached catalog would
        # dangle once the connection closes, hence the materialize. A missing or
        # broken file is a no-op (never aborts the parquet rebuild above).
        uploads_extract = workspace / "uploads" / "extract.duckdb"
        if uploads_extract.exists():
            try:
                conn.execute(f"ATTACH '{uploads_extract.resolve()}' AS _uploads (READ_ONLY)")
                try:
                    up_tables = conn.execute(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_catalog='_uploads' AND table_type='BASE TABLE' "
                        "AND table_name <> '_meta'"
                    ).fetchall()
                    for (t_name,) in up_tables:
                        try:
                            conn.execute(
                                f"CREATE OR REPLACE TABLE {quote_ident(t_name)} "
                                f"AS SELECT * FROM _uploads.{quote_ident(t_name)}"
                            )
                        except duckdb.Error:
                            continue
                finally:
                    conn.execute("DETACH _uploads")
            except duckdb.Error:
                pass

        return _register_snapshot_views(conn, workspace, blocked_names)
    finally:
        conn.close()


def _register_stack_views(conn, workspace: Path, claimed: set[str]) -> None:
    """Register DuckDB views over the stack-sync tree (#1325).

    `_rebuild_duckdb_views` used to walk only `parquet_dir`
    (`<workspace>/server/parquet/`), so a table `agnes pull` landed via the
    v49 stack sync (`cli/lib/pull_sync.py`, step 8 — `.claude/data/_direct/`
    + `.claude/data/<package_slug>/`, reference files into the
    content-addressed `.claude/data/_shared/`) sat on disk with no view:
    `cli/lib/local_tables.py`'s module docstring and `agnes status`'s
    "downloaded (no local view)" count both documented the gap this closes.

    One view per analyst-facing table NAME
    (`cli/lib/local_tables.py::stack_reference_files`) — the reference
    FILENAME, never the content-addressed `_shared/<table_id>.parquet` stem
    (`_shared` itself is never walked directly; it carries no name). Two
    packages referencing the same table share one name and are collapsed to
    a single registration by that helper, so this loop never double-creates
    a view.

    `claimed` already holds every name `server/parquet/` (or a pre-existing
    user BASE TABLE) took in this same rebuild — `server/parquet/` is the
    long-standing path, so a same-name collision is left to it rather than
    shadowed or flip-flopped between the two on successive pulls. Mutated in
    place as this loop registers names, so a corrupt/invalid reference is
    simply skipped (never aborts the rebuild), mirroring the `parquet_dir`
    loop above.

    `view_name` is quoted via `quote_ident` before reaching SQL: it comes
    from a filename on disk, not the (already-sanitized) manifest — nothing
    stops a stray file, a pre-sanitization-era sync, or manual tampering
    from putting an unsafe name there.
    """
    import duckdb

    from cli.lib.local_tables import stack_reference_files

    for view_name, ref_path in sorted(stack_reference_files(workspace).items()):
        if view_name in claimed:
            continue
        if not _is_valid_parquet(ref_path):
            continue
        abs_path = str(ref_path.resolve()).replace("'", "''")
        try:
            conn.execute(f"CREATE VIEW {quote_ident(view_name)} AS SELECT * FROM read_parquet('{abs_path}')")
        except duckdb.Error:
            continue
        claimed.add(view_name)


def _quote_ident(name: str) -> str:
    """Quote a SQL identifier, doubling embedded double-quotes.

    Mirrors `src.profiler.quote_ident`, re-stated here rather than imported:
    that module pulls in `src.db`, and this path runs on the analyst's laptop
    at every session start. A snapshot name is validated at creation time, but
    the name used here comes from a *filename on disk*, which nothing stops a
    user (or another tool) from writing directly.
    """
    return '"' + str(name).replace('"', '""') + '"'


def _register_snapshot_views(conn, workspace: Path, blocked_names: set[str] | None = None) -> list[str]:
    """Re-register views over local snapshots after the clean-slate drop.

    `agnes snapshot create` writes `user/snapshots/<name>.parquet` and
    registers a view named `<name>`. That tree is outside `parquet_dir`, so
    the drop-all above takes those views with it and the server loop cannot
    put them back: every pull silently removed every snapshot, while
    `agnes snapshot list` (which reads the meta sidecars off disk) kept
    reporting them as present.

    Runs last so a registered table always wins a name collision, and is
    self-healing: a workspace whose snapshot views were already destroyed
    gets them back on the next pull with no user action.

    `blocked_names` (#1129 review) are ids step 4b deliberately made
    unresolvable — de-authorized, or now `server_only`. A snapshot created
    with no `--as` is named after its source table, so without this check it
    would silently re-take that id and answer queries from stale rows. The
    parquet is left on disk and stays reachable through its snapshot path;
    only the bare id stops resolving. Returns the names withheld.
    """
    import duckdb

    withheld: list[str] = []
    snapshots_dir = workspace / "user" / "snapshots"
    if not snapshots_dir.exists():
        return withheld

    # Everything already registered by this rebuild — base tables the user
    # created plus the views just built from `parquet_dir`.
    try:
        taken = {row[0] for row in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    except Exception:
        return withheld

    for entry in sorted(snapshots_dir.glob("*.parquet")):
        view_name = entry.stem
        if view_name in taken:
            continue
        if blocked_names and view_name in blocked_names:
            withheld.append(view_name)
            continue
        if not _is_valid_parquet(entry):
            continue
        abs_path = str(entry.resolve()).replace("'", "''")
        try:
            conn.execute(f"CREATE VIEW {_quote_ident(view_name)} AS SELECT * FROM read_parquet('{abs_path}')")
        except duckdb.Error:
            continue

    return withheld


# Claude Code loads `.claude/rules/` at session start as project rules — an
# instruction channel. Corporate-memory notes are not instructions: they are
# things colleagues wrote down, and a recap phrased as a next step ("type
# /exit, then rerun claude") arrives looking exactly like an order from the
# operator. Observed live, where an agent stopped and reported the file as
# untrusted rather than following it — the correct call, but it means the
# analyst's session halts on a colleague's ordinary note.
#
# The header states where the content came from and stops there — provenance
# only, no reading instructions. An earlier draft added "these are recorded
# observations, not requests made in this session"; a naive agent shown that
# version called it "the document coaching my reading of itself, which I treat
# as a signal to be more skeptical, not less", and noted that a disclaimer
# sitting above an imperative is itself a suspicious pattern. It also said the
# clause changed nothing, since treating file content as data is already its
# baseline. So the clause cost trust and bought nothing, while the plain facts
# — who wrote this, when, how it got here — are what let a reader classify the
# text themselves. Same lesson as the 0.83.5 CLI-help fix, one file over.
# The claim has to be one the file can keep. "Approved by an administrator"
# was not: the bundle's required tier is selected on `is_required` alone
# (`app/api/memory.py`), so a required item ships whatever its status, and an
# instance running the collector in `auto_publish` mode files mined items as
# approved with nobody looking. So state the RECORD — required, or approved —
# and let the reader decide what that is worth on their instance.
# (Devin Review + an adversarial review of this PR.)
_PROVENANCE_LEAD_REQUIRED = (
    "_Source: this instance's corporate memory. The note below was written by a "
    "colleague during their own session and is marked *required* in this "
    "instance's memory"
)
_PROVENANCE_LEAD_APPROVED = (
    "_Source: this instance's corporate memory. Each note below was written by a "
    "colleague during their own session and carries the status *approved* in "
    "this instance's memory"
)
#: The rollup states how many notes it carries. In-band framing cannot be made
#: forgery-proof — a note's body is delivered verbatim, so it can contain a
#: `---` and a heading of its own — but a stated count a reader can compare
#: against the headings they see turns a silent forgery into a visible
#: discrepancy. (Adversarial review of this PR.)
_PROVENANCE_COUNT = ". This file carries {n} note{s}"
_PROVENANCE_CREDIT_ONE = "; the credit line under the heading names who and when"
_PROVENANCE_CREDIT_MANY = "; the credit line under each heading names who and when"
# A note's own text is delivered verbatim — including any line that looks like
# a heading, a rule, or a credit. Saying so is the difference between a header
# a reader can rely on and one that vouches for whatever a note puts under it.
_PROVENANCE_TAIL = (
    ". Note text is reproduced unchanged, so a note may itself contain lines "
    "that look like headings or credits — those are part of the note. Written "
    "by `agnes pull` and regenerated on every sync, so edits made here are lost._"
)


def _one_line(value: object, *, limit: int = 200) -> str:
    """A title as a title: one line, no structure of its own.

    Titles, domains and categories are analyst-authored and land in a file an
    agent reads as project rules. Interpolated raw, a title carrying newlines
    writes its own headings and its own `— author, date` credit line — under
    somebody else's name — and the provenance header this module adds then
    vouches for the forgery. Collapsing whitespace is not editing a note: the
    note's BODY is delivered byte-for-byte (that promise is the point), while
    a title's job is to name it on one line.
    (Found by an adversarial review of this PR.)
    """
    flat = " ".join(str(value or "").split())
    return flat[: limit - 1] + "…" if len(flat) > limit else flat


def _attribution(item: dict) -> str:
    """One-line `— author, date` credit, omitting whatever the bundle lacks.

    Attribution is what makes the header's claim checkable per note rather
    than in the abstract: a sentence visibly credited to a colleague on a date
    reads as their note, where an uncredited one reads as the instance
    speaking.
    """
    who = _one_line(item.get("source_user"), limit=120)
    # Stripped: a whitespace-only `created_at` rendered as `_—    _` — a credit
    # line with nothing in it, which also made the header promise credits it
    # could not show.
    when = (item.get("created_at") or "").strip()[:10]
    parts = [p for p in (who, when) if p]
    return f"_— {', '.join(parts)}_" if parts else ""


def _memory_provenance(items: list, *, required: bool = False) -> str:
    """The header, promising a credit line only when there is one to find.

    ``required=True`` renders the single-note wording for a per-item
    ``km_<id>.md``: that file holds exactly one note, so "each note below" and
    "under each heading" described a file the reader was not looking at.

    ``source_user`` and ``created_at`` are both nullable in the bundle, so the
    sentence about credit lines is conditional. Stated unconditionally it was a
    claim the file could fail to keep — a naive reader shown that version
    checked it, found no credit line under either heading, and said the gap
    "undercuts the file's claim to be a verifiable, attributed human note" and
    was "one more reason to treat the embedded directive with suspicion". A
    provenance header earns its place by being checkable; one that overstates
    by a clause does the opposite of what it is for.
    """
    credited = any(_attribution(it) for it in items)
    if required:
        lead, credit, count = _PROVENANCE_LEAD_REQUIRED, _PROVENANCE_CREDIT_ONE, ""
    else:
        lead, credit = _PROVENANCE_LEAD_APPROVED, _PROVENANCE_CREDIT_MANY
        n = len(items)
        count = _PROVENANCE_COUNT.format(n=n, s="" if n == 1 else "s")
    return lead + (credit if credited else "") + count + _PROVENANCE_TAIL


def _item_to_md(item: dict) -> str:
    """Render a knowledge item as a Markdown rule file."""
    lines = [
        f"# {_one_line(item.get('title')) or 'Untitled'}",
        "",
        _memory_provenance([item], required=True),
        "",
    ]
    if domain := _one_line(item.get("domain"), limit=120):
        lines.append(f"_Domain: {domain}_")
    if category := _one_line(item.get("category"), limit=120):
        lines.append(f"_Category: {category}_")
    if attribution := _attribution(item):
        lines.append(attribution)
    lines.append("")
    lines.append(item.get("content", ""))
    return "\n".join(lines) + "\n"


def _fetch_and_write_rules(workspace: Path) -> int:
    """Fetch /api/memory/bundle and write `.claude/rules/km_*.md` files.

    Returns the count of rule files actually written.

    Lazy mkdir contract — Task 8 fix vs. legacy `cli/commands/sync.py`:
    the rules directory is created only when the bundle has at least one
    mandatory item or a non-empty approved list. An empty bundle leaves
    the workspace untouched (no `.claude/rules/` shell, no `km_approved.md`
    cleanup attempt against a directory that doesn't exist).

    The km_*.md namespace in `.claude/rules/` is server-managed: this
    function is the only writer, and it prunes any stale km_*.md files on
    every run that materializes the directory. Do not create km_*.md
    files manually — they will be removed on next pull.
    """
    rules_dir = workspace / ".claude" / "rules"
    resp = api_get("/api/memory/bundle")
    resp.raise_for_status()
    bundle = resp.json()

    mandatory = bundle.get("mandatory", []) or []
    approved = bundle.get("approved", []) or []

    # Lazy mkdir — empty bundle leaves the workspace tree alone.
    if not mandatory and not approved:
        return 0

    rules_dir.mkdir(parents=True, exist_ok=True)
    written: set[str] = set()

    # One file per mandatory item.
    for item in mandatory:
        item_id = item.get("id", "")
        if not _SAFE_ID_RE.match(item_id):
            # Silently skip unsafe ids — caller has no Typer.echo here.
            continue
        fname = f"km_{item_id}.md"
        (rules_dir / fname).write_text(_item_to_md(item), encoding="utf-8")
        written.add(fname)

    # Approved items roll up into a single file. Notes are separated by a rule
    # and credited individually — in a rollup, one note's imperative otherwise
    # runs straight into the next note's heading with nothing marking where one
    # colleague's text ends and another's begins.
    if approved:
        lines = ["# Approved Corporate Knowledge\n", _memory_provenance(approved) + "\n"]
        for item in approved:
            lines.append("---\n")
            lines.append(f"## {_one_line(item.get('title')) or 'Untitled'}\n")
            if attribution := _attribution(item):
                lines.append(attribution + "\n")
            lines.append(item.get("content", "") + "\n")
        (rules_dir / "km_approved.md").write_text("\n".join(lines), encoding="utf-8")
        written.add("km_approved.md")
    else:
        stale = rules_dir / "km_approved.md"
        if stale.exists():
            stale.unlink()

    # Prune stale per-item files no longer mandatory.
    for existing in rules_dir.glob("km_*.md"):
        if existing.name not in written and existing.name != "km_approved.md":
            existing.unlink()

    return len(written)


def _policied_table_names_from_manifest(manifest: dict) -> list[str]:
    """Names of tables in the manifest's ``data_packages[].tables[]``
    sections that carry ``access_policy: true`` (table access policies §10
    item 4) -- the analyst's OWN granted stack, not the whole registry.

    Sourced from the typed v49 section (``app/api/sync.py::
    _table_manifest_entry``), the same one the RBAC name-filter above
    already reads -- the flat legacy ``manifest["tables"]`` dict carries no
    ``access_policy`` marker and never will (§10 item 4 targets the typed
    sections only). De-duped and sorted for a stable file across pulls that
    only reorder unrelated manifest fields.
    """
    names: set[str] = set()
    for pkg in manifest.get("data_packages") or []:
        for t in pkg.get("tables") or []:
            if t.get("access_policy") and t.get("name"):
                names.add(t["name"])
    return sorted(names)


def _write_access_policy_rules(manifest: dict, workspace: Path) -> int:
    """Write/prune ``.claude/rules/access_policies.md`` (table access
    policies §10 item 4): name every access-policied table in the
    analyst's own stack, so an agent carries the caveat in context BEFORE
    it writes a query against one of these tables -- the only link in the
    disclosure chain (§10) that reaches an agent before the fact rather
    than after a response comes back with a `row_scope` note attached.

    Deliberately NOT named ``ka_<x>.md`` / ``km_<x>.md`` despite living
    next to those two managed namespaces in the same directory: each is
    swept by its OWN owner's prune loop on every pull --
    ``_sync_knowledge_digests`` deletes any ``ka_*.md`` file that isn't one
    of ITS digest slugs the moment the manifest carries a
    ``knowledge_artifacts`` key (see the empty-list case in that function's
    own docstring), and ``_fetch_and_write_rules`` does the same over
    ``km_*.md``. A same-prefix file here would be deleted out from under
    this feature by an unrelated pull step.

    No hash-based skip, unlike the digest/memory writers above: the content
    is just a name list (cheap to rebuild every pull), and a table dropping
    off the stack must prune the file promptly rather than wait for a
    content hash to differ.
    """
    rules_dir = workspace / ".claude" / "rules"
    target = rules_dir / "access_policies.md"
    names = _policied_table_names_from_manifest(manifest)
    if not names:
        if target.exists():
            target.unlink()
        return 0

    rules_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Access-Policied Tables",
        "",
        "_Server-managed; do not edit._",
        "",
        "The following tables in your stack carry a row/column access "
        "policy: every query against one of them returns YOUR scoped "
        "slice, not the whole table (the server also flags this per-response "
        "as `row_scope`). Before summarizing a result from one of these "
        "tables, state that qualification -- never present a count or "
        "aggregate over one as an organisation-wide figure.",
        "",
    ]
    lines += [f"- `{name}`" for name in names]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(names)


def _make_tree_writable(path: Path) -> None:
    """chmod every file under ``path`` writable before it's overwritten or
    ``shutil.rmtree``'d. The semantic cache writes files 0o444 (read-only —
    the server is the source of truth, an edit would be silently reverted on
    the next pull); unlinking a read-only file needs directory write
    permission on POSIX (already sufficient there) but needs the FILE itself
    writable on Windows, so this is cheap insurance rather than a no-op.
    """
    for p in path.rglob("*"):
        if p.is_file():
            try:
                p.chmod(0o644)
            except OSError:
                pass


def _sync_semantic_cache(workspace: Path) -> tuple[int, int]:
    """Fetch ``/api/semantic-models/bundle`` and render the read-only local
    semantic-layer cache under ``<workspace>/semantic/<slug>/…`` (Fáze 1 —
    "distribuce jako fyzická cache s TTL", semantic-layer follow-up plan).

    Returns ``(models_updated, models_removed)`` — ``models_updated`` counts
    FILES written (mirrors ``rules_count``'s per-file counting, not
    per-model), ``models_removed`` counts whole model directories pruned
    because the model left the caller's accessible set.

    Rewrites every accessible model's files on every run rather than hash-
    diffing per model first (mirrors ``_fetch_and_write_rules``'s `km_*.md`
    bundle, not the parquet per-table diff path): documents are small text,
    and a stale local file surviving a revoked grant is a correctness bug
    the km_/ka_ prune loops already treat the same way — not a scenario
    worth trading for the complexity of a per-model skip.

    A pre-this-feature server (404, no ``/api/semantic-models/bundle``
    route) leaves any existing local cache untouched rather than nuking it
    — the same "older server, no key" posture ``_sync_knowledge_digests``
    takes for a manifest missing its own key.
    """
    semantic_dir = workspace / "semantic"
    resp = api_get("/api/semantic-models/bundle")
    if resp.status_code == 404:
        return 0, 0
    resp.raise_for_status()
    bundle = resp.json()

    from src.semantic.cache_render import DEFAULT_TTL_SECONDS, render_semantic_cache

    rows = bundle.get("models") or []
    generated_at = bundle.get("generated_at") or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    ttl_seconds = int(bundle.get("ttl_seconds") or DEFAULT_TTL_SECONDS)
    files = render_semantic_cache(rows, generated_at=generated_at, ttl_seconds=ttl_seconds)

    live_slugs = {row.get("slug") for row in rows if isinstance(row, dict) and row.get("slug")}

    # Prune whole model directories the caller can no longer read (revoked
    # grant, deleted model, source re-sync that renamed the slug).
    removed = 0
    if semantic_dir.is_dir():
        for existing_dir in sorted(semantic_dir.iterdir()):
            if existing_dir.is_dir() and existing_dir.name not in live_slugs:
                _make_tree_writable(existing_dir)
                shutil.rmtree(existing_dir, ignore_errors=True)
                removed += 1

    if not files:
        return 0, removed

    semantic_dir.mkdir(parents=True, exist_ok=True)
    for relpath, content in files.items():
        target = semantic_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.chmod(0o644)
        target.write_text(content, encoding="utf-8")
        target.chmod(0o444)

    # Prune stale files WITHIN a still-live model's directory (e.g. a metric
    # the document dropped) — the write loop above only ever adds/updates.
    for slug in live_slugs:
        slug_dir = semantic_dir / slug
        if not slug_dir.is_dir():
            continue
        for existing in list(slug_dir.rglob("*")):
            if not existing.is_file():
                continue
            rel = existing.relative_to(semantic_dir).as_posix()
            if rel not in files:
                existing.chmod(0o644)
                existing.unlink()

    return len(files), removed
