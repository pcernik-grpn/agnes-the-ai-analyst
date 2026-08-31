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

The PDF route is :mod:`connectors.sharepoint.pdf_structure` and nothing else
(owner decision 2026-08-31 — one pipeline, no dual modes). It reconstructs
headings and tables from glyph positions and degrades *per page*, inside
itself, to that page's plain reading-order text whenever the block structure
is ambiguous — a wrong table being worse than no table — so there is no
second, whole-document plain route out here for a failure to fall back to.
Pages are separated by ``\\n\\n---\\n\\n``.

A PDF with no text layer at all (a scan) is **not** an error: it returns
``engine="empty"`` with empty markdown. Transcribing scans is a separate
feature with its own cost surface — this module never calls a model.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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
        text = _convert_pdf(path, filename)
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


def _convert_pdf(path: Path, filename: str) -> str:
    """PDF → markdown through the structure pass, and only through it.

    :func:`connectors.sharepoint.pdf_structure.reconstruct_pdf` is the ONE
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
    """
    try:
        # Presence probe for the typed error below: `reconstruct_pdf` imports
        # pypdfium2 lazily too, and an uninstalled extra must surface as
        # MissingConversionDependency naming `agnes[extraction]`, never as a
        # bare ImportError from three frames down.
        import pypdfium2  # noqa: F401
    except ImportError as exc:
        raise MissingConversionDependency(filename, "pypdfium2", engine=ENGINE_PYPDFIUM2) from exc

    from connectors.sharepoint.pdf_structure import reconstruct_pdf

    try:
        structured = str(reconstruct_pdf(path)).strip()
    except Exception as exc:
        raise ConversionError(
            filename,
            f"could not convert PDF ({type(exc).__name__})",
            engine=ENGINE_PYPDFIUM2,
        ) from exc

    # Near-empty means no usable text layer — a scan, or pure vector art.
    # Signalled by returning empty so convert_to_markdown reports
    # engine="empty"; not an error, and never a different engine.
    return structured if len(structured) >= MIN_PDF_TEXT_CHARS else ""


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
