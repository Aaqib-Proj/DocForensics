"""Sanity check for core/pdf_extractor.py + core/diff_engine.py.

Builds synthetic PDFs with PyMuPDF and asserts:
  TEST 1: reflow into a narrower single column produces ZERO diffs
          (the original MVP-3 bug).
  TEST 2: every deliberate real difference layered on top of a reflow
          IS caught, with the right kind/severity.
  TEST 3: a 3-column layout is read in column-major order (top-to-
          bottom within each column, columns left-to-right) instead
          of interleaving columns row-by-row -- the MVP-4 known
          limitation this milestone (MVP-5) fixes.
  TEST 4: the severity ruleset's priority ladder -- a dosage/digit
          change is always "critical" even though a generic edit-
          distance check might call "5"/"10" "similar", while a
          dropped-letter/stray-space typo is downgraded to "typo" so
          it doesn't drown out the dosage change.
  TEST 5: a ruled table dominating the page is read cell-by-cell in
          row-major order (not sliced into page-level column bands),
          the two renderings diff as ZERO, and the cross-cell hyphen
          "double-" / "When" is NOT stitched into a phantom
          "doubleWhen" token -- the MVP-7 table-awareness fix.
  TEST 6: extraction-time Unicode normalization (NBSP, zero-width,
          soft hyphen, ligatures) and the hyphen-stitch rules (body
          text still stitches; table cells never do).
  TEST 8: out-of-order section matching -- a section moved to the
          front is matched where it stands (no row, no removed/added
          noise), while genuine edits inside it stay critical;
          identical / deleted / header-less documents behave as before.
  TEST 10: the core/section_diff.py pipeline -- extract_reading_order
          reads a 3-column page column-major, build_section_map chunks
          by numbered headers (incl. subsections), and
          diff_section_maps diffs strictly by section key with
          diff-match-patch: moves pair by key (no cascade), edits
          surface as one text_changed, missing sections as a single
          deleted/inserted entry, and wrap-artifact keys still pair.
  TEST 11: RTL bidi fixes -- reversing an RTL word's characters must
          NOT corrupt an embedded LTR/digit run glued to it with no
          space (a real observed case: a merged word turned "MIDC"
          into "CDIM" and would turn "210" into "012"), and
          build_comparison_tokens must re-stitch a numeric/code value
          that pdfplumber split into multiple word-fragments purely
          from a font/kerning difference (a real observed case: the
          SAME batch code came back as one word in one PDF but as
          "0" + "-31" in another) -- while never merging two genuine
          words or two numbers separated by a real gap.
  TEST 12: adaptive column-gutter width -- a real observed case: a
          compact folded leaflet using a small body font had a
          genuine, unambiguous column gutter only 10pt wide (normal
          word spacing on that page was under 3pt), which the old flat
          12pt minimum -- tuned for a different document's larger font
          -- rejected, collapsing the page to row-major order and
          interleaving two unrelated side-by-side sections word-by-
          word. The gutter-width minimum now scales with the page's
          OWN measured word spacing instead of one fixed constant.
  TEST 13: a PDF whose MediaBox origin isn't (0, 0) -- a real observed
          case: professionally-produced packaging artwork with a bleed
          box origin of (-9, -9) -- must have its extracted word
          coordinates normalized to match how PyMuPDF renders the
          page, otherwise every highlight lands offset from the actual
          text by that origin amount.
  TEST 14: unnumbered section headers (documents that don't follow
          FDA/EMA-style numbering, e.g. a credit-card Cardmember
          Agreement's "CARDMEMBER AGREEMENT" / "INTEREST CHARGES" /
          "ARBITRATION") get the SAME out-of-order, title-anchored
          section matching TEST 8/9 already lock in for NUMBERED
          headers -- content relocated by a different print layout
          (a real observed case: two unrelated documents combined onto
          one oversized proof sheet, read in the wrong relative order)
          is matched by section identity, not stream position. Also
          locks in the false-positive guard: a document's own
          multi-line title/caption ("FULL PRESCRIBING" / "INFORMATION"
          wrapped across two lines) must never be split into spurious
          sections, and a document control code ("CM.TL24K.LIN.0215")
          must never be mistaken for a header OR block a real one
          immediately after it.
  TEST 15: per-zone column detection (a real observed case: a single-
          column clause, a 2-column glossary, and a 3-column
          arbitration clause all on ONE page -- a real, completely
          clean narrow gutter in one zone was invisible when gutter
          detection ran once for the whole page, because unrelated
          single-column content elsewhere kept "crossing" that same
          x-range) and the column-flow/row-major distinction (a
          borderless "label: value" table -- asymmetric column widths
          -- must be read row-major, each row's own label with its own
          value, not column-major like a genuine equal-width
          multi-column section).
  TEST 16: MVP-21 regressions in MVP-20's own zone-splitting, found on
          two real documents reported the same day -- (1) a numbered
          list item ("1. Within 30 days...") or a bare dose number
          ("150 mg") must NOT be treated as a zone-boundary header
          just because the line starts with a small number: only a
          bare number ALONE on its line, or one followed by an actual
          caps-styled title, counts; (2) once 3+ real column bands are
          detected, one narrow band (e.g. a bullet-marker strip) must
          not collapse the whole zone to row-major -- that width-ratio
          check only applies to the true 2-band label/value case.
  TEST 17: MVP-22 -- three more real-document regressions found the
          SAME day as MVP-21, each surfaced by continuing to test
          against the actual reported files rather than declaring the
          fix done after synthetic tests passed. (1) A label/value
          record whose value cell itself wraps across two independent
          sub-columns (e.g. "for Purchases" terms next to a parallel
          "for Cash Advances" run) must read one sub-column fully, top
          to bottom, then the other -- a flat per-line read splices
          the tail of one record's sub-column into the next record's
          label whenever the two sub-columns wrap to a different
          number of lines. (2) A genuinely multi-column RTL zone must
          read its RIGHTMOST column first, not its leftmost -- every
          multi-column reading-order path before this was built LTR-
          first and never reversed for `rtl=True`. (3) A "table" that
          `find_tables` detects from incidental ruling/border lines
          around what is really ONE flowing paragraph (every row
          populates exactly one column, never two at once -- the
          opposite of what a real table's rows do) must be rejected
          and its words handled as ordinary body text instead.

Run with:
    python dev_tests/test_extraction_diff_smoke.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fitz  # PyMuPDF -- only used here to *build* test PDFs
import pdfplumber  # only used here to independently verify the raw (pre-normalization) coordinates

from core.diff_engine import (
    DiffItem,
    _location_of,
    _match_sections,
    _split_sections,
    diff_documents,
)
from core.pdf_extractor import (
    MIN_COLUMN_GUTTER_WIDTH,
    Word,
    _adaptive_min_gutter,
    _bands_are_column_flow,
    _consistent_internal_gutter_x,
    _detect_column_bands,
    _detect_page_tables,
    _fix_rtl_word_chars,
    _looks_like_real_table,
    _normalize_word_text,
    _order_body_units,
    _order_label_value_zone,
    _sort_line_words,
    _text_is_rtl,
    build_comparison_tokens,
    extract_words,
)
from core.section_diff import (
    build_section_map,
    diff_section_maps,
    extract_reading_order,
    is_likely_header_line,
)

APPROVED_TEXT = (
    "This document describes the recommended dosage of pharmacokinetics "
    "medication for adult patients. The patient should take one tablet "
    "twice daily with food. Do not exceed the maximum recommended dose."
)

# Same wording, but we will render it into a much narrower column so it
# wraps completely differently -- this is the "concise/resized pdf"
# scenario from the bug report.
SUSPECT_TEXT_REFLOWED_ONLY = APPROVED_TEXT

# A second suspect with real, meaningful differences layered on top of
# the reflow: a deleted word, an inserted word, a case change, and a
# genuine wording change.
SUSPECT_TEXT_WITH_REAL_DIFFS = (
    "This document describes the recommended dosage of pharmacokinetics "
    "medication for adult patients. The patient should take ONE tablet "
    "daily with food. Do not exceed the maximum recommended daily dose."
)
# vs approved, three isolated changes:
#   "twice daily" -> "daily"           (word removed: "twice")
#   "one" -> "ONE"                     (case-only change)
#   "maximum recommended dose" -> "maximum recommended daily dose" (word inserted)

# Longer passage (enough words to fill 3 columns meaningfully) used for the
# column-reading-order test. Rendered once as a plain single column, once
# split across 3 side-by-side columns -- identical wording either way, so
# any diff between them is purely a reading-order bug.
THREE_COLUMN_SOURCE_TEXT = (
    "Zolpidem tartrate tablets are indicated for the short term treatment "
    "of insomnia characterized by difficulties with sleep initiation. "
    "The recommended dose for adults is ten milligrams once daily "
    "immediately before bedtime. The total dose should not exceed ten "
    "milligrams per day. Elderly or debilitated patients may be especially "
    "sensitive to the effects of zolpidem tartrate. Patients with hepatic "
    "insufficiency do not clear the drug as rapidly as normal subjects. "
    "The recommended dose in both of these patient populations is five "
    "milligrams once daily immediately before bedtime. Dosage adjustment "
    "may be necessary when this medication is combined with other central "
    "nervous system depressant drugs because of the potentially additive "
    "effects. The effect of these tablets may be slowed by ingestion with "
    "or immediately after a meal. Tablets are available in five milligram "
    "and ten milligram strengths for oral administration."
)

# A minimal pair used to isolate the severity ladder: ONE dosage/digit
# change ("5 mg" -> "10 mg", must be "critical") and ONE dropped-letter/
# stray-space typo ("especially" -> "especial y", must be downgraded to
# "typo") in an otherwise-identical sentence, so nothing else can muddy
# which severity each individual change was actually given.
DOSAGE_APPROVED_TEXT = (
    "The recommended dose in elderly patients is 5 mg once daily. "
    "Elderly or debilitated patients may be especially sensitive to "
    "the effects of this medication."
)
DOSAGE_SUSPECT_TEXT = (
    "The recommended dose in elderly patients is 10 mg once daily. "
    "Elderly or debilitated patients may be especial y sensitive to "
    "the effects of this medication."
)

# A synthetic "adverse reactions" table, dominated by its own internal
# cell gutters, with the exact cross-cell hyphen pattern from the bug
# report: "double-" ends one row's last cell and "When" (here
# "Fatigue") starts the next row's first cell. Before MVP-7 the page-
# level column bands sliced straight through the table (reading it
# column-major) and the hyphen was stitched across cells into a
# phantom "doubleWhen"-style token.
TABLE_CELLS = [
    ["MedDRA SOC", "N", "n", "When"],
    ["Dizziness", "11", "5", "When"],
    ["Headache", "8", "4", "When"],
    ["Cough", "3", "2", "double-"],
    ["Fatigue", "6", "3", "When"],
    ["Nausea", "9", "6", "When"],
]

TABLE_HEADING = "Adverse reactions summary:"

# The reading order the fix must produce: heading first, then the
# table row-major (row by row, cell left-to-right, words within a
# cell in reading order), with "double-" kept standalone.
TABLE_EXPECTED_TOKENS = [
    "Adverse",
    "reactions",
    "summary:",
    "MedDRA",
    "SOC",
    "N",
    "n",
    "When",
    "Dizziness",
    "11",
    "5",
    "When",
    "Headache",
    "8",
    "4",
    "When",
    "Cough",
    "3",
    "2",
    "double-",
    "Fatigue",
    "6",
    "3",
    "When",
    "Nausea",
    "9",
    "6",
    "When",
]


def make_pdf(path: str, text: str, page_width: float, page_height: float = 300) -> None:
    """Render `text` into a single page of the given width, word-wrapped."""
    doc = fitz.open()
    page = doc.new_page(width=page_width, height=page_height)
    rect = fitz.Rect(36, 36, page_width - 36, page_height - 36)
    page.insert_textbox(rect, text, fontsize=11, fontname="helv")
    doc.save(path)
    doc.close()


def make_pdf_with_mediabox_offset(
    path: str, text: str, page_width: float = 400, page_height: float = 300
) -> None:
    """A single-page PDF whose MediaBox origin is NOT (0, 0) -- like a
    real print/packaging artwork PDF's bleed box -- to test that
    extracted word coordinates get normalized to match how PyMuPDF
    renders the page (see MVP-18's `extract_words` fix)."""
    doc = fitz.open()
    page = doc.new_page(width=page_width, height=page_height)
    rect = fitz.Rect(36, 36, page_width - 36, page_height - 36)
    page.insert_textbox(rect, text, fontsize=11, fontname="helv")
    page.set_mediabox(fitz.Rect(-20, -30, page_width - 20, page_height - 30))
    doc.save(path)
    doc.close()


def make_table_pdf(
    path: str,
    page_width: float,
    table_x0: float,
    column_widths: list[float],
    row_height: float,
    table_y: float = 56,
    page_height: float = 320,
) -> None:
    """One page with a short heading above a fully ruled synthetic table.

    Draws real ruling lines (pdfplumber's "lines" table strategy picks
    them up) so the table is detected structurally, then places one
    word per cell with PyMuPDF textboxes.
    """
    doc = fitz.open()
    page = doc.new_page(width=page_width, height=page_height)
    page.insert_textbox(
        fitz.Rect(30, 24, page_width - 30, 50),
        TABLE_HEADING,
        fontsize=11,
        fontname="helv",
    )
    n_rows = len(TABLE_CELLS)
    table_width = sum(column_widths)
    for r in range(n_rows + 1):
        y = table_y + r * row_height
        page.draw_line(
            fitz.Point(table_x0, y), fitz.Point(table_x0 + table_width, y)
        )
    x = table_x0
    for i in range(len(column_widths) + 1):
        page.draw_line(
            fitz.Point(x, table_y), fitz.Point(x, table_y + n_rows * row_height)
        )
        if i < len(column_widths):
            x += column_widths[i]
    for r, row in enumerate(TABLE_CELLS):
        xx = table_x0 + 3
        for c, cell_text in enumerate(row):
            page.insert_textbox(
                fitz.Rect(
                    xx,
                    table_y + r * row_height + 1,
                    xx + column_widths[c] - 3,
                    table_y + (r + 1) * row_height - 1,
                ),
                cell_text,
                fontsize=8,
                fontname="helv",
            )
            xx += column_widths[c]
    doc.save(path)
    doc.close()


def make_multicolumn_pdf(
    path: str,
    text: str,
    page_width: float = 612,
    page_height: float = 500,
    n_columns: int = 3,
    margin: float = 36,
    gutter: float = 24,
    fontsize: float = 11,
) -> None:
    """Render `text` split evenly (by word count) across `n_columns` side-by-side
    column rects on one page -- a synthetic stand-in for a real multi-column
    layout like an FDA drug label, so column-aware reading order can be
    tested without needing a real sample PDF on disk.
    """
    words = text.split()
    chunk_size = max(1, -(-len(words) // n_columns))  # ceil division
    chunks = [
        " ".join(words[i : i + chunk_size]) for i in range(0, len(words), chunk_size)
    ]
    # Pad to exactly n_columns chunks (harmless empty columns if text is short).
    while len(chunks) < n_columns:
        chunks.append("")

    usable_width = page_width - 2 * margin - gutter * (n_columns - 1)
    column_width = usable_width / n_columns

    doc = fitz.open()
    page = doc.new_page(width=page_width, height=page_height)
    for i, chunk in enumerate(chunks[:n_columns]):
        x0 = margin + i * (column_width + gutter)
        rect = fitz.Rect(x0, margin, x0 + column_width, page_height - margin)
        page.insert_textbox(rect, chunk, fontsize=fontsize, fontname="helv")
    doc.save(path)
    doc.close()


def make_sectioned_pdf(
    path: str,
    sections: list[tuple[str, str, str]],
    edit: tuple[str, tuple[str, str]] | None = None,
    drop: str | None = None,
) -> None:
    """A single-column page with numbered all-caps section headers, one
    per `sections` entry (number, title, body), stacked top to bottom.
    `edit` replaces a phrase inside the given title's body; `drop`
    removes that section entirely."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=900)
    y = 40.0
    for num, title, body in sections:
        if drop == title:
            continue
        page.insert_text(
            fitz.Point(36, y), f"{num} {title}", fontsize=11, fontname="helv"
        )
        y += 16
        text = body
        if edit and edit[0] == title:
            text = body.replace(*edit[1])
        page.insert_textbox(
            fitz.Rect(36, y, 576, y + 130), text, fontsize=10, fontname="helv"
        )
        y += 145
    doc.save(path)
    doc.close()


# Four canonical FDA-style sections. Numbers stay STABLE across docs
# (a real label keeps "3 WARNINGS AND PRECAUTIONS" even when the
# section moves) so a move never looks like renumbering.
SECTIONS_4 = [
    (
        "1",
        "INDICATIONS AND USAGE",
        "Zolpidem tartrate tablets are indicated for the short term "
        "treatment of insomnia characterized by difficulties with sleep "
        "initiation.",
    ),
    (
        "2",
        "DOSAGE AND ADMINISTRATION",
        "The recommended dose for adults is ten milligrams once daily "
        "immediately before bedtime. The total dose should not exceed "
        "ten milligrams per day.",
    ),
    (
        "3",
        "WARNINGS AND PRECAUTIONS",
        "Elderly or debilitated patients may be especially sensitive to "
        "the effects of zolpidem tartrate. Patients with hepatic "
        "impairment should use a lower dose.",
    ),
    (
        "4",
        "ADVERSE REACTIONS",
        "The most common adverse reactions include drowsiness dizziness "
        "and diarrhea. Clinical trials reported headache and nausea in "
        "a small proportion of patients.",
    ),
]


def run_tokens(words_a, words_b):
    return diff_documents(build_comparison_tokens(words_a), build_comparison_tokens(words_b))


def run_pipeline(path_a: str, path_b: str):
    words_a = extract_words(path_a)
    words_b = extract_words(path_b)
    tokens_a = build_comparison_tokens(words_a)
    tokens_b = build_comparison_tokens(words_b)
    return diff_documents(tokens_a, tokens_b)


def make_script_pdf(path: str, runs_per_line: list[list[tuple[str, float, float]]]) -> None:
    """One line per entry; each run is (text, fontsize, baseline rise in
    points, positive = raised). Runs are placed touching each other, as
    a word processor sets a superscript/subscript glued to its base."""
    doc = fitz.open()
    page = doc.new_page(width=400, height=300)
    y = 60
    for runs in runs_per_line:
        x = 40.0
        for text, size, rise in runs:
            page.insert_text((x, y - rise), text, fontsize=size, fontname="helv")
            x += fitz.get_text_length(text, fontname="helv", fontsize=size)
        y += 24
    doc.save(path)
    doc.close()


def make_script_docx(path: str) -> None:
    import docx

    document = docx.Document()
    paragraph = document.add_paragraph("Take ")
    paragraph.add_run("2")
    paragraph.add_run("4").font.superscript = True
    paragraph.add_run(" tablets with H")
    paragraph.add_run("2").font.subscript = True
    paragraph.add_run("O daily.")
    document.save(path)


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="docforensics_smoke_")
    approved_path = os.path.join(tmp, "approved.pdf")
    reflow_only_path = os.path.join(tmp, "suspect_reflow_only.pdf")
    real_diffs_path = os.path.join(tmp, "suspect_real_diffs.pdf")

    # Wide column for the approved doc, narrow column for the suspects
    # -- forces different line-wrap points and word spacing.
    make_pdf(approved_path, APPROVED_TEXT, page_width=550)
    make_pdf(reflow_only_path, SUSPECT_TEXT_REFLOWED_ONLY, page_width=260)
    make_pdf(real_diffs_path, SUSPECT_TEXT_WITH_REAL_DIFFS, page_width=260)

    print("=" * 70)
    print("TEST 1: identical wording, different layout -> expect 0 diffs")
    print("=" * 70)
    reflow_diffs = run_pipeline(approved_path, reflow_only_path)
    for d in reflow_diffs:
        print(f"  UNEXPECTED DIFF: {d.kind} | {d.approved_text!r} -> {d.suspect_text!r}")

    if reflow_diffs:
        print(f"FAIL: reflow alone produced {len(reflow_diffs)} false-positive diff(s).")
    else:
        print("PASS: reflow + uneven spacing produced no false positives.")

    print()
    print("=" * 70)
    print("TEST 2: real differences layered on top of reflow -> expect catches")
    print("=" * 70)
    real_diffs = run_pipeline(approved_path, real_diffs_path)
    for d in real_diffs:
        loc_a = d.approved_location
        loc_b = d.suspect_location
        print(f"  [{d.severity:8s}] {d.kind:18s} {d.approved_text!r} -> {d.suspect_text!r}")
        print(f"             approved bbox: {loc_a}    suspect bbox: {loc_b}")

    expected_kinds = {"removed_in_suspect", "text_changed", "added_in_suspect"}
    found_kinds = {d.kind for d in real_diffs}
    missing = expected_kinds - found_kinds
    if missing:
        print(f"FAIL: expected diff kinds {expected_kinds}, missing {missing}.")
    else:
        print(f"PASS: all expected diff categories were caught ({len(real_diffs)} total).")

    has_case_only = any(d.severity == "major" for d in real_diffs)
    if not has_case_only:
        print("FAIL: case-only change ('one' -> 'ONE') was not flagged as 'major'.")
    else:
        print("PASS: case-only change correctly downgraded to 'major' (not 'critical').")

    print()
    print("=" * 70)
    print("TEST 3: 3-column layout -> expect column-major reading order")
    print("=" * 70)
    single_col_path = os.path.join(tmp, "single_column.pdf")
    three_col_path = os.path.join(tmp, "three_column.pdf")
    make_pdf(single_col_path, THREE_COLUMN_SOURCE_TEXT, page_width=550, page_height=500)
    make_multicolumn_pdf(three_col_path, THREE_COLUMN_SOURCE_TEXT, n_columns=3)

    single_col_diffs = run_pipeline(single_col_path, three_col_path)
    if single_col_diffs:
        print(
            f"FAIL: identical wording, single-column vs 3-column, produced "
            f"{len(single_col_diffs)} false-positive diff(s) -- columns were "
            f"read out of order:"
        )
        for d in single_col_diffs[:10]:
            print(f"    {d.kind}: {d.approved_text!r} -> {d.suspect_text!r}")
    else:
        print("PASS: 3-column layout was read in the correct column-major order.")

    print()
    print("=" * 70)
    print("TEST 4: severity priority ladder -- dosage digit change vs. typo")
    print("=" * 70)
    dosage_approved_path = os.path.join(tmp, "dosage_approved.pdf")
    dosage_suspect_path = os.path.join(tmp, "dosage_suspect.pdf")
    make_pdf(dosage_approved_path, DOSAGE_APPROVED_TEXT, page_width=550)
    make_pdf(dosage_suspect_path, DOSAGE_SUSPECT_TEXT, page_width=550)

    dosage_diffs = run_pipeline(dosage_approved_path, dosage_suspect_path)
    for d in dosage_diffs:
        print(f"  [{d.severity:8s}] {d.kind:18s} {d.approved_text!r} -> {d.suspect_text!r}")

    dosage_item = next((d for d in dosage_diffs if "5" in d.approved_text.split()), None)
    typo_item = next((d for d in dosage_diffs if "especial" in d.approved_text.casefold()), None)

    if dosage_item is None or dosage_item.severity != "critical":
        print("FAIL: the '5 mg' -> '10 mg' dosage change was not flagged 'critical'.")
    else:
        print("PASS: dosage/digit change correctly flagged 'critical'.")

    if typo_item is None or typo_item.severity != "typo":
        print("FAIL: the 'especially' -> 'especial y' typo was not downgraded to 'typo'.")
    else:
        print("PASS: dropped-letter/stray-space typo correctly downgraded to 'typo'.")

    print()
    print("=" * 70)
    print("TEST 5: ruled table read cell-by-cell, row-major, ZERO diffs")
    print("        (cross-cell hyphen 'double-' / 'When' must not stitch)")
    print("=" * 70)
    table_approved_path = os.path.join(tmp, "table_approved.pdf")
    table_suspect_path = os.path.join(tmp, "table_suspect.pdf")
    # Identical table content, deliberately different geometry: page
    # width, table x-offset, column widths and row height all change,
    # so every word lands at a different (x, y) -- exactly the
    # "visually identical, structurally different" scenario.
    make_table_pdf(table_approved_path, page_width=420, table_x0=40,
                   column_widths=[150, 36, 36, 80], row_height=22)
    make_table_pdf(table_suspect_path, page_width=380, table_x0=30,
                   column_widths=[110, 30, 30, 60], row_height=18, table_y=60)

    approved_tokens = build_comparison_tokens(extract_words(table_approved_path))
    suspect_tokens = build_comparison_tokens(extract_words(table_suspect_path))
    approved_texts = [t.text for t in approved_tokens]
    suspect_texts = [t.text for t in suspect_tokens]

    if approved_texts != TABLE_EXPECTED_TOKENS:
        print("FAIL: approved table was not read cell-by-cell, row-major.")
        print(f"       expected: {TABLE_EXPECTED_TOKENS}")
        print(f"       got:      {approved_texts}")
        ok5 = False
    else:
        ok5 = True

    if "doubleWhen" in approved_texts or "doubleWhen" in suspect_texts:
        print("FAIL: phantom 'doubleWhen' token -- hyphen stitched across cells.")
        ok5 = False
    else:
        print("PASS: no phantom cross-cell hyphen token on either side.")

    if "double-" not in approved_texts or "double-" not in suspect_texts:
        print("FAIL: 'double-' was swallowed by a hyphen stitch.")
        ok5 = False
    else:
        print("PASS: 'double-' survives as a standalone token on both sides.")

    table_diffs = diff_documents(approved_tokens, suspect_tokens)
    if table_diffs:
        print(
            f"FAIL: identical table content in two geometries produced "
            f"{len(table_diffs)} false-positive diff(s):"
        )
        for d in table_diffs[:10]:
            print(f"    {d.kind}: {d.approved_text!r} -> {d.suspect_text!r}")
        ok5 = False
    else:
        print("PASS: table reflow produced no false positives.")

    if ok5:
        print("PASS: table-aware reading order + cross-cell hyphen guard.")

    print()
    print("=" * 70)
    print("TEST 6: Unicode normalization + hyphen-stitch rules")
    print("=" * 70)
    ok6 = True

    norm_cases = {
        "5\u00a0mg": "5 mg",  # non-breaking space -> plain space
        "\ufb01ne": "fine",  # ligature ﬁ -> fi
        "\ufb03culty": "fficulty",  # ligature ﬃ -> ffi
        "ab\u200bcd": "abcd",  # zero-width space dropped
        "\u00ad": "",  # soft hyphen dropped
        "cafe\u0301": "caf\u00e9",  # NFC: combining accent folded in
    }
    for raw, expected in norm_cases.items():
        got = _normalize_word_text(raw)
        if got != expected:
            print(f"FAIL: _normalize_word_text({raw!r}) = {got!r}, expected {expected!r}")
            ok6 = False
    if ok6:
        print("PASS: all normalization cases (NBSP/ligatures/zero-width/soft hyphen/NFC).")

    # Body text: a genuine line-end hyphen split still stitches back.
    # (Two words on different visual lines: page 1, tops 10 vs 30.)
    body_stitch = build_comparison_tokens(
        [
            Word(text="pharmaco-", page=1, x0=0.0, top=10.0, x1=40.0, bottom=20.0),
            Word(text="kinetics", page=1, x0=0.0, top=30.0, x1=40.0, bottom=40.0),
        ]
    )
    if len(body_stitch) != 1 or body_stitch[0].text != "pharmacokinetics":
        print("FAIL: body-text hyphen split was not stitched back together.")
        ok6 = False
    else:
        print("PASS: body-text hyphen split still stitches (regression check).")

    # Table cells: 'double-' at end of one cell and 'When' at the start
    # of the next row's cell must stay two separate tokens.
    cross_cell = build_comparison_tokens(
        [
            Word(text="double-", page=1, x0=0.0, top=10.0, x1=40.0, bottom=20.0,
                 cell_id=(0, 3, 3)),
            Word(text="Fatigue", page=1, x0=0.0, top=30.0, x1=40.0, bottom=40.0,
                 cell_id=(0, 4, 0)),
        ]
    )
    if len(cross_cell) != 2 or cross_cell[0].text != "double-" or cross_cell[1].text != "Fatigue":
        print(f"FAIL: cross-cell hyphen was stitched: {[t.text for t in cross_cell]}")
        ok6 = False
    else:
        print("PASS: hyphen never stitches across table cell boundaries.")

    # No table at all (cell_id None on both sides, as when a
    # borderless table is not detected): "double-" followed by a
    # capitalized "When" must STILL not stitch -- the text-based guard
    # is layout-independent.
    no_table = build_comparison_tokens(
        [
            Word(text="double-", page=1, x0=0.0, top=10.0, x1=40.0, bottom=20.0),
            Word(text="When", page=1, x0=0.0, top=30.0, x1=40.0, bottom=40.0),
        ]
    )
    if len(no_table) != 2 or no_table[0].text != "double-":
        print(
            f"FAIL: phantom stitch without a detected table: "
            f"{[t.text for t in no_table]}"
        )
        ok6 = False
    else:
        print("PASS: no phantom stitch even without a detected table (layout-independent).")

    # Same cell, wrapped onto a second visual line: a legit split still
    # stitches (a hyphenated word continuing inside one cell).
    in_cell = build_comparison_tokens(
        [
            Word(text="pharmaco-", page=1, x0=0.0, top=10.0, x1=40.0, bottom=20.0,
                 cell_id=(0, 1, 0)),
            Word(text="kinetics", page=1, x0=0.0, top=30.0, x1=40.0, bottom=40.0,
                 cell_id=(0, 1, 0)),
        ]
    )
    if len(in_cell) != 1 or in_cell[0].text != "pharmacokinetics":
        print("FAIL: same-cell hyphen split was not stitched back together.")
        ok6 = False
    else:
        print("PASS: hyphen stitches normally within one cell.")

    print()
    print("=" * 70)
    print("TEST 7: identical content in a different stream position")
    print("        -> matched where it stands: not a row at all")
    print("=" * 70)
    ok7 = True

    def w(text: str, top: float) -> Word:
        return Word(text=text, page=1, x0=0.0, top=top, x1=10.0, bottom=top + 10.0)

    # 'epsilon' exists once on each side, just at a different position
    # (a wrap/order artifact). Moving is not a difference: the two copies
    # are paired with each other (matched), and nothing is reported.
    reorder_approved = build_comparison_tokens(
        [w(t, i * 20.0) for i, t in enumerate(["alpha", "beta", "gamma", "delta", "epsilon"])]
    )
    reorder_suspect = build_comparison_tokens(
        [w(t, i * 20.0) for i, t in enumerate(["alpha", "beta", "epsilon", "gamma", "delta"])]
    )
    pairs7: list = []
    reorder_diffs = diff_documents(reorder_approved, reorder_suspect, pairs7)
    epsilon_paired = ("epsilon", "epsilon") in [(a.text, b.text) for a, b in pairs7]
    if reorder_diffs or not epsilon_paired:
        print(f"FAIL: moved 'epsilon' was reported or left unpaired: "
              f"{[(d.kind, d.approved_text, d.suspect_text) for d in reorder_diffs]}, paired {epsilon_paired}")
        ok7 = False
    else:
        print("PASS: identical text in a different position is matched, not reported.")

    # Control: a genuine one-word change must stay critical (never
    # demoted by the pairing pass).
    real_approved = build_comparison_tokens([w(t, i * 20.0) for i, t in enumerate(["x", "y", "z"])])
    real_suspect = build_comparison_tokens([w(t, i * 20.0) for i, t in enumerate(["x", "q", "z"])])
    real_diffs = diff_documents(real_approved, real_suspect)
    real_item = next((d for d in real_diffs if d.kind == "text_changed"), None)
    if real_item is None or real_item.severity != "critical":
        print(f"FAIL: genuine 'y' -> 'q' change lost its critical severity: {[(d.kind, d.severity) for d in real_diffs]}")
        ok7 = False
    else:
        print("PASS: a genuine wording change stays 'critical' (control).")

    if ok7:
        print("PASS: moved text is matched; a real change is still critical.")

    print()
    print("=" * 70)
    print("TEST 8: out-of-order section matching")
    print("        -> a moved section is matched where it stands; only edits are rows")
    print("=" * 70)
    ok8 = True

    approved_sec = os.path.join(tmp, "t8_approved.pdf")
    moved_sec = os.path.join(tmp, "t8_moved.pdf")
    moved_edited_sec = os.path.join(tmp, "t8_moved_edited.pdf")
    deleted_sec = os.path.join(tmp, "t8_deleted.pdf")
    make_sectioned_pdf(approved_sec, SECTIONS_4)
    # WARNINGS moved to the FRONT, still numbered 3, with a genuine
    # one-word edit inside it -- the "concise leaflet" scenario.
    moved_order = [SECTIONS_4[2], SECTIONS_4[0], SECTIONS_4[1], SECTIONS_4[3]]
    make_sectioned_pdf(moved_sec, moved_order)
    make_sectioned_pdf(
        moved_edited_sec, moved_order,
        edit=("WARNINGS AND PRECAUTIONS", ("especially", "very")),
    )
    make_sectioned_pdf(
        deleted_sec, SECTIONS_4, drop="WARNINGS AND PRECAUTIONS"
    )

    # Scenario A: moved AND edited -> the edit stays critical and is the
    # ONLY row: the move itself is not a difference (no removed/added
    # noise, no "moved" row).
    moved_edited_diffs = run_pipeline(approved_sec, moved_edited_sec)
    criticals = [d for d in moved_edited_diffs if d.severity == "critical"]
    ok_a = (
        len(moved_edited_diffs) == 1
        and len(criticals) == 1
        and criticals[0].approved_text == "especially"
        and criticals[0].suspect_text == "very"
    )
    if ok_a:
        print("PASS: moved+edited section -> only the edit, critical.")
    else:
        print(
            f"FAIL: moved+edited section: "
            f"{[(d.kind, d.severity, d.approved_text) for d in moved_edited_diffs]}"
        )
        ok8 = False

    # Scenario B (control): identical docs, identical order -> 0 items.
    identical_diffs = run_pipeline(approved_sec, approved_sec)
    if identical_diffs:
        print(f"FAIL: identical sectioned docs produced diffs: {[(d.kind, d.approved_text) for d in identical_diffs]}")
        ok8 = False
    else:
        print("PASS: identical sectioned docs -> 0 diffs.")

    # Scenario C (control): pure move, no edit -> nothing to report.
    moved_diffs = run_pipeline(approved_sec, moved_sec)
    if not moved_diffs:
        print("PASS: pure section move -> 0 diffs.")
    else:
        print(f"FAIL: pure move: {[(d.kind, d.severity) for d in moved_diffs]}")
        ok8 = False

    # Scenario D (control): a section deleted outright -> one removed
    # item for the whole section, never a cascade.
    deleted_diffs = run_pipeline(approved_sec, deleted_sec)
    if (
        len(deleted_diffs) == 1
        and deleted_diffs[0].kind == "removed_in_suspect"
        and deleted_diffs[0].severity == "critical"
        and "WARNINGS AND PRECAUTIONS" in deleted_diffs[0].approved_text
    ):
        print("PASS: deleted section -> one critical removed item.")
    else:
        print(f"FAIL: deleted section: {[(d.kind, d.severity, d.approved_text[:30]) for d in deleted_diffs]}")
        ok8 = False

    # Scenario E (fallback): a header-less document takes the plain
    # whole-stream path (no section chunking engaged).
    headerless = build_comparison_tokens(extract_words(reflow_only_path))
    if _split_sections(headerless) is None:
        print("PASS: header-less docs fall back to the whole-stream diff.")
    else:
        print("FAIL: header-less doc was (wrongly) chunked into sections.")
        ok8 = False

    if ok8:
        print("PASS: out-of-order section matching.")

    print()
    print("=" * 70)
    print("TEST 9: wrapped headers + title-anchored matching + relocation")
    print("        -> layout-interleaved content is matched, not reported")
    print("=" * 70)
    ok9 = True

    def w(text: str, top: float) -> Word:
        return Word(text=text, page=1, x0=0.0, top=top, x1=10.0, bottom=top + 10.0)

    # Scenario A: the SAME headers, one document wrapping them across
    # lines (A4 leaflets: "1 INDICATIONS AND" / "USAGE", "4
    # CONTRAINDICATION" / "S") and the other setting them on one line.
    # Detection must recover the full titles and match every section.
    wrapped = [
        w("FULL", 10), w("PRESCRIBING", 10), w("INFORMATION", 24),
        w("1", 40), w("INDICATIONS", 40), w("AND", 54), w("USAGE", 68),
        w("alpha", 82), w("beta", 82), w("gamma", 82), w("delta", 82),
        w("2", 110), w("DOSAGE", 110), w("AND", 124), w("ADMINISTRATION", 138),
        w("epsilon", 152), w("zeta", 152), w("eta", 152), w("theta", 152),
        w("3", 180), w("DOSAGE", 180), w("FORMS", 194), w("AND", 208), w("STRENGTHS", 222),
        w("omicron", 236), w("pi", 236), w("rho", 236),
        w("4", 260), w("CONTRAINDICATION", 260), w("S", 274),
        w("iota", 288), w("kappa", 288), w("lambda", 288),
        w("5", 310), w("WARNINGS", 310), w("AND", 324), w("PRECAUTIONS", 338),
        w("mu", 352), w("nu", 352), w("xi", 352),
    ]
    flat = [
        w("FULL", 10), w("PRESCRIBING", 10), w("INFORMATION", 24),
        w("1", 40), w("INDICATIONS", 40), w("AND", 40), w("USAGE", 40),
        w("alpha", 82), w("beta", 82), w("gamma", 82), w("delta", 82),
        w("2", 110), w("DOSAGE", 110), w("AND", 110), w("ADMINISTRATION", 110),
        w("epsilon", 152), w("zeta", 152), w("eta", 152), w("theta", 152),
        w("3", 180), w("DOSAGE", 180), w("FORMS", 180), w("AND", 180), w("STRENGTHS", 180),
        w("omicron", 236), w("pi", 236), w("rho", 236),
        w("4", 260), w("CONTRAINDICATIONS", 260),
        w("iota", 288), w("kappa", 288), w("lambda", 288),
        w("5", 310), w("WARNINGS", 310), w("AND", 310), w("PRECAUTIONS", 310),
        w("mu", 352), w("nu", 352), w("xi", 352),
    ]
    ta_wrap = build_comparison_tokens(wrapped)
    ta_flat = build_comparison_tokens(flat)
    sections_wrap = _split_sections(ta_wrap)
    # [0] is the preamble (''); the 5 real section titles follow it.
    titles = [s.title for s in sections_wrap][1:] if sections_wrap else []
    expected_titles = [
        "1 INDICATIONS AND USAGE", "2 DOSAGE AND ADMINISTRATION",
        "3 DOSAGE FORMS AND STRENGTHS", "4 CONTRAINDICATION S",
        "5 WARNINGS AND PRECAUTIONS",
    ]
    wrap_diffs = diff_documents(ta_wrap, ta_flat)
    wrap_criticals = [d for d in wrap_diffs if d.severity == "critical"]
    ok_a = (
        sections_wrap is not None
        and len(sections_wrap) == 6  # preamble + 5 sections
        and titles == expected_titles
        and not wrap_criticals
    )
    if ok_a:
        print("PASS: wrapped multi-line headers recovered and matched (0 criticals).")
    else:
        print(f"FAIL: wrapped headers: titles={titles} diffs={[(d.kind, d.approved_text) for d in wrap_diffs]}")
        ok9 = False

    # Scenario B: the column-interleave pattern -- approved's stream
    # order is 1, 2, 4, 5, 3 (section 3's header printed after 4 and 5)
    # and section 2's tail ('eta theta iota kappa') is printed INSIDE
    # section 5's chunk. Title-anchored matching must still pair 2 with
    # 2 and 5 with 5, and the relocated tail must demote to 'reorder'
    # instead of appearing as critical removed/added noise.
    interleaved = [
        w("FULL", 10), w("PRESCRIBING", 10), w("INFORMATION", 24),
        w("1", 40), w("INDICATIONS", 40), w("AND", 40), w("USAGE", 40),
        w("alpha", 82), w("beta", 82), w("gamma", 82), w("delta", 82),
        w("2", 110), w("DOSAGE", 110), w("AND", 110), w("ADMINISTRATION", 110),
        w("epsilon", 152), w("zeta", 152),
        w("4", 200), w("CONTRAINDICATIONS", 200),
        w("iota", 214), w("kappa", 214), w("lambda", 214),
        w("5", 240), w("WARNINGS", 240), w("AND", 240), w("PRECAUTIONS", 240),
        w("mu", 254), w("nu", 254), w("xi", 254),
        w("eta", 268), w("theta", 268), w("iota", 268), w("kappa", 268),
        w("3", 300), w("DOSAGE", 300), w("FORMS", 300), w("AND", 300), w("STRENGTHS", 300),
        w("omicron", 314), w("pi", 314), w("rho", 314),
    ]
    ordered = [
        w("FULL", 10), w("PRESCRIBING", 10), w("INFORMATION", 24),
        w("1", 40), w("INDICATIONS", 40), w("AND", 40), w("USAGE", 40),
        w("alpha", 82), w("beta", 82), w("gamma", 82), w("delta", 82),
        w("2", 110), w("DOSAGE", 110), w("AND", 110), w("ADMINISTRATION", 110),
        w("epsilon", 152), w("zeta", 152), w("eta", 152), w("theta", 152),
        w("iota", 166), w("kappa", 166),
        w("3", 200), w("DOSAGE", 200), w("FORMS", 200), w("AND", 200), w("STRENGTHS", 200),
        w("omicron", 214), w("pi", 214), w("rho", 214),
        w("4", 240), w("CONTRAINDICATIONS", 240),
        w("iota", 254), w("kappa", 254), w("lambda", 254),
        w("5", 280), w("WARNINGS", 280), w("AND", 280), w("PRECAUTIONS", 280),
        w("mu", 294), w("nu", 294), w("xi", 294),
    ]
    ta_int = build_comparison_tokens(interleaved)
    ta_ord = build_comparison_tokens(ordered)
    b_to_a, _ua, _ub = _match_sections(
        _split_sections(ta_int), _split_sections(ta_ord)
    )
    tail_pairs: list = []
    tail_diffs = diff_documents(ta_int, ta_ord, tail_pairs)
    paired_tail = [a.text for a, b in tail_pairs if a.text == b.text and a.text in ("eta", "theta", "iota", "kappa")]
    ok_b = (
        b_to_a.get(2) == 2  # suspect 2 DOSAGE -> approved 2 DOSAGE, NOT 5
        and b_to_a.get(3) == 5  # suspect 3 DOSAGE -> approved 5 (3 DOSAGE, last in stream)
        and b_to_a.get(4) == 3  # suspect 4 -> approved 4 CONTRAINDICATION
        and b_to_a.get(5) == 4  # suspect 5 -> approved 5 WARNINGS
        and not tail_diffs  # the relocated tail is matched where it stands
        and sorted(paired_tail).count("iota") >= 1 and "eta" in paired_tail and "theta" in paired_tail
    )
    if ok_b:
        print("PASS: contaminated chunks still match 2<->2 / 5<->5; "
              "the relocated tail is matched, not reported.")
    else:
        print(f"FAIL: interleave: matches={b_to_a} rows={[(d.kind, d.approved_text, d.suspect_text) for d in tail_diffs]} "
              f"paired tail={paired_tail}")
        ok9 = False

    if ok9:
        print("PASS: wrapped headers + title-anchored matching + relocated text matched.")

    print()
    print("=" * 70)
    print("TEST 10: core/section_diff.py -- column order, section maps,")
    print("         keyed section diffing (diff-match-patch)")
    print("=" * 70)
    ok10 = True

    # 10a: extract_reading_order reads a 3-column page column-major.
    # Concatenating the blocks must reconstruct the source text in the
    # original word order -- any column interleaving breaks this.
    ro_blocks = extract_reading_order(three_col_path)
    ro_text = " ".join(b["text"] for b in ro_blocks)
    collapse = lambda s: " ".join(s.split())  # noqa: E731
    if collapse(ro_text) == collapse(THREE_COLUMN_SOURCE_TEXT):
        print("PASS: extract_reading_order: 3-column page read column-major.")
    else:
        print(
            f"FAIL: extract_reading_order scrambled column order:\n"
            f"  got: {collapse(ro_text)[:120]!r}"
        )
        ok10 = False

    # 10b: build_section_map chunks a sectioned page by header.
    sec_pdf = os.path.join(tmp, "t10_sections.pdf")
    make_sectioned_pdf(sec_pdf, SECTIONS_4)
    sec_map = build_section_map(extract_reading_order(sec_pdf))
    expected_keys = [f"{num} {title}" for num, title, _ in SECTIONS_4]
    if list(sec_map) == expected_keys:
        print("PASS: build_section_map: one section per numbered header, in order.")
    else:
        print(f"FAIL: build_section_map keys: {list(sec_map)} != {expected_keys}")
        ok10 = False
    dosage_blocks = sec_map.get("2 DOSAGE AND ADMINISTRATION", [])
    if dosage_blocks and any(
        "recommended dose" in b["text"].casefold() for b in dosage_blocks
    ):
        print("PASS: build_section_map: body blocks grouped under their header.")
    else:
        print("FAIL: build_section_map: '2 DOSAGE' body blocks missing.")
        ok10 = False
    sub_map = build_section_map(
        [
            {"page": 1, "bbox": [0, 0, 10, 10], "text": "2.1 Dosage in adults"},
            {"page": 1, "bbox": [0, 0, 10, 10], "text": "alpha beta gamma"},
        ]
    )
    if any(k.startswith("2.1") for k in sub_map):
        print("PASS: build_section_map: numbered subsection is its own chunk.")
    else:
        print(f"FAIL: build_section_map missed subsection: {list(sub_map)}")
        ok10 = False

    # 10c: diff_section_maps diffs strictly by section key.
    def blocks_for(sections: list[tuple[str, str, str]]) -> list[dict]:
        blocks = []
        for num, title, body in sections:
            blocks.append({"page": 1, "bbox": [0, 0, 10, 10], "text": f"{num} {title}"})
            blocks.append({"page": 1, "bbox": [0, 0, 10, 10], "text": body})
        return blocks

    approved_map = build_section_map(blocks_for(SECTIONS_4))
    moved_order = [SECTIONS_4[2], SECTIONS_4[0], SECTIONS_4[1], SECTIONS_4[3]]
    moved_map = build_section_map(blocks_for(moved_order))
    moved_entries = diff_section_maps(approved_map, moved_map)
    if not moved_entries:
        print("PASS: diff_section_maps: pure move -> 0 entries (matched by key, no cascade).")
    else:
        print(
            f"FAIL: pure move produced entries: "
            f"{[(e['kind'], e['section']) for e in moved_entries]}"
        )
        ok10 = False

    edited = [
        (
            n,
            t,
            b.replace("especially", "very")
            if t == "WARNINGS AND PRECAUTIONS"
            else b,
        )
        for n, t, b in moved_order
    ]
    edited_entries = diff_section_maps(approved_map, build_section_map(blocks_for(edited)))
    changed = [e for e in edited_entries if e["kind"] == "text_changed"]
    missing = [
        e for e in edited_entries if e["kind"] in ("deleted_section", "inserted_section")
    ]
    if (
        len(edited_entries) == 1
        and len(changed) == 1
        and changed[0]["section"] == "3 WARNINGS AND PRECAUTIONS"
        and "especially" in changed[0]["approved_text"]
        and "very" in changed[0]["suspect_text"]
        and not missing
    ):
        print("PASS: diff_section_maps: moved+edited -> one text_changed, no deleted/inserted.")
    else:
        print(
            f"FAIL: moved+edited: "
            f"{[(e['kind'], e['section'], e.get('approved_text'), e.get('suspect_text')) for e in edited_entries]}"
        )
        ok10 = False

    deleted_map = build_section_map(
        blocks_for([s for s in SECTIONS_4 if s[1] != "WARNINGS AND PRECAUTIONS"])
    )
    deleted_entries = diff_section_maps(approved_map, deleted_map)
    dels = [e for e in deleted_entries if e["kind"] == "deleted_section"]
    if len(deleted_entries) == 1 and dels and dels[0]["section"] == "3 WARNINGS AND PRECAUTIONS":
        print("PASS: diff_section_maps: deleted section -> one deleted_section, no cascade.")
    else:
        print(f"FAIL: deleted section: {[(e['kind'], e['section']) for e in deleted_entries]}")
        ok10 = False

    # Wrap-artifact keys ("4 CONTRAINDICATION S" vs "4 CONTRAINDICATIONS")
    # must still pair via the normalized-key fallback -- never reported
    # as deleted/inserted.
    wrap_app = build_section_map(
        [
            {"page": 1, "bbox": [0, 0, 10, 10], "text": "4 CONTRAINDICATION"},
            {"page": 1, "bbox": [0, 0, 10, 10], "text": "S"},
            {"page": 1, "bbox": [0, 0, 10, 10], "text": "iota kappa lambda"},
        ]
    )
    wrap_sus = build_section_map(
        [
            {"page": 1, "bbox": [0, 0, 10, 10], "text": "4 CONTRAINDICATIONS"},
            {"page": 1, "bbox": [0, 0, 10, 10], "text": "iota kappa lambda"},
        ]
    )
    wrap_entries = diff_section_maps(wrap_app, wrap_sus)
    wrap_missing = [
        e for e in wrap_entries if e["kind"] in ("deleted_section", "inserted_section")
    ]
    wrap_changed = [e for e in wrap_entries if e["kind"] == "text_changed"]
    if not wrap_missing and len(wrap_changed) == 1:
        # The one entry is the header-line wrap artifact itself
        # ("CONTRAINDICATION S" vs "CONTRAINDICATIONS") -- the KEYED
        # pairing worked, which is what this check is about.
        print(
            "PASS: diff_section_maps: wrapped-vs-flat header keys pair by "
            "normalized key (no deleted/inserted)."
        )
    else:
        print(
            f"FAIL: wrap-artifact keys not paired: "
            f"{[(e['kind'], e['section']) for e in wrap_entries]}"
        )
        ok10 = False

    if ok10:
        print("PASS: core/section_diff.py pipeline (extract_reading_order / build_section_map / diff_section_maps).")

    print()
    print("=" * 70)
    print("TEST 11: RTL bidi character-reorder + numeric code-fragment stitch")
    print("=" * 70)
    ok11 = True

    # A merged mixed-script word ("," + 8 Arabic letters + "," + "MIDC",
    # the real case that produced "CDIM" via a naive whole-string
    # reversal) must reorder the Arabic run only, leaving the embedded
    # Latin acronym untouched.
    mixed_word = ",ﺏﻧﺏﺗﻠﺏﻢﺍ,MIDC"
    fixed = _fix_rtl_word_chars(mixed_word) if _text_is_rtl(mixed_word) else mixed_word
    if "MIDC" in fixed and "CDIM" not in fixed:
        print("PASS: embedded Latin acronym survives RTL character reordering.")
    else:
        print(f"FAIL: _fix_rtl_word_chars({mixed_word!r}) = {fixed!r} -- 'MIDC' corrupted.")
        ok11 = False

    # A merged mixed word with embedded DIGITS ("210" glued to an
    # Arabic word via a hyphen) must keep "210" intact, not "012".
    digit_word = "210-دابغناروأ"
    fixed_digits = _fix_rtl_word_chars(digit_word) if _text_is_rtl(digit_word) else digit_word
    if "210" in fixed_digits and "012" not in fixed_digits:
        print("PASS: embedded digit run survives RTL character reordering.")
    else:
        print(f"FAIL: _fix_rtl_word_chars({digit_word!r}) = {fixed_digits!r} -- '210' corrupted.")
        ok11 = False

    # The reported case: pdfplumber split ONE batch code into two
    # word-fragments ("0", "-31") in one PDF but kept it as one word
    # ("0-31") in the other -- build_comparison_tokens must re-stitch
    # the split fragments back into a single matching token.
    split_code = [
        Word(text="0", page=4, x0=477.0, top=181.4, x1=483.0, bottom=192.1),
        Word(text="-31", page=4, x0=483.0, top=181.4, x1=498.6, bottom=192.1),
    ]
    stitched = build_comparison_tokens(split_code)
    if [t.text for t in stitched] == ["0-31"]:
        print("PASS: split numeric code fragments ('0' + '-31') re-stitch to '0-31'.")
    else:
        print(f"FAIL: code-fragment stitch: {[t.text for t in stitched]!r}, expected ['0-31'].")
        ok11 = False

    # Guard: two genuine adjacent WORDS must never merge, even at a
    # near-zero gap -- only digit/punctuation-only fragments may stitch.
    real_words = [
        Word(text="double", page=1, x0=100.0, top=10.0, x1=140.0, bottom=20.0),
        Word(text="When", page=1, x0=140.3, top=10.0, x1=170.0, bottom=20.0),
    ]
    unstitched = build_comparison_tokens(real_words)
    if [t.text for t in unstitched] == ["double", "When"]:
        print("PASS: genuine adjacent words are never merged by the code-fragment rule.")
    else:
        print(f"FAIL: real words were merged: {[t.text for t in unstitched]!r}.")
        ok11 = False

    # Guard: two numeric fragments separated by a REAL inter-word gap
    # must stay separate -- only a near-zero (tokenization-artifact)
    # gap should trigger the stitch.
    real_gap_numbers = [
        Word(text="431", page=1, x0=100.0, top=10.0, x1=120.0, bottom=20.0),
        Word(text="210", page=1, x0=125.0, top=10.0, x1=145.0, bottom=20.0),
    ]
    not_stitched = build_comparison_tokens(real_gap_numbers)
    if [t.text for t in not_stitched] == ["431", "210"]:
        print("PASS: numbers separated by a real gap are not stitched.")
    else:
        print(f"FAIL: numbers with a real gap were merged: {[t.text for t in not_stitched]!r}.")
        ok11 = False

    if ok11:
        print("PASS: RTL bidi character-reorder + numeric code-fragment stitch.")

    print()
    print("=" * 70)
    print("TEST 12: adaptive column-gutter width -- a real but NARROW")
    print("         gutter on a small-font page must still be detected")
    print("=" * 70)
    ok12 = True

    # A real observed case: a compact folded leaflet using a small body
    # font had a genuine, completely unambiguous column gutter (zero
    # rows crossed it) only 10pt wide, while normal word spacing on
    # that SAME page was under 3pt. A flat 12pt minimum -- tuned for a
    # different document's larger font -- rejected the real gutter and
    # collapsed the page to row-major order, interleaving two unrelated
    # side-by-side sections word-by-word. Reconstructed here with
    # hand-built rows (not a rendered PDF) so the test is deterministic
    # and independent of any particular font-rendering engine's exact
    # line-wrapping behavior.
    narrow_gutter_rows = []
    top = 100.0
    for _ in range(20):
        row = [
            {"x0": 100.0, "x1": 130.0, "top": top, "text": "word"},
            {"x0": 131.2, "x1": 160.0, "top": top, "text": "word"},  # 1.2pt gap
            {"x0": 161.3, "x1": 190.0, "top": top, "text": "word"},  # 1.3pt gap
            # 10pt real column gutter here (190.0 -> 200.0)
            {"x0": 200.0, "x1": 230.0, "top": top, "text": "word"},
            {"x0": 231.4, "x1": 260.0, "top": top, "text": "word"},  # 1.4pt gap
        ]
        narrow_gutter_rows.append(row)
        top += 8.0

    adaptive_gutter = _adaptive_min_gutter(narrow_gutter_rows)
    bands_adaptive = _detect_column_bands(
        narrow_gutter_rows, 100.0, 260.0, min_gutter=adaptive_gutter
    )
    bands_old_fixed = _detect_column_bands(
        narrow_gutter_rows, 100.0, 260.0, min_gutter=MIN_COLUMN_GUTTER_WIDTH
    )

    if len(bands_adaptive) == 2:
        print(
            f"PASS: a real 10pt gutter on a small-font page is detected "
            f"(adaptive threshold={adaptive_gutter:.1f}pt, 2 bands: {bands_adaptive})."
        )
    else:
        print(f"FAIL: expected 2 bands with the adaptive threshold, got {bands_adaptive!r}.")
        ok12 = False

    # Control: confirms this test actually exercises the fix -- the OLD
    # flat 12pt minimum must still fail to see this same real gutter,
    # otherwise the test above wouldn't be testing anything new.
    if len(bands_old_fixed) == 1:
        print(
            "PASS: control confirmed -- the old flat 12pt minimum misses "
            "this same real gutter (collapses to 1 band)."
        )
    else:
        print(
            f"FAIL (control): expected the OLD fixed threshold to miss this "
            f"gutter (1 band), got {bands_old_fixed!r} -- this test may no "
            f"longer be exercising the fix."
        )
        ok12 = False

    if ok12:
        print("PASS: adaptive column-gutter width detection.")

    print()
    print("=" * 70)
    print("TEST 13: non-(0,0) MediaBox origin -> coordinates normalized")
    print("         to match PyMuPDF's rendering space")
    print("=" * 70)
    ok13 = True

    offset_path = os.path.join(tmp, "mediabox_offset.pdf")
    make_pdf_with_mediabox_offset(offset_path, "Hello World from an offset page")

    with pdfplumber.open(offset_path) as pdf:
        raw_page = pdf.pages[0]
        page_x0, page_top0 = raw_page.bbox[0], raw_page.bbox[1]
        raw_words = {
            w["text"]: (w["x0"], w["top"]) for w in raw_page.extract_words()
        }

    if (page_x0, page_top0) == (0.0, 0.0):
        print(
            f"FAIL: test setup didn't actually produce a non-(0,0) MediaBox "
            f"origin (page.bbox={raw_page.bbox!r}) -- this test isn't "
            f"exercising the fix."
        )
        ok13 = False
    else:
        print(f"PASS: control confirmed -- test page has a non-(0,0) origin (page.bbox={raw_page.bbox!r}).")

    extracted = {w.text: (w.x0, w.top) for w in extract_words(offset_path)}
    mismatches = []
    for text, (raw_x0, raw_top) in raw_words.items():
        expected = (raw_x0 - page_x0, raw_top - page_top0)
        got = extracted.get(text)
        # 0.5pt: PyMuPDF and pdfminer size glyph boxes a fraction of a
        # point apart; the origin offset under test is 20-30pt.
        if got is None or abs(got[0] - expected[0]) > 0.5 or abs(got[1] - expected[1]) > 0.5:
            mismatches.append((text, expected, got))

    if mismatches:
        print(f"FAIL: {len(mismatches)} word(s) not normalized to the page's own origin:")
        for text, expected, got in mismatches[:5]:
            print(f"    {text!r}: expected {expected}, got {got}")
        ok13 = False
    else:
        print(
            "PASS: extract_words() normalizes every word's coordinates by "
            "the page's own MediaBox/CropBox origin."
        )

    if ok13:
        print("PASS: non-(0,0) MediaBox origin coordinate normalization.")

    print()
    print("=" * 70)
    print("TEST 14: unnumbered section headers -- out-of-order matching")
    print("         + false-positive guards (document title, control code)")
    print("=" * 70)
    ok14 = True

    def w14(text: str, top: float) -> Word:
        return Word(text=text, page=1, x0=0.0, top=top, x1=10.0, bottom=top + 10.0)

    # Scenario A: two documents whose sections use UNNUMBERED headers
    # (no leading section number at all, unlike TEST 8/9's FDA-style
    # documents) and appear in a DIFFERENT relative order -- the real
    # reported case: a "Cardmember Agreement" print sheet combined with
    # an unrelated insert, read column-major in a different order than
    # the reference document. Title-anchored matching must still pair
    # each section by its header text, and the reordered one is matched
    # where it stands rather than reported as a critical cascade.
    approved_unnumbered = [
        w14("CARDMEMBER", 10), w14("AGREEMENT", 10),
        w14("alpha", 24), w14("beta", 24), w14("gamma", 24),
        w14("INTEREST", 50), w14("CHARGES", 50),
        w14("delta", 64), w14("epsilon", 64), w14("zeta", 64),
        w14("ARBITRATION", 90),
        w14("eta", 104), w14("theta", 104), w14("iota", 104),
    ]
    suspect_unnumbered = [
        # ARBITRATION printed FIRST here -- a different relative order,
        # like content relocated by a different print layout.
        w14("ARBITRATION", 10),
        w14("eta", 24), w14("theta", 24), w14("iota", 24),
        w14("CARDMEMBER", 50), w14("AGREEMENT", 50),
        w14("alpha", 64), w14("beta", 64), w14("gamma", 64),
        w14("INTEREST", 90), w14("CHARGES", 90),
        w14("delta", 104), w14("epsilon", 104), w14("zeta", 104),
    ]
    ta_app = build_comparison_tokens(approved_unnumbered)
    ta_susp = build_comparison_tokens(suspect_unnumbered)
    sections_app = _split_sections(ta_app)
    sections_susp = _split_sections(ta_susp)
    unnumbered_diffs = diff_documents(ta_app, ta_susp) if sections_app and sections_susp else None
    unnumbered_criticals = (
        [d for d in unnumbered_diffs if d.severity == "critical"] if unnumbered_diffs else None
    )

    if sections_app is None or sections_susp is None:
        print("FAIL: unnumbered headers were not detected -- section-based diffing never activated.")
        ok14 = False
    elif unnumbered_criticals:
        print(
            f"FAIL: relocated unnumbered sections produced {len(unnumbered_criticals)} "
            f"critical false positive(s) instead of matching by title:"
        )
        for d in unnumbered_criticals[:5]:
            print(f"    {d.kind}: {d.approved_text!r} -> {d.suspect_text!r}")
        ok14 = False
    elif unnumbered_diffs:
        print(
            f"FAIL: the moved 'ARBITRATION' section should be matched where it stands, "
            f"got rows: {[(d.kind, d.approved_text) for d in unnumbered_diffs]!r}"
        )
        ok14 = False
    else:
        print(
            "PASS: unnumbered headers ('CARDMEMBER AGREEMENT', 'INTEREST CHARGES', "
            "'ARBITRATION') matched by title across a different relative order; "
            "the moved section is matched, not reported."
        )

    # Scenario B (false-positive guard): a document's own multi-line
    # title/caption must never be split into spurious sections, and a
    # document control code must never be mistaken for a header OR
    # block a REAL header immediately following it -- the exact
    # reported case ("CM.TL24K.LIN.0215" immediately followed by
    # "CARDMEMBER AGREEMENT").
    guard_words = [
        w14("FULL", 10), w14("PRESCRIBING", 10), w14("INFORMATION", 24),
        w14("CM.TL24K.LIN.0215", 50),
        w14("CARDMEMBER", 64), w14("AGREEMENT", 64),
        w14("alpha", 78), w14("beta", 78), w14("gamma", 78),
        w14("INTEREST", 92), w14("CHARGES", 92),
        w14("delta", 106), w14("epsilon", 106), w14("zeta", 106),
    ]
    guard_sections = _split_sections(build_comparison_tokens(guard_words))
    guard_titles = [s.title for s in guard_sections] if guard_sections else []

    if guard_sections is None:
        print("FAIL: guard scenario didn't even activate section-based diffing.")
        ok14 = False
    elif "INFORMATION" in guard_titles or "FULL PRESCRIBING" in guard_titles:
        print(
            f"FAIL: the document's own wrapped title/caption was split into a "
            f"spurious section: {guard_titles!r}"
        )
        ok14 = False
    elif "CM.TL24K.LIN.0215" in guard_titles:
        print(f"FAIL: a document control code was mistaken for a section header: {guard_titles!r}")
        ok14 = False
    elif "CARDMEMBER AGREEMENT" not in guard_titles:
        print(
            f"FAIL: the real header immediately after the control code was NOT "
            f"detected: {guard_titles!r}"
        )
        ok14 = False
    else:
        print(
            "PASS: a wrapped document caption is never split into spurious "
            "sections, and a control code is never mistaken for (or blocks "
            "detection of) a real header."
        )

    if ok14:
        print("PASS: unnumbered section headers + false-positive guards.")

    print()
    print("=" * 70)
    print("TEST 15: per-zone column detection + column-flow/row-major")
    print("=" * 70)
    ok15 = True

    def w15(text: str, x0: float, x1: float, top: float) -> dict:
        return {"text": text, "x0": x0, "x1": x1, "top": top, "bottom": top + 8.0}

    # Part A: a real observed case -- a single-column clause, followed
    # by a header, followed by a genuine narrow (8pt) 2-column
    # structure. Detecting gutters once for the whole page would miss
    # the narrow gutter entirely: the single-column clause's lines
    # span the full width, so they "cross" that exact x-range
    # constantly. Only per-zone detection (splitting at the header)
    # can find it.
    zone_words: list[dict] = []
    for i in range(10):
        top = 10.0 + i * 12.0
        zone_words.append(w15("word", 20.0, 25.0, top))
        zone_words.append(w15("word", 250.0, 255.0, top))  # crosses the zone-2 gutter
        zone_words.append(w15("word", 480.0, 485.0, top))
    header_top = 10.0 + 10 * 12.0
    zone_words.append(w15("ARBITRATION", 20.0, 90.0, header_top))
    zone2_top0 = header_top + 12.0
    # Two columns of small-type prose: each line FILLS its column (six
    # words, 1.6pt word spaces, as 6-7pt leaflet type sets them), column A
    # 20 -> 200, an 8pt real gutter, column B 208 -> 388. (An earlier
    # version of this fixture put ONE 3pt token per column per row --
    # the shape of a borderless label/value table, which is deliberately
    # read row-major; see the control check below.)
    word_gap = 1.6
    word_w = (180.0 - 5 * word_gap) / 6
    for i in range(5):
        top = zone2_top0 + i * 12.0
        for name, x in (("A", 20.0), ("B", 208.0)):
            for k in range(6):
                zone_words.append(w15(f"{name}{i}" if k == 0 else "w", x, x + word_w, top))
                x += word_w + word_gap

    def read_order(words):
        units, bands = _order_body_units(words, 20.0, 485.0)
        ordered: list[str] = []
        for band_idx in range(len(bands)):
            band_units = sorted((u for u in units if u[0] == band_idx), key=lambda u: u[1])
            for _, _, line_words in band_units:
                ordered.extend(ww["text"] for ww in line_words)
        return [t for t in ordered if t[:1] in "AB" and t[1:].isdigit()]

    ordered = read_order(zone_words)
    table_words = [ww for ww in zone_words if ww["top"] < zone2_top0]
    for i in range(5):
        top = zone2_top0 + i * 12.0
        table_words.append(w15(f"A{i}", 20.0, 23.0, top))
        table_words.append(w15(f"B{i}", 208.0, 211.0, top))
    if read_order(table_words) == ["A0", "B0", "A1", "B1", "A2", "B2", "A3", "B3", "A4", "B4"]:
        print("PASS: one short label beside one short value per row reads row-major (a table).")
    else:
        print(f"FAIL: label/value rows were not read row-major: {read_order(table_words)}")
        ok15 = False

    expected_tail = ["A0", "A1", "A2", "A3", "A4", "B0", "B1", "B2", "B3", "B4"]
    if ordered[-10:] == expected_tail:
        print(
            "PASS: a real 8pt gutter, invisible at the whole-page level, is "
            "correctly detected once zone 2 is judged on its own rows "
            "(column-major: A0-A4 then B0-B4)."
        )
    else:
        print(f"FAIL: expected zone 2 to read {expected_tail!r}, got {ordered[-10:]!r}")
        ok15 = False

    # Part B: a borderless "label: value" table -- narrow label column,
    # wide explanation column, same asymmetry as a real "Governing
    # Law" / "This Agreement is governed by..." row. Must read ROW-
    # major (each row's own label with its own value), not column-
    # major (every label, then every value).
    table_words: list[dict] = []
    rows_data = [
        ("Label1", "Value1a Value1b"),
        ("Label2", "Value2a Value2b"),
        ("Label3", "Value3a Value3b"),
    ]
    for i, (label, value) in enumerate(rows_data):
        top = 10.0 + i * 14.0
        table_words.append(w15(label, 20.0, 60.0, top))
        vx = 80.0
        for tok in value.split():
            table_words.append(w15(tok, vx, vx + 8, top))
            vx += 12.0
        table_words.append(w15(f"tail{i}", vx, vx + 150, top))

    table_units, table_bands = _order_body_units(table_words, 20.0, 400.0)
    table_ordered: list[str] = []
    for band_idx in range(len(table_bands)):
        band_units = sorted((u for u in table_units if u[0] == band_idx), key=lambda u: u[1])
        for _, _, line_words in band_units:
            table_ordered.extend(ww["text"] for ww in line_words)

    expected_row_major = [
        "Label1", "Value1a", "Value1b", "tail0",
        "Label2", "Value2a", "Value2b", "tail1",
        "Label3", "Value3a", "Value3b", "tail2",
    ]
    if table_ordered == expected_row_major:
        print(
            "PASS: an asymmetric-width borderless label/value table reads "
            "row-major (each label with its own value), not column-major."
        )
    else:
        print(f"FAIL: expected row-major {expected_row_major!r}, got {table_ordered!r}")
        ok15 = False

    if ok15:
        print("PASS: per-zone column detection + column-flow/row-major.")

    print()
    print("=" * 70)
    print("TEST 16: MVP-21 -- zone-splitting false positives on numbered")
    print("         list items / dose numbers, and 3+-band row-major bug")
    print("=" * 70)
    ok16 = True

    # Part A: a numbered list ITEM (lowercase tail) must never be
    # mistaken for a header -- the exact real shape that fragmented a
    # credit-card agreement's numbered clauses into 1-2 row zones.
    numbered_list_lines = [
        "1. Within 30 days of receiving your letter, we must tell you",
        "2. Within 90 days of receiving your letter, we must correct",
        "3. You must not yet have fully paid for the purchase.",
    ]
    if not any(is_likely_header_line(t) for t in numbered_list_lines):
        print("PASS: numbered list items (lowercase tail) are never treated as headers.")
    else:
        bad = [t for t in numbered_list_lines if is_likely_header_line(t)]
        print(f"FAIL: numbered list item(s) wrongly treated as headers: {bad!r}")
        ok16 = False

    # Part B: a bare dose number line (unit/name in a non-Latin script,
    # so it can never satisfy the Latin-only caps-token check) must
    # also never be mistaken for a header -- the exact real shape that
    # fragmented an Arabic drug label's dosage table.
    dose_lines = ["150 مج ( أدهاتودا فاسيكا )", "75 مج ( جليسيرزة جابرة )"]
    if not any(is_likely_header_line(t) for t in dose_lines):
        print("PASS: bare dose numbers with a non-Latin tail are never treated as headers.")
    else:
        bad = [t for t in dose_lines if is_likely_header_line(t)]
        print(f"FAIL: dose line(s) wrongly treated as headers: {bad!r}")
        ok16 = False

    # Control: a genuine numbered header (bare number ALONE on its own
    # line -- the real FDA shape) and a number + all-caps tail on the
    # SAME line must both still be detected, proving Part A/B didn't
    # just disable the numbered-header branch outright.
    genuine_headers = ["1", "17 ARBITRATION", "4 CONTRAINDICATIONS"]
    if all(is_likely_header_line(t) for t in genuine_headers):
        print("PASS: control confirmed -- genuine bare/caps-tail numbered headers still detected.")
    else:
        bad = [t for t in genuine_headers if not is_likely_header_line(t)]
        print(f"FAIL: genuine numbered header(s) no longer detected: {bad!r}")
        ok16 = False

    # Part C: a real 4-column zone (one narrow ~70pt bullet-marker
    # column among three ~280-470pt text columns -- the exact real
    # shape from an oversized proof sheet) must still read column-
    # major, not collapse to row-major just because one band is
    # narrow relative to the others.
    wide_bands = [(42.0, 325.0), (335.0, 618.0), (651.0, 724.0), (759.0, 1230.0)]
    if _bands_are_column_flow(wide_bands):
        print(
            "PASS: a 4-band zone with one narrow bullet-marker column still "
            "reads column-major (3+ bands skip the width-ratio check)."
        )
    else:
        print(f"FAIL: 4-band zone wrongly collapsed to row-major: {wide_bands!r}")
        ok16 = False

    # Control: the real 2-band label/value case (the reason the ratio
    # check exists at all) must still be rejected as column-flow.
    label_value_bands = [(20.0, 60.0), (80.0, 400.0)]
    if not _bands_are_column_flow(label_value_bands):
        print("PASS: control confirmed -- a genuine 2-band label/value zone still reads row-major.")
    else:
        print(f"FAIL: 2-band label/value zone wrongly read column-major: {label_value_bands!r}")
        ok16 = False

    if ok16:
        print(
            "PASS: MVP-21 -- zone-splitting false positives on numbered list "
            "items/dose numbers, and 3+-band row-major bug."
        )

    print()
    print("=" * 70)
    print("TEST 17: MVP-22 -- nested sub-column records, RTL column order,")
    print("         and a flowing paragraph misdetected as a real table")
    print("=" * 70)
    ok17 = True

    def w17(text: str, x0: float, x1: float, top: float) -> dict:
        return {"text": text, "x0": x0, "x1": x1, "top": top}

    # Part A: a label/value record whose value cell wraps across TWO
    # independent sub-columns (a real observed case: a credit-card
    # Fees table's "Late Fee" row has a "for Purchases" explanation
    # next to a parallel "for Cash Advances" explanation, each
    # wrapping onto its own following line) must read one sub-column
    # FULLY (top to bottom) before the other, not interleave per
    # physical line -- followed by an ordinary single-column record.
    record1_line1 = [
        w17("Late", 20, 32, 100), w17("Fee", 34, 46, 100),
        w17("Alpha0", 80, 88, 100), w17("Alpha1", 92, 100, 100),
        w17("Beta0", 140, 148, 100), w17("Beta1", 152, 160, 100),
    ]
    record1_line2 = [
        w17("Alpha2", 80, 88, 112), w17("Alpha3", 92, 100, 112),
        w17("Beta2", 140, 148, 112), w17("Beta3", 152, 160, 112),
    ]
    record2_line = [
        w17("Other", 20, 42, 130),
        w17("Simple", 80, 108, 130), w17("Text", 112, 132, 130),
    ]
    lv_zone_rows = [record1_line1, record1_line2, record2_line]
    lv_bands = [(20.0, 60.0), (70.0, 300.0)]
    lv_ordered = [
        w["text"] for line in _order_label_value_zone(lv_zone_rows, lv_bands, rtl=False) for w in line
    ]
    lv_expected = [
        "Late", "Fee", "Alpha0", "Alpha1", "Alpha2", "Alpha3",
        "Beta0", "Beta1", "Beta2", "Beta3", "Other", "Simple", "Text",
    ]
    if lv_ordered == lv_expected:
        print(
            "PASS: a record's own internal sub-column split reads one "
            "sub-column fully before the other, not interleaved per line."
        )
    else:
        print(f"FAIL: expected {lv_expected!r}, got {lv_ordered!r}")
        ok17 = False

    # Control: a lone physical line (too little data for
    # `_consistent_internal_gutter_x` to trust, and Record2 has no
    # real internal split anyway) must NOT be spuriously split.
    split_x = _consistent_internal_gutter_x(
        [[w17("Simple", 80, 108, 130), w17("Text", 112, 132, 130)]], min_gutter=6.0
    )
    if split_x is None:
        print("PASS: control confirmed -- a single-row value never gets a spurious internal split.")
    else:
        print(f"FAIL: single-row value wrongly split at x={split_x!r}")
        ok17 = False

    # Part B: a genuine 3-column RTL zone must read its RIGHTMOST
    # column FIRST -- every multi-column reading-order path in this
    # file was built left-to-right first and never reversed for
    # `rtl=True` (a real observed case: a wide RTL proof sheet's real
    # columns were read left-to-right regardless, splicing a LATER
    # column's text into an EARLIER column's sentence).
    # Columns of running text: about 120pt wide, five words a line (a
    # grid of single 10pt tokens is what a TABLE looks like, and is read
    # row by row since tables are told apart from text columns).
    rtl_zone_words: list[dict] = []
    for i in range(5):
        top = 10.0 + i * 12.0
        for name, x in (("R", 400.0), ("M", 220.0), ("L", 40.0)):  # rightmost first
            for k in range(5):
                word = w17(f"{name}{i}", x + k * 24.0, x + k * 24.0 + 20.0, top)
                word["bottom"] = top + 9.0
                rtl_zone_words.append(word)
    rtl_units, rtl_bands = _order_body_units(rtl_zone_words, 40.0, 516.0, rtl=True)
    rtl_ordered: list[str] = []
    for band_idx in range(len(rtl_bands)):
        band_units = sorted((u for u in rtl_units if u[0] == band_idx), key=lambda u: u[1])
        for _, _, line_words in band_units:
            for ww in line_words:
                if not rtl_ordered or rtl_ordered[-1] != ww["text"]:
                    rtl_ordered.append(ww["text"])
    rtl_expected = (
        [f"R{i}" for i in range(5)] + [f"M{i}" for i in range(5)] + [f"L{i}" for i in range(5)]
    )
    if rtl_ordered == rtl_expected:
        print("PASS: a genuine multi-column RTL zone reads its rightmost column first.")
    else:
        print(f"FAIL: expected RTL column order {rtl_expected!r}, got {rtl_ordered!r}")
        ok17 = False

    # Part C: a "table" whose rows never populate more than one column
    # at once (a real observed case: a decorative bordered box around
    # ONE flowing Arabic paragraph, whose incidental ruling lines
    # `find_tables` misread as a 6-row x 3-column grid) must be
    # rejected -- it is not tabular data. A genuine table, where rows
    # regularly populate multiple columns together, must still pass.
    fake_table_cells = {
        0: (0, 0, 0), 1: (0, 1, 0), 2: (0, 2, 0), 3: (0, 3, 0),
    }
    if not _looks_like_real_table(0, fake_table_cells):
        print("PASS: a table where every row populates only one column is rejected as not tabular.")
    else:
        print("FAIL: single-column-per-row 'table' was accepted as real.")
        ok17 = False

    real_table_cells = {
        10: (1, 0, 0), 11: (1, 0, 1),
        12: (1, 1, 0), 13: (1, 1, 1),
        14: (1, 2, 0), 15: (1, 2, 1),
    }
    if _looks_like_real_table(1, real_table_cells):
        print("PASS: control confirmed -- a genuine table (multi-column rows) is still accepted.")
    else:
        print("FAIL: a genuine multi-column-row table was wrongly rejected.")
        ok17 = False

    if ok17:
        print(
            "PASS: MVP-22 -- nested sub-column records, RTL column order, "
            "and flowing-paragraph-as-table misdetection."
        )

    print()
    print("=" * 70)
    print("TEST 18: MVP-23 -- borderless composition table row matching")
    print("         and RTL neutral bracket bidi line sorting")
    print("=" * 70)
    ok18 = True

    # Part A: Borderless 2-column table with width ratio ~0.42 (e.g. dosage column ~36pt,
    # herb description column ~87pt) must be classified as row-major label-value table,
    # NOT 2-column flowing text.
    composition_bands = [(90.0, 125.87), (165.0, 251.77)]
    if not _bands_are_column_flow(composition_bands):
        print("PASS: 2-band composition table (ratio ~0.41) correctly classified as row-major.")
    else:
        print("FAIL: 2-band composition table wrongly classified as 2-column flow.")
        ok18 = False

    # Part B: Line with Arabic words, neutral brackets, and LTR number in RTL mode
    # must place the bracket next to the Arabic word, not inside the LTR number run.
    test_line = [
        {"text": "مجم", "x0": 80.0, "x1": 93.0, "top": 100.0},
        {"text": "150", "x0": 96.0, "x1": 110.0, "top": 100.0},
        {"text": ")", "x0": 147.0, "x1": 150.0, "top": 100.0},
        {"text": "فاسيكا", "x0": 151.0, "x1": 170.0, "top": 100.0},
        {"text": "أدهاتودا", "x0": 173.0, "x1": 215.0, "top": 100.0},
        {"text": "(", "x0": 218.0, "x1": 222.0, "top": 100.0},
        {"text": "فاساكا", "x0": 225.0, "x1": 253.0, "top": 100.0},
    ]
    sorted_words = [w["text"] for w in _sort_line_words(test_line, rtl=True)]
    expected_order = ["فاساكا", "(", "أدهاتودا", "فاسيكا", ")", "150", "مجم"]
    if sorted_words == expected_order:
        print("PASS: neutral brackets correctly stay with RTL Arabic phrase in bidi sorting.")
    else:
        print(f"FAIL: expected {expected_order!r}, got {sorted_words!r}")
        ok18 = False

    if ok18:
        print("PASS: MVP-23 -- borderless composition table row matching and RTL bidi sorting.")


    print()
    print("=" * 70)
    print("TEST 19: superscripts/subscripts kept as-is and compared as-is")
    print("=" * 70)
    from core.document_extractor import extract_document_words
    from core.script_text import to_script

    script_path = os.path.join(tmp, "t19_scripts.pdf")
    plain_path = os.path.join(tmp, "t19_plain.pdf")
    make_script_pdf(
        script_path,
        [
            # "2" + raised "4": must read "2⁴", not "24".
            [("Take 2", 12, 0), ("4", 8, 4.5), (" tablets", 12, 0)],
            # "h" + lowered "2", "o" + lowered "4" (only 0.5pt lower,
            # as observed in a real proof): must stay on this line.
            [("acid h", 12, 0), ("2", 8, -0.5), ("so", 12, 0), ("4", 8, -0.5), (" here", 12, 0)],
            # Multi-glyph superscript run.
            [("value x", 12, 0), ("10", 8, 4.5), (" end", 12, 0)],
            # Control: small caps -- smaller glyphs on the SAME baseline
            # stay normal text (and stay in one word).
            [("S", 12, 0), ("MALL", 8, 0), (" caps", 12, 0)],
        ],
    )
    make_script_pdf(
        plain_path,
        [
            [("Take 24 tablets", 12, 0)],
            [("acid h2so4 here", 12, 0)],
            [("value x10 end", 12, 0)],
            [("SMALL caps", 12, 0)],
        ],
    )
    script_words = [w.text for w in extract_words(script_path)]
    expected_script_words = [
        "Take", "2⁴", "tablets",
        "acid", "h₂so₄", "here",
        "value", "x¹⁰", "end",
        "SMALL", "caps",
    ]
    ok19 = True
    if script_words == expected_script_words:
        print("PASS: raised/lowered glyphs read as Unicode superscript/subscript, on their own line, small caps untouched.")
    else:
        print(f"FAIL: expected {expected_script_words!r}, got {script_words!r}")
        ok19 = False

    script_diffs = run_pipeline(plain_path, script_path)
    changed = {(d.approved_text, d.suspect_text): d.severity for d in script_diffs}
    if (
        changed.get(("24", "2⁴")) == "critical"
        and changed.get(("h2so4", "h₂so₄")) == "critical"
        and changed.get(("x10", "x¹⁰")) == "critical"
        and len(script_diffs) == 3
    ):
        print("PASS: '24' vs '2⁴' (and subscript/multi-digit cases) are critical numeric changes; nothing else differs.")
    else:
        print(f"FAIL: unexpected diffs {[(d.kind, d.severity, d.approved_text, d.suspect_text) for d in script_diffs]!r}")
        ok19 = False

    if run_pipeline(script_path, script_path):
        print("FAIL: identical superscript/subscript documents produced diffs.")
        ok19 = False
    else:
        print("PASS: identical superscript/subscript documents produce 0 diffs.")

    docx_path = os.path.join(tmp, "t19_scripts.docx")
    make_script_docx(docx_path)
    docx_words = [w.text for w in extract_document_words(docx_path)]
    expected_docx = ["Take", "2⁴", "tablets", "with", "H₂O", "daily."]
    if docx_words == expected_docx:
        print("PASS: DOCX superscript/subscript runs produce the same text as the PDF path.")
    else:
        print(f"FAIL: expected {expected_docx!r}, got {docx_words!r}")
        ok19 = False

    if to_script("q", "sup") == "^{q}" and to_script("y2", "sub") == "_{y2}":
        print("PASS: characters without a Unicode form fall back to ^{...} / _{...}.")
    else:
        print(f"FAIL: fallback gave {to_script('q', 'sup')!r} / {to_script('y2', 'sub')!r}")
        ok19 = False

    if ok19:
        print("PASS: superscript/subscript handling.")

    print()
    print("=" * 70)
    print("TEST 20: moved text across mispaired sections, repeated headers,")
    print("         and severity of long prose")
    print("=" * 70)
    from core.diff_engine import _classify_severity
    from core.section_diff import build_section_list

    def lines_to_words(lines, page=1):
        """One Word per token, one visual line per entry."""
        words = []
        for row, line in enumerate(lines):
            x = 40.0
            for text in line.split():
                width = 6.0 * len(text)
                words.append(Word(text=text, page=page, x0=x, top=40 + row * 14, x1=x + width, bottom=52 + row * 14))
                x += width + 4.0
        return words

    body_2a = [
        "Take the tablets with water once every morning before breakfast",
        "and swallow them whole without chewing or crushing them first.",
    ]
    body_2b = [
        "Patients with severe kidney disease should ask their doctor about",
        "the dose because the medicine is removed more slowly from the body.",
        "Elderly patients may need additional monitoring during the treatment.",
    ]
    approved_lines = (
        ["1 INDICATIONS AND USAGE", "This medicine treats blood clots in adults and children."]
        + ["2 DOSAGE AND ADMINISTRATION"] + body_2a + body_2b
        + ["3 CONTRAINDICATIONS", "Do not use in patients with active bleeding."]
    )
    # The suspect print sheet: a figure caption read as a header ("Kidney
    # Function Chart:") takes the rest of section 2 into its own chunk,
    # which ends up after section 3.
    suspect_lines = (
        ["1 INDICATIONS AND USAGE", "This medicine treats blood clots in adults and children."]
        + ["2 DOSAGE AND ADMINISTRATION"] + body_2a
        + ["3 CONTRAINDICATIONS", "Do not use in patients with active bleeding."]
        + ["Kidney Function Chart:"] + body_2b
    )
    moved_diffs = run_tokens(lines_to_words(approved_lines), lines_to_words(suspect_lines))
    # The caption itself exists only in the suspect: that one is real.
    criticals = [d for d in moved_diffs if d.severity == "critical" and d.suspect_text != "Kidney Function Chart:"]
    moved_rows = [d for d in moved_diffs if "kidney disease" in d.approved_text + d.suspect_text]
    ok20 = True
    if criticals or moved_rows:
        print(f"FAIL: moved section text: criticals={[(d.kind, d.approved_text[:40], d.suspect_text[:40]) for d in criticals]} "
              f"rows for the moved text={len(moved_rows)}")
        ok20 = False
    else:
        print("PASS: text split into a mispaired section is matched, 0 rows for it, 0 criticals.")
    if any(d.kind == "reordered_section" and d.suspect_text.startswith("Kidney") for d in moved_diffs):
        print("FAIL: a content-only pairing was reported as a moved section.")
        ok20 = False
    else:
        print("PASS: no 'Reordered Section' row claimed for a caption paired by content.")

    edited_lines = [line.replace("removed more slowly", "removed more quickly") for line in suspect_lines]
    edited_diffs = run_tokens(lines_to_words(approved_lines), lines_to_words(edited_lines))
    edit = [d for d in edited_diffs if d.approved_text == "slowly" and d.suspect_text == "quickly"]
    stray = [d for d in edited_diffs if d.kind in ("removed_in_suspect", "added_in_suspect")
             and d.suspect_text != "Kidney Function Chart:"]
    if len(edit) == 1 and edit[0].severity == "critical" and not stray:
        print("PASS: a real edit inside the moved text is still reported as critical.")
    else:
        print(f"FAIL: edit inside moved text: {[(d.kind, d.severity, d.approved_text, d.suspect_text) for d in edited_diffs]}")
        ok20 = False

    # Repeated colon headers. In a document WITHOUT numbered sections a
    # colon header ("Your Billing Rights:") starts a section, and a repeat
    # gives a second, separate section in order.
    colon_blocks = [
        {"page": 1, "bbox": (0, i * 14, 100, i * 14 + 12), "text": text}
        for i, text in enumerate([
            "Your Billing Rights:", "Keep this notice.",
            "Interest Charges:", "Rates vary.",
            "Your Billing Rights:", "Second copy.",
        ])
    ]
    titles = [title for title, _ in build_section_list(colon_blocks)]
    if titles == ["Your Billing Rights:", "Interest Charges:", "Your Billing Rights:"]:
        print("PASS: a repeated header gives separate sections, in order.")
    else:
        print(f"FAIL: repeated header sections: {titles}")
        ok20 = False

    # In a NUMBERED document, colon labels ("Manufactured by:", and the
    # chart labels a real rivaroxaban leaflet is full of) deliberately do
    # not start sections -- see test_sample2_layout_diff. What matters is
    # that a change inside such a block is still caught when the block
    # also moves: a manufacturing-site change is a regulatory change.
    def mfr_doc(site, at_end):
        body = ["1 INDICATIONS AND USAGE", "Tizanidine is indicated for spasticity.",
                "2 DOSAGE AND ADMINISTRATION", "Start with 4 mg every six hours."]
        block = ["Manufactured by:", f"Acme Pharma Limited, {site}"]
        lines = body + block if at_end else body[:2] + block + body[2:]
        return build_comparison_tokens(lines_to_words(lines))

    site_diffs = diff_documents(mfr_doc("Plant 1, Hyderabad.", False), mfr_doc("Plant 2, Pune.", True))
    site_rows = [d for d in site_diffs if d.severity == "critical"
                 and ("Hyderabad" in d.approved_text or "Pune" in d.suspect_text)]
    if len(site_rows) >= 1 and all(
        "Hyderabad" in " ".join(d.approved_text for d in site_rows) for _ in [0]
    ) and "Pune" in " ".join(d.suspect_text for d in site_rows):
        print("PASS: a manufacturing-site change inside a moved block is critical.")
    else:
        print(f"FAIL: site change: {[(d.severity, d.kind, d.approved_text, d.suspect_text) for d in site_diffs]}")
        ok20 = False

    prose = ("After 2 weeks the dose rose from 5 mg to 10 mg and then 15 mg, "
             "20 mg and 30 mg over 40 days in most of the treated patients.")
    prose_tokens = build_comparison_tokens(lines_to_words([prose]))
    if _classify_severity("delete", prose_tokens, []) == "critical":
        print("PASS: removed prose containing rising numbers is critical, not chart 'minor'.")
    else:
        print("FAIL: removed prose with rising numbers was downgraded.")
        ok20 = False
    axis_tokens = build_comparison_tokens(lines_to_words(["0 10 20 30 40 50 60"]))
    if _classify_severity("delete", axis_tokens, []) == "minor":
        print("PASS: a bare chart axis is still 'minor' (control).")
    else:
        print("FAIL: chart axis labels are no longer recognized.")
        ok20 = False

    if ok20:
        print("PASS: moved-text pass, repeated headers and prose severity.")

    print()
    print("=" * 70)
    print("TEST 21: what exactly changed inside one difference")
    print("         (core.inline_diff -- the comparator pane's content)")
    print("=" * 70)
    from core.inline_diff import changed_words, inline_segments

    ok21 = True

    def marked(segments):
        """The marked (changed) runs of one side. Empty runs are carets
        (where the OTHER side has text), not marked text."""
        return [text for changed, text in segments if changed and text]

    def unchanged_text_survives(text, segments):
        """The reviewer must always see the real text: the runs put back
        together have to be exactly what came in."""
        return "".join(text for _changed, text in segments) == text

    cases = [
        # (original, suspect, marked on original, marked on suspect)
        ("Store below 25 °C", "Store below 30 °C", ["25"], ["30"]),
        ("Ropinirole 5 mg", "Ropinirol 50 mg", ["e"], ["0"]),
        # "one"/"two" share too little to refine, so they are marked
        # whole; "tablet"/"tablets" differ by one letter, so only the
        # added "s" is marked.
        ("take one tablet", "take two tablets", ["one"], ["two", "s"]),
        ("", "batch code 7781", [], ["batch code 7781"]),
        ("Keep out of reach", "", ["Keep out of reach"], []),
    ]
    for approved, suspect, expect_a, expect_b in cases:
        left, right = inline_segments(approved, suspect)
        if not (unchanged_text_survives(approved, left) and unchanged_text_survives(suspect, right)):
            print(f"FAIL: inline diff changed the text itself: {approved!r} / {suspect!r}")
            ok21 = False
        elif marked(left) != expect_a or marked(right) != expect_b:
            print(
                f"FAIL: {approved!r} vs {suspect!r} marked {marked(left)!r}/{marked(right)!r}, "
                f"expected {expect_a!r}/{expect_b!r}"
            )
            ok21 = False
    if ok21:
        print("PASS: only the words (and characters) that differ are marked.")

    # A spacing-only difference must still be marked -- it is invisible
    # otherwise -- and reported as whitespace, not as changed words.
    left, right = inline_segments("50 mg  daily", "50 mg daily")
    if marked(left) == ["  "] and marked(right) == [" "] and changed_words("a  b", "a b") == ("", ""):
        print("PASS: a spacing-only difference is marked, and named as spacing.")
    else:
        print(f"FAIL: spacing-only difference: {marked(left)!r}/{marked(right)!r}")
        ok21 = False

    # The one-line summary names whole words, not the letters inside them.
    if changed_words("Ropinirole 5 mg", "Ropinirol 50 mg") == ("Ropinirole 5", "Ropinirol 50"):
        print("PASS: the summary line names the changed words, not single letters.")
    else:
        print(f"FAIL: summary words: {changed_words('Ropinirole 5 mg', 'Ropinirol 50 mg')!r}")
        ok21 = False

    # A rewritten phrase is NOT refined character by character: partial
    # marks inside two unrelated words are noise.
    left, right = inline_segments("mg", "tablets")
    if marked(left) == ["mg"] and marked(right) == ["tablets"]:
        print("PASS: unrelated words are marked whole, not letter by letter.")
    else:
        print(f"FAIL: unrelated words refined to {marked(left)!r}/{marked(right)!r}")
        ok21 = False

    if ok21:
        print("PASS: inline (within-difference) comparison.")

    print()
    print("=" * 70)
    print("TEST 22: a contents list is compared with the contents list,")
    print("         not with the sections it points at")
    print("=" * 70)
    ok22 = True

    contents_lines = [
        "FULL PRESCRIBING INFORMATION: CONTENTS",
        "1 INDICATIONS AND USAGE",
        "2 DOSAGE AND ADMINISTRATION",
        "3 CONTRAINDICATIONS",
    ]
    body_lines = [
        "1 INDICATIONS AND USAGE",
        "Tizanidine is indicated for the management of spasticity in adult patients.",
        "Treatment should be reserved for those daily activities and times when relief is needed.",
        "2 DOSAGE AND ADMINISTRATION",
        "The recommended starting dose is 4 mg every six to eight hours as needed.",
        "Doses should be increased slowly to avoid the known adverse reactions of this medicine.",
        "3 CONTRAINDICATIONS",
        "Do not use with potent inhibitors of the enzyme in patients with severe liver disease.",
        "Concomitant use is contraindicated and may cause clinically significant adverse reactions.",
    ]
    # Every header appears twice in each document: once as a contents
    # line, once as the section itself. The approved carries one extra
    # copy of the leaflet, so the n-th occurrence of a header no longer
    # lines up with the n-th on the other side -- which is exactly how a
    # contents line came to claim the real section, reporting its whole
    # body as deleted and the real body's as added.
    toc_approved = build_comparison_tokens(lines_to_words(body_lines + contents_lines + body_lines))
    toc_suspect = build_comparison_tokens(
        lines_to_words(contents_lines + [line.replace("4 mg", "8 mg") for line in body_lines])
    )

    sections_a = _split_sections(toc_approved)
    sections_b = _split_sections(toc_suspect)
    b_to_a, _unmatched_a, _unmatched_b = _match_sections(sections_a, sections_b)
    # A contents line is its header and nothing else; a real section
    # carries a body. Pairing one with the other is the bug.
    crossed = [
        (b_idx, a_idx)
        for b_idx, a_idx in b_to_a.items()
        if (len(sections_a[a_idx].tokens) <= 6) != (len(sections_b[b_idx].tokens) <= 6)
    ]
    if crossed:
        print(f"FAIL: {len(crossed)} contents line(s) paired with a real section: {crossed}")
        ok22 = False
    else:
        print("PASS: every contents line paired with a contents line, not with a section.")

    toc_items = diff_documents(toc_approved, toc_suspect)
    dose_rows = [
        d for d in toc_items
        if d.kind == "text_changed" and d.approved_text == "4" and d.suspect_text == "8"
    ]
    if len(dose_rows) == 1:
        print("PASS: the real dosage edit is one clean '4' -> '8' row.")
    else:
        print(f"FAIL: expected one '4' -> '8' row, got {[(d.kind, d.approved_text, d.suspect_text) for d in toc_items]}")
        ok22 = False

    # The suspect adds nothing (it is the approved minus one copy of the
    # leaflet, plus the dosage edit), and the copy it lacks is reported
    # as whole sections. The mispairing used to shred the dosage edit
    # into a deleted sentence plus an added one, and to report prose
    # that is identical on both sides as moved.
    added = [d for d in toc_items if d.kind == "added_in_suspect"]
    removed = [d for d in toc_items if d.kind == "removed_in_suspect"]
    partial = [d for d in removed if not d.approved_text[:1].isdigit()]
    # Prose echoed as "moved" is the mispairing symptom. A short title line
    # that genuinely sits elsewhere (the contents heading, first in the
    # suspect, after the extra copy in the approved) is reported as moved
    # with BOTH positions filled in, which is correct.
    echoed = [
        d for d in toc_items
        if d.kind == "reordered" and d.approved_text == d.suspect_text != ""
        and len(d.approved_text.split()) >= 8
    ]
    if not added and len(removed) == 3 and not partial and not echoed:
        print("PASS: the missing copy is 3 whole sections; nothing added, nothing falsely moved.")
    else:
        print(
            f"FAIL: {len(added)} added row(s), {len(removed)} removed row(s) "
            f"({len(partial)} mid-sentence), {len(echoed)} identical-text 'moved' row(s)."
        )
        ok22 = False

    if ok22:
        print("PASS: contents list vs. section pairing.")

    print()
    print("=" * 70)
    print("TEST 23: a paragraph is compared with its own counterpart, not")
    print("         with another paragraph that shares its boilerplate")
    print("=" * 70)
    ok23 = True
    from core.diff_engine import _identical_pairs  # _match_sections: imported at module level

    def paragraph_words(paragraphs, page=1):
        """One Word per token, paragraphs separated by a blank-line gap
        (what `_split_paragraphs` chunks on)."""
        words = []
        y = 50.0
        for para in paragraphs:
            for line in para:
                x = 50.0
                for tok in line.split():
                    words.append(
                        Word(text=tok, x0=x, x1=x + len(tok) * 5.0,
                             top=y, bottom=y + 10.0, page=page)
                    )
                    x += len(tok) * 5.0 + 4.0
                y += 12.0
            y += 22.0  # paragraph gap
        return words

    # The same three lines of storage boilerplate end both packaging
    # paragraphs -- the "same few lines" that let one paragraph claim
    # the other.
    boiler = [
        "Store below 25 C in a dry place away from direct sunlight and moisture.",
        "Keep this medicine out of the sight and reach of children at all times.",
        "Do not use this medicine after the expiry date printed on the carton.",
    ]
    unique_1 = ["This leaflet describes the treatment of adult patients with chronic spasticity."]
    unique_2 = ["The prescriber should review renal function before every change of the dose."]
    unique_3 = ["Report any suspected adverse reaction to the national pharmacovigilance system."]
    para_approved = [
        unique_1,
        ["Tablet presentation. The tablets are supplied in blister packs of thirty."] + boiler,
        unique_2,
        ["Suspension presentation. The suspension is supplied in cartons of ninety."] + boiler,
        unique_3,
    ]
    # The suspect rewrote the TABLET paragraph to describe the
    # suspension, so it now reads more like the OTHER paragraph than
    # like its own original (0.97 vs 0.89), and reworded the suspension
    # paragraph too, so neither side has an exact copy to fall back on.
    para_suspect = [
        unique_1,
        ["Suspension presentation. The suspension is supplied in cartons of ninety tablets."] + boiler,
        unique_2,
        ["Oral suspension pack. The product is supplied in cartons of ninety."] + boiler,
        unique_3,
    ]
    para_a = build_comparison_tokens(paragraph_words(para_approved))
    para_b = build_comparison_tokens(paragraph_words(para_suspect))

    chunks_a = _split_sections(para_a)
    chunks_b = _split_sections(para_b)
    if len(chunks_a) == len(chunks_b) == 5:
        print("PASS: both documents split into the same five paragraphs.")
    else:
        print(f"FAIL: paragraph split gave {len(chunks_a)} / {len(chunks_b)} chunks, expected 5 / 5.")
        ok23 = False

    # The three paragraphs that are untouched are identical and occur
    # once: they pair without being compared against anything.
    anchors = sorted(_identical_pairs([c.text_tokens for c in chunks_a],
                                      [c.text_tokens for c in chunks_b]))
    if anchors == [(0, 0), (2, 2), (4, 4)]:
        print("PASS: the unchanged paragraphs pair on identical text alone.")
    else:
        print(f"FAIL: identical-text pairs were {anchors}, expected [(0, 0), (2, 2), (4, 4)].")
        ok23 = False

    pairing = {b: a for b, a in _match_sections(chunks_a, chunks_b)[0].items()}
    if pairing == {0: 0, 1: 1, 2: 2, 3: 3, 4: 4}:
        print("PASS: each paragraph pairs with its own counterpart, in order.")
    else:
        print(f"FAIL: paragraphs paired {pairing} -- a paragraph claimed another one's partner.")
        ok23 = False

    para_items = diff_documents(para_a, para_b)
    texts = [(d.approved_text, d.suspect_text) for d in para_items]
    # Comparing the tablet paragraph against the suspension paragraph
    # produces this nonsense row, and claims a section move with it.
    crossed = [pair for pair in texts if pair == ("Tablet presentation.", "Oral suspension pack.")]
    moved = [d for d in para_items if d.kind == "reordered_section"]
    reported = any("Tablet" == a and "Suspension" == b for a, b in texts)
    if not crossed and not moved and reported:
        print("PASS: the rewritten paragraph is reported against its own original, "
              "and nothing is claimed as moved.")
    else:
        print(f"FAIL: crossed rows {crossed}, {len(moved)} 'moved' row(s), "
              f"own-counterpart row reported: {reported}")
        ok23 = False

    if ok23:
        print("PASS: paragraph-to-paragraph pairing.")

    print()
    print("=" * 70)
    print("TEST 24: rematching ONE discrepancy against the paragraph")
    print("         it belongs to (core.rematch -- the double-click)")
    print("=" * 70)
    ok24 = True
    from core.rematch import APPROVED as RM_APPROVED, RematchContext, rematch_item

    def paragraphs(blocks, page=1):
        """Tokens for a document given as a list of paragraphs."""
        words = []
        y = 50.0
        for block in blocks:
            for line in block:
                x = 50.0
                for tok in line.split():
                    words.append(
                        Word(text=tok, x0=x, x1=x + len(tok) * 5.0,
                             top=y, bottom=y + 10.0, page=page)
                    )
                    x += len(tok) * 5.0 + 4.0
                y += 12.0
            y += 24.0
        return build_comparison_tokens(words)

    def claim_removed(tokens, text):
        """A row claiming `text` was removed -- the shape a bad pairing
        produces, and what a reviewer double-clicks."""
        words = text.split()
        texts = [t.text for t in tokens]
        start_at = next(
            i for i in range(len(texts) - len(words) + 1) if texts[i:i + len(words)] == words
        )
        span = tokens[start_at:start_at + len(words)]
        return DiffItem(
            index=0, kind="removed_in_suspect", severity="critical",
            approved_text=text, suspect_text="",
            approved_location=_location_of(span), suspect_location=None,
        )

    intro = ["This leaflet describes the treatment of adult patients with spasticity."]
    dose = ["The recommended starting dose is 4 mg daily taken with the evening meal."]
    dose_edited = ["The recommended starting dose is 8 mg daily taken with the evening meal."]
    store = ["Store below 25 C in a dry place away from direct sunlight and moisture."]
    report = ["Report any suspected adverse reaction to the national pharmacovigilance centre."]

    rm_a = paragraphs([intro, dose, store, report])
    rm_b = paragraphs([intro, dose_edited, store, report])
    rm_context = RematchContext(rm_a, rm_b)

    # (a) A row claiming an UNCHANGED paragraph was removed: comparing
    #     it with the paragraph it is paired with shows nothing, so the
    #     row goes away. This is the false positive being rectified.
    result = rematch_item(claim_removed(rm_a, " ".join(intro)), rm_a, rm_b,
                          side=RM_APPROVED, context=rm_context)
    if result.found and result.items == []:
        print("PASS: a difference that its own paragraph disproves is removed.")
    else:
        print(f"FAIL: unchanged paragraph rematched to "
              f"{[(i.kind, i.suspect_text) for i in result.items]} ({result.message})")
        ok24 = False

    # (b) The paragraph really was edited: the edit itself comes back,
    #     not the whole paragraph.
    result = rematch_item(claim_removed(rm_a, " ".join(dose)), rm_a, rm_b,
                          side=RM_APPROVED, context=rm_context)
    if result.found and [(i.approved_text, i.suspect_text) for i in result.items] == [("4", "8")]:
        print("PASS: an edited paragraph reports just its edit.")
    else:
        print(f"FAIL: edited paragraph rematched to "
              f"{[(i.approved_text, i.suspect_text) for i in result.items]}")
        ok24 = False

    # (c) Position governs over text. The suspect repeats the dosage
    #     paragraph VERBATIM at the end while the copy in its own place
    #     is the edited one: a word-for-word match elsewhere must not
    #     beat the paragraph that stands where this one stands, or the
    #     dosage change would be declared "no difference".
    rm_b_twice = paragraphs([intro, dose_edited, store, report, dose])
    twice_context = RematchContext(rm_a, rm_b_twice)
    result = rematch_item(claim_removed(rm_a, " ".join(dose)), rm_a, rm_b_twice,
                          side=RM_APPROVED, context=twice_context)
    if result.found and [(i.approved_text, i.suspect_text) for i in result.items] == [("4", "8")]:
        print("PASS: the paragraph at the same position wins over an identical copy elsewhere.")
    else:
        print(f"FAIL: positional pairing lost to a copy elsewhere: "
              f"{[(i.approved_text, i.suspect_text) for i in result.items]} ({result.message})")
        ok24 = False

    # (d) A paragraph the suspect genuinely dropped: nothing in that
    #     part of the document is a counterpart, so the row stands.
    rm_b_short = paragraphs([intro, store, report])
    short_context = RematchContext(rm_a, rm_b_short)
    original = claim_removed(rm_a, " ".join(dose))
    result = rematch_item(original, rm_a, rm_b_short, side=RM_APPROVED, context=short_context)
    if not result.found and result.items == [original] and "genuine" in result.message:
        print("PASS: a genuine deletion is confirmed and left untouched.")
    else:
        print(f"FAIL: genuine deletion rematched to {result.items} ({result.message})")
        ok24 = False

    # (e) A one-word difference is rematched through its PARAGRAPH, never
    #     as a bare word: the dosage change is checked again and stands,
    #     however many stray "8"s the suspect has elsewhere.
    tiny = DiffItem(
        index=0, kind="text_changed", severity="critical",
        approved_text="4", suspect_text="8",
        approved_location=_location_of([t for t in rm_a if t.text == "4"][:1]),
        suspect_location=None,
    )
    rm_b_eights = paragraphs([intro, dose_edited, store, report, ["Take 8 tablets with 4 mg of 8 water."]])
    result = rematch_item(tiny, rm_a, rm_b_eights, side=RM_APPROVED, context=RematchContext(rm_a, rm_b_eights))
    if not result.found and result.items == [tiny] and "stands" in result.message:
        print("PASS: a one-word difference is checked through its paragraph and stands.")
    else:
        print(f"FAIL: short rematch gave found={result.found} {result.message!r}")
        ok24 = False

    # (f) A two-word false positive -- a short phrase paired with the
    #     wrong words -- is found by the words around it and goes away.
    meal = [t for t in rm_a if t.text in ("evening", "meal.")]
    wrong = DiffItem(
        index=0, kind="text_changed", severity="critical",
        approved_text="evening meal.", suspect_text="dry place",
        approved_location=_location_of(meal), suspect_location=None,
    )
    result = rematch_item(wrong, rm_a, rm_b, side=RM_APPROVED, context=rm_context)
    in_middle = DiffItem(
        index=0, kind="text_changed", severity="critical",
        approved_text="starting dose", suspect_text="dose",
        approved_location=_location_of([t for t in rm_a if t.text in ("starting", "dose")][:2]),
        suspect_location=None,
    )
    middle = rematch_item(in_middle, rm_a, rm_b, side=RM_APPROVED, context=rm_context)
    if result.found and result.items == [] and middle.found and middle.items == []:
        print("PASS: a two-word mispairing is placed by its neighbours and removed.")
    else:
        print(f"FAIL: two-word mispairing rematched to {[(i.approved_text, i.suspect_text) for i in result.items]} "
              f"/ {[(i.approved_text, i.suspect_text) for i in middle.items]} ({result.message} | {middle.message})")
        ok24 = False

    # (g) Areas drawn by the reviewer: exactly those two passages are
    #     compared; rows touching them are replaced, and words outside
    #     them that lose their partner are reported, never dropped.
    from core.rematch import rematch_areas
    from core.token_map import TokenMap as _TokenMap

    def area_of(tokens, text):
        words = text.split()
        texts = [t.text for t in tokens]
        start_at = next(i for i in range(len(texts)) if texts[i:i + len(words)] == words)
        loc = _location_of(tokens[start_at:start_at + len(words)])
        x0, top, x1, bottom = loc.bbox
        return [(loc.page, (x0 - 3, top - 3, x1 + 3, bottom + 3))]

    area_matches = []
    area_rows = diff_documents(rm_a, rm_b, area_matches)
    area_map = _TokenMap(rm_a, rm_b, area_matches)
    right = rematch_areas(area_rows, rm_a, rm_b, area_of(rm_a, dose[0]), area_of(rm_b, dose_edited[0]),
                          area_map.partners())
    wrong_area = rematch_areas(area_rows, rm_a, rm_b, area_of(rm_a, dose[0]), area_of(rm_b, store[0]),
                               area_map.partners())
    nothing = rematch_areas(area_rows, rm_a, rm_b, [(1, (500.0, 700.0, 520.0, 720.0))],
                            area_of(rm_b, store[0]), area_map.partners())
    # Pairing the dose with the storage paragraph leaves the whole edited
    # dose paragraph (its "8" included: its row was replaced) and the
    # whole Original storage paragraph without a partner.
    expected_unpaired = len(dose_edited[0].split()) + len(store[0].split())
    if (right.ok and [(i.approved_text, i.suspect_text) for i in right.items] == [("4", "8")]
            and right.replaced == area_rows and right.unpaired == 0
            and wrong_area.ok and wrong_area.unpaired == expected_unpaired and wrong_area.replaced == area_rows
            and not nothing.ok and "Original" in nothing.message):
        print("PASS: two drawn areas are compared with each other; unpaired words are reported.")
    else:
        print(f"FAIL: area rematch right={[(i.approved_text, i.suspect_text) for i in right.items]} "
              f"unpaired {right.unpaired}/{wrong_area.unpaired} (expected {expected_unpaired}), "
              f"empty area ok={nothing.ok}")
        ok24 = False

    # Words left unpaired are marked at their insertion point in the
    # OTHER document -- never by mirroring their own boxes onto it, which
    # lands on unrelated text there.
    one_sided = [i for i in wrong_area.items if bool(i.approved_text) != bool(i.suspect_text)]
    placed = all((i.suspect_caret if i.approved_text else i.approved_caret) is not None for i in one_sided)
    if one_sided and placed:
        print("PASS: every word left unpaired by an area rematch has its insertion point in the other document.")
    else:
        print(f"FAIL: unpaired rows without an insertion point: "
              f"{[(i.approved_text, i.suspect_text) for i in one_sided if not placed]}")
        ok24 = False


    snap = area_map.snapshot()
    area_map.rebind(right.selected["approved"], right.selected["suspect"], right.pairs)
    rebound = all(area_map.partners()["approved"][i] == j for i, j in right.pairs)
    area_map.restore(snap)
    if rebound and area_map.partners() == snap:
        print("PASS: the word pairing follows an area rematch, and is put back on undo.")
    else:
        print("FAIL: TokenMap rebind/restore")
        ok24 = False

    if ok24:
        print("PASS: single-discrepancy rematch.")

    print()
    print("=" * 70)
    print("TEST 25: Moved text is matched -- and never swallows a real difference")
    print("=" * 70)
    ok25 = True
    from core.completeness import unaccounted_rows as _unaccounted25
    from core.token_map import TokenMap as _TM25

    def compare25(lines_a, lines_b):
        ta25 = build_comparison_tokens(lines_to_words(lines_a))
        tb25 = build_comparison_tokens(lines_to_words(lines_b))
        pairs25: list = []
        rows25 = diff_documents(ta25, tb25, pairs25)
        return ta25, tb25, pairs25, rows25

    intro = ["This leaflet describes the treatment of adult patients with spasticity."]
    outro = ["Report any suspected adverse reaction to the national centre listed here."]
    long_para = [
        "The recommended starting dose is 4 mg given once daily in the evening.",
        "Doses may be increased gradually in increments of 2 mg to achieve the",
        "optimum balance of relief and the known adverse reactions of this drug.",
    ]

    # 1. A paragraph that only moved: matched, nothing reported.
    ta, tb, pairs, rows = compare25(intro + long_para + outro, intro + outro + long_para)
    paired_words = sum(1 for x, y in pairs if x.text == y.text)
    if not rows and paired_words == len(ta):
        print("PASS: a paragraph that only moved is matched word for word, no row.")
    else:
        print(f"FAIL: moved paragraph: rows {[(d.kind, d.approved_text[:30]) for d in rows]}, "
              f"paired {paired_words}/{len(ta)}")
        ok25 = False

    # 2. An edit inside the moved paragraph is reported, at its own severity
    #    -- and is the only row.
    long_edited = [line.replace("is 4 mg", "is 8 mg") for line in long_para]
    ta, tb, pairs, rows = compare25(intro + long_para + outro, intro + outro + long_edited)
    if [(d.severity, d.approved_text, d.suspect_text) for d in rows] == [("critical", "4", "8")]:
        print("PASS: a dosage change inside a paragraph that moved is the one row, critical.")
    else:
        print(f"FAIL: edit inside moved text: {[(d.severity, d.approved_text[:20], d.suspect_text[:20]) for d in rows]}")
        ok25 = False

    # 3. A sentence printed twice in one document and once in the other:
    #    the second copy is a real deletion, even though the same words are
    #    elsewhere -- that copy is already matched.
    warning = ["Do not drive or operate machinery until you know how this medicine affects you."]
    ta, tb, pairs, rows = compare25(intro + warning + long_para + warning + outro, intro + warning + long_para + outro)
    deleted = [d for d in rows if d.kind == "removed_in_suspect"]
    if len(rows) == 1 and deleted and deleted[0].severity == "critical" and "operate machinery" in deleted[0].approved_text:
        print("PASS: the second copy of a repeated sentence, deleted, is reported -- not called moved.")
    else:
        print(f"FAIL: deleted repeat: {[(d.kind, d.severity, d.approved_text[:30]) for d in rows]}")
        ok25 = False

    # 4. Moved AND re-cased: one row setting the two places side by side,
    #    rated as the change it is (capitalization), not two criticals and
    #    not nothing.
    ta, tb, pairs, rows = compare25(
        intro + ["KEEP OUT OF THE REACH OF CHILDREN"] + long_para + outro,
        intro + long_para + ["Keep out of the reach of children"] + outro,
    )
    if (len(rows) == 1 and rows[0].kind == "text_changed" and rows[0].severity == "major"
            and rows[0].approved_location and rows[0].suspect_location):
        print("PASS: text moved and re-cased is one capitalization row with both places.")
    else:
        print(f"FAIL: moved+re-cased: {[(d.kind, d.severity, d.approved_text, d.suspect_text) for d in rows]}")
        ok25 = False

    # 5. A short phrase shared by two DIFFERENT sentences is not a move:
    #    both sentences stay reported whole.
    ta, tb, pairs, rows = compare25(
        intro + ["Monitor the blood pressure of the patient every week."] + long_para + outro,
        intro + long_para + ["Reduce the dose of the patient with kidney disease."] + outro,
    )
    removed = " ".join(d.approved_text for d in rows if d.kind == "removed_in_suspect")
    added = " ".join(d.suspect_text for d in rows if d.kind == "added_in_suspect")
    if "blood pressure of the patient" in removed and "dose of the patient with" in added:
        print("PASS: a phrase shared by two different sentences is not paired as moved.")
    else:
        print(f"FAIL: shared phrase: {[(d.kind, d.approved_text, d.suspect_text) for d in rows]}")
        ok25 = False

    # 6. A replacement is never undone by the move pass, even when both of
    #    its sides occur elsewhere: "firmly" and "gently" traded places
    #    between two DIFFERENT sentences -- two real edits. (Two identical
    #    lines that only swapped order are a move: nothing to report.)
    ta, tb, pairs, rows = compare25(
        ["Hold the syringe firmly in one hand.", "Shake the pen gently before each use."],
        ["Hold the syringe gently in one hand.", "Shake the pen firmly before each use."],
    )
    edits = {(d.approved_text, d.suspect_text) for d in rows if d.kind == "text_changed" and d.severity == "critical"}
    if ("firmly", "gently") in edits and ("gently", "firmly") in edits:
        print("PASS: a replacement stays an edit even when both sides occur elsewhere.")
    else:
        print(f"FAIL: replacements: {[(d.kind, d.severity, d.approved_text, d.suspect_text) for d in rows]}")
        ok25 = False

    # 7. A one- or two-word row that moved is matched (shown green) when the
    #    pair cannot be chance -- a real word, the only such row deleted and
    #    the only such row inserted; a lone "and" moved stays two rows.
    ta, tb, pairs, rows = compare25(
        intro + ["Total cholesterol"] + long_para + outro,
        intro + long_para + ["Total cholesterol"] + outro,
    )
    map25 = _TM25(ta, tb, pairs)
    listed = _unaccounted25(rows, map25, len(rows))
    total25 = [k for k, t in enumerate(ta) if t.text in ("Total", "cholesterol")]
    ta2, tb2, pairs2, rows2 = compare25(intro + ["and"] + long_para + outro, intro + long_para + ["and"] + outro)
    map25b = _TM25(ta2, tb2, pairs2)
    rows2 = rows2 + _unaccounted25(rows2, map25b, len(rows2))
    if (not rows and not listed and all(map25.confirmed("approved", k) for k in total25)
            and sorted(d.kind for d in rows2) == ["added_in_suspect", "removed_in_suspect"]):
        print("PASS: a distinctive two-word move is matched; a lone 'and' moved is a deletion and an insertion.")
    else:
        print(f"FAIL: short moves: rows {[(d.kind, d.approved_text) for d in rows]}, "
              f"listed {[(d.kind, d.approved_text) for d in listed]}, 'and' {[(d.kind, d.approved_text, d.suspect_text) for d in rows2]}")
        ok25 = False

    # 8. Re-cased words more than a few words apart inside a moved passage,
    #    and an edit at its start: the stretches between and before are
    #    compared too -- two capitalization changes and the deleted words,
    #    not every word of them as critical insertions and deletions.
    howto_a = ["Know the medicines you take and keep a list of them",
               "How should I take Sildenafil tablets? • Take or give Sildenafil",
               "tablets exactly as your healthcare provider tells you to every day"]
    howto_b = [line.replace("Sildenafil", "sildenafil") for line in howto_a]
    howto_a[0] = howto_a[0].replace("you take", "you or your child take")
    long_intro = intro * 3
    long_outro = outro * 3
    ta, tb, pairs, rows = compare25(long_intro + long_outro + howto_a, long_intro + howto_b + long_outro)
    if sorted((d.kind, d.severity, d.approved_text, d.suspect_text) for d in rows) == [
            ("removed_in_suspect", "critical", "or your child", "")] + [
            ("text_changed", "major", "Sildenafil", "sildenafil")] * 2:
        print("PASS: edits apart inside moved text are each their own row, nothing else.")
    else:
        print(f"FAIL: re-cased inside moved text: {[(d.kind, d.severity, d.approved_text, d.suspect_text) for d in rows]}")
        ok25 = False

    # 9. A phrase in two moved sentences goes to the copy it matches
    #    longest: the sentence present in both is matched whole, not
    #    reported deleted because the other sentence took its first words.
    risk_a = ["the worsening of your physical condition. It is not known if sildenafil tablets are safe and effective.",
              "are pregnant or plan to become pregnant. It is not known if sildenafil tablets will harm your unborn baby."]
    risk_b = ["exercise and lung testing. It is not known if Sildenafil tablets are safe and effective.", risk_a[1]]
    ta, tb, pairs, rows = compare25(long_intro + long_outro + risk_a, long_intro + risk_b + long_outro)
    if rows and not any("harm" in d.approved_text + d.suspect_text or "pregnant" in d.approved_text + d.suspect_text
                        for d in rows):
        print("PASS: a phrase in two moved sentences is matched with the copy it matches longest.")
    else:
        print(f"FAIL: longest copy: {[(d.kind, d.approved_text, d.suspect_text) for d in rows]}")
        ok25 = False

    if ok25:
        print("PASS: moved text is matched; every real difference around and inside it is reported.")

    print()
    print("=" * 70)
    print("TEST 26: ruling lines over a paragraph are not a table")
    print("=" * 70)
    ok26 = True
    from core.pdf_extractor import _cuts_through_words

    # A proof sheet's box art draws rules across flowing text.
    # `find_tables` turns them into a grid, and reading that grid cell
    # by cell interleaves the lines -- the real case was a leaflet's
    # "Reporting of side effects" paragraph coming out as "This
    # includes any the Yellow Play or possible Card Apple App side
    # Scheme Store.".
    ruled_path = os.path.join(tmp, "ruled_paragraph.pdf")
    ruled_lines = [
        "This includes any possible side effects not listed in this",
        "leaflet. You can also report side effects directly via the",
        "Yellow Card Scheme at: www.mhra.gov.uk/yellowcard today.",
    ]
    ruled = fitz.open()
    ruled_page = ruled.new_page(width=400, height=200)
    y = 60
    for line in ruled_lines:
        ruled_page.insert_text((40, y), line, fontsize=10)
        y += 14
    for x in (120.0, 200.0, 280.0):  # rules straight through the words
        ruled_page.draw_line(fitz.Point(x, 40), fitz.Point(x, 110), width=0.4)
    ruled_page.draw_line(fitz.Point(40, 40), fitz.Point(360, 40), width=0.4)
    ruled_page.draw_line(fitz.Point(40, 110), fitz.Point(360, 110), width=0.4)
    ruled.save(ruled_path)
    ruled.close()

    ruled_text = " ".join(w.text for w in extract_words(ruled_path))
    expected_text = " ".join(" ".join(line.split()) for line in ruled_lines)
    if ruled_text == expected_text:
        print("PASS: a paragraph crossed by ruling lines still reads line by line.")
    else:
        print(f"FAIL: ruled paragraph read as {ruled_text!r}")
        ok26 = False

    # The check itself: a grid whose edges cut words is rejected, a
    # grid whose edges run between columns is kept.
    def cell(x0, top, x1, bottom):
        return (x0, top, x1, bottom)

    cutting = {
        "bbox": (40.0, 40.0, 360.0, 110.0),
        "cells": [[cell(40, 40, 120, 110), cell(120, 40, 200, 110), cell(200, 40, 360, 110)]],
    }
    flowing_words = [
        {"text": "possible", "x0": 100.0, "x1": 140.0, "top": 60.0, "bottom": 70.0},
        {"text": "effects", "x0": 150.0, "x1": 190.0, "top": 60.0, "bottom": 70.0},
        {"text": "listed", "x0": 190.0, "x1": 215.0, "top": 60.0, "bottom": 70.0},
        {"text": "directly", "x0": 260.0, "x1": 300.0, "top": 74.0, "bottom": 84.0},
        {"text": "via", "x0": 305.0, "x1": 320.0, "top": 74.0, "bottom": 84.0},
    ]
    real_table_words = [
        {"text": "Hypotension", "x0": 45.0, "x1": 110.0, "top": 60.0, "bottom": 70.0},
        {"text": "12", "x0": 130.0, "x1": 140.0, "top": 60.0, "bottom": 70.0},
        {"text": "Sedation", "x0": 45.0, "x1": 100.0, "top": 74.0, "bottom": 84.0},
        {"text": "48", "x0": 130.0, "x1": 140.0, "top": 74.0, "bottom": 84.0},
        {"text": "Dizziness", "x0": 210.0, "x1": 265.0, "top": 60.0, "bottom": 70.0},
    ]
    if _cuts_through_words(cutting, flowing_words):
        print("PASS: a grid whose edges run through words is rejected.")
    else:
        print("FAIL: word-cutting grid accepted as a table.")
        ok26 = False
    if not _cuts_through_words(cutting, real_table_words):
        print("PASS: a grid whose edges run between the columns is kept.")
    else:
        print("FAIL: a genuine table was rejected.")
        ok26 = False

    if ok26:
        print("PASS: ruling lines vs. real tables.")

    print()
    print("=" * 70)
    print("TEST 27: letter-by-letter comparison of one difference")
    print("=" * 70)
    ok27 = True
    from core.char_compare import (
        align_characters,
        character_changes,
        classify,
        describe,
        summary_note,
    )

    # The two texts line up character under character, so a UI can
    # print one above the other with the differences in the same
    # columns. A character on one side only leaves its column empty.
    columns = align_characters("Ropinirole", "Ropinirol")
    aligned_ok = (
        len(columns) == 10
        and all(not changed for *_pair, changed in columns[:9])
        and columns[9] == ("e", "", True)
    )
    columns_date = align_characters("dd/mm/yy", "06/01/25")
    slashes_line_up = [c for c in columns_date if not c[2]] == [("/", "/", False)] * 2
    if aligned_ok and slashes_line_up:
        print("PASS: the two texts line up character under character.")
    else:
        print(f"FAIL: alignment was {columns!r} / {columns_date!r}")
        ok27 = False

    # Every differing character is identified by code point and name --
    # the whole point of looking letter by letter.
    described = describe("E")
    if described == "'E' U+0045 LATIN CAPITAL LETTER E":
        print("PASS: a character is named with its code point and Unicode name.")
    else:
        print(f"FAIL: describe('E') gave {described!r}")
        ok27 = False
    if describe(" ") == "(no-break space) U+00A0 NO-BREAK SPACE":
        print("PASS: an invisible character is named rather than printed blank.")
    else:
        print(f"FAIL: describe(nbsp) gave {describe(chr(0xa0))!r}")
        ok27 = False

    # The classification IS the finding, so each kind has to be right.
    kinds = {
        ("E", "e"): "upper to lower case",
        ("e", "E"): "lower to upper case",
        ("2", "3"): "digit changed",
        ("d", "0"): "letter changed to a digit",
        ("é", "e"): "accent changed",
        ("a", "а"): "look-alike letter: cyrillic instead of latin",
        (" ", " "): "space changed to no-break space",
        (",", ";"): "punctuation changed",
        ("e", ""): "letter removed",
        ("", "s"): "letter added",
    }
    wrong = {pair: classify(*pair) for pair, want in kinds.items() if classify(*pair) != want}
    if not wrong:
        print("PASS: case, digit, accent, look-alike, spacing and punctuation changes "
              "are each named correctly.")
    else:
        print(f"FAIL: classified wrongly: {wrong}")
        ok27 = False

    # A Cyrillic 'a' in "paracetamol" is identical on the page and
    # wrong in the file: the one error reading cannot catch.
    lookalike = character_changes("paracetamol", "parаcetamol")
    if (
        len(lookalike) == 1
        and "look-alike" in lookalike[0].note
        and describe(lookalike[0].suspect).endswith("CYRILLIC SMALL LETTER A")
    ):
        print("PASS: a look-alike letter from another alphabet is caught and named.")
    else:
        print(f"FAIL: look-alike detection gave {[(c.approved, c.suspect, c.note) for c in lookalike]}")
        ok27 = False

    # The one-line summary leads with the most serious kind.
    mixed = character_changes("Dose 25 mg", "dose 30 mg")
    if summary_note(mixed).startswith("digit changed"):
        print("PASS: the summary names the most serious kind of change first.")
    else:
        print(f"FAIL: summary was {summary_note(mixed)!r}")
        ok27 = False

    if ok27:
        print("PASS: letter-by-letter comparison.")

    print()
    print("=" * 70)
    print("TEST 28: differences are reported as they are found")
    print("=" * 70)
    ok28 = True

    # The deviation list fills while the comparison runs instead of
    # appearing all at once at the end, so the engine hands out each
    # section's differences as it compares it.
    stream_a = build_comparison_tokens(lines_to_words([
        "1 INDICATIONS AND USAGE",
        "This medicine is used for the short term relief of muscle spasm.",
        "2 DOSAGE AND ADMINISTRATION",
        "The recommended starting dose is 4 mg taken three times a day.",
        "3 CONTRAINDICATIONS",
        "Do not use with potent inhibitors of the enzyme in liver disease.",
    ]))
    stream_b = build_comparison_tokens(lines_to_words([
        "1 INDICATIONS AND USAGE",
        "This medicine is used for the long term relief of muscle spasm.",
        "2 DOSAGE AND ADMINISTRATION",
        "The recommended starting dose is 8 mg taken three times a day.",
        "3 CONTRAINDICATIONS",
        "Do not use with potent inhibitors of the enzyme in renal disease.",
    ]))
    batches: list = []
    streamed_final = diff_documents(stream_a, stream_b, None, on_found=batches.append)
    streamed = [item for batch in batches for item in batch]
    if batches and len(batches) >= 2:
        print(f"PASS: differences arrive in {len(batches)} batches as the sections are compared.")
    else:
        print(f"FAIL: everything arrived in {len(batches)} batch(es).")
        ok28 = False

    # What is streamed has to be what the list ends up with, or rows
    # would appear and then be taken away again.
    streamed_texts = sorted((d.approved_text, d.suspect_text) for d in streamed)
    final_texts = sorted((d.approved_text, d.suspect_text) for d in streamed_final)
    if streamed_texts == final_texts:
        print("PASS: the differences shown while comparing are the ones that remain.")
    else:
        print(f"FAIL: streamed {streamed_texts} but ended with {final_texts}")
        ok28 = False

    # Spacing artifacts are dropped from the final results, so they must
    # not be shown on the way either -- a real pair streamed 135 rows
    # that settled to 34 before this.
    artifact_a = build_comparison_tokens(lines_to_words([
        "1 INDICATIONS AND USAGE",
        "The study identi ed a speci c and signi cant effect in the patients.",
    ]))
    artifact_b = build_comparison_tokens(lines_to_words([
        "1 INDICATIONS AND USAGE",
        "The study identied a specic and significant effect in the patients.",
    ]))
    artifact_batches: list = []
    artifact_final = diff_documents(artifact_a, artifact_b, None, on_found=artifact_batches.append)
    artifact_streamed = [item for batch in artifact_batches for item in batch]
    if len(artifact_streamed) == len(artifact_final):
        print("PASS: spacing artifacts are left out of the stream, as they are of the result.")
    else:
        print(f"FAIL: streamed {len(artifact_streamed)} rows for a final {len(artifact_final)}.")
        ok28 = False

    # Without a listener nothing changes.
    if diff_documents(stream_a, stream_b) == streamed_final:
        print("PASS: the results are the same whether or not anyone is listening.")
    else:
        print("FAIL: streaming changed the result.")
        ok28 = False

    if ok28:
        print("PASS: progressive reporting.")

    print()
    print("=" * 70)
    print("TEST 29: a changed or missing dose is never written off as minor")
    print("=" * 70)
    ok29 = True
    from core.diff_engine import _classify_severity as _sev29, category_of as _cat29

    def toks29(text):
        return build_comparison_tokens(lines_to_words([text]))

    # Ordinary label lines that the chart-artwork rule used to treat as
    # plot labels: deleting any of them must be critical.
    label_lines = [
        "Each tablet contains 20 mg", "Dose 5 mg twice daily", "Contains 100 mg per 5 ml",
        "Inject 0.5 ml subcutaneously", "Dilute with 5 ml water", "Batch 1234 Exp 06 2027",
        "Take 2 tablets 3 times", "10 tablets of 20 mg", "2 x 10 tablets",
        "Discard needle", "single-use vial", "Replace the cap", "no", "NO", "off",
        "severe hepatic impairment reduce dose",
    ]
    demoted = [line for line in label_lines if _sev29("delete", toks29(line), []) != "critical"]
    if demoted:
        print(f"FAIL: deleted label lines rated below critical: {demoted}")
        ok29 = False
    else:
        print(f"PASS: all {len(label_lines)} deleted label lines are critical.")

    carton_a = ["ESMOLOC 20", "Each tablet contains 20 mg", "Dose 5 mg twice daily",
                "Contains 100 mg per 5 ml", "Batch 1234 Exp 06 2027", "Keep out of reach of children"]
    carton_b = ["ESMOLOC 20", "Keep out of reach of children"]
    carton = diff_documents(build_comparison_tokens(lines_to_words(carton_a)),
                            build_comparison_tokens(lines_to_words(carton_b)))
    if carton and all(d.severity == "critical" for d in carton):
        print("PASS: a carton losing its strength, dose, batch and expiry lines is critical.")
    else:
        print(f"FAIL: carton deletion: {[(d.severity, d.approved_text) for d in carton]}")
        ok29 = False

    # Chart text the rule was built for still counts as figure text.
    for text in ("0 10 20 30 40 50 60", "SLOW!"):
        if _sev29("delete", toks29(text), []) != "minor":
            print(f"FAIL: figure text {text!r} is no longer recognized.")
            ok29 = False

    # One short artwork-looking side must not drag a long change down.
    stamp_pair = _sev29("replace", toks29("risk due to their age and documented history of gastric ulcers "
                                          "controlled studies do not extend beyond 6 months"),
                        toks29("4:26 pm, Sep 05, 2023"))
    if stamp_pair != "critical":
        print(f"FAIL: a passage paired with a date stamp was rated {stamp_pair}.")
        ok29 = False

    expectations = [
        ("hypertension", "hypotension", "critical", "wording"),
        ("hyperkalemia", "hypokalemia", "critical", "wording"),
        ("should", "shouldn't", "critical", "wording"),
        ("compatible", "incompatible", "critical", "wording"),
        ("10 mg/kg", "10 mg/g", "critical", "wording"),
        ("5.000", "5,000", "critical", "number"),
        ("1.5", "1,5", "critical", "number"),
        ("contraindicated", "contrainsdicated", "major", "spelling"),
        ("MATRIX", "MATRX", "major", "spelling"),
        ("especially", "especial y", "typo", "spelling"),
        ("specifically", "speci cally", "typo", "spelling"),
        ("first", "rst", "typo", "spelling"),
        ("2.3, 5.1,", "2.3,5.1,", "minor", "spacing"),
        ("100 mg", "100mg", "minor", "spacing"),
        ("2633 patients", "2,633 patients", "minor", "number"),
        ("2,633", "2,663", "critical", "number"),
        ("1,500", "1.5", "critical", "number"),
        ("ο", "○", "minor", "punctuation"),
        ("ο", "○ ○", "minor", "punctuation"),
        ("ο", "η", "critical", "wording"),
        ("a, b", "a b", "minor", "punctuation"),
    ]
    wrong = []
    for approved, suspect, severity, category in expectations:
        got = _sev29("replace", toks29(approved), toks29(suspect))
        cat = _cat29(DiffItem(0, "text_changed", got, approved, suspect, None, None))
        if (got, cat) != (severity, category):
            wrong.append((approved, suspect, got, cat))
    if wrong:
        print(f"FAIL: severity/category: {wrong}")
        ok29 = False
    else:
        print(f"PASS: {len(expectations)} meaning, number, spelling and spacing changes rated correctly.")

    spacing = diff_documents(toks29("Take 100 mg daily"), toks29("Take 100mg daily"))
    pieces = diff_documents(toks29("used specifically for"), toks29("used speci fically for"))
    if len(spacing) == 1 and spacing[0].severity == "minor" and not pieces:
        print("PASS: a space lost next to a number is reported; a word drawn in pieces is not.")
    else:
        print(f"FAIL: spacing rows {[(d.approved_text, d.suspect_text) for d in spacing]}, "
              f"split-word rows {[(d.approved_text, d.suspect_text) for d in pieces]}")
        ok29 = False
    if ok29:
        print("PASS: severity ladder.")

    print()
    print("=" * 70)
    print("TEST 30: text counts as moved only when it really is elsewhere")
    print("=" * 70)
    ok30 = True
    moved_a = ["Patients should not take this medicine with alcohol.",
               "Store the tablets below 25 degrees.",
               "Patients may take this medicine with water or with juice.",
               "Patients should not drive if they feel drowsy."]
    moved_b = moved_a[1:] + ["Patients should not share this medicine with others and should not",
                             "take it with grapefruit juice or with alcohol free drinks."]
    rows30 = diff_documents(build_comparison_tokens(lines_to_words(moved_a)),
                            build_comparison_tokens(lines_to_words(moved_b)))
    gone = [d for d in rows30 if "alcohol." in d.approved_text]
    if gone and gone[0].severity == "critical" and gone[0].kind == "removed_in_suspect":
        print("PASS: a deleted warning whose words occur elsewhere is still a critical deletion.")
    else:
        print(f"FAIL: deleted warning: {[(d.severity, d.kind, d.approved_text) for d in rows30]}")
        ok30 = False

    swap_a = ["Intro line one here.", "Keep out of reach and sight of children always.",
              "Middle text continues here.", "Store in the original package to protect from light."]
    swap_b = ["Intro line one here.", "Middle text continues here.",
              "Store in the original package to protect from light.",
              "Keep out of reach and sight of children always."]
    rows30b = diff_documents(build_comparison_tokens(lines_to_words(swap_a)),
                             build_comparison_tokens(lines_to_words(swap_b)))
    if not rows30b:
        print("PASS: a sentence that truly moved is matched where it stands -- no row.")
    else:
        print(f"FAIL: moved sentence: {[(d.kind, d.approved_text, d.suspect_text) for d in rows30b]}")
        ok30 = False

    rx_a = ["Ropinirole Tablets 2 mg", "Manufactured by Acme"]
    rx_b = ["Ropinirole Tablets 2 mg", "S Rx Only POM", "Manufactured by Acme"]
    rows30c = diff_documents(build_comparison_tokens(lines_to_words(rx_a)),
                             build_comparison_tokens(lines_to_words(rx_b)))
    if [(d.severity, d.kind) for d in rows30c] == [("critical", "added_in_suspect")]:
        print("PASS: an added legal-status line found nowhere in the original is critical, not moved.")
    else:
        print(f"FAIL: added legal status: {[(d.severity, d.kind, d.suspect_text) for d in rows30c]}")
        ok30 = False
    if ok30:
        print("PASS: text counts as moved only when it really is elsewhere.")

    print()
    print("=" * 70)
    print("TEST 31: what could not be read is said, and every format is read whole")
    print("=" * 70)
    ok31 = True
    from core.coverage_check import coverage_is_serious, coverage_warnings
    from core.document_extractor import (
        _extract_docx_words, _extract_pptx_words, _extract_xlsx_words, extract_document_tokens,
    )

    full = os.path.join(tmp, "cov_full.pdf")
    stamp = os.path.join(tmp, "cov_stamp.pdf")
    doc31 = fitz.open()
    for _ in range(2):
        page31 = doc31.new_page(width=400, height=600)
        page31.insert_textbox(fitz.Rect(36, 36, 364, 564),
                              " ".join(f"word{i}" for i in range(160)), fontsize=9, fontname="helv")
    doc31.save(full); doc31.close()
    doc31 = fitz.open()
    page31 = doc31.new_page(width=400, height=600)
    page31.insert_text((36, 40), "By Packaging Development at 4:26 pm", fontsize=9, fontname="helv")
    doc31.new_page(width=400, height=600)
    doc31.save(stamp); doc31.close()
    full_tokens, stamp_tokens = extract_document_tokens(full), extract_document_tokens(stamp)
    warnings31 = coverage_warnings(full, stamp, full_tokens, stamp_tokens)
    if (coverage_is_serious(full_tokens, stamp_tokens)
            and any("yielded only" in w for w in warnings31)
            and any("page 2 has no extractable text" in w for w in warnings31)
            and not coverage_warnings(full, full, full_tokens, full_tokens)):
        print("PASS: a near-empty suspect and a page without text are reported; a clean pair is not.")
    else:
        print(f"FAIL: coverage warnings: {warnings31}")
        ok31 = False

    from pptx import Presentation as _Prs
    from pptx.util import Inches as _In
    deck = _Prs()
    slide31 = deck.slides.add_slide(deck.slide_layouts[6])
    box_a = slide31.shapes.add_textbox(_In(1), _In(1), _In(3), _In(1))
    box_a.text_frame.text = "Each tablet contains 20 mg"
    box_b = slide31.shapes.add_textbox(_In(1), _In(3), _In(3), _In(1))
    box_b.text_frame.text = "Keep out of reach of children"
    slide31.shapes.add_group_shape([slide31.shapes.add_group_shape([box_a]), box_b])
    slide31.notes_slide.notes_text_frame.text = "Not for children under 12"
    deck_path = os.path.join(tmp, "grouped.pptx")
    deck.save(deck_path)
    deck_text = " ".join(w.text for w in _extract_pptx_words(deck_path))
    if all(t in deck_text for t in ("contains 20 mg", "reach of children", "under 12")):
        print("PASS: text inside (nested) grouped shapes and speaker notes is read.")
    else:
        print(f"FAIL: grouped deck read as {deck_text!r}")
        ok31 = False

    import docx as _docx
    word31 = _docx.Document()
    word31.sections[0].header.paragraphs[0].text = "DOC-123 Version 4"
    word31.sections[0].footer.paragraphs[0].text = "Revised 05-SEP-2021"
    word31.add_paragraph("Body text.")
    word_path = os.path.join(tmp, "headed.docx")
    word31.save(word_path)
    word_text = " ".join(w.text for w in _extract_docx_words(word_path))
    if word_text.startswith("DOC-123 Version 4") and word_text.endswith("Revised 05-SEP-2021"):
        print("PASS: Word headers and footers are read (approximate layout).")
    else:
        print(f"FAIL: Word document read as {word_text!r}")
        ok31 = False

    import openpyxl as _xl
    book = _xl.Workbook()
    book.active["A1"] = "Total mg"
    book.active["B1"] = "=B2*B3"
    book.active["B2"] = 20
    book.active["B3"] = 3
    book_path = os.path.join(tmp, "formula.xlsx")
    book.save(book_path)
    book_text = [w.text for w in _extract_xlsx_words(book_path)]
    if "=B2*B3" in book_text:
        print("PASS: an uncalculated formula cell is compared by its formula, not dropped.")
    else:
        print(f"FAIL: workbook read as {book_text!r}")
        ok31 = False

    # Small type drawn word by word with no space glyphs: MuPDF's own
    # word boxes restore the boundaries the glyph gaps cannot.
    tight = os.path.join(tmp, "tight.pdf")
    doc31 = fitz.open()
    page31 = doc31.new_page(width=400, height=200)
    tight_words = "patients who have not healed after 4 to 8 weeks of treatment".split()
    font31 = fitz.Font("helv")
    x31 = 20.0
    for word in tight_words:
        page31.insert_text((x31, 60), word, fontsize=6.3, fontname="helv")
        x31 += font31.text_length(word, fontsize=6.3) + 1.5
    doc31.save(tight); doc31.close()
    tight_read = [w.text for w in extract_words(tight)]
    if tight_read == tight_words:
        print("PASS: 6.3pt words with 1.5pt gaps and no space glyphs come out as separate words.")
    else:
        print(f"FAIL: small type read as {tight_read!r}")
        ok31 = False
    if ok31:
        print("PASS: coverage and format completeness.")

    print()
    print("=" * 70)
    print("TEST 32: the review is kept, and the report records it")
    print("=" * 70)
    ok32 = True
    from datetime import datetime as _dt, timezone as _tz
    from core.report import ReportDecision, ReportInput, write_report
    from core.review_store import ReviewStore, comparison_key, file_digest, fingerprints

    rows32 = diff_documents(build_comparison_tokens(lines_to_words(["Take 5 mg daily", "Store dry"])),
                            build_comparison_tokens(lines_to_words(["Take 10 mg daily", "Store dry", "Rx only"])))
    keys = fingerprints(rows32)
    rerun = diff_documents(build_comparison_tokens(lines_to_words(["Take 5 mg daily", "Store dry"])),
                           build_comparison_tokens(lines_to_words(["Take 10 mg daily", "Store dry", "Rx only"])))
    store32 = ReviewStore(os.path.join(tmp, "reviews"))
    key32 = comparison_key(full, stamp)
    store32.save(key32, {"decisions": {keys[0]: {"status": "accepted"}}})
    if (keys == fingerprints(rerun) and len(set(keys)) == len(keys)
            and store32.load(key32)["decisions"][keys[0]]["status"] == "accepted"
            and comparison_key(full, stamp) != comparison_key(stamp, full)):
        print("PASS: differences keep the same identity across runs, and a review round-trips.")
    else:
        print("FAIL: review identity or storage.")
        ok32 = False

    decided = ReportDecision("accepted", "QA Reviewer", _dt(2026, 10, 4, 9, 30, tzinfo=_tz.utc))
    report_input = ReportInput(
        approved_path=full, suspect_path=stamp, items=rows32,
        decision_of=lambda item: decided if item is rows32[0] else None,
        category_of=_cat29, reviewer="QA Reviewer", app_version="test",
        warnings=["Suspect: page 2 has no extractable text"],
        rematch_log=[{"at": "2026-10-04 09:31:00", "reviewer": "QA Reviewer", "outcome": "removed",
                      "approved_text": "x", "suspect_text": "", "reason": "matches its own paragraph"}],
    )
    html_path, csv_path = os.path.join(tmp, "report.html"), os.path.join(tmp, "report.csv")
    write_report(html_path, report_input)
    write_report(csv_path, report_input)
    html32 = open(html_path, encoding="utf-8").read()
    csv32 = open(csv_path, encoding="utf-8-sig").read()
    needed = [file_digest(full), file_digest(stamp), "QA Reviewer", "accepted", "not reviewed",
              "page 2 has no extractable text", "matches its own paragraph"]
    missing = [n for n in needed if n not in html32 or n not in csv32]
    if not missing:
        print("PASS: the HTML and CSV reports carry both SHA-256s, every decision, the warnings and the rematch log.")
    else:
        print(f"FAIL: report is missing {missing}")
        ok32 = False
    if ok32:
        print("PASS: review persistence and report.")

    print()
    print("=" * 70)
    print("TEST 33: the faster extraction gives exactly the same answers")
    print("=" * 70)
    ok33 = True
    import random as _random

    import numpy as _np

    from core import fast_tables as _ft
    from core import mupdf_page as _mp

    # Table geometry: the fast versions against pdfplumber's own, on random
    # grids with near-tolerance offsets -- same intersections, the same
    # edge objects in the same order, the same cells.
    originals33 = _ft.originals()
    rng33 = _random.Random(11)
    mismatches33 = 0
    for _trial in range(150):
        grid = [10.0 * k + rng33.choice([0, 0.5, 1.25]) for k in range(rng33.randint(2, 8))]
        edges33 = []
        for _ in range(rng33.randint(1, 50)):
            if rng33.random() < 0.5:
                x = rng33.choice(grid) + rng33.choice([0, 0, 0.3, 2.9, 3.0, 3.1])
                top = rng33.choice(grid)
                edges33.append({"orientation": "v", "x0": x, "x1": x, "top": top,
                                "bottom": top + rng33.choice([0, 10, 20, 40])})
            else:
                y = rng33.choice(grid) + rng33.choice([0, 0, 0.2, -1.0, 3.0, 2.99999])
                x0 = rng33.choice(grid)
                edges33.append({"orientation": "h", "x0": x0, "x1": x0 + rng33.choice([0, 10, 30]),
                                "top": y, "bottom": y})
        for tol in (1, 3):
            slow = originals33["edges_to_intersections"](edges33, tol, tol)
            fast = _ft.edges_to_intersections(edges33, tol, tol)
            same_edges = list(slow) == list(fast) and all(
                [id(e) for e in slow[k][o]] == [id(e) for e in fast[k][o]] for k in slow for o in ("v", "h")
            )
            if not same_edges or originals33["intersections_to_cells"](slow) != _ft.intersections_to_cells(slow):
                mismatches33 += 1
    if mismatches33:
        print(f"FAIL: fast table geometry differs from pdfplumber's on {mismatches33} grid(s).")
        ok33 = False
    else:
        print("PASS: fast table geometry matches pdfplumber's exactly on 300 random grids.")

    # Rotated pages: the bulk single-precision transform against PyMuPDF.
    transform_bad = 0
    for rotation in (90, 180, 270):
        doc33 = fitz.open()
        page33 = doc33.new_page(width=595.276, height=841.89)
        page33.set_rotation(rotation)
        m33 = page33.rotation_matrix * fitz.Matrix(1, 0, 0, 1, -page33.rect.x0, -page33.rect.y0)
        pts = [(float(_np.float32(rng33.uniform(-20, 900))), float(_np.float32(rng33.uniform(-20, 900))))
               for _ in range(3000)]
        xs, ys = _mp._view_points([p[0] for p in pts], [p[1] for p in pts], m33)
        transform_bad += sum(1 for p, x, y in zip(pts, xs, ys) if tuple(fitz.Point(p) * m33) != (x, y))
        boxes = [(x, y, x + 4.5, y + 7.25) for x, y in pts[:500]]
        transform_bad += sum(
            1 for b, r in zip(boxes, _mp._view_rects(boxes, m33)) if tuple(fitz.Rect(b) * m33) != r
        )
        doc33.close()
    if transform_bad:
        print(f"FAIL: {transform_bad} rotated coordinates differ from PyMuPDF's transform.")
        ok33 = False
    else:
        print("PASS: rotated-page coordinates are bit for bit PyMuPDF's.")

    # A rotated page reads the same words as before the speed-up.
    rotated_pdf = os.path.join(tmp, "rotated_text.pdf")
    doc33 = fitz.open()
    page33 = doc33.new_page(width=400, height=300)
    page33.insert_text((30, 60), "Each tablet contains 20 mg esomeprazole", fontsize=9, fontname="helv")
    page33.set_rotation(90)
    doc33.save(rotated_pdf)
    doc33.close()
    rotated_words = [w.text for w in extract_words(rotated_pdf)]
    if rotated_words == "Each tablet contains 20 mg esomeprazole".split():
        print("PASS: text on a rotated page reads in order.")
    else:
        print(f"FAIL: rotated page read as {rotated_words!r}")
        ok33 = False
    if ok33:
        print("PASS: extraction speed-ups are exact.")

    print()
    print("=" * 70)
    print("TEST 34: dense outsert columns are read column by column")
    print("=" * 70)
    ok34 = True

    def outsert_words(info_lines: int, gutter: float, word_gap: float = 1.6) -> list[dict]:
        """Four columns of 6pt prose (word gap 1.6pt) separated by real
        gutters of `gutter` points at fractional positions -- so whole-point
        buckets lose part of each gap -- with `info_lines` lines of an
        artwork-information block above the MIDDLE two columns only, the
        way the outsert's block sits: it crosses the gutter between them,
        while the outer gutters stay clean."""
        words: list[dict] = []
        col_lefts = []
        col_left = 20.3
        for col in range(4):
            col_lefts.append(col_left)
            for row in range(60):
                top = 60.0 + row * 7.5
                x = col_left
                if col == 1 and row == 30:
                    # A section heading part-way down a middle column: it
                    # starts a new reading zone across whatever band the
                    # column was put in.
                    for text in ("5", "WARNINGS", "AND", "PRECAUTIONS"):
                        words.append({"text": text, "x0": x, "x1": x + 30.0, "top": top, "bottom": top + 6})
                        x += 31.6
                    continue
                for k in range(8):
                    text = f"C{col}L{row}" if k == 0 else "w"
                    words.append({"text": text, "x0": x, "x1": x + 39.5, "top": top, "bottom": top + 6})
                    x += 39.5 + word_gap
            col_left = col_lefts[-1] + 8 * 39.5 + 7 * word_gap + gutter
        for row in range(info_lines):
            top = 10.0 + row * 9.0
            x = col_lefts[1] + 120.0
            while x < col_lefts[2] + 180.0:
                words.append({"text": "INFO", "x0": x, "x1": x + 21.7, "top": top, "bottom": top + 6})
                x += 21.7 + 1.6
        return words

    def column_major(words: list[dict]) -> bool:
        units, bands = _order_body_units(words, 20.0, 1340.0)
        order = []
        for band_idx in range(len(bands)):
            for _, _, line in sorted((u for u in units if u[0] == band_idx), key=lambda u: u[1]):
                order.extend(w["text"] for w in line if w["text"].startswith("C"))
        expected = [f"C{c}L{r}" for c in range(4) for r in range(60) if (c, r) != (1, 30)]
        return order == expected

    if column_major(outsert_words(info_lines=0, gutter=8.3)):
        print("PASS: four 8.3pt-gutter columns read column by column (control).")
    else:
        print("FAIL: plain three-column layout is not read column by column.")
        ok34 = False
    # Whole-point buckets read a gutter 1-2pt narrower than it is; with
    # slightly wider word spacing the minimum gutter rises to ~6.5pt and a
    # real 7.4pt gutter (the FPL's page 2 had 8.1-8.3pt ones measured as 6)
    # was rejected, merging the columns.
    if column_major(outsert_words(info_lines=0, gutter=7.4, word_gap=1.85)):
        print("PASS: a narrow real gutter is not lost to whole-point measurement.")
    else:
        print("FAIL: a 7.4pt gutter with 1.85pt word spacing merged the columns.")
        ok34 = False
    if column_major(outsert_words(info_lines=2, gutter=8.3)):
        print("PASS: an information block above the middle columns does not merge them.")
    else:
        print("FAIL: an information block over the middle columns merged them into one band.")
        ok34 = False

    # And the comparison: the same text set in a different number of rows
    # per column must not come out as "moved" fragments.
    from core.diff_engine import category_of as _cat34

    def to_tokens(words):
        units, bands = _order_body_units(words, 20.0, 1340.0)
        ordered = []
        for band_idx in range(len(bands)):
            for _, _, line in sorted((u for u in units if u[0] == band_idx), key=lambda u: u[1]):
                ordered.extend(line)
        return build_comparison_tokens([
            Word(text=w["text"], page=1, x0=w["x0"], top=w["top"], x1=w["x1"], bottom=w["bottom"])
            for w in ordered
        ])

    with_info = to_tokens(outsert_words(info_lines=2, gutter=8.3))
    plain = to_tokens(outsert_words(info_lines=0, gutter=8.3))
    rows34 = diff_documents(plain, with_info)
    moved34 = [d for d in rows34 if _cat34(d) == "moved"]
    if not moved34 and all(set(d.suspect_text.split()) <= {"INFO"} for d in rows34):
        print("PASS: the only difference reported is the added information block.")
    else:
        print(f"FAIL: {len(moved34)} moved row(s), rows: "
              f"{[(d.kind, d.approved_text[:20], d.suspect_text[:20]) for d in rows34][:6]}")
        ok34 = False
    if ok34:
        print("PASS: outsert column detection.")

    print()
    print("=" * 70)
    print("TEST 35: Highlights in two narrow columns, running headers, sheet order")
    print("=" * 70)
    ok35 = True
    from core.page_furniture import drop_repeated_furniture
    from core.pdf_extractor import _sub_column_segments
    from core.pdf_merge import natural_sort_key

    def highlights_rows(rows_top: int, rows_bottom: int, gap: float = 4.9) -> list[list[dict]]:
        """One page column: a two-column block (left 232-358, right
        358+gap-490, 6pt justified text: 1.8pt word spaces, every fourth
        line stretched to 4.5pt) of `rows_top` lines, then
        `rows_bottom` ordinary full-width lines -- the shape of an outsert's
        Highlights box above the start of the full prescribing information."""
        rows = []
        for r in range(rows_top):
            top = 200.0 + r * 7.0
            row = []
            space = 4.5 if r % 4 == 0 else 1.8
            for name, x, end in (("L", 232.0, 358.0), ("R", 358.0 + gap, 490.0)):
                width = (end - x - 5 * space) / 6
                for k in range(6):
                    row.append({"text": f"{name}{r}" if k == 0 else "w", "x0": x, "x1": x + width,
                                "top": top, "bottom": top + 6})
                    x += width + space
            rows.append(row)
        for r in range(rows_bottom):
            top = 200.0 + (rows_top + r) * 7.0
            x, row = 232.0, []
            for k in range(12):
                row.append({"text": f"B{r}" if k == 0 else "w", "x0": x, "x1": x + 19.0,
                            "top": top, "bottom": top + 6})
                x += 19.0 + 2.6
            rows.append(row)
        return rows

    def reading(segments):
        order = []
        for words, gutter in segments:
            if gutter is None:
                order += [w["text"] for w in sorted(words, key=lambda w: (w["top"], w["x0"])) if w["text"] != "w"]
                continue
            mid = (gutter[0] + gutter[1]) / 2
            for side in (lambda w: w["x1"] <= mid, lambda w: w["x0"] >= mid):
                order += [w["text"] for w in sorted((w for w in words if side(w)), key=lambda w: (w["top"], w["x0"]))
                          if w["text"] != "w"]
        return order

    segments = _sub_column_segments(highlights_rows(40, 10))
    expected = [f"L{r}" for r in range(40)] + [f"R{r}" for r in range(40)] + [f"B{r}" for r in range(10)]
    if segments and reading(segments) == expected:
        print("PASS: a 4.9pt-gutter block reads its left column, then its right, then the text below.")
    else:
        print(f"FAIL: two-column block read as {reading(segments)[:12] if segments else None}")
        ok35 = False

    rule_y = 200.0 + 20 * 7.0 - 2.0
    segments = _sub_column_segments(highlights_rows(40, 0), [(rule_y, 230.0, 492.0)])
    expected = ([f"L{r}" for r in range(20)] + [f"R{r}" for r in range(20)]
                + [f"L{r}" for r in range(20, 40)] + [f"R{r}" for r in range(20, 40)])
    if segments and reading(segments) == expected:
        print("PASS: a rule across the gutter ends one block (the Highlights) and starts the next (Contents).")
    else:
        print(f"FAIL: block with a rule read as {reading(segments)[:12] if segments else None}")
        ok35 = False

    if _sub_column_segments(highlights_rows(0, 40)) is None and _sub_column_segments(highlights_rows(12, 30)) is None:
        print("PASS: ordinary text, and a too-short two-column run, are left exactly as before.")
    else:
        print("FAIL: a two-column block was claimed where there is none.")
        ok35 = False

    bullets = []
    for r in range(40):
        top = 200.0 + r * 7.0
        bullets.append([{"text": "\u2022", "x0": 232.0, "x1": 235.0, "top": top, "bottom": top + 6}]
                       + [{"text": "w", "x0": 245.0 + k * 22.0, "x1": 264.0 + k * 22.0, "top": top, "bottom": top + 6}
                          for k in range(11)])
    if _sub_column_segments(bullets) is None:
        print("PASS: a bullet column beside its text is not a two-column block.")
    else:
        print("FAIL: a bulleted list was split into columns.")
        ok35 = False

    def page_tokens(pages, header=True, footer=True, body_repeat=None):
        words = []
        for page in range(1, pages + 1):
            lines = []
            if header:
                lines.append("Draft Labeling Text 1.14.1.3")
            names = ["one", "two", "three", "four", "five", "six"]
            lines.append(body_repeat or f"Body text unique to page number {names[page - 1]} here.")
            lines.append(f"More body content follows on page {['one', 'two', 'three', 'four', 'five', 'six'][page - 1]}.")
            if footer:
                lines.append(f"Page {page} of {pages}")
            for row, line in enumerate(lines):
                x = 70.0
                for text in line.split():
                    words.append(Word(text=text, page=page, x0=x, top=57.0 + row * 14, x1=x + 6 * len(text),
                                      bottom=69.0 + row * 14))
                    x += 6 * len(text) + 4
        return build_comparison_tokens(words)

    draft = page_tokens(6)
    outsert = page_tokens(1, header=False, footer=False)
    kept, found = drop_repeated_furniture(draft, outsert)
    texts = " ".join(t.text for t in kept)
    if (sorted(found) == ["Draft Labeling Text 1.14.1.3", "Page 1 of 6"]
            and texts.count("Draft Labeling Text") == 1 and texts.count("Page") == 1):
        print("PASS: a running header and footer are compared once, their repeats left out.")
    else:
        print(f"FAIL: furniture found {found}, header kept {texts.count('Draft Labeling Text')}x")
        ok35 = False
    # Both documents repeat it: set aside in both, the first copy of each
    # kept -- their copies fall wherever each document's pages break.
    other_draft = page_tokens(6)
    both_a = drop_repeated_furniture(draft, other_draft)
    both_b = drop_repeated_furniture(other_draft, draft)
    both_ok = all(
        sorted(found) == ["Draft Labeling Text 1.14.1.3", "Page 1 of 6"]
        and " ".join(t.text for t in kept).count("Draft Labeling Text") == 1
        for kept, found in (both_a, both_b)
    )
    long_line = "This full width paragraph line repeats at the top of every single page of the file"
    repeated_body = drop_repeated_furniture(page_tokens(5, header=False, footer=False, body_repeat=long_line))
    if both_ok and not repeated_body[1] and len(page_tokens(2)) == len(drop_repeated_furniture(page_tokens(2))[0]):
        print("PASS: set aside in both when both repeat it; kept for long body lines and 2-page documents.")
    else:
        print(f"FAIL: furniture: both-sides {both_a[1]} / {both_b[1]}, body {repeated_body[1]}")
        ok35 = False

    names = ["Leaflet 8108575 Back.pdf", "Leaflet 8108575 Front.pdf", "part10.pdf", "part2.pdf", "frontier.pdf"]
    if sorted(names, key=natural_sort_key) == ["frontier.pdf", "Leaflet 8108575 Front.pdf",
                                              "Leaflet 8108575 Back.pdf", "part2.pdf", "part10.pdf"]:
        print("PASS: merged sheets default to Front before Back; part2 before part10.")
    else:
        print(f"FAIL: merge order {sorted(names, key=natural_sort_key)}")
        ok35 = False
    if ok35:
        print("PASS: two-column blocks, page furniture and sheet order.")

    print()
    print("=" * 70)
    print("TEST 36: Word draft against an SPL -- hidden print headers, line-break")
    print("         hyphens, subscripts, table rows, dash rules, spacing")
    print("=" * 70)
    ok36 = True
    from core.pdf_extractor import _read_lines, _drop_dash_rules  # noqa: F401

    # (a) A browser's print header set above the page's crop box is not on
    #     the page; text just inside the crop box still is.
    hidden_pdf = os.path.join(tmp, "hidden_header.pdf")
    hdoc = fitz.open()
    hpage = hdoc.new_page(width=400, height=300)
    # Ends about 2pt above the crop box (inside the old 5pt tolerance),
    # its centre outside it -- as the SPL's header did.
    hpage.insert_text((40, 17.5), "These highlights Page 4 of 34", fontsize=10, fontname="helv")
    hpage.insert_text((40, 34), "Visible first line", fontsize=10, fontname="helv")
    hpage.insert_text((40, 120), "Body text of the page", fontsize=10, fontname="helv")
    hpage.set_cropbox(fitz.Rect(0, 20, 400, 300))
    hdoc.save(hidden_pdf)
    hdoc.close()
    hidden_text = " ".join(w.text for w in extract_words(hidden_pdf))
    if "highlights" not in hidden_text and "Visible" in hidden_text and "Body" in hidden_text:
        print("PASS: text printed outside the crop box is not compared; text inside it is.")
    else:
        print(f"FAIL: off-page text handling read {hidden_text!r}")
        ok36 = False

    # (b) A Wingdings bullet given as a private-use code is a bullet.
    if _normalize_word_text("\uf06c") == "\u2022":
        print("PASS: a Wingdings bullet (U+F06C) compares as a bullet.")
    else:
        print(f"FAIL: U+F06C normalized to {_normalize_word_text(chr(0xF06C))!r}")
        ok36 = False

    # (c) A compound broken at its own hyphen across a line is the same
    #     word as the one printed whole -- and the answer is decided per
    #     comparison, not carried over to the next one.
    def lines_words(lines, page=1):
        words, y = [], 50.0
        for line in lines:
            x = 50.0
            for text in line.split():
                words.append(Word(text=text, page=page, x0=x, top=y, x1=x + 6 * len(text), bottom=y + 10))
                x += 6 * len(text) + 4
            y += 14
        return words

    broken = build_comparison_tokens(lines_words(["compared with placebo-", "treated patients in the study"]))
    whole_hyphen = build_comparison_tokens(lines_words(["compared with placebo-treated patients in the study"]))
    whole_plain = build_comparison_tokens(lines_words(["compared with placebotreated patients in the study"]))
    first = diff_documents(broken, whole_hyphen)
    second = diff_documents(broken, whole_plain)
    if not first and not second:
        print("PASS: 'placebo-' + 'treated' matches the word printed whole, with or without its hyphen.")
    else:
        print(f"FAIL: line-break hyphen rows {[(i.approved_text, i.suspect_text) for i in first + second]}")
        ok36 = False
    suspended = build_comparison_tokens(lines_words(["by comparisons to age-", "and sex-matched standards"]))
    if [t.text for t in suspended][3:5] == ["age-", "and"]:
        print("PASS: 'age- and sex-matched' is not stitched into 'ageand'.")
    else:
        print(f"FAIL: suspended hyphen read as {[t.text for t in suspended]}")
        ok36 = False

    # (d) A subscript set a space's width after its base is still its
    #     subscript: "C max" against "Cmax" is spacing, never a rewording.
    sub_pdf = os.path.join(tmp, "spaced_subscript.pdf")
    sdoc = fitz.open()
    spage = sdoc.new_page(width=400, height=200)
    spage.insert_text((40, 60), "increases the C", fontsize=7.44, fontname="helv")
    c_end = 40 + fitz.get_text_length("increases the C", fontname="helv", fontsize=7.44)
    spage.insert_text((c_end + 1.87, 62.6), "max", fontsize=5.94, fontname="helv")
    spage.insert_text((c_end + 1.87 + fitz.get_text_length("max", fontname="helv", fontsize=5.94) + 1.9, 60),
                      "approximately", fontsize=7.44, fontname="helv")
    sdoc.save(sub_pdf)
    sdoc.close()
    sub_words = [w.text for w in extract_words(sub_pdf)]
    if "\u2098\u2090\u2093" in sub_words:
        print("PASS: a subscript a space's width from its base is read as a subscript.")
    else:
        print(f"FAIL: spaced subscript read as {sub_words}")
        ok36 = False

    # (e) Table rows whose cells have different numbers of lines read the
    #     same in both layouts: cell by cell.
    def wd(text, x, top, h=7.4):
        return {"text": text, "x0": x, "x1": x + 4.5 * len(text), "top": top, "bottom": top + h}

    # Layout 1: "Placebo" centred beside two-line dose cells.
    layout1 = [
        [wd("20", 120, 100), wd("40", 180, 100), wd("80", 240, 100)],
        [wd("Placebo", 40, 104.5)],
        [wd("mg/day", 115, 109), wd("mg/day", 175, 109), wd("mg/day", 235, 109)],
    ]
    # Layout 2: the "80" cell is wide enough for one line, centred too.
    layout2 = [
        [wd("20", 120, 100), wd("40", 180, 100)],
        [wd("Placebo", 40, 104.5), wd("80", 240, 104.5), wd("mg/day", 255, 104.5)],
        [wd("mg/day", 115, 109), wd("mg/day", 175, 109)],
    ]

    def read(lines):
        return " ".join(w["text"] for _top, words in _read_lines(lines, rtl=False) for w in words)

    want = "Placebo 20 mg/day 40 mg/day 80 mg/day"
    if read(layout1) == want and read(layout2) == want:
        print("PASS: a header with centred and wrapped cells reads cell by cell in both layouts.")
    else:
        print(f"FAIL: header rows read {read(layout1)!r} / {read(layout2)!r}")
        ok36 = False
    # Every cell wraps, the row label is on the first line only.
    wrapped = [
        [wd("All", 40, 200), wd("Patients", 58, 200), wd("-1.9", 120, 200), wd("-1.1", 180, 200), wd("-1.4", 240, 200)],
        [wd("(n=672)", 115, 209), wd("(n=70)", 175, 209), wd("(n=476)", 235, 209)],
        [wd("Females", 40, 218), wd("-5.1", 120, 218), wd("-0.7", 180, 218), wd("-4.0", 240, 218)],
    ]
    if read(wrapped) == "All Patients -1.9 (n=672) -1.1 (n=70) -1.4 (n=476) Females -5.1 -0.7 -4.0":
        print("PASS: a row whose every cell wraps reads each cell whole.")
    else:
        print(f"FAIL: wrapped row read {read(wrapped)!r}")
        ok36 = False
    # Not tables: ordinary lines, and a title of mixed sizes, keep their order.
    prose = [[wd("The", 40, 300), wd("first", 60, 300), wd("line", 90, 300)],
             [wd("the", 40, 312), wd("second", 60, 312), wd("one", 100, 312)]]
    title = [[wd("INJECTION", 200, 79, 14)], [wd("(Acyclovir", 150, 86, 10), wd("Sodium)", 260, 86, 10)],
             [wd("500mg", 150, 97, 8)]]
    if read(prose) == "The first line the second one" and read(title) == "INJECTION (Acyclovir Sodium) 500mg":
        print("PASS: ordinary text and a mixed-size title are read line by line, as before.")
    else:
        print(f"FAIL: non-table lines read {read(prose)!r} / {read(title)!r}")
        ok36 = False

    # (f) Dash rules around Highlights headings carry no text.
    ruled = build_comparison_tokens(lines_words(["----------- RECENT MAJOR CHANGES- ----------",
                                                 "-------DOSAGE FORMS AND STRENGTHS-------"]))
    plain = build_comparison_tokens(lines_words(["RECENT MAJOR CHANGES", "DOSAGE FORMS AND STRENGTHS"]))
    if [t.text for t in ruled] == [t.text for t in plain] and not diff_documents(ruled, plain):
        print("PASS: '------ HEADING ------' and '------HEADING' compare as the plain heading.")
    else:
        print(f"FAIL: dash rules read {[t.text for t in ruled]}")
        ok36 = False

    # (g) A cross-reference spaced differently is ONE spacing row.
    ref_a = build_comparison_tokens(lines_words(["see Warnings and Precautions (5.2)] . Next sentence here"]))
    ref_b = build_comparison_tokens(lines_words(["see Warnings and Precautions ( 5.2) ]. Next sentence here"]))
    ref_rows = diff_documents(ref_a, ref_b)
    if len(ref_rows) == 1 and ref_rows[0].severity == "minor":
        print("PASS: '(5.2)] .' against '( 5.2) ].' is one minor spacing difference.")
    else:
        print(f"FAIL: spacing rows {[(i.approved_text, i.suspect_text, i.severity) for i in ref_rows]}")
        ok36 = False
    if ok36:
        print("PASS: Word draft against an SPL.")

    print()
    print("=" * 70)
    print("TEST 37: Tables read the same whichever way they are drawn")
    print("=" * 70)
    ok37 = True
    from core.pdf_extractor import _bands_are_number_table, _table_units_for

    def cw(text, x, top, h=9.0, width=None):
        return {"text": text, "x0": x, "x1": x + (width or 5.0 * len(text)), "top": top, "bottom": top + h}

    def phrase(text, x, top, h=9.0):
        out = []
        for part in text.split():
            out.append(cw(part, x, top, h))
            x += 5.0 * len(part) + 3.0
        return out

    def read37(lines):
        return " ".join(w["text"] for _top, words in _read_lines(lines, rtl=False) for w in words)

    # (a) Which zones are tables, and which are columns of text or lists.
    number_bands = [(90, 187), (218, 233), (294, 309), (391, 407), (493, 540)]
    cell_bands = [(90, 187), (236, 349), (383, 540)]
    cell_rows = [phrase("80 mg", 100, 80) + phrase("pale green oval", 240, 80) + phrase("Plain on one side and debossed", 390, 76)]
    prose_rows = [phrase("the quick brown fox jumps over", 92, y) + phrase("a lazy dog sat by the", 238, y)
                  + phrase("river and slept all day", 385, y) for y in (10, 22, 34)]
    list_rows = [phrase("• ritonavir", 92, y) + phrase("• carbamazepine", 238, y) + phrase("• rifampin", 385, y)
                 for y in (10, 22)]
    if (_bands_are_number_table(number_bands) and _bands_are_number_table(cell_bands, cell_rows)
            and not _bands_are_number_table(cell_bands, prose_rows)
            and not _bands_are_number_table(cell_bands, list_rows)):
        print("PASS: number and short-cell columns are a table; text columns and bulleted lists are not.")
    else:
        print("FAIL: table / text-column / list zones misjudged")
        ok37 = False

    # (b) A row label wrapped onto a line of its own: it belongs to the
    #     row it sits closer to.
    up_rows = [phrase("Flushing", 18, 98) + [cw("10%", 104, 98), cw("9%", 166, 98), cw("16%", 227, 98)],
               phrase("Pain in", 18, 111) + [cw("7%", 104, 111), cw("15%", 166, 111), cw("9%", 227, 111)],
               phrase("Limb", 18, 120.5),
               phrase("Myalgia", 18, 133) + [cw("7%", 104, 133), cw("6%", 166, 133), cw("14%", 227, 133)]]
    down_rows = [phrase("Akathisia", 58, 641) + [cw("3", 196, 641), cw("6", 247, 641), cw("11", 296, 641)],
                 phrase("Extrapyramidal", 58, 652),
                 phrase("Disorder**", 58, 663) + [cw("6", 196, 663), cw("6", 247, 663), cw("11", 296, 663)]]
    if (read37(up_rows) == "Flushing 10% 9% 16% Pain in Limb 7% 15% 9% Myalgia 7% 6% 14%"
            and read37(down_rows) == "Akathisia 3 6 11 Extrapyramidal Disorder** 6 6 11"):
        print("PASS: 'Pain in / Limb' and 'Extrapyramidal / Disorder**' each read as one label.")
    else:
        print(f"FAIL: wrapped labels read {read37(up_rows)!r} / {read37(down_rows)!r}")
        ok37 = False

    # (c) A header stacked in columns on a shared line grid: every line of
    #     it, down to the first data row, read column by column -- as the
    #     ruled Word table's cells ("Lurasidone hydrochloride tablets 40
    #     mg/day") and an SPL's header grid rows are (TEST 50).
    stacked = [phrase("Lurasidone", 300, 380) + phrase("Lurasidone", 420, 380),
               phrase("hydrochloride tablets", 300, 391) + phrase("hydrochloride tablets", 420, 391),
               phrase("Placebo", 200, 402) + phrase("40 mg/day", 300, 402) + phrase("80 mg/day", 420, 402),
               phrase("All Patients", 90, 416) + [cw("+0.10", 200, 416), cw("+0.75", 300, 416), cw("+1.20", 420, 416)],
               phrase("Females", 90, 430) + [cw("+0.70", 200, 430), cw("+0.60", 300, 430), cw("+4.40", 420, 430)]]
    want37 = ("Placebo Lurasidone hydrochloride tablets 40 mg/day Lurasidone hydrochloride tablets 80 mg/day "
              "All Patients +0.10 +0.75 +1.20 Females +0.70 +0.60 +4.40")
    if read37(stacked) == want37:
        print("PASS: a stacked header reads column by column; its data rows stay rows.")
    else:
        print(f"FAIL: stacked header read {read37(stacked)!r}")
        ok37 = False

    # (d) A ruled grid that gives a record's second line a grid row of its
    #     own reads like the borderless table: cell by cell.
    grid_words = [cw("All", 20, 100), cw("Patients", 40, 100), cw("+0.10", 120, 100), cw("+0.75", 200, 100),
                  cw("(n=103)", 120, 112), cw("(n=102)", 200, 112),
                  cw("Females", 20, 124), cw("+0.70", 120, 124), cw("+0.60", 200, 124),
                  cw("(n=39)", 120, 136), cw("(n=42)", 200, 136)]
    grid_cells = {0: (0, 0, 0), 1: (0, 0, 0), 2: (0, 0, 1), 3: (0, 0, 2), 4: (0, 1, 1), 5: (0, 1, 2),
                  6: (0, 2, 0), 7: (0, 2, 1), 8: (0, 2, 2), 9: (0, 3, 1), 10: (0, 3, 2)}
    units = _table_units_for(0, list(enumerate(grid_words)), [(0.0, 300.0)], grid_cells, (10, 95, 260, 150))
    grid_read = " ".join(w["text"] for _b, _k, ws in sorted(units, key=lambda u: u[1]) for w in ws)
    if grid_read == "All Patients +0.10 (n=103) +0.75 (n=102) Females +0.70 (n=39) +0.60 (n=42)":
        print("PASS: a grid row with no label continues the cells of the labelled row above.")
    else:
        print(f"FAIL: grid continuation read {grid_read!r}")
        ok37 = False
    if ok37:
        print("PASS: tables read the same whichever way they are drawn.")

    print()
    print("=" * 70)
    print("TEST 38: The Find bar finds words and sentences where they are drawn")
    print("=" * 70)
    ok38 = True
    from core.text_search import SearchIndex, SearchWord, build_search_index

    find_pdf = os.path.join(tmp, "find_bar.pdf")
    fdoc = fitz.open()
    fp1 = fdoc.new_page(width=400, height=300)
    fp1.insert_text((40, 60), "The quick brown fox jumps", fontsize=12)
    fp1.insert_text((40, 80), "over the lazy dog.", fontsize=12)
    fp2 = fdoc.new_page(width=400, height=300)
    fp2.insert_text((40, 60), "Foxes and a fox: THE FOX again.", fontsize=12)
    fp2.set_rotation(90)
    fdoc.save(find_pdf)
    fdoc.close()
    index = build_search_index(find_pdf, "pdf")

    # (a) A sentence across a line break: one match, one box per line.
    across = index.find_all("brown fox jumps over the lazy")
    if len(across) == 1 and [(pg, len(b)) for pg, b in across[0].pages] == [(1, 2)]:
        print("PASS: a sentence across a line break is one match with a box per line.")
    else:
        print(f"FAIL: line-break sentence gave {[(m.pages) for m in across]}")
        ok38 = False

    # (b) Case, whole words, look-alike quotes, every occurrence in order.
    any_case = index.find_all("fox")
    exact = index.find_all("fox", match_case=True)
    whole = index.find_all("fox", whole_word=True)
    quote = SearchIndex([SearchWord(1, 0.0, 0.0, 30.0, 10.0, "Don’t")]).find_all("don't")
    if (len(any_case) == 4 and len(exact) == 2 and len(whole) == 3 and len(quote) == 1
            and [m.page for m in any_case] == [1, 2, 2, 2]):
        print("PASS: case, whole-word and curly-quote searches count every occurrence in order.")
    else:
        print(f"FAIL: fox {len(any_case)}/{len(exact)}/{len(whole)}, don't {len(quote)}")
        ok38 = False

    # (c) Boxes land where the page is drawn: on a rotated page too.
    with fitz.open(find_pdf) as fdoc:
        # search_for answers in the unrotated page; the page is drawn rotated.
        drawn = fdoc[1].search_for("again")[0] * fdoc[1].rotation_matrix
        page_rect = fdoc[1].rect
    box = index.find_all("again")[0].pages[0][1][0]
    # "again" of "again." -- cut along the line, which runs down the drawn page.
    if (abs(box[0] - drawn.x0) < 1 and abs(box[1] - drawn.y0) < 1 and abs(box[3] - drawn.y1) < 3
            and box[3] - box[1] > box[2] - box[0] and page_rect.width == 300):
        print("PASS: a match on a rotated page is boxed where the page is drawn.")
    else:
        print(f"FAIL: rotated-page box {box} vs drawn {tuple(drawn)}")
        ok38 = False

    # (d) Part of a word is boxed as that part, narrow punctuation allowed for.
    part_index = SearchIndex([SearchWord(1, 100.0, 10.0, 145.0, 20.0, "disorder,")])
    part = part_index.find_all("disorder")[0].pages[0][1][0]
    if 140.0 < part[2] < 145.0 and part[0] == 100.0:
        print("PASS: 'disorder' inside 'disorder,' is boxed up to the comma.")
    else:
        print(f"FAIL: partial-word box {part}")
        ok38 = False
    if ok38:
        print("PASS: the Find bar finds words and sentences where they are drawn.")

    print()
    print("=" * 70)
    print("TEST 39: Tables are read cell by cell -- highlighted, or inside another table")
    print("=" * 70)
    ok39 = True

    def grid(page, x0, y0, col_edges, row_edges):
        """Word-style borders: thin black filled rectangles."""
        for x in col_edges:
            page.draw_rect(fitz.Rect(x, y0 + row_edges[0], x + 0.5, y0 + row_edges[-1]), color=None, fill=(0, 0, 0))
        for y in row_edges:
            page.draw_rect(fitz.Rect(x0 + col_edges[0] - x0, y0 + y, col_edges[-1] + 0.5, y0 + y + 0.5),
                           color=None, fill=(0, 0, 0))

    def cell_text(page, x, y, lines):
        for k, line in enumerate(lines):
            page.insert_text((x, y + 11 * k), line, fontsize=9)

    # (a) A highlighted table: yellow highlight boxes behind cell text are
    #     not table rules; the wrapped cells are still read cell by cell.
    hl_pdf = os.path.join(tmp, "t39_highlight.pdf")
    hdoc = fitz.open()
    hp = hdoc.new_page(width=420, height=300)
    cols, rows = [40, 160, 270, 380], [0, 30, 60, 90]
    grid(hp, 40, 40, cols, rows)
    for (r, c), lines in {
        (0, 0): ["Doses per Vial"], (0, 1): ["Strength"], (0, 2): ["NDC"],
        (1, 0): ["4 doses of 2.5 mg/0.6", "mL"], (1, 1): ["10 mg/2.4 mL (4.17", "mg/mL)"], (1, 2): ["0002-4052-11"],
        (2, 0): ["4 doses of 5 mg/0.6", "mL"], (2, 1): ["20 mg/2.4 mL (8.33", "mg/mL)"], (2, 2): ["0002-4057-11"],
    }.items():
        x, y = cols[c] + 4, 40 + rows[r] + 11
        if r and c < 2:  # highlight the changed values, Word style: filled, no outline
            hp.draw_rect(fitz.Rect(x - 1, y - 9, x + 70, y + 3), color=None, fill=(1, 1, 0), overlay=False)
        cell_text(hp, x, y, lines)
    hdoc.save(hl_pdf)
    hdoc.close()
    hl_read = " ".join(w.text for w in extract_words(hl_pdf))
    want_hl = ("Doses per Vial Strength NDC 4 doses of 2.5 mg/0.6 mL 10 mg/2.4 mL (4.17 mg/mL) 0002-4052-11 "
               "4 doses of 5 mg/0.6 mL 20 mg/2.4 mL (8.33 mg/mL) 0002-4057-11")
    if hl_read == want_hl:
        print("PASS: highlight boxes are not table rules; wrapped cells read cell by cell.")
    else:
        print(f"FAIL: highlighted table read {hl_read!r}")
        ok39 = False

    # (b) A table nested in a cell of another (a side-by-side document:
    #     "RLD Label | Proposed Label" with a table in the first cell) is
    #     read cell by cell, row by row -- as the same table standing alone.
    nest_pdf = os.path.join(tmp, "t39_nested.pdf")
    ndoc = fitz.open()
    npage = ndoc.new_page(width=560, height=320)
    grid(npage, 30, 30, [30, 300, 530], [0, 20, 200, 230])
    cell_text(npage, 34, 44, ["RLD Label"])
    cell_text(npage, 304, 44, ["Proposed Label"])
    cell_text(npage, 34, 66, ["Table 1: Adverse Reactions"])
    icols, irows = [36, 120, 200, 290], [0, 36, 52, 68]
    grid(npage, 36, 78, icols, irows)
    for (r, c), lines in {
        (0, 0): ["Adverse", "Reaction"], (0, 1): ["Placebo", "(N=235)", "%"], (0, 2): ["MOUNJARO", "5 mg", "(N=237)"],
        (1, 0): ["Nausea"], (1, 1): ["4"], (1, 2): ["12"],
        (2, 0): ["Diarrhea"], (2, 1): ["9"], (2, 2): ["12"],
    }.items():
        cell_text(npage, icols[c] + 3, 78 + irows[r] + 10, lines)
    cell_text(npage, 34, 168, ["Note: Percentages reflect the number of patients."])
    cell_text(npage, 304, 66, ["Proposed text stays in its own cell."])
    cell_text(npage, 34, 244, ["Last RLD line"])
    cell_text(npage, 304, 244, ["Last proposed line"])
    ndoc.save(nest_pdf)
    ndoc.close()
    nest_read = " ".join(w.text for w in extract_words(nest_pdf))
    want_nest = ("RLD Label Proposed Label Table 1: Adverse Reactions Adverse Reaction Placebo (N=235) % "
                 "MOUNJARO 5 mg (N=237) Nausea 4 12 Diarrhea 9 12 Note: Percentages reflect the number of patients. "
                 "Proposed text stays in its own cell. Last RLD line Last proposed line")
    # What the viewer outlines (View > Show Table Cells): the same tables,
    # with how each is read.
    nest_layout: dict = {}
    extract_words(nest_pdf, table_layout=nest_layout)
    statuses = sorted(t["status"] for t in nest_layout.get(1, []))
    nested_parent = [t["parent"] for t in nest_layout.get(1, []) if t["status"] == "nested"]
    if statuses != ["nested", "table"] or nested_parent != [0]:
        print(f"FAIL: table layout for the viewer {statuses} parents {nested_parent}")
        ok39 = False
    if nest_read == want_nest:
        print("PASS: a table inside a table's cell is read cell by cell, its caption and note in place.")
    else:
        print(f"FAIL: nested table read {nest_read!r}")
        ok39 = False

    # (c) A word before a label/value list's label column is read with the
    #     label ("New code | Revised: February 2026"), not after the values.
    def lw(text, x, top=100.0):
        return {"text": text, "x0": x, "x1": x + 5.0 * len(text), "top": top, "bottom": top + 12.0}
    lv_rows = [[lw("New", 18), lw("code", 43), lw("Revised:", 213), lw("February", 258), lw("2026", 304)]]
    lv_read = " ".join(w["text"] for line in _order_label_value_zone(lv_rows, [(54.0, 68.0), (81.0, 292.0)])
                       for w in line)
    if lv_read == "New code Revised: February 2026":
        print("PASS: a word left of the label column is read with the label.")
    else:
        print(f"FAIL: label/value line read {lv_read!r}")
        ok39 = False
    if ok39:
        print("PASS: tables are read cell by cell, highlighted or nested.")

    print()
    print("=" * 70)
    print("TEST 40: A crop is applied before the page is read; merged header cells")
    print("=" * 70)
    ok40 = True

    def ruled_table(page, cols, rows, cells, span_header=False):
        """Ruled grid (thin lines) with text; a spanning first-row cell
        over columns 1..end when span_header."""
        for x_i, x in enumerate(cols):
            top = rows[1] if span_header and 1 < x_i < len(cols) - 1 else rows[0]
            page.draw_line((x, top), (x, rows[-1]))
        for y in rows:
            page.draw_line((cols[0], y), (cols[-1], y))
        for (r, c), text in cells.items():
            page.insert_text((cols[c] + 4, rows[r] + 14), text, fontsize=9)

    # (a) A side-by-side page cropped to its first two columns: the third
    #     column's words are gone before reading, and the table the viewer
    #     outlines is the kept part (3 rows x 2 columns), still a table.
    sbs_pdf = os.path.join(tmp, "t40_sbs.pdf")
    sdoc = fitz.open()
    spage = sdoc.new_page(width=500, height=200)
    ruled_table(spage, [20, 160, 300, 480], [20, 40, 60, 80], {
        (0, 0): "Reaction", (0, 1): "Placebo", (0, 2): "Notation",
        (1, 0): "Nausea", (1, 1): "4", (1, 2): "changed",
        (2, 0): "Diarrhea", (2, 1): "9", (2, 2): "deleted",
    })
    sdoc.save(sbs_pdf)
    sdoc.close()
    crop40 = {1: (0.0, 200.0, 0.0, 300.0)}  # keep x <= 300: drop the Notation column
    layout40: dict = {}
    kept_words = " ".join(w.text for w in extract_words(sbs_pdf, table_layout=layout40, crop=crop40))
    t40 = layout40.get(1, [{}])[0]
    shape40 = (len(t40.get("cells", [])), max((len(r) for r in t40.get("cells", [])), default=0))
    if (kept_words == "Reaction Placebo Nausea 4 Diarrhea 9" and t40.get("status") == "table"
            and shape40 == (3, 2) and t40["bbox"][2] <= 300.5):
        print("PASS: cropped words are gone before reading; the table outlined is the kept part, read cell by cell.")
    else:
        print(f"FAIL: crop read {kept_words!r}, table {t40.get('status')} {shape40} {t40.get('bbox')}")
        ok40 = False
    from core.document_extractor import extract_document_tokens as _edt40
    if " ".join(t.text for t in _edt40(sbs_pdf, crop=crop40)) != kept_words:
        print("FAIL: extract_document_tokens does not pass the crop on")
        ok40 = False

    # (b) A header cell spanning several columns is one cell: its words
    #     cross the column edges of the rows below without the table
    #     being taken for a grid drawn over flowing text.
    span_pdf = os.path.join(tmp, "t40_span.pdf")
    pdoc = fitz.open()
    ppage = pdoc.new_page(width=500, height=200)
    ruled_table(ppage, [20, 120, 200, 280, 360, 440], [20, 52, 72, 92], {
        (0, 0): "Reaction", (0, 1): "Lurasidone hydrochloride tablets, all doses combined, in",
        (1, 0): "Nausea", (1, 1): "4", (1, 2): "5", (1, 3): "6", (1, 4): "7",
        (2, 0): "Vomiting", (2, 1): "1", (2, 2): "2", (2, 3): "3", (2, 4): "4",
    }, span_header=True)
    ppage.insert_text((124, 46), "the placebo-controlled adolescent studies combined", fontsize=9)
    pdoc.save(span_pdf)
    pdoc.close()
    span_layout: dict = {}
    span_read = " ".join(w.text for w in extract_words(span_pdf, table_layout=span_layout))
    span_status = [t["status"] for t in span_layout.get(1, [])]
    if span_status == ["table"] and span_read.startswith(
        "Reaction Lurasidone hydrochloride tablets, all doses combined, in the placebo-controlled adolescent "
        "studies combined Nausea 4 5 6 7 Vomiting 1 2 3 4"
    ):
        print("PASS: a header cell spanning columns does not make the table 'flowing text'.")
    else:
        print(f"FAIL: spanning header table {span_status}: {span_read!r}")
        ok40 = False
    if ok40:
        print("PASS: crops are applied before reading; merged header cells keep a table a table.")

    print()
    print("=" * 70)
    print("TEST 41: A word one text layer splits is matched, not lost between the two")
    print("=" * 70)
    from core.token_map import TokenMap as _TokenMap41

    def toks41(text):
        return build_comparison_tokens(lines_to_words([text]))

    def pairing41(a_text, b_text):
        ta41, tb41 = toks41(a_text), toks41(b_text)
        found41: list = []
        rows41 = diff_documents(ta41, tb41, found41)
        partners41 = _TokenMap41(ta41, tb41, found41).partners()
        return rows41, partners41

    # Arabic drawn in pieces on one side ("القل ب." / "القلب."): no row,
    # and every word on both sides has a partner (it shows as matched).
    rows_split, partners_split = pairing41(
        "اذا كانوا مصابين بمرض في القل ب. حالات حساسة للهرمونات",
        "اذا كانوا مصابين بمرض في القلب. حالات حساسة للهرمونات",
    )
    # A real change after the split part: the split part is paired, the
    # changed words are not.
    rows_mixed, partners_mixed = pairing41(
        "Warnings and Precautions (5.2)] . 2.3 Concomitant Use with",
        "Warnings and Precautions (5.2)]. 2.4 Recommendations for Use with",
    )
    # A space lost next to a number is a reported spacing difference: it
    # stays unpaired, under its own highlight.
    rows_digit, partners_digit = pairing41("Take 100 mg daily", "Take 100mg daily")
    ok41 = (
        not rows_split and all(p >= 0 for side in partners_split.values() for p in side)
        and rows_mixed and partners_mixed["approved"][3] >= 0 and partners_mixed["approved"][4] >= 0
        and partners_mixed["approved"][5] < 0
        and len(rows_digit) == 1 and partners_digit["approved"][1] < 0 and partners_digit["suspect"][1] < 0
    )
    if ok41:
        print("PASS: a split word is matched; a changed word is not; a spacing difference stays a difference.")
    else:
        print(f"FAIL: split rows {len(rows_split)} partners {partners_split}; mixed {partners_mixed}; "
              f"digit rows {len(rows_digit)} partners {partners_digit}")

    print()
    print("=" * 70)
    print("TEST 42: Every word is matched or listed -- never neither")
    print("=" * 70)
    ok42 = True
    from core.completeness import FURNITURE_NOTE, _covered, furniture_rows, unaccounted_rows
    from core.page_furniture import drop_repeated_furniture as _drop42, furniture_repeats
    from core.token_map import APPROVED as _A42, SUSPECT as _S42, TokenMap as _TM42

    def four_pages(path, header, body):
        doc42 = fitz.open()
        for n in range(4):
            page42 = doc42.new_page(width=400, height=300)
            if header:
                page42.insert_text((40, 30), "Draft Labeling Text 1.14.1.3", fontsize=8)
            for k, line in enumerate(body[n]):
                page42.insert_text((40, 80 + 14 * k), line, fontsize=10)
        doc42.save(path)
        doc42.close()

    body_a = [["Take one tablet daily with food.", "Store below 25 C."], ["Do not crush the tablet."],
              ["Keep out of reach of children."], ["Discard unused tablets."]]
    body_b = [["Take one tablet twice daily with food.", "Store below 25 C."], ["Do not crush the tablet."],
              ["Keep out of reach of children."], ["Return unused tablets."]]
    a42, b42 = os.path.join(tmp, "t42_a.pdf"), os.path.join(tmp, "t42_b.pdf")
    four_pages(a42, True, body_a)
    four_pages(b42, False, body_b)
    every_a, every_b = extract_document_tokens(a42), extract_document_tokens(b42)
    # The worker's steps (workers/diff_worker.py), in order.
    repeats_a = furniture_repeats(every_a, every_b)
    kept_a, _f = _drop42(every_a, every_b)
    kept_b, _f = _drop42(every_b, every_a)
    found42: list = []
    rows42 = diff_documents(kept_a, kept_b, found42)
    map42 = _TM42(kept_a, kept_b, found42)
    rows42 = rows42 + furniture_rows(repeats_a, _A42, len(rows42))
    rows42 = rows42 + unaccounted_rows(rows42, map42, len(rows42))
    furniture42 = [r for r in rows42 if FURNITURE_NOTE in r.approved_text]
    unaccounted = 0
    for side42, every42 in ((_A42, every_a), (_S42, every_b)):
        matched42 = {id(t) for k, t in enumerate(map42.tokens(side42)) if map42.partners()[side42][k] >= 0}
        covered42 = _covered(rows42, every42, side42)
        unaccounted += sum(1 for k, t in enumerate(every42) if id(t) not in matched42 and k not in covered42)
    if unaccounted == 0 and len(furniture42) == 1 and "3 more copies" in furniture42[0].approved_text:
        print("PASS: every word of both documents is matched or in a row; the 3 header repeats are one row.")
    else:
        print(f"FAIL: {unaccounted} words unaccounted; furniture rows {[r.approved_text for r in furniture42]}")
        ok42 = False

    # A word the comparison leaves out of every row is given a row of its own.
    ta42 = build_comparison_tokens(lines_to_words(["take the tablets with water"]))
    tb42 = build_comparison_tokens(lines_to_words(["take the capsules with milk"]))
    partial = _TM42(ta42, tb42, [(ta42[0], tb42[0]), (ta42[1], tb42[1]), (ta42[3], tb42[3])])
    safety = unaccounted_rows([], partial, 0)  # no rows at all: the four unpaired words are unaccounted
    texts42 = sorted((r.kind, r.approved_text or r.suspect_text) for r in safety)
    want42 = [("added_in_suspect", "capsules"), ("added_in_suspect", "milk"),
              ("removed_in_suspect", "tablets"), ("removed_in_suspect", "water")]
    if texts42 == want42 and all(r.severity == "critical" for r in safety):
        print("PASS: words neither matched nor listed become rows of their own.")
    else:
        print(f"FAIL: safety-net rows {texts42}")
        ok42 = False

    # A short match is confirmed (green) only in a small gap between solid
    # matches: "tablet" between one-word edits is; "and" shared by two
    # different sentences is a coincidence -- part of the change it sits in.
    head42, tail42 = "Store the tablets below 25 C in a dry place.", "Keep out of reach of children at all times."
    dense_a = build_comparison_tokens(lines_to_words([head42, "take one tablet twice daily with food.", tail42]))
    dense_b = build_comparison_tokens(lines_to_words([head42, "take ONE tablet daily with food!", tail42]))
    found_d: list = []
    rows_d = diff_documents(dense_a, dense_b, found_d)
    map_d = _TM42(dense_a, dense_b, found_d)
    tablet_d = next(k for k, t in enumerate(dense_a) if t.text == "tablet")
    far_a = build_comparison_tokens(lines_to_words([
        head42, "Swallow each tablet whole with a full glass of water and never chew or crush it during treatment", tail42]))
    far_b = build_comparison_tokens(lines_to_words([
        head42, "Dissolve the contents of one sachet completely in juice and drink the mixture straight after preparing it", tail42]))
    found_f: list = []
    rows_f = diff_documents(far_a, far_b, found_f)
    map_f = _TM42(far_a, far_b, found_f)
    and_f = next(k for k, t in enumerate(far_a) if t.text == "and")
    extra_f = unaccounted_rows(rows_f, map_f, len(rows_f))
    and_listed = any("and" in r.approved_text.split() for r in rows_f + extra_f)
    paired_and = map_f.partners()["approved"][and_f] >= 0
    if (map_d.confirmed("approved", tablet_d) and not map_f.confirmed("approved", and_f)
            and and_listed and (not paired_and or not map_f.confirmed("suspect", map_f.partners()["approved"][and_f]))):
        print("PASS: a short match between small edits is confirmed; a coincidental one is listed, not green.")
    else:
        print(f"FAIL: tablet confirmed {map_d.confirmed('approved', tablet_d)}, 'and' confirmed "
              f"{map_f.confirmed('approved', and_f)} paired {paired_and} listed {and_listed}")
        ok42 = False
    if ok42:
        print("PASS: every word is matched or listed.")

    print()
    print("=" * 70)
    print("TEST 43: A missing line is marked where it belongs in the other document")
    print("=" * 70)
    ok43 = True
    from core.completeness import place_insertion_points
    from core.token_map import TokenMap as _TM43

    # The contents list sits on page 4 of the Original and page 1 of the
    # Suspect, at a different height: the same coordinates on the Suspect
    # are unrelated text. "2.2 ..." is missing from the Suspect.
    contents_a = ["1 INDICATIONS AND USAGE", "2 DOSAGE AND ADMINISTRATION",
                  "2.1 Recommended Dosage in Adults", "2.2 Recommended Dosage in Pediatric Patients",
                  "3 DOSAGE FORMS AND STRENGTHS", "4 CONTRAINDICATIONS"]
    contents_b = [line for line in contents_a if not line.startswith("2.2")]
    words_a43 = lines_to_words(["Hearing loss has been reported in patients taking this medicine."] * 6 + contents_a, page=4)
    words_b43 = lines_to_words(contents_b + ["Hearing loss has been reported in patients taking this medicine."] * 6, page=1)
    ta43, tb43 = build_comparison_tokens(words_a43), build_comparison_tokens(words_b43)
    found43: list = []
    rows43 = diff_documents(ta43, tb43, found43)
    map43 = _TM43(ta43, tb43, found43)
    missing43 = [d for d in rows43 if d.kind == "removed_in_suspect" and "Pediatric" in d.approved_text]
    for d in missing43:
        d.suspect_caret = None  # as the rows the worker adds afterwards arrive
    place_insertion_points(rows43, map43)
    adults_b = next(w for w in words_b43 if w.text == "Adults")
    if (len(missing43) == 1 and missing43[0].suspect_caret is not None
            and missing43[0].suspect_caret.page == 1
            and abs(missing43[0].suspect_caret.bbox[1] - adults_b.top) < 1
            and missing43[0].suspect_caret.bbox[0] >= adults_b.x1 - 1):
        print("PASS: a line missing from the Suspect is marked right after the line before it there.")
    else:
        caret43 = missing43[0].suspect_caret if missing43 else None
        print(f"FAIL: missing line rows {[d.approved_text for d in missing43]}, caret "
              f"{caret43 and (caret43.page, caret43.bbox)}; 'Adults' at {(adults_b.page, adults_b.x1, adults_b.top)}")
        ok43 = False

    # Nothing confirmed before it: the mark goes right before the partner
    # of the first confirmed match after it.
    lead_a = build_comparison_tokens(lines_to_words(["Draft labeling text"] + contents_b, page=2))
    lead_b = build_comparison_tokens(lines_to_words(contents_b, page=1))
    found43b: list = []
    rows43b = diff_documents(lead_a, lead_b, found43b)
    lead_rows = [d for d in rows43b if d.kind == "removed_in_suspect"]
    for d in lead_rows:
        d.suspect_caret = None
    place_insertion_points(rows43b, _TM43(lead_a, lead_b, found43b))
    first_b = lead_b[0]
    if (lead_rows and all(d.suspect_caret is not None and d.suspect_caret.page == 1
                          and abs(d.suspect_caret.bbox[1] - first_b.bbox[1]) < 1 for d in lead_rows)):
        print("PASS: a line missing at the very start is marked before the first matched line.")
    else:
        print(f"FAIL: leading line rows {[(d.approved_text, d.suspect_caret and d.suspect_caret.bbox) for d in lead_rows]}")
        ok43 = False
    if ok43:
        print("PASS: one-sided rows are marked where they belong in the other document.")

    print()
    print("=" * 70)
    print("TEST 44: Dashes typed into text are reported; ruled headings are not")
    print("=" * 70)
    ok44 = True
    from core.diff_engine import category_of as _category44

    # The four dashes typed into a Word draft (sample 2): "OF------" was
    # read as "OF" and matched; the others were minor "Hyphenation".
    plain44 = ["HIGHLIGHTS OF PRESCRIBING INFORMATION",
               "These highlights do not include all the information needed to use RIVAROXABAN",
               "Initial U.S. Approval: 2011",
               "WARNING: PREMATURE DISCONTINUATION OF RIVAROXABAN INCREASES THE RISK"]
    typed44 = ["HIGHLIGHTS OF PRESCRIBING INFORMATION-",
               "These highlights do not include all the information needed to use-- RIVAROXABAN",
               "Initial U.S. Approval: 2011-",
               "WARNING: PREMATURE DISCONTINUATION OF------ RIVAROXABAN INCREASES THE RISK"]
    rows44 = diff_documents(build_comparison_tokens(lines_to_words(plain44)),
                            build_comparison_tokens(lines_to_words(typed44)))
    got44 = sorted((d.approved_text, d.suspect_text, d.severity, _category44(d)) for d in rows44)
    want44 = sorted([("INFORMATION", "INFORMATION-", "major", "punctuation"),
                     ("use", "use--", "major", "punctuation"),
                     ("2011", "2011-", "major", "punctuation"),
                     ("OF", "OF------", "major", "punctuation")])
    if got44 == want44:
        print("PASS: dashes typed after words are major punctuation rows, not minor hyphenation.")
    else:
        print(f"FAIL: typed dashes: {got44}")
        ok44 = False

    # A dash typed at a line end, before a lowercase word, looks like a
    # word broken in two -- but "INFORMATIONneeded" is printed nowhere and
    # "INFORMATION needed" is: the dash is text, the same change.
    broke44 = diff_documents(build_comparison_tokens(lines_to_words(["OF PRESCRIBING INFORMATION", "needed to use it"])),
                             build_comparison_tokens(lines_to_words(["OF PRESCRIBING INFORMATION-", "needed to use it"])))
    if [(d.approved_text, d.suspect_text) for d in broke44] == [("INFORMATION needed", "INFORMATION-needed")]:
        print("PASS: a dash typed at a line end before a lowercase word is reported, not read as a word break.")
    else:
        print(f"FAIL: dash at line end: {[(d.approved_text, d.suspect_text, d.severity, _category44(d)) for d in broke44]}")
        ok44 = False

    # A sign put in front of a number changes its value.
    sign44 = diff_documents(build_comparison_tokens(lines_to_words(["mean change 1.4 from baseline"])),
                            build_comparison_tokens(lines_to_words(["mean change -1.4 from baseline"])))
    if [(d.severity, _category44(d)) for d in sign44] == [("critical", "punctuation")]:
        print("PASS: '1.4' -> '-1.4' is critical.")
    else:
        print(f"FAIL: sign: {[(d.approved_text, d.suspect_text, d.severity, _category44(d)) for d in sign44]}")
        ok44 = False

    # The same rule set apart from its word is the same change.
    spaced44 = diff_documents(build_comparison_tokens(lines_to_words(["DISCONTINUATION OF RIVAROXABAN INCREASES"])),
                              build_comparison_tokens(lines_to_words(["DISCONTINUATION OF ------ RIVAROXABAN INCREASES"])))
    if [(d.severity, d.suspect_text) for d in spaced44] == [("major", "------")]:
        print("PASS: a dash run typed apart from the word is a major row too.")
    else:
        print(f"FAIL: spaced dash run: {[(d.kind, d.severity, d.suspect_text) for d in spaced44]}")
        ok44 = False

    # Rules framing a heading -- any length, solid or spaced, a heading
    # wrapped over two lines, a rule alone on its line -- are still left out.
    ruled_a = ["------------- DRUG INTERACTIONS -------------", "Avoid combined P-gp inhibitors (7.2)",
               "------ DOSAGE FORMS AND", "STRENGTHS ------", "Tablets: 5 mg", "----------------------------"]
    ruled_b = ["---DRUG INTERACTIONS---", "Avoid combined P-gp inhibitors (7.2)",
               "-----------DOSAGE FORMS AND STRENGTHS-----------", "Tablets: 5 mg"]
    if not diff_documents(build_comparison_tokens(lines_to_words(ruled_a)),
                          build_comparison_tokens(lines_to_words(ruled_b))):
        print("PASS: rules framing headings are left out, whatever their length.")
    else:
        print("FAIL: ruled headings gave rows")
        ok44 = False
    if ok44:
        print("PASS: dashes are compared as text everywhere but around ruled headings.")

    print()
    print("=" * 70)
    print("TEST 45: Two versions of an FDA label (RLD 2017 / 2024) -- reading and pairing")
    print("=" * 70)
    ok45 = True
    from core.diff_engine import is_formatting_extra as _extra45
    from core.mupdf_page import _char_text
    from core.page_furniture import drop_repeated_furniture as _drop45
    from core.pdf_extractor import _detect_column_bands as _bands45, _merge_label_strips

    # (a) Three running lines stacked at the top of every page (a stamp
    # over a second stamp line over "Page N of M"), and a footer BOTH
    # documents repeat, their pages breaking at different words: every
    # repeat set aside on both sides, each first copy kept whole.
    def stamped(pages, words_per_page, footer):
        body = ["w" + chr(97 + k % 26) + chr(97 + k // 26 % 26) for k in range(pages * words_per_page)]
        out = []
        for page in range(1, pages + 1):
            lines = ["This label may not be the latest approved by FDA.",
                     "For current labeling information, please visit fda.gov",
                     f"Page {page} of {pages}"] if footer == "Reference ID: 4198886" else [f"Page {page} of {pages}"]
            chunk = body[(page - 1) * words_per_page: page * words_per_page]
            lines += [" ".join(chunk[i:i + 8]) for i in range(0, len(chunk), 8)] + [footer]
            for row, line in enumerate(lines):
                x = 60.0
                for text in line.split():
                    out.append(Word(text=text, page=page, x0=x, top=20.0 + row * 14, x1=x + 6 * len(text),
                                    bottom=32.0 + row * 14))
                    x += 6 * len(text) + 4
        return build_comparison_tokens(out)

    old45, new45 = stamped(6, 40, "Reference ID: 4198886"), stamped(5, 48, "Reference ID: 5405221")
    kept_old, found_old = _drop45(old45, new45)
    kept_new, found_new = _drop45(new45, old45)
    text_old, text_new = " ".join(t.text for t in kept_old), " ".join(t.text for t in kept_new)
    rows45 = diff_documents(kept_old, kept_new)
    stacked_ok = (
        len(found_old) == 4 and len(found_new) == 2
        and text_old.count("Reference ID") == 1 and text_new.count("Reference ID") == 1
        and "This label may not be the latest approved by FDA." in text_old
        and not any(t.startswith("w") and len(t) == 3 for d in rows45 for t in (d.approved_text + " " + d.suspect_text).split())
    )
    if stacked_ok:
        print("PASS: stacked running lines and a footer both repeat are set aside; the body compares clean.")
    else:
        print(f"FAIL: furniture {found_old} / {found_new}; rows {[(d.approved_text[:30], d.suspect_text[:30]) for d in rows45]}")
        ok45 = False

    # (b) Section numbers hanging left of a Contents list are not a column.
    def row_of(*spans):
        return [{"text": t, "x0": x0, "x1": x1, "top": 0.0, "bottom": 10.0} for t, x0, x1 in spans]

    toc_rows = [row_of((n, 36, 40.5), ("HEADING", 54, 120), ("7.4", 342, 353), ("Drugs", 360, 400)) for n in "1234567"]
    sign_rows = [row_of(("Operator/Dev.", 466, 494), ("Name", 140, 200), ("Signed", 510, 600)) for _ in range(5)]
    merged_toc = _merge_label_strips([(10.0, 42.0), (52.0, 289.0), (323.0, 523.0)], toc_rows)
    kept_sign = _merge_label_strips([(130.0, 454.0), (465.0, 495.0), (501.0, 608.0)], sign_rows)
    if len(merged_toc) == 2 and merged_toc[0][0] == 10.0 and len(kept_sign) == 3:
        print("PASS: a strip of section numbers joins its headings; a narrow strip of words stays apart.")
    else:
        print(f"FAIL: label strips {merged_toc} / {kept_sign}")
        ok45 = False

    # (c) An arrow written as a Symbol-font private-use code is the arrow.
    if (_char_text(0xF0AF, "SymbolMT"), _char_text(0xF0AD, "Symbol"), _char_text(0xF0AF, "Wingdings")) == (
            "↓", "↑", ""):
        print("PASS: Symbol-font arrows read as the arrows they draw (Wingdings left alone).")
    else:
        print("FAIL: Symbol-font characters not mapped")
        ok45 = False

    # (d) A soft hyphen drawn on its own is no (empty) word.
    soft = os.path.join(tmp, "t45_soft.pdf")
    doc45 = fitz.open()
    page45 = doc45.new_page(width=300, height=200)
    page45.insert_text((40, 60), "soluble in N,N", fontsize=10)
    page45.insert_text((112, 60), "­", fontsize=10, fontname="cour")
    page45.insert_text((40, 74), "dimethylformamide and water", fontsize=10)
    doc45.save(soft)
    doc45.close()
    if all(w.text.strip() for w in extract_words(soft)):
        print("PASS: a soft hyphen drawn on its own makes no empty word.")
    else:
        print("FAIL: an empty word was read")
        ok45 = False

    # (e) A cross-reference that recurs everywhere is not "moved" across
    # the document: the sentence it closes, deleted, is reported whole.
    filler = lambda tag: [f"{tag} filler line number {w} of the body text here" for w in
                          ("one", "two", "three", "four", "five", "six", "seven", "eight")]
    xref = "[see Boxed Warning and Warnings and Precautions (5.1)]."
    a45 = filler("a") + [f"Fluid retention may lead to heart failure {xref}"] + filler("b") + \
        [f"Monitor patients for signs of failure {xref}"]
    b45 = [f"Reduce the dose in patients with edema {xref}"] + filler("a") + filler("b") + \
        [f"Monitor patients for signs of failure {xref}"]
    rows45e = diff_documents(build_comparison_tokens(lines_to_words(a45)), build_comparison_tokens(lines_to_words(b45)))
    deleted45 = " ".join(d.approved_text for d in rows45e if d.kind == "removed_in_suspect")
    if "Fluid retention" in deleted45 and "Boxed Warning" in deleted45:
        print("PASS: a recurring cross-reference is not paired across the document.")
    else:
        print(f"FAIL: boilerplate moved: {[(d.kind, d.approved_text[:40], d.suspect_text[:40]) for d in rows45e]}")
        ok45 = False

    # (f) A full-width line in a 3-line block is not cut into columns at the
    # space a short line and a page number leave free.
    line1 = [(t, 89 + 40 * i, 89 + 40 * i + 36) for i, t in enumerate("Acute or chronic metabolic acidosis including diabetic ketoacidosis see Warnings and".split())]
    block = [row_of(("Page", 512, 531), ("7", 533, 538), ("of", 540, 547), ("40", 549, 558)), row_of(*line1),
             row_of(("Precautions", 89, 153), ("(5.2)].", 156, 190))]
    if len(_bands45(block, 89.0, 558.0, min_gutter=12.0)) == 1:
        print("PASS: a full-width line in a short block stays one line.")
    else:
        print(f"FAIL: short block split: {_bands45(block, 89.0, 558.0, min_gutter=12.0)}")
        ok45 = False
    if ok45:
        print("PASS: the label-version fixes hold.")

    print()
    print("=" * 70)
    print("TEST 46: Words two rewritten passages share belong to the change")
    print("=" * 70)
    ok46 = True
    from core.completeness import unaccounted_rows as _unacc46
    from core.diff_engine import CATEGORY_LABELS as _labels46
    from core.token_map import TokenMap as _TM46
    head46 = "Store the tablets below 25 C in a dry place away from light."
    tail46 = "Keep out of reach of children at all times and in the carton."
    a46 = build_comparison_tokens(lines_to_words([
        head46, "Patients with severe heart failure must stop treatment immediately", tail46]))
    b46 = build_comparison_tokens(lines_to_words([
        head46, "Doctors should check whether heart failure develops during long courses", tail46]))
    found46: list = []
    rows46 = diff_documents(a46, b46, found46)
    map46 = _TM46(a46, b46, found46)
    rows46 = rows46 + _unacc46(rows46, map46, len(rows46))
    whole_a = any("severe heart failure must" in d.approved_text for d in rows46)
    whole_b = any("whether heart failure develops" in d.suspect_text for d in rows46)
    if whole_a and whole_b and "unconfirmed" not in _labels46 and all(d.kind != "unconfirmed_match" for d in rows46):
        print("PASS: 'heart failure' shared by two rewritten sentences is part of the change, not a row apart.")
    else:
        print(f"FAIL: shared words: {[(d.kind, d.approved_text, d.suspect_text) for d in rows46]}")
        ok46 = False
    # A click goes where the comparison paired the word -- its confirmed
    # partner -- and nowhere else: "heart failure", paired by chance and
    # part of the change, has no click target; every green word has its own.
    jumps = [map46.counterpart("approved", k)[0] for k in range(len(a46))]
    paired = [map46.partners()["approved"][k] if map46.confirmed("approved", k) else None for k in range(len(a46))]
    heart = next(k for k, t in enumerate(a46) if t.text == "heart")
    if jumps == paired and jumps[heart] is None:
        print("PASS: a click jumps only to the word the comparison matched, never by phrase.")
    else:
        print(f"FAIL: click targets differ from the comparison's pairs ('heart' -> {jumps[heart]})")
        ok46 = False

    print()
    print("=" * 70)
    print("TEST 47: An outsert's artwork box, its note, and an underlined header")
    print("=" * 70)
    # Three columns; a ruled box (product name, code, date) across the top
    # of columns 2-3, a note under it running across their gutter, and a
    # header underlined with typed underscores.
    sheet = os.path.join(tmp, "t47_outsert.pdf")
    doc47 = fitz.open()
    page47 = doc47.new_page(width=900, height=600)
    vocab = "alpha beta gamma delta epsilon zeta eta theta iota kappa".split()
    for c, (x0, _x1) in enumerate([(40, 290), (320, 570), (600, 850)]):
        for row in range(30):
            page47.insert_text((x0, (150 if c else 60) + row * 13),
                               f"col{c} line{row} " + " ".join(vocab[(row + c) % 5:(row + c) % 5 + 5]), fontsize=8)
    page47.draw_rect(fitz.Rect(320, 40, 850, 100), width=0.8)
    page47.draw_line((320, 70), (850, 70), width=0.8)
    page47.draw_line((585, 40), (585, 100), width=0.8)
    for x, y, text in ((325, 58, "PRODUCT NAME : Example Tablets"), (590, 58, "CODE : 1234567"),
                       (325, 88, "DATE : 01-01-2025"), (590, 88, "Approved By Quality")):
        page47.insert_text((x, y), text, fontsize=8)
    note47 = "Note: Pharma code and adjacent text must be visible on the folded leaflet in full view."
    page47.insert_text((320, 115), note47, fontsize=8)
    page47.insert_text((40, 20), "Final Package Insert", fontsize=9)
    page47.insert_text((38, 21), "_" * 30, fontsize=9)
    doc47.save(sheet)
    doc47.close()
    read47 = [w.text for w in extract_words(sheet)]
    box_first = read47[:3] == ["PRODUCT", "NAME", ":"] and read47.index("Quality") < read47.index("col1")
    note_whole = " ".join(read47[read47.index("Note:"):read47.index("Note:") + len(note47.split())]) == note47
    header_clean = {"Final", "Package", "Insert"} <= set(read47) and not any("_" in w for w in read47)
    if box_first and note_whole and header_clean:
        print("PASS: the box is read first, its note whole after it, the underlined header as words.")
    else:
        print(f"FAIL: box first {box_first}, note whole {note_whole}, header clean {header_clean}: {read47[:24]}")

    print()
    print("=" * 70)
    print("TEST 48: A Word draft against its merged two-sided outsert")
    print("=" * 70)
    ok48 = True
    from core.diff_engine import _is_script_only, category_of as _cat48
    from core.pdf_extractor import _read_lines as _read48
    from core.completeness import unaccounted_rows as _unacc48
    from core.token_map import TokenMap as _TM48

    def cell48(text, x, top, size=6.0):
        return {"text": text, "x0": x, "x1": x + size * 0.55 * len(text), "top": top, "bottom": top + size}

    # (a) A study number centred beside its three arms, each arm a whole row
    # (no ruling between them): read row by row, as the ruled table is.
    # The outsert's own words (x0, x1, top): lines 6.5pt apart, 6pt high.
    def line48(top, *cells):
        return [{"text": t, "x0": a, "x1": b, "top": top, "bottom": top + 6.0} for t, a, b in cells]

    arms = [
        line48(1035.2, ("1", 823.4, 826.4), ("Lurasidone", 847.5, 873.8), ("hydrochloride", 875.3, 907.9),
               ("(40", 909.4, 917.4), ("mg/day)*", 918.9, 941.6),
               ("54.2", 960.4, 970.9), ("(8.8)", 972.4, 983.9), ("-9.4", 1033.0, 1042.5), ("(1.6)", 1044.0, 1055.5),
               ("-5.6", 1097.5, 1107.0), ("(-9.8,-1.4)", 1108.5, 1133.0)),
        line48(1041.7, ("Lurasidone", 847.5, 873.8), ("hydrochloride", 875.3, 907.9), ("(120", 909.4, 920.4),
               ("mg/day)*", 921.9, 944.6), ("52.7", 960.4, 970.9),
               ("(7.6)", 972.4, 983.9), ("-11.0", 1031.5, 1044.0), ("(1.6)", 1045.5, 1057.0),
               ("-6.7", 1096.0, 1105.5), ("(-11.0,-2.5)", 1107.0, 1134.5)),
        line48(1048.2, ("Placebo", 847.5, 866.1), ("54.7", 960.4, 970.9), ("(8.1)", 972.4, 983.9),
               ("-3.8", 1033.0, 1042.5), ("(1.6)", 1044.0, 1055.5), ("--", 1113.2, 1117.2)),
    ]
    arms_read = " ".join(w["text"] for _top, words in _read48(arms, rtl=False) for w in words)
    if "54.2 (8.8) -9.4 (1.6) -5.6 (-9.8,-1.4) Lurasidone hydrochloride (120 mg/day)* 52.7" in arms_read:
        print("PASS: arms beside a centred study number are read row by row.")
    else:
        print(f"FAIL: arms read {arms_read!r}")
        ok48 = False

    # (b) A Contents entry two layouts read in another order is matched,
    # not a deletion and an insertion; a lone "and" is not.
    toc_a = ["1 INDICATIONS AND USAGE", "2.1 Schizophrenia", "Keep this list of sections close at hand",
             "5 WARNINGS", "Read the whole leaflet before you start"]
    toc_b = ["1 INDICATIONS AND USAGE", "Keep this list of sections close at hand", "2.1 Schizophrenia",
             "5 WARNINGS", "Read the whole leaflet before you start"]
    ta48 = build_comparison_tokens(lines_to_words(toc_a))
    tb48 = build_comparison_tokens(lines_to_words(toc_b))
    found48: list = []
    rows48 = diff_documents(ta48, tb48, found48)
    map48 = _TM48(ta48, tb48, found48)
    rows48 = rows48 + _unacc48(rows48, map48, len(rows48))
    if not any("Schizophrenia" in d.approved_text + d.suspect_text for d in rows48):
        print("PASS: a Contents entry read in another order is matched.")
    else:
        print(f"FAIL: contents entry listed: {[(d.kind, d.approved_text, d.suspect_text) for d in rows48]}")
        ok48 = False

    # (c) Subscripts written two ways are one; a lost subscript on letters is
    # minor; on a number it is not.
    one48 = diff_documents(build_comparison_tokens(lines_to_words(["receptor α_{2C} affinity"])),
                           build_comparison_tokens(lines_to_words(["receptor α₂ _{C} affinity"])))
    cmax48 = diff_documents(build_comparison_tokens(lines_to_words(["mean Cmax was higher"])),
                            build_comparison_tokens(lines_to_words(["mean Cₘₐₓ was higher"])))
    if (all(d.severity == "minor" and _cat48(d) == "spacing" for d in one48)
            and [(d.severity, _cat48(d)) for d in cmax48] == [("minor", "script")]
            and not _is_script_only("H₂O", "H2O")):
        print("PASS: one subscript written two ways is spacing; Cmax / C-max lowered is minor; H2O is not.")
    else:
        print(f"FAIL: scripts {[(d.severity, _cat48(d)) for d in one48]} / {[(d.severity, _cat48(d)) for d in cmax48]}")
        ok48 = False
    if ok48:
        print("PASS: a merged outsert reads and pairs like its draft.")

    print()
    print("=" * 70)
    print("TEST 49: Artwork text drawn twice, raised at full size, upside down, in turned panels")
    print("=" * 70)
    ok49 = True
    sheet49 = os.path.join(tmp, "t49_artwork.pdf")
    doc49 = fitz.open()
    page49 = doc49.new_page(width=600, height=800)
    for _ in range(2):  # a slug drawn twice, one copy on the other
        page49.insert_text((40, 40), "Job 815-02-2022.indd printed twice", fontsize=8)
    page49.insert_text((40, 80), "IMC > 30kg/m", fontsize=11)
    x49 = 40 + fitz.get_text_length("IMC > 30kg/m", fontsize=11)
    page49.insert_text((x49, 77), "2", fontsize=11)  # raised, not smaller
    page49.insert_text((x49 + fitz.get_text_length("2", fontsize=11), 80), ", triglycerides", fontsize=11)
    page49.insert_text((300, 140), "DRUG CAUTION keep away", fontsize=8, rotate=180)  # a foil row upside down
    for y49 in (300, 450, 600):  # three copies of a turned panel down the sheet
        page49.insert_text((60, y49), "It is rare when you first start", fontsize=6, rotate=90)
    doc49.save(sheet49)
    doc49.close()
    read49 = " ".join(w.text for w in extract_words(sheet49))
    checks49 = {
        "drawn twice read once": "Job 815-02-2022.indd printed twice" in read49 and read49.count("815-02-2022") == 1,
        "raised 2 a superscript": "30kg/m²," in read49,
        "upside down read the right way": "DRUG CAUTION keep away" in read49 and "NOITUAC" not in read49,
        "turned panels each whole": read49.count("It is rare when you first start") == 3,
    }
    if all(checks49.values()):
        print("PASS: drawn twice, raised, upside down and turned panels all read as printed.")
    else:
        print(f"FAIL: {[k for k, v in checks49.items() if not v]} -- read {read49!r}")
        ok49 = False

    print()
    print("=" * 70)
    print("TEST 50: Foil columns, table headers, letter-spaced cells, lost letters, artwork boxes")
    print("=" * 70)
    from core.mupdf_page import _join_letter_spaced
    from core.pdf_extractor import _aligned_edge_bands, _peel_spanning_rows, _read_lines, _split_into_zones

    def w50(text, x, top, h, cw=1.6):
        return {"text": text, "x0": x, "x1": x + cw * len(text), "top": top, "bottom": top + h}

    def line50(text, x, top, h, cw=1.6, gap=0.9):
        out = []
        for part in text.split():
            out.append(w50(part, x, top, h, cw))
            x = out[-1]["x1"] + gap
        return out

    # (a) A foil's two columns of tiny type between a title over both and
    #     an address line under them; (b) the same 2pt apart -- found by
    #     the right column's common left edge and its smaller type. Text of
    #     one size with a 2pt gap is not split.
    left50 = ["Each uncoated tablet", "Tamoxifen Citrate IP", "equivalent to 20 mg", "Dosage: As directed",
              "by the Physician", "Store in a cool place", "Protect from light."]
    right50 = ["KEEP OUT OF REACH", "SCHEDULE G DRUG", "It is dangerous to", "take this preparation",
               "except under medical", "supervision. Warning:", "To be sold against"]

    def foil50(right_x, right_h):
        rows = [line50("Tamoxifen Citrate Tablets IP 20 mg", 106, 70, 6.0, cw=3.0)]
        for k, (a, b) in enumerate(zip(left50, right50)):
            rows.append(line50(a, 106, 80 + 4.2 * k, 3.4) + line50(b, right_x, 80.3 + 4.2 * k, right_h))
        rows.append(line50("Verna Indl. Estate, Goa 403 722 INDIA", 127, 80 + 4.2 * 7, 3.4))
        return rows

    left_end50 = max(w["x1"] for row in foil50(300, 2.9)[1:-1] for w in row if w["x0"] < 200)
    wide50 = _peel_spanning_rows([foil50(left_end50 + 6.0, 2.9)])
    narrow50 = _peel_spanning_rows([foil50(left_end50 + 2.0, 2.9)])
    same_size50 = _aligned_edge_bands(foil50(left_end50 + 2.0, 3.4)[1:-1])
    checks50 = {
        "title, columns, address": [len(part) for part, _b in wide50] == [1, 7, 1]
        and len(wide50[1][1] or []) == 2,
        "2pt gutter by edge and size": [len(part) for part, _b in narrow50] == [1, 7, 1]
        and len(narrow50[1][1] or []) == 2,
        "one size is not split": same_size50 is None,
    }

    # (c) A table header -- unruled, stacked line by line, over the label
    #     column too -- reads column by column, as ruled cells do; its data
    #     rows stay rows, and a dose row is not a section heading.
    def cells50(cells, top, h=9.0):
        out = []
        for text, x in cells:
            out += line50(text, x, top, h, cw=5.0, gap=3.0)
        return out

    def read50(lines):
        return [" ".join(w["text"] for w in words) for _top, words in _read_lines(lines, rtl=False)]

    header50 = [
        cells50([("Laboratory", 84), ("Placebo", 155), ("Lurasidone", 230), ("Lurasidone", 330)], 198),
        cells50([("Parameter", 84), ("(N=708)", 155), ("hydrochloride", 222), ("hydrochloride", 322)], 211),
        cells50([("20", 250), ("40", 350)], 224),
        cells50([("mg/day", 240), ("mg/day", 340)], 237),
        cells50([("(N=71)", 240), ("(N=487)", 338)], 250),
        cells50([("Serum", 84)], 264),
        cells50([("Creatinine", 84), ("2%", 165), ("1%", 250), ("2%", 350)], 277),
    ]
    data50 = [
        cells50([("Body System", 84), ("Placebo", 155), ("20 mg", 230), ("40 mg", 300)], 100),
        cells50([("Nausea", 84), ("5", 165), ("11", 250), ("10", 320)], 120),
        cells50([("Vomiting", 84), ("3", 165), ("4", 250), ("5", 320)], 129),
        cells50([("Dizziness", 84), ("2", 165), ("6", 250), ("7", 320)], 138),
    ]
    shifts50 = [
        cells50([("Total Cholesterol", 75), ("5.3%", 168), ("13.8%", 228), ("6.2%", 297)], 453),
        cells50([("(≥240 mg/dL)", 80), ("(30/571)", 162), ("(8/58)", 228), ("(25/402)", 292)], 462.5),
        cells50([("Triglycerides", 81), ("10.1%", 166), ("14.3%", 228), ("10.8%", 295)], 472),
        cells50([("(≥200 mg/dL)", 80), ("(53/526)", 162), ("(7/49)", 228), ("(41/379)", 292)], 481.5),
    ]
    doses50 = [cells50([("Lurasidone hydrochloride", 230)], 90),
               cells50([("20", 250), ("40", 320), ("80", 390)], 100),
               cells50([("mg/day", 240), ("mg/day", 310), ("mg/day", 380)], 110)]
    checks50.update({
        "header by columns": " ".join(read50(header50)) == (
            "Laboratory Parameter Placebo (N=708) Lurasidone hydrochloride 20 mg/day (N=71) "
            "Lurasidone hydrochloride 40 mg/day (N=487) Serum Creatinine 2% 1% 2%"),
        "data rows stay rows": read50(data50)[1:] == ["Nausea 5 11 10", "Vomiting 3 4 5", "Dizziness 2 6 7"],
        "fractions are data": len(read50(shifts50)) == 4,
        "dose row no heading": len(_split_into_zones(doses50)) == 1,
    })

    # (d) A Word cell set to "distributed" reads as its word; atom labels
    #     of a structure stay apart.
    spread50 = [w50(ch, 100 + 6 * k, 50, 8.0, cw=3.0) for k, ch in enumerate("Données")]
    atoms50 = [w50(ch, 100 + 6 * k, 80, 8.0, cw=3.0) for k, ch in enumerate("OONHS")]
    checks50["letter-spaced joined"] = (
        [w["text"] for w in _join_letter_spaced(spread50)] == ["Données"]
        and len(_join_letter_spaced(atoms50)) == 5
    )

    # (e) A text layer that drops "ll" everywhere: a short word losing it
    #     ("well" / "wel") is rated like the long ones, as noise -- but
    #     only on that evidence.
    def words50(text):
        out, x = [], 40.0
        for part in text.split():
            out.append(Word(text=part, page=1, x0=x, top=40, x1=x + 6 * len(part), bottom=52))
            x += 6 * len(part) + 4
        return out

    loss_rows50 = diff_documents(
        build_comparison_tokens(words50("The patient felt well and the hallucinations stopped. A full supply "
                                        "will follow. The parallel trials collapsed early. Allergic reactions were rare.")),
        build_comparison_tokens(words50("The patient felt wel and the halucinations stopped. A ful supply "
                                        "wil follow. The paralel trials colapsed early. Alergic reactions were rare.")),
    )
    lone_rows50 = diff_documents(build_comparison_tokens(words50("The patient felt well today.")),
                                 build_comparison_tokens(words50("The patient felt wel today.")))
    checks50["lost letters one rating"] = (
        len(loss_rows50) == 7 and {r.severity for r in loss_rows50} == {"typo"}
        and [r.severity for r in lone_rows50] == ["major"]
    )

    # (f) An outsert's artwork box against the draft: the box is removed
    #     on its own, not "changed" into the draft's next words.
    box50 = "PRODUCT NAME : Lurasidone Tablets COUNTRY : US ITEM : Outsert"
    art50 = words50(box50 + " FDA-Approved Patient Labeling Read this Medication Guide")
    for w in art50[:len(box50.split())]:
        w.artwork = True
    art_rows50 = diff_documents(
        build_comparison_tokens(art50),
        build_comparison_tokens(words50("Medication Guide FDA-Approved Patient Labeling Read this Medication Guide")),
    )
    checks50["artwork box removed"] = [(r.kind, r.approved_text, r.suspect_text) for r in art_rows50] == [
        ("removed_in_suspect", box50, ""), ("added_in_suspect", "", "Medication Guide")]
    if all(checks50.values()):
        print("PASS: foil columns, stacked headers, spread letters, lost letters and artwork boxes all read right.")
    else:
        print(f"FAIL: {[k for k, v in checks50.items() if not v]}")

    print()
    print(f"(test PDFs written to {tmp} if you want to open them manually)")


class _FailCounter:
    """stdout wrapper that counts lines starting with "FAIL", so the
    suite's exit code reflects every check without threading a flag
    through each one."""

    def __init__(self, stream) -> None:
        self._stream = stream
        self.failures = 0
        self._at_line_start = True

    def write(self, text: str) -> int:
        for line in text.splitlines(keepends=True):
            if self._at_line_start and line.startswith("FAIL"):
                self.failures += 1
            self._at_line_start = line.endswith("\n")
        return self._stream.write(text)

    def flush(self) -> None:
        self._stream.flush()


if __name__ == "__main__":
    counter = _FailCounter(sys.stdout)
    sys.stdout = counter
    try:
        main()
    finally:
        sys.stdout = counter._stream
    if counter.failures:
        print(f"\n{counter.failures} check(s) FAILED.")
        sys.exit(1)
    print("\nAll checks passed.")
