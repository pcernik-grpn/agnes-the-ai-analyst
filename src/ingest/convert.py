"""Document → Markdown conversion — the one converter behind every ingest path.

A document is worthless to search and to the fact-graph pipeline until it is
text. This module is the one place that turns a file on disk into markdown,
whoever brought the file: the SharePoint crawl
(``connectors/sharepoint/crawler.py``) and a Collections upload
(``src/ingest/text_extract.py``) both call :func:`convert_to_markdown`, so a
format either of them can read, both can. It lived under the SharePoint
connector first, which is why some of the vocabulary below still speaks of
"the crawl"; nothing in it knows the source.

The single reason it exists in-tree rather than as a dependency is
**licensing**: the reference converter is AGPL-3.0 for exactly one reason — its
PDF route uses PyMuPDF. Agnes ships under PolyForm Small Business 1.0.0, which
cannot vendor AGPL code, so the design constraint from the fact-graph spec
(``docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md``
§9.1) is absolute and applies to every future edit of this file:

* Office and everything else → **markitdown** (MIT; mammoth BSD-2,
  python-pptx MIT, openpyxl MIT underneath);
* ``.pdf`` → **pypdfium2** (Apache-2.0/BSD-3 over BSD-licensed PDFium);
* **never** PyMuPDF / ``fitz`` / any other AGPL dependency, in this module or
  anywhere it reaches.

Routing
-------
``.md/.txt/.csv/.json/.yaml/.yml`` pass through verbatim (UTF-8,
``errors="replace"``) — matching the crawler's historical ``TEXT_SUFFIXES``
behaviour, so a re-crawl produces byte-identical extractions. ``.pdf`` goes to
pypdfium2. Everything else goes to markitdown — except that ``.docx``/``.pptx``
first get **Docling** (the ``[docling]`` extra, MIT) when it is installed,
because its layout-aware parsing keeps tables and reading order that
markitdown flattens; Docling refusing a document is not a failure, markitdown
gets it next. Docling is deliberately NOT offered the PDF route: the structure
pass below is the one PDF pipeline on every image (owner decision
2026-08-31). The declared ``mime`` is only consulted when the filename carries
no suffix we recognise; it is untrusted metadata from a remote drive and is
never used for anything but choosing a route. A caller that stores files under
content-addressed names and keeps the declared type elsewhere passes it as
``suffix``, which then drives routing instead of the path.

Failure model
-------------
A crawl walks thousands of files and must not die on one of them. Every
foreseeable failure — unreadable file, corrupt PDF, a backend blowing up on
malformed input — is raised as :class:`ConversionError`, which names the file.
A missing optional dependency raises :class:`MissingConversionDependency` (a
subclass, so a caller that only catches ``ConversionError`` still survives) and
names the extra to install; the imports are lazy so importing this module on a
server that never converts anything costs nothing and cannot fail.

The PDF route is :mod:`src.ingest.pdf_structure` and nothing else
(owner decision 2026-08-31 — one pipeline, no dual modes). It reconstructs
headings and tables from glyph positions and degrades *per page*, inside
itself, to that page's plain reading-order text whenever the block structure
is ambiguous — a wrong table being worse than no table — so there is no
second, whole-document plain route out here for a failure to fall back to.
Pages are separated by ``\\n\\n---\\n\\n``.

A PDF with no text layer at all (a scan) is **not** an error: it returns
``engine="empty"`` with empty markdown. Transcribing such a scan is a separate
feature with its own cost surface — :mod:`src.ingest.scan_ocr`,
**off by default** behind ``extraction.scan_ocr.enabled``. While it is off
this module never calls a model and the empty result above is byte-identical
to what it always was; with it on, a scan comes back as ``engine="ocr"`` and a
transcription that could not be produced at all is a :class:`ConversionError`,
never a silently empty document.
"""

from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


#: Suffixes read straight off disk. Kept identical to the crawler's historical
#: ``TEXT_SUFFIXES`` so extraction of an already-textual file is a byte copy
#: rather than a round-trip through a converter that would reflow it.
PASSTHROUGH_SUFFIXES = frozenset({".md", ".txt", ".csv", ".json", ".yaml", ".yml"})

#: MIME types that mean "already text" when the filename tells us nothing.
_PASSTHROUGH_MIMES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/x-markdown",
        "text/csv",
        "text/yaml",
        "text/x-yaml",
        "application/json",
        "application/x-yaml",
        "application/yaml",
    }
)

_PDF_MIMES = frozenset({"application/pdf", "application/x-pdf"})

#: Office formats Docling is offered before markitdown when it is installed.
#: Exactly the formats it reads better than markitdown — never ``.pdf`` (the
#: structure pass is the one PDF route) and never the passthrough set.
DOCLING_SUFFIXES = frozenset({".docx", ".pptx"})

