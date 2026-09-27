"""Multi-format document extraction (core/document_extractor.py).

Milestone scope: extraction only, building on core/pdf_extractor.py's
`Word`/`build_comparison_tokens`/`ComparisonToken` -- nothing here
changes how two word streams get DIFFED (core/diff_engine.py is
format-agnostic already), only how a word stream gets BUILT from a
given file.

Every supported format is normalized to the SAME `Word` shape PDF
extraction already produces: `text`, a 1-indexed `page`, and
(x0, top, x1, bottom) in points, top-left origin. That is what makes
one diff engine and one highlighting UI work for every format without
per-format special cases anywhere else in the app.

What "page" and "coordinates" mean per format (each is a genuine,
different kind of document, so this is a deliberate per-format
mapping, not an approximation of one universal page model):

  * PDF: an actual page, with real typeset coordinates -- unchanged,
    still handled by `core.pdf_extractor.extract_words`.
  * DOCX: laid out by a real engine (Microsoft Word, else LibreOffice)
    into a PDF -- see core/office_convert.py -- and that PDF goes
    through the normal PDF path, so pages, table borders, columns and
    coordinates are the real ones. Only when no engine is installed,
    every paragraph/table cell is placed on ONE synthesized page,
    top-to-bottom in document order (see `_iter_docx_body_order`), at a
    fixed line height: correct reading order, approximate layout.
  * PPTX: `page` = slide number (a natural, real unit for slides).
    Coordinates come from each shape's actual position on the slide
    (EMUs, converted to points), so highlighting lands on the real
    on-slide location, not a synthesized layout.
  * XLSX: `page` = sheet index (a natural, real unit for workbooks).
    Coordinates are synthesized from (row, column) on a fixed
    character grid -- spreadsheets have no inherent point-based
    layout either, so, like DOCX, this gives correct reading order
    and stable highlighting rather than reproducing Excel's actual
    column widths/row heights.
"""

from __future__ import annotations

import logging
import os

from core.pdf_extractor import (
    ComparisonToken,
    Word,
    _normalize_word_text,
    build_comparison_tokens,
    extract_words,
    is_scanned_pdf,
)
from core.office_convert import ConverterUnavailableError, convert_to_pdf
from core.script_text import SUBSCRIPT, SUPERSCRIPT, join_runs

logger = logging.getLogger(__name__)

DOCX_EXTENSIONS = {".docx"}
PPTX_EXTENSIONS = {".pptx"}
XLSX_EXTENSIONS = {".xlsx", ".xlsm"}
PDF_EXTENSIONS = {".pdf"}
SUPPORTED_EXTENSIONS = DOCX_EXTENSIONS | PPTX_EXTENSIONS | XLSX_EXTENSIONS | PDF_EXTENSIONS

# Points-per-inch, used everywhere a format's native unit (EMU, a
# character-grid cell) needs converting to the PDF-point space every
# other part of this app already works in.
POINTS_PER_INCH = 72.0
EMU_PER_INCH = 914400  # OOXML's native length unit (used by PPTX shape positions)


class UnsupportedFormatError(ValueError):
    """Raised for a file extension this app doesn't know how to extract
    text from. Distinct from ValueError so callers (the UI) can show a
    specific "unsupported format" message rather than a generic one.
    """


