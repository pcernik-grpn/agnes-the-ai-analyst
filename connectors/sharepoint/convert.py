"""Document → Markdown conversion for the SharePoint connector.

A crawled document is worthless to the fact-graph pipeline until it is text.
This module is the one place that turns a downloaded file into markdown, and
the single reason it exists in-tree rather than as a dependency is
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
pypdfium2. Everything else goes to markitdown. The declared ``mime`` is only
consulted when the filename carries no suffix we recognise; it is untrusted
metadata from a remote drive and is never used for anything but choosing a
route.

Failure model
-------------
A crawl walks thousands of files and must not die on one of them. Every
foreseeable failure — unreadable file, corrupt PDF, a backend blowing up on
malformed input — is raised as :class:`ConversionError`, which names the file.
A missing optional dependency raises :class:`MissingConversionDependency` (a
subclass, so a caller that only catches ``ConversionError`` still survives) and
names the extra to install; the imports are lazy so importing this module on a
server that never converts anything costs nothing and cannot fail.

v1 scope limits (deliberate, documented)
----------------------------------------
The PDF route extracts *text in reading order*. It does **not** reconstruct
headings, tables, lists, or any other markdown structure — a PDF converts to
plain paragraphs separated by blank lines, with pages separated by
``\\n\\n---\\n\\n``. Reading order is recovered by sorting text rectangles
top-to-bottom then left-to-right *only when PDFium's own order is already
scrambled*; a multi-column PDF whose native order is column-major but not
globally monotone will therefore be re-ordered row-wise, interleaving the
columns. Both are known v1 limits, not bugs.

A PDF with no text layer at all (a scan) is **not** an error: it returns
``engine="empty"`` with empty markdown. Transcribing scans is a separate
feature with its own cost surface — this module never calls a model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Sequence


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
ENGINE_PASSTHROUGH = "passthrough"
ENGINE_EMPTY = "empty"

#: Vertical slack, in PDF points, within which two text rectangles count as
#: sitting on the same line.
_LINE_TOLERANCE_PT = 3.0


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

    ``engine`` is one of ``"markitdown"``, ``"pypdfium2"``, ``"passthrough"``
    or ``"empty"``. ``"empty"`` means conversion succeeded and found no text —
    a scanned PDF, a blank document — and ``markdown`` is then ``""``.
    """

    markdown: str
    engine: str