#: Office formats that are, by definition, a zip (an OOXML package). Checked
#: before any engine sees the bytes: markitdown sniffs content and reads a
#: non-archive behind one of these suffixes as prose — ASCII garbage comes
#: back as UTF-16 mojibake — and a silently indexed wrong document is worse
#: than a refusal naming the file.
_OOXML_SUFFIXES = frozenset({".docx", ".pptx", ".xlsx"})

#: Separator written between PDF pages.
PAGE_BREAK = "\n\n---\n\n"

#: Default ceiling on returned characters. A single pathological document must
#: not be able to exhaust the crawler's memory or the downstream token budget.
DEFAULT_MAX_CHARS = 5_000_000

#: Below this many non-whitespace characters a PDF is treated as having no
#: usable text layer (a scan). Small enough that a genuinely tiny one-line PDF
#: still converts, large enough to absorb the stray ligature or page number
#: PDFium finds in the margins of a scanned page.
MIN_PDF_TEXT_CHARS = 16

ENGINE_MARKITDOWN = "markitdown"
ENGINE_PYPDFIUM2 = "pypdfium2"
#: Office documents parsed by the ``[docling]`` extra — layout-aware, heavy,
#: opt-in. Its own engine name so a reader can tell a Docling table from
#: markitdown's flattened rendering of the same page.
ENGINE_DOCLING = "docling"
ENGINE_PASSTHROUGH = "passthrough"
ENGINE_EMPTY = "empty"
#: A PDF with no text layer, transcribed by the vision model
#: (:mod:`src.ingest.scan_ocr`). Its own engine name, never
#: ``"pypdfium2"``: a downstream reader must be able to tell text that was read
#: off the page from text a model produced from a bitmap.
ENGINE_OCR = "ocr"


class ConversionError(RuntimeError):
    """A file could not be converted.

    Always carries the filename, because the caller is a crawl loop logging one
    line per document and "UnicodeDecodeError" on its own is unactionable. The
    message never carries file *content* — a conversion failure is frequently a
    malformed document, and its bytes may be confidential.
    """

    def __init__(self, filename: str, message: str, *, engine: str | None = None) -> None:
        self.filename = filename
        self.engine = engine
        super().__init__(f"{filename}: {message}")


class MissingConversionDependency(ConversionError):
    """An optional extraction dependency is not installed.

    A subclass of :class:`ConversionError` so a crawl that catches the base
    class keeps running, but distinct so an operator-facing caller can catch it
    first and stop early: every document of that kind will fail identically
    until someone installs the extra.
    """

    def __init__(self, filename: str, package: str, *, engine: str | None = None) -> None:
        self.package = package
        super().__init__(
            filename,
            f"{package} is not installed — install the extraction extra "
            f"(pip install 'agnes[extraction]') to convert this file type",
            engine=engine,
        )


@dataclass
class ConvertResult:
    """The converted document and which engine produced it.

    ``engine`` is one of ``"docling"``, ``"markitdown"``, ``"pypdfium2"``,
    ``"ocr"``, ``"passthrough"`` or ``"empty"``. ``"empty"`` means conversion
    succeeded and found no text — a scanned PDF, a blank document — and
    ``markdown`` is then ``""``.
    """

    markdown: str
    engine: str


def docling_capability() -> bool:
    """Whether the ``docling`` extra is importable in this deployment.

    An import-spec probe, never an import: Docling pulls torch, and a
    capability question asked while composing an error message (or a
    readiness payload) must not pay that cost. Mirrors
    ``src/ingest/embeddings.py::embedding_capability``, which separates a
    hybrid deployment from a lexical-only one the same way.
    """
    import importlib.util

    return importlib.util.find_spec("docling") is not None


def docling_markdown(path: Path) -> str | None:
    """Docling → markdown, or ``None`` when Docling is absent or refuses the file.

    ``None`` is the signal to try the next engine; an empty string is a
    successful conversion of a document with nothing in it. Docling is present
    but failing on THIS document is logged, never raised: the whole point of
    a second engine is that the caller does not have to care which one read
    the file.
    """
    if not docling_capability():
        return None
    try:
        from docling.document_converter import DocumentConverter  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        result = DocumentConverter().convert(str(path))
        return _normalize_newlines(result.document.export_to_markdown() or "")
    except Exception as exc:  # noqa: BLE001 — any backend failure means "not this engine"
        logger.warning("docling could not convert %s (%s); trying the next engine", path.name, type(exc).__name__)
        return None


