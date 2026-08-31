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

``source["safe_prune"]`` (set from a source's ``config.safe_prune``, see
``src/semantic/transports.py``) opts a source into the full-wipe guard: a
run that produced NO valid documents at all, while rows for this scope
already exist, prunes nothing — neither the stored documents here nor, via
``project_document(safe_prune=...)``, their flat projection. An upstream
that answers with nothing usable is indistinguishable from one whose content
was genuinely deleted, and only the sources whose upstream can do the former
(the migrated Keboola Metastore sync) ask for the guard. Off by default: for
a git or upload source, emptying a model IS the delete signal.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.repositories import semantic_model_repo
from src.semantic.projection import ProjectionReport, _model_key, project_document
from src.semantic.document_validation import validate_document

logger = logging.getLogger(__name__)


@dataclass
class ImportReport:
    models_written: int = 0
    models_unchanged: int = 0
    models_pruned: List[str] = field(default_factory=list)
    invalid: List[dict] = field(default_factory=list)
    projection: Optional[ProjectionReport] = None
    #: F3: this run carried a document for a DETACHED model and deliberately
    #: held it back, so the merged projection is a knowingly incomplete
    #: picture of this (source, source_ref). Reported, not just used locally,
    #: because every caller that gates on "did this pass rewrite everything it
    #: owns?" — `partial` here, the legacy-row purge in
    #: `src/semantic/legacy_migration.py` — has to ask the same question, and
    #: `invalid` alone answers only half of it.
    detached_excluded: bool = False


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


def _stable_suffix(parsed: Optional[Dict[str, Any]], content_hash: str) -> str:
    """The suffix that tells two same-named models apart, stably across runs.

    Prefers the model's own upstream identifier — the same
    ``custom_extensions[AGNES].metastore_id`` the projection keys its row ids
    on, so the stored document and its projection stay in agreement. A
    hand-authored document has no such id (``_model_key`` then falls back to
    the name, which is the very thing that collided), so the document's own
    content hash stands in: stable for as long as the content is, which is
    what keeps the next sync a no-op instead of a prune-and-recreate.
    """
    models = (parsed or {}).get("semantic_model") or [{}]
    model = models[0]
    key = _model_key(model)
    return key if key and key != (model.get("name") or "") else content_hash[:12]


def _disambiguated_slug(
    slug: str, parsed: Optional[Dict[str, Any]], content_hash: str, seen_slugs: set[str]
) -> str:
    """``slug`` itself the first time it appears in a batch, ``slug-<stable
    id>`` after that.

    A model NAME is not unique upstream and the slug is this table's storage
    key, so two documents declaring one name would collapse onto a single row
    — the later silently overwriting the earlier. The alternative tried first
    (report the second as ``invalid``) traded that for a different silent
    loss: the document was never stored, the run was permanently ``partial``
    (narrowing every prune after it), and a previously-stored ``slug-<id>``
    row was deleted by ``delete_missing``. The connector composer this
    pipeline replaced disambiguated instead, under exactly this scheme.
    """
    if slug not in seen_slugs:
        return slug
    candidate = f"{slug}-{_stable_suffix(parsed, content_hash)}"
    suffix = 2
    while candidate in seen_slugs:
        candidate = f"{slug}-{_stable_suffix(parsed, content_hash)}-{suffix}"
        suffix += 1
    logger.warning(
        "Semantic import: two documents declare the model name %r; storing the later one as %r so "
        "neither is lost.",
        slug,
        candidate,
    )
    return candidate


def import_documents(source: dict, documents: List[str]) -> ImportReport:
    report = ImportReport()
    repo = semantic_model_repo()
    src_name = source["source"]
    src_ref = source.get("source_ref")
    safe_prune = bool(source.get("safe_prune"))

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

    for text in documents:
        result = validate_document(text)
        content_hash = _content_hash(text)
        slug = _model_name(result.parsed) if result.ok else None
        if slug is not None:
            # First occurrence keeps the clean slug; a second document with
            # the same model name is stored beside it under `slug-<stable id>`
            # rather than dropped (see `_disambiguated_slug`).
            slug = _disambiguated_slug(slug, result.parsed, content_hash, seen_slugs)

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
                report.detached_excluded = True
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

    if safe_prune and not keep_slugs and existing_rows:
        # Full-wipe guard, the document-level twin of ``project_document``'s
        # own (see the module docstring): this run carried no valid document
        # at all while rows for this scope exist. Deleting them here would
        # also strand their projection — with nothing to project, the
        # projection pass below never runs and never prunes.
        logger.warning(
            "Semantic import (%s/%s): no valid documents this run while %d stored model(s) exist; "
            "skipping the prune to avoid a full wipe. Existing models retained.",
            src_name,
            src_ref,
            len(existing_rows),
        )
    else:
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
            partial=bool(report.invalid) or report.detached_excluded,
            safe_prune=safe_prune,
        )

    return report
