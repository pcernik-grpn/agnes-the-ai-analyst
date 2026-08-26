"""Single entry point for BigQuery access — config resolution, client construction,
DuckDB-extension session, and Google-API error translation.

See docs/archive/superpowers/specs/2026-04-29-issue-134-bq-access-unify-design.md
for the full design rationale.
"""

from __future__ import annotations

import functools
import logging
import threading
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Iterator, List, Literal

if TYPE_CHECKING:
    import pyarrow as pa  # noqa: F401

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BqProjects:
    """Pair of GCP project IDs used by Agnes.

    `billing` is the project the BQ client bills jobs to (also used as quota_project_id).
    `data` is the default data project for FROM-clause construction. Today equal to
    instance.yaml `data_source.bigquery.project`; locked to a single project per instance
    until table_registry grows a per-table source_project column. See spec "Non-goals".
    """

    billing: str
    data: str


class BqAccessError(Exception):
    """Typed error for BQ access failures.

    `kind` is one of HTTP_STATUS keys; endpoint translation maps it to status codes.
    """

    HTTP_STATUS = {
        "not_configured": 500,  # admin/config bug — page on-call
        "bq_lib_missing": 500,  # deployment bug
        "auth_failed": 502,  # GCP metadata server unreachable
        "cross_project_forbidden": 502,  # SA lacks serviceusage.services.use on billing project
        "bq_forbidden": 502,  # other Forbidden from BQ
        "bq_bad_request": 400,  # 400 from BQ when caller flagged it as client-derived
        "bq_upstream_error": 502,  # all other upstream BQ failures
        # `responseTooLarge` is a BQ refusal whose root cause is query shape
        # (the user asked for too many rows back inline), not auth or syntax.
        # 400 with a specific actionable hint instead of the generic
        # bq_bad_request / bq_upstream_error mappings, which surfaced the
        # raw BQ message and gave operators no path forward.
        "bq_response_too_large": 400,
    }

    def __init__(self, kind: str, message: str, details: dict | None = None):
        self.kind = kind
        self.message = message
        self.details = details or {}
        super().__init__(message)


_RESPONSE_TOO_LARGE_HINT = (
    "BigQuery refused to return the result inline; the query exceeded BQ's "
    "response size limit. Narrow the WHERE clause, aggregate further, "
    "select fewer columns, or query a materialized table that's already "
    "been bounded server-side."
)


def _classify_response_too_large(msg: str, projects: BqProjects) -> BqAccessError:
    """Build the `bq_response_too_large` BqAccessError with the canonical
    actionable hint and the original BQ message preserved in details for
    operator debugging."""
    return BqAccessError(
        "bq_response_too_large",
        _RESPONSE_TOO_LARGE_HINT,
        details={
            "original": msg,
            "billing_project": projects.billing,
            "data_project": projects.data,
        },
    )


def _is_response_too_large(msg: str) -> bool:
    """Detect BQ's `responseTooLarge` failure mode by message substring.

    The reason code is stable across HTTP transports (gax.BadRequest from
    google-cloud-bigquery, duckdb.IOException from the BQ extension's own
    HTTP layer); both surface 'Response too large to return' verbatim in
    the message body. Match case-insensitively + tolerate the slight
    variant 'response too large' that some surfaces emit without the
    'to return' suffix.
    """
    ml = msg.lower()
    return "response too large" in ml


