"""Import Ossie documents into ``semantic_models``, then project the valid
ones into the flat tables (``metric_definitions``, ``glossary_terms``,
``column_metadata``) queries actually read.

Fetching the raw text lives outside this module (``transports.py``, Task 9);
``import_documents`` takes already-fetched documents as an argument so the
whole pipeline — content-hash no-op, invalid-document handling, scoped
prune, projection — can be exercised without a network call or a git clone.

Pipeline, per call:

1. ``validate_document`` every incoming document.
2. Hash each document's raw text. A document whose hash matches what this
   ``(source, source_ref)`` already stored under the same slug is a no-op:
   the write — and its ``updated_at`` bump — is skipped entirely, which is
   what keeps a routine sync of mostly-unchanged content cheap.
3. Upsert everything that changed — valid documents as ``status='valid'``,
   invalid ones as ``status='invalid'`` with their errors, keyed on a stable
   digest of their own text because a malformed document has no slug to key
   on.
4. Prune ``semantic_models`` rows this ``(source, source_ref)`` previously
   wrote whose slug is not among the *valid* documents seen this run.
   Invalid documents are deliberately excluded from that keep-list: a
   document with no name can never legitimately stand in for a real model's
   slug, and letting its digest ride along would only blur
   ``delete_missing``'s guarantee that a sync can only ever delete rows it
   could itself have written.
5. Project every valid document seen this run — merged into one document
   before a single ``project_document`` call, not one call per document.
   ``project_document`` prunes its own output down to exactly what one call
   wrote, scoped to ``(source, source_ref)`` (see
   ``src/semantic/projection.py``); calling it once per sibling document
   sharing that same ``(source, source_ref)`` would have each later call
   prune the earlier call's still-valid rows right back out.

Steps 3-5 run as one unit with no early return in between: a prune that
lands while projection then raises would leave the instance with rows
deleted and nothing written back to replace them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.repositories import semantic_model_repo
from src.semantic.projection import ProjectionReport, project_document
from src.semantic.document_validation import validate_document


@dataclass
class ImportReport:
    models_written: int = 0
    models_unchanged: int = 0
    models_pruned: List[str] = field(default_factory=list)
    invalid: List[dict] = field(default_factory=list)
    projection: Optional[ProjectionReport] = None


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _model_name(parsed: Optional[Dict[str, Any]]) -> Optional[str]:
    """The document's own identity: its first ``semantic_model`` entry's
    ``name``. A document that is schema-valid but declares no models at all
    has nothing to key storage on, so it is treated the same as a
    YAML-invalid document — there is no slug to protect in ``keep_slugs``
    either way."""
    if not parsed:
        return None
    models = parsed.get("semantic_model") or []
    if not models:
        return None
    return models[0].get("name") or None


def import_documents(source: dict, documents: List[str]) -> ImportReport:
    report = ImportReport()
    repo = semantic_model_repo()
    src_name = source["source"]
    src_ref = source.get("source_ref")

    # One scoped read for the whole batch rather than a per-document lookup:
    # `semantic_model_repo().get_by_slug` is not scoped by source, so two
    # sources publishing the same slug could otherwise shadow each other's
    # content-hash comparison.
    existing_rows = repo.list_all(source=src_name, source_ref=src_ref)
    if src_ref is None:
        # `list_all(source_ref=None)` means "unfiltered", but
        # `delete_missing(source_ref=None)` means "the NULL origin" — the two
        # Nones are deliberately different. Without this narrowing the read
        # would span every ref of this source while the prune touched only the
        # NULL one, so a document identical to one owned by ANOTHER ref would
        # be called unchanged and never written to the origin being imported.
        existing_rows = [m for m in existing_rows if not m.get("source_ref")]
    existing_by_slug = {m["slug"]: m for m in existing_rows}

    keep_slugs: List[str] = []
    valid_documents: List[Dict[str, Any]] = []
    seen_slugs: set[str] = set()
    # F3: set when this batch carried a document for a DETACHED model, whose
    # source-side content is deliberately kept out of `valid_documents`. Forces
    # the end-of-batch projection to prune narrowly (see the `partial` argument
    # to `project_document`), because the merged list is then knowingly missing
    # a model that belongs to this (source, source_ref).
    detached_excluded = False

    for text in documents:
        result = validate_document(text)
        content_hash = _content_hash(text)
        slug = _model_name(result.parsed) if result.ok else None

        if slug is not None and slug in seen_slugs:
            # Two documents in one batch declaring the same model name collapse
            # onto a single row — the row id is derived from the slug, so the
            # later one overwrites the earlier one and the report counts both as
            # written. That is silent loss: in a git-backed source it takes only
            # a copied file. First occurrence wins; the duplicate is reported.
            report.invalid.append(
                {
                    "content_hash": content_hash,
                    "errors": [
                        f"duplicate model name {slug!r} in this import; the first document declaring it was kept"
                    ],
                }
            )
            continue

        if slug is not None:
            seen_slugs.add(slug)
            slug_key = slug
            name = slug
            status = "valid"
            errors: Optional[List[str]] = None
            document_json: Optional[Dict[str, Any]] = result.parsed
            keep_slugs.append(slug_key)
        else:
            slug_key = content_hash
            name = slug_key
            status = "invalid"
            errors = list(result.errors) if result.errors else ["Document declares no semantic_model entries"]
            document_json = None
            report.invalid.append({"content_hash": content_hash, "errors": errors})

        existing = existing_by_slug.get(slug_key)
        if existing is not None and existing.get("sync_mode") == "detached":
            # F3: a detached row is never overwritten by sync — the admin's
            # local edit stays authoritative. Park the latest hash seen from
            # the source (cheap no-op write once it stops changing) so the
            # "source changed since you detached" indicator and re-attach
            # preview can read it without a live fetch.
            #
            # The source's version is also kept OUT of `valid_documents`, so
            # the end-of-batch `project_document` never re-projects it. Both
            # sides write the flat tables (`metric_definitions`,
            # `glossary_terms`, `column_metadata`) under ids scoped to
            # (source, source_ref) — which a detached row deliberately keeps —
            # so leaving it in the batch would silently overwrite the admin's
            # locally-edited projection with the source's content on EVERY
            # sync, while the stored document kept the edit. The admin's edit
            # path (`apply_manual_model` / `update_semantic_model`, both via
            # `_project`) owns this model's projection from detach onward.
            if content_hash != existing.get("source_content_hash"):
                repo.update_source_content_hash(existing["id"], content_hash)
            if status == "valid":
                report.models_unchanged += 1
                detached_excluded = True
            continue

        if status == "valid":
            valid_documents.append(document_json)  # type: ignore[arg-type]

        if existing is not None and existing.get("content_hash") == content_hash:
            if status == "valid":
                report.models_unchanged += 1
            continue

        repo.upsert(
            id="/".join([src_name, src_ref or "_", slug_key]),
            slug=slug_key,
            name=name,
            description=None,
            document=text,
            document_json=document_json,
            spec_version=result.spec_version,
            content_hash=content_hash,
            source=src_name,
            source_ref=src_ref,
            status=status,
            validation_errors=errors,
            validated_at=datetime.now(timezone.utc),
        )
        if status == "valid":
            report.models_written += 1

    # F3: a detached row's slug not in `keep_slugs` means the source stopped
    # sending it this run — track that as "missing", never delete it
    # (`delete_missing` already excludes detached rows on its own). A slug
    # that comes back after being missing gets the marker cleared.
    for existing in existing_rows:
        if existing.get("sync_mode") != "detached":
            continue
        if existing["slug"] in keep_slugs:
            if existing.get("source_missing_since") is not None:
                repo.clear_source_missing(existing["id"])
        else:
            repo.mark_source_missing(existing["id"])

    report.models_pruned = repo.delete_missing(source=src_name, source_ref=src_ref, keep_slugs=keep_slugs)

    if valid_documents:
        merged = {"semantic_model": [m for doc in valid_documents for m in (doc.get("semantic_model") or [])]}
        report.projection = project_document(
            merged,
            source=src_name,
            source_ref=src_ref,
            # `partial` when a document failed validation OR when a detached
            # model was held back (F3): either way the merged list is an
            # incomplete picture of this (source, source_ref), and a
            # full-scope prune would delete the absent model's own rows —
            # for a detached model, precisely the locally-edited projection
            # this sync just took care not to overwrite.
            partial=bool(report.invalid) or detached_excluded,
        )

    return report
