"""Extension-to-tier classification for corpus file uploads.

Two tiers decide how an uploaded file is processed:

* **tier1** — text/document formats the ingestion pipeline can currently
  extract text from (PDF, Office, plain text, structured data).
* **tier2** — image formats (PNG, JPEG, GIF, WebP) stored now and processed
  later via vision/OCR (Slice 5); accepted and written to disk today with
  status ``'pending'``. Kept in lock-step with the vision-supported set.
* **bundle** — zip archives (K1) stored like any upload, then unpacked in the
  background into per-member ``corpus_files`` rows by ``src.ingest.bundle``.
* **None** — unsupported; upload is rejected with HTTP 422.

100 MiB ceiling per file. The cap is enforced during streaming by
``src.file_storage.store_corpus_file`` — rejected before any bytes land
on disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TIER1_EXTENSIONS: frozenset[str] = frozenset(
    {
        "txt",
        "md",
        "html",
        "rtf",
        "csv",
        "tsv",
        "json",
        "jsonl",
        "xlsx",
        "parquet",
        "docx",
        "pptx",
        "epub",
        "eml",
        "pdf",
    }
)

# ``msg`` (Outlook) is deliberately absent, for the same reason TIFF is absent
# from tier 2 below: no extractor can read it on ANY build, so accepting it
# would mean an upload that succeeds and is rejected a minute later — and
# "upload it again", the obvious next move, cannot help. Refusing it in the
# upload response says so immediately. Adding it back means a parser first:
# ``extract-msg`` is the only real option and resolves to 15 transitive
# packages (a Tkinter GUI toolkit, two VBA/malware-analysis tools), so it
# belongs behind an opt-in extra rather than in the default image — and then
# the rejection message must name that extra. Exporting the mail as ``.eml``
# is read by the stdlib on every build.

# Must stay in lock-step with the image formats the vision path can actually
# process — ``IMAGE_EXTS`` in ``src/ingest/runner.py`` and ``_EXT_MEDIA`` in
# ``src/ingest/vision.py`` (the Anthropic vision API media types: PNG, JPEG,
# GIF, WebP). A format here that the ingest pipeline can't route to vision
# would be accepted as ``pending`` at upload and then ``rejected`` by the
# background task — violating the tier2 "stored now, processed later" contract.
# TIFF is deliberately absent: the vision API does not accept ``image/tiff``,
# so such uploads are rejected up front with a clear 422.
TIER2_EXTENSIONS: frozenset[str] = frozenset(
    {
        "png",
        "jpg",
        "jpeg",
        "gif",
        "webp",
    }
)

# Archives unpacked server-side into per-member corpus_files rows (K1 bundle
# ingest). Zip only: it covers Confluence HTML/XML space exports and ad-hoc
# document dumps. tar/7z/rar stay rejected — no streaming-unpack guarantees.
BUNDLE_EXTENSIONS: frozenset[str] = frozenset({"zip"})

# 100 MiB — roomy enough for realistic document uploads; blocks accidental
# camera dumps and large binary assets that would swamp the ingestion queue.
MAX_UPLOAD_BYTES: int = 100 * 1024 * 1024


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def classify(filename: str) -> Optional[str]:
    """Return ``'tier1'``, ``'tier2'``, ``'bundle'``, or ``None`` (unsupported / reject).

    Classification is based solely on the file extension (lower-cased).
    Files without an extension always return ``None``.

    Args:
        filename: Original filename from the upload (e.g. ``"report.PDF"``).
                  The stem is irrelevant; only the suffix is examined.

    Returns:
        ``'tier1'`` for text/document formats, ``'tier2'`` for image formats,
        ``'bundle'`` for zip archives, ``None`` for unsupported or
        extension-less files.
    """
    if not filename:
        return None
    suffix = Path(filename).suffix
    if not suffix:
        return None
    ext = suffix.lstrip(".").lower()
    if not ext:
        return None
    if ext in TIER1_EXTENSIONS:
        return "tier1"
    if ext in TIER2_EXTENSIONS:
        return "tier2"
    if ext in BUNDLE_EXTENSIONS:
        return "bundle"
    return None