def translate_bq_error(
    e: Exception,
    projects: BqProjects,
    *,
    bad_request_status: Literal["client_error", "upstream_error"],
) -> BqAccessError:
    """Convert Google API exceptions into a typed BqAccessError.

    Mapping (FIRST match wins):
      1. BqAccessError                    -> pass through unchanged (CRITICAL: bq.client()
                                             and bq.duckdb_session() can raise BqAccessError
                                             directly for bq_lib_missing / auth_failed; those
                                             must round-trip without reclassification)
      2. Forbidden + 'serviceusage' in str(e).lower()
                                          -> cross_project_forbidden (with hint)
      3. Forbidden                        -> bq_forbidden
      4. 'response too large' in str(e).lower()
                                          -> bq_response_too_large (HTTP 400, with
                                             actionable hint pointing at WHERE /
                                             aggregate / materialized remediations)
      5. BadRequest, bad_request_status='client_error'
                                          -> bq_bad_request (HTTP 400)
      6. BadRequest, bad_request_status='upstream_error'
                                          -> bq_upstream_error (HTTP 502)
      7. GoogleAPICallError (other)       -> bq_upstream_error
      8. 'not found' / 'notfound' in str(e).lower()
                                          -> bq_upstream_error (HTTP 502); BQ
                                             reason=notFound — referenced dataset/
                                             table/job missing or location mismatch.
                                             NB: 'does not exist' is NOT matched
                                             (DuckDB-native catalog wording → re-raise).
      9. Anything else                    -> RE-RAISED unchanged (don't swallow programmer errors)

    The `responseTooLarge` mapping (4) sits ahead of the generic BadRequest
    cases on purpose: BQ surfaces this failure mode as a 400 with a
    specific reason, but the actionable remediation is "shape your query
    differently" — not "your SQL has a syntax error" (the typical
    bq_bad_request user-facing meaning) and not "BQ is broken"
    (bq_upstream_error). Routing it via its own kind keeps the user-facing
    message tight + correct.
    """
    if isinstance(e, BqAccessError):
        return e

    try:
        from google.api_core import exceptions as gax  # type: ignore
    except ImportError:
        # No google lib installed → can't classify Google errors. Re-raise.
        raise e

    msg = str(e)

    if isinstance(e, gax.Forbidden):
        if "serviceusage" in msg.lower():
            return BqAccessError(
                "cross_project_forbidden",
                msg,
                details={
                    "billing_project": projects.billing,
                    "data_project": projects.data,
                    "hint": (
                        "Set data_source.bigquery.billing_project in instance.yaml to a project "
                        "where the SA has serviceusage.services.use, or grant the SA that role "
                        "on the data project."
                    ),
                },
            )
        return BqAccessError(
            "bq_forbidden",
            msg,
            details={"billing_project": projects.billing, "data_project": projects.data},
        )

    # Special-case: `responseTooLarge` arrives as gax.BadRequest (HTTP 400)
    # but has a unique reason code with a specific, actionable remediation.
    # Catch it BEFORE the generic BadRequest mapping below so it doesn't
    # surface as a confusing "bad request" (which implies bad SQL).
    if _is_response_too_large(msg):
        return _classify_response_too_large(msg, projects)

    if isinstance(e, gax.BadRequest):
        if bad_request_status == "client_error":
            return BqAccessError("bq_bad_request", msg)
        return BqAccessError("bq_upstream_error", msg)

    if isinstance(e, gax.GoogleAPICallError):
        return BqAccessError("bq_upstream_error", msg)

    # Last-resort heuristic: the DuckDB bigquery extension is a C++ plugin that
    # makes its own HTTP calls (not via google-cloud-bigquery), so BQ HTTP errors
    # arrive as DuckDB-native exceptions (e.g. duckdb.IOException) rather than
    # google.api_core types — `bigquery_query()` paths in v2_scan/sample/schema
    # would otherwise fall through to the re-raise below and surface as bare 500
    # in production. String-match common BQ HTTP error patterns. Devin ANALYSIS
    # on PR #138 review.
    msg_lower = msg.lower()
    if "forbidden" in msg_lower or " 403 " in msg_lower or "403:" in msg_lower:
        if "serviceusage" in msg_lower:
            return BqAccessError(
                "cross_project_forbidden",
                msg,
                details={
                    "billing_project": projects.billing,
                    "data_project": projects.data,
                    "hint": (
                        "Set data_source.bigquery.billing_project in instance.yaml to a project "
                        "where the SA has serviceusage.services.use, or grant the SA that role "
                        "on the data project."
                    ),
                },
            )
        return BqAccessError(
            "bq_forbidden",
            msg,
            details={"billing_project": projects.billing, "data_project": projects.data},
        )
    if "bad request" in msg_lower or " 400 " in msg_lower or "400:" in msg_lower:
        if bad_request_status == "client_error":
            return BqAccessError("bq_bad_request", msg)
        return BqAccessError("bq_upstream_error", msg)

    # BQ reason=notFound (HTTP 404): a referenced dataset / table / job doesn't
    # exist, or the request location doesn't match the resource's. Documented
    # wording always carries the 'Not found:' prefix; the bare reason token
    # surfaces as '[notFound]' (camelCase, no space) on some transports — match
    # both. Pre-fix these fell through to the re-raise below and surfaced as a
    # bare HTTP 500 on /api/v2/sample (FAI-22). NB: deliberately NOT matching
    # 'does not exist' (that's DuckDB-native catalog wording — let those
    # re-raise as genuine bugs).
    if "not found" in msg_lower or "notfound" in msg_lower:
        return BqAccessError("bq_upstream_error", msg)

    # Don't swallow programmer errors / unknown exceptions
    raise e


def bq_query_parameters_from_policy_params(params: dict) -> list:
    """Build ``google.cloud.bigquery`` ``QueryParameter`` objects from a
    :class:`src.access_policy.PoliciedRelation` ``.params`` dict (§6.2's
    three identity values, §7.1) for ``run_bq_query_to_arrow``'s
    ``query_parameters`` argument.

    ``user_email`` / ``user_id`` bind as scalar ``STRING`` parameters;
    ``user_groups`` (a Python ``list``) binds as an ``ARRAY<STRING>``
    parameter — the shape ``list_contains($user_groups, col)`` transpiles
    to (``EXISTS(SELECT 1 FROM UNNEST(@user_groups) ...)``, §6.5, §7.2)
    expects. Values are never interpolated into SQL text — this is the
    ONLY place a policy's bound values become part of the BigQuery request,
    and they travel as typed job-config parameters, exactly like the
    DuckDB bind path (§6.2).
    """
    from google.cloud import bigquery  # noqa: PLC0415

    query_parameters = []
    for name, value in params.items():
        if isinstance(value, (list, tuple, set)):
            query_parameters.append(bigquery.ArrayQueryParameter(name, "STRING", list(value)))
        else:
            query_parameters.append(bigquery.ScalarQueryParameter(name, "STRING", value))
    return query_parameters


