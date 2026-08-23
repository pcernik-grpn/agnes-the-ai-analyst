"""Lightweight Keboola Storage API client for table export.

The DuckDB Keboola extension was the originally-intended fast path, but on
projects with the `block-shared-snowflake-access` feature flag and on linked
buckets the per-session workspace can't see the bucket schemas at all
(keboola/duckdb-extension#17, fixed upstream in v0.1.6 but not yet in the
community CDN as of 2026-05-06). The `kbcstorage` SDK works but uses
`os.chdir(temp_dir)` to redirect slice downloads, which is process-global —
threaded fan-out races on CWD and slice files land in the wrong directory.

This module talks to Storage API directly and downloads via signed URLs:
- POST /v2/storage/tables/{id}/export-async
- GET  /v2/storage/jobs/{id}  (poll until success/error)
- GET  /v2/storage/files/{id}?federationToken=1
- GET  <signed_url>  (single file or manifest + per-slice URLs for sliced)

No `os.chdir`, no azure-blob/google-cloud-storage SDK dependencies. The
federation-token detail response includes a signed URL for the single-file
case on all three cloud backends; for *sliced* exports each backend hands
back its own scheme (`gs://`, `azure://`, or — on AWS stacks that stage
exports on S3 — raw `s3://`), rewritten below. The S3 case signs with
botocore's SigV4 signer (already a dependency via boto3) because raw
`s3://` carries no signature of its own. Thread-safe: each call uses an
independent download path under a per-call temp directory.

Storage API reference:
- https://keboola.docs.apiary.io/#reference/tables/asynchronous-table-export
- https://keboola.docs.apiary.io/#reference/jobs
- https://keboola.docs.apiary.io/#reference/files/manage-files/file-detail
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import shutil
import tempfile
import time
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, List, Optional, assert_never

import requests

try:  # pragma: no cover - import guard mirrors src/object_store.py
    from botocore.auth import S3SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials
except ImportError:  # pragma: no cover - boto3 is a declared dependency
    S3SigV4Auth = None  # type: ignore[assignment]
    AWSRequest = None  # type: ignore[assignment]
    Credentials = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


class SliceScheme(StrEnum):
    """The URI schemes a sliced-export manifest can hand back.

    Declared as a closed set so `_prepare_slice_request` can dispatch over
    it exhaustively. The alternative — an open `if startswith(...)` chain
    ending in a pass-through — is what let AWS-staged exports ship a raw
    `s3://` URI to `requests` until #1418: the unknown scheme was indistin-
    guishable from an already-signed URL, so the failure surfaced as "No
    connection adapters were found" instead of naming the scheme.

    Adding a member here without giving it an arm is a type error at the
    `assert_never` below, not a runtime surprise on a customer's stack.
    """

    GS = "gs"
    AZURE = "azure"
    S3 = "s3"
    # Backends that hand back a URL already carrying its own signature.
    HTTPS_PRESIGNED = "https"


# Storage API guarantees export jobs are created small and finish in seconds
# to a few minutes for typical bucket-table sizes; the absolute upper bound
# (very large tables, peak Snowflake load) is the operator's
# storage.jobsParallelism + scan duration. 30 min is a generous ceiling that
# matches what the dashboard's data-preview UI would also wait for.
_DEFAULT_EXPORT_TIMEOUT_SEC = int(os.environ.get("AGNES_KEBOOLA_EXPORT_TIMEOUT_SEC", "1800"))
_DEFAULT_POLL_INTERVAL_SEC = float(os.environ.get("AGNES_KEBOOLA_POLL_INTERVAL_SEC", "2"))

# Per-slice HTTP download timeout — separate from the export-job timeout.
# Sliced exports return a manifest of signed URLs; an individual slice is
# bounded in size by Storage API's slicer (typically ~100 MiB), so a few
# minutes is plenty for one HTTP GET.
_DEFAULT_SLICE_DOWNLOAD_TIMEOUT_SEC = int(os.environ.get("AGNES_KEBOOLA_SLICE_TIMEOUT_SEC", "300"))


def get_temp_root() -> Optional[str]:
    """Return the parent dir for per-call tempdirs, or None to use the
    system default.

    Reads ``AGNES_TEMP_DIR`` (compose env, single source of truth) and
    creates the dir if it does not yet exist. Default behaviour
    (``AGNES_TEMP_DIR`` unset) preserves the OSS pre-fix path —
    ``tempfile.TemporaryDirectory(...)`` falls back to the platform's
    `tmpdir` (typically ``/tmp``).

    The agnes-dev cutover surfaced why this knob matters: the
    container's ``/tmp`` lives on the boot disk's overlayfs (29 GiB
    on agnes-dev, shared with /var), so a multi-slice Snowflake
    UNLOAD of a wide table fills it long before the dedicated 20 GiB
    data disk at ``/data`` would. Setting ``AGNES_TEMP_DIR=/data/tmp``
    routes the staging dir to the data disk where the parquets are
    going anyway, no extra mount required (the data disk is already
    bind-mounted).
    """
    root = os.environ.get("AGNES_TEMP_DIR", "").strip()
    if not root:
        return None
    # Best-effort mkdir — if the parent isn't writable we let
    # tempfile.TemporaryDirectory raise the real OSError later with
    # the underlying detail. Avoids a silent fall-through to /tmp.
    try:
        os.makedirs(root, exist_ok=True)
    except OSError as e:
        logger.warning(
            "AGNES_TEMP_DIR=%r not creatable (%s); tempfiles fall back "
            "to system default. Set the env to a writable path or unset "
            "to silence this warning.",
            root,
            e,
        )
        return None
    return root


# Prefix of the per-connection DuckDB spill dirs that
# ``connectors/keboola/extractor.py:_open_consolidation_conn`` points
# ``temp_directory`` at. One directory per connection, never shared: DuckDB
# names its spill files by size class + index only
# (``duckdb_temp_storage_DEFAULT-0.tmp`` — no pid, no random part) and on close
# deletes *every* ``duckdb_temp_storage_*`` in the directory, including files
# another instance is still using, so two DuckDB instances pointed at one
# directory corrupt each other (verified on duckdb 1.5.5 — one of two
# concurrently-spilling processes died with SIGSEGV). Consolidation runs
# concurrently across the api / worker / sync-subprocess roles that share the
# data volume, hence per-connection.
#
# DuckDB creates the directory lazily (first spill) and removes it, with its
# spill files, on a clean close — so nothing is left behind on the normal path
# and non-spilling connections create nothing at all. A hard kill is the one
# case that strands it, which is exactly what :func:`sweep_orphaned_scratch`
# below reclaims.
SPILL_DIR_PREFIX = "kbc-spill-"

# Prefixes of the extractor-owned scratch under the temp root:
# ``kbc-export-`` (the per-export ``tempfile.TemporaryDirectory`` in
# connectors/keboola/extractor.py — materialize_query + legacy extract),
# ``kbc-slice-`` (the per-call sliced-CSV download dir in ``_download_sliced``
# below) and ``kbc-spill-`` (the DuckDB spill dirs described above).
_SCRATCH_PREFIXES = ("kbc-export-", "kbc-slice-", SPILL_DIR_PREFIX)


def sweep_orphaned_scratch(
    root: Optional[str] = None,
    max_age_seconds: Optional[float] = None,
) -> int:
    """Remove orphaned ``kbc-export-*`` / ``kbc-slice-*`` / ``kbc-spill-*``
    staging dirs and return the count.

    A staging dir is created via ``tempfile.TemporaryDirectory`` (the
    ``kbc-export-`` dirs in ``connectors/keboola/extractor.py`` and the
    ``kbc-slice-`` dir in ``_download_sliced`` below), whose ``__exit__``
    removes it on
    *any* normal return — including the ENOSPC / disk-full exception path.
    The DuckDB spill dirs (``kbc-spill-``, see :data:`SPILL_DIR_PREFIX`) are
    self-removing in the same sense: DuckDB deletes the directory it created
    when the connection closes cleanly.
    The only way a dir survives is a **hard kill** (SIGKILL / OOM / the
    auto-upgrade ``docker compose up -d`` recreating the container mid-sync —
    the documented orphan-maker, see ``app/api/sync.py``), where ``__exit__``
    never runs. Without a sweep these accumulate on the data disk until it
    fills and *every* subsequent sync fails on ENOSPC — a self-reinforcing
    failure that otherwise needs a manual ``rm`` to break.

    Age-gated: a dir whose mtime is within ``max_age_seconds`` is left alone
    so a concurrent in-flight export (e.g. another container) is never swept
    out from under itself. Threshold defaults to ``AGNES_SCRATCH_MAX_AGE_SEC``
    (1h) — comfortably longer than any single table export.

    ``root`` defaults to :func:`get_temp_root`; ``None`` (AGNES_TEMP_DIR unset)
    is a no-op since the system ``/tmp`` is cleared on reboot anyway.
    """
    if root is None:
        root = get_temp_root()
    if not root:
        return 0
    if max_age_seconds is None:
        max_age_seconds = float(os.environ.get("AGNES_SCRATCH_MAX_AGE_SEC", "3600"))
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0
    now = time.time()
    removed = 0
    for entry in entries:
        if not entry.name.startswith(_SCRATCH_PREFIXES):
            continue
        if not entry.is_dir(follow_symlinks=False):
            continue
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age < max_age_seconds:
            continue
        try:
            shutil.rmtree(entry.path)
            removed += 1
            logger.info(
                "Swept orphaned scratch dir %s (age %.0fs >= %.0fs threshold)",
                entry.name,
                age,
                max_age_seconds,
            )
        except OSError as e:
            logger.warning("Failed to sweep orphaned scratch %s: %s", entry.path, e)
    if removed:
        logger.info("Orphaned-scratch sweep removed %d dir(s) from %s", removed, root)
    return removed


def warn_if_scratch_survived(path: str) -> None:
    """Log a warning if a ``kbc-export-*`` staging dir is still present after
    its owning ``with tempfile.TemporaryDirectory(...)`` block exited.

    Callers pass ``ignore_cleanup_errors=True`` to that context manager so a
    cleanup failure never masks the export's real exception — but that flag
    silently swallows the failure too, so a leaked dir was previously
    indistinguishable from a hard-kill orphan and the only operator-visible
    signal was the disk slowly filling (see PROD incident 2026-07-16: two
    12 GiB dirs survived with nothing in the logs explaining why). This makes
    that failure mode visible; :func:`sweep_orphaned_scratch` still owns
    actual reclamation once the age threshold passes.
    """
    if os.path.isdir(path):
        logger.warning(
            "kbc-export scratch dir %s still present after TemporaryDirectory "
            "cleanup (ignore_cleanup_errors swallowed the failure) — it will "
            "be reclaimed by the next sweep_orphaned_scratch() pass once it "
            "ages past AGNES_SCRATCH_MAX_AGE_SEC",
            path,
        )


FILE_TYPE_CSV = "csv"
FILE_TYPE_PARQUET = "parquet"
_VALID_FILE_TYPES = {FILE_TYPE_CSV, FILE_TYPE_PARQUET}

# Splits a string into alternating (non-digit, digit) chunks for natural
# sorting — e.g. "...csv_0_0_2.csv" -> ["...csv_", "0", "_", "0", "_", "2", ".csv"].
_DIGIT_RUN_RE = re.compile(r"(\d+)")


def _slice_sort_key(url: str) -> list:
    """Natural (numeric-aware) sort key for a sliced-export slice URL.

    Verified live (2026-07-15): a legacy CSV extraction produced a parquet
    with garbled column names (data values instead of real column names,
    e.g. `'21630'`, `'false'`) — traced to `_download_sliced` concatenating
    slices in the manifest's raw `entries` array order, on the unverified
    assumption that Storage API always returns them already in physical
    order. Keboola's real slice filenames carry a numeric index (observed:
    `...csv_0_0_0.csv`, `...csv_0_0_1.csv`, ...); sorting by those numbers
    — rather than trusting array order or plain string order, which would
    incorrectly place `_0_0_12` before `_0_0_2` — guarantees the header
    slice (index 0) downloads first regardless of manifest ordering. A
    no-op (returns the same order) when the manifest was already correct.

    Only the URL PATH is keyed on: signed-URL query strings (S3
    `?X-Amz-Signature=...`, Azure SAS) carry their own digit runs that vary
    per slice and would otherwise perturb the sort, so they are dropped
    before splitting — the slice index lives in the path, never the query.
    """
    path = urlsplit(url).path
    return [int(chunk) if chunk.isdigit() else chunk for chunk in _DIGIT_RUN_RE.split(path)]


@dataclass
class ExportFilter:
    """Structured Keboola Storage API filter spec.

    Mirrors the BQ materialized path's `source_query` SQL string conceptually
    — both let the admin scope an extracted table — but Storage API takes a
    structured filter object rather than free-form SQL. Empty fields all
    map to "no filter" so a default-constructed ExportFilter exports the
    full table.

    Operators per Apiary docs: eq, ne, in, notIn, ge, gt, le, lt.

    `file_type` controls the format Storage API materializes into File
    Storage. `parquet` is the recommended path for the materialized sync:
    Keboola serves the parquet directly (UNLOADed from Snowflake), the
    extractor renames it into place — no CSV intermediate, no DuckDB
    COPY, no peak-memory load. Falls back to CSV when an admin pins
    `{"file_type":"csv"}` in source_query (e.g. for projects whose
    backend can't UNLOAD parquet, or legacy debugging).
    """

    where_filters: List[dict] = field(default_factory=list)
    columns: List[str] = field(default_factory=list)
    changed_since: Optional[str] = None
    changed_until: Optional[str] = None
    limit: Optional[int] = None
    file_type: str = FILE_TYPE_CSV

    def __post_init__(self):
        if self.file_type not in _VALID_FILE_TYPES:
            raise ValueError(f"file_type must be one of {sorted(_VALID_FILE_TYPES)}, got {self.file_type!r}")

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "ExportFilter":
        """Parse from `table_registry.source_query` JSON. Tolerates None /
        empty / unknown keys (registry stores admin input that may be sparse)."""
        if not data:
            return cls()
        if not isinstance(data, dict):
            raise ValueError(f"ExportFilter.from_dict expects a dict, got {type(data).__name__}")
        # Accept both `file_type` (preferred, matches the rest of the
        # snake_case API) and `fileType` (matches Storage API wire name)
        # so an admin who copies an example from Apiary docs doesn't trip.
        ft = data.get("file_type") or data.get("fileType") or FILE_TYPE_CSV
        return cls(
            where_filters=list(data.get("where_filters") or []),
            columns=list(data.get("columns") or []),
            changed_since=data.get("changed_since"),
            changed_until=data.get("changed_until"),
            limit=data.get("limit"),
            file_type=ft,
        )

    def to_export_params(self) -> dict:
        """Serialize for POST body of `/tables/{id}/export-async`.

        whereFilters arrives as a list of `{column, operator, values}` dicts;
        Storage API also accepts a single `whereColumn`/`whereOperator`/
        `whereValues` triple but the multi-filter form is more general.
        """
        params: dict = {}
        if self.where_filters:
            # Validate shape lightly — surface admin typos as ValueError
            # rather than letting them turn into a 400 from Keboola's API
            # without context.
            for i, f in enumerate(self.where_filters):
                if not isinstance(f, dict):
                    raise ValueError(f"where_filters[{i}] must be a dict")
                missing = {"column", "operator", "values"} - set(f.keys())
                if missing:
                    raise ValueError(f"where_filters[{i}] missing fields: {sorted(missing)}")
                if not isinstance(f["values"], list):
                    raise ValueError(f"where_filters[{i}].values must be a list")
            # Flatten into Keboola's indexed form-field convention. The
            # request is form-encoded (`_post` posts `data=params`), and
            # `requests` stringifies a nested list-of-dicts into a single
            # `whereFilters={'column': ...}` scalar — Keboola then rejects it
            # with "whereFilters should be an array, but parameter contains:
            # 'values'" (or silently returns the full table). Emitting one
            # scalar param per leaf (`whereFilters[i][column]`,
            # `whereFilters[i][operator]`, `whereFilters[i][values][j]`)
            # matches the PHP/Symfony array form-parsing the Storage API
            # expects — the same shape the `kbcstorage` SDK sends.
            for i, f in enumerate(self.where_filters):
                params[f"whereFilters[{i}][column]"] = f["column"]
                params[f"whereFilters[{i}][operator]"] = f["operator"]
                for j, v in enumerate(f["values"]):
                    params[f"whereFilters[{i}][values][{j}]"] = v
        if self.columns:
            params["columns"] = ",".join(self.columns)
        if self.changed_since:
            params["changedSince"] = self.changed_since
        if self.changed_until:
            params["changedUntil"] = self.changed_until
        if self.limit is not None:
            params["limit"] = int(self.limit)
        # Only emit fileType when non-default — keeps the request body
        # quiet for legacy callers that never knew about parquet, and
        # matches the wire-side default behaviour.
        if self.file_type and self.file_type != FILE_TYPE_CSV:
            params["fileType"] = self.file_type
        return params


class StorageApiError(RuntimeError):
    """Wraps a non-2xx Storage API response with the parsed body for context."""

    def __init__(self, message: str, status: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


def is_upstream_client_error(exc: Exception) -> bool:
    """True when an upstream API refused us for a reason WE control — a wrong
    or expired token, a stack URL pointing at the wrong project (4xx).

    Callers use it to keep 502 Bad Gateway for what it means (the upstream is
    unreachable or broken) instead of reporting the admin's own bad token as
    a gateway failure — which reads as "Agnes is down" and sends people
    hunting infrastructure. Duck-typed on ``.status`` so it covers both
    ``StorageApiError`` and ``MetastoreApiError`` without either module
    having to import the other; a transport-level ``requests`` exception has
    no status and is correctly NOT a client error.
    """
    status = getattr(exc, "status", None)
    return isinstance(status, int) and 400 <= status < 500


def normalize_source_table(bucket: str, source_table: str) -> str:
    """Return the bare in-bucket table name for ``source_table``.

    The registry contract stores the bucket id and the bare table name in
    separate columns; export paths compose ``f"{bucket}.{source_table}"``.
    Rows registered by the pre-fix Data-sources wizard (#755 era) stored the
    FULL Keboola table id (``<bucket>.<table>``) in ``source_table``, so the
    composition doubled the bucket prefix and every export targeted a
    nonexistent table id. Stripping a leading ``<bucket>.`` heals those rows
    at use — unambiguous because Keboola table names cannot contain dots.
    """
    if bucket and source_table and source_table.startswith(bucket + "."):
        return source_table[len(bucket) + 1 :]
    return source_table


class KeboolaStorageClient:
    """Thread-safe Storage API client for table export.

    One instance can be reused across threads — `requests.Session` is
    thread-safe when the underlying `HTTPAdapter`'s pool size is sized for
    concurrent calls. Default `pool_connections=20, pool_maxsize=20`
    accommodates the typical AGNES_KEBOOLA_PARALLELISM=8 plus headroom.
    """

    def __init__(self, *, url: str, token: str, session: Optional[requests.Session] = None):
        if not url or not token:
            raise ValueError("KeboolaStorageClient requires url and token")
        # The DuckDB Keboola extension's ATTACH chokes on a trailing slash
        # (`https://connection.<region>.keboola.com/`); the Storage API
        # tolerates either form, but normalising here keeps URL composition
        # below predictable.
        self.base = url.rstrip("/") + "/v2/storage"
        self.token = token
        if session is None:
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
        self.session = session

    # ---- low-level HTTP helpers -------------------------------------------

    def _headers(self) -> dict:
        return {"X-StorageApi-Token": self.token, "Accept": "application/json"}

    def _get(self, path: str, **kwargs) -> dict:
        url = f"{self.base}{path}"
        resp = self.session.get(url, headers=self._headers(), timeout=30, **kwargs)
        return self._parse(resp, "GET", url)

    def _post(self, path: str, *, data: Optional[dict] = None) -> dict:
        url = f"{self.base}{path}"
        resp = self.session.post(url, headers=self._headers(), data=data, timeout=30)
        return self._parse(resp, "POST", url)

    def _parse(self, resp: requests.Response, method: str, url: str) -> dict:
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        if resp.status_code >= 400:
            # Redact the token if it accidentally surfaces in an error body.
            # The Storage API doesn't echo it, but third-party proxies in
            # front of customer instances sometimes do.
            redacted = self._redact(body)
            raise StorageApiError(
                f"{method} {url} -> HTTP {resp.status_code}: {redacted}",
                status=resp.status_code,
                body=body,
            )
        if not isinstance(body, dict):
            raise StorageApiError(
                f"{method} {url} -> unexpected non-JSON response: {str(body)[:200]}",
                status=resp.status_code,
                body=body,
            )
        return body

    def _redact(self, body: Any) -> str:
        s = str(body)
        if self.token and self.token in s:
            s = s.replace(self.token, "<redacted-storage-token>")
        return s[:500]

    # ---- export-async + job polling ---------------------------------------

    def export_table_async(self, table_id: str, params: dict) -> dict:
        """POST /v2/storage/tables/{table_id}/export-async — kicks off the
        async export and returns the job resource. Caller polls `job.id`
        via `wait_for_job` to find the file id when status='success'."""
        return self._post(f"/tables/{table_id}/export-async", data=params)

    def get_table_info(self, table_id: str) -> dict:
        """GET /v2/storage/tables/{table_id} — full table metadata.

        Storage API guarantees `rowsCount` + `dataSizeBytes` on success.
        Other fields (`columns`, `primaryKey`, ...) are present but not
        consumed by the metadata provider today. Raises `StorageApiError`
        on 4xx/5xx — caller decides whether to soften to `None`.
        """
        return self._get(f"/tables/{table_id}")

    def verify_token(self) -> dict:
        """GET /v2/storage/tokens/verify — token metadata including
        `isMasterToken`, `bucketPermissions`, `owner`. Used by the
        semantic-layer importer's master-token preflight check (the
        Metastore API requires a master token; see
        connectors/keboola/semantic_layer.py:require_master_token) and by
        the admin table-picker's bucket-scoped-token fallback listing."""
        return self._get("/tokens/verify")

    def get_bucket(self, bucket_id: str) -> dict:
        """GET /v2/storage/buckets/{bucket_id} — single bucket metadata.

        Works with bucket-scoped (custom access) tokens for the buckets they
        can read; the admin table-picker's fallback listing uses it to render
        bucket name/stage when the project-wide ``/buckets`` listing is not
        available to the token."""
        return self._get(f"/buckets/{bucket_id}")

    # ---- discovery: buckets + tables ---------------------------------------
    #
    # Unlike the job/file/export endpoints above (always a JSON object),
    # `/buckets` and `/tables` return a bare JSON array — routed through
    # `_get_list`/`_parse_list` instead of the dict-only `_get`/`_parse`.

    def list_buckets(self) -> List[dict]:
        """GET /v2/storage/buckets — all buckets visible to the token.

        Returns the raw list of bucket dicts (``id``, ``name``, ``stage``,
        ``description``, ...). Used by the admin "browse & register tables"
        UI to group tables for display.
        """
        return self._get_list("/buckets")

    def list_tables(self, bucket_id: Optional[str] = None) -> List[dict]:
        """GET /v2/storage/tables (or `/v2/storage/buckets/{bucket_id}/tables`
        when `bucket_id` is given) — table metadata including nested
        `bucket` info, `rowsCount`, `dataSizeBytes`.
        """
        path = f"/buckets/{bucket_id}/tables" if bucket_id else "/tables"
        return self._get_list(path)

    def _get_list(self, path: str) -> List[dict]:
        url = f"{self.base}{path}"
        resp = self.session.get(url, headers=self._headers(), timeout=30)
        return self._parse_list(resp, "GET", url)

    def _parse_list(self, resp: requests.Response, method: str, url: str) -> List[dict]:
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        if resp.status_code >= 400:
            redacted = self._redact(body)
            raise StorageApiError(
                f"{method} {url} -> HTTP {resp.status_code}: {redacted}",
                status=resp.status_code,
                body=body,
            )
        if not isinstance(body, list):
            raise StorageApiError(
                f"{method} {url} -> unexpected non-list response: {str(body)[:200]}",
                status=resp.status_code,
                body=body,
            )
        return body

    def wait_for_job(
        self,
        job_id: int,
        *,
        timeout: float = _DEFAULT_EXPORT_TIMEOUT_SEC,
        poll_interval: float = _DEFAULT_POLL_INTERVAL_SEC,
    ) -> dict:
        """Block until the async job reaches a terminal state. Returns the
        job dict on success; raises `StorageApiError` on failure or timeout.

        The poll interval starts small and backs off slightly so a chain of
        ~10 fast polls covers a sub-30 s job without flogging the API, while
        a 30-min job ends up at a steady cadence after a few minutes.
        """
        deadline = time.monotonic() + timeout
        interval = poll_interval
        while time.monotonic() < deadline:
            job = self._get(f"/jobs/{job_id}")
            status = job.get("status")
            if status == "success":
                return job
            if status == "error":
                raise StorageApiError(
                    f"Storage API job {job_id} reported error: {job.get('error') or job}",
                    body=job,
                )
            time.sleep(interval)
            # Exponential backoff bounded at 10 s — a multi-minute Snowflake
            # scan does not benefit from sub-second polls. 1.5 multiplier
            # reaches 10 s after ~9 polls (~30 s wall-clock) and stays there.
            interval = min(interval * 1.5, 10.0)
        raise StorageApiError(f"Storage API job {job_id} did not finish within {timeout}s")

    # ---- file detail + signed-URL download --------------------------------

    def file_detail(self, file_id: int) -> dict:
        """GET /v2/storage/files/{file_id}?federationToken=1 — returns the
        file metadata plus a presigned URL (`url`) usable directly via HTTP
        without any cloud SDK. For sliced exports the `url` resolves to a
        manifest JSON listing the per-slice signed URLs."""
        return self._get(f"/files/{file_id}", params={"federationToken": 1})

    def download_file(self, file_info: dict, dest_path: Path) -> Path:
        """Download a Storage API file (single or sliced) to `dest_path`.

        Backend variants:
        - **AWS**: signed HTTPS URL in `file_info["url"]` (S3 presigned).
          Sliced manifest entries are signed HTTPS too. Plain HTTP GET works.
        - **Azure**: ``file_info["url"]`` is a signed HTTPS manifest URL.
          Per-slice URLs in the manifest use the ``azure://`` scheme and
          require a SAS token from ``file_info["absCredentials"]``.
        - **GCP**: `file_info["url"]` is a signed HTTPS URL for the
          single-file case. For sliced exports, the manifest at `url`
          lists per-slice paths as `gs://<bucket>/<key>` (NOT signed) —
          requires GCS authentication. We use the OAuth access token from
          `file_info["gcsCredentials"]["access_token"]` and hit the REST
          endpoint
          `https://storage.googleapis.com/storage/v1/b/<bucket>/o/<urlencoded_key>?alt=media`
          with `Authorization: Bearer <token>`. No google-cloud-storage
          SDK dependency.

        Single-file: stream the signed URL directly, gunzipping if the
        URL/name ends in `.gz`. Sliced: stream each slice into
        `dest_path` in order (slice 0 has the CSV header per Storage
        API contract, subsequent slices are header-less data).
        """
        url = file_info.get("url")
        if not url:
            raise StorageApiError(
                f"file detail missing 'url': {self._redact(file_info)}",
                body=file_info,
            )

        is_sliced = bool(file_info.get("isSliced"))
        # Gzip detection is name-based only. Snowflake UNLOAD adds the
        # `.gz` suffix when compression is requested (CSV exports), and
        # leaves it off otherwise (parquet has its own internal
        # compression and is served as plain `.parquet`). The previous
        # `isEncrypted is False` fallback gated on a property that's
        # orthogonal to compression — it would have flagged parquet
        # downloads as gzipped and corrupted them at gunzip time.
        is_gzipped = file_info.get("name", "").endswith(".gz")

        dest_path.parent.mkdir(parents=True, exist_ok=True)

        if is_sliced:
            # GCP sliced manifests carry `gs://` URIs that need an OAuth
            # bearer; Azure carry `azure://` URIs that need a SAS token
            # from absCredentials; AWS carry signed HTTPS URLs that need
            # no extra auth.
            gcs_token = (file_info.get("gcsCredentials") or {}).get("access_token")
            abs_credentials = file_info.get("absCredentials") or {}
            self._download_sliced(
                url,
                dest_path,
                gcs_token=gcs_token,
                abs_credentials=abs_credentials,
                s3_context=self._s3_context(file_info),
            )
        else:
            self._download_single(url, dest_path, gunzip_on_read=is_gzipped)
        return dest_path

    def _download_single(
        self,
        url: str,
        dest_path: Path,
        *,
        gunzip_on_read: bool,
        extra_headers: Optional[dict] = None,
    ) -> None:
        """Stream a single signed URL (or GCS REST URL with bearer token
        in `extra_headers`) into `dest_path`. Transparently gunzips if
        the file name suggests it's a `.gz` — Storage API serves through
        proxies that may rewrite Content-Encoding, so name-based
        detection is more reliable than the header in practice."""
        with self.session.get(
            url,
            stream=True,
            timeout=_DEFAULT_SLICE_DOWNLOAD_TIMEOUT_SEC,
            headers=extra_headers,
        ) as r:
            r.raise_for_status()

            # Pre-flight disk-space check. Storage API's signed-URL
            # response carries ``Content-Length`` (compressed transfer
            # size for gzipped exports). Demand 5× headroom for gunzip
            # cases (decompressed dest typically 3-5× the wire bytes)
            # and 1.25× otherwise (for the ``.part`` + atomic rename
            # window). Skipping the check when ``Content-Length`` is
            # absent (proxies sometimes strip it) means mid-write
            # ``OSError 28`` is still possible; the common case fails
            # fast with an actionable message instead of leaving an
            # orphan ``.part`` + a half-written destination behind
            # AND triggering the Python traceback retention path that
            # held a multi-GiB response buffer in every retained frame
            # on small dev containers (cascaded into a cgroup OOM via
            # ``connectors/keboola/extractor.py`` consolidation
            # connection — see the matching DuckDB-side cap there).
            content_length = r.headers.get("Content-Length")
            if content_length is not None:
                try:
                    expected_bytes = int(content_length)
                except (TypeError, ValueError):
                    expected_bytes = None
                if expected_bytes is not None and expected_bytes > 0:
                    headroom_mult = 5 if gunzip_on_read else 1.25
                    needed = int(expected_bytes * headroom_mult)
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    free = shutil.disk_usage(dest_path.parent).free
                    if free < needed:
                        raise StorageApiError(
                            f"insufficient disk space at {dest_path.parent}: "
                            f"have {free:,} B free, need >= {needed:,} B "
                            f"({headroom_mult}x the {expected_bytes:,} B "
                            f"download for gunzip_on_read={gunzip_on_read}). "
                            f"Free space before retrying.",
                        )

            tmp = dest_path.with_suffix(dest_path.suffix + ".part")
            try:
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            fh.write(chunk)
                if gunzip_on_read:
                    self._gunzip_in_place(tmp, dest_path)
                    tmp.unlink(missing_ok=True)
                else:
                    tmp.replace(dest_path)
            finally:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)

    @staticmethod
    def _gs_to_https(gs_url: str) -> str:
        """Rewrite `gs://<bucket>/<key>` to GCS JSON API media-download URL.

        The JSON API requires the object name URL-encoded as a single
        path segment (slashes inside the key are escaped). `alt=media`
        switches the response from object metadata JSON to the actual
        bytes — matches what `bucket.blob(key).download_as_bytes()` does
        in the google-cloud-storage SDK.
        """
        from urllib.parse import quote

        if not gs_url.startswith("gs://"):
            raise ValueError(f"_gs_to_https expects gs://; got {gs_url!r}")
        path = gs_url[5:]  # strip "gs://"
        bucket, _, key = path.partition("/")
        if not bucket or not key:
            raise ValueError(f"malformed gs:// URL: {gs_url!r}")
        return f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{quote(key, safe='')}?alt=media"

    @staticmethod
    def _azure_to_https(azure_url: str, abs_credentials: dict) -> str:
        """Rewrite ``azure://<account>.blob.core.windows.net/<container>/<blob>``
        to an HTTPS URL with the SAS token from ``absCredentials`` appended.

        Keboola's Azure-backed projects return ``azure://`` scheme URIs in
        sliced-export manifests instead of pre-signed HTTPS SAS URLs.
        ``absCredentials.SASConnectionString`` carries the SAS token needed
        to authenticate the download — parse it out and append as a query
        string so the standard ``requests`` session can handle the download
        without an Azure SDK dependency.

        If ``absCredentials`` is absent or empty the HTTPS URL is returned
        without a SAS token; the download will likely fail with 403, but the
        schema error is avoided and the caller can surface a cleaner message.
        """
        if not azure_url.startswith("azure://"):
            raise ValueError(f"_azure_to_https expects azure://; got {azure_url!r}")
        https_url = "https://" + azure_url[len("azure://") :]

        # Extract SAS token from SASConnectionString.
        # Format: "BlobEndpoint=https://...;SharedAccessSignature=sv=2020-..."
        sas_connection = abs_credentials.get("SASConnectionString", "") or ""
        sas_token = ""
        for part in sas_connection.split(";"):
            if part.startswith("SharedAccessSignature="):
                sas_token = part[len("SharedAccessSignature=") :]
                break

        if sas_token:
            sep = "&" if "?" in https_url else "?"
            https_url = f"{https_url}{sep}{sas_token}"

        return https_url

    @staticmethod
    def _s3_to_https(s3_url: str, region: str) -> tuple[str, str, str]:
        """Rewrite ``s3://<bucket>/<key>`` to a virtual-hosted-style HTTPS URL.

        Returns ``(https_url, bucket, key)`` so the caller can check the
        bucket against the export's own before handing over credentials.
        AWS stacks that stage exports on S3 list raw ``s3://`` URIs in the
        sliced manifest — unlike the single-file case, which is presigned —
        and ``requests`` has no adapter for that scheme at all.
        """
        if not s3_url.startswith("s3://"):
            raise ValueError(f"_s3_to_https expects s3://; got {s3_url!r}")
        from urllib.parse import quote

        path = s3_url[len("s3://") :]
        bucket, _, key = path.partition("/")
        if not bucket or not key:
            raise ValueError(f"malformed s3:// URL: {s3_url!r}")
        if not region:
            raise StorageApiError(
                f"slice URL is s3:// but the file detail carries no 'region'; "
                f"cannot address the bucket endpoint for {bucket!r}"
            )
        return (
            f"https://{bucket}.s3.{region}.amazonaws.com/{quote(key, safe='/')}",
            bucket,
            key,
        )

    @staticmethod
    def _s3_auth_headers(https_url: str, region: str, credentials: dict) -> dict:
        """SigV4-sign a GET for ``https_url`` and return the auth headers.

        Signing into headers rather than presigning into the query string
        keeps the signature out of URLs (and therefore out of proxy and
        access logs) — the same reason the rest of this codebase refuses
        secrets in query strings.
        """
        if S3SigV4Auth is None:  # pragma: no cover - boto3 is a dependency
            raise StorageApiError(
                "slice URL is s3:// but botocore is unavailable; install the "
                "'boto3' dependency to download AWS-staged sliced exports"
            )
        missing = [k for k in ("AccessKeyId", "SecretAccessKey") if not credentials.get(k)]
        if missing:
            raise StorageApiError(
                f"slice URL is s3:// but file detail credentials are missing {missing}; "
                f"the file detail must be fetched with federationToken=1"
            )
        request = AWSRequest(method="GET", url=https_url)
        S3SigV4Auth(
            Credentials(
                credentials["AccessKeyId"],
                credentials["SecretAccessKey"],
                credentials.get("SessionToken"),
            ),
            "s3",
            region,
        ).add_auth(request)
        return dict(request.headers)

    @staticmethod
    def _slice_scheme(slice_url: str, index: int) -> SliceScheme:
        """Classify one manifest entry, refusing anything not declared.

        Refusing here is the point: an unrecognized scheme reaching
        ``requests`` dies as "No connection adapters were found" with no
        mention of the URI, several layers from the manifest that carried
        it.
        """
        scheme, separator, _rest = slice_url.partition("://")
        if not separator:
            raise StorageApiError(f"slice {index} URL has no scheme, cannot be fetched: {slice_url!r}")
        # Plain http is treated as presigned too: some stacks front their
        # object store with an internal endpoint, and rejecting it here
        # would break a path that works today.
        if scheme in ("http", "https"):
            return SliceScheme.HTTPS_PRESIGNED
        try:
            return SliceScheme(scheme)
        except ValueError:
            raise StorageApiError(
                f"slice {index} uses unsupported scheme {scheme + '://'!r}; "
                f"this build handles {', '.join(s.value + '://' for s in SliceScheme)}"
            ) from None

    @classmethod
    def _prepare_slice_request(
        cls,
        slice_url: str,
        index: int,
        *,
        gcs_token: Optional[str],
        abs_credentials: dict,
        s3_context: dict,
    ) -> tuple[str, Optional[dict]]:
        """Map one manifest entry onto ``(url, extra_headers)`` to fetch.

        The single scheme dispatch for both sliced entry points *and* the
        legacy SDK client, so a backend fixed in one is fixed in all three:
        - GCP: ``gs://`` → GCS REST + OAuth bearer from ``gcsCredentials``
        - Azure: ``azure://`` → HTTPS + SAS token from ``absCredentials``
        - AWS: ``s3://`` → virtual-hosted HTTPS + SigV4 headers from
          ``credentials``; already-signed HTTPS passes through untouched.

        A classmethod because the legacy client reaches it without holding a
        Storage API client of its own.
        """
        scheme = cls._slice_scheme(slice_url, index)
        if scheme is SliceScheme.GS:
            if not gcs_token:
                raise StorageApiError(
                    f"slice {index} URL is gs:// but no gcs_token provided in file_detail.gcsCredentials"
                )
            return cls._gs_to_https(slice_url), {"Authorization": f"Bearer {gcs_token}"}
        elif scheme is SliceScheme.AZURE:
            return cls._azure_to_https(slice_url, abs_credentials), None
        elif scheme is SliceScheme.S3:
            return cls._s3_slice_request(slice_url, index, s3_context)
        elif scheme is SliceScheme.HTTPS_PRESIGNED:
            return slice_url, None
        else:
            assert_never(scheme)

    @classmethod
    def _s3_slice_request(cls, s3_url: str, index: int, s3_context: dict) -> tuple[str, dict]:
        """Rewrite + sign one ``s3://`` slice: ``(https_url, auth_headers)``.

        Split out of ``_prepare_slice_request`` so the legacy SDK client
        (``connectors/keboola/client.py``, still the download path for
        incremental and partitioned syncs) signs AWS slices through this
        exact code rather than growing a second implementation — the bucket
        check and header signing below are the security-relevant half.
        """
        region = s3_context.get("region") or ""
        https_url, bucket, _key = cls._s3_to_https(s3_url, region)
        expected = (s3_context.get("s3Path") or {}).get("bucket")
        # The manifest is fetched from Keboola, but its contents are not
        # ours to trust blindly: a slice pointing at some other bucket
        # would aim the export's credentials at a target the export does
        # not cover. Refuse rather than sign.
        if expected and bucket != expected:
            raise StorageApiError(
                f"slice {index} names bucket {bucket!r} but the export lives in "
                f"{expected!r}; refusing to sign a request outside the export"
            )
        headers = cls._s3_auth_headers(https_url, region, s3_context.get("credentials") or {})
        return https_url, headers

    @staticmethod
    def _s3_context(file_info: dict) -> dict:
        """Pull the non-secret + credential bits the s3:// branch needs."""
        return {
            "region": file_info.get("region") or "",
            "s3Path": file_info.get("s3Path") or {},
            "credentials": file_info.get("credentials") or {},
        }

    def _download_sliced(
        self,
        manifest_url: str,
        dest_path: Path,
        *,
        gcs_token: Optional[str] = None,
        abs_credentials: Optional[dict] = None,
        s3_context: Optional[dict] = None,
    ) -> None:
        """Sliced exports: the file detail's `url` points at a JSON manifest
        whose `entries[].url` lists per-slice locations. Download each slice
        and concatenate into `dest_path`. The first slice contains the CSV
        header (Storage API guarantees stable header positioning) — entries
        are re-sorted by `_slice_sort_key` before download since the
        manifest's own array order is not independently guaranteed (see
        that function's docstring).

        Per-slice URL forms:
        - signed HTTPS (S3 presigned, Azure SAS) — plain GET works.
        - `gs://<bucket>/<key>` (GCP) — requires `gcs_token` (OAuth bearer
          shipped in the file_detail's `gcsCredentials.access_token`).
          Mapped to `https://storage.googleapis.com/storage/v1/b/<bucket>/o/<encoded_key>?alt=media`.
        """
        m = self.session.get(manifest_url, timeout=_DEFAULT_SLICE_DOWNLOAD_TIMEOUT_SEC)
        m.raise_for_status()
        manifest = m.json()
        entries = manifest.get("entries") or []
        if not entries:
            raise StorageApiError(
                f"sliced manifest had no entries: {str(manifest)[:200]}",
                body=manifest,
            )
        sorted_entries = sorted(entries, key=lambda e: _slice_sort_key(e.get("url") or ""))
        if sorted_entries != entries:
            logger.warning(
                "Sliced export manifest entries were not in URL-sorted order "
                "(%d entries); re-sorted before download so the header slice "
                "downloads first.",
                len(entries),
            )
        entries = sorted_entries

        with tempfile.TemporaryDirectory(
            prefix="kbc-slice-",
            dir=get_temp_root(),
            ignore_cleanup_errors=True,
        ) as tmpdir:
            slice_paths: List[Path] = []
            for i, entry in enumerate(entries):
                surl = entry.get("url")
                if not surl:
                    raise StorageApiError(
                        f"slice {i} missing 'url': {str(entry)[:200]}",
                        body=entry,
                    )
                sp = Path(tmpdir) / f"slice-{i:05d}"
                surl, extra_headers = self._prepare_slice_request(
                    surl,
                    i,
                    gcs_token=gcs_token,
                    abs_credentials=abs_credentials or {},
                    s3_context=s3_context or {},
                )
                # Slices may individually be gzipped — same heuristic as
                # single-file: if the slice URL's path ends in `.gz`, gunzip
                # after download.
                gz = ".gz" in surl.split("?")[0].rsplit("/", 1)[-1]
                self._download_single(
                    surl,
                    sp,
                    gunzip_on_read=gz,
                    extra_headers=extra_headers,
                )
                slice_paths.append(sp)

            # Concat. Sliced CSV exports include the header in slice 0 only
            # (Storage API contract); subsequent slices are header-less.
            with open(dest_path, "wb") as out:
                for sp in slice_paths:
                    with open(sp, "rb") as fh:
                        shutil.copyfileobj(fh, out, length=64 * 1024)

    @staticmethod
    def _gunzip_in_place(src: Path, dest: Path) -> None:
        with gzip.open(src, "rb") as gz, open(dest, "wb") as out:
            shutil.copyfileobj(gz, out, length=64 * 1024)

    # ---- high-level: export-async + poll, returning file metadata ---------

    def prepare_export(
        self,
        table_id: str,
        *,
        export_filter: Optional[ExportFilter] = None,
        export_timeout: float = _DEFAULT_EXPORT_TIMEOUT_SEC,
    ) -> dict:
        """Run export-async + wait_for_job + file_detail and return the
        file metadata. Caller decides how to download (single vs
        sliced) — needed for the parquet path where sliced output must
        be downloaded slice-by-slice and then DuckDB-merged (cat-style
        concat would corrupt the per-slice parquet footers).

        Returns:
            {"job_id": int, "file_id": int, "rows": int|None,
             "file_info": dict, "file_type": str}
        """
        f = export_filter or ExportFilter()
        params = f.to_export_params()
        job_resp = self.export_table_async(table_id, params)
        job_id = job_resp.get("id")
        if not job_id:
            raise StorageApiError(
                f"export-async response missing job id: {self._redact(job_resp)}",
                body=job_resp,
            )
        job = self.wait_for_job(job_id, timeout=export_timeout)
        results = job.get("results") or {}
        file_id = (results.get("file") or {}).get("id") or results.get("fileId")
        if not file_id:
            raise StorageApiError(
                f"job {job_id} succeeded but had no result file: {self._redact(job)}",
                body=job,
            )
        file_info = self.file_detail(file_id)
        return {
            "job_id": int(job_id),
            "file_id": int(file_id),
            "rows": (results.get("totalRowsCount") or results.get("rowsCount") or job.get("totalRowsCount")),
            "file_info": file_info,
            "file_type": f.file_type,
        }

    def download_file_slices(self, file_info: dict, dest_dir: Path) -> List[Path]:
        """Download a sliced Storage API export as separate per-slice
        files into ``dest_dir``. Returns the slice paths in manifest
        order. Use when the slices must be processed individually
        (e.g. parquet — each slice is a complete parquet file with its
        own footer; concatenation would invalidate it). For CSV where
        concat-with-header-only-on-first-slice is the right thing,
        ``download_file`` is the correct entry point.
        """
        url = file_info.get("url")
        if not url:
            raise StorageApiError(
                f"file detail missing 'url': {self._redact(file_info)}",
                body=file_info,
            )
        if not file_info.get("isSliced"):
            raise StorageApiError(
                "download_file_slices called on a non-sliced file_info; use download_file for the single-file case"
            )
        gcs_token = (file_info.get("gcsCredentials") or {}).get("access_token")
        abs_credentials = file_info.get("absCredentials") or {}
        m = self.session.get(url, timeout=_DEFAULT_SLICE_DOWNLOAD_TIMEOUT_SEC)
        m.raise_for_status()
        manifest = m.json()
        entries = manifest.get("entries") or []
        if not entries:
            raise StorageApiError(
                f"sliced manifest had no entries: {str(manifest)[:200]}",
                body=manifest,
            )
        dest_dir.mkdir(parents=True, exist_ok=True)
        slice_paths: List[Path] = []
        for i, entry in enumerate(entries):
            surl = entry.get("url")
            if not surl:
                raise StorageApiError(
                    f"slice {i} missing 'url': {str(entry)[:200]}",
                    body=entry,
                )
            surl, extra_headers = self._prepare_slice_request(
                surl,
                i,
                gcs_token=gcs_token,
                abs_credentials=abs_credentials,
                s3_context=self._s3_context(file_info),
            )
            gz = ".gz" in surl.split("?")[0].rsplit("/", 1)[-1]
            sp = dest_dir / f"slice-{i:05d}"
            self._download_single(
                surl,
                sp,
                gunzip_on_read=gz,
                extra_headers=extra_headers,
            )
            slice_paths.append(sp)
        return slice_paths

    # ---- high-level: export to local file (csv or parquet) ----------------

    def export_table(
        self,
        table_id: str,
        dest_path: Path,
        *,
        export_filter: Optional[ExportFilter] = None,
        export_timeout: float = _DEFAULT_EXPORT_TIMEOUT_SEC,
    ) -> dict:
        """End-to-end: export-async → poll → download to ``dest_path``.

        ``export_filter.file_type`` controls the format Storage API
        materializes (``csv`` default, ``parquet`` when explicitly set).
        ``dest_path`` is the local file we write the bytes to; the caller
        decides the extension. The downloader streams chunks to disk so
        memory stays bounded regardless of file size.

        For CSV the sliced case is handled transparently — slices are
        concatenated into ``dest_path`` (header in slice 0 only). For
        **sliced parquet**, callers must use ``prepare_export`` +
        ``download_file_slices`` instead — concatenating parquet slices
        invalidates the per-slice footer. ``export_table`` will raise
        StorageApiError if it sees a sliced parquet, to fail loud.

        Returns a small stats dict so callers can log / record provenance:
            {"job_id": int, "file_id": int, "rows": int|None, "bytes": int,
             "file_type": str}
        """
        prep = self.prepare_export(
            table_id,
            export_filter=export_filter,
            export_timeout=export_timeout,
        )
        file_info = prep["file_info"]
        if prep["file_type"] == FILE_TYPE_PARQUET and file_info.get("isSliced"):
            raise StorageApiError(
                f"sliced parquet export for {table_id}: use "
                f"prepare_export + download_file_slices and merge with "
                f"DuckDB COPY (concat would corrupt parquet footers)",
                body=file_info,
            )
        self.download_file(file_info, dest_path)
        size = dest_path.stat().st_size if dest_path.exists() else 0
        return {
            "job_id": prep["job_id"],
            "file_id": prep["file_id"],
            "rows": prep["rows"],
            "bytes": size,
            "file_type": prep["file_type"],
        }

    # Backwards-compat alias retained for external callers (e.g. ad-hoc
    # scripts) that imported the old name. The behavior matches calling
    # `export_table` with whatever `file_type` the export_filter carries
    # — the *_to_csv suffix is now imprecise (Storage API can also serve
    # parquet here), but renaming the import would force unrelated repos
    # to coordinate. Prefer `export_table` in new code.
    def export_table_to_csv(
        self,
        table_id: str,
        dest_csv: Path,
        *,
        export_filter: Optional[ExportFilter] = None,
        export_timeout: float = _DEFAULT_EXPORT_TIMEOUT_SEC,
    ) -> dict:
        return self.export_table(
            table_id,
            dest_csv,
            export_filter=export_filter,
            export_timeout=export_timeout,
        )
