"""Collections API — file corpus CRUD + multipart upload (Slice 2).

Endpoints:

  POST   /api/collections                         auth (owned by creator)
  GET    /api/collections                         auth (RBAC-filtered list)
  GET    /api/collections/{collection_id}         require_collection_access("{collection_id}")
  DELETE /api/collections/{collection_id}         owner or admin
  POST   /api/collections/{collection_id}/files   require_collection_access("{collection_id}")
  GET    /api/collections/{collection_id}/files   require_collection_access("{collection_id}")
  DELETE /api/collections/{collection_id}/files/{file_id}
                                                  require_collection_access("{collection_id}")
  POST   /api/collections/{collection_id}/files/{file_id}/reingest
                                                  require_collection_access("{collection_id}")
  GET    /api/collections/{collection_id}/files/{file_id}/preview
                                                  collection access OR corpus_file grant
  GET    /api/collections/{collection_id}/files/{file_id}/raw
                                                  collection access OR corpus_file grant

RBAC model: **create** = any authenticated user (the corpus is owned by its
creator and private to them); **delete** = owner or admin; file
**upload/list/delete** and collection **read** = admin, owner
(``created_by``), or any user whose groups hold an explicit
``resource_grants`` row for ``(collection, <collection_id>)`` (see
``can_access_collection``). Admins short-circuit every grant check.

Fail-closed: the GET list returns only collections the caller can access
(granted + owned); unknown collections on entity-scoped endpoints return 404
(not 403) so callers cannot probe for existence of collections they cannot
access.

The two **preview** endpoints widen the read rule by one case — a grant on the
``corpus_file`` itself also grants them — because a file shared out of a folder
has to be viewable by the person it was shared with, who holds no grant on the
parent collection.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.auth.access import (
    accessible_collection_ids,
    can_access_collection,
    is_user_admin,
    require_collection_access,
)
from app.auth.dependencies import get_current_user
from app.services.journey import mark_journey
from src.corpus_allowlist import classify
from src.file_storage import delete_corpus_file, store_corpus_file
from src.sql_ident import quote_ident
from src.repositories import (
    corpus_chunks_repo,
    corpus_files_repo,
    file_corpora_repo,
    table_registry_repo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/collections", tags=["collections"])

# Crash-stuck 'processing' rows must stay recoverable: BackgroundTasks aren't
# durable, so a server crash mid-ingest leaves a corpus_files row parked at
# 'processing' forever. A naive 409 guard on that status would then
# permanently block reingest — the very tool meant to recover it. Past this
# many minutes since the row's last update, 'processing' is treated as stale
# (crash-abandoned) rather than a live in-flight run. Tune upward if Part B's
# long-running ingests routinely exceed this window.
REINGEST_STALE_PROCESSING_MINUTES = 15


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateCollectionRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = None


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _auto_slug(name: str) -> str:
    """Generate a URL-safe slug from a collection name.

    Falls back to ``"collection"`` for names with no alphanumerics (e.g. "!!!"),
    which would otherwise yield an empty slug (degenerate ``/library/`` URL +
    spurious 409 collisions on the second such name).

    The trailing ``strip("-")`` runs *after* the ``[:100]`` cap: truncation can
    re-expose a hyphen at the boundary (a long name whose 100th char lands on a
    word separator), so we strip once more to keep the stored slug clean.
    """
    return _SLUG_RE.sub("-", name.lower()).strip("-")[:100].strip("-") or "collection"


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _collection_out(row: dict) -> dict:
    return {
        "id": row["id"],
        "slug": row["slug"],
        "name": row["name"],
        "description": row["description"],
        "created_by": row["created_by"],
        "created_at": str(row["created_at"]) if row.get("created_at") else None,
        "updated_at": str(row["updated_at"]) if row.get("updated_at") else None,
    }


def _file_out(row: dict) -> dict:
    return {
        "file_id": row["id"],
        "corpus_id": row["corpus_id"],
        "filename": row["filename"],
        "sha256": row["sha256"],
        "file_type": row["file_type"],
        "size_bytes": row["size_bytes"],
        "parent_file_id": row.get("parent_file_id"),
        "path": row.get("path"),
        "processing_status": row["processing_status"],
        "processing_detail": row.get("processing_detail"),
        "created_at": str(row["created_at"]) if row.get("created_at") else None,
    }


# ---------------------------------------------------------------------------
# Collection CRUD
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
async def create_collection(
    payload: CreateCollectionRequest,
    user: dict = Depends(get_current_user),
):
    """Create a new file corpus (any authenticated user).

    The corpus is owned by the creator (``created_by``) and is private to
    them — reachable via ownership without a ``resource_grants`` row (see
    ``can_access_collection``). Admins may additionally grant a corpus to
    groups to share it.

    Returns the created collection object (id, slug, name, …).
    ``slug`` is auto-generated from ``name`` when omitted, and an explicit
    ``slug`` is normalised to a URL-safe form (``[a-z0-9-]``) so it always
    resolves via ``/library/{slug}``; a collision on the unique slug index
    returns **409**.
    """
    # Always normalise through _auto_slug so the stored slug is URL-safe
    # ([a-z0-9-]) and reachable via /library/{slug}, whether it was admin-
    # provided or derived from the name. An explicit slug like "my/collection"
    # becomes "my-collection"; a whitespace-only or all-symbol slug collapses to
    # empty and falls back to the name (then _auto_slug's "collection" default).
    slug = _auto_slug(payload.slug) if (payload.slug or "").strip() else _auto_slug(payload.name)
    repo = file_corpora_repo()
    try:
        corpus_id = repo.create(
            name=payload.name,
            slug=slug,
            description=payload.description,
            created_by=user["id"],
        )
    except Exception as exc:
        # DuckDB raises ConstraintException; PG raises IntegrityError.
        # Both contain "slug" in the message for a UNIQUE collision.
        err = str(exc).lower()
        if "unique" in err or "duplicate" in err or "constraint" in err:
            raise HTTPException(
                status_code=409,
                detail=f"collection_slug_conflict:{slug}",
            ) from exc
        raise

    row = repo.get(corpus_id)
    logger.info("collection created id=%s slug=%s by=%s", corpus_id, slug, user.get("email"))
    # Onboarding step "Add or share something": bringing your own knowledge in
    # starts here — the Library's upload flow creates the collection first, then
    # posts the files into it.
    mark_journey(user.get("id"), catalog_discovered=True)
    return _collection_out(row)


def _accessible_corpus_ids(user) -> list[str]:
    """The collection ids the caller may access (fail-closed).

    Resolves the set **once** via ``accessible_collection_ids`` (admin -> None
    => every collection; ``SessionPrincipal`` co-session callers get their
    intersection set; other non-admins get granted collections plus the ones
    they own) instead of a per-row check. Goes through the repository factory
    (no raw DuckDB conn) → correct on the Postgres backend.
    """
    allowed = accessible_collection_ids(user)
    rows = file_corpora_repo().list_all()
    if allowed is None:
        return [r["id"] for r in rows]
    return [r["id"] for r in rows if r["id"] in allowed]


@router.get("")
async def list_collections(
    user=Depends(get_current_user),
):
    """List collections accessible to the caller (fail-closed)."""
    allowed = accessible_collection_ids(user)  # None => admin
    rows = [r for r in file_corpora_repo().list_all() if allowed is None or r["id"] in allowed]
    return {"items": [_collection_out(r) for r in rows]}


def _empty_search_hint(searched: int, corpus_id: Optional[str]) -> str:
    """Why an empty search is empty, in terms the caller can act on.

    Two different diagnoses share one empty ``results``:

    * ``searched == 0`` — genuinely nothing to search. Telling this caller
      to rephrase would send them in circles; they need a grant.
    * ``searched > 0`` — the corpora were read and nothing matched. The
      three behaviours below are the ones that make a *reasonable* query
      miss, so they are named explicitly rather than left to be inferred
      from a silent empty list:

      - matching is whole-word — ``test`` does not find ``Testovaci``;
      - there is no wildcard — ``*`` and ``""`` return nothing, not
        everything, so "show me what is in here" has no query form.
    """
    if searched == 0:
        if corpus_id:
            return (
                "That collection is not accessible to you, so nothing was searched. "
                "Ask an admin to share it, or call collections_list to see what you can reach."
            )
        return (
            "No collections are shared with you yet, so nothing was searched. "
            "This is an access question, not a query one — call collections_list to confirm, "
            "then ask an admin to grant a collection."
        )
    scope = "the selected collection" if corpus_id else f"{searched} accessible collection(s)"
    return (
        f"Searched {scope} and found no match. You DO have access — this is a wording "
        "miss, not an access problem. Note: matching is whole word (`test` will not find "
        "`Testovaci`) and there is no wildcard (`*` and an empty query return nothing). "
        "File names are searched too, as a fallback when no document body matches — so "
        "nothing here matched either. Try a distinctive word you expect inside the "
        "document, or call collection_get to list the files first."
    )


@router.get("/search")
async def search_collections(
    q: str,
    k: int = 10,
    corpus_id: Optional[str] = None,
    user=Depends(get_current_user),
):
    """Hybrid search across the caller's accessible collections.

    Fail-closed: only the caller's granted collections are searched; an
    optional ``corpus_id`` narrows to one (ignored if not accessible). Declared
    before ``/{collection_id}`` so ``search`` isn't captured as a collection id.

    The response carries ``retrieval`` (``hybrid | lexical_only``) so clients
    can tell semantic-scored results from the lexical-only degradation that
    kicks in when the embeddings extra is not installed (#898).

    An empty result also carries ``searched_collections`` and a ``hint``.
    Nothing in a bare ``[]`` separates "you cannot see any collection" from
    "your words are not in the text", and an agent handed that ambiguity
    picks the scarier reading: observed live, a chat agent searched six
    ways, found nothing, and told the owner of the file "I don't have
    access to your files or collections" — which then became the
    conversation's permanent title. The count is what makes the difference
    checkable, and the hint names the three engine behaviours that make a
    reasonable query miss (see ``src.ingest.retrieval``).
    """
    from src.ingest.retrieval import retrieval_mode, search as _search

    allowed = _accessible_corpus_ids(user)
    # A BLANK `corpus_id` means "no filter", not "the collection whose id is
    # the empty string". `is not None` accepted `?corpus_id=` — which every
    # HTML form and most clients send for an unset optional — narrowed the
    # allowed list to nothing, and then landed in the hint's `searched == 0`
    # branch, telling a caller with plenty of access that no collections are
    # shared with them: the exact wrong conclusion this change set exists to
    # prevent, produced by the fix for it. (Devin Review on this PR.)
    corpus_id = corpus_id or None
    if corpus_id is not None:
        allowed = [c for c in allowed if c == corpus_id]
    k = max(1, min(k, 50))
    results = _search(allowed, q, k=k)
    payload: dict = {"results": results, "retrieval": retrieval_mode()}
    if not results:
        payload["searched_collections"] = len(allowed)
        payload["hint"] = _empty_search_hint(len(allowed), corpus_id)
    return payload


@router.get("/{collection_id}")
async def get_collection(
    collection_id: str,
    user=Depends(require_collection_access("{collection_id}")),
):
    """Return a collection's metadata + file list.

    Requires the caller to hold a grant on this collection (admins exempt).
    Returns **404** (not 403) when the collection does not exist, so that
    unprivileged callers cannot probe for existence via the error code
    difference.
    """
    row = file_corpora_repo().get(collection_id)
    if not row:
        raise HTTPException(status_code=404, detail="collection_not_found")
    files = corpus_files_repo().list_for_corpus(collection_id)
    return {**_collection_out(row), "files": [_file_out(f) for f in files]}


def _purge_derived_tabular_rows(corpus_id: str) -> None:
    """Remove derived table_registry rows + parquet files for a corpus.

    Called synchronously from both ``delete_file`` (single-file variant, by
    table_id) and ``delete_collection`` (corpus-wide variant). After removing
    registry rows we call ``orchestrator.rebuild_source`` so the master views
    in ``analytics.duckdb`` no longer expose the deleted table(s). Best-effort:
    a rebuild failure is logged but not raised — the durable artefacts (registry
    + parquet) are already gone.
    """

    from src.db import _get_data_dir
    from src.orchestrator import SyncOrchestrator

    deleted_ids = table_registry_repo().delete_for_corpus(corpus_id)
    if not deleted_ids:
        return

    source_name = f"collection_{corpus_id}"
    data_dir = _get_data_dir() / "extracts" / source_name / "data"
    ext_db = _get_data_dir() / "extracts" / source_name / "extract.duckdb"

    # Remove parquet files and drop views from extract.duckdb.
    for table_id in deleted_ids:
        parquet = data_dir / f"{table_id}.parquet"
        if parquet.exists():
            try:
                parquet.unlink()
            except OSError as exc:
                logger.warning("could not remove parquet %s: %s", parquet, exc)

    # Drop the views from extract.duckdb (best-effort — DB may not exist yet
    # if the file was never ingested, e.g. processing_status='rejected').
    if ext_db.exists():
        try:
            from src.duckdb_conn import _open_duckdb

            ec = _open_duckdb(str(ext_db))
            try:
                for table_id in deleted_ids:
                    safe_name = table_id.replace('"', '""')
                    ec.execute(f"DROP VIEW IF EXISTS {quote_ident(safe_name)}")
                    ec.execute("DELETE FROM _meta WHERE table_name = ?", [table_id])
            finally:
                ec.close()
        except Exception as exc:
            logger.warning("could not clean extract.duckdb for %s: %s", source_name, exc)

    # Rebuild master views so the deleted tables are no longer queryable.
    try:
        SyncOrchestrator().rebuild_source(source_name)
    except Exception as exc:
        logger.warning("rebuild_source(%s) after derived-table purge failed: %s", source_name, exc)


def _schedule_derived_purge(corpus_id: str, file_id: str | None = None) -> None:
    """Route a derived-table purge to the right executor.

    Worker-role process (single-box ``all``) → run the purge inline, exactly
    as before. Process WITHOUT the worker role (role-split ``api`` replica) →
    enqueue the ``collections-purge`` job so the worker plane performs the
    extract.duckdb surgery + ``rebuild_source`` — the api plane must stay
    analytics-write-free (three-plane spec §3.1). The purge helpers are
    already tolerant of rows/files that vanished between enqueue and run
    (they no-op on missing state), so at-least-once delivery is safe.
    """
    from app.roles import Role, role_enabled

    if role_enabled(Role.WORKER):
        if file_id:
            _purge_derived_tabular_row_for_file(corpus_id, file_id)
        else:
            _purge_derived_tabular_rows(corpus_id)
        return
    from src.repositories import jobs_repo

    row = jobs_repo().enqueue(
        "collections-purge",
        payload={"corpus_id": corpus_id, "file_id": file_id},
        idempotency_key=f"collections-purge:{corpus_id}:{file_id or ''}",
    )
    logger.info(
        "api-role replica: derived purge for corpus=%s file=%s enqueued as job %s (deduped=%s)",
        corpus_id,
        file_id,
        row.get("id"),
        row.get("deduped"),
    )


def _purge_derived_tabular_row_for_file(corpus_id: str, file_id: str) -> None:
    """Variant of ``_purge_derived_tabular_rows`` for a single file deletion.

    The table_id encoding is defined in ``src/ingest/tabular.py``::

        fid_suffix = file_id.replace("cf_", "")[:8]
        table_id = f"collection_{corpus_id}_{base}_{fid_suffix}"

    Rather than re-derive the base from the filename (fragile), we query the
    registry directly for the row whose ``source_table`` ends with the
    fid_suffix, which is a unique-enough discriminator for a given corpus.
    """
    fid_suffix = file_id.replace("cf_", "")[:8]
    source_name = f"collection_{corpus_id}"
    rows = table_registry_repo().list_by_source("collection")
    matching = [r for r in rows if r.get("bucket") == corpus_id and r.get("id", "").endswith(fid_suffix)]
    if not matching:
        return  # non-tabular file or not yet indexed — nothing to purge
    for row in matching:
        table_registry_repo().unregister(row["id"])

    from src.db import _get_data_dir
    from src.orchestrator import SyncOrchestrator

    data_dir = _get_data_dir() / "extracts" / source_name / "data"
    ext_db = _get_data_dir() / "extracts" / source_name / "extract.duckdb"

    for row in matching:
        table_id = row["id"]
        parquet = data_dir / f"{table_id}.parquet"
        if parquet.exists():
            try:
                parquet.unlink()
            except OSError as exc:
                logger.warning("could not remove parquet %s: %s", parquet, exc)

    if ext_db.exists():
        try:
            from src.duckdb_conn import _open_duckdb

            ec = _open_duckdb(str(ext_db))
            try:
                for row in matching:
                    table_id = row["id"]
                    safe_name = table_id.replace('"', '""')
                    ec.execute(f"DROP VIEW IF EXISTS {quote_ident(safe_name)}")
                    ec.execute("DELETE FROM _meta WHERE table_name = ?", [table_id])
            finally:
                ec.close()
        except Exception as exc:
            logger.warning("could not clean extract.duckdb for %s: %s", source_name, exc)

    try:
        SyncOrchestrator().rebuild_source(source_name)
    except Exception as exc:
        logger.warning("rebuild_source(%s) after single-file purge failed: %s", source_name, exc)


@router.delete("/{collection_id}", status_code=204)
async def delete_collection(
    collection_id: str,
    user: dict = Depends(get_current_user),
):
    """Soft-delete a collection (owner or admin).

    The creator can delete their own upload; admins can delete any. Sets
    ``deleted_at``; the collection becomes invisible on GET list and
    returns 404 on entity-scoped reads. Derived table_registry rows, parquets,
    and extract.duckdb views are purged synchronously (they are regenerable from
    the uploaded files; soft-delete of the collection is treated as hard-delete
    for the derived rows).
    """
    row = file_corpora_repo().get(collection_id)
    if not row:
        raise HTTPException(status_code=404, detail="collection_not_found")
    if not is_user_admin(user["id"]) and row.get("created_by") != user["id"]:
        raise HTTPException(status_code=403, detail="collection_not_owned")
    _schedule_derived_purge(collection_id)
    file_corpora_repo().soft_delete(collection_id)
    logger.info("collection deleted id=%s by=%s", collection_id, user.get("email"))


# ---------------------------------------------------------------------------
# File upload / list / delete
# ---------------------------------------------------------------------------


def _purge_file_row(collection_id: str, row: dict, *, keep_blob_path: str | None = None) -> None:
    """Remove a file (and any bundle children) plus their blobs, derived
    tables, chunks, and ``corpus_files`` rows.

    Shared by ``delete_file`` and upsert-on-upload. A bundle archive owns child
    rows (``parent_file_id`` → the archive) each with their own blob, chunks
    and possibly derived tables; those are purged too — otherwise a re-uploaded
    or deleted archive leaves orphaned members that keep surfacing in search.
    Traversal is recursive to be safe, though nested archives aren't ingested.

    Ordering per row mirrors ``delete_collection``: derived purge → chunks →
    row, then blobs last. Chunks never outlive their file (they would surface
    in search with a null filename).

    Blob deletion is refcount-aware: content-addressed blobs are keyed by
    sha256 and NOT refcounted, so two rows with identical bytes share one blob.
    A blob is unlinked only once no surviving row references it — and never
    when it equals ``keep_blob_path`` (the caller just (re)stored a byte-
    identical replacement there, whose row isn't inserted yet).
    """
    cf_repo = corpus_files_repo()
    chunks_repo = corpus_chunks_repo()

    # Collect the row and all descendants (archive → members → …).
    to_delete: list[dict] = [row]
    stack = [row["id"]]
    while stack:
        for child in cf_repo.list_children(stack.pop()):
            to_delete.append(child)
            stack.append(child["id"])

    blob_paths = {r.get("storage_path") for r in to_delete if r.get("storage_path")}

    for r in to_delete:
        _schedule_derived_purge(collection_id, r["id"])
        chunks_repo.delete_for_file(r["id"])
        cf_repo.delete(r["id"])

    # Rows are gone now, so count reflects only survivors. Skip the just-stored
    # replacement blob and any blob another (unrelated) row still references.
    for blob in blob_paths:
        if blob == keep_blob_path:
            continue
        if cf_repo.count_by_storage_path(collection_id, blob) == 0:
            delete_corpus_file(blob)


def _replace_existing_by_path(collection_id: str, path: str | None, *, keep_blob_path: str | None) -> None:
    """Upsert helper: purge any existing file sharing ``(collection_id, path)``.

    No-op when ``path`` is None (plain-insert upload) or nothing matches.
    Called only AFTER the replacement blob is safely stored, so a failed
    re-upload never destroys the existing file.
    """
    if not path:
        return
    existing = corpus_files_repo().get_by_path(collection_id, path)
    if existing:
        _purge_file_row(collection_id, existing, keep_blob_path=keep_blob_path)


@router.post("/{collection_id}/files", status_code=201)
async def upload_files(
    collection_id: str,
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    paths: Optional[List[str]] = Form(None),
    user=Depends(require_collection_access("{collection_id}")),
):
    """Upload one or more files into a collection.

    Each file passes through the extension allowlist:

    * **tier1** (txt, pdf, docx, …) → content-addressed write +
      ``processing_status='pending'``.
    * **tier2** (png, jpg, tiff, …) → same write + ``'pending'``
      (vision/OCR ingestion deferred to Slice 5).
    * **bundle** (zip) → same write + ``'pending'``; the background task
      unpacks it and ingests every supported member as its own child row
      (``parent_file_id`` → the archive row).
    * **unsupported** (.dwg, .exe, …) → stored raw +
      ``processing_status='rejected'`` with ``processing_detail`` describing
      the reason. The *whole response* returns **422** when any file is
      rejected (all results are still returned so the caller sees which
      files succeeded and which were rejected).

    **Upsert:** an optional ``paths`` form field (repeated, paired positionally
    with ``files``) gives each file a caller-supplied logical identity. When a
    file with the same ``(collection_id, path)`` already exists, it is REPLACED
    (old blob/chunks/derived tables purged) instead of inserting a duplicate —
    so a doc-sync client can re-upload idempotently. Files without a ``path``
    keep the legacy plain-insert behavior. The purge runs only after the
    replacement is safely stored, so a failed re-upload never destroys the
    existing file. When ``paths`` is supplied it MUST have exactly one entry
    per file (positional pairing), else the request is rejected with **400** —
    a short/misaligned list would silently assign paths to the wrong files.
    The ``(corpus_id, path)`` invariant is also enforced by a unique index.

    Returns a list of ``{file_id, filename, path, processing_status, …}`` for
    every uploaded file (in upload order).
    """
    # Verify the collection exists (grant check already done by the dependency).
    corpus = file_corpora_repo().get(collection_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="collection_not_found")

    # Positional pairing is only safe when the lists line up 1:1.
    if paths is not None and len(paths) != len(files):
        raise HTTPException(
            status_code=400,
            detail=f"paths_length_mismatch: {len(paths)} paths for {len(files)} files",
        )

    # A duplicate non-blank path within the same batch would replace an
    # earlier file in this same request with a later one — the earlier
    # file's row (and blob) get purged by `_replace_existing_by_path`
    # after its `_file_out` entry and ingest task were already queued, so
    # the response would reference a file_id that no longer exists and
    # schedule a no-op ingest. Reject up front instead of silently
    # dropping a file.
    if paths is not None:
        non_blank = [p.strip() for p in paths if p and p.strip()]
        if len(non_blank) != len(set(non_blank)):
            raise HTTPException(status_code=400, detail="duplicate_path_in_batch")

    cf_repo = corpus_files_repo()
    results = []
    any_rejected = False
    _to_ingest: List[str] = []

    for idx, upload in enumerate(files):
        fname = upload.filename or "unknown"
        tier = classify(fname)
        # Optional per-file logical identity for upsert, paired positionally
        # with `files`. Blank/missing → None (legacy plain-insert).
        path = paths[idx].strip() if (paths and idx < len(paths) and paths[idx]) else None
        path = path or None

        if tier is None:
            # Unsupported type — store raw bytes but record as rejected.
            # Per spec: we do store the bytes (content-addressed, same path)
            # and write a corpus_files row with status='rejected'.
            try:
                stored = await store_corpus_file(collection_id, fname, upload)
                storage_path = stored.storage_path
                sha = stored.sha256
                size = stored.size_bytes
                ext = stored.ext.lstrip(".")
            except HTTPException:
                # Oversize or empty — still record as rejected with no blob.
                storage_path = None
                sha = ""
                size = 0
                ext = fname.rsplit(".", 1)[-1] if "." in fname else ""

            # Upsert only when the blob was actually stored; a failed store
            # must not destroy an existing file, and its row carries no path.
            effective_path = path if storage_path is not None else None
            _replace_existing_by_path(collection_id, effective_path, keep_blob_path=storage_path)
            file_id = cf_repo.add(
                corpus_id=collection_id,
                filename=fname,
                sha256=sha,
                file_type=ext or None,
                size_bytes=size or None,
                storage_path=storage_path,
                path=effective_path,
            )
            cf_repo.set_status(
                file_id,
                status="rejected",
                detail={"reason": "unsupported_type", "filename": fname},
            )
            row = cf_repo.get(file_id)
            results.append(_file_out(row))
            any_rejected = True

        else:
            # tier1 or tier2 — store and mark pending.
            try:
                stored = await store_corpus_file(collection_id, fname, upload)
            except HTTPException as exc:
                # Size cap or empty — treat as rejected so the rest of the
                # batch still processes.
                file_id = cf_repo.add(
                    corpus_id=collection_id,
                    filename=fname,
                    sha256="",
                    file_type=None,
                    size_bytes=None,
                    storage_path=None,
                )
                cf_repo.set_status(
                    file_id,
                    status="rejected",
                    detail={"reason": f"storage_error:{exc.detail}"},
                )
                row = cf_repo.get(file_id)
                results.append(_file_out(row))
                any_rejected = True
                continue

            # Replace any existing file sharing this logical path (no-op when
            # path is None). keep_blob_path guards the content-addressed blob
            # we just stored in case the replacement is byte-identical.
            _replace_existing_by_path(collection_id, path, keep_blob_path=stored.storage_path)
            file_id = cf_repo.add(
                corpus_id=collection_id,
                filename=fname,
                sha256=stored.sha256,
                file_type=stored.ext.lstrip(".") or None,
                size_bytes=stored.size_bytes,
                storage_path=stored.storage_path,
                path=path,
            )
            # Default status is 'pending' (set by the repo on insert).
            row = cf_repo.get(file_id)
            results.append(_file_out(row))
            _to_ingest.append(file_id)
            logger.info(
                "corpus_file uploaded collection=%s file_id=%s sha=%s tier=%s",
                collection_id,
                file_id,
                stored.sha256[:12],
                tier,
            )

    # Kick off Tier-1 ingestion in the background (tabular → registered DuckDB
    # table; documents → chunks). Rejected/unsupported files are not scheduled.
    from src.ingest.runner import ingest_file

    for fid in _to_ingest:
        background_tasks.add_task(ingest_file, fid)

    if any_rejected:
        # Return 422 with full result list so clients know which files
        # succeeded and which were rejected.
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=422, content=results)

    return results


@router.get("/{collection_id}/files")
async def list_files(
    collection_id: str,
    user=Depends(require_collection_access("{collection_id}")),
):
    """List all files in a collection (all processing statuses)."""
    corpus = file_corpora_repo().get(collection_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="collection_not_found")
    files = corpus_files_repo().list_for_corpus(collection_id)
    return {"files": [_file_out(f) for f in files]}


class MoveFileBody(BaseModel):
    target_collection_id: str = Field(min_length=1)


@router.post("/{collection_id}/files/{file_id}/move")
async def move_file(
    collection_id: str,
    file_id: str,
    payload: MoveFileBody,
    user=Depends(require_collection_access("{collection_id}")),
):
    """Move a file into another collection — the Library's drag-and-drop.

    Gated on BOTH ends: the path dependency proves access to the source, and
    the target is re-checked here (otherwise a caller could push a file into
    someone else's collection).

    When the source collection is left empty it is soft-deleted: a single-file
    artefact IS its file in the Library, so dragging that file into a folder
    must not strand an empty husk in the listing.
    """
    target_id = payload.target_collection_id
    if target_id == collection_id:
        raise HTTPException(status_code=400, detail="same_collection")

    cf_repo = corpus_files_repo()
    fc_repo = file_corpora_repo()
    row = cf_repo.get(file_id)
    if not row or row.get("corpus_id") != collection_id:
        raise HTTPException(status_code=404, detail="file_not_found")

    target = fc_repo.get(target_id)
    if not target:
        raise HTTPException(status_code=404, detail="target_not_found")
    if not is_user_admin(user["id"]) and not can_access_collection(user["id"], target_id):
        # 404, not 403 — same reason as everywhere else here: never confirm the
        # existence of a collection the caller can't reach.
        raise HTTPException(status_code=404, detail="target_not_found")

    if not cf_repo.move_to_corpus(file_id, target_id):
        raise HTTPException(status_code=404, detail="file_not_found")

    source_emptied = False
    try:
        if not cf_repo.list_for_corpus(collection_id):
            fc_repo.soft_delete(collection_id)
            source_emptied = True
    except Exception as e:
        logger.warning("move_file: could not tidy empty source %s: %s", collection_id, e)

    logger.info(
        "corpus_file moved file_id=%s from=%s to=%s by=%s (source_emptied=%s)",
        file_id,
        collection_id,
        target_id,
        user.get("email"),
        source_emptied,
    )
    return {
        "file_id": file_id,
        "collection_id": target_id,
        "source_collection_id": collection_id,
        "source_emptied": source_emptied,
    }


@router.delete("/{collection_id}/files/{file_id}", status_code=204)
async def delete_file(
    collection_id: str,
    file_id: str,
    user=Depends(require_collection_access("{collection_id}")),
):
    """Delete a file from a collection.

    Removes the blob from disk (best-effort) and the ``corpus_files`` row.
    """
    cf_repo = corpus_files_repo()
    row = cf_repo.get(file_id)
    if not row or row.get("corpus_id") != collection_id:
        raise HTTPException(status_code=404, detail="file_not_found")
    _purge_file_row(collection_id, row)
    logger.info(
        "corpus_file deleted file_id=%s collection=%s by=%s",
        file_id,
        collection_id,
        user.get("id") if isinstance(user, dict) else "?",
    )


def _is_stale_processing(row: dict) -> bool:
    """True if a ``processing`` row's ``updated_at`` predates the staleness
    threshold — i.e. likely crash-abandoned rather than a live in-flight run.

    ``updated_at`` may come back as a datetime (naive from DuckDB, tz-aware
    from Postgres) or, defensively, as a string — normalise to an aware UTC
    datetime before comparing (mirrors the idiom in
    ``app/api/bq_metadata_refresh.py`` / ``app/auth/pat_resolver.py``).
    """
    updated_at = row.get("updated_at")
    if updated_at is None:
        return True  # no timestamp to trust — don't block recovery on it
    if isinstance(updated_at, str):
        updated_at = datetime.fromisoformat(updated_at)
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=REINGEST_STALE_PROCESSING_MINUTES)
    return updated_at < cutoff


@router.post("/{collection_id}/files/{file_id}/reingest", status_code=202)
async def reingest_file(
    collection_id: str,
    file_id: str,
    background_tasks: BackgroundTasks,
    user=Depends(require_collection_access("{collection_id}")),
):
    """Re-run ingestion for one file (after a fix, a new extractor, or a
    pre-status-honesty backfill).

    Purges the file's derived artifacts first — the derived table_registry
    row/parquet for tabular files (chunks are cleared by the ingest itself,
    which is idempotent) — then resets the row to ``pending`` and re-runs
    ``ingest_file``. Returns 202 with the pending row.

    Worker-role process (single-box ``all``) → purge runs inline, then
    ``ingest_file`` is scheduled as a FastAPI BackgroundTask — unchanged from
    before this endpoint existed on role-split deployments, since purge always
    completes first.

    Process WITHOUT the worker role (role-split ``api`` replica) → purge and
    re-ingest must run as ONE ordered unit on the worker plane, not decoupled:
    an enqueued purge job racing an in-process ``ingest_file`` BackgroundTask
    could have the purge land *after* the re-ingest completes and delete the
    freshly rebuilt table (same deterministic ``table_id``). So a single
    ``collections-purge`` job is enqueued with ``reingest_after_purge=True``;
    the worker handler purges, then calls ``ingest_file`` — always in that
    order, in one job.
    """
    cf_repo = corpus_files_repo()
    row = cf_repo.get(file_id)
    if not row or row.get("corpus_id") != collection_id:
        raise HTTPException(status_code=404, detail="file_not_found")

    # Reject while a run is already in flight so two near-simultaneous
    # requests (second admin tab, direct API caller) don't schedule racing
    # ingest_file executions interleaving chunk deletes/writes. Narrow-window
    # guard, not a lock — a true simultaneous pair can still slip through
    # (accepted; ingest sets 'processing' as its first step). Excludes rows
    # that have been 'processing' for longer than the staleness threshold —
    # BackgroundTasks aren't durable, so a crash mid-ingest would otherwise
    # leave the row permanently stuck and permanently un-reingestable.
    if row.get("processing_status") == "processing" and not _is_stale_processing(row):
        raise HTTPException(status_code=409, detail="reingest_in_progress")

    from app.roles import Role, role_enabled

    if role_enabled(Role.WORKER):
        _purge_derived_tabular_row_for_file(collection_id, file_id)
        cf_repo.set_status(file_id, status="pending", detail={"reason": "reingest requested"})

        from src.ingest.runner import ingest_file

        background_tasks.add_task(ingest_file, file_id)
    else:
        from src.repositories import jobs_repo

        jobs_repo().enqueue(
            "collections-purge",
            payload={"corpus_id": collection_id, "file_id": file_id, "reingest_after_purge": True},
            idempotency_key=f"collections-purge:{collection_id}:{file_id}",
        )
        cf_repo.set_status(file_id, status="pending", detail={"reason": "reingest requested"})

    return {**_file_out(cf_repo.get(file_id))}


# ---------------------------------------------------------------------------
# Preview — "what IS this file?" without a download
# ---------------------------------------------------------------------------

# Formats the browser can render itself, served as the real bytes. Deliberately
# a CLOSED map, not "everything that isn't text": uploads accept `html` (and a
# bundle can carry anything), and serving attacker-authored HTML/SVG inline
# from our own origin is stored XSS against every viewer of the collection.
# Anything absent here is previewed as TEXT or not at all — never streamed
# inline with a type the browser will execute.
_PREVIEW_INLINE_MEDIA: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "pdf": "application/pdf",
}

# Extensions whose stored bytes ARE the text. Everything else that is not in
# `_PREVIEW_INLINE_MEDIA` (docx, xlsx, pptx, parquet, epub, zip …) previews
# through the text ingestion already extracted into `corpus_chunks`, which is
# the only text that exists for those formats. `html` lands here on purpose:
# shown as source, in a `<pre>`, never rendered.
_PREVIEW_TEXTUAL_EXTS: frozenset[str] = frozenset(
    {"txt", "md", "csv", "tsv", "json", "jsonl", "html", "rtf", "eml", "log", "yaml", "yml"}
)

# A preview is a glance, not the file: cap what we read off disk AND what we
# return, so a 100 MiB CSV can't turn a modal into a 100 MiB response.
_PREVIEW_READ_MAX_BYTES = 512 * 1024
_PREVIEW_MAX_CHARS = 20_000


def _readable_file_or_404(collection_id: str, file_id: str, user: dict) -> dict:
    """The file's row, if this caller may read it — else 404.

    Mirrors the per-file access rule the web detail page uses: the parent
    collection's access (admin / owner / group grant) OR a grant on the file
    itself, so a file shared *out* of a folder stays previewable by the person
    it was shared with. 404 (never 403) for missing AND for no-access, matching
    the rest of this module so the URL space can't be probed.
    """
    from app.auth.session_principal import PRINCIPAL_TYPES
    from app.resource_types import ResourceType

    if not file_corpora_repo().get(collection_id):
        raise HTTPException(status_code=404, detail="collection_not_found")
    row = corpus_files_repo().get(file_id)
    if not row or row.get("corpus_id") != collection_id:
        raise HTTPException(status_code=404, detail="file_not_found")

    if isinstance(user, PRINCIPAL_TYPES):
        # A co-session / agent-session caller is not a user dict: its authority
        # IS its intersection — no admin short-circuit, and the per-file grant
        # below never widens it (that would hand an agent a file its scope
        # doesn't name). Fail-closed when the type isn't in the intersection.
        from src.rbac import get_accessible_ids

        allowed = get_accessible_ids(user, ResourceType.COLLECTION.value) or frozenset()
        if collection_id in allowed:
            return row
        raise HTTPException(status_code=404, detail="file_not_found")

    if can_access_collection(user["id"], collection_id):
        return row

    from src.repositories import resource_grants_repo

    try:
        granted = set(resource_grants_repo().list_resource_ids_for_user(user["id"], ResourceType.CORPUS_FILE.value))
    except Exception:  # pragma: no cover - grant lookup is best-effort
        granted = set()
    if file_id in granted:
        return row
    raise HTTPException(status_code=404, detail="file_not_found")


def _blob_path_or_none(row: dict):
    """Resolve a row's blob to a real file inside the corpus storage root.

    `storage_path` is written by ``store_corpus_file`` (never by a caller), but
    it is still a filesystem path read out of the database: realpath-contain it
    under ``${DATA_DIR}/file_corpora`` so a bad row can never make this
    endpoint serve, say, ``/etc/passwd``.

    Returns ``None`` when there is no readable blob — a row can legitimately
    carry no path (an oversize or empty upload is recorded ``rejected`` with
    ``storage_path=None`` but still keeps the extension derived from its
    filename), and a path can outlive its bytes. Callers decide whether that is
    fatal: serving raw bytes has nothing to send, but a *preview* still has the
    extracted text and the status sentence to fall back on.
    """
    from pathlib import Path

    from src.db import _get_data_dir

    raw = row.get("storage_path")
    if not raw:
        return None
    root = (_get_data_dir() / "file_corpora").resolve()
    path = Path(raw).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


def _blob_path_or_404(row: dict):
    """``_blob_path_or_none`` for the callers that cannot degrade gracefully."""
    path = _blob_path_or_none(row)
    if path is None:
        raise HTTPException(status_code=404, detail="file_blob_missing")
    return path


def _no_text_reason(row: dict) -> str:
    """Why this file has no text to show, in a sentence worth relaying.

    Shared by the two branches that can come up empty: a format with no
    extractable text, and an image/PDF whose ingest has not produced chunks
    yet. Non-browser readers (`agnes collections cat`, the
    ``collection_file_read`` MCP tool) print this instead of inventing an
    explanation — "not indexed yet" and "rejected" are very different
    answers to "why can't I read this?".
    """
    status = row.get("processing_status") or "pending"
    return {
        "pending": "This file hasn't been indexed yet — its text preview appears once ingestion runs.",
        "processing": "Indexing is running — its text preview appears when it finishes.",
        "rejected": "This file was rejected during ingestion, so there is no text to preview.",
        "needs_review": "This file needs review before its text can be previewed.",
    }.get(status, "No preview is available for this format.")


def _extracted_text(file_id: str) -> str:
    """Joined chunk text for a file — the only text a docx/xlsx/pdf-scan has."""
    chunks = corpus_chunks_repo().list_for_file(file_id)
    if not chunks:
        return ""
    out: list[str] = []
    total = 0
    for c in chunks:
        text = (c.get("text") or "").strip()
        if not text:
            continue
        out.append(text)
        total += len(text)
        if total >= _PREVIEW_MAX_CHARS:
            break
    return "\n\n".join(out)


@router.get("/{collection_id}/files/{file_id}/preview")
async def preview_file(
    collection_id: str,
    file_id: str,
    user=Depends(get_current_user),
):
    """What to show for this file, and how — the modal's single fetch.

    Returns a `kind` the client renders directly:

    * ``image`` / ``pdf`` — fetch ``raw_url`` and let the browser draw it.
    * ``text`` — ``text`` holds the preview (source for textual uploads, the
      ingested text for formats whose bytes aren't readable), ``truncated``
      says a glance is all this is.
    * ``none`` — nothing to show yet; ``reason`` says why, in the words the
      modal shows the caller.

    Deliberately one endpoint for every format: the client should not have to
    know which extensions are streamable, which are text and which are only
    previewable once ingestion has run.
    """
    row = _readable_file_or_404(collection_id, file_id, user)
    ext = (row.get("file_type") or "").lower()
    base = {
        "file_id": file_id,
        "collection_id": collection_id,
        "filename": row.get("filename"),
        "file_type": ext or None,
        "size_bytes": row.get("size_bytes"),
        "raw_url": None,
        "text": None,
        "truncated": False,
        "source": None,
        "reason": None,
    }

    if ext in _PREVIEW_INLINE_MEDIA:
        # A present blob is still the normal case, and a *text-less* inline
        # medium with no bytes must keep 404ing — a broken <img> in the modal
        # is worse than an honest error, which is why this check was here.
        #
        # But the 404 used to come first unconditionally, and that trade-off
        # stopped being symmetric once non-browser readers existed: `agnes
        # collections cat` and the `collection_file_read` MCP tool cannot draw
        # anything, so for a PDF whose bytes are gone but whose ingested text
        # is sitting in `corpus_chunks` they reported a hard error instead of
        # the answer. The textual branch below already degrades in exactly
        # this situation. So: degrade when there IS text, 404 when there is
        # not. `raw_url` is withheld in the degraded case rather than pointing
        # at an endpoint that would 404 — that is what keeps the modal from
        # rendering the broken embed this check exists to prevent.
        # (Devin Review on this PR.)
        media_blob = _blob_path_or_none(row)
        media_text = _extracted_text(file_id)
        if media_blob is None and not media_text:
            _blob_path_or_404(row)  # raises 404 file_blob_missing
        # The modal draws these from `raw_url` and ignores `text` — but a
        # non-browser reader (`agnes collections cat`, the
        # `collection_file_read` MCP tool) cannot draw anything, and a PDF
        # usually DOES have ingested text. Returning here before the
        # `_extracted_text` branch below made "what is in this PDF?" answer
        # "no text preview is available" while the text sat in corpus_chunks.
        # Attaching it costs the modal one unused field and keeps `kind`
        # (its actual switch) untouched.
        #
        # …untouched EXCEPT when the bytes are gone. `kind` is what the modal
        # switches on (`file_preview.js`), not `raw_url`: `kind: "pdf"` builds
        # an <iframe> and assigns the URL unconditionally, so withholding the
        # URL alone left it pointing at `null` — a blank frame with no error
        # handler, which is the broken embed the 404 existed to prevent, now
        # reached by a different route. (`kind: "image"` degrades better, its
        # <img> has an onerror, but it still throws the text away.) So the
        # degraded case reports what it actually has: `kind: "text"`, which
        # renders the extracted text and the "extracted during indexing" note
        # the textual branch already uses. One server-side decision rather
        # than a second one in the client, so the CLI and MCP readers see the
        # same shape. (Devin Review on this PR, twice.)
        if media_blob is None:
            return {
                **base,
                "kind": "text",
                "text": media_text[:_PREVIEW_MAX_CHARS],
                "truncated": len(media_text) > _PREVIEW_MAX_CHARS,
                "source": "extracted",
            }
        return {
            **base,
            "kind": "image" if ext != "pdf" else "pdf",
            "raw_url": f"/api/collections/{collection_id}/files/{file_id}/raw",
            "text": media_text[:_PREVIEW_MAX_CHARS] or None,
            "truncated": len(media_text) > _PREVIEW_MAX_CHARS,
            "source": "extracted" if media_text else None,
            # A text-less image/PDF must still say why: a bare `text: null`
            # gives a non-browser caller nothing to relay.
            "reason": None if media_text else _no_text_reason(row),
        }

    # Textual formats read their own bytes when they have them. A missing blob
    # is NOT fatal here: an oversize or empty upload is recorded `rejected` with
    # storage_path=None yet keeps the extension from its filename, so 404ing
    # would render the modal's generic "could not be loaded" for exactly the
    # rows whose status sentence ("rejected during ingestion…") is the useful
    # answer — and would throw away extracted text that is already in the DB.
    # Fall through to the same two outcomes every non-textual format gets.
    # Inline media above keeps its 404: there, a broken <img> is worse.
    path = _blob_path_or_none(row) if ext in _PREVIEW_TEXTUAL_EXTS else None
    if path is not None:
        with path.open("rb") as fh:
            # One byte past the cap: enough to know the file continues.
            data = fh.read(_PREVIEW_READ_MAX_BYTES + 1)
        clipped = len(data) > _PREVIEW_READ_MAX_BYTES
        text = data[:_PREVIEW_READ_MAX_BYTES].decode("utf-8", errors="replace")
        truncated = clipped or len(text) > _PREVIEW_MAX_CHARS
        return {
            **base,
            "kind": "text",
            "text": text[:_PREVIEW_MAX_CHARS],
            "truncated": truncated,
            "source": "file",
        }

    text = _extracted_text(file_id)
    if text:
        return {
            **base,
            "kind": "text",
            "text": text[:_PREVIEW_MAX_CHARS],
            "truncated": len(text) > _PREVIEW_MAX_CHARS,
            "source": "extracted",
        }

    return {**base, "kind": "none", "reason": _no_text_reason(row)}


@router.get("/{collection_id}/files/{file_id}/raw")
async def raw_file(
    collection_id: str,
    file_id: str,
    user=Depends(get_current_user),
):
    """Stream a browser-renderable file inline (images + PDF only).

    Serves ONLY the closed ``_PREVIEW_INLINE_MEDIA`` set, with the media type
    taken from that map rather than from anything the uploader controls, plus
    ``nosniff`` so a mislabelled body can't be re-interpreted as HTML. Any
    other extension is 415 with a pointer at the text preview — this endpoint
    is a viewer, not a download route.
    """
    from fastapi.responses import FileResponse

    row = _readable_file_or_404(collection_id, file_id, user)
    ext = (row.get("file_type") or "").lower()
    media = _PREVIEW_INLINE_MEDIA.get(ext)
    if not media:
        raise HTTPException(
            status_code=415,
            detail=(
                f"no inline preview for '.{ext or 'unknown'}' — "
                f"GET /api/collections/{collection_id}/files/{file_id}/preview for its text"
            ),
        )
    path = _blob_path_or_404(row)
    return FileResponse(
        path=str(path),
        media_type=media,
        headers={
            # The blob is named `<sha256><ext>` on disk; `inline` keeps it in
            # the viewer, and the sanitized filename is only a display hint.
            "Content-Disposition": f'inline; filename="{_safe_download_name(row)}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=60",
            # The app-wide defaults are `X-Frame-Options: DENY` +
            # `frame-ancestors 'none'`, which would block the modal's own PDF
            # iframe — same-origin framing included. Narrow both to SELF (never
            # wider) for this one response; SecurityHeadersMiddleware applies
            # its defaults with setdefault, so these win. Images don't need it
            # (an <img> is not framing), but one rule for the endpoint beats a
            # per-extension header set.
            "X-Frame-Options": "SAMEORIGIN",
            "Content-Security-Policy": "frame-ancestors 'self'; object-src 'none'; base-uri 'none'",
        },
    )


def _safe_download_name(row: dict) -> str:
    """Quote-free, path-free filename for a Content-Disposition header."""
    from pathlib import Path

    name = Path(row.get("filename") or "file").name
    return re.sub(r"[^A-Za-z0-9._ -]", "_", name) or "file"