def run_bq_query_to_arrow(
    bq: "BqAccess",
    sql: str,
    *,
    labels: dict[str, str] | None = None,
    query_parameters: list | None = None,
) -> tuple["pa.Table", dict]:
    """Run a billable BigQuery query via ``google-cloud-bigquery``
    ``client.query(labels=...)`` and return ``(arrow_table, job_info)``.

    The billable execution on the scan / remote-select paths would otherwise run
    through the DuckDB ``bigquery_query()`` extension, which owns the job config
    and exposes **no** label hook and **no** job id — so those jobs carry no
    cost-attribution labels (#752). Routing a fully-materialized result through
    ``client.query`` instead is shape-equivalent (the extension materializes the
    result into DuckDB anyway) and lets the job carry ``labels`` and surface its
    metadata. Mirrors the labeled-job + Storage-Read-API-fallback shape of
    ``src.remote_query.register_bq`` (#751) and ``v2_scan._run_bq_scan``.

    ``query_parameters`` — a list of ``bigquery.ScalarQueryParameter`` /
    ``bigquery.ArrayQueryParameter`` (build via
    ``bq_query_parameters_from_policy_params``) — threads named ``@name``
    binds into the job config (table access policies design §7.1): the
    DuckDB ``bigquery_query()`` extension's dollar-quoted string-literal
    payload has no bind mechanism at all, which is why a policied
    ``query_mode='remote'`` table's transpiled policy runs through THIS
    function instead of that extension.

    ``job_info`` has keys ``bq_job_id`` / ``bytes_scanned`` / ``bytes_billed``
    for the caller's audit log. SQL here is user-derived → BadRequest → 400
    (``bad_request_status="client_error"``).
    """
    from google.cloud import bigquery  # noqa: PLC0415

    client = bq.client()  # raises BqAccessError(bq_lib_missing/auth_failed) — propagates
    try:
        job = client.query(
            sql,
            job_config=bigquery.QueryJobConfig(labels=labels or {}, query_parameters=query_parameters or []),
        )
        try:
            table = job.to_arrow()
        except Exception as storage_exc:
            # Some SAs lack BQ Storage Read API access; fall back to the slower
            # REST-based fetch rather than failing the whole query (mirrors
            # register_bq / _run_bq_scan).
            if "readsessions" in str(storage_exc) or "PERMISSION_DENIED" in str(storage_exc):
                logger.warning("BQ Storage API unavailable, falling back to REST")
                table = job.to_arrow(create_bqstorage_client=False)
            else:
                raise
    except Exception as e:
        raise translate_bq_error(e, bq.projects, bad_request_status="client_error")

    job_info = {
        "bq_job_id": getattr(job, "job_id", None),
        "bytes_scanned": getattr(job, "total_bytes_processed", None),
        "bytes_billed": getattr(job, "total_bytes_billed", None),
    }
    return table, job_info


def _vault_credentials():
    """Return google-auth Credentials from the vault SA JSON, or None if absent.

    Mirrors the same vault path used by ``connectors.bigquery.auth._fetch_vault_sa_token``
    so the Python ``bigquery.Client`` path honours vault keys the same way as the
    DuckDB BQ extension path.  Returns None silently when the vault entry is missing.
    """
    try:
        from app.datasource_secrets import datasource_secret  # noqa: PLC0415
        from google.oauth2 import service_account  # type: ignore  # noqa: PLC0415
        from google.auth.transport.requests import Request as _GAuthRequest  # type: ignore  # noqa: PLC0415

        sa_json = datasource_secret("BIGQUERY_SERVICE_ACCOUNT_JSON")
        if not sa_json:
            return None
        import json as _json  # noqa: PLC0415

        info = _json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        creds.refresh(_GAuthRequest())
        return creds
    except Exception:
        return None


