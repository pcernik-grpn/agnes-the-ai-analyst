"""Attachment download endpoint — streams connector-catalogued binaries.

A connector that stores attachment files on the server declares its
catalogue in ``src/attachment_sources.py`` (table, id column, path column,
permitted root); this route serves any declared source. Jira is the first:
the ``attachments`` catalogue's ``local_path`` records where
``JiraService.download_all_attachments()`` put each file.

RBAC is table-level: "can this caller read attachment X" first reduces to
"can this caller read the table that catalogues X" (``can_access_table``),
the same gate as the parquet download route. On top of that, a table
access policy (``src/access_policy.py``) can narrow WHICH ROWS of that
table a caller sees — an attachment binary belongs to exactly one row, so
before the bytes are released this module also confirms that row is
visible in the caller's own policied view of the catalogue table
(``_row_visible_under_access_policy``); a table with no policy attached is
untouched (K3, RLS review #1979). A catalogue table marked ``server_only``
keeps its attachment binaries on the server just as it keeps its parquet;
the query-mode allowlist half of that route's distribution gate is
parquet-distribution business and deliberately does not transfer
(attachments are lazy per-id fetches, not manifest sync).

Misses must stay distinguishable from denials: a catalogued attachment can
have no bytes on the server (over-50MB skip, transform-time miss, file
removed since), and a client needs to tell that 404 apart from a 403 so it
can fall back to the upstream system's own API for exactly those cases.
"""

import errno
import logging
import os
import stat as stat_module
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.auth.dependencies import _get_db, get_current_user
from src.access_policy import PolicyError, PolicyIdentityUnresolvable, policied_relation
from src.attachment_sources import AttachmentSource, get_attachment_source, list_attachment_sources
from src.audit_helpers import client_kind_from_user, identity_for_audit, log_safe
from src.db import get_analytics_db_readonly
from src.rbac import can_access_table, table_not_in_stack_message
from src.sql_ident import quote_ident

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/attachments", tags=["attachments"])


def _audit(user: dict, source: str, attachment_id: str, result: str, **params) -> None:
    """Audit one download attempt, granted or denied — same shape as
    ``data.download``; ``log_safe`` guarantees a failed audit write never
    fails the request itself."""
    log_safe(
        user_id=identity_for_audit(user)[0],
        action="attachment.download",
        resource=f"attachment:{source}/{attachment_id}"[:256],
        params={"source": source, **params},
        result=result,
        client_kind=client_kind_from_user(user),
    )


