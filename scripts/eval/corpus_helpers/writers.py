"""Format writers for the planted corpus.

Real Office binaries (.docx/.pptx/.xlsx) are produced via python-docx /
python-pptx / openpyxl when installed. When a library is missing, the
corresponding writer falls back to a Markdown rendering of the same content
plus an HTML-comment note recording the intended format -- the planted
quotes still land verbatim in the file, so every downstream fixture
(EQ1/EQ3/EQ8 substring checks) stays satisfiable regardless of environment.
The scanned-PDF trap uses Pillow (already a core dependency) directly and
never falls back -- it is only used for one deliberately-image-only document.
"""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont

    _PIL_AVAILABLE = True
except ImportError:  # pragma: no cover - Pillow is a core dependency
    _PIL_AVAILABLE = False


def _has(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def available_formats() -> dict[str, bool]:
    """Which optional Office-writer libraries are importable right now."""
    return {
        "docx": _has("docx"),
        "pptx": _has("pptx"),
        "xlsx": _has("openpyxl"),
    }


FALLBACK_NOTE = {
    "docx": "<!-- corpus_gen fallback: intended .docx, python-docx unavailable -->",
    "pptx": "<!-- corpus_gen fallback: intended .pptx, python-pptx unavailable -->",
    "xlsx": "<!-- corpus_gen fallback: intended .xlsx, openpyxl unavailable -->",
}


def _write_markdown(path: Path, title: str, paragraphs: list[str], fallback_note: str | None = None) -> None:
    lines = []
    if fallback_note:
        lines.append(fallback_note)
        lines.append("")
    lines.append(f"# {title}")
    lines.append("")
    for p in paragraphs:
        lines.append(p)
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_document(
    path_no_ext: Path,
    fmt: str,
    title: str,
    paragraphs: list[str],
) -> tuple[Path, str, bool]:
    """Write one document. Returns (actual_path, actual_format, was_fallback).

    `fmt` is the REQUESTED format (md/docx/pptx/xlsx); the actual format may
    differ (fallback to md) if the writer library is unavailable.
    """
    if fmt == "md":
        p = path_no_ext.with_suffix(".md")
        _write_markdown(p, title, paragraphs)
        return p, "md", False

    if fmt == "docx":
        if _has("docx"):
            p = path_no_ext.with_suffix(".docx")
            _write_docx(p, title, paragraphs)
            return p, "docx", False
        p = path_no_ext.with_suffix(".md")
        _write_markdown(p, title, paragraphs, FALLBACK_NOTE["docx"])
        return p, "md", True

    if fmt == "pptx":
        if _has("pptx"):
            p = path_no_ext.with_suffix(".pptx")
            _write_pptx(p, title, paragraphs)
            return p, "pptx", False
        p = path_no_ext.with_suffix(".md")
        # Deck-shaped fallback: one paragraph per "slide" heading.
        slide_paragraphs = [f"## Slide {i + 1}\n\n{p}" for i, p in enumerate(paragraphs)]
        _write_markdown(p, title, slide_paragraphs, FALLBACK_NOTE["pptx"])
        return p, "md", True

    if fmt == "xlsx":
        if _has("openpyxl"):
            p = path_no_ext.with_suffix(".xlsx")
            _write_xlsx(p, title, paragraphs)
            return p, "xlsx", False
        p = path_no_ext.with_suffix(".md")
        # Table-shaped fallback: a Markdown table, one row per paragraph.
        table = ["| # | Line |", "|---|---|"]
        for i, para in enumerate(paragraphs):
            table.append(f"| {i + 1} | {para} |")
        _write_markdown(p, title, ["\n".join(table)], FALLBACK_NOTE["xlsx"])
        return p, "md", True

    raise ValueError(f"unknown format: {fmt}")


def _write_docx(path: Path, title: str, paragraphs: list[str]) -> None:
    import docx  # noqa: PLC0415

    doc = docx.Document()
    doc.add_heading(title, level=1)
    for p in paragraphs:
        doc.add_paragraph(p)
    doc.save(str(path))


def _write_pptx(path: Path, title: str, paragraphs: list[str]) -> None:
    from pptx import Presentation  # noqa: PLC0415
    from pptx.util import Inches, Pt  # noqa: PLC0415

    prs = Presentation()
    title_layout = prs.slide_layouts[0]
    body_layout = prs.slide_layouts[1]

    slide = prs.slides.add_slide(title_layout)
    slide.shapes.title.text = title

    for p in paragraphs:
        slide = prs.slides.add_slide(body_layout)
        slide.shapes.title.text_frame.text = title
        body = slide.placeholders[1].text_frame
        body.text = p
        body.paragraphs[0].font.size = Pt(18)
    # Silence unused-import lints for Inches (kept for future slide sizing).
    _ = Inches
    prs.save(str(path))


def _write_xlsx(path: Path, title: str, paragraphs: list[str]) -> None:
    import openpyxl  # noqa: PLC0415

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A1"] = title
    ws["A2"] = "#"
    ws["B2"] = "Line"
    for i, p in enumerate(paragraphs):
        ws.cell(row=i + 3, column=1, value=i + 1)
        ws.cell(row=i + 3, column=2, value=p)
    wb.save(str(path))


def write_scan_pdf(path: Path, planted_text: str, extra_lines: list[str] | None = None) -> None:
    """Write an image-only ("scanned") PDF: no text layer, planted text
    baked into pixels only. Requires Pillow (core dependency)."""
    if not _PIL_AVAILABLE:  # pragma: no cover - Pillow is a core dependency
        raise RuntimeError("Pillow is required to write the scan trap")

    width, height = 850, 1100  # ~US-letter at 100dpi
    img = Image.new("L", (width, height), color=255)
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()

    lines = [planted_text, *(extra_lines or [])]
    y = 80
    for line in lines:
        draw.text((60, y), line, fill=0, font=font)
        y += 40

    # Pillow's PDF writer stamps CreationDate/ModDate with the current wall
    # clock by default, which would make otherwise-deterministic byte output
    # (and therefore the doc_id sha256) depend on when the generator ran.
    # Pin both to a fixed epoch so re-runs with the same seed are identical.
    fixed_time = time.gmtime(0)
    img.convert("RGB").save(str(path), "PDF", resolution=100.0, creationDate=fixed_time, modDate=fixed_time)


# ── Extraction (for the generator's own tests, mirrors what a real
#    conversion pipeline would read back) ──────────────────────────────


def extract_text(path: Path) -> str | None:
    """Best-effort text extraction, matching the writer library used for
    that format. Returns None if there's no extraction path (e.g. a
    genuine image-only scan) or the library is unavailable."""
    suffix = path.suffix.lower()
    if suffix == ".md":
        return path.read_text(encoding="utf-8")
    if suffix == ".docx" and _has("docx"):
        import docx  # noqa: PLC0415

        doc = docx.Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs)
    if suffix == ".pptx" and _has("pptx"):
        from pptx import Presentation  # noqa: PLC0415

        prs = Presentation(str(path))
        chunks = []
        for slide in prs.slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    chunks.append(shape.text_frame.text)
        return "\n".join(chunks)
    if suffix == ".xlsx" and _has("openpyxl"):
        import openpyxl  # noqa: PLC0415

        wb = openpyxl.load_workbook(str(path), read_only=True)
        chunks = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                for cell in row:
                    if cell is not None:
                        chunks.append(str(cell))
        return "\n".join(chunks)
    if suffix == ".pdf":
        import pypdf  # noqa: PLC0415

        reader = pypdf.PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    return None