def _default_client_factory(projects: BqProjects):
    """Real BigQuery client construction. Raises BqAccessError on import / auth / config issues.

    Resolution order matches ``connectors.bigquery.auth.get_metadata_token``:
    1. ``GOOGLE_APPLICATION_CREDENTIALS`` / ADC — resolved by ``bigquery.Client()`` default.
    2. Vault ``BIGQUERY_SERVICE_ACCOUNT_JSON`` — tried when ADC fails, so operators who
       store a SA JSON via the admin UI get full Python-client coverage (discover_tables,
       test-connection) in addition to the DuckDB-extension path.

    ``bigquery.Client(...)`` resolves Application Default Credentials at construction
    time; in environments without ADC (CI without service-account key, dev laptop
    that hasn't run `gcloud auth application-default login`) it raises
    ``google.auth.exceptions.DefaultCredentialsError`` synchronously. Translate to
    typed ``BqAccessError(auth_failed)`` so endpoints surface a structured 502 with
    a helpful hint instead of a raw stack trace.
    """
    try:
        from google.cloud import bigquery  # type: ignore
        from google.api_core.client_options import ClientOptions  # type: ignore
    except ImportError as e:
        raise BqAccessError(
            "bq_lib_missing",
            "google-cloud-bigquery is not installed",
            details={"original": str(e)},
        )

    try:
        from google.auth import exceptions as gauth_exc  # type: ignore

        auth_error_types: tuple = (gauth_exc.DefaultCredentialsError,)
    except ImportError:
        auth_error_types = ()

    client_opts = ClientOptions(quota_project_id=projects.billing)
    try:
        return bigquery.Client(
            project=projects.billing,
            client_options=client_opts,
        )
    except auth_error_types:
        pass  # ADC unavailable — try vault next

    vault_creds = _vault_credentials()
    if vault_creds is not None:
        logger.info("BQ Python client: using vault BIGQUERY_SERVICE_ACCOUNT_JSON")
        return bigquery.Client(
            project=projects.billing,
            credentials=vault_creds,
            client_options=client_opts,
        )

    raise BqAccessError(
        "auth_failed",
        "GCP credentials unavailable: no ADC and no vault BIGQUERY_SERVICE_ACCOUNT_JSON",
        details={
            "hint": (
                "Run `gcloud auth application-default login` for local dev, set "
                "GOOGLE_APPLICATION_CREDENTIALS to a service-account key, or paste "
                "a service-account JSON via Admin → Datasource Credentials."
            ),
        },
    )


def _default_pool_size() -> int:
    """Resolve the BQ DuckDB-extension session pool size from instance.yaml.

    Reads ``data_source.bigquery.session_pool_size`` (default 4). Sentinel
    ``0`` disables pooling (every acquire builds + closes a fresh session;
    matches pre-pool behavior). Negative / non-numeric values fall back to
    the default — the pool is a perf optimization, not a correctness
    boundary, so an unparseable config shouldn't fail-stop the app.
    """
    try:
        from app.instance_config import get_value
    except Exception:
        return 4
    raw = get_value("data_source", "bigquery", "session_pool_size", default=4)
    try:
        n = int(raw) if raw is not None else 4
    except (TypeError, ValueError):
        logger.warning(
            "BQ session_pool_size=%r is not an int; falling back to default 4",
            raw,
        )
        return 4
    if n < 0:
        return 4
    return n


def _build_fresh_bq_session():
    """Build a single fresh in-memory DuckDB conn with the bigquery extension
    INSTALL/LOAD'd, the auth SECRET created from get_metadata_token(), and
    per-session settings applied. Translates auth / install failures to
    BqAccessError. Caller owns the close.

    Used internally by the pool; also used directly when pooling is disabled.
    """
    from connectors.bigquery.auth import get_metadata_token, BQMetadataAuthError
    from src.duckdb_conn import _open_duckdb

    try:
        token = get_metadata_token()
    except BQMetadataAuthError as e:
        raise BqAccessError(
            "auth_failed",
            f"could not fetch GCP metadata token: {e}",
            details={"original": str(e)},
        )

    conn = _open_duckdb(":memory:")
    try:
        conn.execute("INSTALL bigquery FROM community; LOAD bigquery;")
        escaped = token.replace("'", "''")
        conn.execute(f"CREATE OR REPLACE SECRET bq_s (TYPE bigquery, ACCESS_TOKEN '{escaped}')")
    except Exception as e:
        # Build failed — must close the half-initialised conn, otherwise it
        # leaks across the pool's lifetime.
        try:
            conn.close()
        except Exception:
            pass
        raise BqAccessError(
            "bq_lib_missing",
            f"failed to install/load BigQuery DuckDB extension: {e}",
            details={"original": str(e)},
        )
    apply_bq_session_settings(conn)
    return conn


def _refresh_bq_secret(conn) -> None:
    """Refresh the auth SECRET on a pooled connection so token rotation
    (default GCE metadata token TTL ~1 hr) doesn't break long-lived
    pooled entries.

    Cheap when the token cache is warm (a few µs). Failures are
    non-fatal here — the pool's liveness probe + per-acquire build
    fallback will catch genuinely-broken entries.
    """
    from connectors.bigquery.auth import get_metadata_token

    try:
        token = get_metadata_token()
        escaped = token.replace("'", "''")
        conn.execute(f"CREATE OR REPLACE SECRET bq_s (TYPE bigquery, ACCESS_TOKEN '{escaped}')")
    except Exception as e:
        # Bubble up so the pool drops this entry and rebuilds.
        raise BqAccessError(
            "auth_failed",
            f"could not refresh BQ secret on pooled session: {e}",
            details={"original": str(e)},
        )


def _is_pool_entry_alive(conn) -> bool:
    """Cheap liveness probe — `SELECT 1`. Returns False on any error so
    the pool reaper drops the entry and builds a fresh one."""
    try:
        result = conn.execute("SELECT 1").fetchone()
        return result is not None and result[0] == 1
    except Exception:
        return False


