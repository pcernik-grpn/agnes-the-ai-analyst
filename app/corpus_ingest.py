"""Create artifacts (file corpora) from already-in-memory bytes.

Shared entry point for turning a raw uploaded file into a private **single-file
artifact** (a one-file ``file_corpora`` collection). Used by the chat composer's
"+" upload so a document/image dropped in chat persists as an artifact —
identical to one uploaded on the Artifacts page — instead of living only in the
chat workspace.

Reuses the same primitives as ``app/api/collections.py`` (the file-storage
content-addressing, the ``file_corpora`` / ``corpus_files`` repos via the
backend factory, the extension allowlist, and ``ingest_file``), so no new
repository methods and therefore no DuckDB<->Postgres parity surface is added.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

from src.corpus_allowlist import classify
from src.file_storage import store_corpus_bytes
from src.repositories import corpus_files_repo, file_corpora_repo

logger = logging.getLogger(__name__)

# Mirrors _auto_slug in app/api/collections.py (URL-safe [a-z0-9-], collapsed).
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(name: str) -> str:
    return _SLUG_RE.sub("-", name.lower()).strip("-")[:100].strip("-") or "artefact"


def create_single_file_artefact(
    *,
    owner_id: str,
    filename: str,
    data: bytes,
    description: Optional[str] = None,
) -> Optional[dict]:
    """Create a private one-file artifact owned by ``owner_id``.

    Returns ``{"collection": <row>, "file_id": <str>, "file_type": <str|None>}``
    on success, or ``None`` when the file type is unsupported by the corpus
    allowlist — in that case the caller keeps whatever copy it already made
    (e.g. the chat workspace file) rather than creating a rejected artifact.

    Ingestion is **not** run here; schedule ``src.ingest.runner.ingest_file``
    with the returned ``file_id`` (as a background task) after this returns, so
    chunking/embedding happens off the request path exactly like the Artifacts
    upload flow.
    """
    if classify(filename) is None:
        # Unsupported extension — don't manufacture a rejected artifact from a
        # casual chat drop; the caller's workspace copy is enough.
        return None

    fc_repo = file_corpora_repo()

    # Name the artifact after the file. The Artifacts list presents a one-file
    # artifact AS the file, so the collection name is secondary — but the slug
    # must be unique, and two "report.pdf" drops collide, so retry with a short
    # random suffix on a unique-constraint violation.
    base = _slug(filename.rsplit(".", 1)[0] or filename)
    corpus_id: Optional[str] = None
    for attempt in range(6):
        slug = base if attempt == 0 else f"{base}-{os.urandom(2).hex()}"
        try:
            corpus_id = fc_repo.create(
                name=filename,
                slug=slug,
                description=description,
                created_by=owner_id,
            )
            break
        except Exception as exc:  # DuckDB ConstraintException / PG IntegrityError
            e = str(exc).lower()
            if "unique" in e or "duplicate" in e or "constraint" in e:
                continue
            raise
    if corpus_id is None:
        raise RuntimeError(f"could not allocate a unique slug for artifact '{filename}'")

    stored = store_corpus_bytes(corpus_id, filename, data)
    file_id = corpus_files_repo().add(
        corpus_id=corpus_id,
        filename=filename,
        sha256=stored.sha256,
        file_type=stored.ext.lstrip(".") or None,
        size_bytes=stored.size_bytes,
        storage_path=stored.storage_path,
    )
    row = fc_repo.get(corpus_id)
    logger.info(
        "single-file artifact created id=%s file_id=%s owner=%s from=%s",
        corpus_id,
        file_id,
        owner_id,
        filename,
    )
    return {"collection": row, "file_id": file_id, "file_type": stored.ext.lstrip(".") or None}