def convert_to_markdown(
    path: Path,
    mime: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    suffix: str | None = None,
) -> ConvertResult:
    """Convert one file to markdown.

    Args:
        path: the file on local disk. Must already exist; this function never
            fetches anything and never follows a URL.
        mime: the source system's declared content type. Untrusted, advisory —
            used only when the suffix is unrecognised.
        max_chars: ceiling on returned characters. Output longer than this is
            cut at the limit and a truncation marker appended.
        suffix: the file's declared type (``".pptx"``), for a caller whose
            storage names carry none — Collections keep an upload as
            ``<sha256><ext>`` and the type as a column. When given it replaces
            the path's own suffix for routing and is handed to markitdown as
            the extension to convert as.

    Returns:
        :class:`ConvertResult` — never ``None``, never a partially-written file.

    Raises:
        ConversionError: the file is unreadable, corrupt, or the backend
            refused it. Named so a crawl can log and move on.
        MissingConversionDependency: the backend for this file type is not
            installed.
        ValueError: ``max_chars`` is not positive (a programming error in the
            caller, not a bad document).
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")

    path = Path(path)
    filename = path.name or str(path)
    hint = suffix.lower() if suffix else None
    suffix = hint or path.suffix.lower()
    declared = (mime or "").split(";", 1)[0].strip().lower()

    try:
        if not path.is_file():
            raise ConversionError(filename, "not a readable file")
    except OSError as exc:  # unreadable parent dir, broken symlink, ...
        raise ConversionError(filename, f"cannot stat file: {exc}") from exc

    if suffix in PASSTHROUGH_SUFFIXES or (not suffix and declared in _PASSTHROUGH_MIMES):
        text = _read_text(path, filename, max_chars)
        engine = ENGINE_PASSTHROUGH
    elif suffix == ".pdf" or (not suffix and declared in _PDF_MIMES):
        text, engine = _convert_pdf(path, filename)
    else:
        text, engine = _convert_document(path, filename, suffix, hint)

    if not text.strip():
        # Conversion succeeded and there was nothing in it. Not an error: a
        # scanned PDF and a blank .txt are both legitimate crawl outcomes, and
        # the caller decides whether to route them to a vision transcription
        # pass. This module never calls a model.
        return ConvertResult(markdown="", engine=ENGINE_EMPTY)

    return ConvertResult(markdown=_truncate(text, max_chars), engine=engine)


# --------------------------------------------------------------- passthrough


def _read_text(path: Path, filename: str, max_chars: int) -> str:
    """Read an already-textual file.

    ``errors="replace"`` rather than a decode attempt ladder: the crawler's
    contract is that a file is always indexed, and a mojibake paragraph is a
    better outcome for a fact graph than a dropped document. Reads one
    character past the cap so :func:`_truncate` can tell "exactly at the limit"
    from "over it" without pulling a gigabyte into memory.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read(max_chars + 1)
    except OSError as exc:
        raise ConversionError(filename, f"cannot read file: {exc}", engine=ENGINE_PASSTHROUGH) from exc


# ------------------------------------------------------ docling + markitdown


def _convert_document(path: Path, filename: str, suffix: str, hint: str | None) -> tuple[str, str]:
    """Everything that is neither passthrough nor PDF: Docling first for the
    office formats it reads better, markitdown for the rest and as the second
    reader when Docling refuses a document.

    Returns ``(markdown, engine)``. The one subtle case is Docling present,
    failing, and markitdown NOT installed (a rich image built without the
    ``[extraction]`` extra): that is the file's problem, not a missing extra —
    ``MissingConversionDependency`` would send an operator to install a
    reader for a document Docling already could not read — so it surfaces as
    a plain :class:`ConversionError` attributed to Docling.
    """
    if suffix in _OOXML_SUFFIXES and not zipfile.is_zipfile(path):
        raise ConversionError(filename, f"not an OOXML archive — a '{suffix}' must be a zip package")

    docling_refused = False
    if suffix in DOCLING_SUFFIXES and docling_capability():
        text = docling_markdown(path)
        if text is not None:
            return text, ENGINE_DOCLING
        docling_refused = True
    try:
        return _convert_markitdown(path, filename, file_extension=hint), ENGINE_MARKITDOWN
    except MissingConversionDependency as exc:
        if docling_refused:
            raise ConversionError(
                filename,
                "docling could not convert this file, and no second reader is installed",
                engine=ENGINE_DOCLING,
            ) from exc
        raise


def _convert_markitdown(path: Path, filename: str, *, file_extension: str | None = None) -> str:
    """Office and everything else → markitdown (MIT).

    Imported lazily so this module stays importable without the extraction
    extra, and constructed with ``enable_plugins=False``: markitdown's plugin
    mechanism auto-loads third-party entry points, which would let any package
    that happens to be installed execute code inside the crawl.
    ``file_extension`` is the caller's suffix hint, passed on only when given
    so a path that carries its own extension converts exactly as before.
    """
    try:
        from markitdown import MarkItDown
    except ImportError as exc:
        raise MissingConversionDependency(filename, "markitdown", engine=ENGINE_MARKITDOWN) from exc

    try:
        converter = MarkItDown(enable_plugins=False)
        if file_extension:
            result = converter.convert_local(str(path), file_extension=file_extension)
        else:
            result = converter.convert(str(path))
    except Exception as exc:
        # markitdown surfaces backend failures as many different exception
        # types (its own UnsupportedFormatException, zipfile.BadZipFile,
        # XML parse errors, ...). A crawl cannot enumerate them, and any of
        # them means the same thing: this document did not convert.
        raise ConversionError(
            filename,
            f"markitdown could not convert this file ({type(exc).__name__})",
            engine=ENGINE_MARKITDOWN,
        ) from exc

    text = getattr(result, "text_content", None)
    if text is None:
        text = getattr(result, "markdown", None)
    return _normalize_newlines(text or "")