# Module-level pool state. Process-cached (mirrors get_bq_access's lifetime).
# Not fork-safe — single uvicorn worker process is the supported deployment
# shape per CLAUDE.md.
_pool: deque = deque()
_pool_lock = threading.Lock()


def _reset_session_pool_for_tests() -> None:
    """Drop and close every pooled entry. Test helper — production code
    should not call this. Exposed so test fixtures + the existing
    test_bq_access tests can pin pre-test pool state to empty."""
    with _pool_lock:
        while _pool:
            entry = _pool.popleft()
            try:
                entry.close()
            except Exception:
                pass


@contextmanager
def _default_duckdb_session_factory(projects: BqProjects):
    """Yield a pooled in-memory DuckDB conn with bigquery extension loaded
    + SECRET set from get_metadata_token(). Translates auth / install
    failures to BqAccessError(kind='auth_failed' or 'bq_lib_missing').

    Pooling: amortizes the ~0.5 s INSTALL/LOAD/ATTACH cost across requests
    by keeping pre-warmed connections in a bounded deque. Acquire reuses
    an existing entry when available (refreshing its auth SECRET so
    token rotation doesn't break long-lived entries) and probes liveness
    cheaply via ``SELECT 1`` before handing it to the caller. On normal
    exit the connection returns to the pool; on exception it's closed
    instead (the underlying session may carry dirty state).

    Pool size is ``data_source.bigquery.session_pool_size`` (default 4;
    sentinel ``0`` disables pooling entirely, matching pre-pool
    behavior). Process-cached, not fork-safe.

    Note: `projects.billing` is not used by this factory directly — bigquery_query()
    callers pass it themselves as the first positional arg to identify the billing
    project. The factory keeps the parameter for symmetry with _default_client_factory.
    """
    pool_size = _default_pool_size()

    # Acquire: prefer a warm entry, fall back to fresh build.
    conn = None
    if pool_size > 0:
        while True:
            with _pool_lock:
                entry = _pool.popleft() if _pool else None
            if entry is None:
                break
            if not _is_pool_entry_alive(entry):
                # Reaper: drop broken entries.
                try:
                    entry.close()
                except Exception:
                    pass
                continue
            try:
                # Refresh the auth SECRET so a long-lived pool entry
                # doesn't keep a stale token past its TTL. Cheap when
                # the token cache is warm.
                _refresh_bq_secret(entry)
            except BqAccessError:
                try:
                    entry.close()
                except Exception:
                    pass
                continue
            # Re-apply session settings (`bq_query_timeout_ms`, …) on
            # every reuse so an operator's `/admin/server-config` change
            # propagates to pooled entries without requiring container
            # restart. Without this, a long-lived pool entry keeps the
            # value baked in at first build forever (devil's-advocate
            # R1 finding #3). `apply_bq_session_settings` is idempotent
            # and fail-soft — re-running on every acquire is cheap.
            try:
                apply_bq_session_settings(entry)
            except Exception:
                # apply_bq_session_settings is documented as never
                # raising for legitimate "extension doesn't recognise
                # setting" cases (it only logs). Defensive guard for
                # any unforeseen failure mode — keep the entry, the
                # caller's actual query may still succeed.
                pass
            conn = entry
            break

    if conn is None:
        conn = _build_fresh_bq_session()

    try:
        yield conn
    except Exception:
        # Caller saw an exception — the conn may be in a dirty state.
        # Don't return to pool; close to release native resources.
        try:
            conn.close()
        except Exception:
            pass
        raise
    else:
        # Normal exit — return to pool if there's room.
        if pool_size > 0:
            with _pool_lock:
                if len(_pool) < pool_size:
                    _pool.append(conn)
                    return
        # Pool disabled or full — close.
        try:
            conn.close()
        except Exception:
            pass


