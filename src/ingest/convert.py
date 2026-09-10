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
pypdfium2. ``.docx``/``.pptx`` first get **Docling** (the ``[docling]``
extra, MIT) when it is installed, because its layout-aware parsing keeps
tables and reading order that markitdown flattens; Docling refusing a
document is not a failure, markitdown gets it next. Docling is deliberately
NOT offered the PDF route: the structure pass below is the one PDF pipeline
on every image (owner decision 2026-08-31). The legacy Office / OpenDocument
suffixes markitdown cannot read directly — ``.doc/.rtf/.odt``,
``.ppt/.odp``, ``.xls/.ods``, and (not "legacy" by vintage, but the same
"markitdown/openpyxl cannot read this one directly" constraint — live
finding 2026-09, 155 ``.xlsb`` + 21 ``.xlsm`` failures) ``.xlsb/.xlsm`` —
are first re-saved by headless LibreOffice into the OOXML sibling markitdown
already handles (docx/pptx/xlsx respectively; both ``.xlsb`` and ``.xlsm``
target ``xlsx``), then routed through the same markitdown call as everything
else; the run is reported as ``"libreoffice+markitdown"`` so a downstream
reader can tell it from a direct markitdown conversion. Everything else goes
to markitdown directly. The declared ``mime`` is only consulted when the
filename carries no suffix we recognise; it is untrusted metadata from a
remote drive and is never used for anything but choosing a route. A caller
that stores files under content-addressed names and keeps the declared type
elsewhere passes it as ``suffix``, which then drives routing instead of the
path.

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

Rescue chain (live finding 2026-09, wave 2)
--------------------------------------------
``.xlsx``/``.pptx``/``.docx`` need no LibreOffice pre-convert at all — markitdown
reads OOXML directly — but a real share of them still fail outright: 723 of
1 162 markitdown "could not convert" failures on one site's crawl were plain
``.xlsx``. Rather than counting those as an immediate ``convert_failed``, this
module escalates through a RESCUE CHAIN (:func:`_convert_markitdown_with_rescue`,
:data:`RESCUE_RESAVE_TARGETS`, :data:`RESCUE_FALLBACK_KIND`): re-save the file
through the SAME LibreOffice mechanism the legacy-format route already uses and
retry markitdown once on the re-saved copy, and if that still fails, fall back
to a LibreOffice-produced CSV (one block per sheet, spreadsheets) or a
LibreOffice-produced PDF run through this module's own PDF route (decks and
documents) — never a competing reader. ``.xlsm``/``.xlsb``/``.xls``/``.ods`` and
the other :data:`LEGACY_OFFICE_SUFFIXES` already go through one LibreOffice
resave before markitdown ever sees them; if THAT markitdown attempt also fails,
they join the same fallback rung (never a second, redundant resave-and-retry —
they already had their one resave). Which rung succeeded, if any, is reported
on :attr:`ConvertResult.rescue` (``""`` when no rescue was needed). A rescue is
NEVER attempted for :class:`UnsupportedConversionFormat` (no backend was even
tried) or :class:`MissingConversionDependency` (installing LibreOffice cannot
fix a missing markitdown) — only for a genuine backend failure on a rescuable
suffix.

A large ``.xlsx`` (above :data:`LARGE_XLSX_STREAMING_THRESHOLD_BYTES`) skips
markitdown's full in-memory parse entirely and is read directly with openpyxl
in ``read_only=True`` streaming mode (:func:`_read_xlsx_as_text`, the same
reader the CSV rescue rung reuses) — see that function's docstring for why a
221-file, 15 MB-average live finding motivated it.

