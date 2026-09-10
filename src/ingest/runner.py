"""Ingestion router — drive a single uploaded file through Tier-1 ingestion.

Routes by file type: tabular → a registered DuckDB table; prose → ``corpus_chunks``
(text only; embeddings are Slice 4); images (tier-2) → left ``pending`` for the
vision slice (Slice 5). Moves ``processing_status`` pending → processing →
indexed | needs_review | rejected. Idempotent: re-ingesting a document replaces its chunks.
"""

from __future__ import annotations

import logging

from src.ingest.chunking import chunk_text
from src.ingest.tabular import EmptyExtraction, UnsupportedTabular, ingest_tabular
from src.ingest.text_extract import _PLAIN_EXTS, ExtractResult, UnsupportedDocument, extract_text
from src.repositories import corpus_chunks_repo, corpus_files_repo, file_corpora_repo

logger = logging.getLogger(__name__)

TABULAR_EXTS = {"csv", "tsv", "parquet", "json", "jsonl", "xlsx", "xls"}
# Only formats the vision API (src/ingest/vision._EXT_MEDIA) can actually read.
# tif/tiff/bmp would route to vision but always return None → stuck 'pending'
# with a misleading reason, so they fall through to the text path and get an
# honest "no extractor" rejection instead (conversion is a future slice).
IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp"}


def _ext_of(filename: str, file_type: str | None) -> str:
    if file_type and "/" not in file_type:
        return file_type.lower().lstrip(".")
    if "." in filename:
        return filename.rsplit(".", 1)[-1].lower()
    return ""


def _chunk_embed_store(corpus_id: str, file_id: str, source) -> tuple[int, bool]:
    """Chunk ``source`` (ExtractResult or str), embed best-effort, store.

    Returns ``(chunk_count, embedded)``. Idempotent: clears the file's prior
    chunks first. Embedding is best-effort (optional extra); failure → vectors
    NULL and lexical-only retrieval, never an ingest failure.

    ``corpus_id`` is re-read from the file row here rather than trusted from
    the caller. ``ingest_file`` reads it once at the top and everything slow
    happens in between (conversion, OCR, embedding), so a file MOVED to
    another collection in that window would otherwise have these chunks
    written under the collection it has already left: the move re-homes the
    rows that exist when it runs and cannot touch rows written after it, so
    the ingest would silently restore the very leak the move closed
    (``app/api/collections.py::move_file``). The passed value stays the
    fallback for a file row that has since disappeared.
    """
    chunks = chunk_text(source)
    chunks_repo = corpus_chunks_repo()
    current = corpus_files_repo().get(file_id) or {}
    corpus_id = current.get("corpus_id") or corpus_id
    chunks_repo.delete_for_file(file_id)
    rows = [
        {
            "corpus_id": corpus_id,
            "file_id": file_id,
            "ordinal": c.ordinal,
            "text": c.text,
            "section_path": c.section_path,
        }
        for c in chunks
    ]
    embedded = False
    try:
        from src.ingest.embeddings import embed_texts

        vectors = embed_texts([r["text"] for r in rows]) if rows else None
        if vectors is not None and len(vectors) == len(rows):
            for r, v in zip(rows, vectors):
                r["embedding"] = v
            embedded = True
    except Exception:  # pragma: no cover - model runtime issues
        logger.warning("embedding failed for file_id=%s — storing without vectors", file_id)
    n = chunks_repo.add_many(rows)
    return n, embedded