def apply_bq_session_settings(conn) -> None:
    """Apply per-session DuckDB BigQuery-extension settings from instance config.

    Two unrelated concerns share this function because both must fire on
    every BQ session — initial creation AND every pool acquire (see the
    re-apply call from ``_default_duckdb_session_factory``'s acquire path):

    1. **Core DuckDB resource caps (unconditional)** — ``memory_limit=2GB``,
       ``threads=2``, ``preserve_insertion_order=false``. The default
       ``memory_limit`` is 80 % of host RAM, which on a 4 GiB cgroup
       container resolves to ~3.2 GiB of buffer pool — enough to OOM-kill
       uvicorn during materialize. See ``connectors/keboola/extractor.py``
       module-level comment for the empirical trace. These run uniformly,
       regardless of any opt-out below.

    2. **BQ extension setting (conditional on instance config)** —
       ``bq_query_timeout_ms`` from ``data_source.bigquery.query_timeout_ms``.
       The extension default is 90 s, which is too tight for analyst-scale
       queries against view-backed BQ datasets — bumping the default to
       600 s here. Sentinel ``0`` (or a non-numeric / unparseable value)
       leaves the extension default in place.

    Call AFTER ``LOAD bigquery`` on every DuckDB session that touches BQ:
    BqAccess's session factory, the standalone extractor in
    ``connectors/bigquery/extractor.py``, the orchestrator's
    ``_remote_attach`` path in ``src/orchestrator.py``, and ``src/db.py``'s
    read-only analytics-DB factory (called from ``_reattach_remote_extensions``
    plus a belt-and-suspenders call from ``get_analytics_db_readonly`` itself).

    SET failures are logged at WARNING level (previously silent) so operators
    can diagnose timeouts that surface as the extension default 90 s when the
    intended value was higher. The applied value is verified via
    ``current_setting('bq_query_timeout_ms')``; a mismatch is also logged.
    """
    # Resource caps run UNCONDITIONALLY, before any early-exit on the
    # extension-setting branch below — if instance_config is unavailable
    # or the operator opted out of the BQ timeout, we still need the
    # memory cap on this pool entry. Otherwise an entry that took the
    # opt-out path stays at DuckDB's 80%-of-host default and re-opens
    # the OOM window the cap was supposed to close. See #431 follow-up.
    for stmt in (
        "SET memory_limit='2GB'",
        "SET threads=2",
        "SET preserve_insertion_order=false",
    ):
        try:
            conn.execute(stmt)
        except Exception as e:
            logger.warning(
                "apply_bq_session_settings: %s failed (%s); pool entry will "
                "run with DuckDB defaults — re-check pool config",
                stmt,
                e,
            )

    try:
        from app.instance_config import get_value
    except Exception as e:
        logger.warning(
            "apply_bq_session_settings: instance_config unavailable (%s); "
            "extension default bq_query_timeout_ms (90 s) will apply",
            e,
        )
        return
    raw = get_value(
        "data_source",
        "bigquery",
        "query_timeout_ms",
        default=600_000,
    )
    try:
        ms = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        logger.warning(
            "apply_bq_session_settings: query_timeout_ms=%r is not an int; extension default (90 s) will apply",
            raw,
        )
        return
    if ms <= 0:
        # Operator opt-out: leave extension default in place. Log INFO so the
        # choice shows up in startup logs without being noisy.
        logger.info(
            "apply_bq_session_settings: query_timeout_ms=%d (≤0); extension "
            "default bq_query_timeout_ms (90 s) will apply",
            ms,
        )
        return
    try:
        conn.execute(f"SET bq_query_timeout_ms = {int(ms)}")
    except Exception as e:
        # Most common cause: the BigQuery extension is not loaded on this
        # connection yet (caller forgot the `LOAD bigquery` step), or the
        # installed extension version pre-dates the setting. Either way the
        # 90 s default sticks and remote queries time out unexpectedly.
        # Surface this — silent fallback was the bug behind real outages.
        logger.warning(
            "apply_bq_session_settings: SET bq_query_timeout_ms=%d failed (%s); "
            "extension default (90 s) will apply. Likely cause: BigQuery "
            "extension not loaded on this connection, or the installed "
            "extension version does not support this setting.",
            ms,
            e,
        )
        return
    # Verify the setting actually landed — protects against silent ignores
    # the extension might do in some failure modes.
    try:
        result = conn.execute("SELECT current_setting('bq_query_timeout_ms')").fetchone()
        actual = int(result[0]) if result and result[0] is not None else None
    except Exception as e:
        logger.warning(
            "apply_bq_session_settings: could not read back "
            "bq_query_timeout_ms (%s); cannot verify setting was applied",
            e,
        )
        return
    if actual != ms:
        logger.warning(
            "apply_bq_session_settings: requested bq_query_timeout_ms=%d but "
            "current_setting reports %r — extension may have ignored the SET",
            ms,
            actual,
        )
    else:
        logger.debug(
            "apply_bq_session_settings: bq_query_timeout_ms=%d applied",
            ms,
        )


class BqAccess:
    """Single entry point for BigQuery access. Stateless after construction.

    Factories are injectable for tests:
        bq = BqAccess(
            BqProjects(billing="test-billing", data="test-data"),
            client_factory=lambda projects: mock_client,
        )
    """

    def __init__(
        self,
        projects: BqProjects,
        *,
        client_factory: Callable[[BqProjects], object] | None = None,
        duckdb_session_factory: Callable[[BqProjects], object] | None = None,
    ):
        self._projects = projects
        self._client_factory = client_factory or _default_client_factory
        self._duckdb_session_factory = duckdb_session_factory or _default_duckdb_session_factory

    @property
    def projects(self) -> BqProjects:
        return self._projects

    def client(self):
        """Construct (or retrieve from injected factory) a BigQuery client."""
        return self._client_factory(self._projects)

    @contextmanager
    def duckdb_session(self) -> Iterator[object]:
        """Yield in-memory DuckDB conn with bigquery extension loaded + SECRET set."""
        with self._duckdb_session_factory(self._projects) as conn:
            yield conn