class _CatalogueUnavailable(Exception):
    """The catalogue view could not be QUERIED — distinct from "no row".

    Raised by :func:`_lookup_stored_path` when the failure is not the one
    benign case (view absent because the source was declared but never
    synced). Mapping these onto 404 ``attachment_not_found`` would send the
    client to the upstream API for rows that exist — the exact confusion the
    module docstring's "misses must stay distinguishable" rule forbids.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _lookup_stored_path(
    decl: AttachmentSource, view_name: str, attachment_id: str
) -> tuple[bool, str | None, str | None]:
    """Look ``attachment_id`` up in the source's catalogue view.

    Returns ``(row_exists, stored_path, original_name)`` —
    ``original_name`` comes from the declaration's ``filename_column`` (the
    name the upstream system shows, e.g. Jira's ``filename``) and is ``None``
    when the source declares none. The id column is compared as VARCHAR so a
    BIGINT-typed catalogue column never throws on a non-numeric path
    parameter.

    Only a genuinely ABSENT view (source declared but never synced) reads as
    "no row". Any other failure raises :class:`_CatalogueUnavailable`:
    ``get_analytics_db_readonly`` deliberately swallows its own re-ATTACH
    errors, so a broken analytics DB also surfaces here as a
    ``CatalogException`` — the view's absence is therefore verified against
    the catalog instead of trusted from the exception — and a declaration
    whose columns drifted from the connector's output raises a
    ``BinderException``, which is a server misconfiguration, not a miss.
    """
    cols = quote_ident(decl.path_column)
    if decl.filename_column:
        cols += f", {quote_ident(decl.filename_column)}"
    sql = (
        f"SELECT {cols} FROM {quote_ident(view_name)} WHERE CAST({quote_ident(decl.id_column)} AS VARCHAR) = ? LIMIT 1"
    )
    try:
        analytics = get_analytics_db_readonly()
    except Exception as exc:
        # The OPEN itself can fail (read-only open refused while a
        # read-write handle is alive, corrupt/locked file, DuckLake catalog
        # connectivity) — same malfunction class as a failed query: it must
        # reach the audited 503, not escape as a bare 500 with no audit row.
        raise _CatalogueUnavailable("catalog_open_failed") from exc
    try:
        try:
            row = analytics.execute(sql, [attachment_id]).fetchone()
        except duckdb.CatalogException as exc:
            try:
                present = analytics.execute(
                    "SELECT 1 FROM information_schema.tables WHERE table_name = ?",
                    [view_name],
                ).fetchone()
            except Exception:
                raise _CatalogueUnavailable("catalog_probe_failed") from exc
            if present is None:
                return False, None, None  # declared but never synced
            raise _CatalogueUnavailable("catalog_query_failed") from exc
        except duckdb.Error as exc:
            # BinderException (declaration/connector column drift) and any
            # other engine error: never "not found".
            raise _CatalogueUnavailable("catalog_query_failed") from exc
    finally:
        analytics.close()
    if row is None:
        return False, None, None
    return True, row[0], (row[1] if decl.filename_column else None)


def _row_visible_under_access_policy(
    rbac_key: str, attachment_id: str, decl: AttachmentSource, user: dict
) -> tuple[bool, str | None]:
    """K3 — is the row ``attachment_id`` belongs to visible in ``user``'s
    policied view of the catalogue table?

    ``can_access_table`` (checked by the caller before this runs) only
    answers "can this caller read the TABLE at all" — a table access
    policy narrows WHICH ROWS once they can, and this route serves a
    single row's bytes, so it must clear the same per-row filter every
    other read of the table's rows does (``src/access_policy.py``).

    Resolves once via ``policied_relation`` (admin bypass and the
    no-policy passthrough are both its concern, not this function's) and,
    only when the table actually carries a policy, runs a bound existence
    check through the resolved relation against the SAME analytics
    connection ``_lookup_stored_path`` reads from — the master view lives
    under the registry row's own ``.name`` there, exactly what a policy
    body's own ``FROM <name>`` resolves against, so no wrap
    (``policied_from_sql``) is needed, the same shortcut
    ``src.access_policy._count_through_relation`` takes.

    Returns ``(visible, failure_reason)``. ``visible=True`` unconditionally
    for a table with no policy attached (or an admin-bypass caller) —
    ``policied_relation`` reports that as ``policied=False`` — so the inert
    case never opens a second connection (``failure_reason=None``).
    Fails CLOSED (``visible=False``) on every other outcome: an
    unresolvable identity, a failure to OPEN the analytics connection, or
    the existence query itself raising — a caller must never receive a
    row's bytes because a policy failed to answer, only because it
    explicitly said yes. ``failure_reason`` is ``"policy_check_failed"``
    for exactly those failure cases (finding C, follow-up review of PR
    #2023 — the connection open used to sit OUTSIDE this guarded region
    and could reach the caller as an unhandled 500) so the route's audit
    row can tell "the policy check itself broke" apart from
    ``failure_reason=None``'s "the policy ran and genuinely denies this
    row" — both still 404 ``attachment_not_found`` to the client either
    way, the distinction is audit-only.
    """
    try:
        relation = policied_relation(rbac_key, user)
    except (PolicyIdentityUnresolvable, PolicyError):
        logger.warning("attachment.download: row-visibility policy could not be resolved for table %r", rbac_key)
        return False, "policy_check_failed"
    if not relation.policied:
        return True, None

    sql = (
        f"SELECT 1 FROM ({relation.relation_sql}) AS __agnes_attachment_row_check__ "
        f"WHERE CAST({quote_ident(decl.id_column)} AS VARCHAR) = $__agnes_attachment_row_id LIMIT 1"
    )
    params = dict(relation.params)
    params["__agnes_attachment_row_id"] = attachment_id

    # The connection OPEN lives inside this same guarded region as the
    # execution below — it can fail for the same infra reasons
    # `_lookup_stored_path`'s own open guards against (read-only open
    # refused while a read-write handle is alive, corrupt/locked file,
    # DuckLake catalog connectivity), and must fail closed exactly like an
    # execution failure rather than propagate past this function.
    try:
        conn = get_analytics_db_readonly()
    except Exception:
        logger.warning(
            "attachment.download: could not open the analytics DB for the row-visibility policy check for table %r",
            rbac_key,
            exc_info=True,
        )
        return False, "policy_check_failed"
    try:
        row = conn.execute(sql, params).fetchone()
    except Exception:
        logger.warning(
            "attachment.download: row-visibility policy check failed to execute for table %r",
            rbac_key,
            exc_info=True,
        )
        return False, "policy_check_failed"
    finally:
        conn.close()
    return (row is not None), None


def _open_contained(root: Path, stored: str | None) -> tuple[BinaryIO | None, os.stat_result | None, str]:
    """Open the catalogue's path value as a safe regular file under ``root``.

    The value is data read from a table, not a trusted constant — so it is
    contained even though the connector wrote it: reject backslashes, NUL
    and ``..`` segments up front, then require the resolved path to stay
    under the resolved root (the guard pattern of ``_resolve_part_path`` in
    ``app/api/data.py``). Returns ``(open file, fstat of that descriptor,
    "")`` on success — fstat of the OPEN descriptor, so the size the
    response advertises is of exactly the inode being streamed; a concurrent
    re-publish of the same path cannot make Content-Length disagree with the
    body — or ``(None, None, reason)`` with reason ``no_path_recorded`` /
    ``path_rejected`` / ``file_missing`` / ``file_unreadable`` for the audit
    trail. Only an absent file (``ENOENT``/``ENOTDIR``, or a non-regular
    inode) is ``file_missing`` — the genuine "no bytes here" the handler may
    render as a miss; any other ``OSError`` (``EACCES``/``EPERM``/``EIO``…)
    is a server-side malfunction, logged and returned as
    ``file_unreadable`` so the handler refuses loudly instead of sending
    the client upstream for bytes the server actually holds. The caller
    owns closing the file.
    """
    if not stored:
        return None, None, "no_path_recorded"
    if "\\" in stored or "\x00" in stored:
        return None, None, "path_rejected"
    candidate = Path(stored)
    if ".." in candidate.parts:
        return None, None, "path_rejected"
    base = root.resolve()
    try:
        resolved = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
    except OSError:
        return None, None, "path_rejected"
    if not resolved.is_relative_to(base):
        logger.warning("attachment catalogue path escapes the permitted root and was not served: %r", stored)
        return None, None, "path_rejected"
    # O_NONBLOCK so a FIFO planted under the root cannot block this
    # threadpool worker until a writer appears — open returns immediately,
    # fstat identifies it, and it is refused before any read. On a regular
    # file (confirmed below) the flag has no effect on reads.
    try:
        fd = os.open(resolved, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            return None, None, "file_missing"
        # EACCES/EPERM/EIO/ELOOP…: the server cannot read a file it
        # catalogued — a malfunction (e.g. the serving process outside the
        # writer's 0o660 group), never a miss. Log it: the 404 path is
        # deliberately quiet, this one must not be.
        logger.warning("attachment file exists in the catalogue but could not be opened: %r (%s)", stored, exc)
        return None, None, "file_unreadable"
    st = os.fstat(fd)
    if not stat_module.S_ISREG(st.st_mode):
        os.close(fd)
        return None, None, "file_missing"
    return os.fdopen(fd, "rb"), st, ""


# Deliberately sync (`def`, not `async def`): everything here blocks —
# `can_access_table`, the analytics lookup (which re-ATTACHes every extract
# and may refresh remote-extension credentials), stat(), and the audit
# write. FastAPI runs sync handlers on the anyio threadpool; declared
# `async`, that same work would hold the single uvicorn event loop and stall
# every other in-flight request (see app/api/mcp_per_table.py for the same
# choice, documented).
@router.get("/{source}/{attachment_id}/download")
def download_attachment(
    source: str,
    attachment_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Stream one connector-catalogued attachment binary.

    Outcomes a client can tell apart:

    - 404 ``unknown_attachment_source`` — ``{source}`` has no declaration
      (decided before any catalogue or filesystem work).
    - 403 — caller lacks read access to the source's catalogue table.
    - 404 ``attachment_not_found`` — no catalogue row with this id, OR the
      row exists but the caller's table access policy hides it (K3):
      deliberately the SAME code/shape as the miss so a policy-hidden row
      is indistinguishable from an absent one.
    - 404 ``attachment_not_stored`` — the row exists but the server holds
      no bytes (over-size skip, transform-time miss, or removed since);
      fall back to the upstream system's own API for these.
    - 503 ``attachment_unreadable`` — the row exists and points at a file
      the server cannot OPEN (permissions/I/O) — a malfunction, logged,
      never disguised as a miss.
    - 200 — the bytes, with the original filename in Content-Disposition.
    """
    decl = get_attachment_source(source)
    if decl is None:
        _audit(user, source, attachment_id, "error.404", error="unknown_attachment_source")
        raise HTTPException(
            status_code=404,
            detail={
                "code": "unknown_attachment_source",
                "hint": f"declared sources: {', '.join(list_attachment_sources()) or '(none)'}",
            },
        )

    # The declaration names the catalogue table; grants key on the registry
    # `id` while master views are named by `name`, and the two are not
    # guaranteed to coincide (see app/api/data.py — it resolves the same
    # way). Resolve the registry row once and use each half for its own job;
    # an unregistered (or unreadable-registry) table falls back to the
    # declared name for both, which then fails closed in `can_access_table`.
    rbac_key = view_name = decl.table
    reg_row = None
    try:
        from src.repositories import table_registry_repo

        reg_row = table_registry_repo().get(decl.table) or table_registry_repo().get_by_name(decl.table)
        if reg_row is not None:
            rbac_key = reg_row["id"]
            view_name = reg_row["name"]
    except Exception:
        logger.exception("attachment.download: registry resolution failed for %s", decl.table)
        # Never fall through: `reg_row=None` would silently skip the
        # `server_only` gate below, releasing bytes a gate that never ran
        # was supposed to hold back — the parquet route 503s for exactly
        # this failure (`_distribution_refusal` in app/api/data.py). An
        # UNREGISTERED table is a different case and still flows past here
        # (reg_row stays None without raising): there is no policy row to
        # fail to read, and RBAC fails it closed for analysts.
        _audit(user, source, attachment_id, "error.503", error="registry_unavailable")
        raise HTTPException(
            status_code=503,
            detail={
                "code": "registry_unavailable",
                "hint": "could not read the table registry to check this table's policy; retry",
            },
        )

    # RBAC first, before any lookup or filesystem work — an unauthorized
    # caller learns nothing beyond the refusal. Written as `denied`, not an
    # error: that is the audit read layer's class for correct policy
    # refusals (src/audit_helpers.py), and a 403 here is never a
    # malfunction, so it must not inflate the Activity Center error bucket.
    if not can_access_table(user, rbac_key, conn):
        _audit(user, source, attachment_id, "denied", table=rbac_key)
        raise HTTPException(status_code=403, detail=table_not_in_stack_message(rbac_key))

    # `server_only` is the admin's "these bytes do not leave the server"
    # lever; the parquet download honours it (`_distribution_refusal`,
    # app/api/data.py) and the route releasing the SAME table's attachment
    # binaries must not be the way around it. Like there, this is not an
    # authorization check — it is a property of the table, true for every
    # caller, admin god-mode included — which is why it runs after RBAC, so
    # an unauthorized caller still learns nothing beyond its own 403. The
    # query-mode allowlist half of that gate is parquet-distribution
    # business and deliberately does not transfer: attachments are lazy
    # per-id fetches, not manifest sync.
    if reg_row is not None and bool(reg_row.get("server_only")):
        _audit(user, source, attachment_id, "denied", error="attachment_table_server_only", table=rbac_key)
        raise HTTPException(
            status_code=403,
            detail={
                "code": "attachment_table_server_only",
                "hint": (
                    "the catalogue table is marked server_only — its attachments "
                    "stay on the server just as its parquet does"
                ),
            },
        )

    # K3 (RLS review #1979): table-level RBAC just cleared is not row-level
    # visibility. Only a REGISTERED table can carry `access_policy_sql` —
    # skip entirely for the unregistered fallback (`reg_row is None`),
    # matching the same "no policy row to fail to read" reasoning the
    # `server_only` check above already applies. Run BEFORE any catalogue
    # lookup so a hidden row and a genuinely absent one both land on the
    # exact same 404 below — telling a caller "the row exists, your policy
    # just denies it" would itself be the disclosure a policy exists to
    # prevent.
    policy_failure_reason = None
    row_visible = True
    if reg_row is not None:
        row_visible, policy_failure_reason = _row_visible_under_access_policy(rbac_key, attachment_id, decl, user)
    if reg_row is not None and not row_visible:
        _audit(
            user,
            source,
            attachment_id,
            "error.404",
            error="attachment_not_found",
            # `policy_check_failed` (finding C, follow-up review of PR
            # #2023) distinguishes "the policy check itself broke" (an
            # infra failure the guard fails closed on) from the default
            # `policied_row_not_visible` -- "the policy ran and genuinely
            # denies this row" -- so ops can tell the two apart in the
            # audit trail. The client-visible 404 body is identical either
            # way; the distinction is audit-only, on purpose (K3).
            reason=policy_failure_reason or "policied_row_not_visible",
        )
        raise HTTPException(
            status_code=404,
            detail={
                "code": "attachment_not_found",
                "hint": f"no row with {decl.id_column}={attachment_id!r} in {view_name}",
            },
        )

    try:
        row_exists, stored, original_name = _lookup_stored_path(decl, view_name, attachment_id)
    except _CatalogueUnavailable as exc:
        # Same posture as `registry_unavailable` above: a catalogue that
        # cannot be queried must refuse loudly, not masquerade as a miss —
        # 404 here sends the client to the upstream API for rows that exist.
        _audit(user, source, attachment_id, "error.503", error="catalogue_unavailable", reason=exc.reason)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "catalogue_unavailable",
                "hint": "the attachment catalogue could not be queried; retry",
            },
        ) from exc
    if not row_exists:
        _audit(user, source, attachment_id, "error.404", error="attachment_not_found")
        raise HTTPException(
            status_code=404,
            detail={
                "code": "attachment_not_found",
                "hint": f"no row with {decl.id_column}={attachment_id!r} in {view_name}",
            },
        )

    fh, st, miss_reason = _open_contained(decl.root(), stored)
    if fh is None or st is None:
        if miss_reason == "file_unreadable":
            # The catalogue says the bytes are here and the OS refused to
            # hand them over (EACCES/EIO…) — a malfunction, same posture as
            # `registry_unavailable`/`catalogue_unavailable`: refuse loudly
            # rather than tell the client the server never stored the file
            # and send every caller upstream while the outage looks normal.
            _audit(user, source, attachment_id, "error.503", error="attachment_unreadable", reason=miss_reason)
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "attachment_unreadable",
                    "hint": (
                        "the catalogued attachment file exists but could not be "
                        "opened (permissions or I/O); server-side misconfiguration — "
                        "see server logs"
                    ),
                },
            )
        _audit(user, source, attachment_id, "error.404", error="attachment_not_stored", reason=miss_reason)
        raise HTTPException(
            status_code=404,
            detail={
                "code": "attachment_not_stored",
                "hint": (
                    "the catalogue row exists but the server holds no bytes for it "
                    "(over-size skip, transform-time miss, or removed since); "
                    "fetch it from the upstream system directly"
                ),
            },
        )

    # Label the download with the name the upstream system shows (Jira
    # stores files as "<id>_<filename>", so the path basename is id-prefixed).
    # The catalogue value is data, not a trusted constant — basename-only,
    # after the same unsafe-byte rejects the path guard applies.
    # fdopen-wrapped descriptors carry no path; the basename of the
    # catalogue value (already past the unsafe-byte rejects) is the same name.
    download_name = Path(stored).name
    if original_name and "\x00" not in original_name and "\\" not in original_name:
        download_name = Path(original_name).name or download_name

    _audit(user, source, attachment_id, "success", bytes=st.st_size, filename=download_name)

    # Same Content-Disposition shapes FileResponse would emit (the CLI
    # parses both): plain when the name survives percent-quoting unchanged,
    # RFC 5987 extended otherwise.
    quoted = quote(download_name)
    disposition = (
        f'attachment; filename="{download_name}"'
        if quoted == download_name
        else f"attachment; filename*=utf-8''{quoted}"
    )

    def _stream(f: BinaryIO = fh, remaining: int = st.st_size):
        # Streamed from the descriptor opened above, bounded at the fstat'd
        # size: Content-Length comes from that same fstat, and the read loop
        # stops exactly there, so the body can never exceed the advertised
        # length (an overrun is an ASGI protocol error, not a truncated
        # response) even if some writer grows the file under the reader.
        # Self-consistency holds by this bound alone; the connectors'
        # atomic publishes (os.replace) additionally keep an overlapping
        # rewrite serving the complete old file rather than a torn one.
        try:
            while remaining > 0 and (chunk := f.read(min(1 << 20, remaining))):
                remaining -= len(chunk)
                yield chunk
        finally:
            f.close()

    return StreamingResponse(
        _stream(),
        media_type="application/octet-stream",
        headers={
            "Content-Length": str(st.st_size),
            "Content-Disposition": disposition,
        },
    )