def convert_to_markdown(
    path: Path,
    mime: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> ConvertResult:
    """Convert one file to markdown.

    Args:
        path: the file on local disk. Must already exist; this function never
            fetches anything and never follows a URL.
        mime: the source system's declared content type. Untrusted, advisory —
            used only when the suffix is unrecognised.
        max_chars: ceiling on returned characters. Output longer than this is
            cut at the limit and a truncation marker appended.

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
    suffix = path.suffix.lower()
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
        text = _convert_pdf(path, filename, max_chars)
        engine = ENGINE_PYPDFIUM2
    else:
        text = _convert_markitdown(path, filename)
        engine = ENGINE_MARKITDOWN

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


# --------------------------------------------------------------- markitdown


def _convert_markitdown(path: Path, filename: str) -> str:
    """Office and everything else → markitdown (MIT).

    Imported lazily so this module stays importable without the extraction
    extra, and constructed with ``enable_plugins=False``: markitdown's plugin
    mechanism auto-loads third-party entry points, which would let any package
    that happens to be installed execute code inside the crawl.
    """
    try:
        from markitdown import MarkItDown
    except ImportError as exc:
        raise MissingConversionDependency(filename, "markitdown", engine=ENGINE_MARKITDOWN) from exc

    try:
        result = MarkItDown(enable_plugins=False).convert(str(path))
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


def _convert_pdf(path: Path, filename: str, max_chars: int) -> str:
    """PDF → markdown: structure pass first, plain reading-order as fallback.

    The default path is :func:`connectors.sharepoint.pdf_structure.reconstruct_pdf`
    (headings + tables from glyph positions; it degrades per page to the same
    reading-order text this module's plain loop produces, so it is never
    worse). The plain loop below remains the fallback when the structure pass
    itself fails. Pages are separated by :data:`PAGE_BREAK`; a page with no
    text contributes nothing but still consumes a separator, so page
    numbering stays meaningful.
    """
    try:
        import pypdfium2 as pdfium  # type: ignore[import-untyped]
    except ImportError as exc:
        raise MissingConversionDependency(filename, "pypdfium2", engine=ENGINE_PYPDFIUM2) from exc

    try:
        from connectors.sharepoint.pdf_structure import reconstruct_pdf

        structured = str(reconstruct_pdf(path)).strip()
    except Exception as exc:  # noqa: BLE001 — degrade to plain extraction below
        logger.warning(
            "sharepoint.convert: structure pass failed for %s (%s); using plain extraction",
            filename,
            type(exc).__name__,
        )
    else:
        # Same no-text-layer contract as the plain path: near-empty means a
        # scan (engine="empty" upstream), never a different engine.
        return structured if len(structured) >= MIN_PDF_TEXT_CHARS else ""

    try:
        document = pdfium.PdfDocument(str(path))
    except Exception as exc:
        raise ConversionError(
            filename,
            f"could not open PDF ({type(exc).__name__})",
            engine=ENGINE_PYPDFIUM2,
        ) from exc

    pages: List[str] = []
    total = 0
    try:
        for index in range(len(document)):
            try:
                page = document[index]
                page_text = _extract_page_text(page)
            except Exception as exc:
                # One damaged page does not invalidate the rest of the
                # document; record the gap and carry on.
                logger.warning(
                    "sharepoint.convert: skipping unreadable page %d of %s (%s)",
                    index + 1,
                    filename,
                    type(exc).__name__,
                )
                page_text = ""
            pages.append(page_text)
            total += len(page_text)
            if total > max_chars:
                break
    except Exception as exc:  # malformed page tree — len()/indexing itself fails
        raise ConversionError(
            filename,
            f"corrupt PDF structure ({type(exc).__name__})",
            engine=ENGINE_PYPDFIUM2,
        ) from exc
    finally:
        try:
            document.close()
        except Exception:  # pragma: no cover - defensive
            pass

    text = PAGE_BREAK.join(pages).strip()
    if len(text.strip()) < MIN_PDF_TEXT_CHARS:
        # No usable text layer: a scan, or a PDF of pure vector art. Signalled
        # by returning empty so convert_to_markdown reports engine="empty".
        return ""
    return text


@dataclass(frozen=True)
class _Segment:
    """One text rectangle on a page, in PDF user space (origin bottom-left)."""

    top: float
    left: float
    text: str


def _extract_page_text(page: Any) -> str:
    """Text of a single page, in reading order."""
    textpage = page.get_textpage()
    try:
        segments = _page_segments(textpage)
        if not segments:
            return _normalize_newlines(textpage.get_text_range()).strip()
        if not _is_reading_order(segments):
            segments = _sorted_reading_order(segments)
        return _render_lines(segments)
    finally:
        try:
            textpage.close()
        except Exception:  # pragma: no cover - defensive
            pass


def _page_segments(textpage: Any) -> List[_Segment]:
    """Text rectangles of a page, in PDFium's own order."""
    try:
        count = textpage.count_rects()
    except Exception:  # pragma: no cover - PDFium refused the page
        return []
    segments: List[_Segment] = []
    for index in range(max(count, 0)):
        left, bottom, right, top = textpage.get_rect(index)
        text = textpage.get_text_bounded(left=left, bottom=bottom, right=right, top=top)
        text = _normalize_newlines(text or "").strip()
        if text:
            segments.append(_Segment(top=float(top), left=float(left), text=text))
    return segments


def _is_reading_order(segments: Sequence[_Segment]) -> bool:
    """Is PDFium's native order already top-to-bottom, left-to-right?

    Native order usually *is* reading order — it follows the content stream,
    which authoring tools emit in reading order — and it handles multi-column
    layouts that a naive geometric sort would interleave. So it is kept unless
    it is demonstrably scrambled.
    """
    for previous, current in zip(segments, segments[1:]):
        if current.top > previous.top + _LINE_TOLERANCE_PT:
            return False  # jumped back up the page
        same_line = abs(current.top - previous.top) <= _LINE_TOLERANCE_PT
        if same_line and current.left < previous.left:
            return False  # jumped back left within a line
    return True


def _sorted_reading_order(segments: Sequence[_Segment]) -> List[_Segment]:
    """Sort scrambled segments into lines: top-to-bottom, then left-to-right.

    Lines are clustered rather than sorted on a rounded key, so two rectangles
    a hair apart vertically cannot land in different lines just because a
    bucket boundary happens to fall between them.
    """
    by_height = sorted(segments, key=lambda segment: -segment.top)
    ordered: List[_Segment] = []
    line: List[_Segment] = []
    line_top: float | None = None
    for segment in by_height:
        if line_top is None or abs(segment.top - line_top) <= _LINE_TOLERANCE_PT:
            if line_top is None:
                line_top = segment.top
            line.append(segment)
            continue
        ordered.extend(sorted(line, key=lambda item: item.left))
        line = [segment]
        line_top = segment.top
    ordered.extend(sorted(line, key=lambda item: item.left))
    return ordered


def _render_lines(segments: Sequence[_Segment]) -> str:
    """Join ordered segments, breaking a line where the baseline drops."""
    lines: List[str] = []
    current: List[str] = []
    line_top: float | None = None
    for segment in segments:
        if line_top is not None and abs(segment.top - line_top) > _LINE_TOLERANCE_PT:
            lines.append(" ".join(current))
            current = []
        current.append(segment.text)
        line_top = segment.top
    if current:
        lines.append(" ".join(current))
    return "\n".join(line for line in lines if line.strip()).strip()


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
    "DEFAULT_MAX_CHARS",
    "PAGE_BREAK",
    "PASSTHROUGH_SUFFIXES",
]