def _fetch_bq_columns_full_impl(bq, dataset: str, table: str) -> list[dict]:
    """Implementation that raises on BQ errors. Returns the column list
    or raises the original BQ exception. Validates identifiers; raises
    ``ValueError`` on bad shape. Sentinel-config (``bq.projects.data == ""``)
    surfaces via ``bq.client()`` raising ``BqAccessError(not_configured)``.

    Used by callers that need typed exceptions for HTTP status
    classification — currently only ``app/api/v2_schema._fetch_bq_schema``
    via ``translate_bq_error``.
    """
    from src.identifier_validation import validate_quoted_identifier

    if not bq.projects.data:
        bq.client()  # raises BqAccessError(not_configured)

    if not (
        validate_quoted_identifier(bq.projects.data, "BQ project")
        and validate_quoted_identifier(dataset, "BQ dataset")
        and validate_quoted_identifier(table, "BQ source_table")
    ):
        raise ValueError("unsafe BQ identifier in registry — refusing to query")

    bq_sql = (
        f"SELECT column_name, data_type, is_nullable, "
        f"       is_partitioning_column, clustering_ordinal_position "
        f"FROM `{bq.projects.data}.{dataset}.INFORMATION_SCHEMA.COLUMNS` "
        f"WHERE table_name = ? ORDER BY ordinal_position"
    )
    with bq.duckdb_session() as conn:
        rows = conn.execute(
            "SELECT * FROM bigquery_query(?, ?, ?)",
            [bq.projects.billing, bq_sql, table],
        ).fetchall()

    return [
        {
            "name": r[0],
            "type": r[1],
            "nullable": r[2] == "YES",
            "is_partitioning_column": r[3] == "YES",
            "clustering_ordinal_position": r[4],
        }
        for r in rows
    ]


def fetch_bq_columns_full(bq, dataset: str, table: str) -> list[dict] | None:
    """Best-effort wrapper around ``_fetch_bq_columns_full_impl`` — returns
    ``None`` on any failure (sentinel-unconfigured, unsafe identifier, BQ
    query exception). Does NOT raise. For callers that don't need typed
    exceptions (the metadata provider; the partition/cluster path of
    v2_schema).
    """
    try:
        return _fetch_bq_columns_full_impl(bq, dataset, table)
    except Exception as e:
        logger.warning(
            "BQ COLUMNS fetch failed for %s.%s.%s: %s",
            bq.projects.data,
            dataset,
            table,
            e,
        )
        return None


def _resolve_bq_projects() -> BqProjects | None:
    """Row-first resolution of the projects BqAccess needs, or ``None`` when
    unconfigured. Cheap: an env-var check, then (usually) a single indexed
    ``source_connections`` lookup — safe to call on every ``get_bq_access()``.

    Resolution order:
      1. BIGQUERY_PROJECT env var → both billing + data (legacy override)
      2. the default ``source_connections`` row (source_type='bigquery') —
         D2.2: the row `connections_seed.py` already writes on first boot
         finally becomes load-bearing
      3. instance.yaml data_source.bigquery.billing_project/project — legacy
         fallback for an un-migrated instance with no row yet
    """
    import os

    env_project = os.environ.get("BIGQUERY_PROJECT", "").strip()
    if env_project:
        return BqProjects(billing=env_project, data=env_project)

    from src.connection_resolver import resolve_source_connection

    row = resolve_source_connection("bigquery")
    if row is not None:
        config = row.get("config") or {}
        data = str(config.get("project") or "").strip()
        billing = str(config.get("billing_project") or "").strip() or data
        return BqProjects(billing=billing, data=data) if data else None

    from app.instance_config import get_value

    billing = (get_value("data_source", "bigquery", "billing_project", default="") or "").strip()
    data = (get_value("data_source", "bigquery", "project", default="") or "").strip()
    if not data:
        return None
    if not billing:
        billing = data
    return BqProjects(billing=billing, data=data)


@functools.lru_cache(maxsize=8)
def _bq_access_for_projects(projects: BqProjects | None) -> BqAccess:
    """Build (and cache) the ``BqAccess`` for a resolved ``BqProjects``.

    Cached on the RESOLVED VALUE, not on nothing — so a config or connection
    row change that resolves to different projects is automatically a cache
    miss, no explicit invalidation call needed. ``BqProjects`` is a frozen,
    hashable dataclass, so two calls that resolve identically hit the same
    cache entry (and return the SAME BqAccess instance, which callers rely
    on, e.g. `TestGetBqAccess.test_is_cached`).
    """
    if projects is None:
        # Return a "not configured" sentinel BqAccess. Construction succeeds so FastAPI
        # Depends(get_bq_access) resolves cleanly on non-BQ instances (Keboola-only,
        # CSV-only) where every v2 endpoint would otherwise 500 during dep-injection
        # — even for local-source tables that never touch BigQuery.
        # The error is deferred to bq.client() / bq.duckdb_session() so the endpoint's
        # try/except BqAccessError catches it normally if (and only if) the endpoint
        # actually tries to query BQ. Devin BUG_0001 on PR #138 review.
        def _raise_not_configured(_projects):
            raise BqAccessError(
                "not_configured",
                "BigQuery project not configured",
                details={
                    "hint": (
                        "Set data_source.bigquery.project in instance.yaml, or "
                        "register a bigquery connection under /admin/connections "
                        "(and optionally a billing_project for cross-project "
                        "deployments). BIGQUERY_PROJECT env var also accepted as "
                        "legacy override."
                    ),
                },
            )

        @contextmanager
        def _raise_not_configured_session(_projects):
            _raise_not_configured(_projects)
            yield  # unreachable; keeps generator protocol

        return BqAccess(
            BqProjects(billing="", data=""),
            client_factory=_raise_not_configured,
            duckdb_session_factory=_raise_not_configured_session,
        )

    return BqAccess(projects)


