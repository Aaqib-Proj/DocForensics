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
          front is reported as ONE low-severity "reordered_section"
          instead of critical removed/added noise, while genuine edits
          inside it stay critical; identical / deleted / header-less
          documents behave as before.
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

from core.diff_engine import _match_sections, _split_sections, diff_documents
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
    print("        -> demoted to 'reorder', never a false 'critical' pair")
    print("=" * 70)
    ok7 = True

    def w(text: str, top: float) -> Word:
        return Word(text=text, page=1, x0=0.0, top=top, x1=10.0, bottom=top + 10.0)

    # 'epsilon' exists once on each side, just at a different position
    # (a wrap/order artifact). The diff engine must pair the removed +
    # added copies into the lowest 'reorder' tier instead of flagging
    # two critical disappearances/appearances.
    reorder_approved = build_comparison_tokens(
        [w(t, i * 20.0) for i, t in enumerate(["alpha", "beta", "gamma", "delta", "epsilon"])]
    )
    reorder_suspect = build_comparison_tokens(
        [w(t, i * 20.0) for i, t in enumerate(["alpha", "beta", "epsilon", "gamma", "delta"])]
    )
    reorder_diffs = diff_documents(reorder_approved, reorder_suspect)
    reordered = [d for d in reorder_diffs if d.kind == "reordered"]
    criticals = [d for d in reorder_diffs if d.severity == "critical"]
    if len(reordered) != 2 or any(d.approved_text != "epsilon" and d.suspect_text != "epsilon" for d in reordered):
        print(f"FAIL: moved 'epsilon' was not paired as two reorder items: {[(d.kind, d.approved_text, d.suspect_text) for d in reorder_diffs]}")
        ok7 = False
    elif criticals:
        print(f"FAIL: moved 'epsilon' still produced critical items: {[(d.kind, d.approved_text, d.suspect_text) for d in criticals]}")
        ok7 = False
    else:
        print("PASS: identical text in a different position is paired and demoted to 'reorder'.")

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
        print("PASS: reorder-pairing pass.")

    print()
    print("=" * 70)
    print("TEST 8: out-of-order section matching")
    print("        -> moved section = one 'reordered_section', not a cascade")
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

    # Scenario A: moved AND edited -> the edit stays critical, the move
    # is one reorder item, and NOTHING else (no removed/added noise).
    moved_edited_diffs = run_pipeline(approved_sec, moved_edited_sec)
    criticals = [d for d in moved_edited_diffs if d.severity == "critical"]
    moves = [d for d in moved_edited_diffs if d.kind == "reordered_section"]
    noise = [
        d for d in moved_edited_diffs
        if d.kind in ("removed_in_suspect", "added_in_suspect")
    ]
    ok_a = (
        len(moved_edited_diffs) == 2
        and len(criticals) == 1
        and criticals[0].approved_text == "especially"
        and criticals[0].suspect_text == "very"
        and len(moves) == 1
        and moves[0].approved_text == "3 WARNINGS AND PRECAUTIONS"
        and moves[0].severity == "reorder"
        and not noise
    )
    if ok_a:
        print("PASS: moved+edited section -> edit critical, move one 'reordered_section'.")
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

    # Scenario C (control): pure move, no edit -> one reorder item only.
    moved_diffs = run_pipeline(approved_sec, moved_sec)
    crit = [d for d in moved_diffs if d.severity == "critical"]
    if len(moved_diffs) == 1 and moved_diffs[0].kind == "reordered_section" and not crit:
        print("PASS: pure section move -> 1 'reordered_section', 0 criticals.")
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
    print("        -> layout-interleaved content is 'reorder', not critical")
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
    tail_diffs = diff_documents(ta_int, ta_ord)
    criticals = [d for d in tail_diffs if d.severity == "critical"]
    tail_reorders = [
        d for d in tail_diffs
        if d.kind == "reordered" and "eta theta iota kappa" in d.approved_text + d.suspect_text
    ]
    ok_b = (
        b_to_a.get(2) == 2  # suspect 2 DOSAGE -> approved 2 DOSAGE, NOT 5
        and b_to_a.get(3) == 5  # suspect 3 DOSAGE -> approved 5 (3 DOSAGE, last in stream)
        and b_to_a.get(4) == 3  # suspect 4 -> approved 4 CONTRAINDICATION
        and b_to_a.get(5) == 4  # suspect 5 -> approved 5 WARNINGS
        and not criticals
        and len(tail_reorders) == 2  # both sides of the relocated tail demoted
    )
    if ok_b:
        print("PASS: contaminated chunks still match 2<->2 / 5<->5; "
              "relocated tail demoted to 'reorder'.")
    else:
        print(f"FAIL: interleave: matches={b_to_a} criticals={[(d.kind, d.approved_text, d.suspect_text) for d in criticals]} "
              f"reorders={[(d.approved_text, d.suspect_text) for d in tail_reorders]}")
        ok9 = False

    if ok9:
        print("PASS: wrapped headers + title-anchored matching + relocation demotion.")

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
    # each section by its header text, and the reordered one must
    # demote to 'reordered_section' rather than a critical cascade.
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
    reordered_titles = (
        {d.approved_text for d in unnumbered_diffs if d.kind == "reordered_section"}
        if unnumbered_diffs
        else set()
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
    elif "ARBITRATION" not in reordered_titles:
        print(
            f"FAIL: expected 'ARBITRATION' to be reported as one reordered_section, "
            f"got reordered titles: {reordered_titles!r}"
        )
        ok14 = False
    else:
        print(
            "PASS: unnumbered headers ('CARDMEMBER AGREEMENT', 'INTEREST CHARGES', "
            "'ARBITRATION') matched by title across a different relative order, "
            "0 criticals, the moved section correctly demoted to 'reordered_section'."
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
        return {"text": text, "x0": x0, "x1": x1, "top": top}

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
    col_a_x0, col_b_x0 = 20.0, 208.0  # 8pt real gutter (200 -> 208)
    for i in range(5):
        top = zone2_top0 + i * 12.0
        zone_words.append(w15(f"A{i}", col_a_x0, col_a_x0 + 3, top))
        zone_words.append(w15(f"B{i}", col_b_x0, col_b_x0 + 3, top))

    units, bands = _order_body_units(zone_words, 20.0, 485.0)
    ordered: list[str] = []
    for band_idx in range(len(bands)):
        band_units = sorted((u for u in units if u[0] == band_idx), key=lambda u: u[1])
        for _, _, line_words in band_units:
            ordered.extend(ww["text"] for ww in line_words)

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
    rtl_zone_words: list[dict] = []
    for i in range(5):
        top = 10.0 + i * 12.0
        rtl_zone_words.append(w17(f"R{i}", 400.0, 410.0, top))  # rightmost
        rtl_zone_words.append(w17(f"M{i}", 200.0, 210.0, top))  # middle
        rtl_zone_words.append(w17(f"L{i}", 20.0, 30.0, top))    # leftmost
    rtl_units, rtl_bands = _order_body_units(rtl_zone_words, 20.0, 410.0, rtl=True)
    rtl_ordered: list[str] = []
    for band_idx in range(len(rtl_bands)):
        band_units = sorted((u for u in rtl_units if u[0] == band_idx), key=lambda u: u[1])
        for _, _, line_words in band_units:
            rtl_ordered.extend(ww["text"] for ww in line_words)
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
    moved_rows = [d for d in moved_diffs if d.kind == "reordered" and "kidney disease" in d.approved_text]
    ok20 = True
    if criticals or len(moved_rows) != 1:
        print(f"FAIL: moved section text: criticals={[(d.kind, d.approved_text[:40], d.suspect_text[:40]) for d in criticals]} "
              f"moved rows={len(moved_rows)}")
        ok20 = False
    else:
        print("PASS: text split into a mispaired section is one 'moved' row, 0 criticals.")
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

    toc_blocks = [
        {"page": 1, "bbox": (0, i * 14, 100, i * 14 + 12), "text": text}
        for i, text in enumerate([
            "Manufactured by:", "Acme Pharma, Plant 1.",
            "1 INDICATIONS AND USAGE", "Body text one.",
            "Manufactured by:", "Acme Pharma, Plant 2.",
        ])
    ]
    titles = [title for title, _ in build_section_list(toc_blocks)]
    if titles == ["Manufactured by:", "1 INDICATIONS AND USAGE", "Manufactured by:"]:
        print("PASS: a repeated header gives separate sections, in order.")
    else:
        print(f"FAIL: repeated header sections: {titles}")
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
        """The marked (changed) runs of one side."""
        return [text for changed, text in segments if changed]

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
    echoed = [d for d in toc_items if d.kind == "reordered" and d.approved_text == d.suspect_text != ""]
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
