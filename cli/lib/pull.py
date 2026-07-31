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

import hashlib
import os
import re
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import httpx

from cli.client import api_get, api_post, stream_download
from cli.config import get_sync_state, save_sync_state


@dataclass
class PullResult:
    """Outcome of a `run_pull` invocation.

    Fields:
    - `tables_updated`: count of parquets actually re-downloaded this run.
    - `tables_removed`: count of local `server/parquet/<name>.parquet` files
      pruned this run because the table left the authorized typed (v49)
      stack. Always 0 against a pre-v49 server that emits no typed sections.
    - `parquets_total`: count of non-remote tables visible in the manifest.
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
    rules_count: int = 0
    knowledge_updated: int = 0
    knowledge_removed: int = 0
    digests_updated: int = 0
    digests_removed: int = 0
    tables_via_signed_url: int = 0
    tables_via_app: int = 0
    duration_s: float = 0.0
    errors: list[dict] = field(default_factory=list)
    # v49 (Phase 7, Task 7.5) — per-type stack-sync result. Populated when
    # the manifest carries any of ``direct_tables`` / ``data_packages`` /
    # ``memory_domains``. Kept off the constructor signature (None default)
    # so older callers reading ``tables_updated`` keep compiling.
    stack_sync: object = None


_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,128}$")

# #596 — hash-mismatch recovery in `_download_one`. A download whose bytes
# don't match the manifest hash is treated as transient (corrupt mid-flight
# transfer, a server-side parquet rewrite that raced the manifest read) and
# re-downloaded up to this many extra times before the table is recorded as
# a hard error. The prior good `<tid>.parquet` is preserved across the whole
# loop (download lands in a sidecar; only a verified sidecar is promoted), so
# even a persistent mismatch never leaves the table missing from disk.
_DOWNLOAD_RETRIES = 2
_DOWNLOAD_RETRY_BACKOFFS_S = (0.5, 1.0)

# WF-4 (wave 2H) — direct-to-object-storage fetch of a manifest `signed_url`.
# Bounded connect/read timeouts so a stalled object-store endpoint doesn't
# hang the whole pull; the app-served fallback below has its own budget.
_SIGNED_URL_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=10.0)
# Chunk size matches `_file_md5`'s read chunking — no functional requirement
# (the md5 is computed over the whole file after it lands), just keeps the
# streaming discipline visibly consistent with the rest of this module.
_SIGNED_URL_CHUNK_BYTES = 8192


def _fetch_signed_url(url: str, target_path: str, progress_callback=None) -> None:
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
                with open(target_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=_SIGNED_URL_CHUNK_BYTES):
                        f.write(chunk)
                        if progress_callback and chunk:
                            progress_callback(len(chunk))
        except _SSRFRejected as e:
            raise ValueError(f"signed_url rejected: {e.reason}") from e


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


def _diff_parts(
    server_parts: list[dict], local_parts: dict, table_dir: Path
) -> tuple[list[dict], set[str]]:
    """Compute ``(fetch, prune)`` for a partitioned table.

    ``fetch`` = server part dicts whose local hash differs OR whose file is
    missing on disk (a matching local hash is NOT proof the file is present —
    same existence-guard rationale as the single-file path). ``prune`` =
    relpaths present locally (on disk or in prior state) that the server no
    longer lists.
    """
    server_by_path = {p["path"]: p for p in server_parts}
    fetch = [
        p
        for path, p in server_by_path.items()
        if local_parts.get(path) != p["hash"] or not (table_dir / path).exists()
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
    pruned. On any fetch/verify failure nothing is moved — the prior table dir
    is left intact. The per-part moves themselves are not one atomic unit, so a
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
            fetch_part(relpath, dest)
            got = _file_md5(dest)
            if got != expected:
                return None, False, f"part {relpath} hash mismatch: expected {expected} got {got}"
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
        # A transport/IO error (e.g. `stream_download` network blip) or a
        # promote/prune failure must be RETURNED as a per-table error, not
        # raised — otherwise one flaky partitioned table would abort the whole
        # pull and discard tables that already downloaded fine. All-or-nothing
        # still holds: nothing was promoted, the prior table dir is intact.
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

        server_tables = manifest.get("tables", {}) or {}
        local_state = get_sync_state()
        local_tables = local_state.get("tables", {})

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
        # operator nuking `server/parquet/` during cleanup, a different
        # workspace sharing the same `~/.config/agnes/sync_state.json`
        # (TODO(workspace-scoped-sync-state) below) writing one workspace's
        # parquets while another reads sync_state and assumes "I already
        # have these." Without the existence guard, `agnes pull` would skip
        # the download and the downstream DuckDB view rebuild fails on a
        # missing file. Hash-equal-but-file-missing → force re-download.
        to_download: list[str] = []
        partitioned_tids: list[str] = []
        non_remote_total = 0
        parquet_dir = workspace / "server" / "parquet"
        for tid, info in server_tables.items():
            if info.get("query_mode") == "remote":
                continue
            if skip_materialize and info.get("query_mode") == "materialized":
                # Operator opt-out for first-init. Materialized rows are
                # still discoverable via `agnes catalog` and queryable
                # the next time `agnes pull` runs without --skip-materialize.
                continue
            # #506 — when typed sections are present, the stack is the unit of
            # access: never download a flat-dict table the typed stack omits
            # (admin god-mode over-list). Pre-v49 servers have
            # `authorized_names is None` → no filter.
            if authorized_names is not None and tid not in authorized_names:
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
                Progress,
                BarColumn,
                DownloadColumn,
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

            def _entry() -> dict:
                return {
                    "hash": expected_hash,
                    "rows": info.get("rows", 0),
                    "size_bytes": info.get("size_bytes", 0),
                }

            try:
                if signed_url:
                    try:
                        _fetch_signed_url(signed_url, str(sidecar), progress_callback=cb)
                        ok, _verify_err = _verify_and_promote(sidecar, target, expected_hash)
                        if ok:
                            return tid, _entry(), None, "signed_url"
                        # md5 mismatch (or, on a hash-less legacy manifest, a
                        # failed PAR1 check) — fall through to the app path.
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
                        # is untouched; record + bail.
                        return tid, None, last_err, None
                    except Exception as exc:
                        last_err = str(exc)
                        sidecar.unlink(missing_ok=True)
                        if attempt < _DOWNLOAD_RETRIES:
                            time.sleep(_DOWNLOAD_RETRY_BACKOFFS_S[min(attempt, len(_DOWNLOAD_RETRY_BACKOFFS_S) - 1)])
                            continue
                        return tid, None, last_err, None
                # Loop exhausted without an explicit return (defensive).
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
        from urllib.parse import quote as _urlquote

        for tid in partitioned_tids:
            info = server_tables[tid]
            server_parts = info.get("parts") or []
            local_parts = (local_tables.get(tid) or {}).get("parts") or {}

            def _fetch_part(relpath: str, dest: Path, _tid: str = tid) -> None:
                stream_download(
                    f"/api/data/{_tid}/download?part={_urlquote(relpath)}",
                    str(dest),
                )

            entry, changed, err = _sync_partitioned_table(
                tid,
                server_parts,
                local_parts,
                parquet_dir,
                _fetch_part,
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

        # 5. Persist sync state (only on real runs).
        # TODO(workspace-scoped-sync-state): currently saved to
        # ~/.config/agnes/sync_state.json (per legacy sync.py behavior).
        # Two workspaces sharing one user account share this state.
        # Future: scope to <workspace>/.agnes/sync_state.json so workspace
        # bootstrap leaves no residue outside <workspace>/.
        local_state["tables"] = local_tables
        local_state["last_sync"] = datetime.now(timezone.utc).isoformat()
        save_sync_state(local_state)

        # 6. Rebuild DuckDB views — unconditional. The DB file is the
        # load-bearing artifact for downstream readers.
        _rebuild_duckdb_views(workspace, parquet_dir)

        # 7. Fetch corporate-memory bundle and lazily write
        # `.claude/rules/km_*.md`. Best-effort: a server outage on this
        # endpoint must not fail the whole pull.
        try:
            written = _fetch_and_write_rules(workspace)
            result.rules_count = written
        except Exception as exc:
            result.errors.append({"stage": "memory_bundle", "error": str(exc)})

        # 8. v49 stack sync — per-type loop into ``~/.claude/data/`` and
        # ``~/.claude/memory/`` with reference-counted dedup. Runs only
        # when the manifest carries the v49 fields (older servers /
        # backward-compat workspaces are untouched). Best-effort:
        # failure here records under ``result.errors`` but doesn't abort
        # the rest of the pull.
        if any(k in manifest for k in ("direct_tables", "data_packages", "memory_domains")):
            try:
                result.stack_sync = _run_stack_sync_from_manifest(manifest, workspace)
            except Exception as exc:
                result.errors.append({"stage": "stack_sync", "error": str(exc)})

    result.duration_s = time.monotonic() - started

    # 9. Pull-confirm telemetry — fire-and-forget POST so the server can
    # close the loop on the ``sync.pull_started`` event from Phase 6.
    try:
        _emit_pull_confirm(server_url, token, result)
    except Exception:
        pass

    return result


def _run_stack_sync_from_manifest(manifest: dict, workspace: Path):
    """Build a ``pull_sync.PullStackOptions`` from the manifest payload
    and invoke ``run_stack_sync``. The local sync root is the
    ``<workspace>/.claude/`` dir so the stack-sync artifacts live next
    to the existing ``<workspace>/.claude/rules/`` / ``<workspace>/.claude/
    settings.json`` tree (workspace-scoped, not user-home, matching
    Section 5.3 of the spec for analyst workspaces)."""
    from cli.lib.pull_sync import PullStackOptions, run_stack_sync

    local_root = workspace / ".claude"

    def _fetcher(url: str, target: Path) -> None:
        stream_download(url, str(target))

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
    )
    return run_stack_sync(opts)


def _emit_pull_confirm(server_url: str, token: str, result: "PullResult") -> None:
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


def _sync_knowledge_artifacts(manifest: dict, workspace: Path, local_state: dict, result: "PullResult") -> None:
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


def _digest_to_md(body: dict) -> str:
    """Render one maintained-digest content response as `ka_<slug>.md`.

    Title h1 + a maintained-note (server-managed, don't edit) + a visible
    STALE banner blockquote when ``status == "stale"`` (the never-silent
    invariant, K4 #799) + the digest's ``output_md`` body.
    """
    slug = body.get("slug") or ""
    lines = [f"# {body.get('title') or slug}", ""]
    lines.append(
        f"_Maintained digest `ka_{slug}` — regenerated by Agnes when "
        f"its sources change (last generated: {body.get('generated_at') or 'never'}). "
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


def _sync_knowledge_digests(manifest: dict, workspace: Path, local_state: dict, result: "PullResult") -> None:
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
        if md5 and known.get(did, {}).get("md5") == md5 and target.exists():
            continue  # hash-equal AND file present — same guard as parquets/artifacts
        try:
            resp = api_get(entry.get("url") or f"/api/knowledge/digests/{did}/content")
            resp.raise_for_status()
            body = resp.json()
            rules_dir.mkdir(parents=True, exist_ok=True)  # lazy mkdir, km_ contract
            target.write_text(_digest_to_md(body), encoding="utf-8")
            known[did] = {"md5": md5, "slug": slug}
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


def _rebuild_duckdb_views(workspace: Path, parquet_dir: Path) -> None:
    """Recreate DuckDB views from downloaded parquets. Preserve user tables.

    The DuckDB file at `<workspace>/user/duckdb/analytics.duckdb` is
    created unconditionally (even on an empty pull) — downstream readers
    expect the file to exist. The parquet rebuild loop is a no-op when
    `parquet_dir` is missing.
    """
    import duckdb  # noqa: F401  (kept for the duckdb.Error path below)
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
                conn.execute(f'DROP VIEW IF EXISTS "{view_name}"')
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
                            f'CREATE VIEW "{view_name}" AS SELECT * FROM '
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
                        conn.execute(f"CREATE VIEW \"{view_name}\" AS SELECT * FROM read_parquet('{abs_path}')")
                    except duckdb.Error:
                        continue

        _register_snapshot_views(conn, workspace)
    finally:
        conn.close()


def _quote_ident(name: str) -> str:
    """Quote a SQL identifier, doubling embedded double-quotes.

    Mirrors `src.profiler.quote_ident`, re-stated here rather than imported:
    that module pulls in `src.db`, and this path runs on the analyst's laptop
    at every session start. A snapshot name is validated at creation time, but
    the name used here comes from a *filename on disk*, which nothing stops a
    user (or another tool) from writing directly.
    """
    return '"' + str(name).replace('"', '""') + '"'


def _register_snapshot_views(conn, workspace: Path) -> None:
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
    """
    import duckdb  # noqa: F401  (duckdb.Error below)

    snapshots_dir = workspace / "user" / "snapshots"
    if not snapshots_dir.exists():
        return

    # Everything already registered by this rebuild — base tables the user
    # created plus the views just built from `parquet_dir`.
    try:
        taken = {row[0] for row in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    except Exception:
        return

    for entry in sorted(snapshots_dir.glob("*.parquet")):
        view_name = entry.stem
        if view_name in taken:
            continue
        if not _is_valid_parquet(entry):
            continue
        abs_path = str(entry.resolve()).replace("'", "''")
        try:
            conn.execute(f"CREATE VIEW {_quote_ident(view_name)} AS SELECT * FROM read_parquet('{abs_path}')")
        except duckdb.Error:
            continue


def _item_to_md(item: dict) -> str:
    """Render a knowledge item as a Markdown rule file."""
    lines = [f"# {item.get('title', 'Untitled')}"]
    if item.get("domain"):
        lines.append(f"_Domain: {item['domain']}_")
    if item.get("category"):
        lines.append(f"_Category: {item['category']}_")
    lines.append("")
    lines.append(item.get("content", ""))
    return "\n".join(lines)


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

    # Approved items roll up into a single file.
    if approved:
        lines = ["# Approved Corporate Knowledge\n"]
        for item in approved:
            lines.append(f"## {item.get('title', 'Untitled')}\n")
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