def get_bq_access() -> BqAccess:
    """Module-level FastAPI Depends target. Resolves projects (row-first,
    see :func:`_resolve_bq_projects`) and returns a cached ``BqAccess``.

    Cache invalidation: keyed on the *resolved value* (see
    :func:`_bq_access_for_projects`) rather than process-lifetime — an admin
    saving a different bigquery connection row is a different resolved
    ``BqProjects``, so the very next call picks it up on every process,
    with no cross-process staleness and no explicit cache-clear call
    required. `get_bq_access.cache_clear()` is still exposed (forwarding to
    the inner cache) for the existing `app.instance_config.reset_cache()`
    contract and test suite that call it explicitly.

    Tests inject via `app.dependency_overrides[get_bq_access] = lambda: bq` for
    endpoints, or construct `BqAccess(...)` directly for non-endpoint code.
    """
    return _bq_access_for_projects(_resolve_bq_projects())


# Preserves the `functools.cache`-style `.cache_clear()` contract every
# existing caller (tests, `app.instance_config.reset_cache()`) already
# depends on, even though `get_bq_access` itself is now a plain function
# delegating to the inner value-keyed cache.
get_bq_access.cache_clear = _bq_access_for_projects.cache_clear  # type: ignore[attr-defined]


def validate_bigquery_startup_config() -> List[str]:
    """Surface common config gaps that only fail at first BQ call (not at boot).

    Returns a list of warning strings (empty when nothing notable). Caller
    typically logs each at WARNING and continues — startup never blocks on
    config quality issues, only on hard schema problems.

    Checks (in order, each independent):

    1. Cross-project setup (``project`` ≠ ``billing_project``) without
       ``location`` set. The region-scoped metadata path
       (``_fetch_via_table_storage`` in metadata.py) falls back to
       ``client.get_dataset()`` per-table on every cache refresh when
       ``location`` is unset, which works for some IAM shapes but silently
       fails with ``"provider returned no data"`` for others (the
       on-disk symptom from issue #343). Setting
       ``data_source.bigquery.location`` to the dataset's region makes the
       fast path deterministic.

    2. ``billing_project`` defaulted to ``project`` while the two values
       suggest a cross-project setup (project name contains "data" or
       "dataview", billing name contains "ai" or "foundryai" — heuristic).
       Almost-always-wrong combo: pre-fix the SA on ``project`` lacks
       ``serviceusage.services.use`` and every query 502s. We can't be
       sure, so we warn rather than reject.

    Lives in this module (not app/main.py) so the SDK / CLI / scripts that
    use ``BqAccess`` outside the FastAPI process can call it too.
    """
    warnings: List[str] = []
    try:
        from app.instance_config import get_value
    except Exception:
        return warnings  # config layer not available — likely test harness
    project = (get_value("data_source", "bigquery", "project") or "").strip()
    billing = (get_value("data_source", "bigquery", "billing_project") or "").strip()
    location = (get_value("data_source", "bigquery", "location") or "").strip()
    if not project:
        return warnings  # BQ not configured — nothing to check
    effective_billing = billing or project
    if effective_billing != project and not location:
        warnings.append(
            f"data_source.bigquery.project={project!r} differs from "
            f"billing_project={effective_billing!r} (cross-project setup) "
            f"but data_source.bigquery.location is not set. The metadata "
            f"cache will fall back to per-table REST dataset.get() and may "
            f"silently return 'provider returned no data' for some IAM "
            f"shapes. Set data_source.bigquery.location (e.g. 'us-central1' "
            f"or 'EU') to the region where the dataset lives — see issue "
            f"#343."
        )
    if not billing and project:
        # Heuristic detection of the common cross-project mistake: data
        # project named like a warehouse, project the SA actually lives in
        # named like the app. The user typically wants billing_project to
        # equal the SA's home project.
        proj_low = project.lower()
        warehouse_like = any(s in proj_low for s in ("dataview", "warehouse", "datalake", "-dw-", "-data-"))
        if warehouse_like:
            warnings.append(
                f"data_source.bigquery.project={project!r} looks like a "
                f"shared data warehouse but billing_project is unset, so "
                f"jobs will bill to {project!r}. If the service account "
                f"doesn't have serviceusage.services.use on {project!r}, "
                f"every query will fail with 403. Set "
                f"data_source.bigquery.billing_project to the SA's home "
                f"project — see issue #343."
            )
    return warnings