def document_kind(path: str) -> str:
    """Return one of "pdf", "docx", "pptx", "xlsx" for a supported
    file, based on its extension.

    Raises:
        UnsupportedFormatError: the extension isn't one this app
            supports.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in PDF_EXTENSIONS:
        return "pdf"
    if ext in DOCX_EXTENSIONS:
        return "docx"
    if ext in PPTX_EXTENSIONS:
        return "pptx"
    if ext in XLSX_EXTENSIONS:
        return "xlsx"
    raise UnsupportedFormatError(
        f"'{path}' has an unsupported file type ({ext or '(no extension)'}). "
        f"Supported: PDF (.pdf), Word (.docx), PowerPoint (.pptx), Excel (.xlsx/.xlsm)."
    )


def extract_document_words(path: str) -> list[Word]:
    """Single entry point: extract every Word from ``path``, whatever
    supported format it is.

    This is the ONLY function the rest of the app (workers/diff_worker.py)
    needs to call -- it dispatches to the right per-format extractor
    and always returns plain `Word` objects in the same shape/reading-
    order guarantees `core.pdf_extractor.extract_words` already makes
    for PDFs.

    Raises:
        UnsupportedFormatError: ``path``'s extension isn't supported.
        ValueError: the file exists but can't be parsed as its
            claimed format (corrupt/password-protected/not really that
            format despite its extension), or is a scanned/image-only
            PDF (this build compares digital text only).
    """
    kind = document_kind(path)
    if kind == "pdf":
        return _extract_pdf_words(path)
    if kind == "docx":
        pdf_path = layout_pdf_path(path)
        if pdf_path is not None:
            return _extract_pdf_words(pdf_path, display_name=os.path.basename(path))
        return _extract_docx_words(path)
    if kind == "pptx":
        return _extract_pptx_words(path)
    if kind == "xlsx":
        return _extract_xlsx_words(path)
    raise UnsupportedFormatError(path)  # unreachable given document_kind's own checks


def layout_pdf_path(path: str) -> str | None:
    """The PDF a DOCX is viewed and compared through, or None when no
    layout engine is installed (then the approximate synthesized layout
    is used for both). Conversion failures other than a missing engine
    raise ``core.office_convert.ConversionError``."""
    try:
        return convert_to_pdf(path)
    except ConverterUnavailableError:
        logger.warning("No Word/LibreOffice found; using approximate layout for %s", path)
        return None


def extract_synthesized_words(path: str) -> list[Word]:
    """Words for the approximate, engine-free DOCX/PPTX/XLSX layout."""
    kind = document_kind(path)
    if kind == "docx":
        return _extract_docx_words(path)
    if kind == "pptx":
        return _extract_pptx_words(path)
    if kind == "xlsx":
        return _extract_xlsx_words(path)
    raise UnsupportedFormatError(path)


def pdf_page_count(path: str) -> int:
    import fitz

    with fitz.open(path) as doc:
        return doc.page_count


def extract_pdf_page_range(
    path: str, first_page: int, last_page: int, display_name: str | None = None
) -> list[Word]:
    """Words of pages ``first_page``..``last_page`` of a PDF (for
    parallel extraction, see workers/extraction_cache.py). The chunk
    that includes page 1 also performs the scanned-document check."""
    if first_page == 1 and is_scanned_pdf(path):
        raise ValueError(_scanned_message(display_name or os.path.basename(path)))
    return extract_words(path, first_page, last_page)


def _scanned_message(name: str) -> str:
    return (
        f"'{name}' has no extractable digital text on any "
        "page (it appears to be a scanned/image-only PDF). This build only "
        "compares real digital text -- scanned/OCR input isn't supported."
    )


def _extract_pdf_words(path: str, display_name: str | None = None) -> list[Word]:
    if is_scanned_pdf(path):
        raise ValueError(_scanned_message(display_name or os.path.basename(path)))
    return extract_words(path)


def extract_document_tokens(path: str) -> list[ComparisonToken]:
    """``extract_document_words`` + ``build_comparison_tokens`` in one
    top-level call, so it can run in a worker process (see
    workers/extraction_cache.py)."""
    return build_comparison_tokens(extract_document_words(path))


def get_page_count(path: str) -> int:
    """Number of pages/slides/sheets in ``path``, in whatever unit
    `extract_document_words` uses as "page" for that format (see this
    module's docstring). Used by the UI to size Prev/Next controls and
    by the diff engine's cross-document page resolution.
    """
    kind = document_kind(path)
    if kind == "pdf":
        import fitz

        with fitz.open(path) as doc:
            return doc.page_count
    if kind == "docx":
        pdf_path = layout_pdf_path(path)
        if pdf_path is None:
            return 1  # approximate layout: one synthesized page
        return get_page_count(pdf_path)
    if kind == "pptx":
        from pptx import Presentation

        return len(Presentation(path).slides)
    if kind == "xlsx":
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            return len(wb.worksheets)
        finally:
            wb.close()
    raise UnsupportedFormatError(path)


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------

# Synthesized layout constants for DOCX (see module docstring: there is
# no real page/coordinate model to draw from, so one is built that is
# internally consistent -- same units used for reading order,
# highlighting, and the UI's synthesized preview render).
_DOCX_PAGE_WIDTH_PT = 612.0  # US Letter width, for wrapping only
_DOCX_MARGIN_PT = 72.0
_DOCX_LINE_HEIGHT_PT = 14.0
_DOCX_FONT_PT = 9.0  # must match ui.document_panel's _SYNTH_FONT_PT
_DOCX_TABLE_CELL_PAD_PT = 8.0

# Rough average glyph-advance weights (as a fraction of font point
# size), approximating typical sans-serif metrics -- not exact (no
# real font metrics are available in this Qt-free module), but close
# enough in relative proportion that an ALL-CAPS/bold heading is
# estimated meaningfully wider than a mixed-case sentence instead of
# using one flat per-character constant regardless of case. That
# mattered in practice: a flat estimate badly under-measured caps-heavy
# headings, and ui.document_panel's synthesized preview used to draw
# words at their real (much wider) rendered width with no compensation,
# so consecutive words visually ran together with no visible gap (e.g.
# "HIGHLIGHTS OF PRESCRIBING INFORMATION" rendering as the unreadable
# "HIGHLIGHTSOF PRESCRIBINGINFORMATION"). document_panel now also
# scales each word's glyphs to fit its assigned box exactly, which is
# the actual guarantee against overlap regardless of the estimate's
# accuracy -- this table only keeps that scaling close to 1:1 so the
# rendered text isn't needlessly squeezed or stretched.
_NARROW_CHARS = frozenset("iIl.,:;'!|jft ")
_WIDE_CHARS = frozenset("mMWw@")


def _char_width_pt(ch: str) -> float:
    if ch in _WIDE_CHARS:
        return _DOCX_FONT_PT * 0.85
    if ch in _NARROW_CHARS:
        return _DOCX_FONT_PT * 0.30
    if ch.isupper() or ch.isdigit():
        return _DOCX_FONT_PT * 0.62
    return _DOCX_FONT_PT * 0.50


def _text_width_pt(text: str) -> float:
    return sum(_char_width_pt(ch) for ch in text)


def _iter_docx_body_order(document):
    """Yield a document's paragraphs and tables in true reading order
    (interleaved as they actually appear), not python-docx's own
    `.paragraphs`/`.tables` accessors, which each only return one type
    and LOSE their relative document order -- a table between two
    paragraphs would otherwise end up read after every paragraph
    instead of in its real position.
    """
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            yield "paragraph", Paragraph(child, document)
        elif isinstance(child, CT_Tbl):
            yield "table", Table(child, document)


def _extract_docx_words(path: str) -> list[Word]:
    try:
        import docx
    except ImportError as exc:
        raise RuntimeError(
            "Reading .docx files needs the optional 'python-docx' package. "
            "Install it with:\n    pip install python-docx"
        ) from exc

    try:
        document = docx.Document(path)
    except Exception as exc:
        raise ValueError(f"Could not open '{path}' as a Word document: {exc}") from exc

    words: list[Word] = []
    y = _DOCX_MARGIN_PT
    max_x = _DOCX_PAGE_WIDTH_PT - _DOCX_MARGIN_PT

    for kind, element in _iter_docx_body_order(document):
        if kind == "paragraph":
            text = _docx_paragraph_text(element)
            if text.strip():
                y = _place_line(words, text, _DOCX_MARGIN_PT, y, max_x, page=1)
            else:
                y += _DOCX_LINE_HEIGHT_PT * 0.5  # a blank paragraph still takes some space
        elif kind == "table":
            y += _DOCX_LINE_HEIGHT_PT * 0.25
            for row in element.rows:
                row_start_y = y
                row_max_y = y
                x = _DOCX_MARGIN_PT
                col_width = (max_x - _DOCX_MARGIN_PT) / max(len(row.cells), 1)
                for cell in row.cells:
                    cell_text = "\n".join(_docx_paragraph_text(p) for p in cell.paragraphs)
                    if cell_text.strip():
                        end_y = _place_line(
                            words,
                            cell_text,
                            x + _DOCX_TABLE_CELL_PAD_PT,
                            row_start_y,
                            x + col_width - _DOCX_TABLE_CELL_PAD_PT,
                            page=1,
                        )
                        row_max_y = max(row_max_y, end_y)
                    x += col_width
                y = max(row_max_y, row_start_y + _DOCX_LINE_HEIGHT_PT)
    return words


def _docx_run_script(run) -> str | None:
    """"sup"/"sub" for a run formatted as superscript/subscript, directly
    or through its character style."""
    for font in (run.font, run.style.font if run.style is not None else None):
        if font is None:
            continue
        if font.superscript:
            return SUPERSCRIPT
        if font.subscript:
            return SUBSCRIPT
    return None


def _docx_paragraph_text(paragraph) -> str:
    """A paragraph's text with superscript/subscript runs kept visible
    (see core/script_text.py) -- ``paragraph.text`` drops that
    formatting."""
    runs = []
    for item in paragraph.iter_inner_content():
        for run in getattr(item, "runs", None) or [item]:  # a Hyperlink holds runs
            runs.append((run.text, _docx_run_script(run)))
    return join_runs(runs)


def _place_line(
    words: list[Word], text: str, x_start: float, y: float, x_max: float, page: int
) -> float:
    """Append Words for one paragraph/cell/shape's text, wrapping at
    ``x_max`` using a fixed average character width, tagged with
    ``page``. Returns the Y position after the last wrapped line (so
    the caller knows where to place whatever comes next). Shared by
    the DOCX, PPTX, and XLSX extractors -- the wrap/placement math is
    identical, only the page number and starting geometry differ.

    Text is run through the same invisible-artifact normalization
    (NFKC, zero-width chars, soft hyphens, NBSP, ligatures) PDF
    extraction already applies -- see core.pdf_extractor's
    ``_normalize_word_text`` -- so a DOCX/PPTX/XLSX word and its PDF
    counterpart compare equal even when one embeds, say, a ligature
    glyph the other spells out in full. Bracket-mirroring (that
    function's RTL-only correction) is deliberately never applied here:
    these extractors don't do pdfplumber's character-order handling
    this app's PDF/RTL pipeline exists to correct in the first place.
    """
    text = _normalize_word_text(text)
    tokens = text.split()
    x = x_start
    space_width = _char_width_pt(" ")
    for tok in tokens:
        tok_width = _text_width_pt(tok)
        if x + tok_width > x_max and x > x_start:
            x = x_start
            y += _DOCX_LINE_HEIGHT_PT
        words.append(
            Word(
                text=tok,
                page=page,
                x0=x,
                top=y,
                x1=x + tok_width,
                bottom=y + _DOCX_LINE_HEIGHT_PT,
            )
        )
        x += tok_width + space_width
    return y + _DOCX_LINE_HEIGHT_PT


# ---------------------------------------------------------------------------
# PPTX
# ---------------------------------------------------------------------------


def _extract_pptx_words(path: str) -> list[Word]:
    try:
        from pptx import Presentation
    except ImportError as exc:
        raise RuntimeError(
            "Reading .pptx files needs the optional 'python-pptx' package. "
            "Install it with:\n    pip install python-pptx"
        ) from exc

    try:
        presentation = Presentation(path)
    except Exception as exc:
        raise ValueError(f"Could not open '{path}' as a PowerPoint file: {exc}") from exc

    words: list[Word] = []
    for slide_number, slide in enumerate(presentation.slides, start=1):
        # Shapes in a slide's own XML order approximate the visual
        # reading order well enough for word-level diffing (top-level
        # title/body placeholders are typically added before free
        # shapes) -- there is no more authoritative "reading order"
        # exposed by python-pptx.
        for shape in slide.shapes:
            if shape.has_table:
                _place_pptx_table(words, shape, slide_number)
            elif shape.has_text_frame:
                _place_pptx_text_frame(words, shape, slide_number)
    return words


def _emu_to_pt(emu) -> float:
    return (emu or 0) / EMU_PER_INCH * POINTS_PER_INCH


def _place_pptx_text_frame(words: list[Word], shape, slide_number: int) -> None:
    left = _emu_to_pt(shape.left)
    top = _emu_to_pt(shape.top)
    width = _emu_to_pt(shape.width) or 400.0
    line_height = 14.0
    y = top
    for paragraph in shape.text_frame.paragraphs:
        text = _pptx_paragraph_text(paragraph)
        if text.strip():
            y = _place_line(words, text, left, y, left + width, page=slide_number)
        else:
            y += line_height


def _pptx_paragraph_text(paragraph) -> str:
    """A slide paragraph's text with superscript/subscript runs kept
    visible. PowerPoint marks them with a ``baseline`` offset on the
    run properties (positive = raised), which python-pptx doesn't
    expose directly."""
    runs = []
    for run in paragraph.runs:
        rpr = run._r.rPr
        offset = int(rpr.get("baseline", 0)) if rpr is not None else 0
        kind = SUPERSCRIPT if offset > 0 else SUBSCRIPT if offset < 0 else None
        runs.append((run.text, kind))
    return join_runs(runs) or paragraph.text


def _place_pptx_table(words: list[Word], shape, slide_number: int) -> None:
    table = shape.table
    left = _emu_to_pt(shape.left)
    top = _emu_to_pt(shape.top)
    width = _emu_to_pt(shape.width) or 400.0
    col_count = max(len(table.columns), 1)
    col_width = width / col_count
    row_height = 16.0
    for row_idx, row in enumerate(table.rows):
        for col_idx, cell in enumerate(row.cells):
            text = "\n".join(_pptx_paragraph_text(p) for p in cell.text_frame.paragraphs)
            if not text.strip():
                continue
            x = left + col_idx * col_width
            y = top + row_idx * row_height
            _place_line(words, text, x + 2, y, x + col_width - 2, page=slide_number)


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------

_XLSX_COL_WIDTH_PT = 64.0
_XLSX_ROW_HEIGHT_PT = 14.0


def _extract_xlsx_words(path: str) -> list[Word]:
    try:
        import openpyxl
    except ImportError as exc:
        raise RuntimeError(
            "Reading .xlsx files needs the optional 'openpyxl' package. "
            "Install it with:\n    pip install openpyxl"
        ) from exc

    try:
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise ValueError(f"Could not open '{path}' as an Excel workbook: {exc}") from exc

    words: list[Word] = []
    try:
        for sheet_number, worksheet in enumerate(workbook.worksheets, start=1):
            for row in worksheet.iter_rows():
                for cell in row:
                    if cell.value is None:
                        continue
                    text = str(cell.value)
                    if not text.strip():
                        continue
                    # Whole-cell superscript/subscript formatting. (Rich
                    # text inside one cell isn't available in read-only mode.)
                    vert_align = getattr(cell.font, "vertAlign", None)
                    if vert_align in ("superscript", "subscript"):
                        text = join_runs(
                            [(text, SUPERSCRIPT if vert_align == "superscript" else SUBSCRIPT)]
                        )
                    x0 = _DOCX_MARGIN_PT + (cell.column - 1) * _XLSX_COL_WIDTH_PT
                    top = _DOCX_MARGIN_PT + (cell.row - 1) * _XLSX_ROW_HEIGHT_PT
                    _place_line(
                        words,
                        text,
                        x0,
                        top,
                        x0 + _XLSX_COL_WIDTH_PT - 2,
                        page=sheet_number,
                    )
    finally:
        workbook.close()
    return words