# ---------------------------------------------------------------------- pdf


def _convert_pdf(path: Path, filename: str) -> tuple[str, str]:
    """PDF → markdown through the structure pass, and only through it.

    :func:`src.ingest.pdf_structure.reconstruct_pdf` is the ONE
    PDF route (owner decision 2026-08-31 — one pipeline, no dual modes). It
    already degrades *internally*, per page: a page whose blocks are
    ambiguous falls back to that page's plain reading-order text and is
    counted in the run's ``degraded_pages``, and a page pdfium cannot load at
    all contributes an empty chunk so the ``---`` separators stay aligned
    with the real page numbers. So there is nothing a second, whole-document
    plain loop out here could rescue — a structure-pass exception means the
    FILE could not be opened as a PDF, which is a :class:`ConversionError`
    the crawler counts in ``convert_failed`` and walks past.

    Takes no ``max_chars``: the cap is applied once, by the caller
    (:func:`convert_to_markdown`'s :func:`_truncate`), for every route.

    Returns ``(markdown, engine)`` — the engine because this is the one route
    with two of them: the structure pass, and the scan-OCR fallback below.
    """
    try:
        # Presence probe for the typed error below: `reconstruct_pdf` imports
        # pypdfium2 lazily too, and an uninstalled extra must surface as
        # MissingConversionDependency naming `agnes[extraction]`, never as a
        # bare ImportError from three frames down.
        import pypdfium2  # noqa: F401
    except ImportError as exc:
        raise MissingConversionDependency(filename, "pypdfium2", engine=ENGINE_PYPDFIUM2) from exc

    from src.ingest.pdf_structure import reconstruct_pdf

    try:
        structured = str(reconstruct_pdf(path)).strip()
    except Exception as exc:
        raise ConversionError(
            filename,
            f"could not convert PDF ({type(exc).__name__})",
            engine=ENGINE_PYPDFIUM2,
        ) from exc

    if len(structured) >= MIN_PDF_TEXT_CHARS:
        return structured, ENGINE_PYPDFIUM2

    # Near-empty means no usable text layer — a scan, or pure vector art.
    # OFF by default (`extraction.scan_ocr.enabled`, a COST switch: ~$0.2-0.5
    # per document at vision rates), and while it is off this returns empty
    # exactly as it always did, so convert_to_markdown reports engine="empty".
    # Enabled, the pages are rendered and transcribed by the vision model and
    # the document comes back as engine="ocr" — and a transcription that could
    # not be produced at all raises, because a caller who turned OCR on must
    # never receive a silently empty document.
    from src.ingest import scan_ocr

    if not scan_ocr.scan_ocr_enabled():
        return "", ENGINE_PYPDFIUM2
    try:
        return scan_ocr.transcribe_scan(path), ENGINE_OCR
    except scan_ocr.ScanOcrUnavailable as exc:
        raise ConversionError(filename, f"scan OCR failed: {exc}", engine=ENGINE_OCR) from exc


# ------------------------------------------------------------------ helpers


def _normalize_newlines(text: str) -> str:
    """PDFium hands back CRLF; markdown downstream assumes LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _truncate(text: str, max_chars: int) -> str:
    """Cut over-long output at the cap and say so, in-band.

    The marker is appended *after* the cut, so the returned string can exceed
    ``max_chars`` by the marker's own length. That is deliberate: a downstream
    reader must be able to tell a truncated document from a short one, and
    silently dropping the notice to stay under an exact byte count would defeat
    the point.
    """
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n[truncated: output exceeded {max_chars} characters]"


__all__ = [
    "ConversionError",
    "ConvertResult",
    "MissingConversionDependency",
    "convert_to_markdown",
    "docling_capability",
    "docling_markdown",
    "DEFAULT_MAX_CHARS",
    "DOCLING_SUFFIXES",
    "ENGINE_DOCLING",
    "ENGINE_EMPTY",
    "ENGINE_MARKITDOWN",
    "ENGINE_OCR",
    "ENGINE_PASSTHROUGH",
    "ENGINE_PYPDFIUM2",
    "PAGE_BREAK",
    "PASSTHROUGH_SUFFIXES",
]