def ingest_file(file_id: str, *, preloaded_text: str | None = None, image_count: int = 0) -> str:
    """Ingest one uploaded file. Returns the final ``processing_status``.

    ``preloaded_text`` lets a caller that already holds the document's TEXT
    in memory (the SharePoint crawl: ``_prepare_document`` converts a source
    file to markdown, then ``_Ingestor.ingest`` writes it and calls this
    function) skip the redundant ``extract_text`` read of the SAME content
    back off disk — a real, measured contributor to that crawl's
    parent-process memory pressure: a second full-size copy of the
    converted markdown on top of every copy convert/anonymize/encode/store
    already held. Only used for the plain-text branch below (the one
    ``extract_text`` itself would read verbatim, no transform) — a
    caller's preloaded text for any other extension is ignored and the
    normal disk read runs, since e.g. the HTML branch still needs
    ``_strip_html`` applied.

    ``image_count`` is the SAME ``src.ingest.convert.ConvertResult.
    image_count`` a disk-read ``extract_text`` call would have picked up on
    its own office branch — but a ``preloaded_text`` caller converted the
    document itself, BEFORE this function ever sees it, so there is no
    ``ExtractResult`` here to carry that count unless the caller hands it
    over explicitly (live finding 2026-09-09: the SharePoint crawl's
    preloaded-text path recorded ``image_count: 0`` on every document,
    even one whose disclosed-image markdown was sitting right there in
    ``preloaded_text``). Ignored for every other branch — they build their
    own ``ExtractResult`` via ``extract_text``, which already carries its
    own count.
    """
    cf_repo = corpus_files_repo()
    row = cf_repo.get(file_id)
    if not row:
        return "missing"
    if row.get("processing_status") == "rejected":
        return "rejected"  # already rejected at upload (unsupported type)

    corpus_id = row["corpus_id"]
    filename = row.get("filename") or ""
    storage_path = row.get("storage_path")
    file_type = row.get("file_type")

    if not storage_path:
        cf_repo.set_status(file_id, status="rejected", detail={"reason": "no_storage_path"})
        return "rejected"

    cf_repo.set_status(file_id, status="processing")
    ext = _ext_of(filename, file_type)

    try:
        if ext == "zip":
            # Bundle (K1): unpack + per-member child ingestion. ingest_bundle
            # sets the archive row's own status in every path.
            import src.ingest.bundle as _bundle

            return _bundle.ingest_bundle(corpus_id, file_id, storage_path)

        if ext in TABULAR_EXTS:
            # Stamp the uploader as owner so the derived table is queryable by
            # them (rbac.can_access_table admits collection-derived tables via the
            # owning collection's access). Fall back to "ingest" if the corpus
            # owner can't be resolved.
            try:
                _corpus = file_corpora_repo().get(corpus_id)
                _owner = (_corpus or {}).get("created_by") or "ingest"
            except Exception:
                _owner = "ingest"
            table_id = ingest_tabular(
                corpus_id, file_id, storage_path, file_type, filename=filename, registered_by=_owner
            )
            cf_repo.set_status(
                file_id,
                status="indexed",
                detail={"tier": 1, "kind": "tabular", "derived_table_id": table_id},
            )
            return "indexed"

        if ext in IMAGE_EXTS:
            # Tier-2 — try the gated vision fallback (multimodal OCR). Without a
            # configured model/key it returns None and we leave the file pending
            # so a later, configured run can pick it up (not an error).
            from src.ingest.vision import extract_image_text

            text = extract_image_text(storage_path, ext=ext)
            if not text:
                cf_repo.set_status(
                    file_id,
                    status="pending",
                    detail={"tier": 2, "kind": "image", "note": "awaiting vision (no model/key)"},
                )
                return "pending"
            n, embedded = _chunk_embed_store(corpus_id, file_id, text)
            if n == 0:
                cf_repo.set_status(
                    file_id,
                    status="needs_review",
                    detail={"tier": 2, "kind": "image", "reason": "extraction produced no text chunks"},
                )
                return "needs_review"
            cf_repo.set_status(
                file_id,
                status="indexed",
                detail={"tier": 2, "kind": "image", "chunk_count": n, "vision_used": True, "embedded": embedded},
            )
            return "indexed"

        # Prose document → extract + chunk → corpus_chunks. A preloaded text
        # is only trusted for the plain-text branch `extract_text` itself
        # would take (`_PLAIN_EXTS` — see this function's docstring): any
        # other extension still needs its real transform (e.g. HTML's
        # `_strip_html`), so a mismatched `preloaded_text` there is ignored
        # rather than risking un-transformed content reaching storage.
        if preloaded_text is not None and (ext in _PLAIN_EXTS or ext == ""):
            result: ExtractResult | str = ExtractResult(full_text=preloaded_text, image_count=image_count)
        else:
            result = extract_text(storage_path, file_type)
        # 0 for every non-office reader (plain text, HTML, .eml, .epub, PDF)
        # — none of them can ever embed a picture markitdown drops; see
        # `ExtractResult.image_count`'s docstring for why this is the one
        # field this function reads off `result` before handing it to
        # `_chunk_embed_store`, which only wants `full_text`/`elements`.
        image_count = result.image_count if isinstance(result, ExtractResult) else 0
        n, embedded = _chunk_embed_store(corpus_id, file_id, result)
        if n == 0:
            cf_repo.set_status(
                file_id,
                status="needs_review",
                detail={"tier": 1, "kind": "document", "reason": "extraction produced no text chunks"},
            )
            return "needs_review"
        cf_repo.set_status(
            file_id,
            status="indexed",
            detail={"tier": 1, "kind": "document", "chunk_count": n, "embedded": embedded, "image_count": image_count},
        )
        return "indexed"

    except (UnsupportedTabular, UnsupportedDocument) as exc:
        cf_repo.set_status(file_id, status="rejected", detail={"reason": str(exc)})
        return "rejected"
    except EmptyExtraction as exc:
        cf_repo.set_status(
            file_id,
            status="needs_review",
            detail={"tier": 1, "kind": "tabular", "reason": str(exc)},
        )
        return "needs_review"
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("ingest_file failed file_id=%s", file_id)
        cf_repo.set_status(file_id, status="rejected", detail={"reason": f"ingest_error: {exc}"})
        return "rejected"
