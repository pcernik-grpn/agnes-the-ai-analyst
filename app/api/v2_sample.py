"""GET /api/v2/sample/{table_id}?n=5 — sample rows (spec §3.3)."""

from __future__ import annotations
import logging
import math
import time
from fastapi import APIRouter, Depends, HTTPException, Query
import duckdb

from app.auth.dependencies import get_current_user, _get_db
from src.db import _open_duckdb
from src.audit_helpers import identity_for_audit, client_kind_from_user
from src.rbac import can_access_table
from src.access_policy import (
    PolicyError,
    PolicyIdentityUnresolvable,
    assert_unique_output_columns,
    policied_from_sql,
    policied_relation,
    policy_cache_identity,
    row_scope_payload,
)
from app.api.v2_cache import TTLCache
from connectors.bigquery.access import BqAccess, BqAccessError, get_bq_access

from src.repositories import (
    audit_repo,
    table_registry_repo,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v2", tags=["v2"])

_sample_cache = TTLCache(maxsize=512, ttl_seconds=3600)
_MAX_N = 100


class TableNotSyncedError(FileNotFoundError):
    """Registered row exists but no local data has landed yet.

    Subclasses FileNotFoundError so every existing catch keeps working; the
    endpoint maps it to a 404 whose detail explains the pending/failing
    first sync instead of the misleading bare "table not found" (which reads
    as "registration failed" to the admin who just registered it)."""

    def __init__(self, table_id: str, detail: str):
        super().__init__(table_id)
        self.detail = detail


class TableNotPreviewableError(TableNotSyncedError):
    """No parquet, and there never will be one — by design, not by failure.

    ``query_mode='remote'`` only: every query goes live to the upstream source,
    so nothing is ever materialized. Such rows used to fall into the not-synced
    branch and be reported as "the first sync is pending or failing", sending an
    admin to hunt a job that does not exist and never will.

    ``server_only`` is deliberately NOT one of these — it suppresses
    *distribution* to analyst laptops while the server still materializes the
    parquet, so a missing one there is a real failure and must keep saying so.

    Subclasses ``TableNotSyncedError`` so every existing catch and the endpoint's
    404 mapping keep working unchanged; only the explanation differs.
    """


def _not_previewable_detail(table_id: str, *, query_mode: str) -> str:
    """Explain a `query_mode='remote'` row, which has no server parquet at all.

    ``server_only`` deliberately does NOT belong here, and an earlier version of
    this fix got that wrong. It is a *distribution* suppressor: the server still
    materializes the parquet, `agnes pull` just never ships it to a laptop —
    ``app/api/sync.py`` says as much ("remote tables have no server parquet at
    all, and server_only ones are deliberately not distributed"), and the
    registration validator rejects ``server_only`` together with
    ``query_mode='remote'`` for that reason. So a `server_only` row whose parquet
    is missing HERE, on the server, is a genuinely pending or failing sync, and
    telling its admin "no sync is pending" would hide a broken job behind a
    reassurance (Devin Review on #1189).

    The predicate this originally borrowed from — `sync.py`'s signed-URL gate —
    lumps the two together correctly, because for *distribution* they coincide.
    For *previewability on the server* they do not.
    """
    return (
        f"table {table_id!r} is registered with query_mode={query_mode or 'remote'!r}, so it is "
        "queried at the source and never materialized as a parquet — there is no sample to "
        f"preview, and no sync is pending. Read it at the source instead: `agnes query --remote "
        f'"SELECT * FROM {table_id} LIMIT 10"`.'
    )


def _not_synced_detail(table_id: str, *, registry_name: str | None = None) -> str:
    """Explain a registered-but-dataless table, with the last sync error
    when sync_state recorded one.

    ``sync_state.table_id`` is keyed by the registry ``name`` — the same
    write-side convention the parquet filename follows (see
    `app/utils.py::_physical_key_candidates`) — so the error lookup tries the
    name first and the id as a fallback. Keyed by id alone, the recorded
    failure never surfaced for a row whose id was slugified from its name.
    """
    detail = (
        f"table {table_id!r} is registered but has no synced data yet — "
        "the first sync is pending or failing (see the sync status on "
        "Admin → Tables)"
    )
    try:
        from src.repositories import sync_state_repo

        err = ""
        for key in dict.fromkeys((registry_name, table_id)):
            if not key:
                continue
            state = sync_state_repo().get_table_state(key) or {}
            err = state.get("error") or ""
            if err:
                break
        if err:
            detail += f"; last sync error: {str(err)[:300]}"
    except Exception:
        logger.debug("sync_state lookup failed for %s while building sample 404 detail", table_id)
    return detail


def _sanitize_for_json(obj):
    """Recursively replace NaN / ±inf floats with None so the response
    survives JSON serialization. FastAPI's default encoder rejects these
    (``ValueError: Out of range float values are not JSON compliant``)
    even though Python's stdlib ``json`` accepts them by default. NaNs
    show up routinely in DuckDB / BigQuery scans (NULL → NaN through the
    pandas DataFrame round-trip), so the endpoint must sanitize at the
    data-prep boundary rather than rely on the serializer."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, list):
        return [_sanitize_for_json(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(_sanitize_for_json(x) for x in obj)
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    return obj


def _fetch_bq_sample(bq, dataset: str, table: str, n: int) -> list[dict]:
    """Fetch up to `n` sample rows from a BQ table via the DuckDB BQ extension.

    `bq.duckdb_session()` provides a DuckDB conn with the bigquery extension
    loaded + auth secret installed. SQL here is server-constructed (validated
    identifiers + LIMIT n) — a BQ BadRequest means registry corruption, not
    user fault, so it surfaces as `bq_upstream_error` (HTTP 502).
    """
    from connectors.bigquery.access import translate_bq_error
    from src.identifier_validation import validate_quoted_identifier

    # Surface "BQ not configured" as the structured 500 BqAccessError(not_configured)
    # with hint pointing at instance.yaml, NOT as the misleading 400 unsafe_identifier
    # the empty-string sentinel BqAccess would otherwise trigger from
    # validate_quoted_identifier below. Devin BUG_0002 on PR #138.
    if not bq.projects.data:
        bq.client()  # raises BqAccessError(not_configured); endpoint catches it

    # Defense in depth: registry already validates these, but the v2 API
    # endpoints are downstream of admin REST writes that might bypass that
    # gate. A `source_table` containing a backtick would otherwise break
    # out of the `…` quoted identifier and execute arbitrary BQ SQL.
    if not (
        validate_quoted_identifier(bq.projects.data, "BQ project")
        and validate_quoted_identifier(dataset, "BQ dataset")
        and validate_quoted_identifier(table, "BQ source_table")
    ):
        raise ValueError("unsafe BQ identifier in registry — refusing to query")

    # Task 10/13: BigQuery policy enforcement (transpile + named params via
    # `policied_relation(..., dialect="bigquery")`) is not wired into this
    # execution path yet -- this function itself still runs the raw,
    # unfiltered physical table. The Task 13 caller guard (`build_sample`,
    # above) fails closed before reaching here for a policied table's
    # non-admin caller, so this remains reachable only for a non-policied
    # table or an admin bypass -- never silently for a filtered caller.
    bq_sql = f"SELECT * FROM `{bq.projects.data}.{dataset}.{table}` LIMIT {int(n)}"
    with bq.duckdb_session() as conn:
        try:
            df = conn.execute(
                "SELECT * FROM bigquery_query(?, ?)",
                [bq.projects.billing, bq_sql],
            ).fetchdf()
            return df.to_dict(orient="records")
        except Exception as e:
            raise translate_bq_error(e, bq.projects, bad_request_status="upstream_error")


def _fetch_remote_view_sample(table_id: str, row: dict, n: int) -> list[dict]:
    """Live sample for a ``query_mode='remote'`` row through its analytics view.

    The orchestrator maintains a view over the re-ATTACHed source for every
    remote row that resolves locally (``_remote_attach`` — Snowflake ``sf``,
    Keboola ``kbc``, Databricks with ``attach_enabled``), and
    ``get_analytics_db_readonly()`` re-ATTACHes those catalogs, so this is the
    same object ``/api/query`` serves these rows through. BigQuery never
    reaches here — its live branch in ``build_sample`` runs first.

    An engine whose remote rows have no local view (Databricks without
    attach), or a currently-broken ATTACH, surfaces as a query error — mapped
    back to the by-design :class:`TableNotPreviewableError`, with the real
    failure prepended so the admin is not reassured past an actual outage.
    Including the engine's error text discloses nothing new: the caller has
    already passed ``can_access_table`` for this row, and ``/api/query``
    hands the same caller the identical engine error verbatim for the same
    table.
    """
    from src.db import get_analytics_db_readonly
    from src.identifier_validation import validate_identifier
    from src.sql_ident import quote_ident

    view_name = row.get("name") or table_id
    # Registry rows are admin-written, not trusted: the orchestrator applies
    # the same strict validator before creating the view, so a name that
    # fails it has no view to read anyway — refuse before touching SQL.
    if not validate_identifier(view_name, "remote sample view"):
        raise ValueError(f"unsafe table name in registry: {view_name!r}")

    conn = get_analytics_db_readonly()
    try:
        df = conn.execute(f"SELECT * FROM {quote_ident(view_name)} LIMIT {int(n)}").fetchdf()
        return df.to_dict(orient="records")
    except Exception as exc:
        raise TableNotPreviewableError(
            table_id,
            f"live sample through the server's analytics view failed "
            f"({type(exc).__name__}: {str(exc)[:200]}); " + _not_previewable_detail(table_id, query_mode="remote"),
        )
    finally:
        conn.close()


def _sample_is_cacheable(*, source_type: str, query_mode: str, has_access_policy: bool) -> bool:
    """Whether this row's 5-row preview may be served from ``_sample_cache``.

    Two exemptions, for different reasons, and one carve-out inside the second.

    A policied row is uncacheable because the cache key is ``{table_id}|{n}``
    with no identity in it, so one caller's slice would be handed to the next.
    (Today a policied row fails closed on this surface before it could cache
    anything, but the key would be wrong the moment enforcement is wired in.)

    A ``remote`` row is uncacheable because ``remote`` means the read goes live
    — that is the whole argument for evaluating the remote branch AHEAD of
    parquet resolution, so that a row flipped materialized→remote cannot
    preview its stale leftover parquet. Serving a copy up to
    ``_sample_cache``'s hour old is that same staleness from a different
    store, and it would make the branch's own comment untrue. The cost that
    gives up is small and bounded: ``SELECT * FROM <view> LIMIT n`` with
    ``n <= _MAX_N``, a capped pull through the extension for Snowflake and
    Keboola. Only Databricks with the experimental, off-by-default
    ``attach_enabled`` turns it into a parquet read.

    **BigQuery is excluded from that exemption**, and the reason is not
    liveness but billing. A BQ remote row takes the branch above this one
    (``source_type == "bigquery"`` and not ``materialized``) into
    ``_fetch_bq_sample``, which pushes the statement to BigQuery — a metered,
    billable scan per call, unlike every other engine here. It was cacheable
    before this exemption existed (``cacheable = not has_access_policy``), so
    exempting it would have made every catalog tile render and every ``agnes
    describe`` bill a fresh query. That is a cost regression, not a
    correctness gain, and it is also outside the scope of a change about the
    NON-BigQuery live branch: BQ's caching is left exactly as it was.
    """
    if has_access_policy:
        return False
    if (source_type or "").lower() == "bigquery":
        return True
    return query_mode != "remote"


def build_sample(
    conn: duckdb.DuckDBPyConnection,
    user: dict,
    table_id: str,
    *,
    n: int,
    bq: BqAccess,
) -> dict:
    n = max(1, min(int(n), _MAX_N))

    # RBAC + existence check MUST run before cache lookup — otherwise an
    # unauthorized user can read cached sample rows fetched by an authorized one.
    repo = table_registry_repo()
    row = repo.get(table_id)
    if not row:
        raise FileNotFoundError(table_id)

    if not can_access_table(user, table_id, conn):
        raise PermissionError(table_id)

    source_type = row.get("source_type") or ""

    # Internal source — never cache. Sample rows here are RBAC-scoped per
    # caller (alice sees alice's rows; admin sees all), so a shared cache
    # would leak alice's rows to bob on the next request. The source data
    # is small + the per-request query is cheap, so skipping the cache
    # entirely is the right trade-off.
    if source_type == "internal":
        from connectors.internal.access import (
            INTERNAL_TABLES_BY_ID,
            build_filter_clause,
            sample_internal_rows,
        )
        from app.auth.access import is_user_admin as _is_admin
        from app.auth.session_principal import PRINCIPAL_TYPES

        if table_id not in INTERNAL_TABLES_BY_ID:
            raise FileNotFoundError(table_id)
        internal_def = INTERNAL_TABLES_BY_ID[table_id]
        # is_user_admin takes (user_id, conn) — earlier draft passed the
        # whole user dict and crashed with TypeError on first request
        # (review #278/2). Same fix as app/api/query.py:_run_internal_query.
        # A Principal has no .get("id") — treat co-session / agent-session as non-admin
        # for internal row-level filter. build_filter_clause expects a dict
        # so pass a shim when user is a principal.
        if isinstance(user, PRINCIPAL_TYPES):
            is_admin = False
            user_dict_shim = {"id": "", "email": ""}
        else:
            is_admin = _is_admin(user.get("id"), conn) if user.get("id") else False
            user_dict_shim = user
        where_clause = build_filter_clause(internal_def, user_dict_shim, is_admin)
        # The source rows live in the active state backend (DuckDB or Postgres);
        # sample_internal_rows dispatches on use_pg() so the preview is correct
        # on either — a raw always-DuckDB read returned nothing on Postgres.
        rows = sample_internal_rows(internal_def, where_clause, n)
        return {"table_id": table_id, "rows": _sanitize_for_json(rows), "source": source_type}

    # A policied table's sample is caller-scoped the same way an internal
    # source's is (§9) — row filtering + column masking both depend on the
    # caller's identity, so a shared `table_id|n` key would serve team A's
    # rows to team B on the next request. `cache_key`/`cacheable` default to
    # the plain (pre-existing) shape; a policied table re-derives both
    # below, once identity resolution has actually run — only the
    # local-parquet branch does that today (the BQ-sample branch has no
    # identity to key on yet, see the Task 10 note in `_fetch_bq_sample`
    # above, so it keeps skipping the cache entirely, exactly as before
    # this task).
    has_access_policy = bool(row.get("access_policy_sql"))

    cache_key = f"{table_id}|{n}"
    cacheable = _sample_is_cacheable(
        source_type=str(source_type or ""),
        query_mode=str(row.get("query_mode") or ""),
        has_access_policy=has_access_policy,
    )
    if cacheable:
        cached = _sample_cache.get(cache_key)
        if cached is not None:
            return cached

    # Task 11 (§10): populated below only on the local-parquet branch, which
    # is the only one that currently resolves through policied_relation —
    # the BQ-sample branch has no execution path that could carry it yet
    # (see the Task 13 fail-closed guard immediately below), so there is
    # nothing to disclose there.
    row_scope: dict | None = None

    if source_type == "bigquery" and (row.get("query_mode") or "") != "materialized":
        if has_access_policy:
            # Task 13 (§8 ratchet): this branch pushes the sample straight to
            # BigQuery via the DuckDB `bigquery_query()` extension -- Task 10
            # only wired `policied_relation(dialect="bigquery")` into
            # `/api/query`'s AST-rewrite path (app/api/query.py), never into
            # this table_id-shaped surface's live-BQ branch, so unguarded it
            # would hand back the RAW, unfiltered physical table to any
            # caller with base table-level access. Fail closed (§17: "every
            # failure denies") instead: the same `policy_error` 500 the
            # local-parquet branch below already returns for an
            # unresolvable policy. Admin bypass is preserved --
            # `policied_relation` itself decides that (not a bare
            # `access_policy_sql` check), so an admin keeps seeing the raw
            # sample exactly as before this change.
            #
            # TODO(follow-up): wire this branch the way
            # `app/api/query.py::_execute_policied_remote_bq` wires the
            # AST-rewrite surface -- transpile via
            # `policied_relation(table_id, user, dialect="bigquery")`,
            # resolve the policy body's own `FROM <name>` to the physical
            # `` `project.dataset.table` `` path, convert `.params` to BQ
            # `QueryParameter`s, and execute through the jobs API
            # (`run_bq_query_to_arrow`) instead of the `bigquery_query()`
            # push-down this branch uses today. Left undone here: it needs
            # its own cost/quota/label design pass for this endpoint (which
            # has none of those today), not just a mechanical swap.
            try:
                bq_relation = policied_relation(table_id, user)
            except PolicyIdentityUnresolvable:
                raise HTTPException(status_code=403, detail={"reason": "policy_identity_unresolvable"})
            except PolicyError as exc:
                raise HTTPException(status_code=500, detail={"reason": "policy_error", "table": exc.table_id})
            if bq_relation.policied:
                raise HTTPException(status_code=500, detail={"reason": "policy_error", "table": table_id})
        rows = _fetch_bq_sample(bq, row.get("bucket") or "", row.get("source_table") or table_id, n)
    elif (row.get("query_mode") or "") == "remote":
        # Non-BQ remote rows: live sample through the analytics view — the
        # parity twin of the BQ branch above (these rows used to be refused
        # outright as not-previewable). Checked BEFORE parquet resolution on
        # purpose: a row flipped materialized→remote can leave a stale
        # parquet on disk, and `remote` means every read goes live. The same
        # sentence is why `_sample_is_cacheable` exempts these rows from
        # `_sample_cache` — an hour-old copy is the same staleness with a
        # different store.
        if has_access_policy:
            # Same fail-closed ratchet as the BQ branch above (Task 13):
            # policy rewrite is not wired into this surface either, so a
            # caller the policy would filter must never see the raw live
            # rows. Admin bypass preserved — `policied_relation` decides.
            try:
                remote_relation = policied_relation(table_id, user)
            except PolicyIdentityUnresolvable:
                raise HTTPException(status_code=403, detail={"reason": "policy_identity_unresolvable"})
            except PolicyError as exc:
                raise HTTPException(status_code=500, detail={"reason": "policy_error", "table": exc.table_id})
            if remote_relation.policied:
                raise HTTPException(status_code=500, detail={"reason": "policy_error", "table": table_id})
        rows = _fetch_remote_view_sample(table_id, row, n)
    else:
        # Resolve by source-name-agnostic lookup — the extract directory is not
        # necessarily the source_type (e.g. the bundled `demo` extract).
        # `_glob` so a PARTITIONED table resolves too: that sync writes
        # `data/<table_id>/<partition>.parquet`, a directory, so the single-file
        # lookup returned None and a healthy fully-synced table was reported as
        # having a pending or failing first sync (Devin Review on #1189).
        from app.utils import LOCAL_PARQUET_READ_EXPR, resolve_local_parquet_glob

        # `registry_name`: the write side keys the parquet filename by the
        # row's `name`, not its `id` — see `_physical_key_candidates` in
        # app/utils.py. Without it, any row whose id was slugified from the
        # name (the register handler lowercases + underscores it) read as
        # "no synced data yet" while fully synced.
        parquet = resolve_local_parquet_glob(table_id, source_type, registry_name=row.get("name"))
        if parquet is None:
            # The registry row exists (checked above), so this is never "no
            # such table" — and `query_mode='remote'` never reaches this
            # branch (it takes the live-view branch above), so a missing
            # parquet here is genuinely "no data has landed yet" — including
            # for `server_only`, which suppresses distribution to analyst
            # laptops, not materialization here.
            #
            # `registry_name`: sync_state is keyed by the row's `name`, the
            # same write-side convention the parquet filename follows, so the
            # last-sync-error lookup must try the name first. Keyed by id
            # alone, a recorded failure never surfaced for a slugified row.
            raise TableNotSyncedError(table_id, _not_synced_detail(table_id, registry_name=row.get("name")))

        # Table access policies (§5): this connection is a throwaway
        # :memory: DB with nothing but the parquet attached — no analytics
        # catalog, so the policy body's own `FROM <name>` has nothing to
        # bind against unless we wrap it. Resolve first; the inert (not
        # policied) branch below stays byte-identical to the pre-existing
        # code.
        try:
            relation = policied_relation(table_id, user)
        except PolicyIdentityUnresolvable:
            raise HTTPException(status_code=403, detail={"reason": "policy_identity_unresolvable"})
        except PolicyError as exc:
            raise HTTPException(status_code=500, detail={"reason": "policy_error", "table": exc.table_id})

        if has_access_policy:
            # Task 12 (§9): re-key on the caller's identity now that
            # identity resolution has actually run — covers BOTH the
            # genuinely-filtered case AND the admin-bypass one (a policied
            # table with `relation.policied=False` for an admin must still
            # not be cached under the same key a non-admin's filtered
            # slice would read from). Re-check the cache now that the real
            # key is known — a hit skips the DuckDB read below entirely —
            # and write under the SAME key at the bottom.
            cache_key = f"{table_id}|{n}|policy:{policy_cache_identity(user, table_id=table_id)!r}"
            cacheable = True
            cached = _sample_cache.get(cache_key)
            if cached is not None:
                return cached

        # Task 11 (§10): disclose that these sample rows are a caller-scoped
        # slice, not the whole table. `None` (no key added below) for the
        # inert/admin-bypass case, matching /api/query's row_scope contract.
        if relation.policied:
            row_scope = row_scope_payload([relation.table_id])

        c = _open_duckdb(":memory:")
        try:
            if relation.policied:
                # The parquet path is server-resolved, never user input, so
                # it is safe to splice as an escaped literal — it must NOT
                # be a `?` placeholder: the policy binds named `$user_*`
                # parameters, and DuckDB refuses to mix positional and
                # named parameters in one statement.
                escaped_parquet = parquet.replace("'", "''")
                from_sql = policied_from_sql(
                    relation,
                    table_name=row["name"],
                    source_sql=f"read_parquet('{escaped_parquet}', union_by_name=true, hive_partitioning=true)",
                )
                # Read-path guard (§17): DESCRIBE the policy relation itself —
                # a masking policy that re-derives a column `*` still emits has
                # duplicate output names and leaks the plaintext copy (pandas
                # `.to_dict` below renames the 2nd dup, hiding it under the
                # expected key). The outer `SELECT * FROM (...)` would dedup
                # and mask the collision, so DESCRIBE `from_sql` directly.
                try:
                    _out_cols = [r[0] for r in c.execute(f"DESCRIBE {from_sql}", relation.params).fetchall()]
                    assert_unique_output_columns(_out_cols, relation.table_id)
                except PolicyError as exc:
                    raise HTTPException(status_code=500, detail={"reason": "policy_error", "table": exc.table_id})
                df = c.execute(f"SELECT * FROM {from_sql} LIMIT {n}", relation.params).fetchdf()
            else:
                df = c.execute(
                    f"SELECT * FROM {LOCAL_PARQUET_READ_EXPR} LIMIT {n}",
                    [parquet],
                ).fetchdf()
            rows = df.to_dict(orient="records")
        finally:
            c.close()

    rows = _sanitize_for_json(rows)
    payload = {"table_id": table_id, "rows": rows, "source": source_type}
    if row_scope is not None:
        payload["row_scope"] = row_scope
    if cacheable:
        _sample_cache.set(cache_key, payload)
    return payload


@router.get("/sample/{table_id}")
def sample(
    table_id: str,
    n: int = Query(default=5, ge=1, le=_MAX_N),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
    bq: BqAccess = Depends(get_bq_access),
):
    # Plain ``def`` — opens a `bq.duckdb_session()` and runs sync queries
    # through the BQ extension. See PR #188 Tier 1 entry.
    t0 = time.monotonic()
    resource = f"table:{table_id}"[:256]
    try:
        result = build_sample(conn, user, table_id, n=n, bq=bq)
        try:
            audit_repo().log(
                user_id=identity_for_audit(user)[0],
                action="catalog.sample",
                resource=resource,
                params={
                    "rows_returned": len(result.get("rows", [])),
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                },
                result="success",
                client_kind=client_kind_from_user(user),
            )
        except Exception:
            logger.exception("audit_log write failed for catalog.sample; continuing")
        return result
    except (FileNotFoundError, PermissionError, ValueError, BqAccessError) as exc:
        try:
            if isinstance(exc, FileNotFoundError):
                status_code = 404
            elif isinstance(exc, PermissionError):
                status_code = 403
            elif isinstance(exc, ValueError):
                status_code = 400
            else:
                status_code = BqAccessError.HTTP_STATUS.get(exc.kind, 500)  # type: ignore[union-attr]
            audit_repo().log(
                user_id=identity_for_audit(user)[0],
                action="catalog.sample",
                resource=resource,
                params={"duration_ms": int((time.monotonic() - t0) * 1000), "error": str(exc)[:200]},
                result=f"error.{status_code}",
                client_kind=client_kind_from_user(user),
            )
        except Exception:
            logger.exception("audit_log write failed on error path for catalog.sample; continuing")
        if isinstance(exc, TableNotSyncedError):
            raise HTTPException(status_code=404, detail=exc.detail)
        if isinstance(exc, FileNotFoundError):
            raise HTTPException(status_code=404, detail=f"table {table_id!r} not found")
        if isinstance(exc, PermissionError):
            from src.rbac import table_not_in_stack_message

            raise HTTPException(
                status_code=403,
                detail=table_not_in_stack_message(table_id),
            )
        if isinstance(exc, ValueError):
            raise HTTPException(
                status_code=400,
                detail={"error": "unsafe_identifier", "message": str(exc), "details": {}},
            )
        raise HTTPException(
            status_code=BqAccessError.HTTP_STATUS.get(exc.kind, 500),  # type: ignore[union-attr]
            detail={"error": exc.kind, "message": exc.message, "details": exc.details},  # type: ignore[union-attr]
        )