Embedded pictures (disclosure, not OCR)
----------------------------------------
markitdown drops the CONTENT of an embedded picture on every route through
this module — this file never builds an image-understanding pipeline, and
that stays true after this paragraph — but on two routes it left behind a
placeholder that misled a reader instead of informing one (live finding
2026-09-09): a ``.docx`` picture (mammoth's default image handler) came back
as ``![alt](data:image/png;base64...)``, markitdown's own truncation of a
base64 payload it never keeps either, and a ``.pptx`` picture came back as
``![alt](PictureN.jpg)`` — a name python-pptx derives from PowerPoint's own
per-SHAPE name, which repeats across slides and, worse, across two entirely
unrelated documents, reading like a stable, fetchable filename when it is
neither. :func:`_disclose_image_placeholders` rewrites both, for the
markitdown-family engines only (:data:`_IMAGE_DISCLOSURE_ENGINES` — Docling's
own picture marker is a different, opt-in surface this slice does not touch),
into ``[image N of TOTAL in this document — not indexed, LOCATION]``: an
ordinal scoped to THIS conversion (never collides with another document, and
never looks like a filename anything could fetch), plus the nearest
preceding slide number or section heading markitdown already emitted in the
surrounding text — read here, never invented. :attr:`ConvertResult.
image_count` carries the same total so a caller can act on it (e.g. an
indexed-file's ``processing_detail``) without re-scanning the markdown.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
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

#: Legacy Office / OpenDocument suffixes markitdown's Office backends
#: (mammoth, python-pptx, openpyxl) do not read: the pre-2007 OLE2 binary
#: formats and the OpenDocument formats alike. Mapped to the OOXML target
#: format headless LibreOffice must produce so markitdown can take over —
#: never a second, competing reader, just a re-save into the format the
#: existing route already handles.
#:
#: ``.xlsb``/``.xlsm`` ride the same map for a DIFFERENT reason — they are
#: not pre-2007 legacy formats, ``.xlsb`` is Excel's binary (non-XML) OOXML
#: sibling and ``.xlsm`` is the macro-enabled OOXML sibling — but openpyxl
#: (markitdown's xlsx backend) cannot read the binary container at all and
#: fails on some macro-enabled workbooks the same way, live finding 2026-09:
#: 155 ``.xlsb`` + 21 ``.xlsm`` failures on a real crawl. LibreOffice reads
#: both natively, so the fix is the identical re-save-then-markitdown route,
#: just targeting the same ``xlsx`` a plain ``.xls`` already does.
LEGACY_OFFICE_TARGETS: dict[str, str] = {
    ".doc": "docx",
    ".rtf": "docx",
    ".odt": "docx",
    ".ppt": "pptx",
    ".odp": "pptx",
    ".xls": "xlsx",
    ".ods": "xlsx",
    ".xlsb": "xlsx",
    ".xlsm": "xlsx",
}
LEGACY_OFFICE_SUFFIXES = frozenset(LEGACY_OFFICE_TARGETS)

#: Ceiling on one LibreOffice ``--convert-to`` invocation. A module constant
#: rather than an ``instance.yaml`` knob (no speculative config surface for a
#: value nobody has needed to tune) — generous enough for a large legacy
#: spreadsheet or deck, short enough that one pathological file cannot stall
#: a crawl's conversion pool.
LIBREOFFICE_TIMEOUT_SECONDS = 120

#: OOXML suffixes markitdown reads DIRECTLY (no LibreOffice pre-convert
#: needed to make markitdown accept the format at all) but which still fail
#: outright on a meaningful share of real files — live finding 2026-09,
#: wave 2: 723 of 1 162 markitdown "could not convert" failures on one
#: site's crawl were plain ``.xlsx``, plus 117 ``.pptx``. Maps a suffix to
#: the LibreOffice ``--convert-to`` target used for the rescue chain's rung
#: 1 (resave the SAME format and retry markitdown once) — see
#: :func:`_convert_markitdown_with_rescue`.
RESCUE_RESAVE_TARGETS: dict[str, str] = {".xlsx": "xlsx", ".pptx": "pptx", ".docx": "docx"}

#: Rung 2 of the rescue chain (reached when rung 1 also fails, or was
#: skipped because the file already went through ONE LibreOffice resave via
#: :data:`LEGACY_OFFICE_TARGETS`): spreadsheets fall back to a
#: LibreOffice-produced CSV (:func:`_read_xlsx_as_text`, one block per
#: sheet), decks and documents fall back to a LibreOffice-produced PDF run
#: through this module's own PDF route (:func:`_convert_pdf`). Covers every
#: suffix either :data:`RESCUE_RESAVE_TARGETS` or :data:`LEGACY_OFFICE_
#: TARGETS` can reach — a document that fails markitdown even after its one
#: resave has nowhere left to go but this same fallback.
RESCUE_FALLBACK_KIND: dict[str, str] = {
    ".xlsx": "csv",
    ".xlsm": "csv",
    ".xlsb": "csv",
    ".xls": "csv",
    ".ods": "csv",
    ".pptx": "pdf",
    ".ppt": "pdf",
    ".docx": "pdf",
    ".doc": "pdf",
    ".rtf": "pdf",
    ".odt": "pdf",
    ".odp": "pdf",
}

#: Above this size, a plain ``.xlsx`` skips markitdown's full in-memory
#: parse entirely and is read directly with openpyxl in streaming
#: (``read_only=True``) mode — see :func:`_read_xlsx_as_text`. Live finding
#: 2026-09: 221 large xlsx/xlsm files (average 15 MB) hit the (then-flat)
#: 300s per-document conversion budget on one site's crawl; set below that
#: average so most of that population takes the cheap streaming path
#: instead of the slower, size-scaled markitdown route.
LARGE_XLSX_STREAMING_THRESHOLD_BYTES = 10 * 1024 * 1024

#: Base per-document conversion time budget, in seconds — the flat ceiling
#: this module's live finding (see :data:`CONVERSION_BUDGET_PER_MB_SECONDS`)
#: showed was not enough on its own. Mirrors the crawler's own
#: ``extraction.crawler.item_timeout_s`` default
#: (``connectors.sharepoint.crawler._DEFAULT_ITEM_TIMEOUT_S``); kept as a
#: separate constant here because this module must not import the crawler
#: (the crawler already imports this module).
CONVERSION_BUDGET_BASE_SECONDS = 300.0

#: Extra seconds of budget granted per MB of input size, on top of
#: :data:`CONVERSION_BUDGET_BASE_SECONDS`. Live finding 2026-09: 221 large
#: xlsx/xlsm files (average 15 MB) hit the flat 300s budget on one site's
#: crawl — at 20s/MB a 15 MB file gets 300 + 15*20 = 600s, comfortably past
#: what those failures needed.
CONVERSION_BUDGET_PER_MB_SECONDS = 20.0

#: Absolute ceiling on the size-scaled budget, regardless of input size — a
#: module constant, not an ``instance.yaml`` knob (no speculative config
#: surface for a value nobody has needed to tune): even a pathological
#: file must not stall a crawl's conversion pool indefinitely.
CONVERSION_BUDGET_MAX_SECONDS = 1_800.0

ENGINE_MARKITDOWN = "markitdown"
ENGINE_PYPDFIUM2 = "pypdfium2"
#: Office documents parsed by the ``[docling]`` extra — layout-aware, heavy,
#: opt-in. Its own engine name so a reader can tell a Docling table from
#: markitdown's flattened rendering of the same page.
ENGINE_DOCLING = "docling"
ENGINE_PASSTHROUGH = "passthrough"
ENGINE_EMPTY = "empty"
#: Legacy Office / OpenDocument file pre-converted by headless LibreOffice
#: and then read through the ordinary markitdown route. Its own engine name,
#: never plain ``"markitdown"``: a downstream reader must be able to tell a
#: file that went through the LibreOffice re-save from one markitdown read
#: natively.
ENGINE_LIBREOFFICE_MARKITDOWN = "libreoffice+markitdown"
#: A PDF with no text layer, transcribed by the vision model
#: (:mod:`src.ingest.scan_ocr`). Its own engine name, never
#: ``"pypdfium2"``: a downstream reader must be able to tell text that was read
#: off the page from text a model produced from a bitmap.
ENGINE_OCR = "ocr"
#: Rescue chain rung 1: a rescuable OOXML file (see
#: :data:`RESCUE_RESAVE_TARGETS`) whose FIRST markitdown attempt failed, then
#: succeeded after a LibreOffice resave into the same format. Distinct from
#: ``ENGINE_LIBREOFFICE_MARKITDOWN`` (the legacy-format route, which ALWAYS
#: resaves, never as a rescue) so a downstream reader can tell "this format
#: always needs LibreOffice" from "markitdown could not read this ONE file
#: directly".
ENGINE_LIBREOFFICE_RESCUE = "libreoffice_resave+markitdown"
#: Rescue chain rung 2, spreadsheets: a LibreOffice-produced CSV (one block
#: per sheet) read after both the direct markitdown attempt and (when
#: applicable) rung 1 failed. See :func:`_read_xlsx_as_text`.
ENGINE_CSV_FALLBACK = "libreoffice_csv_fallback"
#: Rescue chain rung 2, decks/documents: a LibreOffice-produced PDF run
#: through this module's own PDF route (:func:`_convert_pdf`).
ENGINE_PDF_FALLBACK = "libreoffice_pdf_fallback"
#: A large ``.xlsx`` (see :data:`LARGE_XLSX_STREAMING_THRESHOLD_BYTES`) read
#: directly with openpyxl in streaming mode, never through markitdown's full
#: in-memory parse. See :func:`_read_xlsx_as_text`.
ENGINE_XLSX_STREAMING = "openpyxl_streaming"


def conversion_budget_seconds(size_bytes: int, *, base_seconds: float = CONVERSION_BUDGET_BASE_SECONDS) -> float:
    """Size-scaled per-document conversion time budget.

    ``base_seconds`` + :data:`CONVERSION_BUDGET_PER_MB_SECONDS` per MB of
    ``size_bytes``, capped at :data:`CONVERSION_BUDGET_MAX_SECONDS`. Used by
    the crawler's conversion process pool
    (``connectors.sharepoint.crawler._ConvertProcessPool``) to size EACH
    file's own per-item timeout instead of applying one flat ceiling to
    every document regardless of size — see :data:`CONVERSION_BUDGET_PER_MB_
    SECONDS`'s docstring for the live finding this fixes.

    ``base_seconds`` defaults to this module's own constant but accepts an
    override so a caller can pass through an admin's configured
    ``extraction.crawler.item_timeout_s`` instead of silently ignoring it.
    ``base_seconds <= 0`` disables the budget entirely (returns ``0.0``) —
    the same "0 means unbounded" convention ``item_timeout_s`` already uses,
    preserved rather than silently turning an explicit "no bound" into a
    scaled, nonzero one.
    """
    if base_seconds <= 0:
        return 0.0
    if size_bytes <= 0:
        return base_seconds
    size_mb = size_bytes / (1024 * 1024)
    return min(CONVERSION_BUDGET_MAX_SECONDS, base_seconds + CONVERSION_BUDGET_PER_MB_SECONDS * size_mb)


#: Closed vocabulary for WHY one document's conversion failed — the crawler
#: persists this into ``failed_items[...]['error_class']``
#: (``connectors.sharepoint.crawler._note_retry``, 2026-09-04 finding #66
#: item 1) and uses it to decide when a document has failed
#: DETERMINISTICALLY enough to stop retrying it without a download
#: (``crawler._doomed_skip_reason``, item 2) — never freeform text, so that
#: decision never depends on parsing prose. Not every possible crawl-time
#: failure is listed here — ``download_error``/``ingest_error`` are the
#: crawler's OWN classes for its download/ingest stages, set there directly
#: (this module never sees those stages) — the ones below are exactly what
#: THIS module's own raise sites can attribute a failure to.
ERROR_CLASS_MARKITDOWN_REJECT = "markitdown_reject"
ERROR_CLASS_LIBREOFFICE_NO_OUTPUT = "libreoffice_no_output"
ERROR_CLASS_PDFIUM_ERROR = "pdfium_error"
ERROR_CLASS_TIMEOUT = "timeout"
ERROR_CLASS_MEMORY_KILL = "memory_kill"
ERROR_CLASS_WORKER_CRASH = "worker_crash"
ERROR_CLASS_INGEST_ERROR = "ingest_error"
ERROR_CLASS_DOWNLOAD_ERROR = "download_error"
ERROR_CLASS_OTHER = "other"

#: The subset of the vocabulary above treated as DETERMINISTIC — the same
#: file, the same bytes, will be rejected the same way every time, so a
#: bounded number of attempts is enough to know not to try again without an
#: operator's explicit ``force_reprocess`` (2026-09-04 finding #66 item 2).
#: Timeouts, memory kills and worker crashes are deliberately EXCLUDED —
#: they are environmental (a busy host, a transient resource ceiling), not a
#: property of the document, and stay retryable forever. ``ingest_error`` is
#: deterministic only when paired with an unchanged content hash — this set
#: alone is necessary but not sufficient for it; the crawler applies that
#: extra check itself (``_note_retry``'s ``content_sha``).
DETERMINISTIC_ERROR_CLASSES = frozenset(
    {
        ERROR_CLASS_MARKITDOWN_REJECT,
        ERROR_CLASS_LIBREOFFICE_NO_OUTPUT,
        ERROR_CLASS_PDFIUM_ERROR,
        ERROR_CLASS_INGEST_ERROR,
    }
)


class ConversionError(RuntimeError):
    """A file could not be converted.

    Always carries the filename, because the caller is a crawl loop logging one
    line per document and "UnicodeDecodeError" on its own is unactionable. The
    message never carries file *content* — a conversion failure is frequently a
    malformed document, and its bytes may be confidential.

    ``error_class`` (one of the ``ERROR_CLASS_*`` constants above, or ``None``
    when this raise site has no opinion) is set at the exact point that knows
    which backend rejected the file and why — never inferred later from the
    message text, which is free-form and scope-gated (see
    :func:`_convert_failure_detail`). ``None`` propagates as ``"other"`` at
    the crawler boundary (``_prepare_document``), the same conservative,
    always-retryable default an unclassified failure already gets.
    """

    def __init__(
        self, filename: str, message: str, *, engine: str | None = None, error_class: str | None = None
    ) -> None:
        self.filename = filename
        self.engine = engine
        self.error_class = error_class
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

    ``engine`` is one of ``"docling"``, ``"markitdown"``, ``"pypdfium2"``,
    ``"ocr"``, ``"passthrough"``, ``"libreoffice+markitdown"``, ``"empty"``,
    or one of the rescue-chain / streaming engines above
    (``ENGINE_LIBREOFFICE_RESCUE``, ``ENGINE_CSV_FALLBACK``,
    ``ENGINE_PDF_FALLBACK``, ``ENGINE_XLSX_STREAMING``). ``"empty"`` means
    conversion succeeded and found no text — a scanned PDF, a blank
    document — and ``markdown`` is then ``""``.

    ``rescue`` names which rung of the rescue chain succeeded, when one was
    needed: ``""`` (no rescue — the ordinary case), ``"libreoffice_resave"``,
    ``"csv_fallback"``, or ``"pdf_fallback"``. Empty for every route that
    never goes through the rescue chain (passthrough, PDF, plain markitdown,
    the ordinary legacy-office resave, the streaming route's happy path).

    ``image_count`` is how many embedded pictures markitdown found in this
    document and could not index (see :func:`_disclose_image_placeholders`)
    — ``0`` for every document with none, and always ``0`` for a route that
    never runs the disclosure rewrite (Docling's own picture markers are a
    separate, opt-in surface not covered by this slice; the CSV/PDF rescue
    rungs and the PDF/streaming routes have no picture markdown to begin
    with). A reader must never infer "no images" from ``image_count == 0``
    on one of those routes — it means "not counted here", not "none found".
    """

    markdown: str
    engine: str
    rescue: str = ""
    image_count: int = 0


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
    except Exception:  # noqa: BLE001 — an unimportable docling means "not this engine", same as the convert failure below
        return None
    try:
        result = DocumentConverter().convert(str(path))
        return _normalize_newlines(result.document.export_to_markdown() or "")
    except Exception as exc:  # noqa: BLE001 — any backend failure means "not this engine"
        logger.warning("docling could not convert %s (%s); trying the next engine", path.name, type(exc).__name__)
        return None


# --------------------------------------------------------- image disclosure

#: Engines whose OUTPUT this module rewrites for embedded-picture disclosure
#: (see the module docstring's "Embedded pictures" section). Every one of
#: them is a markitdown call somewhere in its route — the base64/filename
#: placeholder shapes :data:`_IMAGE_MARKER_RE` matches are markitdown's own,
#: emitted the identical way regardless of which rung of the rescue chain
#: reached it. Deliberately excludes: :data:`ENGINE_DOCLING` (a different,
#: opt-in extra with its own picture-marker convention, not this slice's
#: concern — see the module docstring); :data:`ENGINE_CSV_FALLBACK` /
#: :data:`ENGINE_PDF_FALLBACK` / :data:`ENGINE_XLSX_STREAMING` (openpyxl or
#: this module's own PDF route — no markitdown image markdown ever reaches
#: them); :data:`ENGINE_PASSTHROUGH` / :data:`ENGINE_PYPDFIUM2` /
#: :data:`ENGINE_OCR` (never markitdown at all).
_IMAGE_DISCLOSURE_ENGINES = frozenset({ENGINE_MARKITDOWN, ENGINE_LIBREOFFICE_MARKITDOWN, ENGINE_LIBREOFFICE_RESCUE})

#: markitdown's two image-loss shapes, matched alongside the slide/heading
#: markers that locate them — one linear pass, one compiled pattern. Every
#: alternative starts on a distinct literal (``<!--``, a line-leading ``#``,
#: or ``![``) and every repeated group is bounded by a delimiter the group
#: itself excludes (``[^\]]*`` cannot consume the ``]`` that ends it; ``.+``
#: is anchored to end-of-line by ``re.MULTILINE``'s ``$``) — no ambiguous
#: overlap between branches and no nested quantifiers, so this stays
#: linear-time over arbitrarily large converted text regardless of input
#: shape (see ``.claude/skills/agnes-conventions/references/security.md``
#: §5). The pptx branch (``jpgname``) matches only ``.jpg`` — not a
#: speculative "any image extension" — because that suffix is hard-coded in
#: markitdown's own ``_pptx_converter.py`` (``filename = re.sub(r"\W", "",
#: shape.name) + ".jpg"``, verified against the installed version); matching
#: what the pinned dependency actually emits, not what it theoretically
#: could.
#:
#: ``b64`` and ``jpgname`` are separate named groups, not one alternative,
#: because they carry a DIFFERENT trust level: the base64 shape only ever
#: comes from mammoth's own truncation of a real embedded picture (never a
#: real base64 payload — see the module docstring), so a match is always a
#: genuine loss, while the bare ``name.jpg`` shape is ALSO exactly what
#: markitdown emits for an ordinary, un-lost relative image reference inside
#: an HTML document it converts (``<img src="logo.jpg">`` ->
#: ``![Logo](logo.jpg)`` — ``ENGINE_MARKITDOWN`` covers HTML too). Nothing in
#: the regex itself can tell those two apart; :func:`_disclose_image_
#: placeholders` gates ``jpgname`` at runtime on the document actually being
#: a PowerPoint conversion (live finding 2026-09-09 — see that function).
_IMAGE_MARKER_RE = re.compile(
    r"<!--\s*Slide number:\s*(?P<slide>\d+)\s*-->"
    r"|^#{1,6}[ \t]+(?P<heading>[^\n]+)$"
    r"|!\[[^\]]*\]\((?P<b64>data:[\w./+-]*;base64\.\.\.)\)"
    r"|!\[[^\]]*\]\((?P<jpgname>\w+\.jpg)\)",
    re.MULTILINE,
)

#: A section heading can be arbitrarily long; a disclosure line stays a
#: one-line pointer, not a second copy of the document's outline.
_MAX_LOCATION_HEADING_CHARS = 80


def _match_is_the_whole_line(text: str, m: "re.Match[str]") -> bool:
    """Is this match the entire line it sits on?

    markitdown emits a pptx picture placeholder as a paragraph of its own
    (`<!-- Slide number: 1 -->\n\n![image.png](Picture1.jpg)`), while text
    the deck's author typed sits inside a sentence. A slide's text box may
    legitimately contain Markdown-looking text — `Deploy with
    ![status](logo.jpg)` — and rewriting that would delete real slide
    content and announce a picture that never existed. The document having
    slides is not enough to tell those apart, because every converted deck
    has slide markers; the placeholder standing alone is what distinguishes
    markitdown's own output from the author's words.
    """
    line_start = text.rfind("\n", 0, m.start()) + 1
    line_end = text.find("\n", m.end())
    line_end = len(text) if line_end == -1 else line_end
    return text[line_start:line_end].strip() == m.group(0).strip()


def _disclose_image_placeholders(text: str) -> tuple[str, int]:
    """Rewrite markitdown's image-loss placeholders into an honest,
    per-document, located disclosure. Never OCR, never a vision call — see
    the module docstring's "Embedded pictures" section for the two shapes
    replaced and why.

    Walks :data:`_IMAGE_MARKER_RE`'s matches in document order exactly once,
    tracking the most recently seen slide number (pptx) and heading (docx)
    as TWO SEPARATE pieces of state — never one "most recent wins" location,
    because a pptx slide's own title becomes a ``#`` heading in markitdown's
    output right after that slide's own number comment: if a single most-
    recent value tracked both shapes, that heading would silently overwrite
    the (more precise) slide number for every image on the same slide (live
    finding 2026-09-09). The slide number, once seen, always wins for a
    ``jpgname`` (pptx) placeholder; a ``b64`` (docx) placeholder only ever
    appears in a document that never emits a slide marker at all, so it
    falls straight through to the heading, exactly as before. A NEW slide
    marker resets the heading, so a stale title from a previous slide (or
    from before the first one) can never leak onto this slide's images
    either. An image before the first slide/heading marker gets no location
    clause at all — still a stable, document-scoped ordinal, just without a
    "where" to add.

    ``jpgname`` matches (see :data:`_IMAGE_MARKER_RE`) are additionally
    gated on the document having shown at least one slide-number marker
    ANYWHERE — the signal that this is genuinely a PowerPoint conversion,
    not an HTML (or other markitdown) document whose own ordinary,
    un-lost relative image reference happens to render as the identical
    ``![alt](name.jpg)`` markdown shape. Without a slide marker in sight, a
    ``jpgname`` match is left untouched: not rewritten, not counted — it was
    never actually lost.

    Returns ``(rewritten_text, image_count)``. ``image_count`` is ``0`` and
    ``text`` is returned UNCHANGED (not even copied) when there is nothing to
    rewrite — the common case, and every document with no embedded pictures
    (or, on an HTML document, no genuine picture loss to disclose).
    """
    matches = list(_IMAGE_MARKER_RE.finditer(text))
    saw_slide_marker = any(m.group("slide") is not None for m in matches)

    def _is_disclosable_image(m: re.Match[str]) -> bool:
        if m.group("slide") is not None or m.group("heading") is not None:
            return False
        if m.group("jpgname") is not None:
            if not saw_slide_marker:
                return False  # an ordinary HTML image link, not a lost picture
            if not _match_is_the_whole_line(text, m):
                return False  # the deck author's own words, not a placeholder
        return True

    total = sum(1 for m in matches if _is_disclosable_image(m))
    if total == 0:
        return text, 0

    out: list[str] = []
    last_end = 0
    current_slide: str | None = None
    current_heading: str | None = None
    ordinal = 0
    for m in matches:
        out.append(text[last_end : m.start()])
        last_end = m.end()
        if m.group("slide") is not None:
            current_slide = m.group("slide")
            current_heading = None
            out.append(m.group(0))
            continue
        if m.group("heading") is not None:
            heading_text = m.group("heading").strip()[:_MAX_LOCATION_HEADING_CHARS].replace("[", "(").replace("]", ")")
            current_heading = heading_text
            out.append(m.group(0))
            continue
        if m.group("jpgname") is not None and not _is_disclosable_image(m):
            # Not a lost picture: either the document has no slides at all
            # (an ordinary HTML image link) or this one sits inside a line of
            # slide text. Left exactly as markitdown emitted it.
            out.append(m.group(0))
            continue
        # An image marker: neither the slide nor the heading branch matched,
        # and it is not a filtered-out HTML jpg link.
        ordinal += 1
        if current_slide is not None:
            current_location: str | None = f"slide {current_slide}"
        elif current_heading is not None:
            current_location = f'in section "{current_heading}"'
        else:
            current_location = None
        location = f", {current_location}" if current_location else ""
        out.append(f"[image {ordinal} of {total} in this document — not indexed{location}]")
    out.append(text[last_end:])
    return "".join(out), total


def convert_to_markdown(
    path: Path,
    mime: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    source_path: str | None = None,
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
        source_path: the document's ORIGINAL drive-relative path (as opposed
            to ``path``, the local temp file) — threaded through to the scan-
            OCR triage stage-0 path rules (``extraction.scan_ocr.triage.
            skip_path_patterns``/``full_path_patterns``), which need the
            real path to match against. Used for nothing else in this
            module: routing here still keys off ``path``'s suffix/mime, and
            ``source_path`` never appears in a raised error's message.
            ``None`` (any caller with no path context) matches no pattern.
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

    # Cheapest possible refusal, and it must precede EVERY route below (not
    # just the plain markitdown one): a `.docx`/`.pptx`/`.xlsx` that is not a
    # zip is not a truncated OOXML package, it is not an OOXML package at all,
    # and neither a LibreOffice resave nor a CSV/PDF rescue rung can make one
    # out of it — so the rescue chain would burn three subprocesses to reach
    # the same answer this line gives for free.
    if suffix in _OOXML_SUFFIXES and not zipfile.is_zipfile(path):
        raise ConversionError(filename, f"not an OOXML archive — a '{suffix}' must be a zip package")

    rescue = ""
    if suffix in PASSTHROUGH_SUFFIXES or (not suffix and declared in _PASSTHROUGH_MIMES):
        text = _read_text(path, filename, max_chars)
        engine = ENGINE_PASSTHROUGH
    elif suffix == ".pdf" or (not suffix and declared in _PDF_MIMES):
        text, engine = _convert_pdf(path, filename, source_path=source_path)
    elif suffix == ".xlsx" and _file_size(path) > LARGE_XLSX_STREAMING_THRESHOLD_BYTES:
        text, engine, rescue = _convert_large_xlsx(path, filename, max_chars)
    elif suffix in LEGACY_OFFICE_SUFFIXES:
        text, engine, rescue = _convert_legacy_office(path, filename, suffix, max_chars=max_chars)
    elif suffix in DOCLING_SUFFIXES and docling_capability():
        text, engine, rescue = _convert_docling_then_rescue(path, filename, suffix, max_chars=max_chars)
    elif suffix in RESCUE_RESAVE_TARGETS:
        text, engine, rescue = _convert_markitdown_with_rescue(
            path, filename, suffix, max_chars=max_chars, pre_resaved=False
        )
    else:
        text, engine = _convert_document(path, filename, suffix, hint)

    if not text.strip():
        # Conversion succeeded and there was nothing in it. Not an error: a
        # scanned PDF and a blank .txt are both legitimate crawl outcomes, and
        # the caller decides whether to route them to a vision transcription
        # pass. This module never calls a model.
        return ConvertResult(markdown="", engine=ENGINE_EMPTY, rescue=rescue)

    image_count = 0
    if engine in _IMAGE_DISCLOSURE_ENGINES:
        text, image_count = _disclose_image_placeholders(text)

    return ConvertResult(markdown=_truncate(text, max_chars), engine=engine, rescue=rescue, image_count=image_count)


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


def _convert_docling_then_rescue(path: Path, filename: str, suffix: str, *, max_chars: int) -> tuple[str, str, str]:
    """``.docx``/``.pptx`` on an image that has Docling: Docling first, the
    rescue chain behind it.

    Docling reads layout — the tables and reading order markitdown flattens —
    so it goes first when the extra is present. Docling REFUSING a document is
    not a failure, and it must not cost the file its rescue chain either: it
    falls through to exactly the route it would have taken on an image without
    Docling, rungs included, so the two features compose instead of the newer
    one shadowing the older.

    The one subtle case is Docling present, refusing, and no second reader
    installed at all (a rich image built without the ``[extraction]`` extra):
    that is the file's problem, not a missing extra — a
    ``MissingConversionDependency`` would send an operator to install a reader
    for a document Docling already could not read — so it surfaces as a plain
    :class:`ConversionError` attributed to Docling.

    Returns ``(markdown, engine, rescue)`` — the rescue chain's own triple, or
    ``(text, ENGINE_DOCLING, "")`` when Docling reads the file.
    """
    text = docling_markdown(path)
    if text is not None:
        return text, ENGINE_DOCLING, ""
    try:
        return _convert_markitdown_with_rescue(path, filename, suffix, max_chars=max_chars, pre_resaved=False)
    except MissingConversionDependency as exc:
        raise ConversionError(
            filename,
            "docling could not convert this file, and no second reader is installed",
            engine=ENGINE_DOCLING,
        ) from exc


def _convert_document(path: Path, filename: str, suffix: str, hint: str | None) -> tuple[str, str]:
    """Everything no route above claimed: straight to markitdown.

    Docling and the rescue chain both key off a suffix set the dispatch checks
    BEFORE this function, so by the time a file arrives here it is neither an
    OOXML package nor a legacy Office one — markitdown is the only reader that
    was ever going to read it.
    """
    return _convert_markitdown(path, filename, file_extension=hint), ENGINE_MARKITDOWN


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
        converter = MarkItDown(enable_plugins=False)
        if file_extension:
            result = converter.convert_local(str(path), file_extension=file_extension)
        else:
            result = converter.convert(str(path))
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
            error_class=ERROR_CLASS_MARKITDOWN_REJECT,
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


def _libreoffice_exit_error_class(returncode: int) -> str:
    """Classify a non-zero ``soffice`` exit for :func:`_run_libreoffice_convert`.

    A NEGATIVE returncode (POSIX: ``subprocess`` reports ``-N`` for a
    process killed by signal ``N``) means something outside the document
    killed the subprocess — ``SIGKILL`` (``-9``) is this pool's own memory
    guard or the kernel's OOM killer (:data:`ERROR_CLASS_MEMORY_KILL`, same
    reasoning as the conversion pool's own ``_ConvertMemoryGuard``), any
    other signal is an ordinary crash (:data:`ERROR_CLASS_WORKER_CRASH`).
    Both are ENVIRONMENTAL, not a property of the file, and stay retryable.
    A POSITIVE returncode means ``soffice`` ran to completion and refused
    the file on its own — the file itself is the reason
    (:data:`ERROR_CLASS_LIBREOFFICE_NO_OUTPUT`, deterministic).
    """
    if returncode < 0:
        return ERROR_CLASS_MEMORY_KILL if returncode == -9 else ERROR_CLASS_WORKER_CRASH
    return ERROR_CLASS_LIBREOFFICE_NO_OUTPUT


def _run_libreoffice_convert(path: Path, filename: str, target_format: str, *, engine: str) -> Path:
    """Shell out to headless LibreOffice (``soffice --headless --convert-to
    <target> --outdir <tmpdir> <file>``) and return the path to the
    converted file, inside a throwaway temp directory.

    On FAILURE the temp directory is already removed before this function
    raises. On SUCCESS it is left in place — the CALLER owns the returned
    path's parent directory and must remove it once done reading from it
    (typically in a ``finally``). This is the core :func:`_convert_legacy_
    office` used to shell out to LibreOffice before the rescue chain existed
    — factored out so BOTH the legacy-format route (always resaves) and the
    rescue chain's rung 1 (resave-and-retry) and CSV/PDF fallback rung reuse
    the identical mechanism rather than three copies of the same subprocess
    plumbing.

    ``soffice`` missing from ``PATH`` raises the same typed
    :class:`MissingConversionDependency` a missing Python backend would,
    naming ``"libreoffice"``, so the file is COUNTED as a named conversion
    failure exactly like a missing markitdown today — never silently
    skipped. A non-zero exit or a timeout raises :class:`ConversionError`,
    the same class :func:`_convert_markitdown` raises for a backend
    failure.
    """
    if shutil.which("soffice") is None:
        raise MissingConversionDependency(filename, "libreoffice", engine=engine)

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
                engine=engine,
                error_class=ERROR_CLASS_TIMEOUT,
            ) from exc

        if completed.returncode != 0:
            raise ConversionError(
                filename,
                f"libreoffice exited with status {completed.returncode}",
                engine=engine,
                error_class=_libreoffice_exit_error_class(completed.returncode),
            )

        converted = sorted(Path(tmpdir).glob(f"*.{target_format}"))
        if not converted:
            raise ConversionError(
                filename,
                "libreoffice produced no output file",
                engine=engine,
                error_class=ERROR_CLASS_LIBREOFFICE_NO_OUTPUT,
            )
        return converted[0]
    except BaseException:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise


def _convert_legacy_office(path: Path, filename: str, suffix: str, *, max_chars: int) -> tuple[str, str, str]:
    """Legacy Office / OpenDocument formats markitdown cannot read directly.

    Re-saves the file into the OOXML sibling markitdown already handles —
    ``.doc/.rtf/.odt`` → docx, ``.ppt/.odp`` → pptx, ``.xls/.ods/.xlsb/
    .xlsm`` → xlsx (:func:`_run_libreoffice_convert`) — and hands the
    result to :func:`_convert_markitdown_with_rescue` with ``pre_resaved=
    True``: this suffix already had its ONE LibreOffice resave, so a
    markitdown failure here goes straight to the rescue chain's rung 2
    (CSV/PDF fallback), never a second, redundant resave-and-retry.

    Returns ``(markdown, engine, rescue)`` — see :class:`ConvertResult`'s
    docstring for what ``rescue`` may hold. The temp directory
    :func:`_run_libreoffice_convert` created is ALWAYS removed, success or
    failure.
    """
    target_format = LEGACY_OFFICE_TARGETS[suffix]
    converted_path = _run_libreoffice_convert(path, filename, target_format, engine=ENGINE_LIBREOFFICE_MARKITDOWN)
    try:
        return _convert_markitdown_with_rescue(converted_path, filename, suffix, max_chars=max_chars, pre_resaved=True)
    finally:
        shutil.rmtree(converted_path.parent, ignore_errors=True)


# --------------------------------------------------------------- rescue chain


def _read_xlsx_as_text(path: Path, filename: str, max_chars: int, *, engine: str) -> str:
    """Read an ``.xlsx``-shaped file directly with openpyxl in
    ``read_only=True`` streaming mode — never through markitdown's full
    in-memory parse.

    ``read_only`` mode iterates rows off the zip's XML stream instead of
    materializing the whole worksheet, which is what keeps a huge workbook's
    memory flat — and, because ``iter_rows`` is a genuine generator over
    that stream, breaking out of the loop below genuinely stops reading
    rather than merely stopping the caller from seeing more. One block per
    sheet, headed by ``## <sheet name>``, rows joined as a comma-separated
    line (``None`` cells rendered empty).

    Stops emitting the moment the RUNNING TOTAL across every sheet so far
    would reach ``max_chars`` (the caller's document-wide cap) — a live
    finding 3M-character workbook stops early, mid-sheet, instead of ever
    materializing the whole thing only to have :func:`_truncate` cut it
    down afterward. Shared across every sheet on purpose: a workbook with
    several huge sheets still stops at the SAME total ceiling
    :func:`convert_to_markdown` would have applied regardless of how many
    sheets it took to get there.

    Used both by the large-``.xlsx`` streaming route
    (:func:`_convert_large_xlsx`) and the rescue chain's CSV fallback rung
    (:func:`_rescue_fallback`) — the same reader, two different reasons to
    reach it.
    """
    try:
        import openpyxl
    except Exception as exc:
        raise MissingConversionDependency(filename, "openpyxl", engine=engine, cause=exc) from exc

    try:
        workbook = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception as exc:
        raise ConversionError(
            filename,
            f"openpyxl could not open this file ({type(exc).__name__})",
            engine=engine,
            error_class=ERROR_CLASS_MARKITDOWN_REJECT,
        ) from exc

    parts: list[str] = []
    total = 0
    try:
        for sheet in workbook.worksheets:
            if total >= max_chars:
                break
            header = f"## {sheet.title}\n\n"
            parts.append(header)
            total += len(header)
            for row in sheet.iter_rows(values_only=True):
                if total >= max_chars:
                    break
                line = ",".join("" if cell is None else str(cell) for cell in row) + "\n"
                parts.append(line)
                total += len(line)
    except Exception as exc:
        raise ConversionError(
            filename,
            f"openpyxl could not read this file's rows ({type(exc).__name__})",
            engine=engine,
            error_class=ERROR_CLASS_MARKITDOWN_REJECT,
        ) from exc
    finally:
        workbook.close()
    return _normalize_newlines("".join(parts))


def _convert_large_xlsx(path: Path, filename: str, max_chars: int) -> tuple[str, str, str]:
    """The size-based cheap route for a large ``.xlsx`` (see :data:`LARGE_
    XLSX_STREAMING_THRESHOLD_BYTES`): read it directly with openpyxl
    streaming (:func:`_read_xlsx_as_text`), skipping markitdown's full
    in-memory parse entirely.

    If even THAT fails, falls back to a fresh LibreOffice resave (a clean,
    LibreOffice-normalized copy can succeed where reading the original
    raw file did not) and streams the resaved copy the same way — the same
    CSV-fallback mechanism :func:`_rescue_fallback` uses, reached directly
    here since markitdown was never attempted on this route to retry.
    """
    try:
        text = _read_xlsx_as_text(path, filename, max_chars, engine=ENGINE_XLSX_STREAMING)
        return text, ENGINE_XLSX_STREAMING, ""
    except (UnsupportedConversionFormat, MissingConversionDependency):
        raise
    except ConversionError as exc:
        converted_path = _run_libreoffice_convert(path, filename, "xlsx", engine=ENGINE_CSV_FALLBACK)
        try:
            text = _read_xlsx_as_text(converted_path, filename, max_chars, engine=ENGINE_CSV_FALLBACK)
        except ConversionError as exc2:
            raise ConversionError(
                filename,
                f"openpyxl streaming: {exc} | libreoffice resave+streaming: {exc2}",
                engine=ENGINE_XLSX_STREAMING,
                error_class=exc2.error_class or ERROR_CLASS_MARKITDOWN_REJECT,
            ) from exc2
        finally:
            shutil.rmtree(converted_path.parent, ignore_errors=True)
        return text, ENGINE_CSV_FALLBACK, "csv_fallback"


def _rescue_fallback(path: Path, filename: str, suffix: str, *, max_chars: int, pre_resaved: bool) -> tuple[str, str]:
    """Rescue chain rung 2 — the last resort once markitdown (and, for a
    direct-route suffix, rung 1's resave-and-retry) have both failed.

    Spreadsheets (:data:`RESCUE_FALLBACK_KIND` ``"csv"``) fall back to a
    LibreOffice-produced ``.xlsx`` read through :func:`_read_xlsx_as_text` —
    NOT ``soffice --convert-to csv`` directly: that only ever exports the
    ACTIVE sheet, and this rung's whole point is every sheet, headed by its
    own name. Decks/documents (``"pdf"``) fall back to a LibreOffice-produced
    PDF run through this module's own PDF route (:func:`_convert_pdf`) —
    reusing the one PDF pipeline rather than a second text extractor.

    ``pre_resaved`` mirrors :func:`_convert_markitdown_with_rescue`'s own
    parameter: when the caller already handed this function a LibreOffice-
    produced ``.xlsx`` (the legacy-office route's one resave), the csv
    branch reads it directly instead of paying for a redundant xlsx-to-xlsx
    LibreOffice round trip.

    Returns ``(markdown, engine)``.
    """
    kind = RESCUE_FALLBACK_KIND.get(suffix)
    if kind == "csv":
        if pre_resaved:
            return _read_xlsx_as_text(path, filename, max_chars, engine=ENGINE_CSV_FALLBACK), ENGINE_CSV_FALLBACK
        converted_path = _run_libreoffice_convert(path, filename, "xlsx", engine=ENGINE_CSV_FALLBACK)
        try:
            return (
                _read_xlsx_as_text(converted_path, filename, max_chars, engine=ENGINE_CSV_FALLBACK),
                ENGINE_CSV_FALLBACK,
            )
        finally:
            shutil.rmtree(converted_path.parent, ignore_errors=True)
    if kind == "pdf":
        converted_path = _run_libreoffice_convert(path, filename, "pdf", engine=ENGINE_PDF_FALLBACK)
        try:
            text, _pdf_engine = _convert_pdf(converted_path, filename)
            return text, ENGINE_PDF_FALLBACK
        finally:
            shutil.rmtree(converted_path.parent, ignore_errors=True)
    raise ConversionError(filename, "no rescue fallback available for this file type", engine=ENGINE_MARKITDOWN)


#: Rescue-chain gating (2026-09-04 finding #66 item 3 — live finding:
#: hundreds of spreadsheets that fail conversion in every run each walked
#: BOTH rescue rungs under the size-aware conversion budget, holding 16-22
#: conversion children for minutes; page throughput fell from ~70k items/h
#: to ~100 items per 10 minutes). When rung 1's own LibreOffice re-save
#: fails for one of these reasons, its CSV/PDF fallback (rung 2) reaches the
#: IDENTICAL LibreOffice mechanism on the IDENTICAL source bytes — retrying
#: it is not a second chance, it is the same failure again at the same (or
#: greater) cost, so :func:`_convert_markitdown_with_rescue` stops the chain
#: right there instead. A markitdown-only failure on the resave's OUTPUT
#: (``libreoffice resave+retry`` — the resave itself succeeded) is NOT in
#: this set: the fallback rung reads the file through a different mechanism
#: (openpyxl direct, or a PDF re-render) and may still succeed where
#: markitdown alone did not.
_RESCUE_CHAIN_STOP_CLASSES = frozenset(
    {
        ERROR_CLASS_LIBREOFFICE_NO_OUTPUT,
        ERROR_CLASS_TIMEOUT,
        ERROR_CLASS_MEMORY_KILL,
        ERROR_CLASS_WORKER_CRASH,
    }
)


def _convert_markitdown_with_rescue(
    path: Path, filename: str, suffix: str, *, max_chars: int, pre_resaved: bool
) -> tuple[str, str, str]:
    """Try markitdown; on a genuine backend failure for a rescuable suffix,
    escalate through the rescue chain instead of failing outright.

    ``pre_resaved`` is ``True`` only when the caller (:func:`_convert_legacy
    _office`) already ran this exact file through ONE LibreOffice resave
    before calling here — in that case rung 1 (a SECOND resave-and-retry,
    into the SAME already-resaved format) is skipped as redundant and a
    failure escalates straight to rung 2. For the direct route (``.xlsx``/
    ``.pptx``/``.docx``, ``pre_resaved=False``) both rungs are tried in
    order — UNLESS rung 1's own re-save fails in a way
    :data:`_RESCUE_CHAIN_STOP_CLASSES` says makes rung 2 futile (see that
    constant's docstring), in which case the chain stops right there.

    Never rescues :class:`UnsupportedConversionFormat` (no backend was even
    attempted — see that class's docstring) or :class:`MissingConversion
    Dependency` for markitdown itself (installing LibreOffice cannot fix a
    missing markitdown): both propagate immediately, unrescued.

    Returns ``(markdown, engine, rescue)``. When every rung fails (or the
    chain is stopped early), raises a single :class:`ConversionError` whose
    message concatenates EACH rung's own last error text (not just the
    first failure) — see ``docs/sharepoint-extraction.md`` for why: the next
    reconciliation pass needs to name the reason, and "could not convert"
    alone does not. Its ``error_class`` names whichever rung actually
    stopped the chain, not necessarily the FIRST failure.
    """
    base_engine = ENGINE_LIBREOFFICE_MARKITDOWN if pre_resaved else ENGINE_MARKITDOWN
    try:
        text = _convert_markitdown(path, filename)
        return text, base_engine, ""
    except (UnsupportedConversionFormat, MissingConversionDependency):
        raise
    except ConversionError as first_exc:
        if suffix not in RESCUE_FALLBACK_KIND:
            raise
        errors = [f"markitdown: {first_exc}"]

        if not pre_resaved and suffix in RESCUE_RESAVE_TARGETS:
            try:
                resaved_path = _run_libreoffice_convert(
                    path, filename, RESCUE_RESAVE_TARGETS[suffix], engine=ENGINE_LIBREOFFICE_RESCUE
                )
            except ConversionError as resave_exc:
                errors.append(f"libreoffice resave: {resave_exc}")
                if resave_exc.error_class in _RESCUE_CHAIN_STOP_CLASSES:
                    errors.append(
                        f"{RESCUE_FALLBACK_KIND[suffix]} fallback: skipped — rung 1's own re-save already failed"
                    )
                    raise ConversionError(
                        filename, " | ".join(errors), engine=base_engine, error_class=resave_exc.error_class
                    ) from first_exc
            else:
                try:
                    text = _convert_markitdown(resaved_path, filename)
                    return text, ENGINE_LIBREOFFICE_RESCUE, "libreoffice_resave"
                except ConversionError as retry_exc:
                    errors.append(f"libreoffice resave+retry: {retry_exc}")
                finally:
                    shutil.rmtree(resaved_path.parent, ignore_errors=True)

        try:
            text, engine = _rescue_fallback(path, filename, suffix, max_chars=max_chars, pre_resaved=pre_resaved)
        except ConversionError as fallback_exc:
            errors.append(f"{RESCUE_FALLBACK_KIND[suffix]} fallback: {fallback_exc}")
            raise ConversionError(
                filename,
                " | ".join(errors),
                engine=base_engine,
                error_class=fallback_exc.error_class or ERROR_CLASS_MARKITDOWN_REJECT,
            ) from first_exc
        rescue = "csv_fallback" if engine == ENGINE_CSV_FALLBACK else "pdf_fallback"
        return text, engine, rescue


# ---------------------------------------------------------------------- pdf


def _convert_pdf(path: Path, filename: str, *, source_path: str | None = None) -> tuple[str, str]:
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
    except Exception as exc:
        # See the matching comment in _convert_markitdown: broader than
        # ImportError so a MemoryError from this import's own native
        # dependency chain carries its cause instead of reading as a plain
        # "not installed".
        raise MissingConversionDependency(filename, "pypdfium2", engine=ENGINE_PYPDFIUM2, cause=exc) from exc

    from src.ingest.pdf_structure import reconstruct_pdf

    try:
        structured = str(reconstruct_pdf(path)).strip()
    except Exception as exc:
        raise ConversionError(
            filename,
            f"could not convert PDF ({type(exc).__name__})",
            engine=ENGINE_PYPDFIUM2,
            error_class=ERROR_CLASS_PDFIUM_ERROR,
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
        return scan_ocr.transcribe_scan(path, source_path=source_path), ENGINE_OCR
    except scan_ocr.ScanOcrUnavailable as exc:
        raise ConversionError(filename, f"scan OCR failed: {exc}", engine=ENGINE_OCR) from exc


# ------------------------------------------------------------------ helpers


def _file_size(path: Path) -> int:
    """Best-effort file size in bytes — ``0`` on any ``OSError`` (a file the
    earlier ``path.is_file()`` check already proved exists ought never fail
    here, but this is a routing decision, not the conversion itself, so it
    must never be where a crawl's error surfaces)."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


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
    "CONVERSION_BUDGET_BASE_SECONDS",
    "CONVERSION_BUDGET_MAX_SECONDS",
    "CONVERSION_BUDGET_PER_MB_SECONDS",
    "DEFAULT_MAX_CHARS",
    "DETERMINISTIC_ERROR_CLASSES",
    "DOCLING_SUFFIXES",
    "ENGINE_DOCLING",
    "ENGINE_EMPTY",
    "ENGINE_MARKITDOWN",
    "ENGINE_OCR",
    "ENGINE_PASSTHROUGH",
    "ENGINE_PYPDFIUM2",
    "ERROR_CLASS_DOWNLOAD_ERROR",
    "ERROR_CLASS_INGEST_ERROR",
    "ERROR_CLASS_LIBREOFFICE_NO_OUTPUT",
    "ERROR_CLASS_MARKITDOWN_REJECT",
    "ERROR_CLASS_MEMORY_KILL",
    "ERROR_CLASS_OTHER",
    "ERROR_CLASS_PDFIUM_ERROR",
    "ERROR_CLASS_TIMEOUT",
    "ERROR_CLASS_WORKER_CRASH",
    "LARGE_XLSX_STREAMING_THRESHOLD_BYTES",
    "LEGACY_OFFICE_SUFFIXES",
    "LEGACY_OFFICE_TARGETS",
    "LIBREOFFICE_TIMEOUT_SECONDS",
    "PAGE_BREAK",
    "PASSTHROUGH_SUFFIXES",
    "RESCUE_FALLBACK_KIND",
    "RESCUE_RESAVE_TARGETS",
    "ConversionError",
    "ConvertResult",
    "MissingConversionDependency",
    "UnsupportedConversionFormat",
    "conversion_budget_seconds",
    "convert_to_markdown",
    "docling_capability",
    "docling_markdown",
]
