"""Semantic-model / semantic-source admin API, plus the public export and
search surface (open semantic-layer contract, Task 10).

Two tiers:

- ``/api/admin/semantic-models`` and ``/api/admin/semantic-sources`` are
  ``require_admin`` — creating/editing/deleting the canonical Ossie
  documents and configuring where they sync from is an admin action.
- ``/api/semantic-models/{slug}.yaml`` (export) and
  ``/api/semantic-models/search`` are any-authenticated-user, gated instead
  on the linked Data Package's grant (``data_package_semantic_models``) —
  a model rides the same visibility as the package(s) it belongs to, the
  same way a table's visibility rides its package membership
  (``can_access_table``). A model with no linked package is reachable by
  admins only, since there is no package grant to check.

Ownership rule (the only enforcement point for it, since no UI ships in
this task): a model whose ``source`` is not ``'manual'`` was written by a
sync (``import_source``) and refuses edits with 409 ``source_owned`` —
the next sync would silently revert an edit made here, so editing at the
source is the only way to make a change stick.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from app.auth.access import require_admin
from app.auth.dependencies import _get_db, get_current_user
from app.resource_types import ResourceType
from src.repositories import semantic_model_repo, semantic_source_repo
from src.semantic.document_validation import validate_document
from src.semantic.projection import project_document, prune_model
from src.semantic_context import get_semantic_context as _get_semantic_context
from src.semantic_context import get_semantic_schema as _get_semantic_schema
from src.semantic_validation import validate_query

router = APIRouter(tags=["semantic-models"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class SemanticModelCreate(BaseModel):
    document: str
    description: Optional[str] = None


class SemanticModelUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class SemanticQueryValidate(BaseModel):
    sql: str
    expected: Optional[list[dict]] = None
    target_engine: str = "duckdb"


class SemanticSourceCreate(BaseModel):
    kind: str
    name: str
    adapter: str = "native"
    config: dict = {}
    enabled: bool = True


class SemanticSourceUpdate(BaseModel):
    name: Optional[str] = None
    adapter: Optional[str] = None
    config: Optional[dict] = None
    enabled: Optional[bool] = None


_VALID_KINDS = ("git", "upload", "connection")


def _assert_known_adapter(name: str) -> None:
    """Refuse an adapter name nothing is registered under.

    Without this a typo registers happily and only fails on the first sync —
    the "registered but never runs" state the connector validators already
    exist to prevent. `UnknownAdapter` already names what IS available.
    """
    from src.semantic.adapters import UnknownAdapter, get_adapter

    try:
        get_adapter(name)
    except UnknownAdapter as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


# ---------------------------------------------------------------------------
# Access helper — the linked-package grant gate shared by export + search
# ---------------------------------------------------------------------------


def _can_read_model(user: dict, model_row: dict, conn: duckdb.DuckDBPyConnection) -> bool:
    """True iff ``user`` may read ``model_row``: admin, a grant on any Data
    Package the model is linked to, or a direct grant on the model itself.

    The direct grant layers UNDER the package path the way per-table grants
    layer under the package stack. It is not decoration: ``ResourceType
    .SEMANTIC_MODEL`` is registered, so ``/admin/access`` offers it as a
    grantable resource — and a control an admin can set but nothing ever reads
    is worse than no control at all, because it reports success while granting
    nothing.
    """
    from app.auth.access import can_access, is_user_admin

    user_id = user.get("id") if isinstance(user, dict) else None
    if not user_id:
        return False
    if is_user_admin(user_id, conn):
        return True
    if can_access(user_id, ResourceType.SEMANTIC_MODEL.value, model_row["id"], conn):
        return True
    package_ids = semantic_model_repo().list_packages_for_model(model_row["id"])
    return any(can_access(user_id, ResourceType.DATA_PACKAGE.value, pkg_id, conn) for pkg_id in package_ids)


def _export_denied_message(slug: str) -> str:
    return (
        f"Semantic model '{slug}' is not linked to a Data Package you have access to. "
        "Ask an admin to link it to a Data Package you have access to, or grant you one it already belongs to."
    )


def _project(document_json: Optional[dict], *, source: str, source_ref: Optional[str]) -> None:
    """Project one stored model's document into the flat tables
    (``metric_definitions``, ``glossary_terms``, ``column_metadata``) —
    the same call ``src/semantic/importer.py`` makes for a synced source, so
    a model created or edited through this admin API also reaches
    ``agnes catalog --metrics``, chat, and search rather than sitting in
    ``semantic_models`` unread.

    ``partial=True``: every ``source='manual'`` row shares one provenance
    tuple (``source='manual', source_ref=None``) — unlike a git/upload
    source sync, where ``import_documents`` merges every document of ONE
    sync batch before a single ``project_document`` call. Here each POST/PUT
    is its own call for just ONE model, so an unscoped prune would delete a
    *sibling* manual model's already-projected rows on every unrelated
    write. ``partial`` narrows the prune to this document's own model-id
    prefix (see ``project_document``'s docstring), leaving every other
    model's rows untouched.
    """
    if not document_json:
        return
    project_document(document_json, source=source, source_ref=source_ref, partial=True)


def _resolve_model(model_ref: str) -> Optional[dict]:
    """Accept either a model id or its slug — ids are opaque
    (``<source>/<source_ref>/<slug>``), so a slug is the friendlier handle
    for an interactive admin."""
    repo = semantic_model_repo()
    return repo.get(model_ref) or repo.get_by_slug(model_ref)


# ---------------------------------------------------------------------------
# Admin: semantic-models CRUD
# ---------------------------------------------------------------------------


@router.get("/api/admin/semantic-models")
async def list_semantic_models(
    source: Optional[str] = None,
    source_ref: Optional[str] = None,
    user: dict = Depends(require_admin),
):
    """List every stored semantic model (any status), admin-only."""
    return semantic_model_repo().list_all(source=source, source_ref=source_ref)


@router.post("/api/admin/semantic-models", status_code=201)
async def create_semantic_model(
    body: SemanticModelCreate,
    user: dict = Depends(require_admin),
):
    """Create (or replace) a hand-authored (``source='manual'``) model from
    a pasted Ossie document. Invalid input 422s with the schema errors —
    never stored half-valid."""
    result = validate_document(body.document)
    if not result.ok:
        raise HTTPException(status_code=422, detail={"errors": result.errors})

    models = (result.parsed or {}).get("semantic_model") or []
    slug = models[0].get("name") if models else None
    if not slug:
        raise HTTPException(
            status_code=422,
            detail={"errors": ["Document declares no semantic_model entry with a name"]},
        )

    content_hash = hashlib.sha256(body.document.encode()).hexdigest()
    row = semantic_model_repo().upsert(
        id=f"manual/_/{slug}",
        slug=slug,
        name=slug,
        description=body.description,
        document=body.document,
        document_json=result.parsed,
        spec_version=result.spec_version,
        content_hash=content_hash,
        source="manual",
        source_ref=None,
        status="valid",
        validation_errors=None,
        validated_at=datetime.now(timezone.utc),
    )
    _project(result.parsed, source="manual", source_ref=None)
    return row


@router.get("/api/admin/semantic-models/{model_id:path}")
async def get_semantic_model(model_id: str, user: dict = Depends(require_admin)):
    row = _resolve_model(model_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Semantic model '{model_id}' not found")
    return row


@router.put("/api/admin/semantic-models/{model_id:path}")
async def update_semantic_model(model_id: str, body: SemanticModelUpdate, user: dict = Depends(require_admin)):
    row = _resolve_model(model_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Semantic model '{model_id}' not found")
    if row["source"] != "manual":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "source_owned",
                "message": (
                    f"this model is owned by source '{row['source']}'"
                    + (f" (source_ref={row['source_ref']!r})" if row.get("source_ref") else "")
                    + " — edit it there, then re-sync, rather than here"
                ),
            },
        )
    updated = semantic_model_repo().upsert(
        id=row["id"],
        slug=row["slug"],
        name=body.name if body.name is not None else row["name"],
        description=body.description if body.description is not None else row["description"],
        document=row["document"],
        document_json=row["document_json"],
        spec_version=row["spec_version"],
        content_hash=row["content_hash"],
        source=row["source"],
        source_ref=row["source_ref"],
        status=row["status"],
        validation_errors=row["validation_errors"],
        validated_at=row["validated_at"],
    )
    # The document itself is unchanged here (this endpoint only touches
    # name/description), so this is normally a no-op re-projection — a
    # safety net that keeps the projected rows in sync should an earlier
    # write ever have failed to project.
    _project(updated["document_json"], source=updated["source"], source_ref=updated["source_ref"])
    return updated


@router.delete("/api/admin/semantic-models/{model_id:path}", status_code=204)
async def delete_semantic_model(model_id: str, user: dict = Depends(require_admin)):
    row = _resolve_model(model_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Semantic model '{model_id}' not found")
    # Prune the flat projection (metric_definitions/glossary_terms/
    # column_metadata) BEFORE deleting the document row — the row is the
    # only place that still carries the document once this call returns, and
    # `prune_model` needs it to derive the exact model-id prefix `_project`
    # wrote under. Otherwise a model created/edited through this API (which
    # now projects, see `_project`) would leave those rows orphaned forever.
    if row.get("document_json"):
        prune_model(row["document_json"], source=row["source"], source_ref=row["source_ref"])
    semantic_model_repo().delete(row["id"])


# ---------------------------------------------------------------------------
# Admin: semantic-sources CRUD + sync
# ---------------------------------------------------------------------------


@router.get("/api/admin/semantic-sources")
async def list_semantic_sources(enabled_only: bool = False, user: dict = Depends(require_admin)):
    return semantic_source_repo().list_all(enabled_only=enabled_only)


@router.post("/api/admin/semantic-sources", status_code=201)
async def create_semantic_source(body: SemanticSourceCreate, user: dict = Depends(require_admin)):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    if body.kind not in _VALID_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown kind {body.kind!r} (expected one of {', '.join(_VALID_KINDS)})",
        )
    _assert_known_adapter(body.adapter)
    from uuid import uuid4

    source_id = f"ss_{uuid4().hex[:12]}"
    return semantic_source_repo().create(
        id=source_id,
        kind=body.kind,
        name=body.name.strip(),
        adapter=body.adapter,
        config=body.config,
        enabled=body.enabled,
    )


@router.get("/api/admin/semantic-sources/{source_id}")
async def get_semantic_source(source_id: str, user: dict = Depends(require_admin)):
    row = semantic_source_repo().get(source_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Semantic source '{source_id}' not found")
    return row


@router.put("/api/admin/semantic-sources/{source_id}")
async def update_semantic_source(source_id: str, body: SemanticSourceUpdate, user: dict = Depends(require_admin)):
    repo = semantic_source_repo()
    if repo.get(source_id) is None:
        raise HTTPException(status_code=404, detail=f"Semantic source '{source_id}' not found")
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return repo.get(source_id)
    if fields.get("adapter") is not None:
        _assert_known_adapter(fields["adapter"])
    return repo.update(source_id, **fields)


@router.delete("/api/admin/semantic-sources/{source_id}", status_code=204)
async def delete_semantic_source(source_id: str, user: dict = Depends(require_admin)):
    if not semantic_source_repo().delete(source_id):
        raise HTTPException(status_code=404, detail=f"Semantic source '{source_id}' not found")


@router.post("/api/admin/semantic-sources/{source_id}/sync")
async def sync_semantic_source(source_id: str, user: dict = Depends(require_admin)):
    if semantic_source_repo().get(source_id) is None:
        raise HTTPException(status_code=404, detail=f"Semantic source '{source_id}' not found")

    from dataclasses import asdict

    from src.semantic.transports import import_source

    try:
        report = import_source(source_id)
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller, not swallowed
        raise HTTPException(status_code=502, detail=f"sync failed: {exc}") from exc
    return asdict(report)


# ---------------------------------------------------------------------------
# Public: search + export, gated on the linked Data Package's grant
# ---------------------------------------------------------------------------


@router.get("/api/semantic-models/search")
async def search_semantic_models(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(10, ge=1, le=100),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Case-insensitive substring search over slug/name/description, RBAC
    filtered to models linked to a Data Package the caller can access
    (admins see everything)."""
    needle = q.lower()
    matches: list[dict[str, Any]] = []
    for row in semantic_model_repo().list_all():
        haystack = " ".join(filter(None, [row.get("slug"), row.get("name"), row.get("description")])).lower()
        if needle not in haystack:
            continue
        if not _can_read_model(user, row, conn):
            continue
        matches.append(row)
        if len(matches) >= limit:
            break
    return {"query": q, "models": matches, "count": len(matches)}


@router.get("/api/semantic-models/{slug}.yaml")
async def export_semantic_model(
    slug: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Export the stored document byte-for-byte — never re-serialized, so
    comments and key order survive."""
    row = semantic_model_repo().get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Semantic model '{slug}' not found")
    if not _can_read_model(user, row, conn):
        raise HTTPException(status_code=403, detail=_export_denied_message(slug))
    return Response(content=row["document"], media_type="text/yaml")


# ---------------------------------------------------------------------------
# Public: query validation against the caller's semantic models (parity
# spec §5) — same RBAC tier as search/export (a Data Package or direct
# model grant, not admin-only).
# ---------------------------------------------------------------------------


_NO_MODEL_MESSAGE = (
    "No semantic model is available to validate against. Ask an admin to "
    "import or author one (see docs/semantic-layer.md)."
)


def _accessible_valid_documents(
    user: dict, conn: duckdb.DuckDBPyConnection, model_refs: Optional[set[str]] = None
) -> list[dict[str, Any]]:
    """The individual model dicts (``document_json["semantic_model"]``
    entries) of every ``status='valid'`` semantic-model row ``user`` may
    read.

    ``validate_query`` operates on a list of single-model dicts (its own
    module docstring's shape — ``datasets``/``metrics``/``custom_extensions``
    at the top level), not the stored row's full parsed-YAML wrapper
    (``{"semantic_model": [...]}``, per ``document_validation.py``'s Ossie
    schema) — so this unwraps one level. A stored row's ``semantic_model``
    list is usually one entry, but is flattened in full in case a document
    ever declares more than one model.

    Scoped the same way search/export are (``_can_read_model``): a query may
    span more than one row, so every accessible valid document is handed to
    the validator, which unions its detection across all of them.
    """
    documents: list[dict[str, Any]] = []
    # Case-folded once, not per row (the match below is case-insensitive like
    # object-id matching).
    refs_cf = {str(r).casefold() for r in model_refs} if model_refs is not None else None
    for row in semantic_model_repo().list_all():
        if row.get("status") != "valid" or not row.get("document_json"):
            continue
        if not _can_read_model(user, row, conn):
            continue
        models = row["document_json"].get("semantic_model")
        if not isinstance(models, list):
            continue
        model_dicts = [m for m in models if isinstance(m, dict)]
        if refs_cf is None:
            documents.extend(model_dicts)
            continue
        # `model_refs` restricts to specific models, case-insensitively (like
        # object-id matching, so `--model Retail` works as `--id ORDERS` does).
        # An id (`<source>/<source_ref>/<slug>`) or slug match selects the WHOLE
        # row; a match on a document model NAME narrows to THAT model entry only
        # — a multi-model row must not leak the models the caller didn't ask
        # for, and the `model` label each object carries is that name. Accepting
        # the name at all is what lets that returned label round-trip back into
        # `model_ids` (Devin review on #1398).
        if (str(row.get("id") or "")).casefold() in refs_cf or (str(row.get("slug") or "")).casefold() in refs_cf:
            documents.extend(model_dicts)
            continue
        matched = [m for m in model_dicts if str(m.get("name") or "").casefold() in refs_cf]
        documents.extend(matched)
    return documents


@router.post("/api/semantic-models/validate-query")
async def validate_semantic_query(
    body: SemanticQueryValidate,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Validate a SQL statement against the caller's accessible semantic
    models: constraint violations, dialect fit, and (optionally) which
    expected datasets/metrics/relationships it hits.

    Wraps the pure ``src.semantic_validation.validate_query`` — best-effort
    text matching against declared document content, not SQL parsing; see
    that module's own LIMITATIONS. Gated fail-closed: when the caller has no
    accessible ``status='valid'`` model, this returns ``{"available": False,
    ...}`` rather than the engine's all-clear default for an empty document
    list, which would otherwise read as a false "valid: true".
    """
    documents = _accessible_valid_documents(user, conn)
    if not documents:
        return {"available": False, "error": "no_semantic_model", "message": _NO_MODEL_MESSAGE}
    result = validate_query(body.sql, documents, expected=body.expected, target_engine=body.target_engine)
    result["available"] = True
    return result


# ---------------------------------------------------------------------------
# Public: agent read-parity tools (parity spec §4/§5) — get_semantic_context
# and get_semantic_schema. Same RBAC tier as search/export/validate-query (a
# Data Package or direct model grant, not admin-only): read tier, analysts
# and agents are the audience.
# ---------------------------------------------------------------------------


@router.get("/api/semantic-models/context")
async def get_semantic_context_endpoint(
    selections: str = Query(
        ...,
        description=(
            'JSON list of {"semantic_type": "dataset"|"metric"|"relationship", "ids": [...]?} objects. '
            "Absent/empty ids returns every object of that type compactly; explicit ids return full attributes."
        ),
    ),
    model_ids: Optional[list[str]] = Query(
        None,
        description=(
            "Restrict to these models by id, slug, or model name (the `model` label each object "
            "carries; repeatable, case-insensitive); default = every accessible model."
        ),
    ),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Typed context lookup over the caller's accessible semantic models —
    the ``get_semantic_context`` parity tool.

    Wraps the pure ``src.semantic_context.get_semantic_context`` over the
    same ``_accessible_valid_documents`` RBAC tier as search/export/
    validate-query. An empty result (no accessible model, or no object of
    the requested type/id) is not an error — this endpoint has no
    misleading "all clear" to gate against, unlike ``validate-query``.
    """
    try:
        parsed_selections = json.loads(selections)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"selections is not valid JSON: {exc}") from exc
    if not isinstance(parsed_selections, list):
        raise HTTPException(status_code=400, detail="selections must be a JSON list of {semantic_type, ids?} objects")

    documents = _accessible_valid_documents(user, conn, model_refs=set(model_ids) if model_ids else None)
    return _get_semantic_context(documents, parsed_selections)


@router.get("/api/semantic-models/schema")
async def get_semantic_schema_endpoint(
    semantic_types: list[str] = Query(
        ..., description="Object types to describe: dataset, metric, relationship (repeatable)."
    ),
    user: dict = Depends(get_current_user),
):
    """The vendored Apache Ossie JSON Schema for the requested object types —
    the ``get_semantic_schema`` parity tool.

    Not RBAC-gated on any model (there is nothing model-specific to hide —
    it reflects the schema every model is validated against), only on
    ``get_current_user`` — any authenticated user, same floor as the rest of
    this read surface. Served straight from
    ``src.semantic.document_validation``'s vendored, pinned schema, never a
    hand-written copy.
    """
    del user  # authentication-only dependency — nothing model-specific to gate on
    return _get_semantic_schema(semantic_types)
