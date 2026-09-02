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
pypdfium2. The legacy Office / OpenDocument suffixes markitdown cannot read
directly — ``.doc/.rtf/.odt``, ``.ppt/.odp``, ``.xls/.ods`` — are first
re-saved by headless LibreOffice into the OOXML sibling markitdown already
handles (docx/pptx/xlsx respectively), then routed through the same
markitdown call as everything else; the run is reported as
``"libreoffice+markitdown"`` so a downstream reader can tell it from a direct
markitdown conversion. Everything else goes to markitdown directly. The
declared ``mime`` is only consulted when the filename carries no suffix we
recognise; it is untrusted metadata from a remote drive and is never used for
anything but choosing a route.

Failure model
-------------
A crawl walks thousands of files and must not die on one of them. Every
foreseeable failure — unreadable file, corrupt PDF, a backend blowing up on
malformed input — is raised as :class:`ConversionError`, which names the file.
A missing optional dependency raises :class:`MissingConversionDependency` (a
subclass, so a caller that only catches ``ConversionError`` still survives) and
names the extra to install; the imports are lazy so importing this module on a
server that never converts anything costs nothing and cannot fail. A file no
registered markitdown converter even attempts (Power BI ``.pbix``, OneNote
``.one``, and other formats with no backend at all) raises
:class:`UnsupportedConversionFormat` — also a subclass, but the caller counts
it apart from an attempted-and-failed conversion (see that class's docstring).

The PDF route is :mod:`connectors.sharepoint.pdf_structure` and nothing else
(owner decision 2026-08-31 — one pipeline, no dual modes). It reconstructs
headings and tables from glyph positions and degrades *per page*, inside
itself, to that page's plain reading-order text whenever the block structure
is ambiguous — a wrong table being worse than no table — so there is no
second, whole-document plain route out here for a failure to fall back to.
Pages are separated by ``\\n\\n---\\n\\n``.

A PDF with no text layer at all (a scan) is **not** an error: it returns
``engine="empty"`` with empty markdown. Transcribing such a scan is a separate
feature with its own cost surface — :mod:`connectors.sharepoint.scan_ocr`,
**off by default** behind ``extraction.scan_ocr.enabled``. While it is off
this module never calls a model and the empty result above is byte-identical
to what it always was; with it on, a scan comes back as ``engine="ocr"`` and a
transcription that could not be produced at all is a :class:`ConversionError`,
never a silently empty document.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
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

#: Legacy Office / OpenDocument suffixes markitdown's Office backends
#: (mammoth, python-pptx, openpyxl) do not read: the pre-2007 OLE2 binary
#: formats and the OpenDocument formats alike. Mapped to the OOXML target
#: format headless LibreOffice must produce so markitdown can take over —
#: never a second, competing reader, just a re-save into the format the
#: existing route already handles.
LEGACY_OFFICE_TARGETS: dict[str, str] = {
    ".doc": "docx",
    ".rtf": "docx",
    ".odt": "docx",
    ".ppt": "pptx",
    ".odp": "pptx",
    ".xls": "xlsx",
    ".ods": "xlsx",
}
LEGACY_OFFICE_SUFFIXES = frozenset(LEGACY_OFFICE_TARGETS)

#: Ceiling on one LibreOffice ``--convert-to`` invocation. A module constant
#: rather than an ``instance.yaml`` knob (no speculative config surface for a
#: value nobody has needed to tune) — generous enough for a large legacy
#: spreadsheet or deck, short enough that one pathological file cannot stall
#: a crawl's conversion pool.
LIBREOFFICE_TIMEOUT_SECONDS = 120

ENGINE_MARKITDOWN = "markitdown"
ENGINE_PYPDFIUM2 = "pypdfium2"
ENGINE_PASSTHROUGH = "passthrough"
ENGINE_EMPTY = "empty"
#: Legacy Office / OpenDocument file pre-converted by headless LibreOffice
#: and then read through the ordinary markitdown route. Its own engine name,
#: never plain ``"markitdown"``: a downstream reader must be able to tell a
#: file that went through the LibreOffice re-save from one markitdown read
#: natively.
ENGINE_LIBREOFFICE_MARKITDOWN = "libreoffice+markitdown"
#: A PDF with no text layer, transcribed by the vision model
#: (:mod:`connectors.sharepoint.scan_ocr`). Its own engine name, never
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


class UnsupportedConversionFormat(ConversionError):
    """No conversion backend recognizes this file at all.

    Distinct from the base :class:`ConversionError` (a backend WAS found and
    attempted, and failed on this specific document's content): this is
    markitdown reporting that no registered converter's ``accepts()`` matched
    the file — video/audio containers markitdown genuinely has no codec path
    for, Power BI ``.pbix``, OneNote ``.one``, and similar formats with no
    backend at all (verified empirically per format, never assumed from a
    suffix list — see ``markitdown.UnsupportedFormatException``, the signal
    this wraps). The crawler counts these separately (``skipped_unsupported``,
    never ``errors``/``convert_failed``): the document was never attempted,
    so there is nothing to retry and nothing to diagnose.
    """


class MissingConversionDependency(ConversionError):
    """An optional extraction dependency is not installed — or its lazy
    import failed for some other reason (e.g. an out-of-memory error inside
    a conversion child running under a tight ``RLIMIT_AS``).

    A subclass of :class:`ConversionError` so a crawl that catches the base
    class keeps running, but distinct so an operator-facing caller can catch it
    first and stop early: every document of that kind will fail identically
    until someone installs the extra (or the underlying cause is fixed).

    ``cause``, when given, is the exception the lazy ``import`` actually
    raised — folded into the message as ``"<type>: <first 200 chars>"`` so a
    live finding (a ``MemoryError`` under a memory-limited child reported as
    a plain "not installed") carries its real cause instead of masking it.
    Without ``cause`` (e.g. a missing binary on ``PATH``, never an import
    failure) the message stays the plain "not installed" wording.
    """

    def __init__(
        self,
        filename: str,
        package: str,
        *,
        engine: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.package = package
        install_hint = "install the extraction extra (pip install 'agnes[extraction]') to convert this file type"
        if cause is not None:
            reason = type(cause).__name__
            cause_text = str(cause)[:200]
            if cause_text:
                reason = f"{reason}: {cause_text}"
            message = (
                f"{package} could not be imported ({reason}) — {install_hint}, "
                f"or check the conversion child's memory limit"
            )
        else:
            message = f"{package} is not installed — {install_hint}"
        super().__init__(filename, message, engine=engine)


@dataclass
class ConvertResult:
    """The converted document and which engine produced it.

    ``engine`` is one of ``"markitdown"``, ``"pypdfium2"``, ``"passthrough"``,
    ``"libreoffice+markitdown"`` or ``"empty"``. ``"empty"`` means conversion
    succeeded and found no text — a scanned PDF, a blank document — and
    ``markdown`` is then ``""``.
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
        text, engine = _convert_pdf(path, filename)
    elif suffix in LEGACY_OFFICE_SUFFIXES:
        text = _convert_legacy_office(path, filename, suffix)
        engine = ENGINE_LIBREOFFICE_MARKITDOWN
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
        from markitdown import UnsupportedFormatException as _MarkItDownUnsupported
    except Exception as exc:
        # Broader than ImportError on purpose: a live finding showed a
        # MemoryError from deep inside numpy/OpenBLAS init (this import's
        # transitive dependency chain, under a conversion child's RLIMIT_AS)
        # propagating uncaught and reading as "markitdown is not installed"
        # by the time anyone saw it — `cause=exc` keeps the real exception
        # type and text in the message instead of losing it.
        raise MissingConversionDependency(filename, "markitdown", engine=ENGINE_MARKITDOWN, cause=exc) from exc

    try:
        result = MarkItDown(enable_plugins=False).convert(str(path))
    except _MarkItDownUnsupported as exc:
        # No registered converter's `accepts()` matched this file AT ALL —
        # markitdown never attempted a conversion, as opposed to every other
        # branch below (a backend was found and it failed). See
        # `UnsupportedConversionFormat`'s docstring for why the crawler
        # counts this differently. Never document content: markitdown's own
        # message here names no file bytes, only "no suitable converter".
        raise UnsupportedConversionFormat(
            filename,
            "no conversion backend recognizes this file type",
            engine=ENGINE_MARKITDOWN,
        ) from exc
    except Exception as exc:
        # markitdown surfaces backend failures as many different exception
        # types (its own FileConversionException, zipfile.BadZipFile, XML
        # parse errors, ...). A crawl cannot enumerate them, and any of them
        # means the same thing: a backend was found and this document did
        # not convert.
        raise ConversionError(
            filename,
            f"markitdown could not convert this file ({type(exc).__name__})",
            engine=ENGINE_MARKITDOWN,
        ) from exc

    text = getattr(result, "text_content", None)
    if text is None:
        text = getattr(result, "markdown", None)
    return _normalize_newlines(text or "")


# ---------------------------------------------------------------- legacy office

#: Serializes soffice invocations within ONE process (see _convert_legacy_office).
_LIBREOFFICE_LOCK = threading.Lock()
#: pid → this process's own LibreOffice user profile directory. Keyed by pid,
#: not cached once: a worker child forked after the parent's first conversion
#: must not inherit (and race on) the parent's profile.
_LIBREOFFICE_PROFILES: dict[int, str] = {}


def _libreoffice_profile_dir() -> str:
    """A LibreOffice ``UserInstallation`` directory private to this process,
    created on first use and reused for every later conversion in the same
    process (the first headless start on a fresh profile costs seconds; the
    ones after it do not)."""
    pid = os.getpid()
    profile = _LIBREOFFICE_PROFILES.get(pid)
    if profile is None or not os.path.isdir(profile):
        profile = tempfile.mkdtemp(prefix=f"agnes-libreoffice-profile-{pid}-")
        _LIBREOFFICE_PROFILES[pid] = profile
    return profile


def _convert_legacy_office(path: Path, filename: str, suffix: str) -> str:
    """Legacy Office / OpenDocument formats markitdown cannot read directly.

    Shells out to headless LibreOffice (``soffice --headless --convert-to
    <target> --outdir <tmpdir> <file>``) to re-save the file into the OOXML
    sibling markitdown already handles — ``.doc/.rtf/.odt`` → docx,
    ``.ppt/.odp`` → pptx, ``.xls/.ods`` → xlsx — in a throwaway temp dir that
    is ALWAYS removed, success or failure. ``soffice`` missing from ``PATH``
    raises the same typed :class:`MissingConversionDependency` a missing
    Python backend would, naming ``"libreoffice"``, so the file is COUNTED as
    a named conversion failure exactly like a missing markitdown today —
    never silently skipped. A non-zero exit or a timeout raises
    :class:`ConversionError`, the same class :func:`_convert_markitdown`
    raises for a backend failure.
    """
    target_format = LEGACY_OFFICE_TARGETS[suffix]

    if shutil.which("soffice") is None:
        raise MissingConversionDependency(filename, "libreoffice", engine=ENGINE_LIBREOFFICE_MARKITDOWN)

    tmpdir = tempfile.mkdtemp(prefix="agnes-libreoffice-")
    try:
        try:
            # One soffice at a time PER PROCESS, on a profile that is this
            # process's own: LibreOffice keeps a lock in its user profile and
            # a second headless instance on the same profile exits 1 (measured
            # live: 3 of 6 concurrent conversions failed). The crawl's
            # parallelism is across worker child processes, so a per-process
            # profile + lock costs nothing there and makes an in-process
            # caller (threads) serialize instead of fail.
            with _LIBREOFFICE_LOCK:
                completed = subprocess.run(
                    [
                        "soffice",
                        f"-env:UserInstallation=file://{_libreoffice_profile_dir()}",
                        "--headless",
                        "--norestore",
                        "--convert-to",
                        target_format,
                        "--outdir",
                        tmpdir,
                        str(path),
                    ],
                    capture_output=True,
                    timeout=LIBREOFFICE_TIMEOUT_SECONDS,
                    check=False,
                )
        except subprocess.TimeoutExpired as exc:
            raise ConversionError(
                filename,
                f"libreoffice conversion timed out after {LIBREOFFICE_TIMEOUT_SECONDS}s",
                engine=ENGINE_LIBREOFFICE_MARKITDOWN,
            ) from exc

        if completed.returncode != 0:
            raise ConversionError(
                filename,
                f"libreoffice exited with status {completed.returncode}",
                engine=ENGINE_LIBREOFFICE_MARKITDOWN,
            )

        converted = sorted(Path(tmpdir).glob(f"*.{target_format}"))
        if not converted:
            raise ConversionError(
                filename,
                "libreoffice produced no output file",
                engine=ENGINE_LIBREOFFICE_MARKITDOWN,
            )

        return _convert_markitdown(converted[0], filename)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------- pdf


def _convert_pdf(path: Path, filename: str) -> tuple[str, str]:
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

    Returns ``(markdown, engine)`` — the engine because this is the one route
    with two of them: the structure pass, and the scan-OCR fallback below.
    """
    try:
        # Presence probe for the typed error below: `reconstruct_pdf` imports
        # pypdfium2 lazily too, and an uninstalled extra must surface as
        # MissingConversionDependency naming `agnes[extraction]`, never as a
        # bare ImportError from three frames down.
        import pypdfium2  # noqa: F401
    except Exception as exc:
        # See the matching comment in _convert_markitdown: broader than
        # ImportError so a MemoryError from this import's own native
        # dependency chain carries its cause instead of reading as a plain
        # "not installed".
        raise MissingConversionDependency(filename, "pypdfium2", engine=ENGINE_PYPDFIUM2, cause=exc) from exc

    from connectors.sharepoint.pdf_structure import reconstruct_pdf

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
    from connectors.sharepoint import scan_ocr

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
    "UnsupportedConversionFormat",
    "convert_to_markdown",
    "DEFAULT_MAX_CHARS",
    "PAGE_BREAK",
    "PASSTHROUGH_SUFFIXES",
    "LEGACY_OFFICE_SUFFIXES",
    "LEGACY_OFFICE_TARGETS",
    "LIBREOFFICE_TIMEOUT_SECONDS",
]
